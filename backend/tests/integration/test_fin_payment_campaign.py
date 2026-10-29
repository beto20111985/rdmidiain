"""Pagamento → campanha com os domínios REAIS (W-CAMP checkout + W-INV reserva + W-FIN
cobrança/webhook): o preço vem do servidor; o webhook ``paid`` ASSINADO passa pela rota
pública e pelo processador; MANUAL ⇒ EM_ANALISE (nunca APROVADA); AUTO ⇒ APROVADA; a
reserva de inventário é convertida; replay do webhook não duplica nada."""
import secrets
import uuid

from sqlalchemy import func, select, update

from app.core.authz import AuthContext
from app.core.settings_service import set_setting
from app.models.financial import LedgerTransaction, PaymentOrder, PaymentOrderCallback
from app.models.organizations import Organization
from app.models.pricing import Order
from app.modules.financial import commission, payments
from tests.fin_helpers import fin_env, webhook_for  # noqa: F401
from tests.integration.test_camp_lifecycle import checkout, new_campaign, status_of, world


async def _checkout_world(db, client):
    t, org, ests, screens, s = await world(db, client)
    await db.execute(update(Organization).where(Organization.id == org.id).values(
        email="financeiro@anunciante.com.br", phone="62999990001", document=f"{secrets.randbelow(10**14):014d}",
        document_type="CNPJ"))
    await db.commit()
    cid = await new_campaign(client, s, db=db, org=org)
    r = await checkout(client, s, cid)
    assert r.status_code == 201, r.text
    body = r.json()
    order = await db.get(Order, uuid.UUID(body["order_id"]))
    po = await db.get(PaymentOrder, order.payment_order_id)
    return org, cid, body, order, po


async def _paid_webhook(db, client, po, event_id):
    raw, headers = webhook_for(po, event_id=event_id, fee_cents=150)
    r = await client.post("/api/v1/webhooks/cloudpaypag", content=raw, headers=headers)
    assert r.status_code == 200, r.text
    if r.json().get("duplicate"):
        return "DUPLICATE"
    from app.models.financial import WebhookEvent

    ev = (await db.execute(select(WebhookEvent).where(WebhookEvent.webhook_event_id == event_id))).scalar_one()
    st = await payments.process_webhook_event(db, ev.id)
    await db.commit()
    return st


async def _hold_status(db, cid):
    from app.models.inventory import InventoryHold

    return [h.status for h in (await db.execute(select(InventoryHold).where(
        InventoryHold.campaign_id == uuid.UUID(str(cid))).execution_options(populate_existing=True))).scalars()]


async def test_manual_approval_payment_goes_to_review_never_approved(db, client, fin_env):
    org, cid, body, order, po = await _checkout_world(db, client)
    assert po.status == "PENDING" and po.amount_cents == body["total_cents"] and po.provider_external_ref
    assert po.reference_type == "campaign_checkout" and po.reference_id == str(order.id)
    assert po.eligible_revenue_cents == body["items"][0]["media_cents"]
    assert (await status_of(db, cid)).status == "AGUARDANDO_PAGAMENTO"
    assert await _paid_webhook(db, client, po, 9001) == "PROCESSED"
    c = await status_of(db, cid)
    assert c.status == "EM_ANALISE" and c.approved_at is None
    assert set(await _hold_status(db, cid)) <= {"CONVERTED"}
    cb = (await db.execute(select(PaymentOrderCallback))).scalar_one()
    assert (cb.name, cb.status) == ("campaigns.checkout.on_order_paid", "DONE")
    (cc,) = await commission.terms_of(db, uuid.UUID(str(cid)))  # termo INITIAL desta cobrança
    assert (cc.kind, cc.payment_order_id) == ("INITIAL", po.id)
    assert cc.pool_cents == cc.eligible_revenue_cents * 500 // 10_000
    # replay (outro webhook_event_id) não duplica receita nem converte de novo
    assert await _paid_webhook(db, client, po, 9002) == "IGNORED"
    assert (await db.execute(select(func.count()).select_from(LedgerTransaction)
                             .where(LedgerTransaction.kind == "PAYMENT_RECEIVED"))).scalar_one() == 1
    assert (await status_of(db, cid)).status == "EM_ANALISE"


