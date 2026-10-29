"""TOTP: setup/enable/verify, anti-replay, janela, recuperação, obrigatório p/ admin/financeiro, lockout."""
import time

import time_machine
from sqlalchemy import select

from app.core import totp
from app.models.users import User
from tests.factories import auth_headers, get_user, login_as, make_user, setup_mfa_via_api, totp_code

LOGIN = "/api/v1/auth/login"
VERIFY = "/api/v1/auth/mfa/verify"


async def _mfa_token(client, u):
    r = await client.post(LOGIN, json={"login": u.email, "password": u.password})
    assert r.status_code == 200 and r.json()["status"] == "mfa_required", r.text
    assert r.json()["expires_in"] == 300 and "access_token" not in r.json()
    return r.json()["mfa_token"]


async def test_setup_enable_and_login_with_totp(client, db):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    r = await client.post("/api/v1/auth/mfa/totp/setup", json={"password": "errada-123x"}, headers=s.headers)
    assert r.status_code == 400
    r = await client.post("/api/v1/auth/mfa/totp/setup", json={"password": u.password}, headers=s.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/") and "secret" in body
    assert "qrserver" not in r.text and "http" not in body["otpauth_uri"].split("?")[0]
    fresh = await get_user(db, u.id)
    assert fresh.totp_enabled is False and fresh.totp_pending_secret_enc and body["secret"] not in fresh.totp_pending_secret_enc
    u.totp_secret = body["secret"]
    r = await client.post("/api/v1/auth/mfa/totp/enable", json={"code": "000000"}, headers=s.headers)
    assert r.status_code == 400
    r = await client.post("/api/v1/auth/mfa/totp/enable", json={"code": totp_code(u)}, headers=s.headers)
    assert r.status_code == 200
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    fresh = await get_user(db, u.id)
    assert fresh.totp_enabled and fresh.totp_secret_enc and fresh.totp_pending_secret_enc is None
    # setup com 2FA ativo não troca o segredo (contrato: setup não altera 2FA ativo)
    r = await client.post("/api/v1/auth/mfa/totp/setup", json={"password": u.password}, headers=s.headers)
    assert r.status_code == 409
    assert (await get_user(db, u.id)).totp_secret_enc == fresh.totp_secret_enc
    # login agora exige 2º fator
    tok = await _mfa_token(client, u)
    r = await client.post(VERIFY, json={"mfa_token": tok, "code": totp_code(u)})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert (await client.get("/api/v1/me", headers=auth_headers(r.json()["access_token"]))).json()["mfa"]["configured"]


async def test_totp_replay_rejected(client, db):
    u = await make_user(db, role="employee", mfa=True)
    code = totp_code(u)
    tok1 = await _mfa_token(client, u)
    assert (await client.post(VERIFY, json={"mfa_token": tok1, "code": code})).status_code == 200
    tok2 = await _mfa_token(client, u)
    r = await client.post(VERIFY, json={"mfa_token": tok2, "code": code})  # mesmo código, mesmo passo
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_MFA_CODE"


async def test_previous_window_code_accepted_but_not_older_than_last_used(client, db):
    """Política de janela ±1 + anti-replay, com relógio CONGELADO no meio de um passo.

    Antes dependia do segundo atual: sob carga, a virada do passo de 30 s entre gerar o
    código e o servidor verificar tirava o "passo anterior" da janela (flaky).
    """
    u = await make_user(db, role="employee", mfa=True)
    n = totp.current_step() + 20  # passo N, no futuro, fixo
    with time_machine.travel(n * totp.PERIOD + totp.PERIOD / 2, tick=False):
        assert totp.current_step() == n

        async def verify(step: int) -> int:
            tok = await _mfa_token(client, u)
            r = await client.post(VERIFY, json={"mfa_token": tok, "code": totp.code_at_step(u.totp_secret, step)})
            return r.status_code

        assert await verify(n - 3) == 401  # fora da janela
        assert await verify(n - 1) == 200  # janela ANTERIOR aceita (último aceito = N-1)
        assert await verify(n - 1) == 401  # replay do mesmo passo
        assert await verify(n) == 200      # passo atual (N > N-1)
        assert await verify(n - 1) == 401  # passo anterior ao último aceito → recusado
        assert await verify(n + 1) == 200  # janela SEGUINTE aceita (tolerância de relógio)
        assert await verify(n) == 401      # ≤ último aceito (N+1)
        assert await verify(n + 2) == 401  # fora da janela


async def test_mfa_token_single_use(client, db):
    u = await make_user(db, role="employee", mfa=True)
    tok = await _mfa_token(client, u)
    assert (await client.post(VERIFY, json={"mfa_token": tok, "code": totp_code(u)})).status_code == 200
    r = await client.post(VERIFY, json={"mfa_token": tok, "code": totp_code(u)})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_MFA_TOKEN"


async def test_totp_failures_count_for_lockout(client, db):
    u = await make_user(db, role="employee", mfa=True)
    statuses = []
    for i in range(5):
        tok = await _mfa_token(client, u)
        r = await client.post(VERIFY, json={"mfa_token": tok, "code": "000000" if i % 2 else "111111"})
        statuses.append(r.status_code)
    assert statuses == [401, 401, 401, 401, 423]
    fresh = await get_user(db, u.id)
    assert fresh.locked_until is not None
    r = await client.post(LOGIN, json={"login": u.email, "password": u.password})
    assert r.status_code == 423


async def test_recovery_code_single_use(client, db):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    codes = (await setup_mfa_via_api(client, u, s.access_token))["recovery_codes"]
    tok = await _mfa_token(client, u)
    r = await client.post(VERIFY, json={"mfa_token": tok, "code": codes[0].lower()})
    assert r.status_code == 200
    tok = await _mfa_token(client, u)
    assert (await client.post(VERIFY, json={"mfa_token": tok, "code": codes[0]})).status_code == 401


async def test_mfa_mandatory_for_admin_financial_super_admin(client, db):
    for role in ("admin", "financial", "super_admin"):
        u = await make_user(db, role=role)
        r = await client.post(LOGIN, json={"login": u.email, "password": u.password})
        body = r.json()
        assert r.status_code == 200 and body["status"] == "mfa_setup_required", (role, body)
        assert "access_token" not in body and "set-cookie" not in r.headers
        setup_tok = body["mfa_setup_token"]
        # o token de setup NÃO abre outras rotas
        assert (await client.get("/api/v1/me", headers=auth_headers(setup_tok))).status_code == 401
        assert (await client.get("/api/v1/users", headers=auth_headers(setup_tok))).status_code == 401
        out = await setup_mfa_via_api(client, u, setup_tok)
        assert out["status"] == "ok" and len(out["recovery_codes"]) == 10
        me = await client.get("/api/v1/me", headers=auth_headers(out["access_token"]))
        assert me.status_code == 200 and me.json()["mfa"] == {"required": True, "configured": True}
        assert (await client.get("/api/v1/auth/sessions", headers=auth_headers(out["access_token"]))).status_code == 200


async def test_role_requiring_mfa_granted_later_blocks_existing_session(client, db):
    from tests.factories import assign

    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    await assign(db, u, "financial")
    r = await client.get("/api/v1/organizations", headers=s.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "MFA_SETUP_REQUIRED"
    assert (await client.get("/api/v1/me", headers=s.headers)).json()["mfa"] == {"required": True, "configured": False}


async def test_disable_requires_password_and_code_and_forbidden_for_mandatory(client, db):
    u = await make_user(db, role="employee", mfa=True)
    s = await login_as(client, u)
    r = await client.post("/api/v1/auth/mfa/totp/disable", json={"password": u.password, "code": "000000"},
                          headers=s.headers)
    assert r.status_code == 400
    r = await client.post("/api/v1/auth/mfa/totp/disable", json={"password": "errada-123x", "code": totp_code(u)},
                          headers=s.headers)
    assert r.status_code == 400
    r = await client.post("/api/v1/auth/mfa/totp/disable", json={"password": u.password, "code": totp_code(u)},
                          headers=s.headers)
    assert r.status_code == 204
    assert (await get_user(db, u.id)).totp_enabled is False
    adm = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, adm)
    r = await client.post("/api/v1/auth/mfa/totp/disable", json={"password": adm.password, "code": totp_code(adm)},
                          headers=sa.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "MFA_REQUIRED_FOR_ROLE"


async def test_totp_secret_encrypted_at_rest(db, client):
    u = await make_user(db, role="employee", mfa=True)
    row = (await db.execute(select(User.totp_secret_enc).where(User.id == u.id))).scalar_one()
    assert row.startswith("v1:") and u.totp_secret not in row


async def test_mfa_token_expires(client, db):
    u = await make_user(db, role="employee", mfa=True)
    tok = await _mfa_token(client, u)
    with time_machine.travel(time.time() + 301, tick=False):
        r = await client.post(VERIFY, json={"mfa_token": tok, "code": totp_code(u)})
    assert r.status_code == 401
