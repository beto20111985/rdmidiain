"""auth_rbac_audit_foundation — AÇÃO 002 / W1

Fundação de autenticação, RBAC com escopos, auditoria append-only com cadeia de
hash, configurações de negócio e LGPD.

Gerada com ``alembic revision --autogenerate`` contra banco descartável em
``d058863a802f`` e REVISADA manualmente:
- guardas de dados (aborta se tabelas legadas que serão reestruturadas tiverem
  linhas que não podem ser migradas automaticamente);
- catálogo RBAC legado (códigos ``recurso:acao`` do seed antigo, nunca atribuídos)
  substituído pelo catálogo do contrato §3 (``app.core.rbac_sync``);
- ``users.is_super_admin`` migrado para ``role_assignments`` (super_admin GLOBAL);
- sessões legadas (tokens sem ``sid``) revogadas;
- segredos TOTP em claro descartados (2FA desativado; perfis com 2FA obrigatório
  reconfiguram no próximo login);
- DDL extra de ``app.db.ddl``: ``rd_forbid_mutation``, cadeia de hash da auditoria
  e triggers append-only em audit_logs, system_setting_history, consent_records;
- tabelas legadas de vínculo (organization_roles, organization_users,
  user_org_permissions) removidas — substituídas por role_assignments.

Revision ID: 0003
Revises: d058863a802f
Create Date: 2026-10-07
"""
from typing import Sequence, Union

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db import ddl

revision: str = "0003"
down_revision: Union[str, None] = "d058863a802f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MY_APPEND_ONLY_TABLES = ("audit_logs", "system_setting_history", "consent_records")


def _count(table: str) -> int:
    return op.get_bind().execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()


def _guard_empty(*tables: str) -> None:
    for t in tables:
        n = _count(t)
        if n:
            raise RuntimeError(
                f"0003: a tabela legada '{t}' tem {n} linha(s) que não podem ser migradas automaticamente. "
                "Exporte/trate os dados manualmente antes do upgrade."
            )


