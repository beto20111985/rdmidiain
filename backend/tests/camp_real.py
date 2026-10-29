"""
Apoio aos testes W-CAMP com os domínios REAIS (sem FakeDeps): inventário (W-INV ``hold``/
``convert_hold``/``release_hold``/sweeper/manifesto) e financeiro (W-FIN ``create_payment_order`` com o
provedor FALSO de teste do próprio W-FIN, webhook ASSINADO pela rota pública e processador real).
"""
from __future__ import annotations

import secrets
import uuid
from datetime import date, timedelta

from sqlalchemy import select, text, update

from app.core.authz import AuthContext
from app.core.settings_service import set_setting
from tests.camp_support import make_media

SYS = AuthContext.system("system")


async def set_payer(db, org_id) -> None:
    from app.models.organizations import Organization

    await db.execute(update(Organization).where(Organization.id == org_id).values(
        email=f"fin{secrets.token_hex(3)}@anunciante.com.br", phone="62999990001",
        document=f"{secrets.randbelow(10**14):014d}", document_type="CNPJ"))
    await db.commit()


async def approval(db, mode: str) -> None:
    await set_setting(db, SYS, "campaign.approval_mode", mode, reason="teste")
    await db.commit()


async def today_local(db) -> date:
    return (await db.execute(text("SELECT (now() AT TIME ZONE 'America/Sao_Paulo')::date"))).scalar_one()


async def create_campaign(client, s, db, org, *, start: date, period: str = "AVULSO", days: int = 7, duration: int = 30,
                          freq: int = 2, bands=None, targets=None, name: str = "Campanha Real") -> str:
    body = {"name": name, "period_type": period, "start_date": start.isoformat(), "duration_s": duration,
            "freq_kind": "PER_HOUR", "freq_value": freq,
            "targets": targets or [{"type": "CITY", "value": "GO:Goiânia"}]}
    if period == "AVULSO":
        body["end_date"] = (start + timedelta(days=days - 1)).isoformat()
    if bands:
        body["time_bands"] = bands
    r = await client.post("/api/v1/campaigns", headers=s.headers, json=body)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    m = await make_media(db, org, duration_ms=duration * 1000)
    cr = (await client.post("/api/v1/creatives", headers=s.headers,
                            json={"name": "Peça", "media_file_id": str(m.id), "duration_seconds": duration})).json()
    r = await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=s.headers, json={"creative_id": cr["id"]})
    assert r.status_code == 201, r.text
    return cid


async def pay_order(db, client, order_id) -> str:
    """Webhook ``paid`` ASSINADO (provedor falso do W-FIN) pela rota pública + processamento real."""
    from app.models.financial import PaymentOrder, WebhookEvent
    from app.models.pricing import Order
    from app.modules.financial import payments
    from tests.fin_helpers import webhook_for

    o = (await db.execute(select(Order).where(Order.id == uuid.UUID(str(order_id)))
                          .execution_options(populate_existing=True))).scalar_one()
    po = await db.get(PaymentOrder, o.payment_order_id)
    event_id = secrets.randbelow(2**40) + 1
    raw, headers = webhook_for(po, event_id=event_id, fee_cents=150)
    r = await client.post("/api/v1/webhooks/cloudpaypag", content=raw, headers=headers)
    assert r.status_code == 200, r.text
    ev = (await db.execute(select(WebhookEvent).where(WebhookEvent.webhook_event_id == event_id))).scalar_one()
    st = await payments.process_webhook_event(db, ev.id)
    await db.commit()
    return st


async def checkout_and_pay(db, client, s, cid) -> dict:
    r = await client.post(f"/api/v1/campaigns/{cid}/checkout",
                          headers={**s.headers, "Idempotency-Key": f"key-{uuid.uuid4().hex}"}, json={})
    assert r.status_code == 201, r.text
    assert await pay_order(db, client, r.json()["order_id"]) == "PROCESSED"
    return r.json()


async def make_active(db, cid, screen_id) -> None:
    from app.modules.campaigns import service as S

    c = await camp(db, cid)
    if c.status in ("APROVADA", "AGENDADA"):
        await S.mark_syncing(db, c.id)
        await db.commit()
    assert await S.mark_active_from_ack(db, [c.id], screen_id=screen_id, manifest_version=1) in ([c.id], [])
    await db.commit()
    assert (await camp(db, cid)).status == "ATIVA"


async def camp(db, cid):
    from app.models.campaigns import Campaign

    return (await db.execute(select(Campaign).where(Campaign.id == uuid.UUID(str(cid)))
                             .execution_options(populate_existing=True))).scalar_one()


async def holds(db, cid) -> list:
    from app.models.inventory import InventoryHold

    return list((await db.execute(select(InventoryHold).where(InventoryHold.campaign_id == uuid.UUID(str(cid)))
                                  .order_by(InventoryHold.created_at)
                                  .execution_options(populate_existing=True))).scalars())


async def booked(db, cid) -> dict:
    """{hold_id: nº de linhas BOOKED} da campanha."""
    from sqlalchemy import func

    from app.models.inventory import BookingScreen

    rows = (await db.execute(select(BookingScreen.hold_id, func.count()).where(
        BookingScreen.campaign_id == uuid.UUID(str(cid)), BookingScreen.status == "BOOKED")
        .group_by(BookingScreen.hold_id))).all()
    return {h: n for h, n in rows}


async def age_holds(db, cid=None) -> int:
    """Simula a passagem de > 2 h: vence as reservas HELD e roda o sweeper REAL do W-INV."""
    from app.modules.inventory import service as inv

    q = ("UPDATE inventory_holds SET created_at = now() - interval '3 hours', "
         "hold_expires_at = now() - interval '1 second' WHERE status = 'HELD'")
    params = {}
    if cid is not None:
        q += " AND campaign_id = :c"
        params["c"] = uuid.UUID(str(cid))
    await db.execute(text(q), params)
    await db.execute(text("UPDATE booking_screens SET hold_expires_at = now() - interval '1 second' "
                          "WHERE status = 'HELD'"))
    await db.commit()
    out = await inv.sweep_expired_holds(db)
    await db.commit()
    return len(out)


async def manifest_campaigns(db, screen_id) -> set:
    from app.modules.inventory import service as inv

    sm = await inv.generate_manifest(db, screen_id, reason="test", force=True)
    await db.commit()
    return {v.get("campaign_id") for v in sm.manifest["creatives"].values() if v.get("kind") == "EXTERNAL"}
