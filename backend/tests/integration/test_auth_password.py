"""Esqueci a senha (código de 6 dígitos por e-mail), reset, troca de senha."""
import re
import time
from datetime import timedelta

import time_machine
from sqlalchemy import select

from app.core.timeutils import utcnow
from app.models.users import PasswordResetCode
from tests.factories import CSRF, get_user, login_as, make_user

FORGOT = "/api/v1/auth/password/forgot"
RESET = "/api/v1/auth/password/reset"


def _code(mail, email):
    m = mail.last_to(email)
    assert m is not None, "e-mail não enviado"
    return re.search(r"\b(\d{6})\b", m.text).group(1)


async def test_forgot_is_202_without_enumeration(client, db, mail):
    u = await make_user(db, role="employee")
    r1 = await client.post(FORGOT, json={"email": u.email})
    r2 = await client.post(FORGOT, json={"email": "nao-existe@example.com"})
    assert r1.status_code == r2.status_code == 202 and r1.json() == r2.json()
    assert len(mail.outbox) == 1 and mail.outbox[0].to == u.email
    row = (await db.execute(select(PasswordResetCode))).scalar_one()
    assert len(row.code_hash) == 64 and _code(mail, u.email) not in row.code_hash  # só HMAC no banco


async def test_reset_single_use_revokes_sessions(client_factory, db, mail):
    u = await make_user(db, role="employee")
    a = client_factory()
    s = await login_as(a, u)
    c = client_factory()
    await c.post(FORGOT, json={"email": u.email})
    code = _code(mail, u.email)
    r = await c.post(RESET, json={"email": u.email, "code": code, "new_password": "curta"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "PASSWORD_POLICY"
    r = await c.post(RESET, json={"email": u.email, "code": code, "new_password": "NovaSenhaForte2026"})
    assert r.status_code == 204
    # uso único
    r = await c.post(RESET, json={"email": u.email, "code": code, "new_password": "OutraSenhaForte2026"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_RESET_CODE"
    # sessões anteriores revogadas
    assert (await a.get("/api/v1/me", headers=s.headers)).status_code == 401
    assert (await a.post("/api/v1/auth/refresh", headers=CSRF)).status_code == 401
    u.password = "NovaSenhaForte2026"
    await login_as(c, u)


async def test_reset_code_expires_after_15_minutes(client, db, mail):
    u = await make_user(db, role="employee")
    await client.post(FORGOT, json={"email": u.email})
    code = _code(mail, u.email)
    with time_machine.travel(utcnow() + timedelta(minutes=16), tick=False):
        r = await client.post(RESET, json={"email": u.email, "code": code, "new_password": "NovaSenhaForte2026"})
    assert r.status_code == 400


async def test_reset_wrong_code_attempts_invalidate(client, db, mail):
    u = await make_user(db, role="employee")
    await client.post(FORGOT, json={"email": u.email})
    code = _code(mail, u.email)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(5):
        r = await client.post(RESET, json={"email": u.email, "code": wrong, "new_password": "NovaSenhaForte2026"})
        assert r.status_code == 400
    r = await client.post(RESET, json={"email": u.email, "code": code, "new_password": "NovaSenhaForte2026"})
    assert r.status_code == 400  # código correto não vale mais após 5 erros


async def test_new_forgot_invalidates_previous_code(client, db, mail):
    u = await make_user(db, role="employee")
    await client.post(FORGOT, json={"email": u.email})
    first = _code(mail, u.email)
    await client.post(FORGOT, json={"email": u.email})
    second = _code(mail, u.email)
    if first != second:
        r = await client.post(RESET, json={"email": u.email, "code": first, "new_password": "NovaSenhaForte2026"})
        assert r.status_code == 400
    r = await client.post(RESET, json={"email": u.email, "code": second, "new_password": "NovaSenhaForte2026"})
    assert r.status_code == 204


async def test_change_password(client_factory, db):
    u = await make_user(db, role="employee")
    a, b = client_factory(), client_factory()
    sa = await login_as(a, u)
    sb = await login_as(b, u)
    r = await a.post("/api/v1/auth/password/change", json={"current_password": "errada-123x", "new_password": "NovaSenhaForte2026"},
                     headers=sa.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_PASSWORD"
    r = await a.post("/api/v1/auth/password/change",
                     json={"current_password": u.password, "new_password": u.email.split("@")[0] + "123"},
                     headers=sa.headers)
    assert r.status_code == 400
    r = await a.post("/api/v1/auth/password/change", json={"current_password": u.password, "new_password": "NovaSenhaForte2026"},
                     headers=sa.headers)
    assert r.status_code == 204
    fresh = await get_user(db, u.id)
    assert fresh.hashed_password.startswith("$argon2id$") and fresh.password_changed_at is not None
    assert (await a.get("/api/v1/me", headers=sa.headers)).status_code == 200  # sessão atual mantida
    assert (await b.get("/api/v1/me", headers=sb.headers)).status_code == 401  # demais revogadas
    r = await a.post("/api/v1/auth/login", json={"login": u.email, "password": u.password})
    assert r.status_code == 401
    u.password = "NovaSenhaForte2026"
    await login_as(a, u)


async def test_failed_current_password_counts_for_lockout(client, db):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    for _ in range(4):
        r = await client.post("/api/v1/auth/password/change",
                              json={"current_password": "errada-123x", "new_password": "NovaSenhaForte2026"},
                              headers=s.headers)
        assert r.status_code == 400
    r = await client.post("/api/v1/auth/password/change",
                          json={"current_password": "errada-123x", "new_password": "NovaSenhaForte2026"},
                          headers=s.headers)
    assert r.status_code == 423
    assert time.time()
