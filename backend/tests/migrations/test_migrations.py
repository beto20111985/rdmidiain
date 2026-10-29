"""Migrations em bancos DESCARTÁVEIS (rdtest_mig_*): limpo, downgrade/upgrade, com dados, drift.

A cópia do banco de desenvolvimento (pg_dump/pg_restore) + upgrade foi executada
manualmente pelo W1 (ver relatório); aqui o "upgrade com dados" usa dados sintéticos
equivalentes (usuário super admin legado, sessão ativa, catálogo RBAC antigo).
"""
import os
import uuid
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import _ORIGINAL_SYNC_URL

ALEMBIC_INI = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "alembic.ini")

# Tabelas cujo schema é responsabilidade do W1 (0003)
W1_TABLES = {
    "users", "user_sessions", "refresh_tokens", "user_recovery_codes", "password_reset_codes", "user_invites",
    "permissions", "roles", "role_permissions", "role_assignments", "audit_logs", "system_settings",
    "system_setting_history", "consent_records", "data_deletion_requests",
}


def _url(db):
    return make_url(_ORIGINAL_SYNC_URL).set(database=db).render_as_string(hide_password=False)


@contextmanager
def scratch_db():
    name = f"rdtest_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield name
    finally:
        with admin.connect() as c:
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"), {"n": name})
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def alembic(db, *args):
    os.environ["ALEMBIC_DATABASE_URL"] = _url(db)
    try:
        cfg = Config(ALEMBIC_INI)
        getattr(command, args[0])(cfg, *args[1:])
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)


def q(db, sql, **params):
    eng = create_engine(_url(db))
    try:
        with eng.begin() as c:
            res = c.execute(text(sql), params)
            return res.all() if res.returns_rows else None
    finally:
        eng.dispose()


CATALOG_SQL = """
select 'col', table_name, column_name, data_type, coalesce(character_maximum_length::text,''), is_nullable,
       coalesce(column_default,'') from information_schema.columns where table_schema='public'
union all select 'idx', tablename, indexname, indexdef, '', '', '' from pg_indexes where schemaname='public'
union all select 'con', conrelid::regclass::text, conname, pg_get_constraintdef(oid), '', '', ''
       from pg_constraint where connamespace='public'::regnamespace
union all select 'trg', event_object_table, trigger_name, action_timing || event_manipulation, '', '', ''
       from information_schema.triggers
union all select 'fn', proname, '', '', '', '', '' from pg_proc where pronamespace='public'::regnamespace
order by 1,2,3,4
"""


def test_clean_upgrade_downgrade_roundtrip_is_structurally_identical(_test_database):
    with scratch_db() as ref, scratch_db() as db:
        alembic(ref, "upgrade", "d058863a802f")
        # W-DB: o round-trip ABAIXO da 0003 vai até a 0004 (passa pelos downgrades 0004→0003→d058).
        # A 0005 (seeds de sistema) grava auditoria (append-only) e a guarda da 0003 recusa, por
        # projeto, re-migrar auditoria legada; head ↔ 0003 é coberto em test_migrations_0004.py.
        alembic(db, "upgrade", "0004")
        assert q(db, "select version_num from alembic_version")[0][0] == "0004"
        alembic(db, "downgrade", "d058863a802f")
        assert q(db, CATALOG_SQL) == q(ref, CATALOG_SQL)
        alembic(db, "upgrade", "0004")
        # triggers append-only + cadeia presentes após o upgrade
        trg = {r[0] for r in q(db, "select tgname from pg_trigger where not tgisinternal")}
        assert {"trg_audit_logs_chain", "trg_audit_logs_append_only", "trg_audit_logs_no_truncate",
                "trg_system_setting_history_append_only", "trg_consent_records_append_only"} <= trg
        alembic(db, "downgrade", "base")
        assert q(db, "select count(*) from pg_tables where schemaname='public' and tablename <> 'alembic_version'")[0][0] == 0


