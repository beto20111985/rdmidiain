"""RD Mídia Indoor — Celery: manutenção geral dos domínios do W-BIZ (AÇÃO 002).

Substitui os stubs legados ``check_backup`` / ``check_offline_players`` /
``cleanup_expired_sessions`` (só registravam log — sucesso falso):
- backup é do W-INFRA (contêiner ``rd-db-backup`` com healthcheck próprio);
- telas offline: W-PLAY ``presence_sweep``/``offline_alerts`` (notificam via ``notify``);
- sessões expiradas: W1 ``purge_auth_artifacts``.

Tasks:
- ``expire_crm_proposals`` (diário): propostas ENVIADA/ACEITA vencidas → EXPIRADA.
- ``advance_rollouts`` (5 min): continua rollouts do control plane EM_ANDAMENTO
  (respeitando ``max_concurrency``); executor nulo/simulado — nunca deploy real.
"""
from __future__ import annotations

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app
from app.workers.tasks.auth import run_async

# registra callbacks de pagamento do designer no processo do worker (webhooks do financeiro)
import app.modules.designer.service  # noqa: E402,F401

logger = get_task_logger(__name__)


@celery_app.task(name="app.workers.tasks.maintenance.expire_crm_proposals", bind=True)
def expire_crm_proposals(self) -> dict:
    return run_async(_expire())


async def _expire() -> dict:
    from app.core.database import worker_session
    from app.modules.crm.service import expire_due

    async with worker_session() as db:
        n = await expire_due(db)
    return {"expired": n}


@celery_app.task(name="app.workers.tasks.maintenance.advance_rollouts", bind=True)
def advance_rollouts(self) -> dict:
    return run_async(_advance())


async def _advance() -> dict:
    from sqlalchemy import select

    from app.core.database import worker_session
    from app.models.control_plane import CpRollout
    from app.modules.control_plane.service import dispatch
    from app.modules.notifications.service import drain

    started = 0
    async with worker_session() as db:
        ids = list((await db.execute(select(CpRollout.id).where(CpRollout.status == "EM_ANDAMENTO"))).scalars())
        for rid in ids:
            started += len(await dispatch(db, None, rid))
    await drain()
    return {"rollouts": len(ids), "started": started}
