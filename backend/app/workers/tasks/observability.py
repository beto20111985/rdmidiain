"""RD Mídia Indoor — Celery: heartbeat do worker (observabilidade, W-INFRA / P002 §54).

``worker_heartbeat`` é agendada pelo beat a cada 60 s (fila ``default``, ``expires`` 55 s
para não acumular heartbeats velhos) e grava o timestamp no Redis da aplicação. O
``/metrics`` da API expõe ``rd_worker_last_heartbeat_timestamp_seconds``; o Prometheus
alerta se ficar mais de 3 min sem atualização (worker OU beat parados — o timestamp de
publicação do beat diferencia os dois casos).
"""
from __future__ import annotations

from app.workers.celery_app import celery_app
from app.workers.observability import HEARTBEAT_TASK, record_worker_heartbeat


@celery_app.task(name=HEARTBEAT_TASK, ignore_result=True)
def worker_heartbeat() -> dict:
    return {"ts": record_worker_heartbeat()}
