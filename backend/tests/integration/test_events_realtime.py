"""Barramento Redis Pub/Sub, filtro por escopo e tickets de WebSocket (contrato §5)."""
import asyncio
import json
import uuid

import pytest
from starlette.testclient import TestClient

from app.core import events
from app.core.authz import AuthContext, Grant
from app.core.permissions import ROLE_TEMPLATES
from tests.factories import CSRF, login_as, make_org, make_user


def _ctx(role, stype="GLOBAL", sid=None):
    g = Grant(uuid.uuid4(), uuid.uuid4(), role, False, stype, sid, None, ROLE_TEMPLATES[role].permissions)

    class U:
        id = uuid.uuid4()
        admin_master_seat = None
        totp_enabled = True
        is_admin_master = False

    return AuthContext(user=U(), grants=(g,))


async def test_publish_is_received_by_pattern_subscriber(redis):
    org = uuid.uuid4()
    ps = redis.pubsub()
    await ps.psubscribe(events.subscribe_pattern())
    await asyncio.sleep(0.05)
    env = await events.publish("campaign.updated", {"id": "c1", "status": "ACTIVE"}, org_id=org)
    got = None
    for _ in range(50):
        msg = await ps.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if msg:
            got = msg
            break
    await ps.punsubscribe()
    await ps.aclose()
    assert got is not None and got["channel"] == events.channel_org(org)
    data = json.loads(got["data"])
    assert data["id"] == env["id"] and data["type"] == "campaign.updated" and data["ts"].endswith("Z")
    assert events.public_envelope(data) == {k: data[k] for k in ("type", "id", "ts", "data")}


async def test_publish_sync_from_worker_code(redis):
    ps = redis.pubsub()
    await ps.psubscribe(events.subscribe_pattern())
    await asyncio.sleep(0.05)
    await asyncio.to_thread(events.publish_sync, "media.processing", {"id": "m1", "progress": 50}, org_id=uuid.uuid4())
    got = None
    for _ in range(50):
        got = await ps.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if got:
            break
    await ps.aclose()
    assert got and json.loads(got["data"])["type"] == "media.processing"


def test_unknown_event_type_rejected():
    with pytest.raises(ValueError):
        events.build_envelope("hack.me", {})


def test_visible_to_respects_scope():
    a, b = uuid.uuid4(), uuid.uuid4()
    adv_a = _ctx("advertiser", "ORGANIZATION", a)
    env_a = events.build_envelope("campaign.updated", {}, org_id=a)
    env_b = events.build_envelope("campaign.updated", {}, org_id=b)
    assert events.visible_to(adv_a, env_a) and not events.visible_to(adv_a, env_b)
    admin = _ctx("admin")
    assert events.visible_to(admin, env_b)
    platform = events.build_envelope("screen.status", {})
    assert events.visible_to(admin, platform) and not events.visible_to(adv_a, platform)
    personal = events.build_envelope("notification.created", {}, user_id=adv_a.user_id)
    assert events.visible_to(adv_a, personal) and not events.visible_to(admin, personal)
    fin = events.build_envelope("finance.approval_request", {})
    assert not events.visible_to(admin, fin)  # finance.approve só Admin Master


async def test_ws_ticket_single_use_and_ttl(client, db, redis):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    r = await client.post("/api/v1/realtime/ticket", headers=s.headers)
    assert r.status_code == 200 and r.json()["expires_in"] == 30
    ticket = r.json()["ticket"]
    from app.core.config import settings

    keys = [k async for k in redis.scan_iter(match=settings.REDIS_KEY_PREFIX + "wsticket:*")]
    assert len(keys) == 1 and 0 < await redis.ttl(keys[0]) <= 30
    assert ticket not in keys[0]  # guardado como hash
    info = await events.consume_ws_ticket(ticket)
    assert info["user_id"] == str(u.id)
    assert await events.consume_ws_ticket(ticket) is None  # uso único
    assert (await client.post("/api/v1/realtime/ticket")).status_code == 401


