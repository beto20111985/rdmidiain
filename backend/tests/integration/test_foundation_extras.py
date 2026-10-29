"""Extras da fundação pedidos pelos domínios: coluna imutável, DDL de domínio, eventos registráveis,
anexos de e-mail (Mailpit real quando disponível), /health/ready."""
import socket
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core import events
from app.core.config import settings
from app.core.email import OutgoingEmail, SmtpEmailSender, build_mime
from app.db import ddl


async def test_immutable_column_trigger(db):
    await db.execute(text("CREATE TABLE tmp_imm (id int primary key, code text, name text)"))
    for stmt in ddl.immutable_columns_trigger("tmp_imm", ["code"]):
        await db.execute(text(stmt))
    await db.execute(text("INSERT INTO tmp_imm VALUES (1, 'RD-ABCDEF', 'a')"))
    await db.execute(text("UPDATE tmp_imm SET name = 'b' WHERE id = 1"))  # outras colunas: ok
    await db.commit()
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("UPDATE tmp_imm SET code = 'RD-ZZZZZZ' WHERE id = 1"))
    assert "RD_IMMUTABLE_COLUMN" in str(ei.value)
    await db.rollback()
    await db.execute(text("DROP TABLE tmp_imm"))
    await db.commit()


def test_domain_ddl_modules_registry(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("tests_fake_domain_ddl")
    mod.statements = lambda tables: ["SELECT 1", f"-- {sorted(tables or [])}"]
    sys.modules["tests_fake_domain_ddl"] = mod
    monkeypatch.setattr(ddl, "DOMAIN_DDL_MODULES", ["tests_fake_domain_ddl", "app.modules.nao_existe.ddl"])
    out = ddl.domain_statements({"a"})
    assert out == ["SELECT 1", "-- ['a']"]
    with pytest.raises(ModuleNotFoundError):
        ddl.domain_statements({"a"}, strict=True)
    assert 'CREATE EXTENSION IF NOT EXISTS "btree_gist"' in ddl.extension_statements()


async def test_btree_gist_available_in_test_db(db):
    n = (await db.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'btree_gist'"))).scalar_one()
    assert n == 1


def test_register_event_type():
    events.register_event_type("playlist.updated", "playlists.view")
    env = events.build_envelope("playlist.updated", {"x": 1}, org_id=uuid.uuid4())
    assert env["type"] == "playlist.updated"
    with pytest.raises(ValueError):
        events.register_event_type("playlist.updated", "screens.view")  # conflito
    with pytest.raises(ValueError):
        events.register_event_type("x.y", "nao.existe")
    for t in ("screen.updated", "establishment.updated", "screen.status", "screen.sync", "campaign.updated",
              "approval.pending", "payment.updated", "media.processing", "inventory.updated", "notification.created",
              "finance.approval_request", "withdrawal.updated", "ticket.updated", "designer.updated"):
        assert t in events.event_types()


def test_build_mime_with_attachments():
    msg = build_mime(OutgoingEmail(to="a@example.com", subject="Relatório", text="segue",
                                   attachments=[("relatorio.pdf", "application/pdf", b"%PDF-1.4 teste"),
                                                ("../evil\r\nname.csv", "text/csv", b"a,b\n1,2\n")]))
    parts = [p for p in msg.iter_attachments()]
    assert [p.get_filename() for p in parts] == ["relatorio.pdf", ".._evil__name.csv"]
    assert parts[0].get_content_type() == "application/pdf" and parts[0].get_content() == b"%PDF-1.4 teste"
    with pytest.raises(TypeError):
        build_mime(OutgoingEmail(to="a@example.com", subject="x", text="y", attachments=[("a", "text/plain", "str")]))


def _mailpit_up() -> bool:
    try:
        socket.create_connection(("mailpit", 1025), timeout=1).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _mailpit_up(), reason="Mailpit (dependência de infraestrutura de dev) indisponível")
async def test_smtp_sender_delivers_attachment_to_mailpit(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "mailpit")
    monkeypatch.setattr(settings, "SMTP_PORT", 1025)
    monkeypatch.setattr(settings, "SMTP_STARTTLS", False)
    monkeypatch.setattr(settings, "SMTP_USER", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    tag = uuid.uuid4().hex[:10]
    await SmtpEmailSender().send(OutgoingEmail(
        to="destino@example.com", subject=f"TEST_MIDIAINDOOR_002 anexo {tag}", text="corpo",
        attachments=[("comprovante.pdf", "application/pdf", b"%PDF-1.4 conteudo " + tag.encode())]))
    async with httpx.AsyncClient(base_url="http://mailpit:8025") as c:
        found = None
        for _ in range(20):
            r = await c.get("/api/v1/search", params={"query": f"subject:{tag}"})
            msgs = r.json().get("messages", [])
            if msgs:
                found = msgs[0]
                break
        assert found is not None, "mensagem não chegou ao Mailpit"
        full = (await c.get(f"/api/v1/message/{found['ID']}")).json()
        assert [a["FileName"] for a in full["Attachments"]] == ["comprovante.pdf"]
        await c.request("DELETE", "/api/v1/messages", json={"IDs": [found["ID"]]})  # limpeza


async def test_health_ready(client, db):
    from app.main import _alembic_heads

    heads = sorted(_alembic_heads())
    assert heads  # há head nas migrations
    existed = (await db.execute(text("SELECT to_regclass('alembic_version')"))).scalar() is not None
    if not existed:  # modo rápido (create_all): sem alembic_version → não pronto
        r = await client.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["checks"] == {"db": "ok", "redis": "ok", "migrations": "fail"}
        await db.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
        for h in heads:
            await db.execute(text("INSERT INTO alembic_version VALUES (:h)"), {"h": h})
        await db.commit()
    try:
        r = await client.get("/health/ready")
        assert r.status_code == 200, r.text
        assert r.json()["checks"] == {"db": "ok", "redis": "ok", "migrations": "ok"}
        if not existed:
            await db.execute(text("UPDATE alembic_version SET version_num = 'revisao_antiga'"))
            await db.commit()
            r = await client.get("/health/ready")
            assert r.status_code == 503 and r.json()["checks"]["migrations"] == "fail"
    finally:
        if not existed:
            await db.execute(text("DROP TABLE alembic_version"))
            await db.commit()
