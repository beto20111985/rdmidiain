"""Dupla aprovação dos dois Admin Master (HTTP real + PostgreSQL real): autoaprovação
proibida, TOTP errado/reusado, senha financeira, expiração, adulteração de payload,
concorrência, terceiro Admin Master impossível e PIX OUT da RD sem retry."""
import asyncio
import re
import uuid
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.settings_service import get_setting
from app.core.timeutils import utcnow
from app.models.financial import FinancialApprovalEvent, FinancialApprovalRequest, Payable, Withdrawal
from app.models.notifications import Notification
from app.models.settings import SystemSettingHistory
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import common, withdrawals
from tests.factories import login_as, make_user, totp_code
from tests.fin_helpers import (  # noqa: F401
    balance, fin_env, http_approval_code, last_code, make_admin_masters, provider_config, scope,
)

API = "/api/v1/financial/approvals"
TAMPERED = '{"key": "commission.pool_bps", "value": 10000}'


async def _setup(db, client_factory):
    a, b = await make_admin_masters(db)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    return a, b, ca, cb, sa, sb


async def _create(c, s, action="SETTING_CHANGE", payload=None, reason="Ajuste de pool aprovado em reunião"):
    r = await c.post(API, json={"action_type": action, "payload": payload or {"key": "commission.pool_bps", "value": 400},
                                "reason": reason}, headers=s.headers)
    assert r.status_code == 201, r.text
    return r.json()


def _approve_body(user, req, *, code=None, pw=None, email_code=None):
    body = {"totp_code": code or totp_code(user), "financial_password": pw or user.fin_password,
            "payload_sha256": req["payload_sha256"]}
    if email_code:
        body["email_code"] = email_code
    return body


async def _ok_body(c, s, mail, user, req, **kw):
    """Corpo válido com os três fatores (o código por e-mail é pedido pela rota real)."""
    return _approve_body(user, req, email_code=await http_approval_code(c, s, req["id"], mail, user), **kw)


