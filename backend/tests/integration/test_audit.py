"""Auditoria append-only com cadeia de hash (contrato §4) — no PostgreSQL real."""
import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.audit import audit, verify_chain
from app.core.authz import AuthContext
from app.core.database import create_engine, session_factory
from app.models.audit import AuditLog
from tests.factories import login_as, make_org, make_user


async def _write(db, n=3, **kw):
    for i in range(n):
        await audit(db, AuthContext.system("worker"), f"test.event{i}", "thing", uuid.uuid4(),
                    before={"a": i}, after={"a": i + 1, "password": "x"}, **kw)
    await db.commit()


async def test_chain_and_redaction(db):
    await _write(db, 3)
    rows = (await db.execute(text("SELECT seq, prev_hash, row_hash, after_data FROM audit_logs ORDER BY seq"))).all()
    assert len(rows) == 3
    assert rows[0].prev_hash == "0" * 64
    assert rows[1].prev_hash == rows[0].row_hash and rows[2].prev_hash == rows[1].row_hash
    assert rows[0].after_data["password"] == "[REDACTED]"
    rep = await verify_chain(db)
    assert rep.ok and rep.rows_checked == 3


@pytest.mark.parametrize("stmt", ["UPDATE audit_logs SET action = 'x'", "DELETE FROM audit_logs", "TRUNCATE audit_logs"])
async def test_append_only_enforced_by_database(db, stmt):
    await _write(db, 1)
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text(stmt))
    assert "RD_APPEND_ONLY" in str(ei.value)
    await db.rollback()
    assert (await db.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one() == 1


async def test_tampering_is_detected_by_verify_chain(db):
    await _write(db, 3)
    # adulteração direta (como superusuário, contornando o trigger) é detectada pela cadeia
    await db.execute(text("SET LOCAL session_replication_role = replica"))
    await db.execute(text("UPDATE audit_logs SET reason = 'adulterado' WHERE seq = (SELECT min(seq)+1 FROM audit_logs)"))
    await db.commit()
    rep = await verify_chain(db)
    assert not rep.ok and rep.problem.startswith("row_hash")


@pytest.mark.concurrency
async def test_concurrent_writers_keep_a_single_linear_chain(db_engine):
    """20 transações concorrentes gravando auditoria: cadeia continua linear e válida."""

    async def writer(i):
        eng = create_engine(nullpool=True)
        try:
            async with session_factory(eng)() as s:
                await audit(s, AuthContext.system("worker"), "conc.write", "k", i)
                await asyncio.sleep(0.01)
                await s.commit()
        finally:
            await eng.dispose()

    await asyncio.gather(*(writer(i) for i in range(20)))
    async with session_factory(db_engine)() as s:
        rep = await verify_chain(s)
        assert rep.ok and rep.rows_checked == 20


async def test_audit_rejected_in_repeatable_read(db):
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    await audit(db, None, "x.y")
    with pytest.raises(DBAPIError) as ei:
        await db.flush()
    assert "RD_AUDIT_ISOLATION" in str(ei.value)
    await db.rollback()


async def test_audit_api_list_filter_scope_and_export(client, db):
    org_a, org_b = await make_org(db), await make_org(db)
    admin = await make_user(db, role="super_admin", mfa=True)
    s = await login_as(client, admin)
    await audit(db, None, "domain.thing", "thing", "1", org_id=org_a.id)
    await audit(db, None, "domain.thing", "thing", "2", org_id=org_b.id)
    await db.commit()
    r = await client.get("/api/v1/audit", params={"action": "domain.thing"}, headers=s.headers)
    assert r.status_code == 200 and r.json()["total"] == 2
    r = await client.get("/api/v1/audit", params={"action": "auth.*"}, headers=s.headers)
    assert r.json()["total"] >= 1  # o próprio login
    r = await client.get("/api/v1/audit/export", params={"action": "domain.thing"}, headers=s.headers)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].startswith("seq,created_at") and len(lines) == 3
    r = await client.get("/api/v1/audit/verify", headers=s.headers)
    assert r.json()["ok"] is True

    # usuário sem audit.view → 403
    emp = await make_user(db, role="employee")
    se = await login_as(client, emp)
    assert (await client.get("/api/v1/audit", headers=se.headers)).status_code == 403
    assert (await client.get("/api/v1/audit/export", headers=se.headers)).status_code == 403


