"""REV (AÇÃO 002): saque pedido ANTES da troca da chave PIX nunca é pago para a chave revogada.

Defesa em profundidade, PostgreSQL real + gateway via respx (contagem de chamadas):
(1) troca/revogação da chave CANCELA saques ainda não executados (manual e automático do dia 1),
    motivo ``PIX_KEY_CHANGED``, reserva devolvida ao saldo, auditoria e notificação ao dono;
(2) ``execute_withdrawal`` revalida sob lock a chave VIGENTE (não revogada, fora das 12 h) ANTES de
    qualquer chamada externa — senão cancela sem chamar o gateway;
saque pedido durante o período de 12 h é recusado; caminho feliz paga a chave vigente."""
import json
import uuid
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select, text

from app.core.crypto import encrypt_str
from app.core.errors import DomainError
from app.core.timeutils import utcnow
from app.models.establishments import Establishment
from app.models.financial import EstablishmentPixKey, Withdrawal
from app.models.notifications import Notification
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import ledger, withdrawals
from tests.factories import login_as, make_user
from tests.fin_helpers import balance, ctx_for, est_with_owner, fin_env, provider_config, scope  # noqa: F401

F = "/api/v1/financial"
URL = "https://cloudpaypag.top/api/v1/integration/withdrawals"
OLD_KEY = "12345678909"


async def _setup(db, *, cents=10000):
    org, est, owner = await est_with_owner(db, mfa=False)
    db.add(EstablishmentPixKey(
        id=uuid.uuid4(), establishment_id=est.id, key_type="cpf",
        key_value_enc=encrypt_str(OLD_KEY, purpose="pix_key", aad=f"est:{est.id}"), key_fingerprint="1" * 64,
        key_masked="***456***09", holder_name="Antigo", holder_document_enc="v1:x", holder_document_masked="***",
        active_from=utcnow() - timedelta(hours=13), created_by=owner.id))
    exp = await ledger.account(db, "RD:Expense:Commission")
    av = await ledger.account(db, "EST:CommissionPayable:Available", owner_id=est.id, org_id=est.org_id)
    await ledger.post(db, kind="TEST_CREDIT", idempotency_key=f"c:{uuid.uuid4()}", legs=[(exp, cents), (av, -cents)],
                      description="comissão disponível")
    await db.commit()
    return org, est, owner


async def _execute(wid, scope):
    """Executa com o cliente REAL do adapter contra o gateway simulado (respx) e conta as chamadas."""
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    try:
        with respx.mock:
            route = respx.post(URL).mock(return_value=httpx.Response(200, json={
                "withdrawal_id": 1, "external_id": f"wd_t_{uuid.uuid4().hex[:8]}", "status": "processing",
                "valor": 50.00, "taxa": 0.0, "valor_liquido": 50.00}))
            status = await withdrawals.execute_withdrawal(wid, scope=scope)
            return status, [json.loads(c.request.content) for c in route.calls]
    finally:
        cpp.set_provider_override(None)


async def _w(db, wid) -> Withdrawal:
    db.expunge_all()
    return await db.get(Withdrawal, wid)


async def test_rev_requested_before_key_change_is_cancelled_and_gateway_never_called(db, client_factory, fin_env,
                                                                                      scope):
    org, est, owner = await _setup(db)
    w = await withdrawals.create_establishment_withdrawal(db, await db.get(Establishment, est.id), 5000,
                                                          requested_by=owner.id, idempotency_key="rev-old-key")
    await db.commit()
    assert w.status == "REQUESTED" and await balance(db, "EST:CommissionPayable:InTransit", est.id) == 5000
    admin = await make_user(db, role="admin", mfa=True)
    ca = client_factory()
    r = await ca.post(f"{F}/establishments/{est.id}/pix-keys", headers=(await login_as(ca, admin)).headers, json={
        "key_type": "cpf", "key_value": "98765432100", "holder_name": "Novo Titular", "holder_document": "98765432100",
        "reason": "chave antiga comprometida"})
    assert r.status_code == 201, r.text
    w = await _w(db, w.id)
    assert w.status == "CANCELLED" and w.failure_reason.startswith("PIX_KEY_CHANGED")
    assert await balance(db, "EST:CommissionPayable:Available", est.id) == 10000  # reserva devolvida
    assert await balance(db, "EST:CommissionPayable:InTransit", est.id) == 0
    n = (await db.execute(select(Notification).where(Notification.user_id == owner.id,
                                                     Notification.event_type == "withdrawal.failed"))).scalar_one()
    assert "PIX_KEY_CHANGED" in n.body
    aud = (await db.execute(text("SELECT after_data FROM audit_logs WHERE action = 'pix_key.changed' "
                                 "AND entity_id = :e"), {"e": str(est.id)})).scalar_one()
    assert aud["cancelled_withdrawals"] == [str(w.id)]
    # o worker tenta executar depois da troca: nada sai
    status, sent = await _execute(w.id, scope)
    assert status == "CANCELLED" and sent == []


