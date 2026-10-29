"""master_credential_seal — AÇÃO 003 / agente E (D1 do revisor I: Master-fantoche pelo papel de runtime)

Defeito (MÉDIA, sem detecção): o ``rd_app`` SEM a chave da aplicação conseguia ocupar um assento de Master:
(a) INSERT em ``admin_master_invites`` com ``token_hash`` escolhido (``invited_by`` = Master ativo, assento 2) e
ativação pela API com os fatores do atacante; (b) UPDATE de ``users.hashed_password``/``must_change_password`` de um
titular (ex.: assento 1 pendente de primeiro acesso) → login → setup com os fatores do atacante. Em DUAL isso
derruba a dupla aprovação.

Correção (banco = defesa em profundidade; quem CONFERE o HMAC é a aplicação, agente B):
- ``admin_master_invites.seal`` (TEXT NULL) e ``users.master_credential_seal`` (TEXT NULL): selos HMAC calculados
  pela aplicação com a chave (o banco não conhece a chave e não confere o HMAC);
- ``rd_admin_master_invite_guard`` (papel de runtime): INSERT exige ``seal``; ``token_hash`` e ``seal`` só mudam
  JUNTOS (reenvio); ``seal`` nunca volta a NULL ⇒ ``RD_MASTER_INVITE_SEAL_REQUIRED``;
- ``rd_users_master_guard`` (papel de runtime, titular de assento): ``master_credential_seal`` nunca volta a NULL; UPDATE
  que mude ``hashed_password``, ``must_change_password``, ``totp_enabled``, ``totp_secret_enc``,
  ``financial_password_hash``, ``email``, ``is_active`` ou ``admin_master_seat`` precisa trocar o selo por um valor NÃO
  nulo e DIFERENTE ⇒ senão ``RD_MASTER_CREDENTIAL_SEAL_REQUIRED`` (força rastro; selo lixo é recusado pela aplicação
  no login/ativação e por ``verify_integrity``);
- ``rd_users_master_log``: ``master_credential_seal`` entra na lista de colunas registradas (``RAISE LOG``);
- dono das tabelas/superusuário (funções SECURITY DEFINER, migrations, testes) continuam isentos, como nos demais guards
  de titular: ``rd_security_assign_master_seat``/``rd_security_recover_master`` inalteradas — a aplicação re-sela na
  mesma transação, logo em seguida (UPDATE só do selo, permitido ao papel de runtime).

Dados: convites PENDING sem selo ⇒ REVOKED (``revoke_reason='migration_0009'``; auditoria
``migration.0009.invite_revoked_unsealed`` por convite, sem e-mail). Titulares ficam com selo NULL até a ADOÇÃO pela
aplicação (``bootstrap-instance`` sela uma vez, auditado — agente B); resumo em ``migration.0009.credential_seal_added``.

Corpos CONGELADOS (lição do D3): a 0009 aplica ``FUNCTIONS_0009`` literal; o downgrade restaura os corpos da 0008
(``FUNCTIONS_0008_RESTORE``) e remove as colunas. Irreversível no downgrade: os selos gravados (descartados) e os
convites revogados pela 0009 (continuam REVOKED). Auditoria ``migration.0009.downgrade``.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REVOKE_REASON_0009 = "migration_0009: convite sem selo da aplicação (D1)"

# ─── Corpos aplicados pela 0009, CONGELADOS (NÃO editar; vigentes no código: security.ddl.*_V3) ──────────
FUNCTIONS_0009: tuple[str, ...] = (
    """
