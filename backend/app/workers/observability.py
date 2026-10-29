"""
RD Mídia Indoor — Observabilidade do Celery (P002 §54) sem exporter de terceiros.

Sinais gravados no Redis da aplicação (``rkey`` → prefixo da instância), seguros com
prefork (operações atômicas, cada processo filho com a sua conexão):

- heartbeat do WORKER: task ``worker_heartbeat`` agendada pelo beat a cada 60 s grava o
  timestamp de execução (``obs:celery:worker_heartbeat``);
- heartbeat do BEAT: ``before_task_publish`` da task de heartbeat grava o timestamp de
  publicação (``obs:celery:beat_heartbeat_publish``) — distingue "beat parado" de
  "worker parado";
- falhas / retries por task: ``task_failure`` / ``task_retry`` → ``HINCRBY`` em hashes
  (``obs:celery:task_failures`` / ``obs:celery:task_retries``); nome da task como campo
  (conjunto limitado de tasks registradas → cardinalidade limitada).

Leitura (``collect_celery_metrics``) — custo O(1) por scrape: 1 MGET + 2 HGETALL no Redis
da aplicação e 1 pipeline de LLEN (5 filas × 4 níveis de prioridade do kombu) no broker.
Nenhum handler de sinal propaga exceção (observabilidade nunca derruba task).
"""
from __future__ import annotations

import asyncio
import time
import weakref
from typing import Iterable, Optional

import structlog

from app.core.config import settings
from app.core.redis import get_sync_redis, rkey

log = structlog.get_logger("rd.workers.observability")

HEARTBEAT_TASK = "app.workers.tasks.observability.worker_heartbeat"
HEARTBEAT_INTERVAL_S = 60

# kombu (transporte Redis): fila = lista com o nome da fila; prioridades != 0 usam
# "<fila>\x06\x16<prioridade>" (Channel.sep / priority_steps padrão).
KOMBU_PRIORITY_SEP = "\x06\x16"
KOMBU_PRIORITY_STEPS = (0, 3, 6, 9)


def k_worker_heartbeat() -> str:
    return rkey("obs", "celery", "worker_heartbeat")


def k_beat_publish() -> str:
    return rkey("obs", "celery", "beat_heartbeat_publish")


def k_failures() -> str:
    return rkey("obs", "celery", "task_failures")


def k_retries() -> str:
    return rkey("obs", "celery", "task_retries")


def _task_name(sender) -> str:
    name = getattr(sender, "name", None) or (sender if isinstance(sender, str) else None) or "unknown"
    return str(name)[:200]


# ─── Escrita (processos Celery) ──────────────────────────────────────────────

def record_worker_heartbeat(now: Optional[float] = None) -> float:
    ts = time.time() if now is None else now
    get_sync_redis().set(k_worker_heartbeat(), f"{ts:.3f}")
    return ts


def _on_task_failure(sender=None, **_kw) -> None:
    try:
        get_sync_redis().hincrby(k_failures(), _task_name(sender), 1)
    except Exception as exc:  # pragma: no cover - Redis indisponível
        log.warning("obs_failure_counter_error", error_type=type(exc).__name__)


def _on_task_retry(sender=None, **_kw) -> None:
    try:
        get_sync_redis().hincrby(k_retries(), _task_name(sender), 1)
    except Exception as exc:  # pragma: no cover
        log.warning("obs_retry_counter_error", error_type=type(exc).__name__)


def _on_before_publish(sender=None, **_kw) -> None:
    # sender = NOME da task publicada; só a de heartbeat interessa (beat)
    if sender != HEARTBEAT_TASK:
        return
    try:
        get_sync_redis().set(k_beat_publish(), f"{time.time():.3f}")
    except Exception as exc:  # pragma: no cover
        log.warning("obs_beat_publish_error", error_type=type(exc).__name__)


def _on_worker_ready(sender=None, **_kw) -> None:
    """Pré-cria os contadores (valor 0) das tasks registradas: a série existe ANTES da
    primeira falha, então ``increase()`` no Prometheus enxerga a 1ª ocorrência."""
    try:
        app = getattr(sender, "app", None)
        names = [n for n in (app.tasks.keys() if app else []) if not n.startswith("celery.")]
        if not names:
            return
        r = get_sync_redis()
        pipe = r.pipeline(transaction=False)
        for n in names:
            pipe.hsetnx(k_failures(), n[:200], 0)
            pipe.hsetnx(k_retries(), n[:200], 0)
        pipe.execute()
    except Exception as exc:  # pragma: no cover
        log.warning("obs_worker_ready_error", error_type=type(exc).__name__)


_connected = False


