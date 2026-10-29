"""AÇÃO 003 — ataques à independência dos Admin Master e ao primeiro acesso (ADR_011 D6, D7, D9).

- Master A não define/reseta senha, TOTP, senha financeira, sessões nem e-mail/ativação de B; não impersona B;
  não vê o link de ativação.
- ``super_admin`` não é concedido pela API (convite genérico, /users, convite antigo pendente).
- Token de setup: escopo mínimo, invalidado pela troca de senha, inútil fora das rotas de setup.
- 3º Master impossível (API, CLI e função do banco).
"""
from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app import cli
from app.core.crypto import sha256_hex
from app.core.timeutils import utcnow
from tests.factories import CSRF, adopt_master_seats, auth_headers, login_as, make_org, make_user, totp_code
from tests.sec_helpers import API, active_master, bootstrap_master, strong_password


def _err(r) -> str:
    return r.json()["error"]["code"]


async def test_master_a_cannot_touch_master_b(client, db):
    a = await active_master(db, name="A")
    b = await make_user(db, role="super_admin", mfa=True, admin_master_seat=2, name="B")
    await adopt_master_seats(db, b)
    sa = await login_as(client, a)
    sb = await login_as(client, b)
    r = await client.get(f"{API}/users/{b.id}", headers=sa.headers)
    assert r.status_code == 200 and r.json()["restricted"] is True and "email" not in r.json()
    attempts = [
        ("PATCH", f"/users/{b.id}", {"is_active": False}),
        ("PATCH", f"/users/{b.id}", {"email": "novo@empresa.com.br"}),
        ("POST", f"/users/{b.id}/mfa/reset", {"reason": "perdeu o celular mesmo"}),
        ("POST", f"/users/{b.id}/sessions/revoke-all", {"reason": "quero derrubar o B"}),
        ("POST", f"/users/{b.id}/unlock", {"reason": "desbloqueio indevido"}),
        ("POST", f"/users/{b.id}/assignments", {"role": "admin"}),
    ]
    for method, path, body in attempts:
        r = await client.request(method, f"{API}{path}", json=body, headers=sa.headers)
        assert r.status_code in (403, 404), (path, r.status_code, r.text)
    r = await client.post(f"{API}/auth/impersonation", json={"user_id": str(b.id), "reason": "teste de impersonar B"},
                          headers=sa.headers)
    assert r.status_code == 403
    # nenhuma rota de fatores aceita id de outro usuário: as de setup agem só sobre o próprio titular
    r = await client.post(f"{API}/auth/setup/financial-password",
                          json={"login_password": a.password, "totp_code": "123456", "financial_password": "Fin-zz123456",
                                "user_id": str(b.id)}, headers=sa.headers)
    assert r.status_code == 409 and _err(r) == "SETUP_STEP_OUT_OF_ORDER"  # age só sobre o próprio A (nada pendente)
    # B continua intacto e com sessão válida
    row = (await db.execute(text(
        "SELECT is_active, totp_enabled, financial_password_hash IS NOT NULL, email FROM users WHERE id = :i"),
        {"i": b.id})).one()
    assert tuple(row) == (True, True, True, b.email)
    assert (await client.get(f"{API}/me", headers=sb.headers)).status_code == 200


async def test_super_admin_never_granted_by_api(client, db):
    a = await active_master(db)
    sa = await login_as(client, a)
    target = await make_user(db, role="employee")
    r = await client.post(f"{API}/users/{target.id}/assignments", json={"role": "super_admin"}, headers=sa.headers)
    assert r.status_code == 403 and _err(r) == "ROLE_RESERVED_FOR_ADMIN_MASTER"
    r = await client.post(f"{API}/auth/invites", json={"email": "novo.sa@empresa.com.br", "role": "super_admin"},
                          headers=sa.headers)
    assert r.status_code == 403 and _err(r) == "ROLE_RESERVED_FOR_ADMIN_MASTER"
    # convite genérico ANTIGO de super_admin (anterior à AÇÃO 003) não vale mais
    raw = secrets.token_urlsafe(32)
    rid = (await db.execute(text("SELECT id FROM roles WHERE slug = 'super_admin'"))).scalar()
    await db.execute(text(
        "INSERT INTO user_invites (id, email, token_hash, invited_by, role_id, scope_type, expires_at, created_at) "
        "VALUES (:id, 'velho@empresa.com.br', :h, :by, :r, 'GLOBAL', :exp, now())"),
        {"id": uuid.uuid4(), "h": sha256_hex(raw), "by": a.id, "r": rid,
         "exp": utcnow() + timedelta(days=3)})
    await db.commit()
    r = await client.post(f"{API}/auth/invites/accept",
                          json={"token": raw, "name": "Velho SA", "password": strong_password()})
    assert r.status_code == 400 and _err(r) == "INVITE_INVALID"
    assert (await db.execute(text(
        "SELECT count(*) FROM role_assignments ra JOIN roles r ON r.id = ra.role_id WHERE r.slug = 'super_admin'"))
    ).scalar() == 1  # só o Master


async def test_setup_token_scope_and_replay(client, db):
    m = await bootstrap_master(dual=True)
    r = await client.post(f"{API}/auth/login", json={"login": m.email, "password": m.password})
    tok = r.json()["mfa_setup_token"]
    h = auth_headers(tok)
    for method, path in (("GET", "/me"), ("GET", "/users"), ("POST", "/realtime/ticket"), ("GET", "/security/masters"),
                         ("GET", "/auth/sessions"), ("POST", "/auth/logout-all")):
        r = await client.request(method, f"{API}{path}", headers=h)
        assert r.status_code == 401, (path, r.status_code)
    # token de setup não é refresh nem cookie; após trocar a senha o anterior morre
    new_pw = strong_password()
    r = await client.post(f"{API}/auth/setup/password", json={"current_password": m.password, "new_password": new_pw},
                          headers=h)
    assert r.status_code == 200 and "set-cookie" not in r.headers
    r = await client.post(f"{API}/auth/mfa/totp/setup", json={"password": new_pw}, headers=h)
    assert r.status_code == 401  # replay do token antigo
    # token forjado/alterado
    assert (await client.get(f"{API}/auth/setup/status", headers=auth_headers(tok[:-2] + "xx"))).status_code == 401


