"""
RD Mídia Indoor — Campanhas, criativos, alvos, revisões e aprovação (W-CAMP, AÇÃO 002).

Hierarquia (P001 §38; ETAPA2 #390): ANUNCIANTE → CAMPANHA → CRIATIVO → SEGMENTAÇÃO →
PROGRAMAÇÃO → EXIBIÇÃO. Campanha, criativo, pedido (``orders``, em pricing.py) e
cobrança (W-FIN) são entidades separadas.

- Status canônicos em PT (P001 §39; ADR_006 #10). REPRODUZINDO é DERIVADO de PoP
  recente (não é armazenado).
- Campanha guarda a especificação "de trabalho" (editável em RASCUNHO/REJEITADA).
  No checkout a especificação é CONGELADA numa ``CampaignRevision`` (preço, telas,
  criativos). A revisão aprovada vigente é ``live_revision_id``; alterações após a
  aprovação criam nova revisão (kind CHANGE) e a anterior continua valendo até a nova
  ser aprovada (ETAPA2 #397–#406, #698–#701).
- Criativo: código público ``RD-XXXXXX`` (alfabeto seguro, sem L,I,X,W,O,K,0,1),
  único e IMUTÁVEL; nova versão relevante = novo criativo com novo código
  (``supersedes_id``). Duração ∈ {30, 60, 120} s (CHECK no banco).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from typing import TYPE_CHECKING

from app.core.database import Base
from app.models.base import BaseModelMixin

if TYPE_CHECKING:  # pragma: no cover
    from app.models.media import MediaFile  # noqa: F401


# ─── Enums ───────────────────────────────────────────────────────────────────

class CampaignStatus(str, enum.Enum):
    """Máquina de estados oficial (P001 §39). Ver ``app.modules.campaigns.states``."""
    RASCUNHO = "RASCUNHO"
    AGUARDANDO_PAGAMENTO = "AGUARDANDO_PAGAMENTO"
    AGUARDANDO_MIDIA = "AGUARDANDO_MIDIA"
    EM_ANALISE = "EM_ANALISE"
    APROVADA = "APROVADA"
    AGENDADA = "AGENDADA"
    SINCRONIZANDO = "SINCRONIZANDO"
    ATIVA = "ATIVA"
    PAUSADA = "PAUSADA"
    FINALIZADA = "FINALIZADA"
    REJEITADA = "REJEITADA"
    CANCELADA = "CANCELADA"


CAMPAIGN_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in CampaignStatus)


class TargetType(str, enum.Enum):
    """Segmentação (P001 §45). Não existe alvo por TV individual (ETAPA2 #134/#144)."""
    COUNTRY = "COUNTRY"
    REGION = "REGION"
    STATE = "STATE"
    CITY = "CITY"
    NEIGHBORHOOD = "NEIGHBORHOOD"
    POSTAL_CODE = "POSTAL_CODE"
    CATEGORY = "CATEGORY"
    ESTABLISHMENT = "ESTABLISHMENT"


class PeriodType(str, enum.Enum):
    """Períodos contratáveis (ETAPA2 #503–#507). Trimestral NÃO existe."""
    AVULSO = "AVULSO"
    MENSAL = "MENSAL"
    SEMESTRAL = "SEMESTRAL"
    ANUAL = "ANUAL"


class FrequencyKind(str, enum.Enum):
    PER_HOUR = "PER_HOUR"
    PER_DAY = "PER_DAY"
    SPECIFIC_TIME = "SPECIFIC_TIME"


class Priority(str, enum.Enum):
    STANDARD = "STANDARD"
    PREMIUM = "PREMIUM"


class Exclusivity(str, enum.Enum):
    """ETAPA2 #492 (decisão do coordenador): COMPETITOR_EXCLUSIVE = nenhuma campanha
    concorrente pode ser reservada nas mesmas telas em períodos sobrepostos."""
    NONE = "NONE"
    COMPETITOR_EXCLUSIVE = "COMPETITOR_EXCLUSIVE"


class SalesMode(str, enum.Enum):
    """Self-service ou concluída com o comercial (ETAPA2 #1017–#1022)."""
    SELF_SERVICE = "SELF_SERVICE"
    COMMERCIAL = "COMMERCIAL"


class ApprovalMode(str, enum.Enum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


class ApprovalSource(str, enum.Enum):
    MANUAL = "MANUAL"  # revisão humana da RD
    AUTO_GLOBAL = "AUTO_GLOBAL"  # campaign.approval_mode = AUTO
    AUTO_TRUSTED_CLIENT = "AUTO_TRUSTED_CLIENT"  # exceção por organização confiável


class RevisionKind(str, enum.Enum):
    INITIAL = "INITIAL"
    CHANGE = "CHANGE"
    RENEWAL = "RENEWAL"  # extensão do período (só a data final muda; sem reaprovação de conteúdo)


class RevisionStatus(str, enum.Enum):
    AGUARDANDO_PAGAMENTO = "AGUARDANDO_PAGAMENTO"
    AGUARDANDO_MIDIA = "AGUARDANDO_MIDIA"
    EM_ANALISE = "EM_ANALISE"
    APROVADA = "APROVADA"
    REJEITADA = "REJEITADA"
    SUBSTITUIDA = "SUBSTITUIDA"
    CANCELADA = "CANCELADA"
    EXPIRADA = "EXPIRADA"


class CreativeKind(str, enum.Enum):
    """Tipos de conteúdo com engine comum (P001 §24)."""
    EXTERNAL = "EXTERNAL"
    INTERNAL = "INTERNAL"
    INSTITUTIONAL = "INSTITUTIONAL"
    FALLBACK = "FALLBACK"


class CreativeStatus(str, enum.Enum):
    RASCUNHO = "RASCUNHO"
    EM_ANALISE = "EM_ANALISE"
    APROVADO = "APROVADO"
    REJEITADO = "REJEITADO"
    SUBSTITUIDO = "SUBSTITUIDO"
    ARQUIVADO = "ARQUIVADO"


class TechnicalStatus(str, enum.Enum):
    PENDENTE = "PENDENTE"  # mídia ainda não está READY
    VALIDO = "VALIDO"
    INVALIDO = "INVALIDO"


class Orientation(str, enum.Enum):
    HORIZONTAL = "HORIZONTAL"
    VERTICAL = "VERTICAL"


ALLOWED_DURATIONS_S: tuple[int, ...] = (30, 60, 120)


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# ─── Campanha ────────────────────────────────────────────────────────────────

class Campaign(BaseModelMixin, Base):
    """Campanha publicitária (especificação de trabalho + estado)."""

    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(f"status IN {_in(CAMPAIGN_STATUS_VALUES)}", name="status_valid"),
        CheckConstraint("duration_s IN (30, 60, 120)", name="duration_valid"),
        CheckConstraint("days_mask BETWEEN 1 AND 127", name="days_mask_valid"),
        CheckConstraint("freq_value > 0", name="freq_value_positive"),
        CheckConstraint("min_interval_s >= 0", name="min_interval_nonneg"),
        CheckConstraint(f"period_type IN {_in(p.value for p in PeriodType)}", name="period_type_valid"),
        CheckConstraint(f"freq_kind IN {_in(f.value for f in FrequencyKind)}", name="freq_kind_valid"),
        CheckConstraint(f"priority IN {_in(p.value for p in Priority)}", name="priority_valid"),
        CheckConstraint(f"sales_mode IN {_in(s.value for s in SalesMode)}", name="sales_mode_valid"),
        CheckConstraint(f"exclusivity IN {_in(e.value for e in Exclusivity)}", name="exclusivity_valid"),
        CheckConstraint("end_date IS NULL OR start_date IS NULL OR end_date >= start_date", name="dates_order"),
        CheckConstraint("contracted_price_cents >= 0", name="contracted_price_nonneg"),
        Index("ix_campaigns_org_id_status", "org_id", "status"),
        Index("ix_campaigns_status", "status"),
        Index("ix_campaigns_end_date", "end_date"),
        Index("ix_campaigns_review_due_at", "review_due_at"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=CampaignStatus.RASCUNHO.value)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    # Concorrência otimista nas edições (PATCH com expected_version).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Comercial
    sales_mode: Mapped[str] = mapped_column(String(20), nullable=False, default=SalesMode.SELF_SERVICE.value)
    seller_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    plan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=True
    )

    # Programação (datas LOCAIS — vencimento 23:59:59 no fuso de cada tela; P001 §51)
    period_type: Mapped[str] = mapped_column(String(20), nullable=False, default=PeriodType.AVULSO.value)
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    duration_s: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    freq_kind: Mapped[str] = mapped_column(String(20), nullable=False, default=FrequencyKind.PER_HOUR.value)
    freq_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    specific_times: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)  # ["HH:MM", ...] locais
    min_interval_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_mask: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=127)  # bit0 = segunda
    time_bands: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: [[0, 1440]])
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default=Priority.STANDARD.value)
    exclusivity: Mapped[str] = mapped_column(String(30), nullable=False, default="NONE", server_default="NONE")
    allow_same_advertiser_consecutive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    request_art_service: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Renovação automática (ETAPA2 #509): gera a cobrança do próximo período D-N antes do vencimento local.
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))

    # Classificação do anúncio no nível da campanha (usada quando ainda não há criativo)
    product_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    brand_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=True
    )

    # Aprovação
    approval_source: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    review_due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rejection_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Pagamento / pedido (preço congelado — nunca vindo do cliente)
    contracted_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    payment_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    requires_new_payment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payment_issue: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # ex.: PAID_WITHOUT_INVENTORY

    # Revisões
    live_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_revisions.id", ondelete="RESTRICT", use_alter=True, name="fk_campaigns_live_revision"),
        nullable=True,
    )
    pending_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaign_revisions.id", ondelete="RESTRICT", use_alter=True, name="fk_campaigns_pending_revision"),
        nullable=True,
    )
    status_before_review: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    paused_from: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # Marcos
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    syncing_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    creatives: Mapped[list["CampaignCreative"]] = relationship(
        "CampaignCreative", back_populates="campaign", cascade="all, delete-orphan"
    )
    targets: Mapped[list["CampaignTarget"]] = relationship(
        "CampaignTarget", back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignSegment(Base):
    """Segmentos declarados do anúncio no nível da campanha (ETAPA2 #107–#110)."""

    __tablename__ = "campaign_segments"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT"), primary_key=True
    )