CREATE OR REPLACE FUNCTION rd_users_master_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF public.rd_security_privileged() THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.admin_master_seat IS NOT NULL THEN
            RAISE EXCEPTION 'RD_MASTER_SEAT_PROTECTED: assento de Admin Master só é atribuído pelas funções de segurança'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.admin_master_seat IS NOT NULL THEN
            RAISE EXCEPTION 'RD_MASTER_PROTECTED: titular de assento de Admin Master não pode ser apagado'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.admin_master_seat IS DISTINCT FROM OLD.admin_master_seat THEN
        RAISE EXCEPTION 'RD_MASTER_SEAT_PROTECTED: assento de Admin Master só muda pelas funções de segurança'
            USING ERRCODE = 'P0001';
    END IF;
    IF OLD.admin_master_seat IS NOT NULL THEN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.email IS DISTINCT FROM OLD.email
           OR NEW.is_active IS DISTINCT FROM OLD.is_active
           OR NEW.anonymized_at IS DISTINCT FROM OLD.anonymized_at
           OR NEW.master_recovered_at IS DISTINCT FROM OLD.master_recovered_at THEN
            RAISE EXCEPTION 'RD_MASTER_PROTECTED: identidade/ativação do Admin Master só muda pelas funções de segurança'
                USING ERRCODE = 'P0001';
        END IF;
        IF OLD.totp_enabled AND (NOT NEW.totp_enabled OR NEW.totp_secret_enc IS DISTINCT FROM OLD.totp_secret_enc) THEN
            RAISE EXCEPTION 'RD_MASTER_TOTP_PROTECTED: TOTP do Admin Master não pode ser desligado nem trocado'
                USING ERRCODE = 'P0001';
        END IF;
        IF OLD.financial_password_hash IS NOT NULL AND NEW.financial_password_hash IS NULL THEN
            RAISE EXCEPTION 'RD_MASTER_FINANCIAL_PASSWORD_PROTECTED: senha financeira do Admin Master não pode ser removida'
                USING ERRCODE = 'P0001';
        END IF;
        -- 0009 (D1): selo de credenciais da aplicação — nunca volta a NULL e muda junto com hash/fatores
        IF OLD.master_credential_seal IS NOT NULL
           AND (NEW.master_credential_seal IS NULL OR btrim(NEW.master_credential_seal) = '') THEN
            RAISE EXCEPTION 'RD_MASTER_CREDENTIAL_SEAL_REQUIRED: selo de credenciais do Admin Master não pode ser removido'
                USING ERRCODE = 'P0001';
        END IF;
        IF (NEW.hashed_password IS DISTINCT FROM OLD.hashed_password
            OR NEW.must_change_password IS DISTINCT FROM OLD.must_change_password
            OR NEW.totp_enabled IS DISTINCT FROM OLD.totp_enabled
            OR NEW.totp_secret_enc IS DISTINCT FROM OLD.totp_secret_enc
            OR NEW.financial_password_hash IS DISTINCT FROM OLD.financial_password_hash
            OR NEW.email IS DISTINCT FROM OLD.email
            OR NEW.is_active IS DISTINCT FROM OLD.is_active
            OR NEW.admin_master_seat IS DISTINCT FROM OLD.admin_master_seat)
           AND (NEW.master_credential_seal IS NULL OR btrim(NEW.master_credential_seal) = ''
                OR NEW.master_credential_seal IS NOT DISTINCT FROM OLD.master_credential_seal) THEN
            RAISE EXCEPTION 'RD_MASTER_CREDENTIAL_SEAL_REQUIRED: mudança de credencial do Admin Master exige novo selo da aplicação'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_users_master_log() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    cols text[] := ARRAY[]::text[];
