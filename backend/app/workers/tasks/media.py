"""
RD Mídia Indoor — Tasks Celery de mídia (fila ``media``). Dono: W-MEDIA (AÇÃO 002).

- ``process_media(media_id)``: pipeline completo (``app.modules.media.pipeline``) —
  validação técnica → transcode/normalização → validação final → preview → READY |
  REJECTED | FAILED. Idempotente (READY/REJECTED → no-op; lease impede execução
  concorrente). Falha transitória → retry controlado com backoff (até
  ``MEDIA_MAX_ATTEMPTS``); temporários sempre limpos.
- ``media_maintenance()`` (beat a cada 60 s, fila ``default`` — não fica preso atrás de
  transcodificações longas na fila ``media``): recupera mídia de worker que caiu/reiniciou
  (lease curto vencido → reenfileira UMA vez com backoff; última tentativa → FAILED
  ``MEDIA_WORKER_LOST``), reenfileira UPLOADED parada (broker fora) e limpa temporários.
- ``media_gc()``: remove do storage arquivos de mídias excluídas (soft delete) após o
  período de carência, SOMENTE se nenhuma referência existir (reconsultada).

NUNCA transcodificar dentro da request HTTP (P001 §28; PU §66).
"""
from __future__ import annotations

import uuid

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app
from app.workers.tasks.auth import run_async

logger = get_task_logger(__name__)


@celery_app.task(
    name="app.workers.tasks.media.process_media",
    bind=True,
    queue="media",
    acks_late=True,
    max_retries=5,
    soft_time_limit=3300,
    time_limit=3500,
)
def process_media(self, media_id: str) -> dict:
    from app.modules.media.pipeline import process_media as run
    from app.modules.media.policy import media_settings

    outcome = run_async(run(uuid.UUID(str(media_id))))
    logger.info("process_media %s", outcome.as_dict())
    if outcome.retry and not getattr(self.request, "is_eager", False):
        countdown = media_settings().RETRY_BASE_S * (2 ** max(0, outcome.attempts - 1))
        raise self.retry(countdown=countdown)
    return outcome.as_dict()


@celery_app.task(name="app.workers.tasks.media.media_maintenance", bind=True, queue="default",
                 soft_time_limit=120, time_limit=180)
def media_maintenance(self) -> dict:
    from app.modules.media.pipeline import sweep_storage_staging
    from app.modules.media.service import requeue_stale

    result = run_async(requeue_stale())
    result["staging_removed"] = sweep_storage_staging()
    logger.info("media_maintenance %s", result)
    return result


@celery_app.task(name="app.workers.tasks.media.media_gc", bind=True, queue="media")
def media_gc(self) -> dict:
    from app.modules.media.service import gc_deleted_media

    result = run_async(gc_deleted_media())
    logger.info("media_gc %s", result)
    return result
