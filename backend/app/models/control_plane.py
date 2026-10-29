"""
RD Mídia Indoor — Control plane RD (AÇÃO 002 / W-BIZ).

Requisitos: ETAPA2 #26–#40; P001 §14–§15; P002 item 39.

Gerencia INSTÂNCIAS (RD produção, DEV na VPS, white-label, franquia), grupos, versões
(releases: RASCUNHO → VALIDADA na instância DEV → PUBLICADA), rollouts seletivos
(uma / grupo / todas, versão escolhida) com fila e LIMITE DE CONCORRÊNCIA (sem efeito
metralhadora), rollback e histórico append-only (``cp_events``).

A execução real do deploy (Portainer) é DEPENDÊNCIA EXTERNA: ``DeploymentExecutor``
com implementação NULA (registra ``BLOQUEADO_DEPENDENCIA``) e simulação local
explicitamente rotulada (``SIMULACAO_LOCAL`` — nunca altera a versão reportada pela
instância). A versão "atual" de uma instância vem SOMENTE do heartbeat dela.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _in(col: str, values) -> str:
    return f"{col} IN (" + ",".join(f"'{v}'" for v in values) + ")"


INSTANCE_KINDS = ("RD_PRODUCAO", "DEV", "WHITE_LABEL", "FRANQUIA", "LICENCA")
RELEASE_STATUS = ("RASCUNHO", "VALIDADA", "PUBLICADA", "DESCONTINUADA")
ROLLOUT_STATUS = ("PENDENTE", "EM_ANDAMENTO", "PAUSADO", "CONCLUIDO", "SIMULADO", "BLOQUEADO_DEPENDENCIA", "FALHOU",
                  "CANCELADO")
TARGET_STATUS = ("NA_FILA", "EM_EXECUCAO", "CONCLUIDO", "SIMULADO", "BLOQUEADO_DEPENDENCIA", "FALHOU", "CANCELADO")


class CpInstanceGroup(Base):
    __tablename__ = "cp_instance_groups"
    __table_args__ = (UniqueConstraint("name", name="uq_cp_instance_groups_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CpInstance(Base):
    __tablename__ = "cp_instances"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("slug", name="uq_cp_instances_slug"),
        CheckConstraint(_in("kind", INSTANCE_KINDS), name="kind_valid"),
        CheckConstraint(_in("status", ("ATIVA", "INATIVA", "MANUTENCAO")), name="status_valid"),
        Index("ix_cp_instances_group_id", "group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    base_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    group_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cp_instance_groups.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="ATIVA")
    current_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # reportada pelo heartbeat
    last_deployed_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    health_status: Mapped[str] = mapped_column(String(15), nullable=False, default="DESCONHECIDO")
    health: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    token_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    license: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())


class CpRelease(Base):
    __tablename__ = "cp_releases"
    __table_args__ = (
        UniqueConstraint("version", name="uq_cp_releases_version"),
        CheckConstraint(_in("status", RELEASE_STATUS), name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="RASCUNHO")
    image_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # registry privado (sem código-fonte)
    changelog: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    validated_on_instance_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cp_instances.id", ondelete="SET NULL"), nullable=True
    )
    validated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CpRollout(Base):
    __tablename__ = "cp_rollouts"
    __table_args__ = (
        CheckConstraint(_in("status", ROLLOUT_STATUS), name="status_valid"),
        CheckConstraint(_in("target_kind", ("UMA", "GRUPO", "TODAS")), name="target_kind_valid"),
        CheckConstraint(_in("executor", ("NULO", "SIMULACAO_LOCAL")), name="executor_valid"),
        CheckConstraint(_in("failure_policy", ("PARAR", "CONTINUAR")), name="failure_policy_valid"),
        CheckConstraint("max_concurrency BETWEEN 1 AND 20", name="max_concurrency_range"),
        Index("ix_cp_rollouts_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    release_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cp_releases.id", ondelete="RESTRICT"), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    target_ref: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    failure_policy: Mapped[str] = mapped_column(String(10), nullable=False, default="PARAR")
    executor: Mapped[str] = mapped_column(String(20), nullable=False, default="NULO")
    status: Mapped[str] = mapped_column(String(25), nullable=False, default="PENDENTE")
    is_rollback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rollback_of_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cp_rollouts.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CpRolloutTarget(Base):
    __tablename__ = "cp_rollout_targets"
    __table_args__ = (
        UniqueConstraint("rollout_id", "instance_id", name="uq_cp_rollout_targets_rollout_instance"),
        CheckConstraint(_in("status", TARGET_STATUS), name="status_valid"),
        # nunca dois deploys simultâneos na mesma instância
        Index("uq_cp_rollout_targets_instance_running", "instance_id", unique=True,
              postgresql_where=text("status = 'EM_EXECUCAO'")),
        Index("ix_cp_rollout_targets_rollout_status", "rollout_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rollout_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cp_rollouts.id", ondelete="CASCADE"), nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("cp_instances.id", ondelete="RESTRICT"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    from_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    to_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(25), nullable=False, default="NA_FILA")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class CpEvent(Base):
    """Histórico do control plane (append-only)."""

    __tablename__ = "cp_events"
    __table_args__ = (
        Index("ix_cp_events_instance_id", "instance_id"),
        Index("ix_cp_events_rollout_id", "rollout_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(60), nullable=False)
    instance_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    release_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    rollout_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
