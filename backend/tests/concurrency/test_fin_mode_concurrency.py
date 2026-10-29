"""Concorrência entre autorizações financeiras e troca do modo de aprovação (AÇÃO 003; ADR_011 D2
"serialização"; SPEC 003 §4.2, §15-C) — PostgreSQL real, conexões independentes.

Toda criação/aprovação/autorização toma o lock consultivo do modo (``7240030001``) COMPARTILHADO; a
mudança de modo o toma EXCLUSIVO. Logo: nenhuma autorização atravessa uma troca de modo (a que espera
enxerga o estado novo), duas transições simultâneas ⇒ uma vence, e autorizações comuns não se bloqueiam.
"""
import asyncio
import uuid

from sqlalchemy import func, select

from app.core.database import session_factory
from app.core.settings_service import get_setting
from app.models.financial import FinancialApprovalRequest
from app.models.security import InstanceApprovalModeHistory
from app.modules.financial import approvals
from app.modules.security import approval_mode
from tests.factories import login_as, totp_code
from tests.fin_helpers import ctx_for, fin_env, http_approval_code, last_code, make_admin_masters  # noqa: F401

API = "/api/v1/financial/approvals"
REASON = "Mudança do modo de aprovação decidida pelos sócios em ata"


async def _versions(db) -> int:
    return (await db.execute(select(func.count()).select_from(InstanceApprovalModeHistory))).scalar_one()


async def _status(db, rid) -> str:
    db.expire_all()
    return (await db.get(FinancialApprovalRequest, rid)).status


def _body(user, req: dict, email_code: str) -> dict:
    return {"totp_code": totp_code(user), "financial_password": user.fin_password,
            "payload_sha256": req["payload_sha256"], "email_code": email_code}


async def _held_authorization(db_engine, user, req_id, mail):
    """Executa a autorização ÚNICA numa sessão própria e devolve a sessão SEM commit (lock consultivo
    ainda preso) — simula a transação em andamento do outro lado."""
    s = session_factory(db_engine)()
    await approvals.request_email_code(s, await ctx_for(s, user), req_id)
    await s.commit()
    await approvals.authorize(s, await ctx_for(s, user), req_id, totp_code=totp_code(user),
                              financial_password=user.fin_password,
                              payload_sha256=(await s.get(FinancialApprovalRequest, req_id)).payload_sha256,
                              email_code=last_code(mail, user))
    return s


async def test_authorization_waits_for_mode_transition_and_sees_new_state(db, db_engine, client_factory, fin_env, mail):
    """Transição em andamento (lock EXCLUSIVO) ⇒ a autorização SINGLE concorrente espera e, depois do
    commit, encontra o pedido cancelado: nenhuma aprovação SINGLE executa depois do DUAL efetivo."""
    a, b = await make_admin_masters(db, mode="SINGLE")
    ca = client_factory()
    sa = await login_as(ca, a)
    x = (await ca.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "refund.window_days", "value": 9},
                                  "reason": "janela de reembolso"}, headers=sa.headers)).json()
    ecode = await http_approval_code(ca, sa, x["id"], mail, a)
    m = await approvals.create_request(db, await ctx_for(db, b), "APPROVAL_MODE_CHANGE", {"target_mode": "DUAL"}, REASON)
    await db.commit()
    held = await _held_authorization(db_engine, b, m.id, mail)  # SINGLE→DUAL aplicado, sem commit
    try:
        task = asyncio.create_task(ca.post(f"{API}/{x['id']}/authorize", json=_body(a, x, ecode), headers=sa.headers))
        await asyncio.sleep(1.5)
        assert not task.done(), "a autorização deveria esperar o lock exclusivo da troca de modo"
        await held.commit()
    finally:
        await held.close()
    r = await asyncio.wait_for(task, timeout=30)
    assert r.status_code == 409 and r.json()["error"]["code"] == "APPROVAL_NOT_PENDING", r.text
    assert await _status(db, m.id) == "APPROVED"
    assert await _status(db, uuid.UUID(x["id"])) == "CANCELLED"
    assert (await approval_mode.current_state(db)).mode == "DUAL" and await _versions(db) == 2
    db.expire_all()
    assert await get_setting(db, "refund.window_days") == 7


