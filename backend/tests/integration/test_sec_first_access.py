"""AÇÃO 003 — primeiro acesso com guard no backend (ADR_011 D6; SPEC §3).

Ordem imposta pelo servidor: PASSWORD_CHANGE → TOTP → FINANCIAL_PASSWORD (bloqueantes) → SECOND_MASTER
(só DUAL, não bloqueante). Cada rota antes da etapa certa é recusada com o código da SPEC.
"""
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import text

from tests.factories import auth_headers, login_as, make_user, totp_code
from tests.sec_helpers import API, bootstrap_master, capture_events, code_for, strong_password


def _err(r) -> str:
    return r.json()["error"]["code"]


@pytest.mark.parametrize("dual", [True, False])
async def test_full_first_access_with_guard(client, db, dual, monkeypatch):
    events = capture_events(monkeypatch)
    m = await bootstrap_master(dual=dual)
    r = await client.post(f"{API}/auth/login", json={"login": m.email, "password": m.password})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "mfa_setup_required"
    assert "access_token" not in body and "set-cookie" not in r.headers
    st = body["setup"]
    assert st["required"] is True and st["next_step"] == "PASSWORD_CHANGE" and st["is_admin_master"] is True
    assert st["steps"] == {"password_change": "PENDING", "totp": "PENDING", "financial_password": "PENDING",
                           "second_master": "NOT_INVITED" if dual else "NOT_REQUIRED"}
    assert st["approval_mode"] == ("DUAL" if dual else "SINGLE")
    setup1 = auth_headers(body["mfa_setup_token"])

    # status com o token de setup; o token não abre outras rotas
    r = await client.get(f"{API}/auth/setup/status", headers=setup1)
    assert r.status_code == 200 and r.json()["next_step"] == "PASSWORD_CHANGE"
    assert (await client.get(f"{API}/me", headers=setup1)).status_code == 401
    assert (await client.get(f"{API}/users", headers=setup1)).status_code == 401
    # TOTP ANTES da troca de senha ⇒ 403 PASSWORD_CHANGE_REQUIRED (ordem no servidor)
    r = await client.post(f"{API}/auth/mfa/totp/setup", json={"password": m.password}, headers=setup1)
    assert r.status_code == 403 and _err(r) == "PASSWORD_CHANGE_REQUIRED"
    assert r.json()["error"]["details"]["next_step"] == "PASSWORD_CHANGE"
    r = await client.post(f"{API}/auth/mfa/totp/enable", json={"code": "123456"}, headers=setup1)
    assert r.status_code == 403 and _err(r) == "PASSWORD_CHANGE_REQUIRED"
    # senha financeira só com sessão (access token)
    r = await client.post(f"{API}/auth/setup/financial-password",
                          json={"login_password": m.password, "totp_code": "123456", "financial_password": "Fin-1234567"},
                          headers=setup1)
    assert r.status_code == 401
    # senha atual errada ⇒ 400 INVALID_PASSWORD (conta no lockout)
    r = await client.post(f"{API}/auth/setup/password",
                          json={"current_password": "Errada-2026x", "new_password": strong_password()}, headers=setup1)
    assert r.status_code == 400 and _err(r) == "INVALID_PASSWORD"
    assert (await db.execute(text("SELECT failed_login_attempts FROM users WHERE email = :e"),
                             {"e": m.email})).scalar() == 1
    # política e reuso
    r = await client.post(f"{API}/auth/setup/password", json={"current_password": m.password, "new_password": "fraca"},
                          headers=setup1)
    assert r.status_code == 400 and _err(r) == "PASSWORD_POLICY"
    r = await client.post(f"{API}/auth/setup/password",
                          json={"current_password": m.password, "new_password": m.password}, headers=setup1)
    assert r.status_code == 400 and _err(r) == "PASSWORD_REUSED"

    # PASSO 1: nova senha ⇒ NOVO token de setup; o antigo deixa de valer
    new_pw = strong_password("Nova")
    r = await client.post(f"{API}/auth/setup/password", json={"current_password": m.password, "new_password": new_pw},
                          headers=setup1)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "mfa_setup_required" and b["setup"]["next_step"] == "TOTP"
    assert b["setup"]["steps"]["password_change"] == "DONE"
    m.password = new_pw
    setup2 = auth_headers(b["mfa_setup_token"])
    assert (await client.get(f"{API}/auth/setup/status", headers=setup1)).status_code == 401
    r = await client.post(f"{API}/auth/setup/password", json={"current_password": new_pw, "new_password": strong_password()},
                          headers=setup2)
    assert r.status_code == 409 and _err(r) == "PASSWORD_CHANGE_NOT_REQUIRED"
    audit_setup = (await db.execute(text(
        "SELECT count(*) FROM audit_logs WHERE action = 'auth.password_changed' AND after_data->>'setup' = 'true'"))
    ).scalar()
    assert audit_setup == 1

    # PASSO 2: TOTP ⇒ sessão
    r = await client.post(f"{API}/auth/mfa/totp/setup", json={"password": m.password}, headers=setup2)
    assert r.status_code == 200
    m.totp_secret = r.json()["secret"]
    r = await client.post(f"{API}/auth/mfa/totp/enable", json={"code": code_for(m)}, headers=setup2)
    assert r.status_code == 200 and r.json()["status"] == "ok" and len(r.json()["recovery_codes"]) == 10
    sess = auth_headers(r.json()["access_token"])

    # guard: tudo fora das rotas liberadas ⇒ 403 FINANCIAL_PASSWORD_SETUP_REQUIRED (inclusive ticket de WS)
    for method, path in (("GET", "/users"), ("GET", "/organizations"), ("POST", "/realtime/ticket"),
                         ("GET", "/security/masters"), ("GET", "/financial/approvals")):
        r = await client.request(method, f"{API}{path}", headers=sess)
        assert r.status_code == 403 and _err(r) == "FINANCIAL_PASSWORD_SETUP_REQUIRED", (path, r.text)
        assert r.json()["error"]["details"]["next_step"] == "FINANCIAL_PASSWORD"
    me = await client.get(f"{API}/me", headers=sess)
    assert me.status_code == 200
    assert me.json()["security_setup"]["next_step"] == "FINANCIAL_PASSWORD"
    assert me.json()["security_setup"]["required"] is True
    assert (await client.get(f"{API}/auth/sessions", headers=sess)).status_code == 200
    r = await client.post(f"{API}/auth/setup/password", json={"current_password": m.password, "new_password": strong_password()},
                          headers=sess)
    assert r.status_code == 409 and _err(r) == "PASSWORD_CHANGE_NOT_REQUIRED"

    # PASSO 3: senha financeira (login + TOTP); erros primeiro
    r = await client.post(f"{API}/auth/setup/financial-password",
                          json={"login_password": m.password, "totp_code": "000000", "financial_password": "Fin-abc12345"},
                          headers=sess)
    assert r.status_code == 403 and _err(r) == "FINANCIAL_STEP_UP_INVALID"
    r = await client.post(f"{API}/auth/setup/financial-password",
                          json={"login_password": m.password, "totp_code": "123456", "financial_password": m.password},
                          headers=sess)  # política checada antes do TOTP (código não consumido)
    assert r.status_code == 400 and _err(r) == "FINANCIAL_PASSWORD_POLICY"
    fin = "Fin-" + secrets.token_hex(6)
    r = await client.post(f"{API}/auth/setup/financial-password",
                          json={"login_password": m.password, "totp_code": code_for(m), "financial_password": fin},
                          headers=sess)
    assert r.status_code == 200, r.text
    st = r.json()["setup"]
    assert st["required"] is False and st["steps"]["financial_password"] == "DONE"
    if dual:
        assert st["next_step"] == "SECOND_MASTER" and st["steps"]["second_master"] == "NOT_INVITED"
        assert st["dual_approval_ready"] is False
    else:
        assert st["next_step"] is None and st["steps"]["second_master"] == "NOT_REQUIRED"
        assert st["dual_approval_ready"] is None
    # painel liberado
    assert (await client.get(f"{API}/users", headers=sess)).status_code == 200
    assert (await client.post(f"{API}/realtime/ticket", headers=sess)).status_code == 200
    assert (await client.get(f"{API}/security/masters", headers=sess)).status_code == 200
    r = await client.post(f"{API}/auth/setup/financial-password",
                          json={"login_password": m.password, "totp_code": "123456", "financial_password": fin + "x"},
                          headers=sess)
    assert r.status_code == 409 and _err(r) == "SETUP_STEP_OUT_OF_ORDER"
    me = (await client.get(f"{API}/me", headers=sess)).json()["security_setup"]
    assert me == st
    # SetupState nunca carrega segredo
    blob = str(me) + str(body)
    for secret in (m.password, fin, m.totp_secret):
        assert secret not in blob
    from app.modules.notifications.service import drain

    await drain()
    assert events.of_type("security.status_changed"), "evento WS security.status_changed não publicado"
    from app.modules.security.approval_mode import verify_integrity

    assert (await verify_integrity(db)).ok