BEGIN
    IF NEW.hashed_password IS DISTINCT FROM OLD.hashed_password THEN cols := array_append(cols, 'hashed_password'); END IF;
    IF NEW.must_change_password IS DISTINCT FROM OLD.must_change_password THEN cols := array_append(cols, 'must_change_password'); END IF;
    IF NEW.totp_enabled IS DISTINCT FROM OLD.totp_enabled THEN cols := array_append(cols, 'totp_enabled'); END IF;
    IF NEW.totp_secret_enc IS DISTINCT FROM OLD.totp_secret_enc THEN cols := array_append(cols, 'totp_secret_enc'); END IF;
    IF NEW.financial_password_hash IS DISTINCT FROM OLD.financial_password_hash THEN cols := array_append(cols, 'financial_password_hash'); END IF;
    IF NEW.email IS DISTINCT FROM OLD.email THEN cols := array_append(cols, 'email'); END IF;
    IF NEW.phone IS DISTINCT FROM OLD.phone THEN cols := array_append(cols, 'phone'); END IF;
    IF NEW.is_active IS DISTINCT FROM OLD.is_active THEN cols := array_append(cols, 'is_active'); END IF;
    IF NEW.anonymized_at IS DISTINCT FROM OLD.anonymized_at THEN cols := array_append(cols, 'anonymized_at'); END IF;
    IF NEW.admin_master_seat IS DISTINCT FROM OLD.admin_master_seat THEN cols := array_append(cols, 'admin_master_seat'); END IF;
    IF NEW.master_recovered_at IS DISTINCT FROM OLD.master_recovered_at THEN cols := array_append(cols, 'master_recovered_at'); END IF;
    IF NEW.master_credential_seal IS DISTINCT FROM OLD.master_credential_seal THEN cols := array_append(cols, 'master_credential_seal'); END IF;
    IF cardinality(cols) > 0 THEN
        RAISE LOG 'RD_SECURITY_EVENT master_user_changed user=% seat=% columns=% by=% app=% addr=%',
            NEW.id, COALESCE(NEW.admin_master_seat, OLD.admin_master_seat), array_to_string(cols, ','), session_user, current_setting('application_name', true), inet_client_addr();
    END IF;
    RETURN NULL;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_admin_master_invite_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: convite de Admin Master não pode ser apagado' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'INSERT' THEN
        -- 0009 (D1): convite do papel de runtime exige o selo da aplicação (conferido na ativação)
        IF NOT public.rd_security_privileged() AND (NEW.seal IS NULL OR btrim(NEW.seal) = '') THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_SEAL_REQUIRED: convite exige o selo da aplicação' USING ERRCODE = 'P0001';
        END IF;
        IF NEW.status IS DISTINCT FROM 'PENDING' THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite nasce PENDING' USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (SELECT 1 FROM public.users x WHERE x.admin_master_seat = NEW.seat) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: assento % ocupado', NEW.seat USING ERRCODE = 'P0001';
        END IF;
        IF NOT public.rd_is_active_master(NEW.invited_by) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convidante não é Admin Master ativo' USING ERRCODE = 'P0001';
        END IF;
        IF public.rd_security_current_approval_mode() IS DISTINCT FROM 'DUAL' THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite de 2º Master só no modo DUAL' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.seat IS DISTINCT FROM OLD.seat OR NEW.email IS DISTINCT FROM OLD.email
       OR NEW.full_name IS DISTINCT FROM OLD.full_name OR NEW.invited_by IS DISTINCT FROM OLD.invited_by
       OR NEW.activation_user_id IS DISTINCT FROM OLD.activation_user_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'RD_MASTER_INVITE_IMMUTABLE: identidade do convite é imutável' USING ERRCODE = 'P0001';
    END IF;
    -- 0009 (D1): token e selo só mudam JUNTOS (reenvio); selo nunca volta a NULL
    IF NOT public.rd_security_privileged()
       AND ((NEW.token_hash IS DISTINCT FROM OLD.token_hash) <> (NEW.seal IS DISTINCT FROM OLD.seal)
            OR (OLD.seal IS NOT NULL AND (NEW.seal IS NULL OR btrim(NEW.seal) = ''))) THEN
        RAISE EXCEPTION 'RD_MASTER_INVITE_SEAL_REQUIRED: token e selo do convite só mudam juntos (reenvio)'
            USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status <> 'PENDING' THEN
        IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite % é final', OLD.status USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NEW.status NOT IN ('ACCEPTED','REVOKED','EXPIRED','LOCKED') THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: transição PENDING → % proibida', NEW.status
                USING ERRCODE = 'P0001';
        END IF;
        IF NEW.status = 'ACCEPTED' AND NOT public.rd_security_privileged() THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: aceite só pela função de ativação do Admin Master'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
""",
)

# ─── Corpos da 0008 dessas 3 funções, CONGELADOS (restaurados no downgrade) ───────────────────────────
FUNCTIONS_0008_RESTORE: tuple[str, ...] = (
    """
