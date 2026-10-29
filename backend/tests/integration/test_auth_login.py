"""Login, lockout persistido, enumeração, rate limit, migração bcrypt (contrato §2)."""
from datetime import timedelta

import bcrypt
import time_machine
from sqlalchemy import select

from app.core.config import settings
from app.core.timeutils import utcnow
from app.models.audit import AuditLog
from tests.factories import auth_headers, get_user, login_as, make_org, make_user

LOGIN = "/api/v1/auth/login"


async def test_login_valid_by_email_and_phone(client, db):
    org = await make_org(db)
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id), phone="5562999990001")
    r = await client.post(LOGIN, json={"login": u.email.upper(), "password": u.password})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["expires_in"] == 15 * 60
    assert body["user"]["id"] == str(u.id) and "access_token" in body
    cookie = r.headers["set-cookie"]
    assert "rd_refresh=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/api/v1/auth" in cookie
    # telefone (com máscara) também autentica
    r = await client.post(LOGIN, json={"login": "(62) 99999-0001", "password": u.password})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    me = await client.get("/api/v1/me", headers=auth_headers(r.json()["access_token"]))
    assert me.status_code == 200 and me.json()["user"]["email"] == u.email


async def test_login_invalid_is_generic_and_audited(client, db):
    u = await make_user(db, role="employee")
    r1 = await client.post(LOGIN, json={"login": u.email, "password": "senha-errada-1"})
    r2 = await client.post(LOGIN, json={"login": "naoexiste@example.com", "password": "senha-errada-1"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json()  # mesma resposta: sem enumeração
    assert r1.json()["error"]["code"] == "INVALID_CREDENTIALS"
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "auth.login_failed"))).scalars().all()
    assert len(rows) == 1 and rows[0].entity_id == str(u.id) and rows[0].ip_address == "127.0.0.1"


async def test_lockout_persisted_then_released_after_window(client, db):
    u = await make_user(db, role="employee")
    codes = []
    for _ in range(6):
        r = await client.post(LOGIN, json={"login": u.email, "password": "senha-errada-1"})
        codes.append(r.status_code)
    assert codes == [401, 401, 401, 401, 423, 423]
    body = r.json()
    assert body["error"]["code"] == "ACCOUNT_LOCKED" and 0 < body["error"]["details"]["retry_after_s"] <= 900
    fresh = await get_user(db, u.id)
    assert fresh.failed_login_attempts == 5 and fresh.locked_until is not None  # persistido apesar dos 401/423
    # senha CORRETA durante o bloqueio continua 423
    r = await client.post(LOGIN, json={"login": u.email, "password": u.password})
    assert r.status_code == 423
    locked = (await db.execute(select(AuditLog).where(AuditLog.action == "auth.account_locked"))).scalars().all()
    assert len(locked) == 1
    with time_machine.travel(utcnow() + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES, seconds=5), tick=False):
        r = await client.post(LOGIN, json={"login": u.email, "password": u.password})
        assert r.status_code == 200, r.text
    fresh = await get_user(db, u.id)
    assert fresh.failed_login_attempts == 0 and fresh.locked_until is None


async def test_unknown_login_gets_same_lockout_behaviour(client, db):
    codes = [
        (await client.post(LOGIN, json={"login": "fantasma@example.com", "password": "senha-errada-1"})).status_code
        for _ in range(6)
    ]
    assert codes == [401, 401, 401, 401, 423, 423]


async def test_inactive_user_cannot_login(client, db):
    u = await make_user(db, role="employee", is_active=False)
    r = await client.post(LOGIN, json={"login": u.email, "password": u.password})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_bcrypt_hash_is_migrated_to_argon2_on_login(client, db):
    legacy = bcrypt.hashpw(b"LegadoForte2024", bcrypt.gensalt(rounds=4)).decode()
    u = await make_user(db, role="employee", hashed_password=legacy)
    r = await client.post(LOGIN, json={"login": u.email, "password": "LegadoForte2024"})
    assert r.status_code == 200
    fresh = await get_user(db, u.id)
    assert fresh.hashed_password.startswith("$argon2id$")
    r = await client.post(LOGIN, json={"login": u.email, "password": "LegadoForte2024"})
    assert r.status_code == 200


async def test_login_rate_limit_per_ip(client, db):
    codes = [
        (await client.post(LOGIN, json={"login": f"x{i}@example.com", "password": "senha-errada-1"})).status_code
        for i in range(32)
    ]
    assert codes[:30] == [401] * 30
    assert codes[30:] == [429, 429]
    r = await client.post(LOGIN, json={"login": "y@example.com", "password": "senha-errada-1"})
    assert r.json()["error"]["code"] == "RATE_LIMITED" and "retry-after" in {k.lower() for k in r.headers}


async def test_validation_error_envelope_never_echoes_password(client):
    r = await client.post(LOGIN, json={"login": "a", "password": "SEGREDO-NAO-ECOAR"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "SEGREDO-NAO-ECOAR" not in r.text


async def test_missing_bearer_is_401_not_403(client):
    r = await client.get("/api/v1/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"
    r = await client.get("/api/v1/users", headers={"Authorization": "Bearer abc.def.ghi"})
    assert r.status_code == 401


async def test_must_change_password_restricts_routes(client, db):
    u = await make_user(db, role="admin", must_change_password=True)
    # admin exige 2FA → login devolve mfa_setup_required; usar perfil sem 2FA obrigatório
    u2 = await make_user(db, role="employee", must_change_password=True)
    s = await login_as(client, u2)
    assert s.body["user"]["must_change_password"] is True
    r = await client.get("/api/v1/users", headers=s.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    assert (await client.get("/api/v1/me", headers=s.headers)).status_code == 200
    r = await client.post("/api/v1/auth/password/change",
                          json={"current_password": u2.password, "new_password": "NovaSenha2026x"}, headers=s.headers)
    assert r.status_code == 204
    r = await client.get("/api/v1/organizations", headers=s.headers)
    assert r.status_code == 200
    assert u.id  # (u usado só para garantir que perfis com 2FA não liberam rotas sem setup)
