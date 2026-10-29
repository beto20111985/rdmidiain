"""
RD Mídia Indoor — Métricas Prometheus baratas (P002 item 54; corrige A2 P16 "WS sempre 0").

- Conexões WS: soma GLOBAL dos contadores que cada processo da API publica no Redis a
  cada 15 s (``ws:proc:<id>``, TTL 45 s) — correto com N workers (antes: contagem do
  processo que respondeu o scrape, e de atributos inexistentes → sempre 0).
- Presença: ``ZCARD`` do sorted set de presença (O(1)).
- Contagens de domínio (telas/campanhas por status, PoP pendente de apropriação):
  calculadas no MÁXIMO uma vez por minuto em todo o cluster (cache Redis + trava NX),
  nunca por scrape.
- ``# HELP``/``# TYPE`` emitidos uma única vez por métrica.
- Celery (W-INFRA): heartbeat do worker/beat, LLEN das filas, falhas/retries por task
  (``app.workers.observability``) — leituras O(1) no Redis.
- Sem Redis (BUG-QA-03): PING com timeout de 0,5 s; falhou ⇒ ``rd_metrics_redis_up 0`` e
  as séries dependentes do Redis são omitidas — o scrape nunca trava.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import structlog
from sqlalchemy import text

from app.core.redis import get_redis, rkey

log = structlog.get_logger("rd.metrics")

CACHE_FRESH_S = 60
CACHE_STALE_S = 3600
# BUG-QA-03: /metrics nunca espera o Redis/DB além disto (scrape_timeout do Prometheus = 10 s)
REDIS_TIMEOUT_S = 0.5
DOMAIN_TIMEOUT_S = 2.0

_DOMAIN_QUERIES = {
    "screens_by_status": "SELECT status, COUNT(*) FROM screens GROUP BY status",
    "campaigns_by_status": "SELECT status, COUNT(*) FROM campaigns GROUP BY status",
    "pop_financial_pending": "SELECT 'PENDING', COUNT(*) FROM playback_events WHERE financial_status = 'PENDING'",
    "devices_by_presence": "SELECT presence, COUNT(*) FROM devices WHERE status = 'ACTIVE' GROUP BY presence",
}


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def metric(self, name: str, value, *, help_text: str, typ: str = "gauge", labels: Optional[dict] = None) -> None:
        if name not in self._declared:
            self._declared.add(name)
            self.lines.append(f"# HELP {name} {help_text}")
            self.lines.append(f"# TYPE {name} {typ}")
        lab = ""
        if labels:
            esc = {k: str(v).replace("\\", "\\\\").replace('"', '\\"') for k, v in labels.items()}
            lab = "{" + ",".join(f'{k}="{v}"' for k, v in esc.items()) + "}"
        self.lines.append(f"{name}{lab} {value}")

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


async def _cached_rows(name: str) -> list:
    r = await get_redis()
    fresh = rkey("metrics", "fresh", name)
    stale = rkey("metrics", "stale", name)
    raw = await r.get(fresh)
    if raw:
        return json.loads(raw)
    if await r.set(rkey("metrics", "lock", name), "1", nx=True, ex=30):
        from app.core.database import get_engine

        try:
            async with get_engine().connect() as conn:
                rows = [[str(a), int(b)] for a, b in (await conn.execute(text(_DOMAIN_QUERIES[name]))).all()]
            payload = json.dumps(rows)
            await r.set(fresh, payload, ex=CACHE_FRESH_S)
            await r.set(stale, payload, ex=CACHE_STALE_S)
            return rows
        except Exception as exc:
            log.error("metrics_domain_query_failed", metric=name, error_type=type(exc).__name__)
    raw = await r.get(stale)
    return json.loads(raw) if raw else []


async def render_metrics() -> str:
    """Nunca trava sem Redis (BUG-QA-03): 1 PING com timeout curto decide se as séries
    que dependem do Redis entram neste scrape; cada leitura também tem timeout próprio.
    Redis fora ⇒ ``rd_metrics_redis_up 0`` e as séries dependentes são OMITIDAS (o scrape
    responde rápido e o RdBackendDown não dispara falsamente)."""
    from app.modules.presence.service import online_count
    from app.modules.websocket.gateway import global_counts

    w = _Writer()
    redis_ok = False
    try:
        r = await get_redis()
        await asyncio.wait_for(r.ping(), REDIS_TIMEOUT_S)
        redis_ok = True
    except Exception as exc:  # inclui asyncio.TimeoutError
        log.warning("metrics_redis_unavailable", error_type=type(exc).__name__)
    w.metric("rd_metrics_redis_up", 1 if redis_ok else 0,
             help_text="1 se o Redis respondeu ao /metrics neste scrape (0 = series do Redis omitidas)")
    if not redis_ok:
        return w.render()

    try:
        c = await asyncio.wait_for(global_counts(), REDIS_TIMEOUT_S)
        present = await asyncio.wait_for(online_count(), REDIS_TIMEOUT_S)
        w.metric("rd_ws_player_connections", c["player"], help_text="Conexoes WS de players (todos os processos)")
        w.metric("rd_ws_admin_connections", c["admin"], help_text="Conexoes WS do painel (todos os processos)")
        w.metric("rd_ws_pairing_connections", c["pairing"], help_text="Conexoes WS de pareamento")
        w.metric("rd_ws_processes", c["processes"], help_text="Processos da API com gateway WS ativo")
        w.metric("rd_ws_messages_in_total", c["in"], help_text="Mensagens recebidas pelos gateways", typ="counter")
        w.metric("rd_ws_messages_out_total", c["out"], help_text="Mensagens enviadas pelos gateways", typ="counter")
        w.metric("rd_ws_dropped_total", c["dropped"], help_text="Conexoes encerradas por fila cheia", typ="counter")
        w.metric("rd_players_present", present, help_text="Players com presenca ativa no Redis")
    except Exception as exc:
        log.error("metrics_ws_failed", error_type=type(exc).__name__)
    for name, metric, label, help_text in (
        ("screens_by_status", "rd_screens_total", "status", "Telas por status"),
        ("campaigns_by_status", "rd_campaigns_total", "status", "Campanhas por status"),
        ("devices_by_presence", "rd_devices_active_total", "presence", "Dispositivos ativos por presenca"),
        ("pop_financial_pending", "rd_pop_financial_pending", None, "Eventos PoP validos aguardando apropriacao"),
    ):
        try:
            rows = await asyncio.wait_for(_cached_rows(name), DOMAIN_TIMEOUT_S)
            for k, v in rows:
                w.metric(metric, v, help_text=help_text, labels={label: k} if label else None)
        except Exception as exc:
            log.error("metrics_cache_failed", metric=name, error_type=type(exc).__name__)
    # ─── Segurança da instância (AÇÃO 003): snapshot do beat/startup; sob demanda só se sumir (trava NX) ───
    try:
        from app.modules.security import metrics as sec_metrics

        snap = await asyncio.wait_for(sec_metrics.read_snapshot(), DOMAIN_TIMEOUT_S)
        if snap:
            sec_metrics.write_metrics(w, snap)
    except Exception as exc:
        log.error("metrics_security_failed", error_type=type(exc).__name__)
    # ─── Celery (W-INFRA, P002 §54): heartbeat, filas, falhas/retries — O(1) por scrape ───
    try:
        from app.workers.observability import collect_celery_metrics

        c = await asyncio.wait_for(collect_celery_metrics(r, timeout_s=REDIS_TIMEOUT_S), 3 * REDIS_TIMEOUT_S)
        w.metric("rd_worker_last_heartbeat_timestamp_seconds", f"{c['worker_heartbeat_ts']:.3f}",
                 help_text="Ultima execucao da task de heartbeat por um worker Celery (unix s; 0 = nunca)")
        w.metric("rd_beat_last_heartbeat_publish_timestamp_seconds", f"{c['beat_publish_ts']:.3f}",
                 help_text="Ultima publicacao da task de heartbeat pelo beat (unix s; 0 = nunca)")
        if c["queues"] is not None:
            for q, n in sorted(c["queues"].items()):
                w.metric("rd_celery_queue_length", n, help_text="Mensagens aguardando na fila do broker",
                         labels={"queue": q})
        w.metric("rd_celery_broker_up", 0 if c["queues"] is None else 1,
                 help_text="1 se o LLEN das filas no broker funcionou neste scrape")
        for task, n in sorted(c["failures"].items()):
            w.metric("rd_celery_task_failures_total", n, help_text="Falhas de task Celery (sinal task_failure)",
                     typ="counter", labels={"task": task})
        for task, n in sorted(c["retries"].items()):
            w.metric("rd_celery_task_retries_total", n, help_text="Retries de task Celery (sinal task_retry)",
                     typ="counter", labels={"task": task})
    except Exception as exc:
        log.error("metrics_celery_failed", error_type=type(exc).__name__)
    return w.render()
