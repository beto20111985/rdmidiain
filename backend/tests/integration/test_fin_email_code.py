"""Código por e-mail obrigatório (REQ-DUP-005 / REQ-SAQ-006; ETAPA2 #687–#689; matriz C-32):
TODA aprovação financeira (não só PIX OUT) e TODO saque do estabelecimento exigem, além de
TOTP + senha financeira, um código de 6 dígitos enviado ao e-mail do próprio usuário — uso único,
10 min, vinculado a (usuário, ação, recurso), tentativas limitadas, auditado."""
import uuid

from sqlalchemy import func, select

from app.core.redis import get_redis
from app.models.audit import AuditLog
from app.modules.financial import ledger, security
from tests.factories import login_as, make_user, totp_code
from tests.fin_helpers import (  # noqa: F401
    est_with_owner,
    fin_env,
    http_approval_code,
    http_withdrawal_code,
    make_admin_masters,
)

F = "/api/v1/financial"


async def _req(c, s, value=9):
    r = await c.post(f"{F}/approvals", json={"action_type": "SETTING_CHANGE",
                                             "payload": {"key": "refund.window_days", "value": value},
                                             "reason": "ajuste da janela de reembolso"}, headers=s.headers)
    assert r.status_code == 201, r.text
    return r.json()


def _body(user, req, email_code=None):
    b = {"totp_code": totp_code(user), "financial_password": user.fin_password, "payload_sha256": req["payload_sha256"]}
    if email_code is not None:
        b["email_code"] = email_code
    return b


