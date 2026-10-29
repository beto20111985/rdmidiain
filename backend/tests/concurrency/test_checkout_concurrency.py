"""Concorrência real (PostgreSQL, transações simultâneas): cupom de uso único disputado por dois
checkouts; mesma campanha em dois checkouts com chaves diferentes; mesma chave em paralelo."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.pricing import Coupon, CouponRedemption, Order
from tests.camp_support import FakeDeps
from tests.integration.test_camp_lifecycle import new_campaign, world

UTC = timezone.utc


@pytest.fixture
def deps(monkeypatch):
    return FakeDeps().install(monkeypatch)


def H(s, key=None):
    return {**s.headers, "Idempotency-Key": key or f"key-{uuid.uuid4().hex}"}


async def test_cupom_uso_unico_disputado(db, client_factory, client, deps):
    t, org, ests, screens, s = await world(db, client)
    now = datetime.now(UTC)
    db.add(Coupon(id=uuid.uuid4(), code="UNICO", discount_type="PERCENT", discount_bps=2000, discount_cents=0,
                  valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=1), max_uses=1, uses_count=0,
                  min_order_cents=0, active=True))
    await db.commit()
    c1 = await new_campaign(client, s, db=db, org=org)
    c2 = await new_campaign(client, s, db=db, org=org)
    a, b = client_factory(), client_factory()
    r1, r2 = await asyncio.gather(
        a.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [c1], "coupon_code": "UNICO"}),
        b.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [c2], "coupon_code": "UNICO"}))
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [201, 409], (r1.text, r2.text)
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["error"]["code"] == "COUPON_EXHAUSTED"
    used = (await db.execute(select(Coupon.uses_count).where(Coupon.code == "UNICO"))).scalar_one()
    assert used == 1
    assert (await db.execute(select(func.count()).select_from(CouponRedemption))).scalar_one() == 1


async def test_mesma_campanha_dois_checkouts_um_vence(db, client_factory, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    clients = [client_factory() for _ in range(4)]
    rs = await asyncio.gather(*[c.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
                                for c in clients])
    assert sorted(r.status_code for r in rs) == [201, 409, 409, 409], [r.text for r in rs]
    assert (await db.execute(select(func.count()).select_from(Order))).scalar_one() == 1
    assert deps.names().count("hold") == 1


async def test_mesma_chave_em_paralelo_devolve_o_mesmo_pedido(db, client_factory, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    key = f"same-{uuid.uuid4().hex}"
    rs = await asyncio.gather(*[client_factory().post("/api/v1/checkout", headers=H(s, key),
                                                      json={"campaign_ids": [cid]}) for _ in range(3)])
    assert sorted(r.status_code for r in rs) == [200, 200, 201], [r.text for r in rs]
    assert len({r.json()["order_id"] for r in rs}) == 1
    assert deps.names().count("create_payment_order") == 1