def test_w1_tables_have_no_drift_after_upgrade(_test_database):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.core.database import Base

    with scratch_db() as db:
        alembic(db, "upgrade", "head")
        eng = create_engine(_url(db))
        with eng.connect() as c:
            mc = MigrationContext.configure(c, opts={"compare_type": True, "compare_server_default": True})
            diffs = compare_metadata(mc, Base.metadata)
        eng.dispose()

        def tables_of(d):
            if isinstance(d, list):
                out = set()
                for x in d:
                    out |= tables_of(x)
                return out
            kind = d[0]
            if kind in ("add_table", "remove_table"):
                return {d[1].name}
            if kind in ("add_column", "remove_column") or kind.startswith("modify_"):
                return {d[2]}
            obj = d[1]
            tbl = getattr(obj, "table", None)
            return {tbl.name} if tbl is not None else set()

        mine = [d for d in diffs if tables_of(d) & W1_TABLES]
        assert mine == [], mine


def test_upgrade_with_existing_data(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "001")
        # usuário inserido ANTES de d058863a802f (bug do NOT NULL sem default — corrigido)
        q(db, "insert into users (id,email,hashed_password,full_name,created_at,updated_at) values "
              "(gen_random_uuid(),'antigo@example.com','x','Antigo',now(),now())")
        alembic(db, "upgrade", "d058863a802f")
        uid = q(db, "insert into users (id,email,hashed_password,full_name,is_active,is_verified,totp_enabled,"
                    "failed_login_attempts,is_super_admin,totp_secret,created_at,updated_at) values "
                    "(gen_random_uuid(),'sa@example.com','x','SA',true,true,true,0,true,'PLAINSECRET',now(),now()) "
                    "returning id")[0][0]
        q(db, "insert into user_sessions (id,user_id,token_hash,refresh_token_hash,expires_at) values "
              "(gen_random_uuid(), :u, 'h1', 'r1', now() + interval '1 day')", u=uid)
        q(db, "insert into permissions (id,code,name) values (gen_random_uuid(),'users:list','legado')")
        q(db, "insert into roles (id,name,is_system) values (gen_random_uuid(),'Legado',true)")
        alembic(db, "upgrade", "0004")  # W-DB: até a 0004 — ver comentário do round-trip (0005 audita os seeds)
        assert q(db, "select count(*) from users")[0][0] == 2
        assert q(db, "select r.slug, ra.scope_type from role_assignments ra join roles r on r.id=ra.role_id "
                     "where ra.user_id = :u", u=uid) == [("super_admin", "GLOBAL")]
        assert q(db, "select revoked_at is not null, revoked_reason from user_sessions") == [(True, "legacy_0003")]
        assert q(db, "select totp_enabled from users where id = :u", u=uid) == [(False,)]  # segredo em claro descartado
        assert q(db, "select count(*) from permissions where code like '%:%'")[0][0] == 0
        assert q(db, "select count(*) from permissions")[0][0] == 69
        assert q(db, "select count(*) from roles where is_system")[0][0] == 10
        # downgrade preserva usuários e reconstrói is_super_admin
        alembic(db, "downgrade", "d058863a802f")
        assert q(db, "select email, is_super_admin from users order by email") == [
            ("antigo@example.com", False), ("sa@example.com", True)]
        alembic(db, "upgrade", "0004")
        assert q(db, "select count(*) from role_assignments")[0][0] == 1


def test_inprocess_alembic_does_not_disable_app_loggers(_test_database):
    """``fileConfig`` padrão desativava ``uvicorn.error``/``rd.*`` para o resto do processo."""
    import logging

    from app.core.logging import configure_logging

    configure_logging()
    watched = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access", "rd.app")]
    for lg in watched:
        lg.disabled = False
    with scratch_db() as db:
        alembic(db, "upgrade", "d058863a802f")
    assert [lg.name for lg in watched if lg.disabled] == []


def test_upgrade_refuses_unmigratable_legacy_rows(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "d058863a802f")
        q(db, "insert into users (id,email,hashed_password,full_name,is_active,is_verified,totp_enabled,"
              "failed_login_attempts,is_super_admin,created_at,updated_at) values "
              "(gen_random_uuid(),'x@example.com','x','X',true,true,false,0,false,now(),now())")
        q(db, "insert into audit_logs (id, action, created_at) values (gen_random_uuid(), 'legado', now())")
        with pytest.raises(Exception) as ei:
            alembic(db, "upgrade", "head")
        assert "audit_logs" in str(ei.value)
        assert q(db, "select version_num from alembic_version")[0][0] == "d058863a802f"  # transação desfeita