async def test_out_of_order_financial_password_and_password_first(client, db):
    # titular com TOTP já ativo mas troca de senha pendente (ex.: recuperação parcial em teste)
    m = await make_user(db, role="super_admin", mfa=True, admin_master_seat=1, must_change_password=True,
                        financial_password=False)
    s = await login_as(client, m, expect_ok=True)
    r = await client.post(f"{API}/auth/setup/financial-password",
                          json={"login_password": m.password, "totp_code": totp_code(m), "financial_password": "Fin-x1234567"},
                          headers=s.headers)
    assert r.status_code == 409 and _err(r) == "SETUP_STEP_OUT_OF_ORDER"
    assert r.json()["error"]["details"]["next_step"] == "PASSWORD_CHANGE"
    r = await client.get(f"{API}/users", headers=s.headers)
    assert r.status_code == 403 and _err(r) == "PASSWORD_CHANGE_REQUIRED"
    r = await client.post(f"{API}/auth/setup/password",
                          json={"current_password": m.password, "new_password": strong_password()}, headers=s.headers)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["setup"]["next_step"] == "FINANCIAL_PASSWORD"
    r = await client.get(f"{API}/users", headers=s.headers)  # mesma sessão continua válida
    assert r.status_code == 403 and _err(r) == "FINANCIAL_PASSWORD_SETUP_REQUIRED"


