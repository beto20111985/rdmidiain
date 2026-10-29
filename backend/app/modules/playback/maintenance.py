"""
RD Mídia Indoor — Jobs controlados do domínio de players (agregação, retenção, alertas).

- ``aggregate_pending``: agrega eventos brutos ainda não agregados em
  ``playback_daily_stats`` (tela, criativo, campanha, dia local, kind) — numa única
  instrução (CTE com ``FOR UPDATE SKIP LOCKED`` + ``ON CONFLICT DO UPDATE``): sem
  contagem dupla com execuções concorrentes.
- ``purge_raw_events``: apaga brutos com mais de ``proof_of_play.raw_retention_days``
  SOMENTE se já agregados E apropriados financeiramente (ou não aplicáveis). Eventos
  ``PENDING`` nunca são apagados.
- ``purge_telemetry``: retenção de telemetria (``player.telemetry_retention_days``).
- ``expire_pairing_requests``: marca pedidos vencidos e remove os antigos.
- ``offline_alerts``: notifica telas offline (≥ 10 min; e marcos ``player.offline_alert_days``).
"""
from __future__ import annotations

from datetime import timedelta

import structlog
from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingDef, get_setting, register
from app.core.timeutils import utcnow
from app.models.devices import Device, DeviceStatus, DeviceTelemetry, PairingRequest, PairingStatus

log = structlog.get_logger("rd.playback.maintenance")

register(
    SettingDef(
        "player.telemetry_retention_days",
        30,
        "int",
        "Retenção das amostras de telemetria dos players (dias).",
        min=1,
        max=365,
        category="players",
    )
)

# Limite do alerta de offline: configuração ``player.offline_alert_after_min`` (default 10 min,
# app.modules.playback.config) — lida a cada execução do job (cache curto por processo).

_AGGREGATE_SQL = text(
    """
WITH batch AS (
    SELECT playback_uuid FROM playback_events
    WHERE aggregated_at IS NULL
    ORDER BY received_at
    LIMIT :lim
    FOR UPDATE SKIP LOCKED
), marked AS (
    UPDATE playback_events e SET aggregated_at = now()
    FROM batch b WHERE e.playback_uuid = b.playback_uuid
    RETURNING e.screen_id, e.creative_id, e.creative_code, e.campaign_id, e.kind, e.local_day,
              e.establishment_id, e.org_id, e.status, e.valid, e.actual_ms, e.started_at
), agg AS (
    SELECT screen_id, creative_id, max(creative_code) AS creative_code, campaign_id, kind, local_day,
           (max(establishment_id::text))::uuid AS establishment_id, (max(org_id::text))::uuid AS org_id,
           count(*) FILTER (WHERE status = 'ACCEPTED') AS acc,
           count(*) FILTER (WHERE status = 'ACCEPTED' AND valid) AS val,
           count(*) FILTER (WHERE status = 'ACCEPTED' AND NOT valid) AS inv,
           count(*) FILTER (WHERE status = 'REJECTED') AS rej,
           COALESCE(sum(actual_ms) FILTER (WHERE status = 'ACCEPTED' AND valid), 0) AS vms,
           COALESCE(sum(actual_ms) FILTER (WHERE status = 'ACCEPTED'), 0) AS ams,
           min(started_at) AS f, max(started_at) AS l,
           max(started_at) FILTER (WHERE status = 'ACCEPTED' AND valid) AS lv
    FROM marked
    GROUP BY screen_id, creative_id, campaign_id, kind, local_day
), ins AS (
    INSERT INTO playback_daily_stats AS s (
        id, screen_id, creative_id, creative_code, campaign_id, kind, local_day, establishment_id, org_id,
        plays_accepted, plays_valid, plays_invalid, plays_rejected, valid_ms, accepted_ms,
        first_started_at, last_started_at, last_valid_started_at, updated_at)
    SELECT gen_random_uuid(), screen_id, creative_id, creative_code, campaign_id, kind, local_day, establishment_id, org_id,
           acc, val, inv, rej, vms, ams, f, l, lv, now()
    FROM agg
    ON CONFLICT (screen_id, creative_id, (COALESCE(campaign_id, '00000000-0000-0000-0000-000000000000'::uuid)), local_day, kind)
    DO UPDATE SET
        plays_accepted = s.plays_accepted + EXCLUDED.plays_accepted,
        plays_valid = s.plays_valid + EXCLUDED.plays_valid,
        plays_invalid = s.plays_invalid + EXCLUDED.plays_invalid,
        plays_rejected = s.plays_rejected + EXCLUDED.plays_rejected,
        valid_ms = s.valid_ms + EXCLUDED.valid_ms,
        accepted_ms = s.accepted_ms + EXCLUDED.accepted_ms,
        first_started_at = LEAST(s.first_started_at, EXCLUDED.first_started_at),
        last_started_at = GREATEST(s.last_started_at, EXCLUDED.last_started_at),
        last_valid_started_at = GREATEST(s.last_valid_started_at, EXCLUDED.last_valid_started_at),
        establishment_id = COALESCE(s.establishment_id, EXCLUDED.establishment_id),
        org_id = COALESCE(s.org_id, EXCLUDED.org_id),
        updated_at = now()
    RETURNING 1
)
SELECT (SELECT count(*) FROM marked) AS events, (SELECT count(*) FROM ins) AS groups
"""
)