async def test_mode_transition_waits_for_inflight_authorization(db, db_engine, client_factory, fin_env, mail):
    """Autorização SINGLE em andamento (lock COMPARTILHADO) ⇒ a troca de modo espera; o pedido autorizado
    antes permanece APROVADO/EXECUTADO (não é cancelado) e a troca é aplicada depois."""
    a, b = await make_admin_masters(db, mode="SINGLE")
    cb = client_factory()
    sb = await login_as(cb, b)
    x = await approvals.create_request(db, await ctx_for(db, a), "SETTING_CHANGE",
                                       {"key": "refund.window_days", "value": 9}, "janela de reembolso")
    await db.commit()
    m = (await cb.post(API, json={"action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "DUAL"},
                                  "reason": REASON}, headers=sb.headers)).json()
    ecode = await http_approval_code(cb, sb, m["id"], mail, b)
    held = await _held_authorization(db_engine, a, x.id, mail)  # SETTING_CHANGE executado, sem commit
    try:
        task = asyncio.create_task(cb.post(f"{API}/{m['id']}/authorize", json=_body(b, m, ecode), headers=sb.headers))
        await asyncio.sleep(1.5)
        assert not task.done(), "a troca de modo deveria esperar a autorização em andamento"
        await held.commit()
    finally:
        await held.close()
    r = await asyncio.wait_for(task, timeout=30)
    assert r.status_code == 200, r.text
    assert await _status(db, x.id) == "APPROVED"
    db.expire_all()
    assert await get_setting(db, "refund.window_days") == 9
    assert (await approval_mode.current_state(db)).mode == "DUAL" and await _versions(db) == 2


async def test_two_simultaneous_transitions_only_one_wins(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db, mode="SINGLE")
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    body = {"action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "DUAL"}, "reason": REASON}
    m1 = (await ca.post(API, json=body, headers=sa.headers)).json()
    m2 = (await cb.post(API, json=body, headers=sb.headers)).json()
    e1 = await http_approval_code(ca, sa, m1["id"], mail, a)
    e2 = await http_approval_code(cb, sb, m2["id"], mail, b)
    r1, r2 = await asyncio.gather(ca.post(f"{API}/{m1['id']}/authorize", json=_body(a, m1, e1), headers=sa.headers),
                                  cb.post(f"{API}/{m2['id']}/authorize", json=_body(b, m2, e2), headers=sb.headers))
    assert sorted([r1.status_code, r2.status_code]) == [200, 409], (r1.text, r2.text)
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["error"]["code"] in ("APPROVAL_NOT_PENDING", "APPROVAL_MODE_CHANGED")
    assert await _versions(db) == 2
    assert (await approval_mode.current_state(db)).mode == "DUAL"
    assert (await approval_mode.verify_integrity(db)).ok


async def test_ordinary_authorizations_do_not_block_each_other(db, client_factory, fin_env, mail):
    """Lock COMPARTILHADO: duas autorizações comuns simultâneas (Masters diferentes) seguem em paralelo."""
    a, b = await make_admin_masters(db, mode="SINGLE")
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    x1 = (await ca.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "refund.window_days", "value": 9},
                                   "reason": "janela de reembolso"}, headers=sa.headers)).json()
    x2 = (await cb.post(API, json={"action_type": "SETTING_CHANGE",
                                   "payload": {"key": "commission.auto_payout_min_cents", "value": 500},
                                   "reason": "mínimo de pagamento automático"}, headers=sb.headers)).json()
    e1 = await http_approval_code(ca, sa, x1["id"], mail, a)
    e2 = await http_approval_code(cb, sb, x2["id"], mail, b)
    r1, r2 = await asyncio.gather(ca.post(f"{API}/{x1['id']}/authorize", json=_body(a, x1, e1), headers=sa.headers),
                                  cb.post(f"{API}/{x2['id']}/authorize", json=_body(b, x2, e2), headers=sb.headers))
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)
    db.expire_all()
    assert await get_setting(db, "refund.window_days") == 9
    assert await get_setting(db, "commission.auto_payout_min_cents") == 500
