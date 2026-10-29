"""Aprovação ÚNICA (modo SINGLE da instância) × DUPLA — HTTP real + PostgreSQL real (AÇÃO 003; ADR_011 D3/D4;
SPEC 003 §4.2, §5.3, §15-C).

SINGLE é um fluxo EXPLÍCITO: o próprio solicitante AUTORIZA (``/authorize``) com TOTP + senha financeira +
código por e-mail (finalidade ``self_authorization``); ``approved_by`` fica NULL. ``/approve`` e ``/reject``
⇒ 409 ``APPROVAL_MODE_SINGLE``. Em DUAL, ``/authorize`` ⇒ 409 ``APPROVAL_MODE_DUAL`` e, com o 2º Master
pendente, nenhuma criação passa (409 ``SECOND_MASTER_REQUIRED``) enquanto o resto do sistema segue."""
import uuid
from datetime import date

import httpx
import respx
from sqlalchemy import func, select

from app.core.settings_service import get_setting
from app.models.audit import AuditLog
from app.models.financial import (
    FinancialApprovalEvent, FinancialApprovalRequest, LedgerTransaction, Payable, Withdrawal,
)
from app.models.notifications import Notification
from app.models.settings import SystemSettingHistory
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import security as fin_security
from app.modules.financial import withdrawals
from tests.factories import login_as, totp_code
from tests.fin_helpers import (  # noqa: F401
    add_second_master, ctx_for, fin_env, http_approval_code, last_code, make_admin_masters, make_single_master,
    provider_config, scope,
)

API = "/api/v1/financial/approvals"
DUMMY = "123456"  # para tentativas recusadas ANTES do step-up (não gasta passo TOTP do usuário de teste)


async def _create(c, s, action="SETTING_CHANGE", payload=None, reason="Ajuste de pool aprovado em reunião",
                  expect=201, **extra):
    r = await c.post(API, json={"action_type": action, "payload": payload or {"key": "commission.pool_bps", "value": 400},
                                "reason": reason, **extra}, headers=s.headers)
    assert r.status_code == expect, r.text
    return r.json()


def _body(user, req, *, code=None, pw=None, email_code=None):
    body = {"totp_code": code or totp_code(user), "financial_password": pw or user.fin_password,
            "payload_sha256": req["payload_sha256"]}
    if email_code:
        body["email_code"] = email_code
    return body


def _err(r) -> str:
    return r.json()["error"]["code"]


