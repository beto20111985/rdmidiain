"""Observabilidade do Celery (W-INFRA, P002 §54): heartbeat, filas, falhas/retries.

Redis REAL (db 15 + prefixo do teste). O broker real NÃO é tocado: o cliente do broker é
substituído pelo Redis de teste e as "filas" usam nomes com o prefixo do teste.
"""
import asyncio
import time

import pytest

from app.core.config import settings
from app.workers import observability as obs
from app.workers.celery_app import QUEUES, celery_app


class _FakeTask:
    def __init__(self, name):
        self.name = name


def test_queues_unchanged_and_heartbeat_scheduled():
    assert QUEUES == ("default", "media", "notifications", "financial", "reports")
    entry = celery_app.conf.beat_schedule["observability-worker-heartbeat"]
    assert entry["task"] == obs.HEARTBEAT_TASK
    assert entry["schedule"] == 60
    assert entry["options"] == {"queue": "default", "expires": 55}
    assert "app.workers.tasks.observability" in celery_app.conf.include  # o worker importa
    celery_app.loader.import_default_modules()  # o que o worker faz no boot
    assert obs.HEARTBEAT_TASK in celery_app.tasks
    assert celery_app.amqp.router.route({}, obs.HEARTBEAT_TASK)["queue"].name == "default"


async def test_heartbeat_task_records_timestamp(redis):
    from app.workers.tasks.observability import worker_heartbeat

    before = time.time()
    worker_heartbeat.apply()  # execução local (mesmo código do worker)
    c = await obs.collect_celery_metrics(redis, broker=redis, queues=[])
    assert before - 1 <= c["worker_heartbeat_ts"] <= time.time() + 1
    assert c["beat_publish_ts"] == 0.0  # nunca publicado neste prefixo


async def test_signals_count_failures_and_retries_per_task(redis):
    from celery.signals import before_task_publish, task_failure, task_retry

    t1, t2 = _FakeTask("app.workers.tasks.x.a"), _FakeTask("app.workers.tasks.x.b")
    for _ in range(3):
        task_failure.send(sender=t1, task_id="i", exception=ValueError("x"))
    task_failure.send(sender=t2, task_id="j", exception=ValueError("y"))
    task_retry.send(sender=t1, request=None, reason="r")
    task_retry.send(sender=t1, request=None, reason="r")
    # publicação de outra task NÃO conta como heartbeat do beat; a de heartbeat conta
    before_task_publish.send(sender="app.workers.tasks.x.a", body={}, exchange="", routing_key="default")
    assert (await redis.get(obs.k_beat_publish())) is None
    before_task_publish.send(sender=obs.HEARTBEAT_TASK, body={}, exchange="", routing_key="default")

    c = await obs.collect_celery_metrics(redis, broker=redis, queues=[])
    assert c["failures"] == {"app.workers.tasks.x.a": 3, "app.workers.tasks.x.b": 1}
    assert c["retries"] == {"app.workers.tasks.x.a": 2}
    assert c["beat_publish_ts"] > 0


async def test_queue_lengths_sum_kombu_priority_lists(redis):
    q1 = settings.REDIS_KEY_PREFIX + "qa"
    q2 = settings.REDIS_KEY_PREFIX + "qb"
    await redis.rpush(q1, "m1", "m2")
    await redis.rpush(f"{q1}{obs.KOMBU_PRIORITY_SEP}3", "m3")
    await redis.rpush(f"{q1}{obs.KOMBU_PRIORITY_SEP}9", "m4", "m5")
    lens = await obs.queue_lengths(redis, [q1, q2])
    assert lens == {q1: 5, q2: 0}


async def test_metrics_endpoint_exposes_celery_series(redis, monkeypatch):
    from app.modules.websocket import metrics as M

    obs.record_worker_heartbeat(now=1_700_000_000.5)
    obs._on_task_failure(sender=_FakeTask("app.workers.tasks.x.c"))
    monkeypatch.setattr(obs, "_broker_client", lambda: redis)  # nunca o broker real
    await redis.rpush("default", "teste-w-infra")  # db 15 (Redis de teste), removido abaixo
    try:
        text = await M.render_metrics()
    finally:
        await redis.delete("default")
    lines = text.splitlines()
    assert "rd_worker_last_heartbeat_timestamp_seconds 1700000000.500" in lines
    assert "rd_beat_last_heartbeat_publish_timestamp_seconds 0.000" in lines
    assert 'rd_celery_queue_length{queue="default"} 1' in lines
    for q in ("media", "notifications", "financial", "reports"):
        assert f'rd_celery_queue_length{{queue="{q}"}} 0' in lines
    assert "rd_celery_broker_up 1" in lines
    assert "rd_metrics_redis_up 1" in lines
    assert 'rd_celery_task_failures_total{task="app.workers.tasks.x.c"} 1' in lines
    assert "# TYPE rd_celery_task_failures_total counter" in lines
    # HELP/TYPE uma única vez por métrica (formato de exposição válido)
    helps = [ln.split()[2] for ln in lines if ln.startswith("# HELP ")]
    assert len(helps) == len(set(helps))


async def test_broker_unavailable_does_not_break_metrics(redis, monkeypatch):
    from app.modules.websocket import metrics as M

    class _Broken:
        def pipeline(self, **_kw):
            raise ConnectionError("broker fora")

    monkeypatch.setattr(obs, "_broker_client", lambda: _Broken())
    text = await M.render_metrics()
    assert "rd_celery_broker_up 0" in text.splitlines()
    assert "rd_worker_last_heartbeat_timestamp_seconds 0.000" in text.splitlines()


