"""Transições do modo de aprovação financeira da instância pela ação ``APPROVAL_MODE_CHANGE`` (AÇÃO 003;
ADR_011 D2; SPEC 003 §4.2, §15-C) — PostgreSQL real, função SECURITY DEFINER real, selos reais.

- SINGLE→DUAL: autorização única com os três fatores; pendentes SINGLE cancelados; a partir daí a dupla
  aprovação exige o 2º Master ativo (409 ``SECOND_MASTER_REQUIRED`` até a ativação).
- DUAL→SINGLE: A solicita COM step-up (TOTP + senha financeira) → B aprova com os três fatores; A nunca
  aprova o próprio; impossível com o 2º Master pendente; pendentes DUAL cancelados; histórico com A e B.
"""
import uuid

import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.core.settings_service import get_setting
from app.models.audit import AuditLog
from app.models.financial import FinancialApprovalEvent, FinancialApprovalRequest
from app.models.notifications import Notification
from app.models.security import InstanceApprovalModeHistory
from app.modules.financial import approvals
from app.modules.security import approval_mode
from tests.factories import login_as, totp_code
from tests.fin_helpers import (  # noqa: F401
    add_second_master, approve_svc, authorize_svc, ctx_for, fin_env, http_approval_code, make_admin_masters,
    make_single_master,
)

API = "/api/v1/financial/approvals"
REASON = "Mudança do modo de aprovação decidida pelos sócios em ata"
DUMMY = "123456"


def _err(r) -> str:
    return r.json()["error"]["code"]


async def _history(db) -> list:
    db.expire_all()
    return list((await db.execute(select(InstanceApprovalModeHistory)
                                  .order_by(InstanceApprovalModeHistory.mode_version))).scalars().all())


async def _status(db, req_id) -> str:
    db.expire_all()
    return (await db.get(FinancialApprovalRequest, uuid.UUID(str(req_id)))).status