async def test_single_master_authorizes_own_request_with_three_factors_and_executes_once(db, client, fin_env, mail):
    a = await make_single_master(db)
    s = await login_as(client, a)
    acts = (await client.get(f"{API}/actions", headers=s.headers)).json()
    assert acts["approval_mode"] == "SINGLE"
    assert any(i["action_type"] == "APPROVAL_MODE_CHANGE" and i["is_mode_change"] for i in acts["items"])
    req = await _create(client, s)
    assert req["status"] == "PENDING" and req["approval_mode"] == "SINGLE" and req["requires_email_code"]
    assert req["can_authorize"] is True and req["can_approve"] is False and req["is_mode_change"] is False
    # SINGLE: nenhum alerta de "aguardando o outro sócio"
    assert (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.event_type == "finance.approval_requested"))).scalar_one() == 0
    # rotas da dupla aprovação recusadas no modo único
    r = await client.post(f"{API}/{req['id']}/approve", json=_body(a, req, code=DUMMY), headers=s.headers)
    assert r.status_code == 409 and _err(r) == "APPROVAL_MODE_SINGLE"
    r = await client.post(f"{API}/{req['id']}/reject", json={"reason": "não quero mais"}, headers=s.headers)
    assert r.status_code == 409 and _err(r) == "APPROVAL_MODE_SINGLE"
    code = totp_code(a)  # falhas desfazem a transação (o passo TOTP não é consumido)
    r = await client.post(f"{API}/{req['id']}/authorize", json=_body(a, req, code=code), headers=s.headers)
    assert r.status_code == 403 and _err(r) == "FINANCIAL_EMAIL_CODE_REQUIRED"
    ecode = await http_approval_code(client, s, req["id"], mail, a)
    r = await client.post(f"{API}/{req['id']}/authorize", json=_body(a, req, code="000000", email_code=ecode),
                          headers=s.headers)
    assert r.status_code == 403 and _err(r) == "FINANCIAL_STEP_UP_INVALID"
    r = await client.post(f"{API}/{req['id']}/authorize", json=_body(a, req, code=code, pw="senha-errada-123",
                                                                     email_code=ecode), headers=s.headers)
    assert r.status_code == 403 and _err(r) == "FINANCIAL_STEP_UP_INVALID"
    wrong = "000000" if ecode != "000000" else "111111"
    r = await client.post(f"{API}/{req['id']}/authorize", json=_body(a, req, code=code, email_code=wrong),
                          headers=s.headers)
    assert r.status_code == 403 and _err(r) == "FINANCIAL_EMAIL_CODE_INVALID"
    assert await get_setting(db, "commission.pool_bps") == 500  # nada executado nas tentativas recusadas
    r = await client.post(f"{API}/{req['id']}/authorize", json=_body(a, req, code=code, email_code=ecode),
                          headers=s.headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "APPROVED" and out["execution_state"] == "EXECUTED"
    assert out["self_authorized_by"] == str(a.id) and out["self_authorized_by_name"] == "Sócio A"
    assert out["approved_by"] is None and out["can_authorize"] is False
    # não executa duas vezes
    r = await client.post(f"{API}/{req['id']}/authorize", json=_body(a, req, code=code, email_code=ecode),
                          headers=s.headers)
    assert r.status_code == 409 and _err(r) == "APPROVAL_NOT_PENDING"
    db.expire_all()
    assert await get_setting(db, "commission.pool_bps") == 400
    assert (await db.execute(select(func.count()).select_from(SystemSettingHistory)
                             .where(SystemSettingHistory.key == "commission.pool_bps"))).scalar_one() == 1
    row = await db.get(FinancialApprovalRequest, uuid.UUID(req["id"]))
    assert row.approval_mode == "SINGLE" and row.approved_by is None and row.self_authorized_by == a.id
    assert row.self_authorized_at is not None
    events = (await db.execute(select(FinancialApprovalEvent.event).where(
        FinancialApprovalEvent.request_id == row.id).order_by(FinancialApprovalEvent.created_at))).scalars().all()
    assert events[0] == "CREATED" and "SELF_AUTHORIZED" in events and "EXECUTION_EXECUTED" in events
    assert "APPROVED" not in events
    aud = (await db.execute(select(AuditLog).where(AuditLog.action == "finance.self_authorized"))).scalar_one()
    assert aud.after_data["factors"] == ["totp", "financial_password", "email"]
    assert aud.after_data["approval_mode"] == "SINGLE" and aud.actor_user_id == a.id
    sent = (await db.execute(select(AuditLog).where(AuditLog.action == "finance.email_code_sent"))).scalars().all()
    assert sent and all(x.entity_type == "self_authorization" for x in sent)


async def test_single_email_code_of_another_request_or_purpose_is_refused(db, client, fin_env, mail):
    a = await make_single_master(db)
    s = await login_as(client, a)
    r1 = await _create(client, s, payload={"key": "refund.window_days", "value": 9})
    r2 = await _create(client, s, payload={"key": "refund.window_days", "value": 10})
    code = totp_code(a)
    other = await http_approval_code(client, s, r1["id"], mail, a)  # código do pedido 1
    r = await client.post(f"{API}/{r2['id']}/authorize", json=_body(a, r2, code=code, email_code=other),
                          headers=s.headers)
    assert r.status_code == 403 and _err(r) == "FINANCIAL_EMAIL_CODE_INVALID"
    # código do MESMO pedido, mas de outra finalidade (a de aprovação DUAL) também não serve
    await fin_security.send_action_code(db, await ctx_for(db, a), purpose="approval", resource=r2["id"], summary="teste")
    await db.commit()
    wrong_purpose = last_code(mail, a)
    r = await client.post(f"{API}/{r2['id']}/authorize", json=_body(a, r2, code=code, email_code=wrong_purpose),
                          headers=s.headers)
    assert r.status_code == 403 and _err(r) == "FINANCIAL_EMAIL_CODE_INVALID"
    db.expire_all()
    assert await get_setting(db, "refund.window_days") == 7
    ok = await http_approval_code(client, s, r2["id"], mail, a)
    r = await client.post(f"{API}/{r2['id']}/authorize", json=_body(a, r2, code=code, email_code=ok), headers=s.headers)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert await get_setting(db, "refund.window_days") == 10


async def test_single_other_master_cannot_authorize_and_no_dual_alerts(db, client_factory, fin_env, mail):
    """SINGLE com dois Masters (ex.: depois de DUAL→SINGLE): cada um autoriza SÓ o que pediu."""
    a, b = await make_admin_masters(db, mode="SINGLE")
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    req = await _create(ca, sa)
    view_b = (await cb.get(f"{API}/{req['id']}", headers=sb.headers)).json()
    assert view_b["can_authorize"] is False and view_b["can_approve"] is False
    r = await cb.post(f"{API}/{req['id']}/email-code", headers=sb.headers)
    assert r.status_code == 403 and _err(r) == "ONLY_REQUESTER_AUTHORIZES"
    r = await cb.post(f"{API}/{req['id']}/authorize", json=_body(b, req, code=DUMMY, email_code=DUMMY),
                      headers=sb.headers)
    assert r.status_code == 403 and _err(r) == "ONLY_REQUESTER_AUTHORIZES"
    r = await cb.post(f"{API}/{req['id']}/approve", json=_body(b, req, code=DUMMY, email_code=DUMMY), headers=sb.headers)
    assert r.status_code == 409 and _err(r) == "APPROVAL_MODE_SINGLE"
    assert (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == b.id, Notification.event_type == "finance.approval_requested"))).scalar_one() == 0
    # no SINGLE o solicitante desiste cancelando (não há rejeição)
    r = await ca.post(f"{API}/{req['id']}/cancel", headers=sa.headers)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert await get_setting(db, "commission.pool_bps") == 500


