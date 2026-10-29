"""Webhook CloudPayPag pela rota HTTP real: assinatura da doc, idempotência (sequencial e
concorrente), paid repetido, fora de ordem, valor divergente, subconta errada, imutabilidade."""
import asyncio
import json

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.models.financial import LedgerTransaction, PaymentOrder, ReconciliationItem, WebhookEvent
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import payments
from tests.fin_helpers import (  # noqa: F401
    DOC_EXAMPLES,
    TEST_SECRET,
    balance,
    create_order,
    fin_env,
    media_item,
    pay,
    payer_org,
    webhook_for,
)

URL = "/api/v1/webhooks/cloudpaypag"


async def _order(db, amount=9990):
    org = await payer_org(db)
    from datetime import date, timedelta
    import uuid

    d = date.today() + timedelta(days=5)
    return org, await create_order(db, org, [media_item(uuid.uuid4(), amount, d, d + timedelta(days=9))], amount)


async def _count(db, model, *where):
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def test_exact_doc_example_payload_is_accepted_and_processed(db, client, fin_env):
    org, order = await _order(db, 9990)
    await db.execute(update(PaymentOrder).where(PaymentOrder.id == order.id)
                     .values(provider_external_ref="sc_2_74adb8287c5a486"))
    await db.commit()
    raw = DOC_EXAMPLES["webhook_paid_example"]["raw"].encode()
    sig = "sha256=" + cpp.sign_payload(json.loads(raw), TEST_SECRET)
    r = await client.post(URL, content=raw, headers={"Content-Type": "application/json", "X-Webhook-Signature": sig})
    assert r.status_code == 200, r.text
    ev = (await db.execute(select(WebhookEvent))).scalar_one()
    assert ev.raw_body == raw.decode() and ev.webhook_event_id == 42 and ev.subconta_id == 2 and ev.status == "RECEIVED"
    assert ("app.workers.tasks.financial.process_webhook_event", [str(ev.id)]) in fin_env.dispatched
    assert "authorization" not in ev.headers
    assert await payments.process_webhook_event(db, ev.id) == "PROCESSED"
    await db.commit()
    o = await db.get(PaymentOrder, order.id)
    await db.refresh(o)
    assert (o.status, o.fee_cents, o.net_cents, o.pix_end_to_end_id) == ("PAID", 299, 9691,
                                                                        "E9999901012026050718000000000001")
    assert await balance(db, "RD:Cash:CloudPayPag") == 9691          # líquido real
    assert await balance(db, "RD:Expense:GatewayFees") == 299        # taxa REAL do webhook (fee)
    assert await balance(db, "ADV:DeferredRevenue", org.id) == 9990  # receita diferida


