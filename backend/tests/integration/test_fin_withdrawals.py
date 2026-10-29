"""Saques do estabelecimento e chave PIX (HTTP + PostgreSQL reais): sem dupla aprovação,
bloqueio de 12 h, estabelecimento não troca chave, notificação, saldo insuficiente,
concorrência, saldo negativo e timeout ⇒ UNKNOWN sem retry."""
import asyncio
import uuid

import httpx
import pytest
import respx
from sqlalchemy import func, select, text

from app.core.errors import DomainError
from app.models.audit import AuditLog
from app.models.financial import EstablishmentPixKey, FinancialApprovalRequest, ReconciliationItem, Withdrawal
from app.models.notifications import Notification
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import ledger, withdrawals
from tests.factories import login_as, make_user, totp_code
from tests.fin_helpers import (  # noqa: F401
    balance, est_with_owner, fin_env, http_withdrawal_code, provider_config, scope,
)

F = "/api/v1/financial"


async def _credit(db, est, cents):
    exp = await ledger.account(db, "RD:Expense:Commission")
    av = await ledger.account(db, "EST:CommissionPayable:Available", owner_id=est.id, org_id=est.org_id)
    await ledger.post(db, kind="TEST_CREDIT", idempotency_key=f"c:{uuid.uuid4()}", legs=[(exp, cents), (av, -cents)],
                      description="comissão disponível (teste)")
    await db.commit()


async def _key(client, admin_s, est, *, ready=True, db=None):
    r = await client.post(f"{F}/establishments/{est.id}/pix-keys", json={
        "key_type": "cpf", "key_value": "123.456.789-09", "holder_name": "Fulano de Tal",
        "holder_document": "12345678909", "reason": "cadastro inicial pelo suporte"}, headers=admin_s.headers)
    assert r.status_code == 201, r.text
    if ready:
        await db.execute(text("UPDATE establishment_pix_keys SET active_from = now() - interval '1 second'"))
        await db.commit()
    return r.json()


def _w(est, amount, owner, email_code=None):
    body = {"establishment_id": str(est.id), "amount_cents": amount, "totp_code": totp_code(owner),
            "financial_password": owner.fin_password}
    if email_code:
        body["email_code"] = email_code
    return body


async def test_key_managed_only_by_staff_with_cooldown_audit_and_notification(db, client_factory, fin_env, mail):
    org, est, owner = await est_with_owner(db)
    admin = await make_user(db, role="admin", mfa=True)
    ca, co = client_factory(), client_factory()
    sa, so = await login_as(ca, admin), await login_as(co, owner)
    r = await co.post(f"{F}/establishments/{est.id}/pix-keys", json={
        "key_type": "cpf", "key_value": "98765432100", "holder_name": "Dono", "holder_document": "98765432100",
        "reason": "quero trocar minha chave"}, headers=so.headers)
    assert r.status_code == 403  # estabelecimento não altera chave
    k = await _key(ca, sa, est, ready=False, db=db)
    assert k["status"] == "COOLDOWN" and k["key_masked"] != "12345678909"
    row = (await db.execute(select(EstablishmentPixKey))).scalar_one()
    assert "12345678909" not in row.key_value_enc
    delta = row.active_from - row.created_at
    assert 11.9 * 3600 < delta.total_seconds() <= 12 * 3600 + 5
    n = (await db.execute(select(Notification).where(Notification.user_id == owner.id,
                                                     Notification.event_type == "pix_key.changed"))).scalars().all()
    assert len(n) == 1
    assert (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == "pix_key.changed"))).scalar_one() == 1
    # proprietário vê a chave mascarada
    lst = await co.get(f"{F}/establishments/{est.id}/pix-keys", headers=so.headers)
    assert lst.status_code == 200 and lst.json()["items"][0]["key_masked"] == k["key_masked"]
    # durante as 12 h: saque bloqueado
    await _credit(db, est, 5000)
    code = await http_withdrawal_code(co, so, est.id, 1000, mail, owner)
    r = await co.post(f"{F}/withdrawals", json=_w(est, 1000, owner, code), headers={**so.headers, "Idempotency-Key": "a1"})
    assert r.status_code == 423 and r.json()["error"]["code"] == "PIX_KEY_COOLDOWN"
    # passado o período: liberado (sem dupla aprovação)
    await db.execute(text("UPDATE establishment_pix_keys SET active_from = now() - interval '1 second'"))
    await db.commit()
    code = await http_withdrawal_code(co, so, est.id, 1000, mail, owner)
    r = await co.post(f"{F}/withdrawals", json=_w(est, 1000, owner, code), headers={**so.headers, "Idempotency-Key": "a2"})
    assert r.status_code == 201, r.text
    assert (await db.execute(select(func.count()).select_from(FinancialApprovalRequest))).scalar_one() == 0