CREATE OR REPLACE FUNCTION rd_users_master_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF public.rd_security_privileged() THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.admin_master_seat IS NOT NULL THEN
            RAISE EXCEPTION 'RD_MASTER_SEAT_PROTECTED: assento de Admin Master só é atribuído pelas funções de segurança'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.admin_master_seat IS NOT NULL THEN
            RAISE EXCEPTION 'RD_MASTER_PROTECTED: titular de assento de Admin Master não pode ser apagado'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.admin_master_seat IS DISTINCT FROM OLD.admin_master_seat THEN
        RAISE EXCEPTION 'RD_MASTER_SEAT_PROTECTED: assento de Admin Master só muda pelas funções de segurança'
            USING ERRCODE = 'P0001';
    END IF;
    IF OLD.admin_master_seat IS NOT NULL THEN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.email IS DISTINCT FROM OLD.email
           OR NEW.is_active IS DISTINCT FROM OLD.is_active
           OR NEW.anonymized_at IS DISTINCT FROM OLD.anonymized_at
           OR NEW.master_recovered_at IS DISTINCT FROM OLD.master_recovered_at THEN
            RAISE EXCEPTION 'RD_MASTER_PROTECTED: identidade/ativação do Admin Master só muda pelas funções de segurança'
                USING ERRCODE = 'P0001';
        END IF;
        IF OLD.totp_enabled AND (NOT NEW.totp_enabled OR NEW.totp_secret_enc IS DISTINCT FROM OLD.totp_secret_enc) THEN
            RAISE EXCEPTION 'RD_MASTER_TOTP_PROTECTED: TOTP do Admin Master não pode ser desligado nem trocado'
                USING ERRCODE = 'P0001';
        END IF;
        IF OLD.financial_password_hash IS NOT NULL AND NEW.financial_password_hash IS NULL THEN
            RAISE EXCEPTION 'RD_MASTER_FINANCIAL_PASSWORD_PROTECTED: senha financeira do Admin Master não pode ser removida'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_users_master_log() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    cols text[] := ARRAY[]::text[];
BEGIN
    IF NEW.hashed_password IS DISTINCT FROM OLD.hashed_password THEN cols := array_append(cols, 'hashed_password'); END IF;
    IF NEW.must_change_password IS DISTINCT FROM OLD.must_change_password THEN cols := array_append(cols, 'must_change_password'); END IF;
    IF NEW.totp_enabled IS DISTINCT FROM OLD.totp_enabled THEN cols := array_append(cols, 'totp_enabled'); END IF;
    IF NEW.totp_secret_enc IS DISTINCT FROM OLD.totp_secret_enc THEN cols := array_append(cols, 'totp_secret_enc'); END IF;
    IF NEW.financial_password_hash IS DISTINCT FROM OLD.financial_password_hash THEN cols := array_append(cols, 'financial_password_hash'); END IF;
    IF NEW.email IS DISTINCT FROM OLD.email THEN cols := array_append(cols, 'email'); END IF;
    IF NEW.phone IS DISTINCT FROM OLD.phone THEN cols := array_append(cols, 'phone'); END IF;
    IF NEW.is_active IS DISTINCT FROM OLD.is_active THEN cols := array_append(cols, 'is_active'); END IF;
    IF NEW.anonymized_at IS DISTINCT FROM OLD.anonymized_at THEN cols := array_append(cols, 'anonymized_at'); END IF;
    IF NEW.admin_master_seat IS DISTINCT FROM OLD.admin_master_seat THEN cols := array_append(cols, 'admin_master_seat'); END IF;
    IF NEW.master_recovered_at IS DISTINCT FROM OLD.master_recovered_at THEN cols := array_append(cols, 'master_recovered_at'); END IF;
    IF cardinality(cols) > 0 THEN
        RAISE LOG 'RD_SECURITY_EVENT master_user_changed user=% seat=% columns=% by=% app=% addr=%',
            NEW.id, COALESCE(NEW.admin_master_seat, OLD.admin_master_seat), array_to_string(cols, ','), session_user, current_setting('application_name', true), inet_client_addr();
    END IF;
    RETURN NULL;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_admin_master_invite_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: convite de Admin Master não pode ser apagado' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status IS DISTINCT FROM 'PENDING' THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite nasce PENDING' USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (SELECT 1 FROM public.users x WHERE x.admin_master_seat = NEW.seat) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: assento % ocupado', NEW.seat USING ERRCODE = 'P0001';
        END IF;
        IF NOT public.rd_is_active_master(NEW.invited_by) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convidante não é Admin Master ativo' USING ERRCODE = 'P0001';
        END IF;
        IF public.rd_security_current_approval_mode() IS DISTINCT FROM 'DUAL' THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite de 2º Master só no modo DUAL' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.seat IS DISTINCT FROM OLD.seat OR NEW.email IS DISTINCT FROM OLD.email
       OR NEW.full_name IS DISTINCT FROM OLD.full_name OR NEW.invited_by IS DISTINCT FROM OLD.invited_by
       OR NEW.activation_user_id IS DISTINCT FROM OLD.activation_user_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'RD_MASTER_INVITE_IMMUTABLE: identidade do convite é imutável' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status <> 'PENDING' THEN
        IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite % é final', OLD.status USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NEW.status NOT IN ('ACCEPTED','REVOKED','EXPIRED','LOCKED') THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: transição PENDING → % proibida', NEW.status
                USING ERRCODE = 'P0001';
        END IF;
        IF NEW.status = 'ACCEPTED' AND NOT public.rd_security_privileged() THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: aceite só pela função de ativação do Admin Master'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
