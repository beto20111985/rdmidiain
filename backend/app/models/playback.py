"""
RD Mídia Indoor — Prova de exibição (PoP, RDPP/1 §7) e agregados diários (W-PLAY).

``PlaybackEvent`` — evento BRUTO recebido do player (PK = ``playback_uuid`` gerado no
dispositivo; ingestão idempotente por ``INSERT … ON CONFLICT DO NOTHING``):
- ``status``: ACCEPTED (autêntico: assinatura ES256 válida do dispositivo autenticado)
  | REJECTED (assinatura inválida / outro dispositivo). Eventos malformados não são
  gravados.
- ``valid``: válido para comissão somente se TODAS as regras do §7 passaram
  (``reason`` = primeira regra violada; ``reasons`` = todas; ``flags`` = anomalias
  sinalizadas que não invalidam sozinhas: SEQ_GAP, CHAIN_BREAK, ...).
- ``financial_status``: NOT_APPLICABLE | PENDING (válido EXTERNAL aguardando o
  financeiro) | APPROPRIATED (entregue a ``financial.accrue_commission_for_events``).
- Retenção: brutos com mais de ``proof_of_play.raw_retention_days`` são apagados
  SOMENTE depois de agregados (``aggregated_at``) e apropriados (ou não aplicáveis).

Particionamento: a tabela é particionável por mês em ``started_at`` (índice
``(screen_id, started_at)``); particionar de fato exige PK ``(playback_uuid,
started_at)`` + tabela de unicidade de ``playback_uuid`` — decisão registrada no
relatório W-PLAY para a migration de escala.

``PlaybackDailyStat`` — agregados preservados por (tela, criativo, campanha, dia local, kind).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ZERO_UUID = uuid.UUID(int=0)


class PlaybackResult(str, enum.Enum):
    """``result`` do evento PoP (RDPP §7)."""

    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class PopStatus(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"  # somente na resposta (nunca gravado)
    REJECTED = "REJECTED"


class FinancialStatus(str, enum.Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    APPROPRIATED = "APPROPRIATED"


class PlaybackEvent(Base):
    __tablename__ = "playback_events"
    __table_args__ = (
        Index("ix_playback_events_screen_started", "screen_id", "started_at"),
        Index("ix_playback_events_device_started_ms", "device_id", "started_at_ms"),
        Index("ix_playback_events_screen_started_ms", "screen_id", "started_at_ms"),
        Index("ix_playback_events_device_nonce", "device_id", "nonce"),
        Index("ix_playback_events_campaign_started", "campaign_id", "started_at"),
        Index(
            "ix_playback_events_fin_pending",
            "received_at",
            postgresql_where=text("financial_status = 'PENDING'"),
        ),
        Index(
            "ix_playback_events_not_aggregated",
            "received_at",
            postgresql_where=text("aggregated_at IS NULL"),
        ),
        Index("ix_playback_events_received_at", "received_at"),
        # W-DB (0004): BRIN em started_at (varreduras por período/retenção; sem partição física)
        Index("ix_playback_events_started_at_brin", "started_at", postgresql_using="brin"),
    )

    playback_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=False
    )
    # Estabelecimento/organização da tela NA DATA da exibição (W-ORG screen_establishment_at)
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Campos do evento (como assinados pelo dispositivo). Sem FK: um evento REJECTED
    # pode citar ids inexistentes e precisa ser guardado como evidência.
    creative_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    creative_code: Mapped[str] = mapped_column(String(16), nullable=False)
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    insertion_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    finished_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256(RDJ1(evento))
    clock_offset_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    offline: Mapped[bool] = mapped_column(Boolean, nullable=False)
    signature: Mapped[str] = mapped_column(String(100), nullable=False)
    claimed: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # device_id/screen_id alegados, se divergentes

    # Resultado da validação
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    flags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    local_day: Mapped[date] = mapped_column(Date, nullable=False)  # dia local (fuso do estabelecimento)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    aggregated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    financial_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FinancialStatus.NOT_APPLICABLE.value
    )
    financial_appropriated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    financial_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PlaybackDailyStat(Base):
    """Agregado diário preservado após a retenção dos brutos."""

    __tablename__ = "playback_daily_stats"
    __table_args__ = (
        Index(
            "uq_playback_daily_stats_key",
            "screen_id",
            "creative_id",
            text("COALESCE(campaign_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "local_day",
            "kind",
            unique=True,
        ),
        Index("ix_playback_daily_stats_campaign_day", "campaign_id", "local_day"),
        Index("ix_playback_daily_stats_screen_day", "screen_id", "local_day"),
        Index("ix_playback_daily_stats_org_day", "org_id", "local_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=False
    )
    creative_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    creative_code: Mapped[str] = mapped_column(String(16), nullable=False)
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    plays_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plays_valid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plays_invalid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plays_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    accepted_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    first_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_valid_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
