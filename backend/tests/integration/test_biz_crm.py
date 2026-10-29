"""W-BIZ — CRM: funil/pipeline, escopo por cidade, proposta com desconto acima do limite
exige aprovação (nunca do próprio vendedor), PDF real (validade 30 dias), envio por
e-mail com anexo, expiração e conversão proposta → pedido pelo checkout REAL do W-CAMP
(inventário/financeiro substituídos pelo FakeDeps do W-CAMP) com atribuição ao vendedor."""
from __future__ import annotations

import io
import uuid
from datetime import timedelta

import pytest
from pypdf import PdfReader
from sqlalchemy import select, update

from app.core.storage import LocalStorageProvider, set_storage
from app.core.timeutils import local_date, utcnow
from app.models.crm import Proposal, SalesAttribution
from app.modules.crm import service as crm
from tests.factories import login_as, make_org, make_user


@pytest.fixture(autouse=True)
async def _drain_after_test(clean_state):
    yield
    from app.modules.notifications.service import drain

    await drain()


@pytest.fixture
def storage(tmp_path):
    set_storage(LocalStorageProvider(tmp_path / "storage"))
    yield
    set_storage(None)


ITEMS = [{"package_code": "PKG-10T-30S", "description": "Pacote 10 telas, 30 s, 30 dias", "quantity": 1,
          "unit_price_cents": 150000},
         {"package_code": "ARTE", "description": "Criação de arte", "quantity": 2, "unit_price_cents": 15000}]


async def _people(client, db):
    seller = await make_user(db, role="sales", name="Vendedor Silva")
    approver = await make_user(db, role="admin", mfa=True, name="Gerente")
    return seller, approver, await login_as(client, seller), await login_as(client, approver)