def connect_signals() -> None:
    """Idempotente. Chamado por ``app.workers.celery_app`` (worker, beat e quem publica)."""
    global _connected
    if _connected:
        return
    from celery.signals import before_task_publish, task_failure, task_retry, worker_ready

    worker_ready.connect(_on_worker_ready, weak=False)
    task_failure.connect(_on_task_failure, weak=False)
    task_retry.connect(_on_task_retry, weak=False)
    before_task_publish.connect(_on_before_publish, weak=False)
    _connected = True


# ─── Leitura (/metrics da API) ───────────────────────────────────────────────

_broker_clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _broker_client():
    import redis.asyncio as aioredis

    loop = asyncio.get_running_loop()
    c = _broker_clients.get(loop)
    if c is None:
        c = aioredis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True,
                                    socket_connect_timeout=0.5, socket_timeout=0.5)
        _broker_clients[loop] = c
    return c


def queue_keys(queue: str) -> list[str]:
    return [queue if p == 0 else f"{queue}{KOMBU_PRIORITY_SEP}{p}" for p in KOMBU_PRIORITY_STEPS]


async def queue_lengths(client, queues: Iterable[str]) -> dict[str, int]:
    queues = list(queues)
    pipe = client.pipeline(transaction=False)
    for q in queues:
        for k in queue_keys(q):
            pipe.llen(k)
    res = await pipe.execute()
    out: dict[str, int] = {}
    n = len(KOMBU_PRIORITY_STEPS)
    for i, q in enumerate(queues):
        out[q] = int(sum(int(x or 0) for x in res[i * n:(i + 1) * n]))
    return out


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def collect_celery_metrics(app_redis, broker=None, queues: Optional[Iterable[str]] = None,
                                 timeout_s: Optional[float] = None) -> dict:
    """→ {worker_heartbeat_ts, beat_publish_ts, failures{task:n}, retries{task:n}, queues{q:n}|None}.
    ``timeout_s``: limite por leitura (o /metrics usa 0,5 s; broker fora ⇒ queues=None)."""

    async def _t(coro):
        return await (asyncio.wait_for(coro, timeout_s) if timeout_s else coro)

    pipe = app_redis.pipeline(transaction=False)
    pipe.mget(k_worker_heartbeat(), k_beat_publish())
    pipe.hgetall(k_failures())
    pipe.hgetall(k_retries())
    (hb, pub), failures, retries = await _t(pipe.execute())
    if queues is None:
        from app.workers.celery_app import QUEUES

        queues = QUEUES
    qlen: Optional[dict] = None
    try:
        qlen = await _t(queue_lengths(broker if broker is not None else _broker_client(), queues))
    except Exception as exc:  # inclui asyncio.TimeoutError
        log.error("obs_queue_length_failed", error_type=type(exc).__name__)
    return {
        "worker_heartbeat_ts": _f(hb),
        "beat_publish_ts": _f(pub),
        "failures": {k: int(v) for k, v in (failures or {}).items()},
        "retries": {k: int(v) for k, v in (retries or {}).items()},
        "queues": qlen,
    }


# ─── Healthcheck do beat (BUG-QA-03) ─────────────────────────────────────────
# Saudável = o beat PUBLICOU o heartbeat recentemente. A existência de
# ``redbeat::lock`` não prova nada: o lock de um beat morto pode sobreviver (AOF) e o
# beat novo fica parado esperando por ele.

def beat_heartbeat_age_s(client=None) -> Optional[float]:
    """Segundos desde a última publicação do heartbeat pelo beat (None = nunca)."""
    import redis as sync_redis

    r = client or sync_redis.Redis.from_url(settings.REDIS_URL, decode_responses=True,
                                            socket_connect_timeout=2, socket_timeout=2)
    v = r.get(k_beat_publish())
    if not v:
        return None
    return max(0.0, time.time() - float(v))


def _main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m app.workers.observability")
    sub = ap.add_subparsers(dest="cmd", required=True)
    hc = sub.add_parser("beat-healthcheck", help="exit 0 se o beat publicou o heartbeat há < --max-age s")
    hc.add_argument("--max-age", type=float, default=180.0)
    args = ap.parse_args(argv)
    try:
        age = beat_heartbeat_age_s()
    except Exception as exc:
        print(f"unhealthy: Redis indisponível ({type(exc).__name__})")
        return 1
    if age is None:
        print("unhealthy: o beat nunca publicou o heartbeat")
        return 1
    if age > args.max_age:
        print(f"unhealthy: último heartbeat do beat há {age:.0f} s (máx. {args.max_age:.0f} s)")
        return 1
    print(f"healthy: heartbeat do beat há {age:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
