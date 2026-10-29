"""
RD Mídia Indoor — Taxonomia comercial (domínio W-ORG; AÇÃO 002).

Requisitos: ETAPA2 #100–#126; P001 §41–§44; P002 #27; A6 §10.7 (nomes alinhados com
W-CAMP — motor de conflitos — e W-INV).

- ``taxonomy_nodes``  — árvore SETOR > CATEGORIA > SUBCATEGORIA (tipo/ramo do
  estabelecimento e do anunciante). ``establishments.category_id`` e
  ``advertiser_profiles.category_id`` apontam para cá.
- ``segments``        — árvore de segmentos/produtos vendidos ou anunciados
  (``bebidas > alcoólicas > cerveja``; ``alimentos > snacks``), profundidade livre.
- ``brands`` / ``products`` (produto pertence a um segmento e, opcionalmente, a uma marca).
- ``establishment_segments`` / ``establishment_products`` — o que o estabelecimento vende
  (múltiplos).
- ``advertiser_profiles`` / ``advertiser_segments`` — ramo e segmentos do anunciante.
- ``competitor_links`` — concorrência explícita entre organizações (par ordenado).
- ``content_policies`` — política de conteúdo por segmento (BLOCK/ALLOW). BLOCK em
  qualquer segmento-ancestral-ou-próprio ⇒ bloqueado (não sobrescrevível). Defaults
  (seed ``app.modules.taxonomy.service.ensure_default_content_policies``): apostas,
  conteúdo adulto, armas = BLOCK; bebidas alcoólicas, tabaco, política, farmacêuticos =
  ALLOW. Toda alteração é auditada (``audit_logs``).

Hierarquia materializada em ``path`` = ``/<id-hex>/<id-hex>/.../`` (ancestrais + o
próprio, sem extensão ltree): descendentes de X ⇔ ``path LIKE X.path || '%'``;
ancestrais de X ⇔ ids presentes em ``X.path``.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TaxonomyKind(str, enum.Enum):
    SECTOR = "SECTOR"
    CATEGORY = "CATEGORY"
    SUBCATEGORY = "SUBCATEGORY"


TAXONOMY_PARENT_KIND: dict[str, Optional[str]] = {
    TaxonomyKind.SECTOR.value: None,
    TaxonomyKind.CATEGORY.value: TaxonomyKind.SECTOR.value,
    TaxonomyKind.SUBCATEGORY.value: TaxonomyKind.CATEGORY.value,
}


class PolicyEffect(str, enum.Enum):
    BLOCK = "BLOCK"
    ALLOW = "ALLOW"


class _TimestampsMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TaxonomyNode(_TimestampsMixin, Base):
    __tablename__ = "taxonomy_nodes"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_taxonomy_nodes_parent_id", "parent_id"),
        Index("ix_taxonomy_nodes_path", "path", postgresql_ops={"path": "text_pattern_ops"}),
        CheckConstraint("kind IN ('SECTOR','CATEGORY','SUBCATEGORY')", name="kind_valid"),
        CheckConstraint(
            "(kind = 'SECTOR' AND parent_id IS NULL AND depth = 0)"
            " OR (kind = 'CATEGORY' AND parent_id IS NOT NULL AND depth = 1)"
            " OR (kind = 'SUBCATEGORY' AND parent_id IS NOT NULL AND depth = 2)",
            name="kind_depth_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))


class Segment(_TimestampsMixin, Base):
    __tablename__ = "segments"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_segments_parent_id", "parent_id"),
        Index("ix_segments_path", "path", postgresql_ops={"path": "text_pattern_ops"}),
        CheckConstraint("(parent_id IS NULL) = (depth = 0)", name="root_depth_consistent"),
        CheckConstraint("depth >= 0 AND depth <= 8", name="depth_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))


class Brand(_TimestampsMixin, Base):
    __tablename__ = "brands"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (Index("ix_brands_owner_org_id", "owner_org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True)
    owner_org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))


class Product(_TimestampsMixin, Base):
    __tablename__ = "products"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_products_segment_id", "segment_id"),
        Index("ix_products_brand_id", "brand_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT"), nullable=False
    )
    brand_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))


class EstablishmentSegment(Base):
    __tablename__ = "establishment_segments"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (Index("ix_establishment_segments_segment_id", "segment_id"),)

    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="CASCADE"), primary_key=True
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EstablishmentProduct(Base):
    __tablename__ = "establishment_products"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (Index("ix_establishment_products_product_id", "product_id"),)

    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AdvertiserProfile(Base):
    __tablename__ = "advertiser_profiles"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (Index("ix_advertiser_profiles_category_id", "category_id"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"), nullable=True
    )
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AdvertiserSegment(Base):
    __tablename__ = "advertiser_segments"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (Index("ix_advertiser_segments_segment_id", "segment_id"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("segments.id", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CompetitorLink(Base):
    """Concorrência explícita entre duas organizações (par canônico org_a_id < org_b_id)."""

    __tablename__ = "competitor_links"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("org_a_id", "org_b_id", name="uq_competitor_links_pair"),
        CheckConstraint("org_a_id < org_b_id", name="ordered_pair"),
        Index("ix_competitor_links_org_b_id", "org_b_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    org_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ContentPolicy(Base):
    __tablename__ = "content_policies"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (CheckConstraint("effect IN ('BLOCK','ALLOW')", name="effect_valid"),)

    segment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True
    )
    effect: Mapped[str] = mapped_column(String(5), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