async def test_third_master_impossible_everywhere(client, db, monkeypatch):
    a = await active_master(db)
    b = await make_user(db, role="super_admin", mfa=True, admin_master_seat=2)
    await adopt_master_seats(db, b)
    sa = await login_as(client, a)
    r = await client.post(f"{API}/security/masters/invites",
                          json={"full_name": "Terceiro", "email": "terceiro@empresa.com.br", "totp_code": "123456"},
                          headers=sa.headers)
    assert r.status_code == 409 and _err(r) == "ADMIN_MASTER_SEATS_FULL"
    monkeypatch.setenv("RD_MASTER2_EMAIL", "terceiro.cli@empresa.com.br")
    monkeypatch.setenv("RD_MASTER2_NAME", "Terceiro CLI")
    monkeypatch.setenv("RD_MASTER2_PASSWORD", strong_password())
    with pytest.raises(SystemExit):
        await cli.cmd_bootstrap_masters([2])
    c = await make_user(db, role="employee")
    c_id = c.id
    from app.core.errors import DomainError
    from app.modules.security import masters

    for seat in (1, 2):
        with pytest.raises(DomainError) as ei:
            await masters.assign_seat(db, user_id=c_id, seat=seat, source="CLI_BOOTSTRAP")
        await db.rollback()
        assert ei.value.status in (409, 400)
    with pytest.raises(Exception):
        await db.execute(text("UPDATE users SET admin_master_seat = 3 WHERE id = :i"), {"i": c_id})
    await db.rollback()
    assert (await db.execute(text("SELECT count(*) FROM users WHERE admin_master_seat IS NOT NULL"))).scalar() == 2


async def test_activation_cannot_be_done_by_inviter_session(client, db, mail):
    """O convidante não obtém o token (só no e-mail de B) e nada na API devolve o link."""
    a = await active_master(db)
    sa = await login_as(client, a)
    r = await client.post(f"{API}/security/masters/invites",
                          json={"full_name": "Sócio B", "email": "b@empresa.com.br", "totp_code": totp_code(a)},
                          headers=sa.headers)
    assert r.status_code == 201
    from tests.sec_helpers import token_from_mail

    token = token_from_mail(mail, "b@empresa.com.br")
    for path in ("/security/masters", "/me", "/auth/setup/status", "/security/approval-mode"):
        body = (await client.get(f"{API}{path}", headers=sa.headers)).text
        assert token not in body and sha256_hex(token) not in body
    rows = (await db.execute(text("SELECT token_hash FROM admin_master_invites"))).all()
    assert rows == [(sha256_hex(token),)]  # banco guarda só o hash


async def test_concurrent_invites_one_wins(client_factory, db, mail, monkeypatch):
    """Dois convites simultâneos (TOTP neutralizado para isolar a corrida) ⇒ exatamente um 201."""
    from app.modules.auth import service as svc

    a = await active_master(db)
    c0 = client_factory()
    sa = await login_as(c0, a)

    async def _ok(*args, **kw):
        return "totp"

    monkeypatch.setattr(svc, "verify_second_factor", _ok)
    clients = [client_factory() for _ in range(2)]
    rs = await asyncio.gather(*[
        c.post(f"{API}/security/masters/invites",
               json={"full_name": f"Sócio B{i}", "email": f"b{i}@empresa.com.br", "totp_code": "123456"},
               headers=sa.headers) for i, c in enumerate(clients)])
    codes = sorted(r.status_code for r in rs)
    assert codes == [201, 409], [r.text for r in rs]
    assert {_err(r) for r in rs if r.status_code == 409} == {"MASTER_INVITE_ALREADY_PENDING"}
    assert (await db.execute(text("SELECT count(*) FROM admin_master_invites WHERE status = 'PENDING'"))).scalar() == 1


async def test_concurrent_activation_one_wins(client_factory, db, mail):
    import pyotp

    from tests.sec_helpers import token_from_mail

    a = await active_master(db)
    c0 = client_factory()
    sa = await login_as(c0, a)
    r = await c0.post(f"{API}/security/masters/invites",
                      json={"full_name": "Sócio B", "email": "b@empresa.com.br", "totp_code": totp_code(a)}, headers=sa.headers)
    assert r.status_code == 201
    token = token_from_mail(mail, "b@empresa.com.br")
    secret = (await c0.post(f"{API}/auth/master-activation/totp", json={"token": token})).json()["secret"]
    code = pyotp.TOTP(secret).now()
    clients = [client_factory() for _ in range(3)]
    rs = await asyncio.gather(*[
        c.post(f"{API}/auth/master-activation/complete",
               json={"token": token, "password": strong_password(), "totp_code": code,
                     "financial_password": "FinB-" + secrets.token_hex(5)}) for c in clients])
    codes = sorted(r.status_code for r in rs)
    assert codes.count(200) == 1 and all(c in (400, 409) for c in codes if c != 200), [r.text for r in rs]
    assert (await db.execute(text("SELECT count(*) FROM users WHERE admin_master_seat = 2"))).scalar() == 1
    assert (await db.execute(text("SELECT count(*) FROM admin_master_seat_events WHERE seat = 2"))).scalar() == 1
    assert CSRF and make_org