class CampaignTarget(Base):
    """Alvo normalizado. Inclusões combinam AND entre tipos e OR dentro do tipo;
    ESTABLISHMENT soma estabelecimentos específicos; exclusões removem."""

    __tablename__ = "campaign_targets"
    __table_args__ = (
        CheckConstraint(f"target_type IN {_in(t.value for t in TargetType)}", name="target_type_valid"),
        UniqueConstraint("campaign_id", "target_type", "target_value", "is_exclude", name="uq_campaign_targets_unique"),
        Index("ix_campaign_targets_campaign_id", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # Valor normalizado: BRA · SUDESTE · GO · GO:goiania · GO:goiania:setor-bueno · 74000 (prefixo de CEP)
    # · <uuid categoria> · <uuid estabelecimento>
    target_value: Mapped[str] = mapped_column(String(255), nullable=False)
    is_exclude: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="targets")


class CampaignCreative(Base):
    """Vínculo campanha × criativo.

    ``is_active``: faz parte da versão VIGENTE (toca). ``pending``: entrou por uma
    alteração ainda não aprovada (não toca). ``replaces_creative_id``: criativo da
    versão vigente que será substituído quando a alteração for aprovada.
    """

    __tablename__ = "campaign_creatives"
    __table_args__ = (
        UniqueConstraint("campaign_id", "creative_id", name="uq_campaign_creatives_campaign_creative"),
        Index("ix_campaign_creatives_campaign_id", "campaign_id"),
        Index("ix_campaign_creatives_creative_id", "creative_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    creative_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("creatives.id", ondelete="RESTRICT"), nullable=False
    )
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pending_removal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replaces_creative_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("creatives.id", ondelete="RESTRICT"), nullable=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="creatives")
    creative: Mapped["Creative"] = relationship("Creative", foreign_keys=[creative_id])


class CampaignRevision(Base):
    """Snapshot congelado (spec + telas + criativos + preço) de uma versão da campanha."""

    __tablename__ = "campaign_revisions"
    __table_args__ = (
        UniqueConstraint("campaign_id", "number", name="uq_campaign_revisions_campaign_number"),
        CheckConstraint(f"kind IN {_in(k.value for k in RevisionKind)}", name="kind_valid"),
        CheckConstraint(f"status IN {_in(s.value for s in RevisionStatus)}", name="status_valid"),
        Index("ix_campaign_revisions_campaign_id", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default=RevisionKind.INITIAL.value)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    screen_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    creative_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    change_summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    quote: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    quote_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    price_diff_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    order_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True
    )
    hold_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    decided_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CampaignStatusHistory(Base):
    """Histórico append-only de transições (SLA, explicação; complementa audit_logs)."""

    __tablename__ = "campaign_status_history"
    __table_args__ = (Index("ix_campaign_status_history_campaign_id", "campaign_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    from_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    trigger: Mapped[str] = mapped_column(String(60), nullable=False)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    real_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="api")
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OrganizationApprovalPolicy(Base):
    """Exceção de aprovação por cliente confiável (ETAPA2 #692; P002 #9)."""

    __tablename__ = "organization_approval_policies"
    __table_args__ = (CheckConstraint("mode IN ('MANUAL', 'AUTO')", name="mode_valid"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ─── Criativo ────────────────────────────────────────────────────────────────

class Creative(BaseModelMixin, Base):
    """Criativo (anúncio). ``code`` = ID público ``RD-XXXXXX`` imutável (trigger)."""

    __tablename__ = "creatives"
    __table_args__ = (
        UniqueConstraint("code", name="uq_creatives_code"),
        CheckConstraint("duration_seconds IN (30, 60, 120)", name="duration_valid"),
        CheckConstraint("code ~ '^RD-[ABCDEFGHJMNPQRSTUVYZ2-9]{6}$'", name="code_format"),
        CheckConstraint(f"kind IN {_in(k.value for k in CreativeKind)}", name="kind_valid"),
        CheckConstraint(f"status IN {_in(s.value for s in CreativeStatus)}", name="status_valid"),
        CheckConstraint(f"technical_status IN {_in(s.value for s in TechnicalStatus)}", name="technical_status_valid"),
        CheckConstraint("orientation IN ('HORIZONTAL', 'VERTICAL')", name="orientation_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_creatives_org_id", "org_id"),
        Index("ix_creatives_status", "status"),
        Index("ix_creatives_root_id", "root_id"),
        Index("ix_creatives_media_file_id", "media_file_id"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(9), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default=CreativeKind.EXTERNAL.value)
    media_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("media_files.id", ondelete="RESTRICT"), nullable=False
    )
    duration_seconds: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    orientation: Mapped[str] = mapped_column(String(12), nullable=False, default=Orientation.HORIZONTAL.value)
    media_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ad_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CreativeStatus.RASCUNHO.value)
    technical_status: Mapped[str] = mapped_column(String(10), nullable=False, default=TechnicalStatus.PENDENTE.value)
    technical_error: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    # Classificação do anúncio (segmentos em creative_segments)
    product_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    brand_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=True
    )

    # Versionamento (nova versão = novo criativo com novo código)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("creatives.id", ondelete="RESTRICT"), nullable=True
    )
    root_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("creatives.id", ondelete="RESTRICT"), nullable=True
    )

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="creatives")


class CreativeSegment(Base):
    """Segmentos do anúncio (classificação individual — ETAPA2 #107–#110)."""

    __tablename__ = "creative_segments"

    creative_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("creatives.id", ondelete="CASCADE"), primary_key=True
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT"), primary_key=True
    )


# Tabelas append-only deste domínio (o agente de banco aplica ddl.append_only_trigger).
CAMPAIGN_APPEND_ONLY_TABLES = ("campaign_status_history",)
