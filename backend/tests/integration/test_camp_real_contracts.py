"""Contrato REAL de cada interface consumida pelo W-CAMP (sem FakeDeps; W-INV e W-FIN de verdade).

Mapa interface → teste real:
- W-INV ``hold`` (TTL 2 h, ``order_id``, ``replaces_hold_id``) ......... test_camp_renewal, test_camp_changes_real
- W-INV ``convert_hold`` (no pagamento; recaptura tardia) ................ test_camp_renewal, test_camp_changes_real
- W-INV ``release_hold`` (SUPERSEDED_BY_REVISION/REFUNDED/CHANGE_REJECTED) test_camp_changes_real
- W-INV ``release_hold`` (CAMPAIGN_CANCELLED; HELD do pedido cancelado) .. aqui
- W-INV ``invalidate_screens`` (aprovação, pausa, retomada) .............. aqui
- W-INV ``external_seconds_by_screen`` (elegibilidade por duração) ....... aqui
- W-FIN ``create_payment_order`` .......................................... todos os testes com checkout real
- W-FIN ``cancel_payment_order`` .......................................... aqui
- W-FIN ``request_refund`` (cancelamento de campanha paga; alteração paga rejeitada) aqui, test_camp_changes_real
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.core.authz import AuthContext
from app.models.financial import PaymentOrder, Refund
from app.models.manifests import ManifestVersion
from app.models.pricing import Order
from app.modules.inventory import service as inv
from app.modules.inventory.policies import create_policy
from tests.camp_real import (
    approval,
    booked,
    checkout_and_pay,
    create_campaign,
    holds,
    make_active,
    set_payer,
    today_local,
)
from tests.camp_support import future_monday
from tests.factories import login_as, make_user
from tests.fin_helpers import fin_env  # noqa: F401  (fixture do W-FIN: provedor falso + segredo de webhook)
from tests.integration.test_camp_lifecycle import world

pytestmark = pytest.mark.usefixtures("fin_env")
SYS = AuthContext.system("system")


async def _setup(db, client):
    t, org, ests, screens, s = await world(db, client)
    await set_payer(db, org.id)
    await approval(db, "AUTO")
    return t, org, ests, screens, s


async def test_cancelar_pedido_pendente_cancela_cobranca_real_e_libera_reserva(db, client):
    t, org, ests, screens, s = await _setup(db, client)
    cid = await create_campaign(client, s, db, org, start=future_monday())
    r = await client.post(f"/api/v1/campaigns/{cid}/checkout",
                          headers={**s.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"}, json={})
    assert r.status_code == 201, r.text
    oid = uuid.UUID(r.json()["order_id"])
    po_id = (await db.execute(select(Order.payment_order_id).where(Order.id == oid))).scalar_one()
    assert (await db.get(PaymentOrder, po_id)).status not in ("CANCELLED", "PAID")
    [h] = await holds(db, cid)
    assert h.status == "HELD" and h.hold_expires_at - h.created_at == timedelta(hours=2)
    r = await client.post(f"/api/v1/checkout/orders/{oid}/cancel", headers=s.headers)
    assert r.status_code == 200 and r.json()["status"] == "CANCELADO", r.text
    po = (await db.execute(select(PaymentOrder).where(PaymentOrder.id == po_id)
                           .execution_options(populate_existing=True))).scalar_one()
    assert po.status == "CANCELLED"  # W-FIN real (cancelamento local da cobrança)
    [h] = await holds(db, cid)
    assert h.status != "HELD"
    n = (await db.execute(text("SELECT count(*) FROM booking_screens WHERE campaign_id = :c AND status IN "
                               "('HELD', 'BOOKED')"), {"c": uuid.UUID(cid)})).scalar_one()
    assert n == 0  # capacidade devolvida


async def test_cancelar_campanha_paga_pede_reembolso_real_e_libera_booked(db, client):
    t, org, ests, screens, s = await _setup(db, client)
    cid = await create_campaign(client, s, db, org, start=future_monday())
    await checkout_and_pay(db, client, s, cid)
    [h] = await holds(db, cid)
    assert h.status == "CONVERTED" and await booked(db, cid) == {h.id: 2}
    r = await client.post(f"/api/v1/campaigns/{cid}/cancel", headers=s.headers,
                          json={"reason": "Cliente desistiu antes do início"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELADA", r.text
    refs = (await db.execute(select(Refund).where(Refund.campaign_id == uuid.UUID(cid)))).scalars().all()
    assert len(refs) == 1 and refs[0].status == "AWAITING_APPROVAL" and refs[0].policy == "FULL_BEFORE_START"
    [h] = await holds(db, cid)
    assert h.status == "CANCELLED" and h.release_reason == "CAMPAIGN_CANCELLED"
    assert await booked(db, cid) == {}


async def _latest_manifest(db, screen_id):
    return (await db.execute(select(ManifestVersion).where(ManifestVersion.screen_id == screen_id)
                             .order_by(ManifestVersion.version.desc()).limit(1)
                             .execution_options(populate_existing=True))).scalar_one_or_none()


async def test_invalidate_screens_real_na_aprovacao_pausa_e_retomada(db, client):
    t, org, ests, screens, s = await _setup(db, client)
    cid = await create_campaign(client, s, db, org, start=await today_local(db))
    await checkout_and_pay(db, client, s, cid)  # AUTO: aprovada no pagamento ⇒ invalidate_screens
    c_uuid = uuid.UUID(cid)
    for sc in screens:
        m = await _latest_manifest(db, sc.id)
        assert m is not None and c_uuid in m.campaign_ids, "manifesto não regenerado na aprovação"
    await make_active(db, cid, screens[0].id)
    v_before = (await _latest_manifest(db, screens[0].id)).version
    r = await client.post(f"/api/v1/campaigns/{cid}/pause", headers=s.headers, json={})
    assert r.status_code == 200 and r.json()["status"] == "PAUSADA", r.text
    m = await _latest_manifest(db, screens[0].id)
    assert m.version > v_before and m.reason == f"campaign_pause:{cid}" and c_uuid not in m.campaign_ids
    r = await client.post(f"/api/v1/campaigns/{cid}/resume", headers=s.headers)
    assert r.status_code == 200, r.text
    m = await _latest_manifest(db, screens[0].id)
    assert m.reason == f"campaign_resumed:{cid}" and c_uuid in m.campaign_ids


async def test_external_seconds_by_screen_real_define_elegibilidade_por_duracao(db, client):
    t, org, ests, screens, s = await _setup(db, client)
    now = (await db.execute(text("SELECT now()"))).scalar_one()
    pol = await create_policy(db, SYS, scope="SCREEN", scope_id=screens[1].id, internal_seconds=180,
                              external_seconds=60, effective_from=now + timedelta(seconds=5),
                              reason="parceiro premium 3+1")
    # política nasce no futuro (não retroativa); simula que ela já vigora desde anteontem
    await db.execute(text("UPDATE rotation_policies SET effective_from = now() - interval '2 days' WHERE id = :i"),
                     {"i": pol.id})
    await db.commit()
    db.expunge(pol)  # a linha foi alterada por SQL: descarta a cópia em memória da sessão
    ext = await inv.external_seconds_by_screen(db, [screens[0].id, screens[1].id])
    assert ext[screens[1].id] == 60 and ext[screens[0].id] >= 120
    body = {"targets": [{"type": "CITY", "value": "GO:Goiânia"}], "duration_s": 120, "freq_kind": "PER_HOUR",
            "freq_value": 1, "start_date": future_monday().isoformat(), "period_type": "AVULSO",
            "end_date": (future_monday() + timedelta(days=6)).isoformat()}
    r = await client.post("/api/v1/pricing/quote", headers=s.headers, json=body)
    assert r.status_code == 200, r.text
    av = r.json()["availability"]
    assert av["screens_available"] == 1 and av["excluded_by_duration"] == 1  # 120 s nunca é cortado
    r = await client.post("/api/v1/pricing/quote", headers=s.headers, json={**body, "duration_s": 30})
    assert r.json()["availability"]["screens_available"] == 2


# ─── (g) invalidação de manifesto DEPOIS do estado gravado (autoflush=False) ─────────────────

async def _versions(db, screen_id) -> list:
    return list((await db.execute(select(ManifestVersion).where(ManifestVersion.screen_id == screen_id)
                                  .order_by(ManifestVersion.version)
                                  .execution_options(populate_existing=True))).scalars())


async def test_cancelamento_pago_regera_manifesto_sem_a_campanha_mesmo_sem_liberar_hold(db, client, monkeypatch):
    """A campanha sai do manifesto porque está CANCELADA no banco, não porque a reserva foi liberada:
    com ``release_hold`` neutralizado (revisão viva cuja capacidade não é devolvida), a versão nova do
    manifesto gerada pelo W-INV REAL ainda assim não contém a campanha."""
    from app.modules.campaigns import service as S

    t, org, ests, screens, s = await _setup(db, client)
    cid = await create_campaign(client, s, db, org, start=await today_local(db))
    await checkout_and_pay(db, client, s, cid)
    await make_active(db, cid, screens[0].id)
    c_uuid = uuid.UUID(cid)
    assert c_uuid in (await _latest_manifest(db, screens[0].id)).campaign_ids
    real = S._inventory_call

    async def no_release(name, *a, **kw):
        if name == "release_hold":
            return None
        return await real(name, *a, **kw)

    monkeypatch.setattr(S, "_inventory_call", no_release)
    r = await client.post(f"/api/v1/campaigns/{cid}/cancel", headers=s.headers,
                          json={"reason": "Cliente encerrou o contrato"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELADA", r.text
    assert {h.status for h in await holds(db, cid)} == {"CONVERTED"}  # reserva NÃO liberada (simulação)
    for sc in screens:
        m = await _latest_manifest(db, sc.id)
        assert m.reason == f"campaign_cancelled:{cid}", m.reason
        assert c_uuid not in m.campaign_ids


async def test_aprovacao_de_alteracao_regera_manifesto_com_a_versao_nova(db, client):
    """Aprovação: a tela que só existe na revisão NOVA recebe versão de manifesto com a campanha
    (``campaign_approved:<id>``) — o gerador do W-INV lê a revisão já gravada."""
    from tests.camp_real import pay_order
    from tests.camp_support import make_est, make_screen

    t, org, ests, screens, s = await _setup(db, client)
    cid = await create_campaign(client, s, db, org, start=await today_local(db))
    await checkout_and_pay(db, client, s, cid)
    await make_active(db, cid, screens[0].id)
    await approval(db, "MANUAL")
    ana = await make_est(db, category=t.supermercado, segments=[t.alimentos], city="Anápolis", state="GO")
    s3 = await make_screen(db, ana)
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers={**s.headers, "Idempotency-Key": "chg-g-0001"},
                          json={"targets": [{"type": "STATE", "value": "GO"}]})
    assert r.status_code == 201, r.text
    assert await pay_order(db, client, r.json()["order"]["order_id"]) == "PROCESSED"
    c_uuid = uuid.UUID(cid)
    assert all(c_uuid not in v.campaign_ids for v in await _versions(db, s3.id))  # pendente: não toca
    sa = await login_as(client, await make_user(db, role="admin", mfa=True))
    r = await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={})
    assert r.status_code == 200, r.text
    vs = await _versions(db, s3.id)
    assert any(v.reason == f"campaign_approved:{cid}" and c_uuid in v.campaign_ids for v in vs), \
        [(v.version, v.reason) for v in vs]
    assert c_uuid in vs[-1].campaign_ids


async def test_renovacao_paga_regera_manifesto_com_a_reserva_nova(db, client):
    """Renovação paga: o manifesto (W-INV real, sem ``force``) continua com a campanha e a versão vigente
    gravada já lista a reserva do período adicional."""
    from app.models.campaigns import Campaign, CampaignRevision
    from app.modules.campaigns.service import revision_hold_ids
    from tests.camp_real import pay_order

    t, org, ests, screens, s = await _setup(db, client)
    cid = await create_campaign(client, s, db, org, start=await today_local(db), period="MENSAL")
    await checkout_and_pay(db, client, s, cid)
    await make_active(db, cid, screens[0].id)
    r = await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/v1/campaigns/{cid}/renew/checkout",
                          headers={**s.headers, "Idempotency-Key": f"rn-{uuid.uuid4().hex}"}, json={})
    assert r.status_code == 201, r.text
    assert await pay_order(db, client, r.json()["order_id"]) == "PROCESSED"
    c = (await db.execute(select(Campaign).where(Campaign.id == uuid.UUID(cid))
                          .execution_options(populate_existing=True))).scalar_one()
    live = await db.get(CampaignRevision, c.live_revision_id)
    assert live.kind == "RENEWAL" and len(revision_hold_ids(live)) == 2
    for sc in screens:
        m = await _latest_manifest(db, sc.id)
        assert m is not None and uuid.UUID(cid) in m.campaign_ids
    a = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'campaign.renewed' AND entity_id = :e"),
                          {"e": cid})).scalar_one()
    assert a == 1