async def test_single_to_dual_then_dual_to_single_full_round_trip(db, redis, fin_env, mail):
    a = await make_single_master(db)
    ctx_a = await ctx_for(db, a)
    # pendente do modo antigo (SINGLE)
    pending_single = await approvals.create_request(db, ctx_a, "SETTING_CHANGE",
                                                    {"key": "refund.window_days", "value": 9}, "janela de reembolso")
    await db.commit()
    pending_single_id = pending_single.id
    # validações da ação
    for payload, reason, code in (
        ({"target_mode": "DUAL"}, "curto demais", "REASON_REQUIRED"),          # motivo < 20
        ({"target_mode": "SINGLE"}, REASON, "APPROVAL_MODE_CHANGE_INVALID"),   # já é SINGLE
        ({"target_mode": "TRIPLE"}, REASON, "APPROVAL_MODE_CHANGE_INVALID"),
        ({"target_mode": "DUAL", "from_mode": "DUAL", "expected_version": 9}, REASON, "APPROVAL_MODE_CHANGE_INVALID"),
    ):
        with pytest.raises(DomainError) as ei:
            await approvals.create_request(db, await ctx_for(db, a), "APPROVAL_MODE_CHANGE", payload, reason)
        assert ei.value.code == code and ei.value.status == 400, (payload, ei.value.code)
        await db.rollback()
    # SINGLE→DUAL: o próprio Master autoriza (fluxo SINGLE) com os três fatores
    m1 = await approvals.create_request(db, await ctx_for(db, a), "APPROVAL_MODE_CHANGE", {"target_mode": "DUAL"},
                                        REASON)
    await db.commit()
    m1_id = m1.id
    assert m1.approval_mode == "SINGLE" and m1.amount_cents is None
    assert m1.payload == {"target_mode": "DUAL", "from_mode": "SINGLE", "expected_version": 1}
    out = await authorize_svc(db, a, m1, mail)
    assert out.status == "APPROVED" and out.execution_state == "EXECUTED" and out.execution_ref == "mode_version:2"
    state = await approval_mode.current_state(db)
    assert (state.mode, state.version) == ("DUAL", 2)
    h = await _history(db)
    assert [(x.mode_version, x.kind, x.to_mode) for x in h] == [(1, "INITIALIZED", "SINGLE"), (2, "SINGLE_TO_DUAL", "DUAL")]
    assert h[1].requested_by == a.id and h[1].authorized_by == a.id and h[1].approval_request_id == m1_id
    assert h[1].reason == REASON
    # pendente SINGLE cancelado na mesma transação (evento + auditoria + aviso)
    assert await _status(db, pending_single_id) == "CANCELLED"
    ev = (await db.execute(select(FinancialApprovalEvent.event).where(
        FinancialApprovalEvent.request_id == pending_single_id))).scalars().all()
    assert "CANCELLED_MODE_CHANGE" in ev
    assert (await db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "finance.approval_cancelled_mode_change"))).scalar_one() == 1
    assert (await db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "security.approval_mode_changed"))).scalar_one() == 1
    # DUAL efetivo com 2º Master ainda não ativo: nenhuma operação de dupla aprovação passa
    with pytest.raises(DomainError) as ei:
        await approvals.create_request(db, await ctx_for(db, a), "SETTING_CHANGE",
                                       {"key": "refund.window_days", "value": 10}, "janela de reembolso")
    assert ei.value.code == "SECOND_MASTER_REQUIRED" and ei.value.status == 409
    await db.rollback()
    # ativado o 2º Master: dupla aprovação liberada
    b = await add_second_master(db)
    nd = await approvals.create_request(db, await ctx_for(db, a), "SETTING_CHANGE",
                                        {"key": "refund.window_days", "value": 10}, "janela de reembolso")
    await db.commit()
    assert nd.approval_mode == "DUAL" and nd.status == "PENDING"
    pending_dual = nd.id
    # DUAL→SINGLE: B solicita com step-up, A aprova com três fatores ⇒ versão 3
    m2 = await approvals.create_request(db, await ctx_for(db, b), "APPROVAL_MODE_CHANGE", {"target_mode": "SINGLE"},
                                        REASON, step_up={"totp_code": totp_code(b), "financial_password": b.fin_password})
    await db.commit()
    assert m2.approval_mode == "DUAL" and m2.payload["expected_version"] == 2
    out = await approve_svc(db, a, m2, mail)
    assert out.execution_ref == "mode_version:3"
    h = await _history(db)
    assert [(x.mode_version, x.kind, x.to_mode) for x in h][-1] == (3, "DUAL_TO_SINGLE", "SINGLE")
    assert h[2].requested_by == b.id and h[2].authorized_by == a.id
    assert await _status(db, pending_dual) == "CANCELLED"
    assert (await approval_mode.verify_integrity(db)).ok
    assert await get_setting(db, "refund.window_days") == 7  # nenhum pendente cancelado foi executado


