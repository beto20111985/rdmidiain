"""
RD Mídia Indoor — Celery Application.

Workers NUNCA dependem de memória do processo da API: comunicação com a API/WS é
por banco (``app.core.database.worker_session``) e Redis Pub/Sub
(``app.core.events.publish_sync`` / ``publish``). Modelo de task:
``app/workers/tasks/auth.py``.

FILAS: ``QUEUES`` abaixo é a lista EXATA que o worker deve consumir
(``celery -A app.workers.celery_app worker -Q default,media,notifications,financial,reports``).
Ao criar fila nova, inclua aqui E avise o responsável pelo docker-compose.

BEAT: organizado por seções de domínio — cada agente adiciona entradas na sua seção.
"""
from celery import Celery
from celery.utils.log import get_task_logger

from app.core.config import settings

logger = get_task_logger(__name__)

QUEUES: tuple[str, ...] = ("default", "media", "notifications", "financial", "reports")

# Beat / RedBeat (BUG-QA-03): lock curto, renovado a cada loop; lock ≥ 3 × loop.
BEAT_MAX_LOOP_INTERVAL_S = 30
REDBEAT_LOCK_TIMEOUT_S = 90

celery_app = Celery(
    "rdmidiaindoor",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        # ─── FUNDAÇÃO (W1) ───
        "app.workers.tasks.auth",
        # ─── DOMÍNIOS ───
        "app.workers.tasks.media",
        "app.workers.tasks.notifications",
        "app.workers.tasks.financial",
        "app.workers.tasks.scheduler",
        "app.workers.tasks.reports",
        "app.workers.tasks.maintenance",
        "app.workers.tasks.playback",  # W-PLAY
        "app.workers.tasks.campaigns",  # W-CAMP
        "app.workers.tasks.observability",  # W-INFRA (heartbeat do worker)
        "app.workers.tasks.security",  # AÇÃO 003 (observador da segurança da instância)
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_queues={q: {"exchange": q, "routing_key": q} for q in QUEUES},
    task_default_queue="default",
    task_default_exchange="default",
    task_default_routing_key="default",
    task_routes={
        "app.workers.tasks.auth.*": {"queue": "default"},
        "app.workers.tasks.media.*": {"queue": "media"},
        "app.workers.tasks.notifications.*": {"queue": "notifications"},
        "app.workers.tasks.financial.*": {"queue": "financial"},
        "app.workers.tasks.reports.*": {"queue": "reports"},
        "app.workers.tasks.scheduler.*": {"queue": "default"},
        "app.workers.tasks.maintenance.*": {"queue": "default"},
        "app.workers.tasks.playback.*": {"queue": "default"},  # W-PLAY
        "app.workers.tasks.campaigns.*": {"queue": "default"},  # W-CAMP
        "app.workers.tasks.observability.*": {"queue": "default"},  # W-INFRA
        "app.workers.tasks.security.*": {"queue": "default"},  # AÇÃO 003
    },
    task_time_limit=3600,
    task_soft_time_limit=3300,
    worker_max_tasks_per_child=100,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Com acks_late, mensagens pré-buscadas por um worker que morre ficariam presas até o
    # visibility_timeout do broker (1 h): busca uma por vez (BUG-QA-01 / W-MEDIA).
    worker_prefetch_multiplier=1,
    redbeat_redis_url=settings.REDIS_URL,
    # BUG-QA-03 (W-INFRA): o lock do RedBeat é renovado a cada loop do beat (tick) e,
    # com o default (5 × 300 s = 1500 s), um beat morto deixava o lock persistido (AOF)
    # e o beat novo esperava até 25 min sem agendar nada. Loop ≤ 30 s e lock 90 s
    # (3 renovações de folga): após queda, um beat novo assume em ≤ ~2 min
    # (expiração do lock + 1 tentativa, pois o RedBeat re-tenta a cada max_interval).
    beat_max_loop_interval=BEAT_MAX_LOOP_INTERVAL_S,
    redbeat_lock_timeout=REDBEAT_LOCK_TIMEOUT_S,
    result_expires=86400,
)


