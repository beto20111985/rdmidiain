"""
RD Mídia Indoor — Observador da segurança da instância (AÇÃO 003; SPEC §7, §8). Fila ``default``.

``security_watch`` (beat a cada 15 min):
- ``verify_integrity`` — falha ⇒ log CRITICAL, auditoria ``security.integrity_check_failed`` (1×/hora) e
  notificação crítica ``security.integrity_alert`` aos titulares (dedupe diário);
- divergência de ENV (deste processo ou do relatório do último ``bootstrap``) ⇒ auditoria 1× por versão +
  aviso IN_APP;
- mudança da versão do modo desde a última observação ⇒ notificação ``security.approval_mode_changed``
  (mesmo dedupe da aplicação: nunca duplica) + ``security.status_changed``;
- snapshot dos gauges ``rd_security_*`` para o /metrics.
"""
from __future__ import annotations

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app
from app.workers.tasks.auth import run_async

logger = get_task_logger(__name__)


@celery_app.task(name="app.workers.tasks.security.security_watch", bind=True, max_retries=0)
def security_watch(self) -> dict:
    result = run_async(_security_watch())
    logger.info("security_watch integrity_ok=%s mode=%s version=%s", result.get("integrity_ok"), result.get("mode"),
                result.get("mode_version"))
    return result


async def _security_watch() -> dict:
    from app.core.database import worker_session
    from app.core.redis import get_redis, rkey
    from app.modules.security import approval_mode as am
    from app.modules.security import metrics as sec_metrics
    from app.modules.security import notifications as sec_notifications
    from app.modules.security.bootstrap import env_divergence_local, read_bootstrap_env_report, record_env_divergence

    async with worker_session() as db:
        report = await am.verify_integrity(db)
        state = await am.current_state(db)
        if not report.ok:
            am.log.critical("security_integrity_failed", origin="beat", problems=len(report.problems))
            await sec_notifications.record_integrity_failure(report, origin="beat", db=db)
        if state is not None:
            local = env_divergence_local(state)
            env_value = local["env_value"] if local["divergence"] else None
            if env_value is None:
                rep = await read_bootstrap_env_report()
                if rep and rep.get("divergence") and int(rep.get("mode_version") or 0) == state.version:
                    env_value = rep.get("env_value")
            if env_value:
                await record_env_divergence(db, state, env_value=str(env_value))
            try:
                r = await get_redis()
                key = rkey("security", "observed_mode_version")
                seen = await r.get(key)
                if seen is not None and int(seen) != state.version:
                    rows = await am.history_rows(db)
                    prev = rows[-2].to_mode if len(rows) >= 2 else state.mode
                    am.log.warning("approval_mode_change_observed", mode_version=state.version, mode=state.mode)
                    await sec_notifications.notify_mode_changed(db, from_mode=prev, to_mode=state.mode,
                                                                version=state.version)
                    await sec_notifications.status_changed_for_masters(db, reason="approval_mode_changed")
                await r.set(key, str(state.version))
            except Exception as exc:  # observador best-effort; métricas/alertas continuam
                am.log.warning("approval_mode_observer_failed", error_type=type(exc).__name__)
        snap = await sec_metrics.compute_snapshot(db, integrity=report)
    await sec_metrics.store_snapshot(snap)
    try:
        from app.modules.notifications.service import drain

        await drain()
    except Exception:  # pragma: no cover
        pass
    return snap