async def test_execute_revalidates_current_key_under_lock_without_calling_gateway(db, fin_env, scope):
    org, est, owner = await _setup(db)
    e = await db.get(Establishment, est.id)
    w1 = await withdrawals.create_establishment_withdrawal(db, e, 3000, requested_by=owner.id, idempotency_key="l2-a")
    await db.commit()
    # chave revogada por um caminho que NÃO passou pela camada 1 (ex.: corrida/escrita direta)
    await db.execute(text("UPDATE establishment_pix_keys SET revoked_at = now() WHERE establishment_id = :e"),
                     {"e": est.id})
    await db.commit()
    status, sent = await _execute(w1.id, scope)
    assert status == "CANCELLED" and sent == []
    w1 = await _w(db, w1.id)
    assert w1.failure_reason.startswith("PIX_KEY_CHANGED") and w1.executing_at is None
    assert await balance(db, "EST:CommissionPayable:Available", est.id) == 10000
    assert await withdrawals.execute_withdrawal(w1.id, scope=scope) == "CANCELLED"  # sem retry automático

    # chave vigente, mas (re)entrou no período de segurança de 12 h ⇒ PIX_KEY_COOLDOWN, sem chamada
    org2, est2, owner2 = await _setup(db)
    w2 = await withdrawals.create_establishment_withdrawal(db, await db.get(Establishment, est2.id), 2000,
                                                           requested_by=owner2.id, idempotency_key="l2-b")
    await db.commit()
    await db.execute(text("UPDATE establishment_pix_keys SET active_from = now() + interval '12 hours' "
                          "WHERE establishment_id = :e"), {"e": est2.id})
    await db.commit()
    status, sent = await _execute(w2.id, scope)
    assert status == "CANCELLED" and sent == []
    assert (await _w(db, w2.id)).failure_reason.startswith("PIX_KEY_COOLDOWN")
    assert await balance(db, "EST:CommissionPayable:Available", est2.id) == 10000


async def test_queued_automatic_payout_is_cancelled_on_key_change_and_cooldown_refuses_new(db, fin_env, scope):
    org, est, owner = await _setup(db, cents=500)
    res = await withdrawals.auto_payouts(db)
    await db.commit()
    assert res["created"] == 1
    auto = (await db.execute(select(Withdrawal).where(Withdrawal.establishment_id == est.id))).scalar_one()
    assert (auto.origin, auto.status, auto.amount_cents) == ("AUTOMATIC", "REQUESTED", 500)
    admin = await make_user(db, role="admin")
    await withdrawals.set_pix_key(db, await ctx_for(db, admin), est.id, key_type="email", key_value="novo@ex.com",
                                  holder_name="Novo Titular", holder_document="98765432100",
                                  reason="troca pedida pelo proprietário")
    await db.commit()
    auto = await _w(db, auto.id)
    assert auto.status == "CANCELLED" and auto.failure_reason.startswith("PIX_KEY_CHANGED")
    assert await balance(db, "EST:CommissionPayable:Available", est.id) == 500
    status, sent = await _execute(auto.id, scope)
    assert status == "CANCELLED" and sent == []
    # durante as 12 h da nova chave: saque recusado (manual) e o automático não recria
    with pytest.raises(DomainError) as ei:
        await withdrawals.create_establishment_withdrawal(db, await db.get(Establishment, est.id), 100,
                                                          requested_by=owner.id, idempotency_key="cooldown")
    assert (ei.value.code, ei.value.status) == ("PIX_KEY_COOLDOWN", 423)
    await db.rollback()
    res = await withdrawals.auto_payouts(db)
    await db.commit()
    assert res["created"] == 0
    assert (await db.execute(select(Withdrawal).where(Withdrawal.establishment_id == est.id,
                                                      Withdrawal.status == "REQUESTED"))).first() is None


async def test_happy_path_pays_current_active_key(db, fin_env, scope):
    org, est, owner = await _setup(db)
    w = await withdrawals.create_establishment_withdrawal(db, await db.get(Establishment, est.id), 5000,
                                                          requested_by=owner.id, idempotency_key="happy")
    await db.commit()
    status, sent = await _execute(w.id, scope)
    assert status == "PROCESSING" and len(sent) == 1
    assert sent[0]["pix_key"] == OLD_KEY and sent[0]["pix_key_type"] == "cpf"