async def test_non_master_setup_state_and_me(client, db):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    st = (await client.get(f"{API}/me", headers=s.headers)).json()["security_setup"]
    assert st == {"required": False, "next_step": None,
                  "steps": {"password_change": "DONE", "totp": "NOT_REQUIRED", "financial_password": "NOT_REQUIRED",
                            "second_master": "NOT_REQUIRED"},
                  "is_admin_master": False, "approval_mode": None, "dual_approval_ready": None}
    r = await client.get(f"{API}/auth/setup/status", headers=s.headers)
    assert r.status_code == 200 and r.json() == st
    r = await client.post(f"{API}/auth/setup/financial-password",
                          json={"login_password": u.password, "totp_code": "123456", "financial_password": "Fin-x1234567"},
                          headers=s.headers)
    assert r.status_code == 409 and _err(r) == "SETUP_STEP_OUT_OF_ORDER"
    assert (await client.get(f"{API}/auth/setup/status")).status_code == 401


async def test_guard_order_password_before_mfa_for_existing_session(client, db):
    from tests.factories import assign

    u = await make_user(db, role="employee", must_change_password=True)
    s = await login_as(client, u)
    await assign(db, u, "financial")  # perfil com 2FA obrigatório concedido depois
    r = await client.get(f"{API}/organizations", headers=s.headers)
    assert r.status_code == 403 and _err(r) == "PASSWORD_CHANGE_REQUIRED"  # nova ordem: senha → TOTP
    r = await client.post(f"{API}/auth/password/change",
                          json={"current_password": u.password, "new_password": strong_password()}, headers=s.headers)
    assert r.status_code == 204
    r = await client.get(f"{API}/organizations", headers=s.headers)
    assert r.status_code == 403 and _err(r) == "MFA_SETUP_REQUIRED"
    assert r.json()["error"]["details"]["next_step"] == "TOTP"
