"""Tasks do W-CAMP executadas no processo (engine NullPool próprio, como no worker)."""
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.core.authz import AuthContext
from app.core.settings_service import set_setting
from app.models.pricing import Order
from app.workers.tasks import campaigns as T
from tests.camp_support import FakeDeps
from tests.integration.test_camp_lifecycle import new_campaign, status_of, world

SYS = AuthContext.system("system")


@pytest.fixture
def deps(monkeypatch):
    return FakeDeps().install(monkeypatch)


async def test_tasks_finalizam_vencidas_e_expiram_pedidos(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    paid = await new_campaign(client, s, db=db, org=org)
    r = await client.post(f"/api/v1/campaigns/{paid}/checkout", headers={**s.headers, "Idempotency-Key": "task-0001"},
                          json={})
    from app.modules.campaigns import checkout as CK

    o = await db.get(Order, uuid.UUID(r.json()["order_id"]))
    await CK.on_order_paid(db, o.id, o.payment_order_id)
    await db.commit()
    # campanha aprovada cujo último dia (local) já passou há dias → FINALIZADA pela task
    await db.execute(text("UPDATE campaigns SET start_date = :a, end_date = :b WHERE id = :i"),
                     {"a": date.today() - timedelta(days=10), "b": date.today() - timedelta(days=3), "i": uuid.UUID(paid)})
    unpaid = await new_campaign(client, s, db=db, org=org)
    r = await client.post(f"/api/v1/campaigns/{unpaid}/checkout",
                          headers={**s.headers, "Idempotency-Key": "task-0002"}, json={})
    await db.execute(text("UPDATE orders SET expires_at = now() - interval '1 minute' WHERE id = :i"),
                     {"i": uuid.UUID(r.json()["order_id"])})
    await db.commit()
    out = await T._finalize()
    assert out["finalized"] == 1
    out = await T._expire_orders()
    assert out["expired"] == 1
    assert (await status_of(db, paid)).status == "FINALIZADA"
    assert (await status_of(db, unpaid)).status == "RASCUNHO"
    # idempotentes
    assert (await T._finalize())["finalized"] == 0 and (await T._expire_orders())["expired"] == 0
