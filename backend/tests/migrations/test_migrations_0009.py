"""Migration 0009 (AÇÃO 003 / agente E — selos de credencial/convite; D1 do revisor I) e D3 (0008 congelada).

Bancos DESCARTÁVEIS (``rdtest_mig_*``). Cenários:
- base → head: colunas ``admin_master_invites.seal``/``users.master_credential_seal``; guards com o selo; 29 funções
  com search_path seguro; ``alembic check`` sem pendências (head == harness em ``test_migrations_0004``);
- 0008 COM DADOS → 0009: convite PENDING sem selo ⇒ REVOKED + auditoria por convite (sem e-mail) + resumo;
  titulares com selo NULL (adoção pela aplicação); cadeia de auditoria íntegra; downgrade não "desrevoga";
- round-trip 0008 → 0009 → 0008 → 0009 com catálogo idêntico (corpos da 0008 restaurados do bloco congelado);
- D3: os corpos que a 0008 aplica são os do bloco CONGELADO, e o catálogo de funções de um banco novo na 0008 é o
  mesmo de um banco que recebeu a 0008 pelo caminho histórico (0007 → 0008).
"""
from sqlalchemy import create_engine, text

from tests.migrations.test_migrations import _url, alembic, q, scratch_db
from tests.migrations.test_migrations_0004 import FULL_CATALOG_SQL, diff, head_revision

SAFE = "search_path=pg_catalog, public, pg_temp"
CHAIN_BAD_SQL = (
    "with x as (select a.seq, a.prev_hash, a.row_hash, lag(a.row_hash) over (order by a.seq) p, "
    "encode(sha256(convert_to(rd_audit_hash_material(a, a.prev_hash), 'UTF8')), 'hex') c "
    "from audit_logs a) select count(*) filter (where c <> row_hash or prev_hash <> "
    "coalesce(p, repeat('0', 64))) from x"
)
FN_DEF_SQL = ("select p.oid::regprocedure::text, md5(pg_get_functiondef(p.oid)), "
              "coalesce(array_to_string(p.proconfig, ';'), ''), p.prosecdef from pg_proc p "
              "where p.pronamespace = 'public'::regnamespace and p.prokind = 'f' and not exists (select 1 from pg_depend d "
              "where d.classid = 'pg_proc'::regclass and d.objid = p.oid and d.deptype = 'e') order by 1")


def test_base_to_head_seal_columns_guards_and_alembic_check(_test_database):
    assert head_revision() == "0009"
    with scratch_db() as db:
        alembic(db, "upgrade", "head")
        cols = {(r[0], r[1]) for r in q(db, "select table_name, column_name from information_schema.columns where "
                                            "column_name in ('seal', 'master_credential_seal')")}
        assert cols == {("admin_master_invites", "seal"), ("users", "master_credential_seal")}
        src = dict(q(db, "select proname, prosrc from pg_proc where proname in "
                         "('rd_users_master_guard','rd_admin_master_invite_guard','rd_users_master_log')"))
        assert "RD_MASTER_CREDENTIAL_SEAL_REQUIRED" in src["rd_users_master_guard"]
        assert "RD_MASTER_INVITE_SEAL_REQUIRED" in src["rd_admin_master_invite_guard"]
        assert "master_credential_seal" in src["rd_users_master_log"]
        fns = q(db, FN_DEF_SQL)
        assert len(fns) == 29 and all(r[2] == SAFE for r in fns), [r[0] for r in fns if r[2] != SAFE]
        alembic(db, "check")