async def test_requester_cannot_approve_and_other_master_executes_once(db, client_factory, fin_env, mail):
    a, b, ca, cb, sa, sb = await _setup(db, client_factory)
    req = await _create(ca, sa)
    assert req["status"] == "PENDING" and req["requested_by"] == str(a.id)
    # o outro sócio recebe o alerta de pendência financeira
    n = (await db.execute(select(Notification).where(Notification.user_id == b.id,
                                                     Notification.event_type == "finance.approval_requested"))).scalars().all()
    assert len(n) == 1
    r = await ca.post(f"{API}/{req['id']}/approve", json=_approve_body(a, req), headers=sa.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SELF_APPROVAL_FORBIDDEN"
    r = await ca.post(f"{API}/{req['id']}/reject", json={"reason": "não quero"}, headers=sa.headers)
    assert r.status_code == 403
    assert await get_setting(db, "commission.pool_bps") == 500
    r = await cb.post(f"{API}/{req['id']}/approve", json=await _ok_body(cb, sb, mail, b, req), headers=sb.headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED" and r.json()["execution_state"] == "EXECUTED"
    db.expire_all()
    assert await get_setting(db, "commission.pool_bps") == 400
    assert (await db.execute(select(func.count()).select_from(SystemSettingHistory)
                             .where(SystemSettingHistory.key == "commission.pool_bps"))).scalar_one() == 1
    # aprovar de novo: não executa duas vezes
    r = await cb.post(f"{API}/{req['id']}/approve", json=_approve_body(b, req), headers=sb.headers)
    assert r.status_code == 409
    # CHECK no banco: aprovador ≠ solicitante mesmo sem passar pelo serviço
    with pytest.raises(DBAPIError):
        await db.execute(text("SET LOCAL session_replication_role = replica"))
        await db.execute(text("UPDATE financial_approval_requests SET approved_by = requested_by"))
        await db.commit()
    await db.rollback()
    events = (await db.execute(select(FinancialApprovalEvent.event).where(
        FinancialApprovalEvent.request_id == uuid.UUID(req["id"])).order_by(FinancialApprovalEvent.created_at))).scalars().all()
    assert events[0] == "CREATED" and "APPROVED" in events and "EXECUTION_EXECUTED" in events


async def test_wrong_totp_reused_totp_and_wrong_financial_password(db, client_factory, fin_env, mail):
    a, b, ca, cb, sa, sb = await _setup(db, client_factory)
    req = await _create(ca, sa)
    bad = await cb.post(f"{API}/{req['id']}/approve", json=_approve_body(b, req, code="000000"), headers=sb.headers)
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "FINANCIAL_STEP_UP_INVALID"
    wrong_pw = await cb.post(f"{API}/{req['id']}/approve", json=_approve_body(b, req, pw="senha-errada-123"),
                             headers=sb.headers)
    assert wrong_pw.status_code == 403
    login_code = totp_code(b, fresh=False)  # mesmo passo usado no login (já consumido)
    reused = await cb.post(f"{API}/{req['id']}/approve", json=_approve_body(b, req, code=login_code), headers=sb.headers)
    assert reused.status_code == 403
    good_code = totp_code(b)
    ok = await cb.post(f"{API}/{req['id']}/approve", json=await _ok_body(cb, sb, mail, b, req, code=good_code),
                       headers=sb.headers)
    assert ok.status_code == 200
    req2 = await _create(ca, sa, payload={"key": "refund.window_days", "value": 10})
    replay = await cb.post(f"{API}/{req2['id']}/approve", json=_approve_body(b, req2, code=good_code), headers=sb.headers)
    assert replay.status_code == 403  # TOTP reusado (anti-replay)
    db.expire_all()
    assert await get_setting(db, "refund.window_days") == 7


async def test_expired_and_mismatched_and_tampered_payload(db, client_factory, fin_env):
    a, b, ca, cb, sa, sb = await _setup(db, client_factory)
    # expirado
    exp = FinancialApprovalRequest(id=uuid.uuid4(), action_type="SETTING_CHANGE",
                                   payload={"key": "refund.window_days", "value": 9},
                                   payload_sha256=common.canonical_sha256({"key": "refund.window_days", "value": 9}),
                                   reason="pedido antigo de teste", requested_by=a.id, status="PENDING",
                                   expires_at=utcnow() - timedelta(minutes=1), execution_state="NOT_STARTED",
                                   idempotency_key=uuid.uuid4().hex, approval_mode="DUAL")
    db.add(exp)
    await db.commit()
    req_exp = {"id": str(exp.id), "payload_sha256": exp.payload_sha256}
    r = await cb.post(f"{API}/{exp.id}/approve", json=_approve_body(b, req_exp), headers=sb.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "APPROVAL_EXPIRED"
    from app.modules.financial import approvals

    assert await approvals.expire_requests(db) == 1
    await db.commit()
    # hash revisado diferente do pedido
    req = await _create(ca, sa)
    body = _approve_body(b, req)
    body["payload_sha256"] = "0" * 64
    r = await cb.post(f"{API}/{req['id']}/approve", json=body, headers=sb.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "APPROVAL_PAYLOAD_MISMATCH"
    # adulteração: trigger impede UPDATE do payload...
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("UPDATE financial_approval_requests SET payload = CAST(:p AS jsonb) WHERE id = :i")
                         .bindparams(p=TAMPERED, i=uuid.UUID(req["id"])))
        await db.commit()
    await db.rollback()
    assert "RD_IMMUTABLE" in str(ei.value)
    # ...e mesmo contornando o trigger (superusuário), a aprovação detecta pelo SHA-256
    await db.execute(text("SET LOCAL session_replication_role = replica"))
    await db.execute(text("UPDATE financial_approval_requests SET payload = CAST(:p AS jsonb) WHERE id = :i")
                     .bindparams(p=TAMPERED, i=uuid.UUID(req["id"])))
    await db.commit()
    r = await cb.post(f"{API}/{req['id']}/approve", json=_approve_body(b, req), headers=sb.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "APPROVAL_PAYLOAD_TAMPERED"
    db.expire_all()
    assert await get_setting(db, "commission.pool_bps") == 500


async def test_concurrent_approvals_by_b_only_one_wins(db, client_factory, fin_env, mail):
    a, b, ca, cb, sa, sb = await _setup(db, client_factory)
    req = await _create(ca, sa)
    body = await _ok_body(cb, sb, mail, b, req)
    c1, c2 = client_factory(), client_factory()
    r1, r2 = await asyncio.gather(c1.post(f"{API}/{req['id']}/approve", json=body, headers=sb.headers),
                                  c2.post(f"{API}/{req['id']}/approve", json=body, headers=sb.headers))
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], (r1.text, r2.text)
    assert (await db.execute(select(func.count()).select_from(SystemSettingHistory))).scalar_one() == 1


async def test_third_admin_master_impossible_and_non_master_cannot_request(db, client, fin_env):
    a, b = await make_admin_masters(db)
    with pytest.raises(IntegrityError):
        await make_user(db, role="super_admin", admin_master_seat=3)
    await db.rollback()
    with pytest.raises(IntegrityError):
        await make_user(db, role="super_admin", admin_master_seat=1)
    await db.rollback()
    sa3 = await make_user(db, role="super_admin", mfa=True)  # super admin SEM assento
    s3 = await login_as(client, sa3)
    r = await client.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "commission.pool_bps", "value": 1},
                                     "reason": "tentativa de terceiro"}, headers=s3.headers)
    assert r.status_code == 403
    fin = await make_user(db, role="financial", mfa=True)
    sf = await login_as(client, fin)
    r = await client.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "commission.pool_bps", "value": 1},
                                     "reason": "tentativa do financeiro"}, headers=sf.headers)
    assert r.status_code == 403
    assert (await client.get(API, headers=sf.headers)).status_code == 200  # financeiro VÊ as pendências


