"""Recuperação da senha financeira esquecida (só o próprio usuário): código de 6 dígitos por
e-mail (15 min, uso único, hash, rate limit) + TOTP (anti-replay); auditoria, notificação e
bloqueio de saques/aprovações por ``finance.password_reset_cooldown_hours``. Um Admin Master
nunca redefine a senha financeira do outro. Nomes (somente) nas respostas de aprovações."""
import re
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from app.core.passwords import verify_password
from app.core.timeutils import utcnow
from app.models.audit import AuditLog
from app.models.financial import FinancialPasswordResetCode
from app.models.notifications import Notification
from app.models.users import User
from tests.factories import login_as, make_org, make_user, totp_code
from tests.fin_helpers import fin_env, make_admin_masters, set_fin_password  # noqa: F401

F = "/api/v1/financial"
NEW_PW = "Nova-Fin-2026x"


def _code(mail, user) -> str:
    return re.search(r"\b(\d{6})\b", mail.last_to(user.email).text).group(1)


async def test_forgot_and_reset_flow_single_use_audit_notification_and_cooldown(db, client, fin_env, mail):
    org = await make_org(db)
    u = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org.id), mfa=True)
    old_pw = await set_fin_password(db, u)
    s = await login_as(client, u)
    r = await client.post(f"{F}/password/forgot", headers=s.headers)
    assert r.status_code == 202 and r.json()["expires_in"] == 900
    code = _code(mail, u)
    row = (await db.execute(select(FinancialPasswordResetCode))).scalar_one()
    assert code not in row.code_hash and len(row.code_hash) == 64  # só o hash é guardado
    wrong = "000000" if code != "000000" else "111111"
    r = await client.post(f"{F}/password/reset", json={"code": wrong, "totp_code": totp_code(u), "new_password": NEW_PW},
                          headers=s.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FINANCIAL_PASSWORD_RESET_INVALID"
    db.expunge_all()
    assert (await db.get(FinancialPasswordResetCode, row.id)).attempts == 1  # tentativa persistida
    r = await client.post(f"{F}/password/reset", json={"code": code, "totp_code": "000000", "new_password": NEW_PW},
                          headers=s.headers)
    assert r.status_code == 403  # TOTP inválido
    r = await client.post(f"{F}/password/reset", json={"code": code, "totp_code": totp_code(u), "new_password": NEW_PW},
                          headers=s.headers)
    assert r.status_code == 200, r.text
    assert r.json()["blocked_until"]
    db.expunge_all()
    usr = await db.get(User, u.id)
    assert verify_password(NEW_PW, usr.financial_password_hash)[0] and not verify_password(old_pw, usr.financial_password_hash)[0]
    # uso único
    r = await client.post(f"{F}/password/reset", json={"code": code, "totp_code": totp_code(u), "new_password": "Outra-Fin-99a"},
                          headers=s.headers)
    assert r.status_code == 403
    assert (await db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "finance.financial_password_reset"))).scalar_one() == 1
    n = (await db.execute(select(Notification).where(Notification.user_id == u.id,
                                                     Notification.event_type == "finance.password_reset"))).scalars().all()
    assert len(n) == 1
    st = (await client.get(f"{F}/security/status", headers=s.headers)).json()
    assert st["reset_blocked_until"] is not None
    # bloqueio de saques por 24 h após a redefinição (antes de qualquer outra checagem do saque)
    from tests.factories import make_establishment

    est = await make_establishment(db, org)
    body = {"establishment_id": str(est.id), "amount_cents": 100, "totp_code": totp_code(u), "financial_password": NEW_PW}
    r = await client.post(f"{F}/withdrawals", json=body, headers={**s.headers, "Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 423 and r.json()["error"]["code"] == "FINANCIAL_PASSWORD_RECENTLY_RESET"
    # passado o período, o bloqueio cai (o saque segue para as demais regras: sem chave PIX ⇒ 409)
    await db.execute(update(FinancialPasswordResetCode).values(used_at=utcnow() - timedelta(hours=25)))
    await db.commit()
    from tests.fin_helpers import http_withdrawal_code

    body["totp_code"] = totp_code(u)
    body["email_code"] = await http_withdrawal_code(client, s, est.id, 100, mail, u)
    r = await client.post(f"{F}/withdrawals", json=body, headers={**s.headers, "Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PIX_KEY_REQUIRED"


async def test_expired_code_and_rate_limit(db, client, fin_env, mail):
    u = await make_user(db, role="financial", mfa=True)
    s = await login_as(client, u)
    assert (await client.post(f"{F}/password/forgot", headers=s.headers)).status_code == 202
    code = _code(mail, u)
    await db.execute(update(FinancialPasswordResetCode).values(expires_at=utcnow() - timedelta(seconds=1)))
    await db.commit()
    r = await client.post(f"{F}/password/reset", json={"code": code, "totp_code": totp_code(u), "new_password": NEW_PW},
                          headers=s.headers)
    assert r.status_code == 403
    assert (await client.post(f"{F}/password/forgot", headers=s.headers)).status_code == 202
    assert (await client.post(f"{F}/password/forgot", headers=s.headers)).status_code == 202
    r = await client.post(f"{F}/password/forgot", headers=s.headers)
    assert r.status_code == 429 and r.json()["error"]["code"] == "FINANCIAL_PASSWORD_RESET_RATE_LIMITED"
    # códigos anteriores invalidados a cada novo pedido: só 1 válido
    assert (await db.execute(select(func.count()).select_from(FinancialPasswordResetCode).where(
        FinancialPasswordResetCode.invalidated_at.is_(None)))).scalar_one() == 1


async def test_admin_master_cannot_reset_the_other_and_reset_blocks_approvals(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    assert (await ca.post(f"{F}/password/forgot", headers=sa.headers)).status_code == 202
    code_a = _code(mail, a)
    # B não consegue usar o código de A (vinculado ao próprio usuário) nem apontar outro usuário
    r = await cb.post(f"{F}/password/reset", json={"code": code_a, "totp_code": totp_code(b), "new_password": NEW_PW},
                      headers=sb.headers)
    assert r.status_code == 403
    r = await cb.post(f"{F}/password/reset", json={"code": code_a, "totp_code": totp_code(b), "new_password": NEW_PW,
                                                   "user_id": str(a.id)}, headers=sb.headers)
    assert r.status_code == 422
    db.expunge_all()
    assert verify_password(a.fin_password, (await db.get(User, a.id)).financial_password_hash)[0]
    # A redefine a própria senha; durante o bloqueio, A não aprova pedidos de B
    r = await ca.post(f"{F}/password/reset", json={"code": code_a, "totp_code": totp_code(a), "new_password": NEW_PW},
                      headers=sa.headers)
    assert r.status_code == 200, r.text
    r = await cb.post(f"{F}/approvals", json={"action_type": "SETTING_CHANGE", "payload": {
        "key": "refund.window_days", "value": 8}, "reason": "ajuste de janela de reembolso"}, headers=sb.headers)
    assert r.status_code == 201
    req = r.json()
    assert req["requested_by_name"] == "Sócio B" and req["approved_by_name"] is None
    assert "email" not in str({k: v for k, v in req.items() if k.endswith("_name")})
    r = await ca.post(f"{F}/approvals/{req['id']}/approve", json={
        "totp_code": totp_code(a), "financial_password": NEW_PW, "payload_sha256": req["payload_sha256"]},
        headers=sa.headers)
    assert r.status_code == 423 and r.json()["error"]["code"] == "FINANCIAL_PASSWORD_RECENTLY_RESET"
    await db.execute(update(FinancialPasswordResetCode).values(used_at=utcnow() - timedelta(hours=25)))
    await db.commit()
    from tests.fin_helpers import http_approval_code

    r = await ca.post(f"{F}/approvals/{req['id']}/approve", json={
        "totp_code": totp_code(a), "financial_password": NEW_PW, "payload_sha256": req["payload_sha256"],
        "email_code": await http_approval_code(ca, sa, req["id"], mail, a)}, headers=sa.headers)
    assert r.status_code == 200, r.text
    assert r.json()["approved_by_name"] == "Sócio A" and r.json()["requested_by_name"] == "Sócio B"
    lst = (await cb.get(f"{F}/approvals", headers=sb.headers)).json()["items"]
    assert lst[0]["approved_by_name"] == "Sócio A"