async def test_pipeline_and_lead_stages(client, db, mail):
    seller, approver, ss, sa = await _people(client, db)
    ids = []
    for i, (city, uf) in enumerate([("Goiânia", "GO"), ("Goiânia", "GO"), ("São Paulo", "SP")]):
        r = await client.post("/api/v1/crm/leads", headers=ss.headers, json={
            "name": f"Lead {i}", "company": f"Empresa {i}", "email": f"lead{i}@example.com", "city": city, "uf": uf,
            "estimated_value_cents": 100000 * (i + 1)})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    assert (await client.post(f"/api/v1/crm/leads/{ids[0]}/stage", headers=ss.headers,
                              json={"stage": "QUALIFICADO"})).status_code == 200
    r = await client.post(f"/api/v1/crm/leads/{ids[1]}/stage", headers=ss.headers, json={"stage": "PERDIDO"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "LEAD_LOST_REASON_REQUIRED"
    assert (await client.post(f"/api/v1/crm/leads/{ids[1]}/stage", headers=ss.headers,
                              json={"stage": "PERDIDO", "reason": "Sem orçamento"})).status_code == 200
    r = await client.post(f"/api/v1/crm/leads/{ids[1]}/stage", headers=ss.headers, json={"stage": "GANHO"})
    assert r.status_code == 409  # perdido não vira ganho direto
    p = (await client.get("/api/v1/crm/pipeline", headers=ss.headers)).json()
    by = {s["stage"]: s for s in p["stages"]}
    assert by["NOVO"]["count"] == 1 and by["QUALIFICADO"]["count"] == 1 and by["PERDIDO"]["count"] == 1
    assert by["QUALIFICADO"]["estimated_value_cents"] == 100000 and p["total"] == 3
    detail = (await client.get(f"/api/v1/crm/leads/{ids[1]}", headers=ss.headers)).json()
    assert [h["to"] for h in detail["stage_history"]] == ["NOVO", "PERDIDO"]

    # vendedor regional (escopo CITY GO:goiania) não vê lead de SP
    regional = await make_user(db, role="sales", scope=("CITY", "GO:goiania"))
    sr = await login_as(client, regional)
    lst = (await client.get("/api/v1/crm/leads", headers=sr.headers)).json()
    assert lst["total"] == 2
    assert (await client.get(f"/api/v1/crm/leads/{ids[2]}", headers=sr.headers)).status_code == 404
    r = await client.post("/api/v1/crm/leads", headers=sr.headers, json={"name": "Fora", "city": "Campinas", "uf": "SP"})
    assert r.status_code == 403
    # anunciante não acessa CRM
    org = await make_org(db)
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    assert (await client.get("/api/v1/crm/leads", headers=(await login_as(client, adv)).headers)).status_code == 403


async def test_discount_above_limit_requires_approval_and_pdf_30_days(client, db, storage, mail):
    seller, approver, ss, sa = await _people(client, db)
    lead = (await client.post("/api/v1/crm/leads", headers=ss.headers, json={
        "name": "Maria Cliente", "company": "Academia Força", "email": "maria@example.com"})).json()
    r = await client.post("/api/v1/crm/proposals", headers=ss.headers, json={
        "title": "Campanha de lançamento", "lead_id": lead["id"], "discount_bps": 1500,
        "discount_justification": "Cliente estratégico", "items": ITEMS})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["subtotal_cents"] == 180000 and p["discount_cents"] == 27000 and p["total_cents"] == 153000
    assert p["number"].startswith("PROP-")
    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/submit", headers=ss.headers)
    assert r.status_code == 200 and r.json()["status"] == "AGUARDANDO_APROVACAO", r.text
    assert r.json()["discount_limit_bps"] == 1000
    # não pode ser enviada antes da aprovação
    assert (await client.post(f"/api/v1/crm/proposals/{p['id']}/send", headers=ss.headers, json={})).status_code == 409
    # vendedor não aprova (sem crm.discount_approve)
    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/discount-decision", headers=ss.headers, json={"approve": True})
    assert r.status_code == 403
    # aprovador que é o próprio vendedor da proposta também é barrado
    seller_admin = await make_user(db, role="admin", mfa=True)
    s2 = await login_as(client, seller_admin)
    own = (await client.post("/api/v1/crm/proposals", headers=s2.headers, json={
        "title": "Própria", "lead_id": lead["id"], "discount_bps": 2000, "discount_justification": "x", "items": ITEMS})).json()
    await db.execute(update(Proposal).where(Proposal.id == uuid.UUID(own["id"])).values(seller_id=seller_admin.id))
    await db.commit()
    await client.post(f"/api/v1/crm/proposals/{own['id']}/submit", headers=s2.headers)
    r = await client.post(f"/api/v1/crm/proposals/{own['id']}/discount-decision", headers=s2.headers, json={"approve": True})
    assert r.status_code == 403 and r.json()["error"]["code"] == "SELF_APPROVAL_FORBIDDEN"

    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/discount-decision", headers=sa.headers,
                          json={"approve": True, "reason": "Ok para lançamento"})
    assert r.status_code == 200 and r.json()["status"] == "APROVADA"
    assert r.json()["discount_approved_by"] == str(approver.id)
    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/send", headers=ss.headers, json={})
    assert r.status_code == 200, r.text
    sent = r.json()
    assert sent["status"] == "ENVIADA" and sent["emailed"] is True
    expected_valid = local_date(utcnow(), "America/Sao_Paulo") + timedelta(days=30)
    assert sent["valid_until"] == expected_valid.isoformat()
    # e-mail ao lead com o PDF anexado (caixa capturada)
    m = mail.last_to("maria@example.com")
    assert m is not None and p["number"] in m.subject
    assert len(m.attachments) == 1 and m.attachments[0][1] == "application/pdf"
    assert m.attachments[0][2].startswith(b"%PDF")
    # PDF real: abre, contém número, total, desconto e validade
    pdf = await client.get(f"/api/v1/crm/proposals/{p['id']}/pdf", headers=ss.headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and pdf.content.rstrip().endswith(b"%%EOF")
    txt = "".join(pg.extract_text() for pg in PdfReader(io.BytesIO(pdf.content)).pages)
    assert p["number"] in txt and "1.530,00" in txt and "15,00%" in txt
    assert f"válida até {expected_valid.strftime('%d/%m/%Y')}" in txt and "30 dias" in txt
    assert pdf.content == m.attachments[0][2]

    # dentro do limite: aprovada direto no envio para aprovação
    q = (await client.post("/api/v1/crm/proposals", headers=ss.headers, json={
        "title": "Pequena", "lead_id": lead["id"], "discount_bps": 500, "items": ITEMS[:1]})).json()
    r = await client.post(f"/api/v1/crm/proposals/{q['id']}/submit", headers=ss.headers)
    assert r.json()["status"] == "APROVADA" and r.json()["discount_approval_required"] is False


async def test_expiry_blocks_acceptance_and_job_marks_expired(client, db, storage, mail):
    seller, approver, ss, sa = await _people(client, db)
    lead = (await client.post("/api/v1/crm/leads", headers=ss.headers, json={"name": "João"})).json()
    p = (await client.post("/api/v1/crm/proposals", headers=ss.headers, json={
        "title": "Vence", "lead_id": lead["id"], "items": ITEMS[:1]})).json()
    await client.post(f"/api/v1/crm/proposals/{p['id']}/submit", headers=ss.headers)
    await client.post(f"/api/v1/crm/proposals/{p['id']}/send", headers=ss.headers, json={})
    await db.execute(update(Proposal).where(Proposal.id == uuid.UUID(p["id"]))
                     .values(expires_at=utcnow() - timedelta(minutes=1)))
    await db.commit()
    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/client-decision", headers=ss.headers, json={"accepted": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PROPOSAL_EXPIRED"
    assert (await client.get(f"/api/v1/crm/proposals/{p['id']}", headers=ss.headers)).json()["status"] == "EXPIRADA"
    assert await crm.expire_due(db) == 1
    await db.commit()
    row = (await db.execute(select(Proposal).where(Proposal.id == uuid.UUID(p["id"])))).scalar_one()
    await db.refresh(row)
    assert row.status == "EXPIRADA"


async def test_conversion_proposal_to_order_with_seller_attribution(client, db, storage, mail, monkeypatch):
    from tests.camp_support import FakeDeps
    from tests.integration.test_camp_lifecycle import new_campaign, world

    deps = FakeDeps().install(monkeypatch)
    t, org, ests, screens, s_adv = await world(db, client)
    seller, approver, ss, sa = await _people(client, db)
    master = await make_user(db, role="super_admin", mfa=True)
    sm = await login_as(client, master)
    r = await client.put("/api/v1/crm/sales-reps", headers=ss.headers, json={
        "user_id": str(seller.id), "kind": "VENDEDOR", "commission_bps": 300})
    assert r.status_code == 403  # % de comissão exige commissions.adjust
    r = await client.put("/api/v1/crm/sales-reps", headers=sm.headers, json={
        "user_id": str(seller.id), "kind": "REPRESENTANTE", "uf": "GO", "city": "Goiânia", "commission_bps": 300})
    assert r.status_code == 200, r.text

    lead = (await client.post("/api/v1/crm/leads", headers=ss.headers, json={
        "name": "Dono da Academia", "org_id": str(org.id), "city": "Goiânia", "uf": "GO"})).json()
    p = (await client.post("/api/v1/crm/proposals", headers=ss.headers, json={
        "title": "Pacote GO", "lead_id": lead["id"], "discount_bps": 1200, "discount_justification": "Volume",
        "items": ITEMS[:1]})).json()
    await client.post(f"/api/v1/crm/proposals/{p['id']}/submit", headers=ss.headers)
    await client.post(f"/api/v1/crm/proposals/{p['id']}/discount-decision", headers=sa.headers, json={"approve": True})
    await client.post(f"/api/v1/crm/proposals/{p['id']}/send", headers=ss.headers, json={})
    cid = await new_campaign(client, s_adv, db=db, org=org)
    # conversão antes do aceite do cliente → bloqueada
    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/convert", headers={**ss.headers, "Idempotency-Key": "conv-0001"},
                          json={"campaign_ids": [cid]})
    assert r.status_code == 409
    assert (await client.post(f"/api/v1/crm/proposals/{p['id']}/client-decision", headers=ss.headers,
                              json={"accepted": True, "note": "Aceite por e-mail"})).status_code == 200
    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/convert",
                          headers={**ss.headers, "Idempotency-Key": f"conv-{uuid.uuid4().hex[:12]}"},
                          json={"campaign_ids": [cid]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "CONVERTIDA" and out["order_id"]
    from app.models.pricing import Order

    order = await db.get(Order, uuid.UUID(out["order_id"]))
    assert order is not None and order.org_id == org.id and order.proposal_id == uuid.UUID(p["id"])
    assert order.commercial_discount_bps == 1200 and order.commercial_discount_approved_by == approver.id
    assert out["order_total_cents"] == order.total_cents > 0
    assert ("create_payment_order" in deps.names()) and ("hold" in deps.names())
    att = (await db.execute(select(SalesAttribution).where(SalesAttribution.order_id == order.id))).scalar_one()
    assert att.seller_id == seller.id and att.commission_bps == 300
    assert att.amount_cents == order.total_cents and att.commission_cents == order.total_cents * 300 // 10000
    lead_now = (await client.get(f"/api/v1/crm/leads/{lead['id']}", headers=ss.headers)).json()
    assert lead_now["stage"] == "GANHO"
    atts = (await client.get("/api/v1/crm/attributions", headers=ss.headers)).json()["items"]
    assert atts[0]["seller_name"] == "Vendedor Silva" and atts[0]["org_name"] == org.name
    # segunda conversão: bloqueada
    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/convert",
                          headers={**ss.headers, "Idempotency-Key": "conv-0002x"}, json={"campaign_ids": [cid]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PROPOSAL_ALREADY_CONVERTED"


async def test_crm_responses_include_names_only(client, db, storage, mail):
    seller, approver, ss, sa = await _people(client, db)
    org = await make_org(db, name="Academia Força Total")
    master = await make_user(db, role="super_admin", mfa=True, name="Diretor Comercial")
    sm = await login_as(client, master)
    lead = (await client.post("/api/v1/crm/leads", headers=ss.headers, json={
        "name": "Maria Cliente", "org_id": str(org.id)})).json()
    assert lead["owner_name"] == "Vendedor Silva" and lead["created_by_name"] == "Vendedor Silva"
    assert lead["org_name"] == "Academia Força Total"
    await client.post(f"/api/v1/crm/leads/{lead['id']}/activities", headers=ss.headers,
                      json={"kind": "LIGACAO", "description": "Primeiro contato"})
    await client.post(f"/api/v1/crm/leads/{lead['id']}/stage", headers=ss.headers, json={"stage": "QUALIFICADO"})
    d = (await client.get(f"/api/v1/crm/leads/{lead['id']}", headers=sa.headers)).json()
    assert d["activities"][0]["actor_name"] == "Vendedor Silva"
    assert [h["actor_name"] for h in d["stage_history"]] == ["Vendedor Silva", "Vendedor Silva"]
    p = (await client.post("/api/v1/crm/proposals", headers=ss.headers, json={
        "title": "Pacote", "lead_id": lead["id"], "discount_bps": 1500, "discount_justification": "Volume",
        "items": ITEMS[:1]})).json()
    assert p["seller_name"] == "Vendedor Silva" and p["org_name"] == "Academia Força Total"
    await client.post(f"/api/v1/crm/proposals/{p['id']}/submit", headers=ss.headers)
    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/discount-decision", headers=sa.headers, json={"approve": True})
    assert r.json()["discount_approved_by_name"] == "Gerente"
    lst = (await client.get("/api/v1/crm/proposals", headers=ss.headers)).json()["items"]
    assert lst[0]["seller_name"] == "Vendedor Silva"
    d = (await client.get(f"/api/v1/crm/leads/{lead['id']}", headers=ss.headers)).json()
    assert d["proposals"][0]["seller_name"] == "Vendedor Silva"
    # estrutura comercial: nome do usuário e do gestor (outro representante)
    mgr = (await client.put("/api/v1/crm/sales-reps", headers=sm.headers, json={
        "user_id": str(approver.id), "kind": "GERENTE"})).json()
    rep = (await client.put("/api/v1/crm/sales-reps", headers=sm.headers, json={
        "user_id": str(seller.id), "kind": "VENDEDOR", "manager_id": mgr["id"]})).json()
    assert rep["user_name"] == "Vendedor Silva" and rep["manager_name"] == "Gerente"
    reps = (await client.get("/api/v1/crm/sales-reps", headers=ss.headers)).json()["items"]
    assert {r["user_name"] for r in reps} == {"Vendedor Silva", "Gerente"}
    # só nomes: nada de e-mail/telefone dos usuários da equipe
    for body in (lead, p, rep, d):
        assert seller.email not in str(body) and approver.email not in str(body)


async def test_convertible_campaigns_scoped_and_minimal(client, db, storage, mail, monkeypatch):
    from tests.camp_support import FakeDeps
    from tests.integration.test_camp_lifecycle import new_campaign, world
    from tests.inv_support import mk_campaign

    FakeDeps().install(monkeypatch)
    t, org, ests, screens, s_adv = await world(db, client)
    seller, approver, ss, sa = await _people(client, db)
    lead = (await client.post("/api/v1/crm/leads", headers=ss.headers, json={
        "name": "Dono", "org_id": str(org.id), "city": "Goiânia", "uf": "GO"})).json()
    p = (await client.post("/api/v1/crm/proposals", headers=ss.headers, json={
        "title": "Pacote GO", "lead_id": lead["id"], "items": ITEMS[:1]})).json()
    ready_id = await new_campaign(client, s_adv, db=db, org=org)
    draft = await mk_campaign(db, org, status="RASCUNHO", name="Rascunho incompleto")
    await mk_campaign(db, org, status="ATIVA", name="Já ativa")
    other = await mk_campaign(db, await make_org(db), status="RASCUNHO", name="De outro cliente")
    r = await client.get(f"/api/v1/crm/proposals/{p['id']}/convertible-campaigns", headers=ss.headers)
    assert r.status_code == 200, r.text  # vendedor sem campaigns.view
    items = {c["id"]: c for c in r.json()["items"]}
    assert set(items) == {ready_id, str(draft.id)}
    assert items[ready_id]["ready"] is True and items[ready_id]["issues"] == []
    assert {i["code"] for i in items[str(draft.id)]["issues"]} == {"SEM_DATAS", "SEM_SEGMENTACAO"}
    assert set(items[ready_id]) == {"id", "name", "status", "start_date", "end_date", "duration_s", "ready", "issues"}
    assert str(other.id) not in items
    # anunciante (sem crm.manage) e vendedor de outra região: sem acesso
    assert (await client.get(f"/api/v1/crm/proposals/{p['id']}/convertible-campaigns",
                             headers=s_adv.headers)).status_code == 403
    regional = await make_user(db, role="sales", scope=("CITY", "SP:sao-paulo"))
    assert (await client.get(f"/api/v1/crm/proposals/{p['id']}/convertible-campaigns",
                             headers=(await login_as(client, regional)).headers)).status_code == 404
    # conversão com campanha de outra organização → 404 (antes de tocar o checkout)
    await client.post(f"/api/v1/crm/proposals/{p['id']}/submit", headers=ss.headers)
    await client.post(f"/api/v1/crm/proposals/{p['id']}/send", headers=ss.headers, json={})
    await client.post(f"/api/v1/crm/proposals/{p['id']}/client-decision", headers=ss.headers, json={"accepted": True})
    r = await client.post(f"/api/v1/crm/proposals/{p['id']}/convert",
                          headers={**ss.headers, "Idempotency-Key": "conv-other-org"}, json={"campaign_ids": [str(other.id)]})
    assert r.status_code == 404
    # proposta sem organização
    q = (await client.post("/api/v1/crm/proposals", headers=ss.headers, json={
        "title": "Sem cliente", "lead_id": (await client.post("/api/v1/crm/leads", headers=ss.headers, json={
            "name": "Lead solto"})).json()["id"], "items": ITEMS[:1]})).json()
    r = await client.get(f"/api/v1/crm/proposals/{q['id']}/convertible-campaigns", headers=ss.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "PROPOSAL_ORG_REQUIRED"


async def test_upsert_inbound_lead_creates_dedupes_and_notifies(db, db_engine, redis, mail):
    """Interface para o W-ORG (auto-cadastro, E2#1021–#1022): cria, dedupe por org e por e-mail,
    lead fechado não deduplica, notificação à equipe, evento da fila após o commit, auditoria system."""
    import asyncio as _asyncio
    import json as _json

    from sqlalchemy import func as _f, text as _text

    from app.core.database import session_factory
    from app.core.redis import rkey
    from app.models.crm import Lead, LeadActivity
    from app.models.notifications import Notification
    from app.modules.notifications.service import drain

    org = await make_org(db, name="Padaria Nova")
    seller_go = await make_user(db, role="sales", scope=("CITY", "GO:goiania"), name="Vendedor GO")
    seller_sp = await make_user(db, role="sales", scope=("CITY", "SP:sao-paulo"), name="Vendedor SP")
    pubsub = redis.pubsub()
    await pubsub.subscribe(rkey("events", "global"))
    await pubsub.get_message(timeout=1)

    lead, created = await crm.upsert_inbound_lead(
        db, name="  Joana\x00 Silva\n", company="Padaria Nova", email=" Joana@Exemplo.COM ", phone="(62) 9 9999-0000<script>",
        org_id=org.id, city="Goiânia", uf="go", notes="Quero anunciar\x07")
    assert created is True
    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.3)
    assert msg is None  # nada antes do commit
    await db.commit()
    await drain()
    for _ in range(20):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg:
            break
    assert msg is not None
    env = _json.loads(msg["data"])
    assert env["type"] == "crm.lead_queue" and env["data"] == {"lead_id": str(lead.id), "created": True, "stage": "NOVO"}
    assert env["scope"]["city"] == "GO:goiania"
    row = await db.get(Lead, lead.id)
    assert (row.name, row.email, row.phone, row.uf, row.city_key) == (
        "Joana Silva", "joana@exemplo.com", "(62) 9 9999-0000", "GO", "GO:goiania")
    assert row.owner_id is None and row.source == "AUTO_CADASTRO" and row.stage == "NOVO" and row.notes == "Quero anunciar"
    notified = set((await db.execute(select(Notification.user_id).where(
        Notification.event_type == "crm.lead_inbound"))).scalars())
    assert seller_go.id in notified and seller_sp.id not in notified
    origin = (await db.execute(_text(
        "SELECT origin FROM audit_logs WHERE action = 'crm.lead_inbound_created' AND entity_id = :i"),
        {"i": str(lead.id)})).scalar_one()
    assert origin == "system"

    # dedupe pela organização (e-mail diferente) e pelo e-mail normalizado (sem org)
    again, created2 = await crm.upsert_inbound_lead(db, name="Joana", company=None, email="outra@exemplo.com",
                                                    phone=None, org_id=org.id, city=None, uf=None)
    assert created2 is False and again.id == lead.id
    again2, created3 = await crm.upsert_inbound_lead(db, name="Joana S.", company=None, email="JOANA@exemplo.com",
                                                     phone=None, org_id=None, city=None, uf=None)
    assert created3 is False and again2.id == lead.id
    await db.commit()
    acts = (await db.execute(select(_f.count()).select_from(LeadActivity).where(LeadActivity.lead_id == lead.id))).scalar_one()
    assert acts == 2

    # lead encerrado (PERDIDO) não deduplica → novo lead
    await db.execute(_text("UPDATE leads SET stage = 'PERDIDO' WHERE id = :i"), {"i": lead.id})
    await db.commit()
    fresh, created4 = await crm.upsert_inbound_lead(db, name="Joana", company=None, email="joana@exemplo.com",
                                                    phone=None, org_id=org.id, city=None, uf=None)
    await db.commit()
    assert created4 is True and fresh.id != lead.id

    # cadastros simultâneos com o mesmo e-mail não duplicam
    sf = session_factory(db_engine)

    async def one():
        async with sf() as s:
            _l, c = await crm.upsert_inbound_lead(s, name="Carlos", company=None, email="carlos@x.com", phone=None,
                                                  org_id=None, city=None, uf=None)
            await s.commit()
            return c

    results = await _asyncio.gather(*[one() for _ in range(5)])
    await drain()
    assert sum(results) == 1
    assert (await db.execute(select(_f.count()).select_from(Lead).where(Lead.email == "carlos@x.com"))).scalar_one() == 1
    # nome obrigatório
    import pytest as _pytest

    from app.core.errors import DomainError as _DE

    with _pytest.raises(_DE):
        await crm.upsert_inbound_lead(db, name=" \x00 ", company=None, email=None, phone=None, org_id=None,
                                      city=None, uf=None)
    await pubsub.unsubscribe()
    await pubsub.aclose()


async def test_client_options_exclude_instance_org_and_reject_it_as_client(client, db, storage, mail):
    """Determinístico mesmo com dados de outros testes no banco: nomes com marcador único, asserções pelos
    ids criados aqui, perfil da instância definido pelo próprio teste e restaurado ao final."""
    import secrets as _secrets

    tok = "Zq" + _secrets.token_hex(4)
    seller, approver, ss, sa = await _people(client, db)
    own = await make_org(db, name=f"RD Comunicação {tok}", business_roles=["ADVERTISER", "PARTNER"])
    adv = await make_org(db, name=f"Academia Alfa {tok}", business_roles=["ADVERTISER"])
    partner = await make_org(db, name=f"Mercado Só Parceiro {tok}", business_roles=["PARTNER"])
    inactive = await make_org(db, name=f"Anunciante Inativo {tok}", business_roles=["ADVERTISER"])
    inactive.is_active = False
    await db.commit()
    mine = {str(own.id), str(adv.id), str(partner.id), str(inactive.id)}
    master = await make_user(db, role="super_admin", mfa=True)  # whitelabel.manage (sem assento de Admin Master)
    sm = await login_as(client, master)
    before = (await client.get("/api/v1/instance/profile", headers=sm.headers)).json()
    r = await client.patch("/api/v1/instance/profile", headers=sm.headers,
                           json={"legal_name": own.name, "document": own.document.lower()})
    assert r.status_code == 200, r.text
    try:
        r = await client.get(f"/api/v1/crm/client-options?search={tok}&limit=50", headers=ss.headers)
        assert r.status_code == 200, r.text
        assert [o["id"] for o in r.json()["items"]] == [str(adv.id)]  # sem instância, só-parceiro e inativo
        assert set(r.json()["items"][0]) == {"id", "name", "trade_name", "city", "uf", "business_roles"}
        r = await client.get(f"/api/v1/crm/client-options?search=RD Comunicação {tok}", headers=ss.headers)
        assert r.json()["items"] == []
        # rota do W-ORG: filtro opcional para seletores (mudança mínima autorizada)
        q = f"/api/v1/organizations?role=ADVERTISER&search={tok}&page_size=100"
        ids = {o["id"] for o in (await client.get(q + "&exclude_instance=true", headers=sm.headers)).json()["items"]}
        assert ids & mine == {str(adv.id), str(inactive.id)}  # instância fora; o resto do filtro inalterado
        ids_all = {o["id"] for o in (await client.get(q, headers=sm.headers)).json()["items"]}
        assert ids_all & mine == {str(own.id), str(adv.id), str(inactive.id)}  # padrão inalterado
        # a mudança do perfil vale na hora (sem cache de processo): sem documento, nada é excluído
        await client.patch("/api/v1/instance/profile", headers=sm.headers, json={"document": "00000000000000"})
        r = await client.get(f"/api/v1/crm/client-options?search={tok}&limit=50", headers=ss.headers)
        assert {o["id"] for o in r.json()["items"]} == {str(own.id), str(adv.id)}
        await client.patch("/api/v1/instance/profile", headers=sm.headers, json={"document": own.document})
        # a instância nunca vira cliente (proposta, lead novo, lead/proposta editados)
        r = await client.post("/api/v1/crm/proposals", headers=ss.headers,
                              json={"title": "Auto-proposta", "org_id": str(own.id), "items": ITEMS[:1]})
        assert r.status_code == 400 and r.json()["error"]["code"] == "CLIENT_IS_INSTANCE"
        r = await client.post("/api/v1/crm/leads", headers=ss.headers, json={"name": "Interno", "org_id": str(own.id)})
        assert r.status_code == 400 and r.json()["error"]["code"] == "CLIENT_IS_INSTANCE"
        r = await client.post("/api/v1/crm/leads", headers=ss.headers, json={"name": "Externo", "org_id": str(adv.id)})
        assert r.status_code == 201, r.text
        lead = r.json()
        r = await client.patch(f"/api/v1/crm/leads/{lead['id']}", headers=ss.headers, json={"org_id": str(own.id)})
        assert r.status_code == 400
        r = await client.post("/api/v1/crm/proposals", headers=ss.headers,
                              json={"title": "Proposta válida", "org_id": str(adv.id), "items": ITEMS[:1]})
        assert r.status_code == 201, r.text
        p = r.json()
        r = await client.patch(f"/api/v1/crm/proposals/{p['id']}", headers=ss.headers, json={"org_id": str(own.id)})
        assert r.status_code == 400 and r.json()["error"]["code"] == "CLIENT_IS_INSTANCE"
        # anunciante não usa o seletor do CRM
        advu = await make_user(db, role="advertiser", scope=("ORGANIZATION", adv.id))
        assert (await client.get("/api/v1/crm/client-options",
                                 headers=(await login_as(client, advu)).headers)).status_code == 403
    finally:  # restaura o perfil singleton (o harness também trunca ao final)
        await client.patch("/api/v1/instance/profile", headers=sm.headers,
                           json={"legal_name": before.get("legal_name"), "document": before.get("document")})