async def test_rd_pix_out_needs_email_code_and_executes_once_without_retry(db, client_factory, fin_env, mail, scope):
    a, b, ca, cb, sa, sb = await _setup(db, client_factory)
    req = await _create(ca, sa, action="RD_WITHDRAWAL",
                        payload={"amount_cents": 10000, "pix_key": "123.456.789-09", "pix_key_type": "cpf"},
                        reason="Retirada mensal dos sócios")
    assert req["payload"]["pix_key_masked"] and "12345678909" not in str(req)  # chave cifrada no pedido
    raw_payload = (await db.execute(text(f"SELECT payload::text FROM financial_approval_requests WHERE id='{req['id']}'"))).scalar_one()
    assert "12345678909" not in raw_payload
    r = await cb.post(f"{API}/{req['id']}/approve", json=_approve_body(b, req), headers=sb.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FINANCIAL_EMAIL_CODE_REQUIRED"
    code = await http_approval_code(cb, sb, req["id"], mail, b)
    r = await cb.post(f"{API}/{req['id']}/approve", json=_approve_body(b, req, email_code=code), headers=sb.headers)
    assert r.status_code == 200, r.text
    assert r.json()["execution_state"] == "PENDING"
    w = (await db.execute(select(Withdrawal))).scalar_one()
    assert ("app.workers.tasks.financial.execute_withdrawal", [str(w.id)]) in fin_env.dispatched
    # execução no worker: timeout ⇒ UNKNOWN, uma única chamada, sem retry
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    with respx.mock:
        route = respx.post("https://cloudpaypag.top/api/v1/integration/withdrawals").mock(
            side_effect=httpx.ReadTimeout("timeout"))
        assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "UNKNOWN"
        assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "UNKNOWN"  # reentrega: nada acontece
        assert route.call_count == 1
    db.expire_all()
    req_row = await db.get(FinancialApprovalRequest, uuid.UUID(req["id"]))
    assert req_row.execution_state == "UNKNOWN"
    assert await balance(db, "RD:Cash:CloudPayPag") == 0  # nada baixado sem confirmação


async def test_rd_pix_out_success_settles_ledger(db, client_factory, fin_env, mail, scope):
    a, b, ca, cb, sa, sb = await _setup(db, client_factory)
    req = await _create(ca, sa, action="RD_WITHDRAWAL",
                        payload={"amount_cents": 10000, "pix_key": "socios@rd.com.br", "pix_key_type": "email"},
                        reason="Retirada mensal dos sócios")
    code = await http_approval_code(cb, sb, req["id"], mail, b)
    assert (await cb.post(f"{API}/{req['id']}/approve", json=_approve_body(b, req, email_code=code),
                          headers=sb.headers)).status_code == 200
    w = (await db.execute(select(Withdrawal))).scalar_one()
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    resp = {"withdrawal_id": 15, "external_id": "wd_sc2_test1", "status": "processing", "valor": 100.00, "taxa": 2.00,
            "valor_liquido": 98.00}
    with respx.mock:
        post = respx.post("https://cloudpaypag.top/api/v1/integration/withdrawals").mock(
            return_value=httpx.Response(200, json=resp))
        assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "PROCESSING"
        assert post.calls.last.request.content == b'{"valor":100.00,"pix_key":"socios@rd.com.br","pix_key_type":"email"}'
        respx.get("https://cloudpaypag.top/api/v1/integration/withdrawals/wd_sc2_test1").mock(
            return_value=httpx.Response(200, json={**resp, "status": "completed"}))
        async with scope() as s:
            ww = await s.get(Withdrawal, w.id, with_for_update=True)
            assert await withdrawals.refresh_withdrawal(s, ww) == "COMPLETED"
    assert await balance(db, "RD:Cash:CloudPayPag") == -10000
    assert await balance(db, "RD:Equity:Withdrawals") == -9800  # PL (natureza credora): retirada debita
    assert await balance(db, "RD:Expense:GatewayFees") == 200
    db.expire_all()
    assert (await db.get(FinancialApprovalRequest, uuid.UUID(req["id"]))).execution_state == "EXECUTED"


async def test_manual_expense_only_after_second_master_approval(db, client_factory, fin_env, mail):
    a, b, ca, cb, sa, sb = await _setup(db, client_factory)
    r = await ca.post("/api/v1/financial/payables", json={
        "description": "Servidor cloud", "category": "infraestrutura", "amount_cents": 45000,
        "competence_date": str(__import__("datetime").date.today()), "reason": "Fatura mensal do servidor"},
        headers=sa.headers)
    assert r.status_code == 202, r.text
    req = r.json()
    assert (await db.execute(select(func.count()).select_from(Payable))).scalar_one() == 0  # nada antes da aprovação
    r = await cb.post(f"{API}/{req['id']}/approve", json=await _ok_body(cb, sb, mail, b, req), headers=sb.headers)
    assert r.status_code == 200, r.text
    pay = (await db.execute(select(Payable))).scalar_one()
    assert pay.amount_cents == 45000 and pay.status == "OPEN" and pay.approval_request_id == uuid.UUID(req["id"])
    assert await balance(db, "RD:AccountsPayable") == 45000
    assert await balance(db, "RD:Expense:Opex:infraestrutura") == 45000