async def test_auto_approval_payment_goes_to_approved(db, client, fin_env):
    await set_setting(db, AuthContext.system("system"), "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    org, cid, body, order, po = await _checkout_world(db, client)
    assert await _paid_webhook(db, client, po, 9101) == "PROCESSED"
    from app.models.campaigns import CampaignStatusHistory

    c = await status_of(db, cid)
    hist = [(h.from_status, h.to_status) for h in (await db.execute(
        select(CampaignStatusHistory).where(CampaignStatusHistory.campaign_id == c.id)
        .order_by(CampaignStatusHistory.created_at))).scalars()]
    # aprovação automática pelo pagamento (o W-CAMP segue para sincronização logo em seguida)
    assert ("AGUARDANDO_PAGAMENTO", "APROVADA") in hist and "EM_ANALISE" not in [t for _, t in hist]
    assert c.approved_at is not None and c.status in ("APROVADA", "AGENDADA", "SINCRONIZANDO", "ATIVA")
    assert set(await _hold_status(db, cid)) <= {"CONVERTED"}


async def test_expired_charge_releases_order(db, client, fin_env):
    org, cid, body, order, po = await _checkout_world(db, client)
    raw, headers = webhook_for(po, status="expired", event_id=9201)
    r = await client.post("/api/v1/webhooks/cloudpaypag", content=raw, headers=headers)
    assert r.status_code == 200
    from app.models.financial import WebhookEvent

    ev = (await db.execute(select(WebhookEvent))).scalar_one()
    assert await payments.process_webhook_event(db, ev.id) == "PROCESSED"
    await db.commit()
    assert (await status_of(db, cid)).status == "RASCUNHO"
    db.expunge_all()
    assert (await db.get(PaymentOrder, po.id)).status == "EXPIRED"


async def test_commission_uses_real_inventory_plan_and_screen_establishment(db, client, fin_env):
    """E_i vem do W-INV (contracted_exhibitions) e o estabelecimento da tela do W-ORG — sem provedores de teste."""
    from app.models.financial import CommissionAccrual, CommissionCampaignScreen, CommissionDayPlan
    from app.models.screens import Screen
    from tests.fin_helpers import pop

    org, cid, body, order, po = await _checkout_world(db, client)
    assert await _paid_webhook(db, client, po, 9301) == "PROCESSED"
    plan = await commission.contracted_plan(db, uuid.UUID(cid))
    assert plan and all(sum(days.values()) > 0 for days in plan.values())
    sid, days = next(iter(plan.items()))
    day = min(days)
    r = await commission.accrue_commission_for_events(db, pop(uuid.UUID(cid), sid, day, days[day]))
    await db.commit()
    assert r["counted"] == days[day]
    (cc,) = await commission.terms_of(db, uuid.UUID(cid))
    # snapshot diário do BOOKING (W-INV) no período do termo
    assert cc.expected_total == sum(n for ds in plan.values() for d, n in ds.items() if cc.start_date <= d <= cc.end_date)
    scr = (await db.execute(select(CommissionCampaignScreen).where(
        CommissionCampaignScreen.terms_id == cc.id, CommissionCampaignScreen.screen_id == sid))).scalar_one()
    s = await db.get(Screen, sid)
    assert scr.establishment_id == s.establishment_id and scr.screen_weight_milli == s.weight_milli
    dp = await db.get(CommissionDayPlan, (cc.id, day))
    assert dp.plan == {str(k): ds[day] for k, ds in plan.items() if ds.get(day)}
    accrued = sum((await db.execute(select(CommissionAccrual.amount_ucents)
                                    .where(CommissionAccrual.terms_id == cc.id))).scalars())
    assert 0 < accrued <= (cc.pool_cents * commission.UC * dp.e_day) // cc.expected_total  # ≤ orçamento do dia