async def test_single_rd_pix_out_executes_once(db, client, fin_env, mail, scope):
    a = await make_single_master(db)
    s = await login_as(client, a)
    req = await _create(client, s, action="RD_WITHDRAWAL",
                        payload={"amount_cents": 10000, "pix_key": "socios@rd.com.br", "pix_key_type": "email"},
                        reason="Retirada mensal do sócio")
    ecode = await http_approval_code(client, s, req["id"], mail, a)
    r = await client.post(f"{API}/{req['id']}/authorize", json=_body(a, req, email_code=ecode), headers=s.headers)
    assert r.status_code == 200, r.text
    assert r.json()["execution_state"] == "PENDING"
    w = (await db.execute(select(Withdrawal))).scalar_one()
    assert w.requested_by == a.id
    assert ("app.workers.tasks.financial.execute_withdrawal", [str(w.id)]) in fin_env.dispatched
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    resp = {"withdrawal_id": 16, "external_id": "wd_sc2_single1", "status": "processing", "valor": 100.00,
            "taxa": 2.00, "valor_liquido": 98.00}
    with respx.mock:
        post = respx.post("https://cloudpaypag.top/api/v1/integration/withdrawals").mock(
            return_value=httpx.Response(200, json=resp))
        assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "PROCESSING"
        await withdrawals.execute_withdrawal(w.id, scope=scope)  # reentrega da task: nada é reenviado
        assert post.call_count == 1
    db.expire_all()
    row = await db.get(FinancialApprovalRequest, uuid.UUID(req["id"]))
    assert row.self_authorized_by == a.id and row.approved_by is None


async def test_single_manual_expense_ledger_meta_records_mode(db, client, fin_env, mail):
    """Despesa manual em SINGLE: lançamentos com meta do modo (quem pediu = quem autorizou)."""
    a = await make_single_master(db)
    s = await login_as(client, a)
    r = await client.post("/api/v1/financial/payables", json={
        "description": "Servidor cloud", "category": "infraestrutura", "amount_cents": 45000,
        "competence_date": str(date.today()), "reason": "Fatura mensal do servidor"},
        headers=s.headers)
    assert r.status_code == 202, r.text
    exp = r.json()
    assert exp["approval_mode"] == "SINGLE" and exp["can_authorize"] is True
    ecode = await http_approval_code(client, s, exp["id"], mail, a)
    r = await client.post(f"{API}/{exp['id']}/authorize", json=_body(a, exp, email_code=ecode), headers=s.headers)
    assert r.status_code == 200, r.text
    pay = (await db.execute(select(Payable))).scalar_one()
    tx = await db.get(LedgerTransaction, pay.ledger_transaction_id)
    assert tx.meta["authorization_mode"] == "SINGLE"
    assert tx.meta["requested_by"] == str(a.id) and tx.meta["authorized_by"] == str(a.id)