""",
)


def _exec(sql: str, **params):
    return op.get_bind().execute(sa.text(sql), params)


def _audit(action: str, entity_type: str | None, entity_id, *, before=None, after=None, reason: str | None = None) -> None:
    """Registro append-only na cadeia de auditoria (trigger da 0003/0008 calcula seq/hash)."""
    _exec(
        "INSERT INTO audit_logs (id, action, entity_type, entity_id, before_data, after_data, reason, origin, "
        "created_at) VALUES (gen_random_uuid(), :a, :et, :eid, CAST(:b AS jsonb), CAST(:af AS jsonb), :r, "
        "'system', now())",
        a=action, et=entity_type, eid=None if entity_id is None else str(entity_id),
        b=None if before is None else json.dumps(before, default=str, ensure_ascii=False, sort_keys=True),
        af=None if after is None else json.dumps(after, default=str, ensure_ascii=False, sort_keys=True), r=reason,
    )


def upgrade() -> None:
    op.add_column("admin_master_invites", sa.Column("seal", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("master_credential_seal", sa.Text(), nullable=True))
    for stmt in FUNCTIONS_0009:
        op.execute(stmt)

    # dados: convite pendente sem selo não pode mais ser ativado com segurança ⇒ revogado (auditado, sem e-mail)
    pending = [dict(r._mapping) for r in _exec(
        "SELECT id, seat, invited_by, expires_at, sent_count FROM admin_master_invites "
        "WHERE status = 'PENDING' AND seal IS NULL ORDER BY created_at, id")]
    for r in pending:
        _exec("UPDATE admin_master_invites SET status = 'REVOKED', revoked_at = now(), revoke_reason = :why "
              "WHERE id = :i", why=REVOKE_REASON_0009, i=r["id"])
        _audit("migration.0009.invite_revoked_unsealed", "admin_master_invite", r["id"],
               before={"status": "PENDING", "seat": r["seat"], "invited_by": r["invited_by"],
                       "expires_at": r["expires_at"], "sent_count": r["sent_count"]},
               after={"status": "REVOKED", "revoke_reason": "migration_0009"},
               reason="AÇÃO 003 / D1: convite criado sem o selo da aplicação")
    holders = int(_exec("SELECT count(*) FROM users WHERE admin_master_seat IS NOT NULL").scalar())
    _audit("migration.0009.credential_seal_added", None, None,
           after={"seat_holders_unsealed": holders, "invites_revoked": len(pending)},
           reason="AÇÃO 003 / D1: selos de credenciais/convite; titulares aguardam adoção pela aplicação")


def _column_exists(table: str, column: str) -> bool:
    return bool(_exec("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                      "AND table_name = :t AND column_name = :c)", t=table, c=column).scalar())


def downgrade() -> None:
    """DEFENSIVO (idempotente): funciona também sobre uma 0009 aplicada pela metade — corpos da 0008 por
    ``CREATE OR REPLACE`` e colunas por ``DROP COLUMN IF EXISTS``."""
    discarded = {}
    for key, table, column in (("invite_seals", "admin_master_invites", "seal"),
                               ("credential_seals", "users", "master_credential_seal")):
        discarded[key] = (int(_exec(f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL").scalar())
                          if _column_exists(table, column) else None)
    for stmt in FUNCTIONS_0008_RESTORE:
        op.execute(stmt)
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS master_credential_seal")
    op.execute("ALTER TABLE admin_master_invites DROP COLUMN IF EXISTS seal")
    _audit("migration.0009.downgrade", None, None, after={"discarded": discarded},
           reason="downgrade 0009: selos descartados; convites revogados pela 0009 permanecem REVOKED")