async def aggregate_pending(db: AsyncSession, limit: int = 5000) -> dict:
    row = (await db.execute(_AGGREGATE_SQL, {"lim": int(limit)})).one()
    return {"events": int(row.events), "groups": int(row.groups)}


async def purge_raw_events(db: AsyncSession, *, now=None, batch: int = 5000) -> int:
    from app.models.playback import FinancialStatus, PlaybackEvent

    days = int(await get_setting(db, "proof_of_play.raw_retention_days"))
    cutoff = (now or utcnow()) - timedelta(days=days)
    eligible = (
        select(PlaybackEvent.playback_uuid)
        .where(
            PlaybackEvent.aggregated_at.is_not(None),
            or_(
                and_(
                    PlaybackEvent.financial_status == FinancialStatus.NOT_APPLICABLE.value,
                    PlaybackEvent.received_at < cutoff,
                    PlaybackEvent.started_at < cutoff,
                ),
                and_(
                    PlaybackEvent.financial_status == FinancialStatus.APPROPRIATED.value,
                    PlaybackEvent.financial_appropriated_at < cutoff,
                ),
            ),
        )
        .limit(batch)
        .scalar_subquery()
    )
    res = await db.execute(delete(PlaybackEvent).where(PlaybackEvent.playback_uuid.in_(eligible)))
    return res.rowcount or 0


async def purge_telemetry(db: AsyncSession, *, now=None) -> int:
    days = int(await get_setting(db, "player.telemetry_retention_days"))
    cutoff = (now or utcnow()) - timedelta(days=days)
    res = await db.execute(delete(DeviceTelemetry).where(DeviceTelemetry.recorded_at < cutoff))
    return res.rowcount or 0


async def expire_pairing_requests(db: AsyncSession, *, now=None) -> dict:
    now = now or utcnow()
    exp = await db.execute(
        update(PairingRequest)
        .where(PairingRequest.status == PairingStatus.PENDING.value, PairingRequest.expires_at <= now)
        .values(status=PairingStatus.EXPIRED.value)
    )
    old = await db.execute(
        delete(PairingRequest).where(
            PairingRequest.status == PairingStatus.EXPIRED.value, PairingRequest.expires_at < now - timedelta(days=7)
        )
    )
    return {"expired": exp.rowcount or 0, "deleted": old.rowcount or 0}