async def test_dual_to_single_requires_step_up_and_the_other_master(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    r = await cb.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "refund.window_days", "value": 9},
                                 "reason": "janela de reembolso"}, headers=sb.headers)
    assert r.status_code == 201
    pending_b = r.json()["id"]
    body = {"action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "SINGLE"}, "reason": REASON}
    r = await ca.post(API, json=body, headers=sa.headers)  # sem step-up
    assert r.status_code == 403 and _err(r) == "FINANCIAL_STEP_UP_INVALID"
    r = await ca.post(API, json={**body, "step_up": {"totp_code": "000000", "financial_password": a.fin_password}},
                      headers=sa.headers)
    assert r.status_code == 403 and _err(r) == "FINANCIAL_STEP_UP_INVALID"
    code_a = totp_code(a)  # recusas desfazem a transação: o passo TOTP não é consumido
    r = await ca.post(API, json={**body, "step_up": {"totp_code": code_a, "financial_password": "errada-123"}},
                      headers=sa.headers)
    assert r.status_code == 403 and _err(r) == "FINANCIAL_STEP_UP_INVALID"
    assert (await db.execute(select(func.count()).select_from(FinancialApprovalRequest).where(
        FinancialApprovalRequest.action_type == "APPROVAL_MODE_CHANGE"))).scalar_one() == 0
    r = await ca.post(API, json={**body, "step_up": {"totp_code": code_a, "financial_password": a.fin_password}},
                      headers=sa.headers)
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["status"] == "PENDING" and m["approval_mode"] == "DUAL" and m["is_mode_change"] is True
    assert m["payload"] == {"target_mode": "SINGLE", "from_mode": "DUAL", "expected_version": 1}
    aud = (await db.execute(select(AuditLog).where(AuditLog.action == "finance.approval_requested",
                                                   AuditLog.entity_id == m["id"]))).scalar_one()
    assert aud.after_data["factors"] == ["totp", "financial_password"] and aud.after_data["target_mode"] == "SINGLE"
    # B é avisado do pedido pendente
    assert (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == b.id, Notification.event_type == "finance.approval_requested"))).scalar_one() == 1
    # unilateral: A não aprova nem "autoriza" o próprio pedido de mudança
    r = await ca.post(f"{API}/{m['id']}/approve", json={"totp_code": DUMMY, "financial_password": a.fin_password,
                                                       "payload_sha256": m["payload_sha256"], "email_code": DUMMY},
                      headers=sa.headers)
    assert r.status_code == 403 and _err(r) == "SELF_APPROVAL_FORBIDDEN"
    r = await ca.post(f"{API}/{m['id']}/authorize", json={"totp_code": DUMMY, "financial_password": a.fin_password,
                                                         "payload_sha256": m["payload_sha256"], "email_code": DUMMY},
                      headers=sa.headers)
    assert r.status_code == 409 and _err(r) == "APPROVAL_MODE_DUAL"
    assert (await approval_mode.current_state(db)).mode == "DUAL"
    # B aprova com TOTP + senha financeira + código por e-mail
    ecode = await http_approval_code(cb, sb, m["id"], mail, b)
    r = await cb.post(f"{API}/{m['id']}/approve", json={"totp_code": totp_code(b), "financial_password": b.fin_password,
                                                       "payload_sha256": m["payload_sha256"], "email_code": ecode},
                      headers=sb.headers)
    assert r.status_code == 200, r.text
    assert r.json()["execution_state"] == "EXECUTED" and r.json()["execution_ref"] == "mode_version:2"
    h = await _history(db)
    assert len(h) == 2 and h[1].kind == "DUAL_TO_SINGLE" and h[1].requested_by == a.id and h[1].authorized_by == b.id
    assert h[1].approval_request_id == uuid.UUID(m["id"])
    # pendente DUAL de B cancelado e B avisado
    assert await _status(db, pending_b) == "CANCELLED"
    assert (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == b.id, Notification.event_type == "finance.approval_decided"))).scalar_one() == 1
    # agora SINGLE: novo pedido nasce SINGLE; o outro Master não decide por ninguém
    r = await ca.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "refund.window_days", "value": 11},
                                 "reason": "janela de reembolso"}, headers=sa.headers)
    assert r.status_code == 201 and r.json()["approval_mode"] == "SINGLE" and r.json()["can_authorize"] is True
    nr = r.json()
    r = await cb.post(f"{API}/{nr['id']}/approve", json={"totp_code": DUMMY, "financial_password": b.fin_password,
                                                        "payload_sha256": nr["payload_sha256"], "email_code": DUMMY},
                      headers=sb.headers)
    assert r.status_code == 409 and _err(r) == "APPROVAL_MODE_SINGLE"
    assert (await approval_mode.verify_integrity(db)).ok


async def test_dual_to_single_impossible_with_second_master_pending(db, client, fin_env, mail):
    a = await make_single_master(db, mode="DUAL")  # DUAL, 2º Master não ativado
    s = await login_as(client, a)
    r = await client.post(API, json={"action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "SINGLE"},
                                     "reason": REASON,
                                     "step_up": {"totp_code": totp_code(a), "financial_password": a.fin_password}},
                          headers=s.headers)
    assert r.status_code == 409 and _err(r) == "SECOND_MASTER_REQUIRED"
    assert (await db.execute(select(func.count()).select_from(FinancialApprovalRequest))).scalar_one() == 0
    state = await approval_mode.current_state(db)
    assert (state.mode, state.version) == ("DUAL", 1)
    assert (await client.get("/api/v1/me", headers=s.headers)).status_code == 200