async def test_every_approval_requires_email_code_single_use_bound_to_request(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    req1, req2 = await _req(ca, sa, 9), await _req(ca, sa, 10)
    assert req1["requires_email_code"] is True
    url1 = f"{F}/approvals/{req1['id']}/approve"
    # sem código ⇒ 403 com código de erro claro (configuração financeira, não PIX OUT)
    r = await cb.post(url1, json=_body(b, req1), headers=sb.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FINANCIAL_EMAIL_CODE_REQUIRED"
    # código de OUTRA aprovação ⇒ inválido
    code2 = await http_approval_code(cb, sb, req2["id"], mail, b)
    r = await cb.post(url1, json=_body(b, req1, code2), headers=sb.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FINANCIAL_EMAIL_CODE_INVALID"
    code1 = await http_approval_code(cb, sb, req1["id"], mail, b)
    wrong = "000000" if code1 != "000000" else "111111"
    r = await cb.post(url1, json=_body(b, req1, wrong), headers=sb.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FINANCIAL_EMAIL_CODE_INVALID"
    # o solicitante não recebe código para o próprio pedido
    r = await ca.post(f"{F}/approvals/{req1['id']}/email-code", headers=sa.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SELF_APPROVAL_FORBIDDEN"
    # caminho feliz (código certo, ainda dentro do limite de tentativas)
    r = await cb.post(url1, json=_body(b, req1, code1), headers=sb.headers)
    assert r.status_code == 200, r.text
    # reutilização: o código de req2 já foi gasto? não — mas o de req1 sim: reenviar em req2 o código de req1 falha
    r = await cb.post(f"{F}/approvals/{req2['id']}/approve", json=_body(b, req2, code1), headers=sb.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FINANCIAL_EMAIL_CODE_INVALID"
    assert (await db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "finance.email_code_sent"))).scalar_one() == 2
    assert "Seu código para confirmar: aprovação de alteração de configuração financeira" in mail.last_to(b.email).text


async def test_expired_reused_and_attempt_limited_codes(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    req = await _req(ca, sa)
    url = f"{F}/approvals/{req['id']}/approve"
    code = await http_approval_code(cb, sb, req["id"], mail, b)
    r = await get_redis()
    await r.delete(security._ecode_key(b.id, "approval", req["id"]))  # expirou (TTL de 10 min no Redis)
    resp = await cb.post(url, json=_body(b, req, code), headers=sb.headers)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FINANCIAL_EMAIL_CODE_INVALID"
    # limite de tentativas: após 5 erros o código correto também é recusado (código invalidado)
    import pytest

    from app.core.errors import DomainError

    code = await http_approval_code(cb, sb, req["id"], mail, b)
    wrong = "999999" if code != "999999" else "888888"
    for _ in range(security.EMAIL_CODE_MAX_ATTEMPTS):
        with pytest.raises(DomainError) as ei:
            await security.verify_action_code(_Ctx(b.id), purpose="approval", resource=req["id"], code=wrong)
        assert ei.value.code == "FINANCIAL_EMAIL_CODE_INVALID"
    with pytest.raises(DomainError) as ei:
        await security.verify_action_code(_Ctx(b.id), purpose="approval", resource=req["id"], code=code)
    assert ei.value.code == "FINANCIAL_EMAIL_CODE_INVALID"
    assert not await r.exists(security._ecode_key(b.id, "approval", req["id"]))


class _Ctx:
    """Contexto mínimo para exercitar o verificador (falhas só alimentam o contador do Redis)."""

    impersonation = None

    def __init__(self, uid):
        self.user_id = uid
        self.kind = "user"
        self.real_user_id = None

    def __getattr__(self, item):
        return None


async def test_establishment_withdrawal_requires_email_code_bound_to_amount(db, client_factory, fin_env, mail):
    from sqlalchemy import text

    org, est, owner = await est_with_owner(db)
    admin = await make_user(db, role="admin", mfa=True)
    ca, co = client_factory(), client_factory()
    sa, so = await login_as(ca, admin), await login_as(co, owner)
    r = await ca.post(f"{F}/establishments/{est.id}/pix-keys", json={
        "key_type": "email", "key_value": "dono@estab.com", "holder_name": "Dono Ltda", "holder_document": "12345678909",
        "reason": "cadastro inicial"}, headers=sa.headers)
    assert r.status_code == 201
    await db.execute(text("UPDATE establishment_pix_keys SET active_from = now() - interval '1 second'"))
    await db.commit()
    exp = await ledger.account(db, "RD:Expense:Commission")
    av = await ledger.account(db, "EST:CommissionPayable:Available", owner_id=est.id, org_id=est.org_id)
    await ledger.post(db, kind="TEST_CREDIT", idempotency_key=f"c:{uuid.uuid4()}", legs=[(exp, 5000), (av, -5000)],
                      description="crédito de teste")
    await db.commit()

    def body(amount, code=None):
        b = {"establishment_id": str(est.id), "amount_cents": amount, "totp_code": totp_code(owner),
             "financial_password": owner.fin_password}
        if code is not None:
            b["email_code"] = code
        return b

    r = await co.post(f"{F}/withdrawals", json=body(1000), headers={**so.headers, "Idempotency-Key": "w-1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "FINANCIAL_EMAIL_CODE_REQUIRED"
    code_other_amount = await http_withdrawal_code(co, so, est.id, 2000, mail, owner)
    r = await co.post(f"{F}/withdrawals", json=body(1000, code_other_amount), headers={**so.headers, "Idempotency-Key": "w-2"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "FINANCIAL_EMAIL_CODE_INVALID"
    code = await http_withdrawal_code(co, so, est.id, 1000, mail, owner)
    assert "saque de R$ 10,00" in mail.last_to(owner.email).text
    r = await co.post(f"{F}/withdrawals", json=body(1000, code), headers={**so.headers, "Idempotency-Key": "w-3"})
    assert r.status_code == 201, r.text
    r = await co.post(f"{F}/withdrawals", json=body(1000, code), headers={**so.headers, "Idempotency-Key": "w-4"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "FINANCIAL_EMAIL_CODE_INVALID"  # reutilizado
    # outro usuário não pede código para estabelecimento alheio
    _, est2, owner2 = await est_with_owner(db)
    c2 = client_factory()
    s2 = await login_as(c2, owner2)
    r = await c2.post(f"{F}/withdrawals/email-code", json={"establishment_id": str(est.id), "amount_cents": 1000},
                      headers=s2.headers)
    assert r.status_code == 404