# ─── FAIL-FAST DE CONFIGURAÇÃO (worker e beat; fora de development/test) ─────
from celery.signals import beat_init, worker_init  # noqa: E402


def _enforce_startup_config(process: str) -> None:
    from app.core.startup_checks import StartupConfigError, enforce_runtime_config

    try:
        enforce_runtime_config(process)
    except StartupConfigError as exc:
        logger.critical(str(exc))
        # SystemExit atravessa o despacho de sinais do Celery (que só loga Exception)
        raise SystemExit(f"[{process}] {exc}") from None


@worker_init.connect
def _check_config_worker(**_kw):
    _enforce_startup_config("worker")
    from app.core.error_tracking import init_error_tracking

    init_error_tracking("worker")  # no-op sem SENTRY_DSN


@beat_init.connect
def _check_config_beat(**_kw):
    _enforce_startup_config("beat")
    from app.core.error_tracking import init_error_tracking

    init_error_tracking("beat")  # no-op sem SENTRY_DSN


# ─── OBSERVABILIDADE (W-INFRA, P002 §54) ─────────────────────────────────────
# Sinais task_failure/task_retry (contadores por task) e before_task_publish (heartbeat
# do beat) → Redis; lidos pelo /metrics da API. Ver app/workers/observability.py.
from app.workers.observability import connect_signals as _connect_obs_signals  # noqa: E402

_connect_obs_signals()