@pytest.mark.parametrize("case", ["no_header", "wrong_sig", "altered_body", "raw_body_hmac", "wrong_secret"])
async def test_invalid_signature_is_401_and_nothing_persisted(db, client, fin_env, case):
    _, order = await _order(db)
    raw, headers = webhook_for(order)
    if case == "no_header":
        headers.pop(cpp.SIGNATURE_HEADER)
    elif case == "wrong_sig":
        headers[cpp.SIGNATURE_HEADER] = "sha256=" + "a" * 64
    elif case == "altered_body":
        raw = raw.replace(b'"status":"paid"', b'"status":"refunded"')
    elif case == "raw_body_hmac":
        import hashlib
        import hmac

        headers[cpp.SIGNATURE_HEADER] = "sha256=" + hmac.new(TEST_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        raw = json.dumps(json.loads(raw), indent=2).encode()  # não canônico: assinatura do corpo bruto não vale
    else:
        raw, headers = webhook_for(order, secret="outro-segredo")
    r = await client.post(URL, content=raw, headers=headers)
    assert r.status_code == 401
    assert await _count(db, WebhookEvent) == 0
    assert fin_env.dispatched == []


async def test_same_event_id_twice_sequential_has_one_effect(db, client, fin_env):
    _, order = await _order(db)
    raw, headers = webhook_for(order, event_id=777)
    r1 = await client.post(URL, content=raw, headers=headers)
    r2 = await client.post(URL, content=raw, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200 and r2.json() == {"ok": True, "duplicate": True}
    assert await _count(db, WebhookEvent) == 1
    ev = (await db.execute(select(WebhookEvent))).scalar_one()
    await payments.process_webhook_event(db, ev.id)
    await db.commit()
    assert await payments.process_webhook_event(db, ev.id) == "PROCESSED"  # reprocessar é inócuo
    await db.commit()
    assert await _count(db, LedgerTransaction, LedgerTransaction.kind == "PAYMENT_RECEIVED") == 1


async def test_same_event_id_concurrent_has_one_effect(db, client_factory, fin_env):
    _, order = await _order(db)
    raw, headers = webhook_for(order, event_id=4242)
    clients = [client_factory() for _ in range(6)]
    rs = await asyncio.gather(*[c.post(URL, content=raw, headers=headers) for c in clients])
    assert all(r.status_code == 200 for r in rs)
    assert sum(1 for r in rs if r.json().get("duplicate")) == 5
    assert await _count(db, WebhookEvent) == 1
    ids = [e for e in (await db.execute(select(WebhookEvent.id))).scalars()]
    # processamento concorrente do mesmo evento também tem um único efeito
    from app.core.database import session_factory

    async def proc(eng_db):
        async with session_factory(eng_db.bind)() as s:
            st = await payments.process_webhook_event(s, ids[0])
            await s.commit()
            return st

    await asyncio.gather(proc(db), proc(db))
    assert await _count(db, LedgerTransaction, LedgerTransaction.kind == "PAYMENT_RECEIVED") == 1
    assert await balance(db, "RD:Cash:CloudPayPag") == order.amount_cents - 299


async def test_paid_repeated_with_other_event_id_does_not_duplicate_revenue(db, fin_env):
    org, order = await _order(db, 20000)
    assert await pay(db, order, event_id=1) == "PROCESSED"
    assert await pay(db, order, event_id=2) == "IGNORED"
    assert await pay(db, order, event_id=3, fee_cents=500) == "IGNORED"
    assert await _count(db, LedgerTransaction, LedgerTransaction.kind == "PAYMENT_RECEIVED") == 1
    assert await balance(db, "ADV:DeferredRevenue", org.id) == 20000
    assert await balance(db, "RD:Cash:CloudPayPag") == 20000 - 299


async def test_out_of_order_events_are_monotonic(db, fin_env):
    org, order = await _order(db, 10000)
    # refunded antes do paid: contabiliza o recebimento (dinheiro entrou) e marca estorno externo p/ conciliação
    assert await pay(db, order, status="refunded", event_id=10) == "PROCESSED"
    assert await pay(db, order, status="paid", event_id=11) == "IGNORED"
    assert await pay(db, order, status="partially_refunded", event_id=12) == "IGNORED"
    o = await db.get(PaymentOrder, order.id)
    await db.refresh(o)
    assert o.status == "REFUNDED"
    assert await _count(db, LedgerTransaction, LedgerTransaction.kind == "PAYMENT_RECEIVED") == 1
    assert await _count(db, ReconciliationItem, ReconciliationItem.kind == "EXTERNAL_REFUND") == 1
    # expirada → paga depois: recebimento contabilizado + item de conciliação (pagamento tardio)
    _, o2 = await _order(db, 5000)
    assert await pay(db, o2, status="expired", event_id=20) == "PROCESSED"
    assert await pay(db, o2, status="paid", event_id=21) == "PROCESSED"
    assert await pay(db, o2, status="expired", event_id=22) == "IGNORED"
    o2 = await db.get(PaymentOrder, o2.id)
    await db.refresh(o2)
    assert o2.status == "PAID"
    assert await _count(db, ReconciliationItem, ReconciliationItem.kind == "PAID_AFTER_EXPIRY") == 1


async def test_divergent_amount_is_divergent_without_effects(db, fin_env):
    _, order = await _order(db, 10000)
    assert await pay(db, order, amount_cents=100, event_id=30) == "DIVERGENT"
    o = await db.get(PaymentOrder, order.id)
    await db.refresh(o)
    assert o.status == "PENDING"
    assert await _count(db, LedgerTransaction) == 0
    item = (await db.execute(select(ReconciliationItem))).scalar_one()
    assert item.kind == "CHARGE_AMOUNT_MISMATCH" and item.internal_amount_cents == 10000 and item.external_amount_cents == 100


async def test_wrong_subconta_is_rejected(db, client, fin_env):
    _, order = await _order(db)
    raw, headers = webhook_for(order, subconta_id=999, event_id=55)
    r = await client.post(URL, content=raw, headers=headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "WEBHOOK_SUBCONTA_MISMATCH"
    ev = (await db.execute(select(WebhookEvent))).scalar_one()
    assert ev.status == "REJECTED"
    assert fin_env.dispatched == []
    assert await payments.process_webhook_event(db, ev.id) == "REJECTED"  # nunca processa
    assert await _count(db, LedgerTransaction) == 0


async def test_instance_route_and_missing_secret(db, client, fin_env):
    _, order = await _order(db)
    raw, headers = webhook_for(order, event_id=60)
    assert (await client.post(URL + "/rd", content=raw, headers=headers)).status_code == 200
    assert (await client.post(URL + "/outra-instancia", content=raw, headers=headers)).status_code == 404
    from tests.fin_helpers import provider_config

    cpp.set_config_override(provider_config(secret=""))
    r = await client.post(URL, content=raw, headers=headers)
    assert r.status_code == 503 and r.json()["error"]["code"] == "WEBHOOK_SECRET_NOT_CONFIGURED"


async def test_raw_webhook_is_immutable(db, client, fin_env):
    _, order = await _order(db)
    await pay(db, order, event_id=70)
    with pytest.raises(DBAPIError):
        await db.execute(text("UPDATE webhook_events SET raw_body = '{}'"))
        await db.commit()
    await db.rollback()
    with pytest.raises(DBAPIError):
        await db.execute(text("DELETE FROM webhook_events"))
        await db.commit()
    await db.rollback()
