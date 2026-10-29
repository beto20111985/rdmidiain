"""W-BIZ — Designer: fluxo briefing→fila→design→revisão→aprovação→publicação, 3 revisões
incluídas e 4ª bloqueada até revisão extra liberada, aprovação formal (IP/data/usuário/UA),
arte aprovada travada (API e trigger no banco), comentários internos × públicos, escopo."""
from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.storage import LocalStorageProvider, set_storage
from app.models.designer import DesignApproval, DesignRequest, DesignVersion
from app.modules.notifications import service as notif
from tests.factories import login_as, make_establishment, make_org, make_user
from tests.fin_helpers import fin_env, pay, payer_org  # noqa: F401  (fixture do W-FIN: provedor CloudPayPag FALSO)


@pytest.fixture(autouse=True)
async def _drain_after_test(clean_state):
    """Espera os envios pós-commit do notify antes do teardown (evita transação órfã)."""
    yield
    from app.modules.notifications.service import drain

    await drain()


@pytest.fixture
def storage(tmp_path):
    set_storage(LocalStorageProvider(tmp_path / "storage"))
    yield
    set_storage(None)


def png(w=1920, h=1080, color=(20, 80, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


async def _setup(client, db):
    org = await payer_org(db)
    client_user = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    designer = await make_user(db, role="designer")
    sc = await login_as(client, client_user)
    sd = await login_as(client, designer)
    return org, client_user, designer, sc, sd


async def _new_request(client, sc, sd, org) -> str:
    r = await client.post("/api/v1/design/requests", headers=sc.headers, json={
        "org_id": str(org.id), "title": "Promo de verão", "briefing": "Arte com logotipo e preço em destaque.",
        "texts": ["Leve 3 pague 2"], "orientation": "HORIZONTAL", "media_type": "IMAGEM", "duration_s": 30})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["status"] == "BRIEFING" and r.json()["code"].startswith("ART-")
    r = await client.post(f"/api/v1/design/requests/{rid}/quote", headers=sd.headers,
                          json={"billing": "NEGOCIADO", "price_cents": 0, "note": "Incluso no contrato"})
    assert r.status_code == 200 and r.json()["status"] == "NA_FILA", r.text
    r = await client.post(f"/api/v1/design/requests/{rid}/assign", headers=sd.headers, json={})
    assert r.status_code == 200 and r.json()["status"] == "EM_DESIGN"
    return rid


async def _upload_version(client, sd, rid, data=None, expect=201):
    r = await client.post(f"/api/v1/design/requests/{rid}/versions", headers=sd.headers,
                          files={"file": ("arte.png", data or png(), "image/png")}, data={"notes": "v"})
    assert r.status_code == expect, r.text
    return r.json()


async def test_three_revisions_then_fourth_blocked_until_extra_released(client, db, storage, mail, fin_env):
    org, cu, du, sc, sd = await _setup(client, db)
    rid = await _new_request(client, sc, sd, org)
    for i in range(1, 4):
        v = await _upload_version(client, sd, rid, png(color=(i * 40, 10, 10)))
        r = await client.post(f"/api/v1/design/requests/{rid}/versions/{v['id']}/decision", headers=sc.headers,
                              json={"decision": "SOLICITAR_ALTERACAO", "comment": f"Ajuste número {i}"})
        assert r.status_code == 200, r.text
        assert r.json()["revisions_used"] == i
    assert r.json()["revisions_remaining"] == 0
    v4 = await _upload_version(client, sd, rid, png(color=(1, 2, 3)))
    r = await client.post(f"/api/v1/design/requests/{rid}/versions/{v4['id']}/decision", headers=sc.headers,
                          json={"decision": "SOLICITAR_ALTERACAO", "comment": "Quarta alteração"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DESIGN_REVISION_LIMIT_REACHED"
    assert r.json()["error"]["details"] == {"included": 3, "extra": 0, "used": 3}
    # cliente foi avisado do limite (na 3ª revisão)
    await notif.drain()
    # revisão extra: cliente solicita → RD aprova (sem custo, com justificativa) → liberada
    r = await client.post(f"/api/v1/design/requests/{rid}/extra-revisions", headers=sc.headers, json={"reason": "Mudou o preço"})
    assert r.status_code == 201 and r.json()["status"] == "SOLICITADA", r.text
    xid = r.json()["id"]
    r = await client.post(f"/api/v1/design/extra-revisions/{xid}/decision", headers=sc.headers, json={"approve": True})
    assert r.status_code == 403  # cliente não aprova a própria revisão extra
    r = await client.post(f"/api/v1/design/extra-revisions/{xid}/decision", headers=sd.headers,
                          json={"approve": True, "price_cents": 0})
    assert r.status_code == 400 and r.json()["error"]["code"] == "NOTE_REQUIRED"
    r = await client.post(f"/api/v1/design/extra-revisions/{xid}/decision", headers=sd.headers,
                          json={"approve": True, "price_cents": 0, "note": "Cortesia comercial"})
    assert r.status_code == 200 and r.json()["status"] == "LIBERADA"
    r = await client.post(f"/api/v1/design/requests/{rid}/versions/{v4['id']}/decision", headers=sc.headers,
                          json={"decision": "SOLICITAR_ALTERACAO", "comment": "Quarta alteração (extra)"})
    assert r.status_code == 200 and r.json()["revisions_used"] == 4
    # contagem (inteiro) e lista de compras em campos SEPARADOS (bug FE-ADM: a lista sobrescrevia o inteiro)
    det = (await client.get(f"/api/v1/design/requests/{rid}", headers=sc.headers)).json()
    assert det["extra_revisions_count"] == 1 and det["extra_revisions"] == 1  # alias deprecado = mesmo inteiro
    assert [x["status"] for x in det["extra_revision_purchases"]] == ["LIBERADA"]
    lst = (await client.get("/api/v1/design/requests", headers=sc.headers)).json()["items"]
    assert lst[0]["extra_revisions_count"] == 1 and "extra_revision_purchases" not in lst[0]
    # cobrada: aprovação da RD com valor → cobrança REAL pelo financeiro (provedor falso de teste)
    v5 = await _upload_version(client, sd, rid, png(color=(5, 5, 5)))
    r = await client.post(f"/api/v1/design/requests/{rid}/versions/{v5['id']}/decision", headers=sc.headers,
                          json={"decision": "SOLICITAR_ALTERACAO", "comment": "Quinta sem pagar"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DESIGN_REVISION_LIMIT_REACHED"
    r = await client.post(f"/api/v1/design/requests/{rid}/extra-revisions", headers=sc.headers, json={})
    xid2 = r.json()["id"]
    r = await client.post(f"/api/v1/design/extra-revisions/{xid2}/decision", headers=sd.headers,
                          json={"approve": True, "price_cents": 5000})
    assert r.status_code == 200 and r.json()["status"] == "AGUARDANDO_PAGAMENTO", r.text
    from app.models.financial import PaymentOrder

    order = await db.get(PaymentOrder, uuid.UUID(r.json()["payment_order_id"]))
    assert order.amount_cents == 5000 and order.reference_type == "design_extra_revision"
    # sem pagamento a revisão continua bloqueada
    r = await client.post(f"/api/v1/design/requests/{rid}/versions/{v5['id']}/decision", headers=sc.headers,
                          json={"decision": "SOLICITAR_ALTERACAO", "comment": "Quinta"})
    assert r.status_code == 409
    assert await pay(db, order) in ("PROCESSED", "PAID", "OK", "processed")
    r = await client.post(f"/api/v1/design/requests/{rid}/versions/{v5['id']}/decision", headers=sc.headers,
                          json={"decision": "SOLICITAR_ALTERACAO", "comment": "Quinta (paga)"})
    assert r.status_code == 200 and r.json()["revisions_used"] == 5, r.text


async def test_formal_approval_records_ip_ua_user_and_locks_version(client, db, storage, mail):
    org, cu, du, sc, sd = await _setup(client, db)
    rid = await _new_request(client, sc, sd, org)
    v1 = await _upload_version(client, sd, rid)
    # designer não aprova pelo cliente
    r = await client.post(f"/api/v1/design/requests/{rid}/versions/{v1['id']}/decision", headers=sd.headers,
                          json={"decision": "APROVAR"})
    assert r.status_code == 403
    r = await client.post(f"/api/v1/design/requests/{rid}/versions/{v1['id']}/decision", headers=sc.headers,
                          json={"decision": "APROVAR", "comment": "Perfeito"})
    assert r.status_code == 200 and r.json()["status"] == "APROVADA", r.text
    appr = (await db.execute(select(DesignApproval).where(DesignApproval.version_id == uuid.UUID(v1["id"])))).scalar_one()
    assert appr.decision == "APROVADA" and appr.user_id == cu.id
    assert appr.ip_address == "127.0.0.1"
    assert appr.user_agent and "pytest-rd" in appr.user_agent
    assert appr.decided_at is not None and appr.version_sha256 == v1["sha256"]
    appr_id = appr.id
    detail = (await client.get(f"/api/v1/design/requests/{rid}", headers=sc.headers)).json()
    assert detail["approvals"][0]["ip_address"] == "127.0.0.1"
    assert detail["versions"][0]["locked"] is True

    # tentativa de alterar a arte aprovada pela API → 409 (alteração = nova versão)
    r = await client.patch(f"/api/v1/design/requests/{rid}/versions/{v1['id']}", headers=sd.headers, json={"notes": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DESIGN_VERSION_LOCKED"
    # e direto no banco (trigger) — UPDATE e DELETE proibidos
    for sql in ("UPDATE design_versions SET notes='hack' WHERE id=:i",
                "UPDATE design_versions SET storage_key='x/y' WHERE id=:i",
                "DELETE FROM design_versions WHERE id=:i"):
        with pytest.raises(DBAPIError) as exc:
            await db.execute(text(sql), {"i": v1["id"]})
        assert "RD_DESIGN_VERSION_LOCKED" in str(exc.value)
        await db.rollback()
    # aprovação é append-only
    with pytest.raises(DBAPIError):
        await db.execute(text("UPDATE design_approvals SET ip_address='1.1.1.1' WHERE id=:i"), {"i": appr_id})
    await db.rollback()

    # nova versão após aprovação → precisa de nova aprovação; a aprovada continua intacta
    v2 = await _upload_version(client, sd, rid, png(color=(9, 9, 9)))
    assert v2["number"] == 2 and v2["status"] == "EM_REVISAO" and v2["locked"] is False
    detail = (await client.get(f"/api/v1/design/requests/{rid}", headers=sc.headers)).json()
    assert detail["status"] == "EM_REVISAO"
    old = next(v for v in detail["versions"] if v["number"] == 1)
    assert old["status"] == "APROVADA" and old["locked"] is True and old["sha256"] == v1["sha256"]
    # publicar exige a nova aprovação
    r = await client.post(f"/api/v1/design/requests/{rid}/publish", headers=sd.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DESIGN_NOT_APPROVED"
    r = await client.post(f"/api/v1/design/requests/{rid}/versions/{v2['id']}/decision", headers=sc.headers,
                          json={"decision": "APROVAR"})
    assert r.status_code == 200 and r.json()["approved_version_id"] == v2["id"]
    n = (await db.execute(select(DesignApproval).where(DesignApproval.request_id == uuid.UUID(rid),
                                                       DesignApproval.decision == "APROVADA"))).scalars().all()
    assert len(n) == 2


async def test_publish_creates_media_through_media_pipeline(client, db, storage, mail):
    org, cu, du, sc, sd = await _setup(client, db)
    rid = await _new_request(client, sc, sd, org)
    v1 = await _upload_version(client, sd, rid)
    await client.post(f"/api/v1/design/requests/{rid}/versions/{v1['id']}/decision", headers=sc.headers,
                      json={"decision": "APROVAR"})
    r = await client.post(f"/api/v1/design/requests/{rid}/publish", headers=sd.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "PUBLICADA" and body["published_media_id"] == body["media"]["id"]
    from app.models.media import MediaFile

    m = await db.get(MediaFile, uuid.UUID(body["media"]["id"]))
    assert m is not None and m.org_id == org.id
    assert getattr(m, "origin", "DESIGN_ART") == "DESIGN_ART"


async def test_wrong_resolution_and_type_rejected(client, db, storage, mail):
    org, cu, du, sc, sd = await _setup(client, db)
    rid = await _new_request(client, sc, sd, org)
    r = await client.post(f"/api/v1/design/requests/{rid}/versions", headers=sd.headers,
                          files={"file": ("arte.png", png(1280, 720), "image/png")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "ART_RESOLUTION_INVALID"
    r = await client.post(f"/api/v1/design/requests/{rid}/versions", headers=sd.headers,
                          files={"file": ("arte.png", b"<html><script>alert(1)</script></html>", "image/png")})
    assert r.status_code == 415
    assert (await db.execute(select(DesignVersion).where(DesignVersion.request_id == uuid.UUID(rid)))).first() is None


async def test_comments_internal_hidden_and_attachments_scoped(client, db, storage, mail):
    org, cu, du, sc, sd = await _setup(client, db)
    rid = await _new_request(client, sc, sd, org)
    r = await client.post(f"/api/v1/design/requests/{rid}/comments", headers=sd.headers,
                          json={"body": "Cliente difícil — nota interna", "is_internal": True})
    assert r.status_code == 201
    r = await client.post(f"/api/v1/design/requests/{rid}/comments", headers=sd.headers,
                          json={"body": "Estamos produzindo", "is_internal": False})
    assert r.status_code == 201
    r = await client.post(f"/api/v1/design/requests/{rid}/comments", headers=sc.headers,
                          json={"body": "tentativa", "is_internal": True})
    assert r.status_code == 403
    cl = (await client.get(f"/api/v1/design/requests/{rid}/comments", headers=sc.headers)).json()["items"]
    assert [c["body"] for c in cl] == ["Estamos produzindo"]
    st = (await client.get(f"/api/v1/design/requests/{rid}/comments", headers=sd.headers)).json()["items"]
    assert len(st) == 2
    # anexo do briefing (logotipo PNG) e download
    r = await client.post(f"/api/v1/design/requests/{rid}/attachments", headers=sc.headers,
                          data={"kind": "LOGOTIPO"}, files={"file": ("logo.png", png(200, 100), "image/png")})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    r = await client.get(f"/api/v1/design/requests/{rid}/attachments/{aid}/file", headers=sc.headers)
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert r.headers["x-content-type-options"] == "nosniff"
    # executável disfarçado é recusado pelos magic bytes
    r = await client.post(f"/api/v1/design/requests/{rid}/attachments", headers=sc.headers,
                          data={"kind": "IMAGEM"}, files={"file": ("foto.png", b"MZ\x90\x00" + b"\x00" * 100, "image/png")})
    assert r.status_code == 415

    # outra organização: 404 em tudo
    other = await make_org(db)
    intruder = await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id))
    si = await login_as(client, intruder)
    for url in (f"/api/v1/design/requests/{rid}", f"/api/v1/design/requests/{rid}/comments",
                f"/api/v1/design/requests/{rid}/attachments/{aid}/file"):
        assert (await client.get(url, headers=si.headers)).status_code == 404
    lst = (await client.get("/api/v1/design/requests", headers=si.headers)).json()
    assert lst["total"] == 0
    r = await client.post("/api/v1/design/requests", headers=si.headers, json={
        "org_id": str(org.id), "title": "invasão", "briefing": "tentando abrir em outra org"})
    assert r.status_code == 404


async def test_establishment_partner_can_request_internal_content(client, db, storage, mail):
    org = await make_org(db, typ=4)
    est = await make_establishment(db, org)
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org.id))
    s = await login_as(client, owner)
    r = await client.post("/api/v1/design/requests", headers=s.headers, json={
        "org_id": str(org.id), "establishment_id": str(est.id), "title": "Cardápio do almoço",
        "briefing": "Arte vertical com o prato do dia", "orientation": "VERTICAL"})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "CONTEUDO_INTERNO"
    lst = (await client.get("/api/v1/design/requests", headers=s.headers)).json()
    assert lst["total"] == 1
    req = await db.get(DesignRequest, uuid.UUID(r.json()["id"]))
    assert req.revisions_included == 3


async def test_comment_authors_rule_and_designers_list(client, db, storage, mail):
    org = await payer_org(db)
    cu = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id), name="Cliente Paula")
    du = await make_user(db, role="designer", name="Designer Ana")
    du2 = await make_user(db, role="designer", name="Designer Bruno")
    sc, sd = await login_as(client, cu), await login_as(client, du)
    rid = await _new_request(client, sc, sd, org)  # atribuído à Designer Ana
    r = await client.post(f"/api/v1/design/requests/{rid}/comments", headers=sd.headers, json={"body": "Começando"})
    assert r.status_code == 201 and r.json()["author_name"] == "Designer Ana" and r.json()["author_is_staff"] is True
    await client.post(f"/api/v1/design/requests/{rid}/comments", headers=sc.headers, json={"body": "Obrigada"})
    cl = (await client.get(f"/api/v1/design/requests/{rid}/comments", headers=sc.headers)).json()["items"]
    assert [(c["author_name"], c["author_is_staff"]) for c in cl] == [("Equipe RD", True), ("Cliente Paula", False)]
    assert cl[0]["author_id"] is None and "Ana" not in str(cl)
    st = (await client.get(f"/api/v1/design/requests/{rid}/comments", headers=sd.headers)).json()["items"]
    assert [c["author_name"] for c in st] == ["Designer Ana", "Cliente Paula"]
    # designers atribuíveis (design.work GLOBAL) com carga atual; sem dados pessoais
    r = await client.get("/api/v1/design/designers", headers=sd.headers)
    assert r.status_code == 200, r.text
    items = {c["name"]: c for c in r.json()["items"]}
    assert set(items) == {"Designer Ana", "Designer Bruno"}
    assert items["Designer Ana"]["active_requests"] == 1 and items["Designer Bruno"]["active_requests"] == 0
    assert "@" not in r.text
    assert (await client.get("/api/v1/design/designers", headers=sc.headers)).status_code == 403
    emp = await make_user(db, role="employee")
    assert (await client.get("/api/v1/design/designers", headers=(await login_as(client, emp)).headers)).status_code == 403
    assert du2.id
