"""Redis indisponível → autenticação responde 503 claro e rápido (P002 §50).

Nunca 401 (não pode parecer senha errada), nunca conta tentativa de lockout, não apaga o
cookie de refresh, não queima código TOTP nem desafio de 2FA; com o Redis de volta, tudo
volta a funcionar. Rotas autenticadas comuns seguem funcionando pelo banco (fonte de
verdade da revogação), sem esperar timeout a cada request (disjuntor).
"""
import time

import pytest
from sqlalchemy import func, select

from app.core import ratelimit
from app.core import redis as rd
from app.core.config import settings
from app.core.errors import ServiceUnavailable
from app.models.audit import AuditLog
from app.models.users import UserSession
from tests.factories import CSRF, get_user, login_as, make_user, totp_code

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
VERIFY = "/api/v1/auth/mfa/verify"
MSG = "Serviço temporariamente indisponível, tente em instantes."

UNAVAILABLE = {
    "conexao-recusada": "redis://127.0.0.1:1/15",
    "sem-resposta": "redis://10.255.255.1:6379/15",
    "nome-inexistente": "redis://rd-redis-parado.invalid:6379/15",
}


class _RedisSwitch:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.good = settings.REDIS_URL

    async def off(self, url: str = UNAVAILABLE["conexao-recusada"]) -> None:
        await rd.close_redis()
        self.mp.setattr(settings, "REDIS_URL", url)
        rd.reset_breaker()

    async def on(self) -> None:
        await rd.close_redis()
        self.mp.setattr(settings, "REDIS_URL", self.good)
        rd.reset_breaker()


@pytest.fixture
async def redis_switch(monkeypatch):
    sw = _RedisSwitch(monkeypatch)
    yield sw
    await sw.on()


def _assert_503(r, max_s: float, elapsed: float):
    assert r.status_code == 503, r.text
    err = r.json()["error"]
    assert err["code"] == "SERVICE_UNAVAILABLE" and err["message"] == MSG
    assert r.headers.get("retry-after") == "5"
    assert elapsed < max_s, f"demorou {elapsed:.2f}s"


async def _timed(coro):
    t0 = time.monotonic()
    r = await coro
    return r, time.monotonic() - t0


@pytest.mark.parametrize("kind", list(UNAVAILABLE), ids=list(UNAVAILABLE))
async def test_login_503_fast_never_401_and_no_lockout(client, db, redis_switch, kind):
    u = await make_user(db, role="employee")
    await redis_switch.off(UNAVAILABLE[kind])
    cases = [(u.email, u.password), (u.email, "senha-errada-1"), ("ninguem@example.com", "senha-errada-1")]
    for i, (login, pw) in enumerate(cases * 2):  # 6 tentativas: acima do limite de lockout
        r, dt = await _timed(client.post(LOGIN, json={"login": login, "password": pw}))
        # 1ª chamada pode esperar o timeout de conexão; as seguintes saem pelo disjuntor
        _assert_503(r, 2.0 if i == 0 else 0.5, dt)
    fresh = await get_user(db, u.id)
    assert fresh.failed_login_attempts == 0 and fresh.locked_until is None
    n_fail = (await db.execute(select(func.count()).select_from(AuditLog).where(
        AuditLog.action.in_(["auth.login_failed", "auth.account_locked"])))).scalar_one()
    assert n_fail == 0
    assert (await db.execute(select(func.count()).select_from(UserSession))).scalar_one() == 0
    # Redis de volta → login normal
    await redis_switch.on()
    r = await client.post(LOGIN, json={"login": u.email, "password": u.password})
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_refresh_503_keeps_cookie_and_recovers(client, db, redis_switch):
    u = await make_user(db, role="employee")
    await login_as(client, u)
    await redis_switch.off()
    r, dt = await _timed(client.post(REFRESH, headers=CSRF))
    _assert_503(r, 2.0, dt)
    assert "set-cookie" not in {k.lower() for k in r.headers}  # sessão não é derrubada
    await redis_switch.on()
    r = await client.post(REFRESH, headers=CSRF)
    assert r.status_code == 200  # refresh não foi consumido durante a indisponibilidade


async def test_mfa_verify_503_does_not_burn_code_or_count_failure(client, db, redis_switch):
    u = await make_user(db, role="employee", mfa=True)
    r = await client.post(LOGIN, json={"login": u.email, "password": u.password})
    tok = r.json()["mfa_token"]
    code = totp_code(u)
    await redis_switch.off()
    r, dt = await _timed(client.post(VERIFY, json={"mfa_token": tok, "code": code}))
    _assert_503(r, 2.0, dt)
    fresh = await get_user(db, u.id)
    assert fresh.failed_login_attempts == 0 and fresh.totp_last_step is None
    await redis_switch.on()
    r = await client.post(VERIFY, json={"mfa_token": tok, "code": code})
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_other_auth_entry_points_503_and_db_backed_routes_keep_working(client, db, redis_switch):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    await redis_switch.off()
    for method, path, body in [
        ("post", "/api/v1/auth/password/forgot", {"email": u.email}),
        ("post", "/api/v1/auth/password/reset", {"email": u.email, "code": "123456", "new_password": "NovaSenha2026x"}),
        ("post", "/api/v1/auth/invites/accept", {"token": "x" * 30, "name": "Fulano", "password": "NovaSenha2026x"}),
    ]:
        r, dt = await _timed(getattr(client, method)(path, json=body))
        _assert_503(r, 2.0, dt)
    r, dt = await _timed(client.post("/api/v1/realtime/ticket", headers=s.headers))
    _assert_503(r, 2.0, dt)
    # rotas autenticadas comuns: o banco é a fonte de verdade da revogação → seguem funcionando, rápido
    r, dt = await _timed(client.get("/api/v1/me", headers=s.headers))
    assert r.status_code == 200 and dt < 2.0
    # logout funciona sem Redis e o access token passa a ser recusado (checagem no banco)
    r = await client.post("/api/v1/auth/logout", headers={**CSRF, **s.headers})
    assert r.status_code == 204
    r, dt = await _timed(client.get("/api/v1/me", headers=s.headers))
    assert r.status_code == 401 and dt < 2.0


async def test_ratelimit_is_fail_closed_unless_explicitly_open(redis_switch):
    await redis_switch.off()
    with pytest.raises(ServiceUnavailable):
        await ratelimit.hit("t_bucket", "id", limit=1, window_s=60)
    assert rd.redis_breaker_open()
    with pytest.raises(ServiceUnavailable):
        await ratelimit.enforce("t_bucket", "id", limit=1, window_s=60)
    assert await ratelimit.hit("t_bucket", "id", limit=1, window_s=60, fail_open=True) == (True, 0)


async def test_redis_up_keeps_strict_behaviour(client, db, redis_switch):
    """Com o Redis de pé nada muda: senha errada continua 401 e conta no lockout."""
    await redis_switch.on()
    u = await make_user(db, role="employee")
    r = await client.post(LOGIN, json={"login": u.email, "password": "senha-errada-1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert (await get_user(db, u.id)).failed_login_attempts == 1