def upgrade() -> None:
    # ─── 0. Guardas: tabelas reestruturadas sem caminho automático de dados ─────
    _guard_empty("organization_users", "organization_roles", "user_org_permissions", "user_invites", "audit_logs")

    # ─── 1. Funções base (sem dependência de tabelas) ───────────────────────────
    # (lista explícita e congelada: funções adicionadas depois em ddl.base_functions()
    # entram por migrations novas)
    for stmt in (ddl.FORBID_MUTATION_FN, ddl.AUDIT_SEQUENCE):
        op.execute(stmt)

    # ─── 2. Remove o modelo de vínculo legado ───────────────────────────────────
    op.drop_index("ix_organization_users_org_id", table_name="organization_users")
    op.drop_index("ix_organization_users_user_id", table_name="organization_users")
    op.drop_table("organization_users")
    op.drop_index("ix_organization_roles_name", table_name="organization_roles")
    op.drop_table("organization_roles")
    op.drop_index("ix_user_org_permissions_org_id", table_name="user_org_permissions")
    op.drop_index("ix_user_org_permissions_role_id", table_name="user_org_permissions")
    op.drop_index("ix_user_org_permissions_user_id", table_name="user_org_permissions")
    op.drop_table("user_org_permissions")

    # ─── 3. RBAC: catálogo legado substituído; roles ganham slug ────────────────
    op.execute("DELETE FROM role_permissions")
    op.execute("DELETE FROM roles")
    op.execute("DELETE FROM permissions")
    op.drop_index("ix_role_permissions_role_id", table_name="role_permissions")  # redundante com a PK
    op.drop_constraint("uq_role_permissions_role_permission", "role_permissions", type_="unique")  # idem
    op.add_column("roles", sa.Column("slug", sa.String(length=50), nullable=False))
    op.add_column("roles", sa.Column("mfa_required", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("roles", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False))
    op.add_column("roles", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False))
    op.alter_column("roles", "is_system", existing_type=sa.BOOLEAN(), server_default=sa.text("false"), existing_nullable=False)
    op.create_unique_constraint("uq_roles_slug", "roles", ["slug"])

    op.create_table(
        "role_assignments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.UUID(), nullable=True),
        sa.Column("scope_value", sa.String(length=120), nullable=True),
        sa.Column("granted_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(scope_type = 'GLOBAL' AND scope_id IS NULL AND scope_value IS NULL) OR (scope_type IN ('ORGANIZATION','ESTABLISHMENT') AND scope_id IS NOT NULL AND scope_value IS NULL) OR (scope_type = 'CITY' AND scope_id IS NULL AND scope_value IS NOT NULL)",
            name=op.f("ck_role_assignments_scope_consistent"),
        ),
        sa.CheckConstraint("scope_type IN ('GLOBAL','ORGANIZATION','ESTABLISHMENT','CITY')", name=op.f("ck_role_assignments_scope_type_valid")),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], name=op.f("fk_role_assignments_granted_by_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name=op.f("fk_role_assignments_role_id_roles"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_role_assignments_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_assignments")),
        sa.UniqueConstraint("user_id", "role_id", "scope_type", "scope_id", "scope_value", name="uq_role_assignments_user_role_scope", postgresql_nulls_not_distinct=True),
    )
    op.create_index("ix_role_assignments_scope", "role_assignments", ["scope_type", "scope_id"], unique=False)
    op.create_index("ix_role_assignments_user_id", "role_assignments", ["user_id"], unique=False)

    # catálogo do contrato §3 (idempotente; também via CLI sync-permissions)
    from app.core.rbac_sync import sync_catalog_sync

    sync_catalog_sync(op.get_bind())

    # is_super_admin → atribuição super_admin GLOBAL
    op.execute(
        "INSERT INTO role_assignments (id, user_id, role_id, scope_type, created_at) "
        "SELECT gen_random_uuid(), u.id, r.id, 'GLOBAL', now() FROM users u JOIN roles r ON r.slug = 'super_admin' "
        "WHERE u.is_super_admin"
    )

    # ─── 4. users ───────────────────────────────────────────────────────────────
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("totp_secret_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("totp_pending_secret_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("admin_master_seat", sa.SmallInteger(), nullable=True))
    op.add_column("users", sa.Column("theme", sa.String(length=10), server_default=sa.text("'system'"), nullable=False))
    op.add_column("users", sa.Column("financial_password_hash", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("financial_password_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True))
    # restaura server defaults removidos por d058863a802f (A2 E14)
    op.alter_column("users", "is_active", existing_type=sa.BOOLEAN(), server_default=sa.text("true"), existing_nullable=False)
    op.alter_column("users", "is_verified", existing_type=sa.BOOLEAN(), server_default=sa.text("false"), existing_nullable=False)
    op.alter_column("users", "totp_enabled", existing_type=sa.BOOLEAN(), server_default=sa.text("false"), existing_nullable=False)
    op.alter_column("users", "failed_login_attempts", existing_type=sa.INTEGER(), server_default=sa.text("0"), existing_nullable=False)
    op.create_unique_constraint("uq_users_admin_master_seat", "users", ["admin_master_seat"])
    op.create_check_constraint(op.f("ck_users_admin_master_seat_valid"), "users", "admin_master_seat IN (1, 2)")
    op.create_check_constraint(op.f("ck_users_theme_valid"), "users", "theme IN ('light', 'dark', 'system')")
    op.create_index("uq_users_phone", "users", ["phone"], unique=True, postgresql_where=sa.text("phone IS NOT NULL"))
    # TOTP em claro é descartado (nunca migrar segredo exposto); 2FA reconfigurado
    op.execute("UPDATE users SET totp_enabled = false WHERE totp_secret IS NOT NULL OR totp_enabled")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "is_super_admin")

    # ─── 5. user_sessions (sessão = família de refresh) ─────────────────────────
    op.add_column("user_sessions", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False))
    op.add_column("user_sessions", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_sessions", sa.Column("revoked_reason", sa.String(length=50), nullable=True))
    op.add_column("user_sessions", sa.Column("device_label", sa.String(length=120), nullable=True))
    op.add_column("user_sessions", sa.Column("amr", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column("user_sessions", sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_sessions", sa.Column("impersonator_user_id", sa.UUID(), nullable=True))
    op.add_column("user_sessions", sa.Column("impersonation_reason", sa.Text(), nullable=True))
    op.add_column("user_sessions", sa.Column("parent_session_id", sa.UUID(), nullable=True))
    op.alter_column("user_sessions", "ip_address", existing_type=sa.VARCHAR(length=50), type_=sa.String(length=64), existing_nullable=True)
    # sessões legadas (JWT sem sid / refresh sem rotação) são encerradas
    op.execute("UPDATE user_sessions SET revoked_at = now(), revoked_reason = 'legacy_0003' WHERE revoked_at IS NULL")
    op.drop_index("ix_user_sessions_token_hash", table_name="user_sessions")
    op.drop_constraint("uq_user_sessions_refresh_token_hash", "user_sessions", type_="unique")
    op.drop_constraint("uq_user_sessions_token_hash", "user_sessions", type_="unique")
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"], unique=False)
    op.create_foreign_key(op.f("fk_user_sessions_parent_session_id_user_sessions"), "user_sessions", "user_sessions", ["parent_session_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_user_sessions_impersonator_user_id_users"), "user_sessions", "users", ["impersonator_user_id"], ["id"], ondelete="RESTRICT")
    op.drop_column("user_sessions", "token_hash")
    op.drop_column("user_sessions", "refresh_token_hash")

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["user_sessions.id"], name=op.f("fk_refresh_tokens_session_id_user_sessions"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"], unique=False)

    op.create_table(
        "user_recovery_codes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_recovery_codes_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_recovery_codes")),
        sa.UniqueConstraint("user_id", "code_hash", name="uq_user_recovery_codes_user_code"),
    )
    op.create_index("ix_user_recovery_codes_user_id", "user_recovery_codes", ["user_id"], unique=False)

    op.create_table(
        "password_reset_codes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("request_ip", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_password_reset_codes_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_codes")),
    )
    op.create_index("ix_password_reset_codes_user_id", "password_reset_codes", ["user_id"], unique=False)

    # ─── 6. user_invites (token só como hash; perfil + escopo) — tabela vazia ───
    op.add_column("user_invites", sa.Column("token_hash", sa.String(length=64), nullable=False))
    op.add_column("user_invites", sa.Column("scope_type", sa.String(length=20), nullable=False))
    op.add_column("user_invites", sa.Column("scope_id", sa.UUID(), nullable=True))
    op.add_column("user_invites", sa.Column("scope_value", sa.String(length=120), nullable=True))
    op.add_column("user_invites", sa.Column("accepted_user_id", sa.UUID(), nullable=True))
    op.add_column("user_invites", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("user_invites", "role_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("user_invites", "created_at", existing_type=postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), existing_nullable=False)
    op.drop_index("ix_user_invites_token", table_name="user_invites")
    op.drop_constraint("uq_user_invites_token", "user_invites", type_="unique")
    op.create_unique_constraint("uq_user_invites_token_hash", "user_invites", ["token_hash"])
    op.drop_constraint("fk_user_invites_org_id_organizations", "user_invites", type_="foreignkey")
    op.create_foreign_key(op.f("fk_user_invites_role_id_roles"), "user_invites", "roles", ["role_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_user_invites_accepted_user_id_users"), "user_invites", "users", ["accepted_user_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint(op.f("ck_user_invites_scope_type_valid"), "user_invites", "scope_type IN ('GLOBAL','ORGANIZATION','ESTABLISHMENT','CITY')")
    op.drop_column("user_invites", "org_id")
    op.drop_column("user_invites", "token")

    # ─── 7. audit_logs (append-only + cadeia de hash) — tabela vazia ───────────
    op.add_column("audit_logs", sa.Column("seq", sa.BigInteger(), nullable=False))
    op.add_column("audit_logs", sa.Column("prev_hash", sa.String(length=64), nullable=False))
    op.add_column("audit_logs", sa.Column("row_hash", sa.String(length=64), nullable=False))
    op.add_column("audit_logs", sa.Column("actor_user_id", sa.UUID(), nullable=True))
    op.add_column("audit_logs", sa.Column("real_user_id", sa.UUID(), nullable=True))
    op.add_column("audit_logs", sa.Column("actor_device_id", sa.UUID(), nullable=True))
    op.add_column("audit_logs", sa.Column("org_id", sa.UUID(), nullable=True))
    op.add_column("audit_logs", sa.Column("user_agent", sa.String(length=500), nullable=True))
    op.add_column("audit_logs", sa.Column("origin", sa.String(length=20), nullable=False))
    op.add_column("audit_logs", sa.Column("request_id", sa.String(length=64), nullable=True))
    op.alter_column("audit_logs", "created_at", existing_type=postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), existing_nullable=False)
    op.alter_column("audit_logs", "ip_address", existing_type=sa.VARCHAR(length=50), type_=sa.String(length=64), existing_nullable=True)
    op.drop_index("ix_audit_logs_user_id", table_name="audit_logs")
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"], unique=False)
    op.create_index("ix_audit_logs_org_id", "audit_logs", ["org_id"], unique=False)
    op.create_index("uq_audit_logs_seq", "audit_logs", ["seq"], unique=True)
    op.drop_constraint("fk_audit_logs_impersonator_id_users", "audit_logs", type_="foreignkey")
    op.drop_constraint("fk_audit_logs_user_id_users", "audit_logs", type_="foreignkey")
    op.create_foreign_key(op.f("fk_audit_logs_actor_user_id_users"), "audit_logs", "users", ["actor_user_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_audit_logs_real_user_id_users"), "audit_logs", "users", ["real_user_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint(op.f("ck_audit_logs_origin_valid"), "audit_logs", "origin IN ('api','worker','player','system','cli')")
    op.drop_column("audit_logs", "user_id")
    op.drop_column("audit_logs", "impersonator_id")

    # ─── 8. Configurações e LGPD ────────────────────────────────────────────────
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name=op.f("fk_system_settings_updated_by_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_settings")),
    )
    op.create_table(
        "system_setting_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("changed_by", sa.UUID(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"], name=op.f("fk_system_setting_history_changed_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_setting_history")),
    )
    op.create_index("ix_system_setting_history_key", "system_setting_history", ["key", "version"], unique=False)

    op.create_table(
        "consent_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("document", sa.String(length=20), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.CheckConstraint("action IN ('accepted','revoked')", name=op.f("ck_consent_records_action_valid")),
        sa.CheckConstraint("document IN ('terms','privacy','cookies','marketing')", name=op.f("ck_consent_records_document_valid")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_consent_records_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_records")),
    )
    op.create_index("ix_consent_records_user_id", "consent_records", ["user_id", "document"], unique=False)

    op.create_table(
        "data_deletion_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_by", sa.UUID(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('PENDING','COMPLETED','REJECTED','CANCELLED')", name=op.f("ck_data_deletion_requests_status_valid")),
        sa.ForeignKeyConstraint(["processed_by"], ["users.id"], name=op.f("fk_data_deletion_requests_processed_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_data_deletion_requests_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_deletion_requests")),
    )
    op.create_index("ix_data_deletion_requests_status", "data_deletion_requests", ["status"], unique=False)
    op.create_index("uq_data_deletion_requests_pending_user", "data_deletion_requests", ["user_id"], unique=True, postgresql_where=sa.text("status = 'PENDING'"))

    # ─── 9. DDL: cadeia de hash da auditoria + append-only ──────────────────────
    for stmt in ddl.audit_chain():
        op.execute(stmt)
    for t in MY_APPEND_ONLY_TABLES:
        for stmt in ddl.append_only_trigger(t):
            op.execute(stmt)


def downgrade() -> None:
    # ATENÇÃO: o downgrade preserva usuários/sessões/auditoria (aproximação do formato
    # antigo), mas descarta: catálogo RBAC novo (o legado NÃO é restaurado), convites,
    # códigos de reset/recuperação, refresh tokens, configurações, consentimentos e
    # pedidos LGPD. Sessões são revogadas.
    for t in MY_APPEND_ONLY_TABLES:
        for stmt in ddl.drop_append_only_trigger(t):
            op.execute(stmt)
    for stmt in ddl.drop_audit_chain():
        op.execute(stmt)

    op.drop_index("uq_data_deletion_requests_pending_user", table_name="data_deletion_requests", postgresql_where=sa.text("status = 'PENDING'"))
    op.drop_index("ix_data_deletion_requests_status", table_name="data_deletion_requests")
    op.drop_table("data_deletion_requests")
    op.drop_index("ix_consent_records_user_id", table_name="consent_records")
    op.drop_table("consent_records")
    op.drop_index("ix_system_setting_history_key", table_name="system_setting_history")
    op.drop_table("system_setting_history")
    op.drop_table("system_settings")

    # audit_logs → formato antigo (actor→user_id, real→impersonator_id)
    op.add_column("audit_logs", sa.Column("impersonator_id", sa.UUID(), autoincrement=False, nullable=True))
    op.add_column("audit_logs", sa.Column("user_id", sa.UUID(), autoincrement=False, nullable=True))
    op.execute("UPDATE audit_logs SET user_id = actor_user_id, impersonator_id = real_user_id")
    op.drop_constraint(op.f("ck_audit_logs_origin_valid"), "audit_logs", type_="check")
    op.drop_constraint(op.f("fk_audit_logs_real_user_id_users"), "audit_logs", type_="foreignkey")
    op.drop_constraint(op.f("fk_audit_logs_actor_user_id_users"), "audit_logs", type_="foreignkey")
    op.create_foreign_key("fk_audit_logs_user_id_users", "audit_logs", "users", ["user_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_audit_logs_impersonator_id_users", "audit_logs", "users", ["impersonator_id"], ["id"], ondelete="RESTRICT")
    op.drop_index("uq_audit_logs_seq", table_name="audit_logs")
    op.drop_index("ix_audit_logs_org_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_user_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"], unique=False)
    op.alter_column("audit_logs", "ip_address", existing_type=sa.String(length=64), type_=sa.VARCHAR(length=50), existing_nullable=True,
                    postgresql_using="left(ip_address, 50)")
    op.alter_column("audit_logs", "created_at", existing_type=postgresql.TIMESTAMP(timezone=True), server_default=None, existing_nullable=False)
    for col in ("request_id", "origin", "user_agent", "org_id", "actor_device_id", "real_user_id", "actor_user_id", "row_hash", "prev_hash", "seq"):
        op.drop_column("audit_logs", col)

    # user_invites → formato antigo
    op.add_column("user_invites", sa.Column("token", sa.VARCHAR(length=255), autoincrement=False, nullable=True))
    op.add_column("user_invites", sa.Column("org_id", sa.UUID(), autoincrement=False, nullable=True))
    op.execute("UPDATE user_invites SET token = token_hash, org_id = CASE WHEN scope_type = 'ORGANIZATION' THEN scope_id END")
    op.alter_column("user_invites", "token", existing_type=sa.VARCHAR(length=255), nullable=False)
    op.drop_constraint(op.f("ck_user_invites_scope_type_valid"), "user_invites", type_="check")
    op.drop_constraint(op.f("fk_user_invites_accepted_user_id_users"), "user_invites", type_="foreignkey")
    op.drop_constraint(op.f("fk_user_invites_role_id_roles"), "user_invites", type_="foreignkey")
    op.create_foreign_key("fk_user_invites_org_id_organizations", "user_invites", "organizations", ["org_id"], ["id"], ondelete="RESTRICT")
    op.drop_constraint("uq_user_invites_token_hash", "user_invites", type_="unique")
    op.create_unique_constraint("uq_user_invites_token", "user_invites", ["token"])
    op.create_index("ix_user_invites_token", "user_invites", ["token"], unique=False)
    op.alter_column("user_invites", "created_at", existing_type=postgresql.TIMESTAMP(timezone=True), server_default=None, existing_nullable=False)
    op.alter_column("user_invites", "role_id", existing_type=sa.UUID(), nullable=True)
    for col in ("revoked_at", "accepted_user_id", "scope_value", "scope_id", "scope_type", "token_hash"):
        op.drop_column("user_invites", col)
    op.execute("UPDATE user_invites SET role_id = NULL")  # perfis novos não existem no formato antigo

    op.drop_index("ix_password_reset_codes_user_id", table_name="password_reset_codes")
    op.drop_table("password_reset_codes")
    op.drop_index("ix_user_recovery_codes_user_id", table_name="user_recovery_codes")
    op.drop_table("user_recovery_codes")
    op.drop_index("ix_refresh_tokens_session_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    # user_sessions → formato antigo (todas revogadas; token_hash sintético único)
    op.add_column("user_sessions", sa.Column("refresh_token_hash", sa.VARCHAR(length=255), autoincrement=False, nullable=True))
    op.add_column("user_sessions", sa.Column("token_hash", sa.VARCHAR(length=255), autoincrement=False, nullable=True))
    op.execute("UPDATE user_sessions SET token_hash = 'downgraded-' || id::text, revoked_at = COALESCE(revoked_at, now())")
    op.alter_column("user_sessions", "token_hash", existing_type=sa.VARCHAR(length=255), nullable=False)
    op.drop_constraint(op.f("fk_user_sessions_impersonator_user_id_users"), "user_sessions", type_="foreignkey")
    op.drop_constraint(op.f("fk_user_sessions_parent_session_id_user_sessions"), "user_sessions", type_="foreignkey")
    op.drop_index("ix_user_sessions_expires_at", table_name="user_sessions")
    op.create_unique_constraint("uq_user_sessions_token_hash", "user_sessions", ["token_hash"])
    op.create_unique_constraint("uq_user_sessions_refresh_token_hash", "user_sessions", ["refresh_token_hash"])
    op.create_index("ix_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=False)
    op.alter_column("user_sessions", "ip_address", existing_type=sa.String(length=64), type_=sa.VARCHAR(length=50), existing_nullable=True,
                    postgresql_using="left(ip_address, 50)")
    for col in ("parent_session_id", "impersonation_reason", "impersonator_user_id", "mfa_verified_at", "amr", "device_label",
                "revoked_reason", "last_seen_at", "created_at"):
        op.drop_column("user_sessions", col)

    # users → formato antigo (is_super_admin a partir das atribuições)
    op.add_column("users", sa.Column("totp_secret", sa.VARCHAR(length=255), autoincrement=False, nullable=True))
    op.add_column("users", sa.Column("is_super_admin", sa.BOOLEAN(), autoincrement=False, nullable=False, server_default=sa.false()))
    op.execute(
        "UPDATE users SET is_super_admin = true WHERE id IN (SELECT ra.user_id FROM role_assignments ra "
        "JOIN roles r ON r.id = ra.role_id WHERE r.slug = 'super_admin' AND ra.scope_type = 'GLOBAL')"
    )
    op.alter_column("users", "is_super_admin", server_default=None)
    op.execute("UPDATE users SET totp_enabled = false")
    op.drop_index("uq_users_phone", table_name="users", postgresql_where=sa.text("phone IS NOT NULL"))
    op.drop_constraint(op.f("ck_users_theme_valid"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_admin_master_seat_valid"), "users", type_="check")
    op.drop_constraint("uq_users_admin_master_seat", "users", type_="unique")
    for col in ("failed_login_attempts", "totp_enabled", "is_verified", "is_active"):
        op.alter_column("users", col, server_default=None)
    for col in ("anonymized_at", "financial_password_changed_at", "financial_password_hash", "theme", "admin_master_seat",
                "totp_enabled_at", "totp_last_step", "totp_pending_secret_enc", "totp_secret_enc", "password_changed_at",
                "must_change_password"):
        op.drop_column("users", col)

    # RBAC → formato antigo (catálogo novo descartado; legado não restaurado)
    op.drop_index("ix_role_assignments_user_id", table_name="role_assignments")
    op.drop_index("ix_role_assignments_scope", table_name="role_assignments")
    op.drop_table("role_assignments")
    op.execute("DELETE FROM role_permissions")
    op.execute("DELETE FROM roles")
    op.execute("DELETE FROM permissions")
    op.drop_constraint("uq_roles_slug", "roles", type_="unique")
    op.alter_column("roles", "is_system", existing_type=sa.BOOLEAN(), server_default=None, existing_nullable=False)
    for col in ("updated_at", "created_at", "mfa_required", "slug"):
        op.drop_column("roles", col)
    op.create_unique_constraint("uq_role_permissions_role_permission", "role_permissions", ["role_id", "permission_id"])
    op.create_index("ix_role_permissions_role_id", "role_permissions", ["role_id"], unique=False)

    op.create_table(
        "organization_roles",
        sa.Column("id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("name", sa.VARCHAR(length=100), autoincrement=False, nullable=False),
        sa.Column("is_system", sa.BOOLEAN(), autoincrement=False, nullable=False),
        sa.Column("description", sa.VARCHAR(length=500), autoincrement=False, nullable=True),
        sa.Column("org_id", sa.UUID(), autoincrement=False, nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name="fk_organization_roles_org_id_organizations", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_organization_roles"),
    )
    op.create_index("ix_organization_roles_name", "organization_roles", ["name"], unique=False)
    op.create_table(
        "organization_users",
        sa.Column("id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("org_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("role_id", sa.UUID(), autoincrement=False, nullable=True),
        sa.Column("is_primary_org", sa.BOOLEAN(), autoincrement=False, nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name="fk_organization_users_org_id_organizations", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["organization_roles.id"], name="fk_organization_users_role_id_organization_roles", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_organization_users_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_organization_users"),
        sa.UniqueConstraint("org_id", "user_id", name="uq_organization_users_org_user"),
    )
    op.create_index("ix_organization_users_user_id", "organization_users", ["user_id"], unique=False)
    op.create_index("ix_organization_users_org_id", "organization_users", ["org_id"], unique=False)
    op.create_table(
        "user_org_permissions",
        sa.Column("id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("org_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("role_id", sa.UUID(), autoincrement=False, nullable=True),
        sa.Column("extra_permissions", postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True),
        sa.Column("granted_by", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], name="fk_user_org_permissions_granted_by_users", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name="fk_user_org_permissions_org_id_organizations", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name="fk_user_org_permissions_role_id_roles", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_org_permissions_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_user_org_permissions"),
        sa.UniqueConstraint("user_id", "org_id", name="uq_user_org_permissions_user_org"),
    )
    op.create_index("ix_user_org_permissions_user_id", "user_org_permissions", ["user_id"], unique=False)
    op.create_index("ix_user_org_permissions_role_id", "user_org_permissions", ["role_id"], unique=False)
    op.create_index("ix_user_org_permissions_org_id", "user_org_permissions", ["org_id"], unique=False)

    op.execute("DROP FUNCTION IF EXISTS rd_forbid_mutation()")
    op.execute("DROP SEQUENCE IF EXISTS rd_audit_seq")