def test_ws_admin_ticket_flow_and_rejections(_test_database):
    """/ws/admin: ticket válido conecta; ticket reutilizado, inválido ou token na query → recusado."""
    from starlette.websockets import WebSocketDisconnect

    from app.core.database import create_engine, session_factory
    from app.main import app
    from tests.conftest import _cleanup_db, _cleanup_redis
    from tests.factories import make_user

    async def setup():
        eng = create_engine(nullpool=True)
        async with session_factory(eng)() as db:
            u = await make_user(db, role="employee")
        await eng.dispose()
        return u

    u = asyncio.run(setup())
    try:
        with TestClient(app, base_url="http://testserver") as tc:
            r = tc.post("/api/v1/auth/login", json={"login": u.email, "password": u.password})
            assert r.status_code == 200, r.text
            access = r.json()["access_token"]
            refresh = tc.cookies.get("rd_refresh")
            t = tc.post("/api/v1/realtime/ticket", headers={"Authorization": f"Bearer {access}"}).json()["ticket"]
            with tc.websocket_connect(f"/ws/admin?ticket={t}") as ws:
                ws.send_text(json.dumps({"type": "ping"}))
                # contrato do gateway WS novo (W-PLAY): ping → {"type": "pong"} (antes: "ping")
                assert json.loads(ws.receive_text())["type"] == "pong"
            for bad in (f"ticket={t}", "ticket=invalido", f"ticket={refresh}", f"ticket={access[:120]}"):
                with pytest.raises(WebSocketDisconnect):
                    with tc.websocket_connect(f"/ws/admin?{bad}") as ws:
                        ws.receive_text()
            with pytest.raises(WebSocketDisconnect):
                with tc.websocket_connect(f"/ws/admin?token={access}") as ws:  # parâmetro antigo
                    ws.receive_text()
            # sessão revogada: ticket emitido antes do logout não conecta
            t2 = tc.post("/api/v1/realtime/ticket", headers={"Authorization": f"Bearer {access}"}).json()["ticket"]
            assert tc.post("/api/v1/auth/logout", headers={**CSRF, "Authorization": f"Bearer {access}"}).status_code == 204
            with pytest.raises(WebSocketDisconnect):
                with tc.websocket_connect(f"/ws/admin?ticket={t2}") as ws:
                    ws.receive_text()
    finally:
        _cleanup_db()
        _cleanup_redis()


async def test_redis_pool_burst_above_max_connections_waits_instead_of_failing(redis, monkeypatch):
    """Rajada com mais operações simultâneas que o pool: o pool bloqueante espera conexão livre."""
    import redis.asyncio as aioredis

    from app.core import redis as rd
    from app.core.config import settings

    monkeypatch.setattr(settings, "REDIS_MAX_CONNECTIONS", 5)
    monkeypatch.setattr(settings, "REDIS_POOL_TIMEOUT_S", 10.0)
    client = rd._new_async_client()
    try:
        assert isinstance(client.connection_pool, aioredis.BlockingConnectionPool)
        assert client.connection_pool.max_connections == 5
        key = rd.rkey("burst")

        async def op(i):
            # BLPOP com timeout segura a conexão por um instante → força disputa pelo pool
            await client.blpop([key + f":{i}"], timeout=0.2)
            return await client.incr(key)

        results = await asyncio.gather(*(op(i) for i in range(60)))
        assert sorted(results) == list(range(1, 61))
        pool = client.connection_pool
        assert len(pool._in_use_connections) + len(pool._available_connections) <= 5
    finally:
        await client.aclose()


async def test_default_pool_is_blocking_with_configured_size(redis):
    import redis.asyncio as aioredis

    from app.core.config import settings

    assert isinstance(redis.connection_pool, aioredis.BlockingConnectionPool)
    assert redis.connection_pool.max_connections == settings.REDIS_MAX_CONNECTIONS == 200