async def test_dual_authorize_is_refused_and_requester_never_approves(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    req = await _create(ca, sa)
    assert req["approval_mode"] == "DUAL" and req["can_authorize"] is False and req["can_approve"] is False
    assert (await cb.get(f"{API}/{req['id']}", headers=sb.headers)).json()["can_approve"] is True
    r = await ca.post(f"{API}/{req['id']}/authorize", json=_body(a, req, code=DUMMY, email_code=DUMMY),
                      headers=sa.headers)
    assert r.status_code == 409 and _err(r) == "APPROVAL_MODE_DUAL"
    r = await cb.post(f"{API}/{req['id']}/authorize", json=_body(b, req, code=DUMMY, email_code=DUMMY),
                      headers=sb.headers)
    assert r.status_code == 409 and _err(r) == "APPROVAL_MODE_DUAL"
    r = await ca.post(f"{API}/{req['id']}/approve", json=_body(a, req, code=DUMMY, email_code=DUMMY), headers=sa.headers)
    assert r.status_code == 403 and _err(r) == "SELF_APPROVAL_FORBIDDEN"
    r = await ca.post(f"{API}/{req['id']}/email-code", headers=sa.headers)
    assert r.status_code == 403 and _err(r) == "SELF_APPROVAL_FORBIDDEN"
    # B aprova com os três fatores ⇒ executa (fluxo DUAL inalterado)
    ecode = await http_approval_code(cb, sb, req["id"], mail, b)
    r = await cb.post(f"{API}/{req['id']}/approve", json=_body(b, req, email_code=ecode), headers=sb.headers)
    assert r.status_code == 200, r.text
    assert r.json()["approved_by"] == str(b.id) and r.json()["self_authorized_by"] is None
    assert r.json()["execution_state"] == "EXECUTED"
    db.expire_all()
    assert await get_setting(db, "commission.pool_bps") == 400


async def test_dual_with_second_master_pending_blocks_only_dual_approval(db, client_factory, fin_env, mail):
    a = await make_single_master(db, mode="DUAL")  # DUAL ligado, 2º Master ainda não ativado
    ca = client_factory()
    sa = await login_as(ca, a)
    r = await ca.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "commission.pool_bps", "value": 400},
                                 "reason": "Ajuste de pool aprovado em reunião"}, headers=sa.headers)
    assert r.status_code == 409 and _err(r) == "SECOND_MASTER_REQUIRED"
    assert r.json()["error"]["details"]["approval_mode"] == "DUAL"
    assert "segundo Administrador Master" in r.json()["error"]["message"]
    r = await ca.post("/api/v1/financial/payables", json={
        "description": "Servidor cloud", "category": "infraestrutura", "amount_cents": 45000,
        "competence_date": str(date.today()), "reason": "Fatura mensal do servidor"},
        headers=sa.headers)
    assert r.status_code == 409 and _err(r) == "SECOND_MASTER_REQUIRED"
    assert (await db.execute(select(func.count()).select_from(FinancialApprovalRequest))).scalar_one() == 0
    # o resto do sistema segue
    assert (await ca.get("/api/v1/me", headers=sa.headers)).status_code == 200
    assert (await ca.get(API, headers=sa.headers)).status_code == 200
    assert (await ca.get(f"{API}/actions", headers=sa.headers)).json()["approval_mode"] == "DUAL"
    assert (await ca.get("/api/v1/settings", headers=sa.headers)).status_code == 200
    # ativado o 2º Master, a dupla aprovação é liberada
    await add_second_master(db)
    req = await _create(ca, sa)
    assert req["approval_mode"] == "DUAL" and req["status"] == "PENDING"