@pytest.mark.parametrize("sender,expected", [(_FakeTask("a.b"), "a.b"), ("c.d", "c.d"), (None, "unknown")])
def test_task_name_normalization(sender, expected):
    assert obs._task_name(sender) == expected


async def test_worker_ready_precreates_zero_counters(redis):
    from celery.signals import worker_ready

    class _Consumer:
        app = celery_app

    celery_app.loader.import_default_modules()
    assert obs.HEARTBEAT_TASK in celery_app.tasks
    obs._on_task_failure(sender=_FakeTask(obs.HEARTBEAT_TASK))  # já existente: não pode zerar
    worker_ready.send(sender=_Consumer())
    c = await obs.collect_celery_metrics(redis, broker=redis, queues=[])
    assert c["failures"][obs.HEARTBEAT_TASK] == 1
    assert c["failures"]["app.workers.tasks.auth.purge_auth_artifacts"] == 0
    assert c["retries"]["app.workers.tasks.auth.purge_auth_artifacts"] == 0
    assert not any(k.startswith("celery.") for k in c["failures"])


# ─── BUG-QA-03: beat com lock curto; healthcheck pelo heartbeat; /metrics sem Redis ───

def test_beat_lock_short_and_renewed_every_loop():
    from redbeat.schedulers import RedBeatScheduler

    from app.workers.celery_app import BEAT_MAX_LOOP_INTERVAL_S, REDBEAT_LOCK_TIMEOUT_S

    assert celery_app.conf.beat_max_loop_interval == BEAT_MAX_LOOP_INTERVAL_S == 30
    assert celery_app.conf.redbeat_lock_timeout == REDBEAT_LOCK_TIMEOUT_S == 90
    assert REDBEAT_LOCK_TIMEOUT_S >= 3 * BEAT_MAX_LOOP_INTERVAL_S  # folga de 3 renovações
    sched = RedBeatScheduler(app=celery_app, lazy=True)  # lazy: não toca o Redis
    assert sched.max_interval == 30   # tick (renova o lock) ao menos a cada 30 s
    assert sched.lock_timeout == 90   # beat morto libera o posto em <= 90 s (antes: 1500 s)


def test_beat_healthcheck_uses_heartbeat_not_lock(redis):
    import redis as sync_redis

    r = sync_redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        assert obs._main(["beat-healthcheck"]) == 1                    # nunca publicou
        r.set(settings.REDIS_KEY_PREFIX + "redbeat::lock", "orfao")    # lock órfão não basta
        assert obs._main(["beat-healthcheck"]) == 1
        r.set(obs.k_beat_publish(), f"{time.time() - 30:.3f}")
        assert obs._main(["beat-healthcheck", "--max-age", "180"]) == 0
        r.set(obs.k_beat_publish(), f"{time.time() - 600:.3f}")
        assert obs._main(["beat-healthcheck", "--max-age", "180"]) == 1
    finally:
        r.close()


def test_beat_healthcheck_redis_down_is_unhealthy(monkeypatch):
    def _boom(client=None):
        raise ConnectionError("redis fora")

    monkeypatch.setattr(obs, "beat_heartbeat_age_s", _boom)
    assert obs._main(["beat-healthcheck"]) == 1


class _HangingRedis:
    async def ping(self):
        await asyncio.sleep(3600)


async def test_metrics_fast_and_flagged_when_redis_hangs(monkeypatch):
    from app.modules.websocket import metrics as M

    async def _get():
        return _HangingRedis()

    monkeypatch.setattr(M, "get_redis", _get)
    t0 = time.monotonic()
    text = await M.render_metrics()
    dt = time.monotonic() - t0
    lines = text.splitlines()
    assert dt < 1.5, dt
    assert "rd_metrics_redis_up 0" in lines
    assert not any(ln.startswith(("rd_ws_", "rd_celery_", "rd_worker_", "rd_screens_total")) for ln in lines)


async def test_metrics_fast_with_unreachable_redis_host(monkeypatch):
    import redis.asyncio as aioredis

    from app.modules.websocket import metrics as M

    # IP não roteável: o connect fica pendurado (SYN sem resposta), como um Redis caído
    dead = aioredis.Redis(host="10.255.255.1", port=6379, socket_connect_timeout=10, socket_timeout=10)

    async def _get():
        return dead

    monkeypatch.setattr(M, "get_redis", _get)
    t0 = time.monotonic()
    text = await M.render_metrics()
    assert time.monotonic() - t0 < 1.5
    assert "rd_metrics_redis_up 0" in text.splitlines()
    await dead.aclose()


async def test_metrics_partial_hang_still_returns_celery(redis, monkeypatch):
    from app.modules.websocket import gateway, metrics as M

    async def _hang():
        await asyncio.sleep(3600)

    monkeypatch.setattr(gateway, "global_counts", _hang)
    monkeypatch.setattr(obs, "_broker_client", lambda: redis)
    t0 = time.monotonic()
    text = await M.render_metrics()
    assert time.monotonic() - t0 < 4.0
    lines = text.splitlines()
    assert "rd_metrics_redis_up 1" in lines
    assert not any(ln.startswith("rd_ws_player_connections") for ln in lines)
    assert any(ln.startswith("rd_worker_last_heartbeat_timestamp_seconds") for ln in lines)


async def test_metrics_http_endpoint_does_not_hang_without_redis(client, monkeypatch):
    from app.modules.websocket import metrics as M

    async def _get():
        return _HangingRedis()

    monkeypatch.setattr(M, "get_redis", _get)
    t0 = time.monotonic()
    resp = await client.get("/metrics")
    assert time.monotonic() - t0 < 2.0
    assert resp.status_code == 200
    assert "rd_metrics_redis_up 0" in resp.text

