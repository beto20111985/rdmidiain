"""
RD Mídia Indoor — Dispositivos (players), pareamento, comandos, kill, telemetria e
versões do aplicativo (domínio W-PLAY; RDPP/1 §1–§4, §8).

Movido de ``app/models/screens.py`` na AÇÃO 002 (``Screen`` continua lá, do W-ORG).

Regras de identidade (ADR_007):
- o servidor guarda SOMENTE a chave pública ES256 (SPKI DER base64url) do dispositivo;
  nenhum segredo/token do dispositivo é persistido;
- no máximo UM dispositivo ATIVO por tela (índice único parcial);
- dispositivo revogado nunca volta a ser válido (re-pareamento = novo ``device_id``).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class DevicePlatform(str, enum.Enum):
    """Plataforma do player (valores do protocolo, minúsculos)."""

    WEB = "web"
    WINDOWS = "windows"
    ANDROID = "android"


class DeviceStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class PairingStatus(str, enum.Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    EXPIRED = "EXPIRED"


class PairingMethod(str, enum.Enum):
    PANEL = "PANEL"  # admin/técnico confirmou no painel (código/QR)
    DEVICE_LOGIN = "DEVICE_LOGIN"  # técnico fez login controlado no próprio dispositivo


class Device(Base):
    """Dispositivo físico que executa o player, vinculado a UMA tela."""

    __tablename__ = "devices"
    __table_args__ = (
        Index("ix_devices_screen_id", "screen_id"),
        Index("ix_devices_status", "status"),
        Index("ix_devices_last_seen_at", "last_seen_at"),
        # no máximo um dispositivo ATIVO por tela
        Index(
            "uq_devices_active_screen",
            "screen_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=False
    )

    # Identidade criptográfica (ES256). Somente a chave PÚBLICA.
    public_key: Mapped[str] = mapped_column(Text, nullable=False)  # SPKI DER base64url sem padding
    key_alg: Mapped[str] = mapped_column(String(10), nullable=False, default="ES256")
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)  # sha256 hex do SPKI

    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    app_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    os_version: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    device_info: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    update_group: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # grupo de rollout (ex.: "piloto")

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=DeviceStatus.ACTIVE.value)
    paired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pairing_method: Mapped[str] = mapped_column(String(20), nullable=False)
    paired_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    revoked_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Presença (o estado "quente" vive no Redis; o banco só recebe transições e flush coalescido)
    presence: Mapped[str] = mapped_column(String(20), nullable=False, default="offline")
    player_state: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_play_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_manifest_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pending_manifest_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    telemetry: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # última telemetria (sem IP)
    telemetry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Cadeia de prova de exibição (RDPP §7)
    pop_last_seq: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    pop_last_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    screen: Mapped["Screen"] = relationship("Screen", foreign_keys=[screen_id], overlaps="device")  # noqa: F821

    @property
    def is_active(self) -> bool:
        return self.status == DeviceStatus.ACTIVE.value


class PairingRequest(Base):
    """Pedido de pareamento criado pelo dispositivo (RDPP §2)."""

    __tablename__ = "pairing_requests"
    __table_args__ = (
        Index("ix_pairing_requests_status_expires", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pairing_code: Mapped[str] = mapped_column(String(9), nullable=False, unique=True)  # XXXX-XXXX
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    key_alg: Mapped[str] = mapped_column(String(10), nullable=False, default="ES256")
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    app_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    device_info: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=PairingStatus.PENDING.value)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    claimed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="SET NULL"), nullable=True
    )
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    result_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    device: Mapped[Optional["Device"]] = relationship("Device", foreign_keys=[device_id])


class CommandStatus(str, enum.Enum):
    PENDING = "PENDING"  # persistido, ainda não entregue
    DELIVERED = "DELIVERED"  # enviado pelo WS, aguardando ack
    RECEIVED = "RECEIVED"  # player confirmou recebimento (não final)
    DONE = "DONE"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


COMMAND_FINAL_STATUSES = frozenset({"DONE", "FAILED", "UNSUPPORTED", "EXPIRED", "CANCELLED"})


class DeviceCommand(Base):
    """Comando remoto persistido (RDPP §8; ETAPA2 #1386–#1391)."""

    __tablename__ = "device_commands"
    __table_args__ = (
        Index("ix_device_commands_device_status", "device_id", "status"),
        Index("ix_device_commands_screen_created", "screen_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="CASCADE"), nullable=False
    )
    command: Mapped[str] = mapped_column(String(30), nullable=False)
    args: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    issued_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CommandStatus.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    acked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    result_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CreativeKill(Base):
    """Kill de criativo (revogação imediata). ``screen_id`` nulo = todas as telas.

    Persistido; entra em ``revoked`` do próximo manifesto (W-INV consulta
    ``app.modules.playback.kills.revoked_creative_ids``) e é publicado na hora.
    """

    __tablename__ = "creative_kills"
    __table_args__ = (
        Index("ix_creative_kills_creative", "creative_id"),
        Index("ix_creative_kills_screen", "screen_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creative_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="CASCADE"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    issued_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lifted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class DeviceTelemetry(Base):
    """Amostras de telemetria (a cada 5 min; sem IP). Retenção configurável."""

    __tablename__ = "device_telemetry"
    __table_args__ = (Index("ix_device_telemetry_device_recorded", "device_id", "recorded_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="CASCADE"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)


class AppReleaseStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"


class AppRelease(Base):
    """Versão do app do player (RDPP §8). Assinatura ES256 feita FORA do servidor com a
    chave de release; o servidor só verifica contra as chaves públicas configuradas."""

    __tablename__ = "app_releases"
    __table_args__ = (
        Index("uq_app_releases_platform_version", "platform", "version", unique=True),
        Index("ix_app_releases_platform_status", "platform", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)  # semver x.y.z
    url: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=AppReleaseStatus.DRAFT.value)
    rollout_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # onda atual (0–100)
    target_groups: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)  # None = todos os grupos
    max_concurrent: Mapped[int] = mapped_column(Integer, nullable=False, default=20)  # downloads simultâneos
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SyncEventType(str, enum.Enum):
    MANIFEST_RECEIVED = "MANIFEST_RECEIVED"
    DOWNLOAD_STARTED = "DOWNLOAD_STARTED"
    DOWNLOAD_COMPLETED = "DOWNLOAD_COMPLETED"
    MANIFEST_APPLIED = "MANIFEST_APPLIED"
    MANIFEST_FAILED = "MANIFEST_FAILED"
    MEDIA_ERROR = "MEDIA_ERROR"


class DeviceSyncEvent(Base):
    """Histórico de sincronização/download por dispositivo (ETAPA2 #798–#799; P001 §103).

    Gravado SOMENTE em transições (nunca por heartbeat repetido); retenção
    ``player.sync_events_retention_days``. ``org_id``/``establishment_id`` = da tela na data.
    """

    __tablename__ = "device_sync_events"
    __table_args__ = (
        Index("ix_device_sync_events_screen_occurred", "screen_id", "occurred_at"),
        Index("ix_device_sync_events_org_occurred", "org_id", "occurred_at"),
        Index("ix_device_sync_events_type_occurred", "type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    manifest_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    media_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