async def offline_alerts(db: AsyncSession, *, now=None) -> int:
    """Notifica telas offline (≥ ``player.offline_alert_after_min``, default 10 min) e os marcos de
    ``player.offline_alert_days``.

    Deduplicação: chave por (tela, marco, episódio de offline = last_seen_at) + cooldown
    do tipo ``screen.offline`` (W-BIZ). Sem o serviço de notificações, não faz nada.
    """
    try:
        from app.modules.notifications.service import EstablishmentRecipients, StaffRecipients, notify
    except ImportError:
        return 0
    from app.modules.playback.integration import screen_info

    from app.modules.playback import config as pcfg

    now = now or utcnow()
    after = timedelta(minutes=await pcfg.offline_alert_after_min(db))
    days_marks = sorted(int(x) for x in (await get_setting(db, "player.offline_alert_days")))
    rows = (
        await db.execute(
            select(Device).where(
                Device.status == DeviceStatus.ACTIVE.value,
                Device.presence == "offline",
                or_(
                    Device.last_seen_at < now - after,
                    and_(Device.last_seen_at.is_(None), Device.paired_at < now - after),
                ),
            )
        )
    ).scalars().all()
    sent = 0
    for d in rows:
        since = d.last_seen_at or d.paired_at
        days = (now - since).days
        mark = max([m for m in days_marks if days >= m], default=0)
        info = await screen_info(db, d.screen_id)
        if info is None:
            continue
        from app.models.screens import Screen as _Screen  # W-BIZ: nome cadastrado da tela

        data = {
            "screen_code": info.code,
            "screen_name": (await db.execute(select(_Screen.name).where(_Screen.id == d.screen_id))).scalar() or info.code,
            "offline_since": since,  # datetime cru; o W-BIZ formata no fuso do estabelecimento
            "days_offline": days,
            "screen_id": str(d.screen_id),
        }
        key = f"screen.offline:{d.screen_id}:{mark}:{int(since.timestamp())}"
        try:
            res = await notify(
                db,
                [EstablishmentRecipients(info.establishment_id, "screens.view"), StaffRecipients("screens.manage", info.city_key)],
                "screen.offline",
                data,
                dedupe_key=key[:255],
                entity=("screen", d.screen_id),
                org_id=info.org_id,
                tz=info.timezone,
            )
            sent += len(getattr(res, "created", None) or [])
        except Exception as exc:
            log.error("offline_alert_failed", error_type=type(exc).__name__)
    return sent


OUTDATED_AFTER = timedelta(hours=1)

_OUTDATED_SQL = text(
    """
SELECT d.id, d.screen_id, l.version, l.generated_at
  FROM devices d
  JOIN LATERAL (SELECT version, generated_at FROM manifest_versions mv
                 WHERE mv.screen_id = d.screen_id ORDER BY version DESC LIMIT 1) l ON true
 WHERE d.status = 'ACTIVE' AND d.presence = 'online'
   AND (d.applied_manifest_version IS NULL OR d.applied_manifest_version < l.version)
   AND l.generated_at < :cutoff
 LIMIT 1000
"""
)


async def outdated_alerts(db: AsyncSession, *, now=None) -> int:
    """Tela ONLINE que não aplicou a versão vigente do manifesto há mais de 1 h → ``screen.outdated``
    (dedupe por tela+versão; cooldown do tipo no W-BIZ). Dashboard: ``rd_devices_active_total``/presença."""
    try:
        from app.modules.notifications.service import EstablishmentRecipients, StaffRecipients, notify
    except ImportError:
        return 0
    from app.models.screens import Screen as _Screen  # W-BIZ: nome cadastrado da tela
    from app.modules.playback.integration import screen_info

    now = now or utcnow()
    try:
        rows = (await db.execute(_OUTDATED_SQL, {"cutoff": now - OUTDATED_AFTER})).all()
    except Exception as exc:  # tabela do W-INV ausente
        log.error("outdated_query_failed", error_type=type(exc).__name__)
        return 0
    sent = 0
    for _dev, screen_id, version, generated_at in rows:
        info = await screen_info(db, screen_id)
        if info is None:
            continue
        try:
            res = await notify(
                db,
                [EstablishmentRecipients(info.establishment_id, "screens.view"), StaffRecipients("screens.manage", info.city_key)],
                "screen.outdated",
                {"screen_code": info.code,
                 "screen_name": (await db.execute(select(_Screen.name).where(_Screen.id == screen_id))).scalar()
                 or info.code,
                 "since": generated_at,  # datetime cru; o W-BIZ formata no fuso do estabelecimento
                 "screen_id": str(screen_id), "version": int(version)},
                dedupe_key=f"screen.outdated:{screen_id}:{int(version)}",
                entity=("screen", screen_id),
                org_id=info.org_id,
                tz=info.timezone,
            )
            sent += len(getattr(res, "created", None) or [])
        except Exception as exc:
            log.error("outdated_alert_failed", error_type=type(exc).__name__)
    return sent
