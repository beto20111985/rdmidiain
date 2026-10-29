"""initial_schema

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000

Migration inicial: cria todas as tabelas, índices compostos e constraints.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────────────
    # USERS
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("totp_secret", sa.String(255), nullable=True),
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_index(op.f("ix_users_is_active"), "users", ["is_active"])

    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("refresh_token_hash", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", sa.String(50), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_sessions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_sessions_token_hash")),
    )
    op.create_index(op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"])
    op.create_index(op.f("ix_user_sessions_token_hash"), "user_sessions", ["token_hash"])

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=True),
        sa.Column("entity_id", sa.String(255), nullable=True),
        sa.Column("before_data", postgresql.JSONB(), nullable=True),
        sa.Column("after_data", postgresql.JSONB(), nullable=True),
        sa.Column("ip_address", sa.String(50), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("impersonator_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_audit_logs_user_id_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["impersonator_id"], ["users.id"], name=op.f("fk_audit_logs_impersonator_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(op.f("ix_audit_logs_user_id"), "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"])
    op.create_index(op.f("ix_audit_logs_created_at"), "audit_logs", ["created_at"])

    # ─────────────────────────────────────────────────────────────────────────
    # ORGANIZATIONS
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("trade_name", sa.String(255), nullable=True),
        sa.Column("document", sa.String(18), nullable=False),
        sa.Column("document_type", sa.String(4), nullable=False, server_default="CNPJ"),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("type", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("document", name=op.f("uq_organizations_document")),
    )
    op.create_index(op.f("ix_organizations_document"), "organizations", ["document"], unique=True)
    op.create_index(op.f("ix_organizations_is_active"), "organizations", ["is_active"])

    op.create_table(
        "organization_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_organization_roles_org_id_organizations"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_roles")),
    )

    op.create_table(
        "organization_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_primary_org", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_organization_users_org_id_organizations"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_organization_users_user_id_users"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["organization_roles.id"], name=op.f("fk_organization_users_role_id_organization_roles"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_users")),
        sa.UniqueConstraint("org_id", "user_id", name="uq_organization_users_org_user"),
    )
    op.create_index(op.f("ix_organization_users_user_id"), "organization_users", ["user_id"])
    op.create_index(op.f("ix_organization_users_org_id"), "organization_users", ["org_id"])

    # user_invites (referencia organizations, users)
    op.create_table(
        "user_invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("token", sa.String(255), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], name=op.f("fk_user_invites_invited_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_user_invites_org_id_organizations"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_invites")),
        sa.UniqueConstraint("token", name=op.f("uq_user_invites_token")),
    )
    op.create_index(op.f("ix_user_invites_email"), "user_invites", ["email"])

    # ─────────────────────────────────────────────────────────────────────────
    # ESTABLISHMENT CATEGORIES + ESTABLISHMENTS
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "establishment_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["establishment_categories.id"], name=op.f("fk_establishment_categories_parent_id_establishment_categories"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_establishment_categories")),
        sa.UniqueConstraint("slug", name=op.f("uq_establishment_categories_slug")),
    )

    op.create_table(
        "establishments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("trade_name", sa.String(255), nullable=True),
        sa.Column("document", sa.String(18), nullable=True),
        sa.Column("legal_name", sa.String(255), nullable=True),
        sa.Column("responsible_name", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("address", sa.String(255), nullable=True),
        sa.Column("number", sa.String(20), nullable=True),
        sa.Column("complement", sa.String(100), nullable=True),
        sa.Column("neighborhood", sa.String(100), nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("state", sa.String(2), nullable=True),
        sa.Column("country", sa.String(3), nullable=False, server_default="BRA"),
        sa.Column("postal_code", sa.String(10), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="America/Sao_Paulo"),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("operational_config", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_establishments_org_id_organizations"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["category_id"], ["establishment_categories.id"], name=op.f("fk_establishments_category_id_establishment_categories"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_establishments")),
    )
    op.create_index(op.f("ix_establishments_org_id"), "establishments", ["org_id"])
    op.create_index("ix_establishments_city_state", "establishments", ["city", "state"])
    op.create_index(op.f("ix_establishments_is_active"), "establishments", ["is_active"])

    # ─────────────────────────────────────────────────────────────────────────
    # DEVICES (antes de screens, pois screens tem FK para devices)
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("screen_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("device_token", sa.String(255), nullable=False),
        sa.Column("device_secret", sa.String(500), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False, server_default="ANDROID"),
        sa.Column("app_version", sa.String(30), nullable=True),
        sa.Column("os_version", sa.String(50), nullable=True),
        sa.Column("paired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(50), nullable=True),
        sa.Column("ssid", sa.String(100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cycle_anchor", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telemetry", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
        sa.UniqueConstraint("device_token", name=op.f("uq_devices_device_token")),
    )
    op.create_index(op.f("ix_devices_device_token"), "devices", ["device_token"], unique=True)
    op.create_index(op.f("ix_devices_last_seen_at"), "devices", ["last_seen_at"])

    # ─────────────────────────────────────────────────────────────────────────
    # SCREENS
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "screens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("establishment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("friendly_id", sa.String(30), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("orientation", sa.String(20), nullable=False, server_default="HORIZONTAL"),
        sa.Column("status", sa.String(20), nullable=False, server_default="UNPAIRED"),
        sa.Column("hardware_brand", sa.String(100), nullable=True),
        sa.Column("hardware_model", sa.String(100), nullable=True),
        sa.Column("inches", sa.Float(), nullable=True),
        sa.Column("resolution_w", sa.Integer(), nullable=True),
        sa.Column("resolution_h", sa.Integer(), nullable=True),
        sa.Column("serial_number", sa.String(100), nullable=True),
        sa.Column("asset_tag", sa.String(100), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("estimated_audience", sa.Integer(), nullable=True),
        sa.Column("tv_owner", sa.String(20), nullable=False, server_default="ESTABLISHMENT"),
        sa.Column("comodato_config", postgresql.JSONB(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["establishment_id"], ["establishments.id"], name=op.f("fk_screens_establishment_id_establishments"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], name=op.f("fk_screens_device_id_devices"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screens")),
        sa.UniqueConstraint("friendly_id", name=op.f("uq_screens_friendly_id")),
    )
    op.create_index("ix_screens_friendly_id", "screens", ["friendly_id"], unique=True)
    op.create_index(op.f("ix_screens_establishment_id"), "screens", ["establishment_id"])
    op.create_index(op.f("ix_screens_status"), "screens", ["status"])
    op.create_index(op.f("ix_screens_device_id"), "screens", ["device_id"])

    # Adicionar FK de devices.screen_id -> screens.id após criar screens
    op.create_foreign_key(
        "fk_devices_screen_id_screens",
        "devices", "screens",
        ["screen_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_devices_screen_id"), "devices", ["screen_id"])

    # ─────────────────────────────────────────────────────────────────────────
    # PAIRING REQUESTS
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "pairing_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pairing_code", sa.String(20), nullable=False),
        sa.Column("qr_token", sa.String(255), nullable=False),
        sa.Column("establishment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["establishment_id"], ["establishments.id"], name=op.f("fk_pairing_requests_establishment_id_establishments"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], name=op.f("fk_pairing_requests_device_id_devices"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pairing_requests")),
        sa.UniqueConstraint("qr_token", name=op.f("uq_pairing_requests_qr_token")),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # MEDIA FILES + CREATIVES
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "media_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("original_filename", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(10), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("orientation", sa.String(20), nullable=True),
        sa.Column("storage_path", sa.String(1000), nullable=False),
        sa.Column("storage_provider", sa.String(20), nullable=False, server_default="local"),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="UPLOADING"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("has_audio_original", sa.Boolean(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_media_files_org_id_organizations"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_media_files_created_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_files")),
    )
    op.create_index(op.f("ix_media_files_org_id"), "media_files", ["org_id"])
    op.create_index(op.f("ix_media_files_status"), "media_files", ["status"])

    op.create_table(
        "creatives",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creative_id", sa.String(20), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("media_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_creatives_org_id_organizations"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["media_file_id"], ["media_files.id"], name=op.f("fk_creatives_media_file_id_media_files"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], name=op.f("fk_creatives_reviewed_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_creatives")),
        sa.UniqueConstraint("creative_id", name="uq_creatives_creative_id"),
    )
    op.create_index("ix_creatives_creative_id", "creatives", ["creative_id"], unique=True)
    op.create_index(op.f("ix_creatives_org_id"), "creatives", ["org_id"])
    op.create_index(op.f("ix_creatives_status"), "creatives", ["status"])

    # ─────────────────────────────────────────────────────────────────────────
    # CAMPAIGNS
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="DRAFT"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("total_budget_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_campaigns_org_id_organizations"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], name=op.f("fk_campaigns_approved_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaigns")),
    )
    op.create_index("ix_campaigns_org_id_status", "campaigns", ["org_id", "status"])
    op.create_index(op.f("ix_campaigns_status"), "campaigns", ["status"])

    op.create_table(
        "campaign_creatives",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creative_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_campaign_creatives_campaign_id_campaigns"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["creative_id"], ["creatives.id"], name=op.f("fk_campaign_creatives_creative_id_creatives"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_creatives")),
        sa.UniqueConstraint("campaign_id", "creative_id", name="uq_campaign_creatives_campaign_creative"),
    )

    op.create_table(
        "campaign_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day_type", sa.String(20), nullable=False, server_default="ALL"),
        sa.Column("days_of_week", postgresql.JSONB(), nullable=True),
        sa.Column("time_from", sa.Time(), nullable=True),
        sa.Column("time_to", sa.Time(), nullable=True),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="America/Sao_Paulo"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_campaign_schedules_campaign_id_campaigns"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_schedules")),
    )

    op.create_table(
        "campaign_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(30), nullable=False),
        sa.Column("target_value", sa.String(255), nullable=False),
        sa.Column("is_exclude", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_campaign_targets_campaign_id_campaigns"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_targets")),
    )
    op.create_index("ix_campaign_targets_type_value", "campaign_targets", ["target_type", "target_value"])

    # ─────────────────────────────────────────────────────────────────────────
    # INVENTORY
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "inventory_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slots_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_inventory_reservations_campaign_id_campaigns"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_inventory_reservations_org_id_organizations"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_reservations")),
        sa.UniqueConstraint("idempotency_key", name="uq_inventory_reservations_idempotency"),
    )

    op.create_table(
        "inventory_slots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("screen_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slot_type", sa.String(20), nullable=False, server_default="EXTERNAL"),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("minute", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("status", sa.String(20), nullable=False, server_default="AVAILABLE"),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["screen_id"], ["screens.id"], name=op.f("fk_inventory_slots_screen_id_screens"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_inventory_slots_campaign_id_campaigns"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reservation_id"], ["inventory_reservations.id"], name=op.f("fk_inventory_slots_reservation_id_inventory_reservations"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_slots")),
        sa.UniqueConstraint("screen_id", "date", "hour", "minute", "duration_seconds", name="uq_inventory_slots_screen_time"),
    )
    op.create_index("ix_inventory_slots_screen_date", "inventory_slots", ["screen_id", "date"])
    op.create_index(op.f("ix_inventory_slots_status"), "inventory_slots", ["status"])

    # ─────────────────────────────────────────────────────────────────────────
    # PLAYBACK
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "playback_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("player_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("screen_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creative_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manifest_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_duration_s", sa.Integer(), nullable=False),
        sa.Column("actual_duration_s", sa.Float(), nullable=True),
        sa.Column("result", sa.String(20), nullable=False, server_default="COMPLETE"),
        sa.Column("sequence_number", sa.BigInteger(), nullable=False),
        sa.Column("nonce", sa.String(100), nullable=False),
        sa.Column("signature", sa.String(500), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=True),
        sa.Column("sync_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["player_id"], ["devices.id"], name=op.f("fk_playback_events_player_id_devices"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["screen_id"], ["screens.id"], name=op.f("fk_playback_events_screen_id_screens"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["creative_id"], ["creatives.id"], name=op.f("fk_playback_events_creative_id_creatives"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_playback_events_campaign_id_campaigns"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_playback_events")),
        sa.UniqueConstraint("player_id", "sequence_number", "nonce", name="uq_playback_events_player_seq_nonce"),
    )
    # Índice composto para queries de relatório por tela + período
    op.create_index("ix_playback_events_screen_started", "playback_events", ["screen_id", "started_at"])
    op.create_index(op.f("ix_playback_events_campaign_id"), "playback_events", ["campaign_id"])
    op.create_index(op.f("ix_playback_events_is_valid"), "playback_events", ["is_valid"])

    op.create_table(
        "player_manifests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("screen_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["screen_id"], ["screens.id"], name=op.f("fk_player_manifests_screen_id_screens"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_player_manifests")),
        sa.UniqueConstraint("screen_id", "version", name="uq_player_manifests_screen_version"),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # FINANCIAL
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("ref_type", sa.String(100), nullable=True),
        sa.Column("ref_id", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("amount_cents > 0", name="ck_ledger_entries_amount_positive"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_ledger_entries_org_id_organizations"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_ledger_entries_created_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_entries")),
        sa.UniqueConstraint("idempotency_key", name="uq_ledger_entries_idempotency"),
    )
    op.create_index("ix_ledger_entries_org_id_type", "ledger_entries", ["org_id", "type"])

    op.create_table(
        "payment_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("gateway_ref", sa.String(255), nullable=True),
        sa.Column("gateway_data", postgresql.JSONB(), nullable=True),
        sa.Column("method", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_payment_orders_org_id_organizations"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_payment_orders_campaign_id_campaigns"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_orders")),
    )

    op.create_table(
        "commissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("screen_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("establishment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("playback_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["screen_id"], ["screens.id"], name=op.f("fk_commissions_screen_id_screens"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["establishment_id"], ["establishments.id"], name=op.f("fk_commissions_establishment_id_establishments"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_commissions_campaign_id_campaigns"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["playback_event_id"], ["playback_events.id"], name=op.f("fk_commissions_playback_event_id_playback_events"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commissions")),
    )

    op.create_table(
        "payouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("establishment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("pix_key", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="REQUESTED"),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("gateway_ref", sa.String(255), nullable=True),
        sa.ForeignKeyConstraint(["establishment_id"], ["establishments.id"], name=op.f("fk_payouts_establishment_id_establishments"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], name=op.f("fk_payouts_requested_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], name=op.f("fk_payouts_approved_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payouts")),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # EQUIPMENT
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "equipment_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("brand", sa.String(100), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("serial_number", sa.String(100), nullable=True),
        sa.Column("asset_tag", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="AVAILABLE"),
        sa.Column("screen_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("purchased_at", sa.Date(), nullable=True),
        sa.Column("warranty_until", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["screen_id"], ["screens.id"], name=op.f("fk_equipment_items_screen_id_screens"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_equipment_items_org_id_organizations"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_items")),
    )

    op.create_table(
        "equipment_movements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("performed_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["equipment_items.id"], name=op.f("fk_equipment_movements_item_id_equipment_items"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["performed_by"], ["users.id"], name=op.f("fk_equipment_movements_performed_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_movements")),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # CRM
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("company", sa.String(255), nullable=True),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="NEW"),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"], name=op.f("fk_leads_assigned_to_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
    )

    op.create_table(
        "proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name=op.f("fk_proposals_lead_id_leads"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_proposals_org_id_organizations"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_proposals_created_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], name=op.f("fk_proposals_approved_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposals")),
    )

    op.create_table(
        "proposal_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discount_percent", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"], name=op.f("fk_proposal_items_proposal_id_proposals"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proposal_items")),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # SUPPORT
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("priority", sa.String(20), nullable=False, server_default="MEDIUM"),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("establishment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("creative_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_tickets_created_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"], name=op.f("fk_tickets_assigned_to_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["establishment_id"], ["establishments.id"], name=op.f("fk_tickets_establishment_id_establishments"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["creative_id"], ["creatives.id"], name=op.f("fk_tickets_creative_id_creatives"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )

    op.create_table(
        "ticket_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], name=op.f("fk_ticket_messages_ticket_id_tickets"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], name=op.f("fk_ticket_messages_author_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_messages")),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # RBAC
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("module", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )

    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name=op.f("fk_role_permissions_role_id_roles"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["permission_id"], ["permissions.id"], name=op.f("fk_role_permissions_permission_id_permissions"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )

    op.create_table(
        "user_org_permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("extra_permissions", postgresql.JSONB(), nullable=True),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_org_permissions_user_id_users"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_user_org_permissions_org_id_organizations"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name=op.f("fk_user_org_permissions_role_id_roles"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], name=op.f("fk_user_org_permissions_granted_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_org_permissions")),
        sa.UniqueConstraint("user_id", "org_id", name="uq_user_org_permissions_user_org"),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # NOTIFICATIONS
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default="IN_APP"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_notifications_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("idempotency_key", name="uq_notifications_idempotency"),
    )
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"])

    op.create_table(
        "notification_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_notification_preferences_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.UniqueConstraint("user_id", "event_type", "channel", name="uq_notification_preferences_user_event_channel"),
    )


def downgrade() -> None:
    # Ordem inversa para respeitar dependências de FK
    op.drop_table("notification_preferences")
    op.drop_table("notifications")
    op.drop_table("user_org_permissions")
    op.drop_table("role_permissions")
    op.drop_table("roles")
    op.drop_table("permissions")
    op.drop_table("ticket_messages")
    op.drop_table("tickets")
    op.drop_table("proposal_items")
    op.drop_table("proposals")
    op.drop_table("leads")
    op.drop_table("equipment_movements")
    op.drop_table("equipment_items")
    op.drop_table("payouts")
    op.drop_table("commissions")
    op.drop_table("payment_orders")
    op.drop_table("ledger_entries")
    op.drop_table("player_manifests")
    op.drop_table("playback_events")
    op.drop_table("inventory_slots")
    op.drop_table("inventory_reservations")
    op.drop_table("campaign_targets")
    op.drop_table("campaign_schedules")
    op.drop_table("campaign_creatives")
    op.drop_table("campaigns")
    op.drop_table("creatives")
    op.drop_table("media_files")
    op.drop_table("pairing_requests")
    op.drop_constraint("fk_devices_screen_id_screens", "devices", type_="foreignkey")
    op.drop_table("screens")
    op.drop_table("devices")
    op.drop_table("establishments")
    op.drop_table("establishment_categories")
    op.drop_table("user_invites")
    op.drop_table("organization_users")
    op.drop_table("organization_roles")
    op.drop_table("organizations")
    op.drop_table("audit_logs")
    op.drop_table("user_sessions")
    op.drop_table("users")