# ─── BEAT SCHEDULE (por domínio) ─────────────────────────────────────────────
celery_app.conf.beat_schedule = {
    # ─── OBSERVABILIDADE (W-INFRA) ───
    "observability-worker-heartbeat": {
        "task": "app.workers.tasks.observability.worker_heartbeat",
        "schedule": 60,
        "options": {"queue": "default", "expires": 55},
    },
    # ─── SEGURANÇA DA INSTÂNCIA (AÇÃO 003): integridade, divergência de ENV, observador do modo ───
    "security-watch": {
        "task": "app.workers.tasks.security.security_watch",
        "schedule": 900,
        "options": {"queue": "default", "expires": 840},
    },
    # ─── FUNDAÇÃO / AUTH (W1) ───
    "purge-auth-artifacts": {
        "task": "app.workers.tasks.auth.purge_auth_artifacts",
        "schedule": 3600,
        "options": {"queue": "default"},
    },
    # ─── INVENTÁRIO / SCHEDULER (W-INV) ───
    "inventory-sweep-holds": {
        "task": "app.workers.tasks.scheduler.sweep_inventory_holds",
        "schedule": 60,
        "options": {"queue": "default"},
    },
    "inventory-regenerate-stale-manifests": {
        "task": "app.workers.tasks.scheduler.regenerate_stale_manifests",
        "schedule": 900,
        "options": {"queue": "default"},
    },
    "inventory-prune-manifest-versions": {
        "task": "app.workers.tasks.scheduler.prune_manifest_versions",
        "schedule": 86400,
        "options": {"queue": "default"},
    },
    # (vencimento de campanhas: agendado pelo W-CAMP — sem duplicidade aqui)
    # ─── CAMPANHAS / PEDIDOS (W-CAMP) ───
    "campaigns-finalize-expired": {
        "task": "app.workers.tasks.campaigns.finalize_expired_campaigns",
        "schedule": 900,
        "options": {"queue": "default"},
    },
    "campaigns-expire-unpaid-orders": {
        "task": "app.workers.tasks.campaigns.expire_unpaid_orders",
        "schedule": 120,
        "options": {"queue": "default"},
    },
    "campaigns-generate-auto-renewals": {
        "task": "app.workers.tasks.campaigns.generate_auto_renewals",
        "schedule": 21600,
        "options": {"queue": "default"},
    },
    # ─── MÍDIA (W-MEDIA) ───
    "media-maintenance": {  # BUG-QA-01: recupera mídia de worker caído em poucos minutos
        "task": "app.workers.tasks.media.media_maintenance",
        "schedule": 60,
        "options": {"queue": "default", "expires": 55},
    },
    "media-gc": {
        "task": "app.workers.tasks.media.media_gc",
        "schedule": 3600,
        "options": {"queue": "media"},
    },
    # ─── PLAYBACK / PROOF-OF-PLAY / PRESENÇA (W-PLAY) ───
    "player-presence-sweep": {
        "task": "app.workers.tasks.playback.presence_sweep",
        "schedule": 60,
        "options": {"queue": "default"},
    },
    "player-presence-flush": {
        "task": "app.workers.tasks.playback.presence_flush",
        "schedule": 300,
        "options": {"queue": "default"},
    },
    "playback-aggregate": {
        "task": "app.workers.tasks.playback.aggregate_playback",
        "schedule": 300,
        "options": {"queue": "default"},
    },
    "playback-deliver-financial": {
        "task": "app.workers.tasks.playback.deliver_financial",
        "schedule": 60,
        "options": {"queue": "default"},
    },
    "player-housekeeping": {
        "task": "app.workers.tasks.playback.housekeeping",
        "schedule": 300,
        "options": {"queue": "default"},
    },
    "playback-purge": {
        "task": "app.workers.tasks.playback.purge_playback",
        "schedule": 86400,
        "options": {"queue": "default"},
    },
    # ─── FINANCEIRO ───
    # (tarefas idempotentes e cientes de data/fuso; "diário" = executa e só age quando devido)
    "financial-pending-webhooks": {
        "task": "app.workers.tasks.financial.process_pending_webhooks",
        "schedule": 60,
        "options": {"queue": "financial"},
    },
    "financial-recognize-revenue": {
        "task": "app.workers.tasks.financial.recognize_revenue_daily",
        "schedule": 3600,
        "options": {"queue": "financial"},
    },
    "financial-accrue-commission": {
        "task": "app.workers.tasks.financial.accrue_commission_daily",
        "schedule": 3600,
        "options": {"queue": "financial"},
    },
    "financial-close-commissions": {
        "task": "app.workers.tasks.financial.close_commissions",
        "schedule": 900,
        "options": {"queue": "financial"},
    },
    "financial-refresh-withdrawals": {
        "task": "app.workers.tasks.financial.refresh_withdrawals",
        "schedule": 300,
        "options": {"queue": "financial"},
    },
    "financial-sweep": {
        "task": "app.workers.tasks.financial.sweep_financial",
        "schedule": 300,
        "options": {"queue": "financial"},
    },
    "financial-reconcile": {
        "task": "app.workers.tasks.financial.reconcile_daily",
        "schedule": 86400,
        "options": {"queue": "financial"},
    },
    # ─── NOTIFICAÇÕES (W-BIZ) ───
    "notifications-retry-pending": {
        "task": "app.workers.tasks.notifications.retry_pending_deliveries",
        "schedule": 120,
        "options": {"queue": "notifications"},
    },
    "notifications-purge-old": {
        "task": "app.workers.tasks.notifications.purge_old_notifications",
        "schedule": 86400,
        "options": {"queue": "notifications"},
    },
    # ─── RELATÓRIOS / MANUTENÇÃO (W-BIZ) ───
    # (stubs legados "daily-backup-check"/"check-offline-players" removidos: backup = W-INFRA;
    #  telas offline = W-PLAY presence_sweep/offline_alerts)
    "reports-monthly": {
        "task": "app.workers.tasks.reports.monthly_reports",
        "schedule": 3600,
        "options": {"queue": "reports"},
    },
    "reports-purge-exports": {
        "task": "app.workers.tasks.reports.purge_expired_exports",
        "schedule": 86400,
        "options": {"queue": "reports"},
    },
    "crm-expire-proposals": {
        "task": "app.workers.tasks.maintenance.expire_crm_proposals",
        "schedule": 3600,
        "options": {"queue": "default"},
    },
    "control-plane-advance-rollouts": {
        "task": "app.workers.tasks.maintenance.advance_rollouts",
        "schedule": 300,
        "options": {"queue": "default"},
    },
}

# ─── AÇÃO 003 — D1: re-selagem automática da credencial dos titulares de assento (todo flush do ORM) ───
from app.modules.security.credentials import register_listeners as _register_credential_listeners  # noqa: E402

_register_credential_listeners()
