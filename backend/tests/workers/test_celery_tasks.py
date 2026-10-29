"""Tasks Celery executadas no MESMO processo, várias vezes (A2 E28: loop/engine).

A mesma task roda 3× seguidas (cada uma com ``asyncio.run`` → loop novo). Com o
engine NullPool criado por execução (``worker_session``) não há erro de
"attached to a different loop"; os efeitos no banco são verificados de verdade.
"""
import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core import events
from app.core.database import create_engine, session_factory, worker_session
from app.core.timeutils import utcnow
from app.workers.celery_app import QUEUES, celery_app
from app.workers.tasks.auth import purge_auth_artifacts, run_async


async def _seed_expired(n_tokens: int) -> uuid.UUID:
    from app.models.users import RefreshToken, User, UserSession

    eng = create_engine(nullpool=True)
    async with session_factory(eng)() as db:
        u = User(id=uuid.uuid4(), email=f"w{uuid.uuid4().hex[:6]}@example.com", full_name="W", hashed_password="x")
        db.add(u)
        await db.flush()
        s = UserSession(id=uuid.uuid4(), user_id=u.id, expires_at=utcnow() - timedelta(days=2), amr=["pwd"])
        db.add(s)
        await db.flush()
        for _ in range(n_tokens):
            db.add(RefreshToken(session_id=s.id, token_hash=uuid.uuid4().hex, expires_at=utcnow() - timedelta(days=2)))
        await db.commit()
    await eng.dispose()
    return u.id


def test_same_task_three_times_in_same_process(clean_state):
    results = []
    for i in range(3):
        asyncio.run(_seed_expired(i + 1))
        res = purge_auth_artifacts.apply().get(timeout=30)  # executa no processo (sem broker)
        results.append(res)
    assert [r["refresh_tokens_deleted"] for r in results] == [1, 2, 3]
    assert [r["sessions_expired"] for r in results] == [1, 1, 1]


def test_worker_session_survives_repeated_asyncio_run(clean_state):
    async def q():
        async with worker_session() as db:
            return (await db.execute(text("SELECT 1"))).scalar_one()

    assert [asyncio.run(q()) for _ in range(5)] == [1] * 5


def test_worker_session_rolls_back_on_error(clean_state):
    async def boom():
        async with worker_session() as db:
            await db.execute(text("INSERT INTO system_settings (key, value) VALUES ('x.y', '1'::jsonb)"))
            raise RuntimeError("falha proposital")

    with pytest.raises(RuntimeError):
        run_async(boom())

    async def count():
        async with worker_session() as db:
            return (await db.execute(text("SELECT count(*) FROM system_settings"))).scalar_one()

    assert run_async(count()) == 0


@celery_app.task(name="tests.publish_from_task")
def _publish_from_task(org_id: str) -> str:
    env = events.publish_sync("inventory.updated", {"org": org_id}, org_id=org_id)
    return env["id"]


def test_task_publishes_event_to_redis(clean_state):
    import redis as sync_redis

    from app.core.config import settings

    r = sync_redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    ps = r.pubsub()
    ps.psubscribe(events.subscribe_pattern())
    ps.get_message(timeout=1)
    org = str(uuid.uuid4())
    ids = [_publish_from_task.apply(args=[org]).get(timeout=10) for _ in range(3)]
    got = []
    for _ in range(100):
        m = ps.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if m:
            got.append(json.loads(m["data"])["id"])
        if len(got) == 3:
            break
    ps.close()
    r.close()
    assert got == ids


def test_worker_consumes_all_routed_queues():
    routed = {v["queue"] for v in celery_app.conf.task_routes.values()}
    assert routed <= set(QUEUES)
    assert set(celery_app.conf.task_queues) == set(QUEUES)
    assert "reports" in QUEUES
