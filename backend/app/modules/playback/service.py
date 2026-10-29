"""
RD Mídia Indoor — Interface pública do domínio de playback (W-PLAY) para outros domínios.

- ``last_valid_play_at(db, campaign_id)`` → instante (UTC) da última exibição VÁLIDA da
  campanha (W-CAMP deriva o estado REPRODUZINDO). Barata: índice
  ``(campaign_id, started_at)`` nos brutos + agregado diário (após a retenção).
- ``revoked_creative_ids(db, screen_id)`` → kills ativos da tela (W-INV, ``manifest.revoked``).
- ``ingest_batch`` / ``deliver_pending_financial`` (ingestão de PoP e entrega ao W-FIN).
- ``sync_report(db, *, screen_ids, org_id, start, end, group_by, tz)`` → relatório de
  sincronizações/downloads (contagens por tipo, tempo até aplicar a versão, falhas por mídia).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.playback import PlaybackDailyStat, PlaybackEvent
from app.modules.playback.ingest import deliver_pending_financial, ingest_batch  # noqa: F401
from app.modules.playback.kills import revoked_creative_ids  # noqa: F401
from app.modules.playback.syncevents import sync_report  # noqa: F401


async def last_valid_play_at(db: AsyncSession, campaign_id: uuid.UUID) -> Optional[datetime]:
    raw = (
        await db.execute(
            select(PlaybackEvent.started_at)
            .where(PlaybackEvent.campaign_id == campaign_id, PlaybackEvent.valid.is_(True))
            .order_by(PlaybackEvent.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    agg = (
        await db.execute(
            select(func.max(PlaybackDailyStat.last_valid_started_at)).where(PlaybackDailyStat.campaign_id == campaign_id)
        )
    ).scalar_one_or_none()
    cands = [x for x in (raw, agg) if x is not None]
    return max(cands) if cands else None