def test_upgrade_0008_with_data_revokes_unsealed_invites_and_keeps_chain(_test_database):
    from app.modules.security import seal

    with scratch_db() as db:
        alembic(db, "upgrade", "0008")
        eng = create_engine(_url(db))
        try:
            with eng.begin() as c:
                ms = seal.mode_seal(prev_seal=seal.GENESIS, mode_version=1, from_mode=None, to_mode="DUAL",
                                    kind="INITIALIZED", source="BOOTSTRAP_ENV", approval_request_id=None,
                                    requested_by=None, authorized_by=None, reason=None, env_value="true")
                c.execute(text("select * from rd_security_init_approval_mode('DUAL', 'BOOTSTRAP_ENV', 'true', :s)"),
                          {"s": ms})
                uid = c.execute(text("insert into users (id,email,hashed_password,full_name,must_change_password) values "
                                     "(gen_random_uuid(),'m@rd-teste.com.br','x','M',true) returning id")).scalar()
                ss = seal.seat_seal(prev_seal=seal.GENESIS, seat=1, user_id=uid, event="ASSIGNED", source="BOOTSTRAP_ENV",
                                    invite_id=None, actor_user_id=None)
                c.execute(text("select rd_security_assign_master_seat(:u, cast(1 as smallint), 'BOOTSTRAP_ENV', null, "
                               "null, :p, :s)"), {"u": uid, "p": seal.GENESIS, "s": ss})
                c.execute(text("update users set must_change_password = false, totp_enabled = true, "
                               "financial_password_hash = 'f' where id = :u"), {"u": uid})
                inv = c.execute(text("insert into admin_master_invites (id,seat,email,full_name,token_hash,"
                                     "activation_user_id,invited_by,status,expires_at) values (gen_random_uuid(),2,"
                                     "'b@rd-teste.com.br','B',repeat('a',64),gen_random_uuid(),:u,'PENDING',"
                                     "now() + interval '72 hours') returning id"), {"u": uid}).scalar()
        finally:
            eng.dispose()
        audit_before = q(db, "select count(*) from audit_logs")[0][0]
        alembic(db, "upgrade", "head")
        assert q(db, "select status, revoked_at is not null, revoke_reason, seal from admin_master_invites where id = :i",
                 i=inv) == [("REVOKED", True, "migration_0009: convite sem selo da aplicação (D1)", None)]
        rows = q(db, "select action, entity_id, before_data, after_data from audit_logs where action like "
                     "'migration.0009.%' order by seq")
        assert [r[0] for r in rows] == ["migration.0009.invite_revoked_unsealed", "migration.0009.credential_seal_added"]
        assert rows[0][1] == str(inv) and rows[0][2]["status"] == "PENDING" and "email" not in rows[0][2]
        assert rows[1][3] == {"seat_holders_unsealed": 1, "invites_revoked": 1}
        assert q(db, "select count(*) from audit_logs")[0][0] == audit_before + 2
        assert q(db, "select master_credential_seal from users where id = :u", u=uid) == [(None,)]
        assert q(db, "select rd_active_master_count()")[0][0] == 1  # titular segue ativo (selo NULL até a adoção)
        assert q(db, CHAIN_BAD_SQL)[0][0] == 0
        # downgrade: colunas removidas; convite continua REVOKED; auditoria do downgrade
        alembic(db, "downgrade", "0008")
        assert q(db, "select count(*) from information_schema.columns where column_name in "
                     "('seal','master_credential_seal')")[0][0] == 0
        assert q(db, "select status from admin_master_invites where id = :i", i=inv) == [("REVOKED",)]
        assert q(db, "select count(*) from audit_logs where action = 'migration.0009.downgrade'")[0][0] == 1
        alembic(db, "upgrade", "head")
        assert q(db, CHAIN_BAD_SQL)[0][0] == 0


def test_round_trip_0008_0009_0008_0009(_test_database):
    with scratch_db() as ref0008, scratch_db() as db:
        alembic(ref0008, "upgrade", "0008")
        alembic(db, "upgrade", "head")
        head_catalog = q(db, FULL_CATALOG_SQL)
        alembic(db, "downgrade", "0008")
        assert q(db, "select version_num from alembic_version")[0][0] == "0008"
        only_down, only_ref = diff(q(db, FULL_CATALOG_SQL), q(ref0008, FULL_CATALOG_SQL))
        assert only_down == [] and only_ref == [], (only_down, only_ref)
        alembic(db, "upgrade", "0009")
        assert q(db, FULL_CATALOG_SQL) == head_catalog


def test_d3_0008_applies_frozen_bodies(_test_database):
    """A 0008 aplica ``FUNCTIONS_0008`` (congelado), não o código vigente: o catálogo de funções na 0008 é o mesmo
    por qualquer caminho e cada corpo é byte a byte o do bloco congelado."""
    from tests.migrations.test_migrations_0008 import _migration_module

    mod = _migration_module()
    with scratch_db() as db:
        alembic(db, "upgrade", "0008")
        prosrc = dict(q(db, "select p.oid::regprocedure::text, p.prosrc from pg_proc p where "
                            "p.pronamespace = 'public'::regnamespace and p.prokind = 'f'"))
        frozen_creates = [s for s in mod.FUNCTIONS_0008 if "CREATE OR REPLACE FUNCTION" in s]
        assert len(frozen_creates) == 25
        for stmt in frozen_creates:
            body = stmt.split("AS $$", 1)[1].rsplit("$$", 1)[0]
            assert body in prosrc.values(), stmt.split("(", 1)[0]
        # o vigente (harness) já difere na 0009 — prova de que a 0008 não segue mais o código vigente
        from app.db import ddl

        vigente = [s for s in ddl.hardened_function_statements() if "CREATE OR REPLACE FUNCTION" in s]
        assert vigente != frozen_creates