async def test_every_row_has_actor_ip_request_id(client, db):
    u = await make_user(db, role="employee")
    await login_as(client, u)
    row = (await db.execute(text("SELECT actor_user_id, ip_address, request_id, origin FROM audit_logs "
                                 "WHERE action = 'auth.login'"))).one()
    assert row.actor_user_id == u.id and row.ip_address == "127.0.0.1" and row.request_id and row.origin == "api"
    assert (await db.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one() >= 1
    assert AuditLog.__tablename__ == "audit_logs"


async def test_audit_filter_impersonated_in_list_and_export(client_factory, db):
    """impersonated=true → só ações com usuário real registrado; false → só as demais; CSV idem."""
    from tests.factories import auth_headers, make_org

    org = await make_org(db)
    sa = await make_user(db, role="super_admin", mfa=True)
    target = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    c = client_factory()
    s = await login_as(c, sa)
    imp = (await c.post("/api/v1/auth/impersonation",
                        json={"user_id": str(target.id), "reason": "Suporte ao chamado #42 do cliente"},
                        headers=s.headers)).json()
    h = auth_headers(imp["access_token"])
    assert (await c.patch("/api/v1/me", json={"theme": "dark"}, headers=h)).status_code == 200
    assert (await c.patch("/api/v1/me", json={"theme": "light"}, headers=s.headers)).status_code == 200

    async def actions(params):
        r = await c.get("/api/v1/audit", params={"action": "user.profile_updated", "page_size": 100, **params},
                        headers=s.headers)
        assert r.status_code == 200, r.text
        return r.json()["items"]

    with_imp = await actions({"impersonated": "true"})
    assert len(with_imp) == 1
    assert with_imp[0]["actor_user_id"] == str(target.id) and with_imp[0]["real_user_id"] == str(sa.id)
    without = await actions({"impersonated": "false"})
    assert len(without) == 1 and without[0]["actor_user_id"] == str(sa.id) and without[0]["real_user_id"] is None
    assert len(await actions({})) == 2
    # filtro combinado com os demais (ator) e sem nenhum resultado
    assert await actions({"impersonated": "true", "actor_user_id": str(target.id)}) == with_imp
    r = await c.get("/api/v1/audit", params={"impersonated": "talvez"}, headers=s.headers)
    assert r.status_code == 422

    # todas as linhas com impersonated=true têm real_user_id; nenhuma com false
    all_true = (await c.get("/api/v1/audit", params={"impersonated": "true", "page_size": 100},
                            headers=s.headers)).json()["items"]
    assert all_true and all(i["real_user_id"] for i in all_true)
    all_false = (await c.get("/api/v1/audit", params={"impersonated": "false", "page_size": 100},
                             headers=s.headers)).json()["items"]
    assert all_false and not any(i["real_user_id"] for i in all_false)

    # CSV respeita o filtro
    import csv
    import io

    def csv_rows(params):
        return list(csv.DictReader(io.StringIO(params.content.decode("utf-8-sig"))))

    r = await c.get("/api/v1/audit/export", params={"action": "user.profile_updated", "impersonated": "true"},
                    headers=s.headers)
    assert r.status_code == 200
    rows = csv_rows(r)
    assert len(rows) == 1 and rows[0]["real_user_id"] == str(sa.id) and rows[0]["actor_user_id"] == str(target.id)
    r = await c.get("/api/v1/audit/export", params={"action": "user.profile_updated", "impersonated": "false"},
                    headers=s.headers)
    rows = csv_rows(r)
    assert len(rows) == 1 and rows[0]["real_user_id"] == "" and rows[0]["actor_user_id"] == str(sa.id)
