"""Refresh com rotação/reuso, CSRF, logout, logout-all, sessões, revogação imediata do access."""
import asyncio

import pytest

from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.users import UserSession
from tests.factories import CSRF, auth_headers, login_as, make_user

REFRESH = "/api/v1/auth/refresh"


async def test_refresh_rotates_and_old_token_reuse_revokes_family(client, db):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    old_cookie = client.cookies.get("rd_refresh")
    r = await client.post(REFRESH, headers=CSRF)
    assert r.status_code == 200, r.text
    new_cookie = client.cookies.get("rd_refresh")
    assert new_cookie and new_cookie != old_cookie
    new_access = r.json()["access_token"]
    assert (await client.get("/api/v1/me", headers=auth_headers(new_access))).status_code == 200

    # atacante reapresenta o refresh ANTIGO → reuso detectado → família revogada
    r = await client.post(REFRESH, headers=CSRF, cookies={"rd_refresh": old_cookie})
    assert r.status_code == 401 and r.json()["error"]["code"] == "REFRESH_REUSED"
    # o refresh novo (legítimo) também deixou de valer e os access tokens morreram
    r = await client.post(REFRESH, headers=CSRF, cookies={"rd_refresh": new_cookie})
    assert r.status_code == 401
    assert (await client.get("/api/v1/me", headers=auth_headers(new_access))).status_code == 401
    assert (await client.get("/api/v1/me", headers=s.headers)).status_code == 401
    audits = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "auth.refresh_reuse_detected" in audits


async def test_refresh_requires_csrf_header_and_allowed_origin(client, db):
    u = await make_user(db, role="employee")
    await login_as(client, u)
    assert (await client.post(REFRESH)).status_code == 403
    assert (await client.post(REFRESH, headers={"X-RD-CSRF": "1"})).status_code == 403
    r = await client.post(REFRESH, headers={"X-RD-CSRF": "1", "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"
    assert (await client.post(REFRESH, headers=CSRF)).status_code == 200


async def test_refresh_without_cookie_401(client):
    r = await client.post(REFRESH, headers=CSRF)
    assert r.status_code == 401


@pytest.mark.concurrency
async def test_concurrent_refresh_only_one_wins(client_factory, db):
    """Duas rotações simultâneas do mesmo refresh: exatamente uma vence; a outra é tratada como reuso."""
    u = await make_user(db, role="employee")
    c = client_factory()
    await login_as(c, u)
    token = c.cookies.get("rd_refresh")
    c1, c2 = client_factory(), client_factory()
    r1, r2 = await asyncio.gather(
        c1.post(REFRESH, headers=CSRF, cookies={"rd_refresh": token}),
        c2.post(REFRESH, headers=CSRF, cookies={"rd_refresh": token}),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 401]


async def test_access_token_rejected_after_logout(client, db):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    assert (await client.get("/api/v1/me", headers=s.headers)).status_code == 200
    r = await client.post("/api/v1/auth/logout", headers={**CSRF, **s.headers})
    assert r.status_code == 204
    assert 'rd_refresh=""' in r.headers.get("set-cookie", "") or "Max-Age=0" in r.headers.get("set-cookie", "")
    assert (await client.get("/api/v1/me", headers=s.headers)).status_code == 401
    assert (await client.post(REFRESH, headers=CSRF)).status_code == 401


async def test_access_rejected_after_logout_even_with_redis_cache_cleared(client, db, redis):
    """Fonte de verdade é o banco: mesmo sem a marca no Redis, sessão revogada → 401."""
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    await client.post("/api/v1/auth/logout", headers={**CSRF, **s.headers})
    from app.core.config import settings

    keys = [k async for k in redis.scan_iter(match=settings.REDIS_KEY_PREFIX + "sess:revoked:*")]
    assert keys, "marca de revogação deveria existir no Redis"
    await redis.delete(*keys)
    assert (await client.get("/api/v1/me", headers=s.headers)).status_code == 401


async def test_logout_all_and_sessions_listing(client_factory, db):
    u = await make_user(db, role="employee")
    a, b = client_factory(), client_factory()
    sa = await login_as(a, u)
    sb = await login_as(b, u)
    r = await a.get("/api/v1/auth/sessions", headers=sa.headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2 and sum(1 for i in items if i["current"]) == 1
    assert {"id", "created_at", "last_seen_at", "ip", "user_agent", "device_label", "current"} <= set(items[0])
    assert items[0]["device_label"] == "Chrome em Windows"
    r = await a.post("/api/v1/auth/logout-all", headers=sa.headers)
    assert r.status_code == 204
    assert (await a.get("/api/v1/me", headers=sa.headers)).status_code == 401
    assert (await b.get("/api/v1/me", headers=sb.headers)).status_code == 401
    assert (await b.post(REFRESH, headers=CSRF)).status_code == 401


async def test_revoke_specific_session_and_cannot_revoke_others(client_factory, db):
    u = await make_user(db, role="employee")
    other = await make_user(db, role="employee")
    a, b, c = client_factory(), client_factory(), client_factory()
    sa = await login_as(a, u)
    sb = await login_as(b, u)
    so = await login_as(c, other)
    sid_b = next(i["id"] for i in (await a.get("/api/v1/auth/sessions", headers=sa.headers)).json()["items"]
                 if not i["current"])
    sid_other = (await c.get("/api/v1/auth/sessions", headers=so.headers)).json()["items"][0]["id"]
    assert (await a.delete(f"/api/v1/auth/sessions/{sid_other}", headers=sa.headers)).status_code == 404
    assert (await a.delete(f"/api/v1/auth/sessions/{sid_b}", headers=sa.headers)).status_code == 204
    assert (await b.get("/api/v1/me", headers=sb.headers)).status_code == 401
    assert (await a.get("/api/v1/me", headers=sa.headers)).status_code == 200
    assert (await c.get("/api/v1/me", headers=so.headers)).status_code == 200


async def test_session_records_real_ip_not_spoofed_xff(client, db):
    u = await make_user(db, role="employee")
    # par TCP 127.0.0.1 é proxy confiável: o IP real é o mais à direita não confiável
    r = await client.post("/api/v1/auth/login", json={"login": u.email, "password": u.password},
                          headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.23"})
    assert r.status_code == 200
    s = (await db.execute(select(UserSession).where(UserSession.user_id == u.id))).scalar_one()
    assert s.ip_address == "198.51.100.23"