async def test_withdrawal_reserves_balance_idempotent_and_wrong_password(db, client_factory, fin_env, mail):
    org, est, owner = await est_with_owner(db)
    admin = await make_user(db, role="admin", mfa=True)
    ca, co = client_factory(), client_factory()
    sa, so = await login_as(ca, admin), await login_as(co, owner)
    await _key(ca, sa, est, db=db)
    await _credit(db, est, 10000)
    bad = _w(est, 6000, owner)
    bad["financial_password"] = "errada-123456"
    r = await co.post(f"{F}/withdrawals", json=bad, headers={**so.headers, "Idempotency-Key": "k-bad"})
    assert r.status_code == 403
    r = await co.post(f"{F}/withdrawals", json=_w(est, 20000, owner, await http_withdrawal_code(co, so, est.id, 20000, mail,
                                                                                            owner)),
                      headers={**so.headers, "Idempotency-Key": "k0"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    body = _w(est, 6000, owner, await http_withdrawal_code(co, so, est.id, 6000, mail, owner))
    r1 = await co.post(f"{F}/withdrawals", json=body, headers={**so.headers, "Idempotency-Key": "k1"})
    assert r1.status_code == 201, r1.text
    r2 = await co.post(f"{F}/withdrawals", json=body, headers={**so.headers, "Idempotency-Key": "k1"})
    assert r2.status_code == 201 and r2.json()["id"] == r1.json()["id"]  # mesma chave ⇒ mesmo saque
    assert (await db.execute(select(func.count()).select_from(Withdrawal))).scalar_one() == 1
    assert await balance(db, "EST:CommissionPayable:Available", est.id) == 4000  # disponível − em andamento
    assert await balance(db, "EST:CommissionPayable:InTransit", est.id) == 6000
    wid = r1.json()["id"]
    assert ("app.workers.tasks.financial.execute_withdrawal", [wid]) in fin_env.dispatched
    bal = await co.get(f"{F}/establishments/{est.id}/balance", headers=so.headers)
    assert bal.json()["available_cents"] == 4000 and bal.json()["in_transit_cents"] == 6000


async def test_concurrent_withdrawals_same_balance_one_fails(db, fin_env, scope):
    org, est, owner = await est_with_owner(db, mfa=False)
    db.add(EstablishmentPixKey(
        id=uuid.uuid4(), establishment_id=est.id, key_type="email",
        key_value_enc=__import__("app.core.crypto", fromlist=["x"]).encrypt_str("a@b.com", purpose="pix_key",
                                                                                aad=f"est:{est.id}"),
        key_fingerprint="0" * 64, key_masked="a***@b.com", holder_name="T", holder_document_enc="v1:x",
        holder_document_masked="***", active_from=__import__("app.core.timeutils", fromlist=["x"]).utcnow(),
        created_by=owner.id))
    await db.commit()
    await _credit(db, est, 10000)

    async def attempt(i):
        try:
            async with scope() as s:
                from app.models.establishments import Establishment

                e = await s.get(Establishment, est.id)
                await withdrawals.create_establishment_withdrawal(s, e, 6000, requested_by=owner.id,
                                                                  idempotency_key=f"conc-{i}")
            return "ok"
        except DomainError as exc:
            return exc.code

    results = await asyncio.gather(*[attempt(i) for i in range(4)])
    assert sorted(results) == ["INSUFFICIENT_BALANCE"] * 3 + ["ok"]
    assert await balance(db, "EST:CommissionPayable:Available", est.id) == 4000


async def test_negative_balance_blocks_withdrawal(db, fin_env, scope):
    org, est, owner = await est_with_owner(db, mfa=False)
    await _credit(db, est, 10000)
    recv = await ledger.account(db, "EST:Receivable", owner_id=est.id, org_id=est.org_id)
    exp = await ledger.account(db, "RD:Expense:Commission")
    await ledger.post(db, kind="TEST_CLAWBACK", idempotency_key="neg", legs=[(recv, 300), (exp, -300)], description="x")
    await db.commit()
    from app.models.establishments import Establishment

    with pytest.raises(DomainError) as ei:
        await withdrawals.create_establishment_withdrawal(db, await db.get(Establishment, est.id), 100,
                                                          requested_by=owner.id, idempotency_key="neg-1")
    assert ei.value.status == 423 and ei.value.code == "WITHDRAWAL_BLOCKED_NEGATIVE_BALANCE"


async def test_other_establishment_cannot_see_or_withdraw(db, client_factory, fin_env):
    _, est_a, owner_a = await est_with_owner(db)
    _, est_b, owner_b = await est_with_owner(db)
    cb = client_factory()
    sb = await login_as(cb, owner_b)
    assert (await cb.get(f"{F}/establishments/{est_a.id}/balance", headers=sb.headers)).status_code == 404
    assert (await cb.get(f"{F}/establishments/{est_a.id}/pix-keys", headers=sb.headers)).status_code == 404
    r = await cb.post(f"{F}/withdrawals", json=_w(est_a, 100, owner_b), headers={**sb.headers, "Idempotency-Key": "x"})
    assert r.status_code == 404
    r = await cb.get(f"{F}/withdrawals", headers=sb.headers)
    assert r.status_code == 200 and r.json()["items"] == []


@pytest.mark.parametrize("outcome", ["timeout", "rejected", "completed"])
async def test_execution_outcomes_without_retry(db, fin_env, scope, outcome):
    org, est, owner = await est_with_owner(db, mfa=False)
    from app.core.crypto import encrypt_str
    from app.core.timeutils import utcnow
    from app.models.establishments import Establishment

    db.add(EstablishmentPixKey(id=uuid.uuid4(), establishment_id=est.id, key_type="cpf",
                               key_value_enc=encrypt_str("12345678909", purpose="pix_key", aad=f"est:{est.id}"),
                               key_fingerprint="1" * 64, key_masked="***", holder_name="T", holder_document_enc="v1:x",
                               holder_document_masked="***", active_from=utcnow(), created_by=owner.id))
    await db.commit()
    await _credit(db, est, 10000)
    w = await withdrawals.create_establishment_withdrawal(db, await db.get(Establishment, est.id), 10000,
                                                          requested_by=owner.id, idempotency_key=f"x-{outcome}")
    await db.commit()
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    url = "https://cloudpaypag.top/api/v1/integration/withdrawals"
    with respx.mock:
        if outcome == "timeout":
            route = respx.post(url).mock(side_effect=httpx.ConnectTimeout("t"))
        elif outcome == "rejected":
            route = respx.post(url).mock(return_value=httpx.Response(400, json={"detail": "Saldo insuficiente"}))
        else:
            route = respx.post(url).mock(return_value=httpx.Response(200, json={
                "withdrawal_id": 1, "external_id": "wd_ok_1", "status": "completed", "valor": 100.00, "taxa": 2.00,
                "valor_liquido": 98.00}))
        st1 = await withdrawals.execute_withdrawal(w.id, scope=scope)
        st2 = await withdrawals.execute_withdrawal(w.id, scope=scope)
        assert route.call_count == 1 and st1 == st2
    if outcome == "timeout":
        assert st1 == "UNKNOWN"
        assert await balance(db, "EST:CommissionPayable:InTransit", est.id) == 10000  # reserva mantida
        assert (await db.execute(select(ReconciliationItem.kind))).scalars().all() == ["WITHDRAWAL_UNKNOWN"]
    elif outcome == "rejected":
        assert st1 == "FAILED"
        assert await balance(db, "EST:CommissionPayable:Available", est.id) == 10000  # devolvido
        assert await balance(db, "EST:CommissionPayable:InTransit", est.id) == 0
    else:
        assert st1 == "COMPLETED"
        assert await balance(db, "EST:CommissionPayable:InTransit", est.id) == 0
        assert await balance(db, "RD:Cash:CloudPayPag") == -10000
        n = (await db.execute(select(Notification).where(Notification.event_type == "withdrawal.paid"))).scalars().all()
        assert len(n) == 1 and n[0].user_id == owner.id
    # estado do saque não regride (trigger)
    with pytest.raises(Exception):
        await db.execute(text(f"UPDATE withdrawals SET status = 'REQUESTED' WHERE id = '{w.id}'"))
        await db.commit()
    await db.rollback()
