"""Migration 0007 (AÇÃO 003 / agente E — bootstrap do Admin Master, modo de aprovação SINGLE × DUAL).

Bancos DESCARTÁVEIS (``rdtest_mig_*``). Cenários:
- base → head limpo; ``alembic check`` sem operações pendentes (drift 0; catálogo do head == harness em
  ``test_migrations_0004``);
- round-trip 0006 → 0007 → 0006 → 0007: estrutura da 0006 idêntica à de um banco recém-migrado até a 0006
  (inclusive o corpo do guard de aprovação congelado) e head reconstruído idêntico;
- upgrade COM DADOS equivalentes ao dev (``super_admin`` legado sem assento com sessão ativa; usuário com
  ``super_admin`` + outro perfil; titular de assento legado; pedido financeiro existente): revogação +
  desativação auditadas (2 linhas para o legado), sessões revogadas, titular intocado, pedido ``DUAL``,
  NENHUM modo inicializado e NENHUM Master criado; downgrade não devolve o papel nem reativa;
- downgrade RECUSADO com pedido SINGLE ou APPROVAL_MODE_CHANGE (revisão mantida);
- privilégios do ``rd_app`` aplicados pela própria migration;
- harness: a limpeza entre testes (``_cleanup_db``) cobre as tabelas novas.
"""
import json
import uuid

import pytest
from sqlalchemy import create_engine, text

from tests.migrations.test_migrations import _url, alembic, q, scratch_db
from tests.migrations.test_migrations_0004 import FULL_CATALOG_SQL, diff, head_revision

CHAIN_BAD_SQL = (
    "with x as (select a.seq, a.prev_hash, a.row_hash, lag(a.row_hash) over (order by a.seq) p, "
    "encode(sha256(convert_to(rd_audit_hash_material(a, a.prev_hash), 'UTF8')), 'hex') c "
    "from audit_logs a) select count(*) filter (where c <> row_hash or prev_hash <> "
    "coalesce(p, repeat('0', 64))) from x"
)
NEW_TABLES = {"admin_master_invites", "admin_master_seat_events", "instance_approval_mode_history"}


def test_clean_base_to_head_and_alembic_check(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "head")
        assert q(db, "select version_num from alembic_version")[0][0] == head_revision()
        tables = {r[0] for r in q(db, "select tablename from pg_tables where schemaname = 'public'")}
        assert NEW_TABLES <= tables
        fns = {r[0] for r in q(db, "select proname from pg_proc where pronamespace = 'public'::regnamespace")}
        assert {"rd_security_privileged", "rd_security_current_approval_mode", "rd_is_active_master",
                "rd_active_master_count", "rd_security_init_approval_mode", "rd_security_apply_mode_transition",
                "rd_security_assign_master_seat", "rd_security_recover_master", "rd_users_master_guard",
                "rd_users_master_log", "rd_role_assignments_master_guard", "rd_admin_master_invite_guard",
                "rd_approval_check_authorization", "rd_approval_request_insert_guard"} <= fns
        trg = {r[0] for r in q(db, "select tgname from pg_trigger where not tgisinternal")}
        assert {"trg_users_master_guard", "trg_users_master_log", "trg_role_assignments_master_guard",
                "trg_admin_master_invites_guard", "trg_admin_master_invites_no_truncate",
                "trg_instance_approval_mode_history_append_only", "trg_admin_master_seat_events_append_only",
                "trg_financial_approval_requests_insert_guard", "trg_financial_approval_requests_guard"} <= trg
        # migration não inicializa modo nem cria Master
        assert q(db, "select count(*) from instance_approval_mode_history")[0][0] == 0
        assert q(db, "select count(*) from users where admin_master_seat is not null")[0][0] == 0
        alembic(db, "check")  # levanta se houver operação de upgrade pendente (drift)


def test_round_trip_0006_0007_0006_0007(_test_database):
    with scratch_db() as ref0006, scratch_db() as db:
        alembic(ref0006, "upgrade", "0006")
        alembic(db, "upgrade", "0006")
        alembic(db, "upgrade", "0007")
        head_catalog = q(db, FULL_CATALOG_SQL)
        alembic(db, "downgrade", "0006")
        assert q(db, "select version_num from alembic_version")[0][0] == "0006"
        only_down, only_ref = diff(q(db, FULL_CATALOG_SQL), q(ref0006, FULL_CATALOG_SQL))
        assert only_down == [] and only_ref == [], (only_down, only_ref)
        alembic(db, "upgrade", "0007")
        assert q(db, FULL_CATALOG_SQL) == head_catalog


def _insert_0006_dev_like_data(db) -> dict:
    """Formato 0006 equivalente ao dev: legado ``adm***`` (super_admin GLOBAL, sem assento, sessão ativa)."""
    ids = {}
    ids["legacy"] = q(db, "insert into users (id,email,hashed_password,full_name,is_active,must_change_password) "
                          "values (gen_random_uuid(),'admin.legado@example.com','!locked','Super Admin',true,true) "
                          "returning id")[0][0]
    ids["multi"] = q(db, "insert into users (id,email,hashed_password,full_name,is_active) values "
                         "(gen_random_uuid(),'multi@example.com','x','Multi',true) returning id")[0][0]
    ids["seat"] = q(db, "insert into users (id,email,hashed_password,full_name,is_active,admin_master_seat,"
                        "totp_enabled,financial_password_hash) values (gen_random_uuid(),'socio@example.com','x',"
                        "'Sócio',true,1,true,'fin') returning id")[0][0]
    for key, roles in (("legacy", ["super_admin"]), ("multi", ["super_admin", "financial"]), ("seat", ["super_admin"])):
        for slug in roles:
            q(db, "insert into role_assignments (id,user_id,role_id,scope_type,created_at) select gen_random_uuid(), :u, "
                  "r.id, 'GLOBAL', now() from roles r where r.slug = :s", u=ids[key], s=slug)
    ids["session"] = q(db, "insert into user_sessions (id,user_id,expires_at,amr) values (gen_random_uuid(), :u, "
                           "now() + interval '1 day', '[]'::jsonb) returning id", u=ids["legacy"])[0][0]
    ids["request"] = q(db, "insert into financial_approval_requests (id,action_type,payload,payload_sha256,reason,"
                           "requested_by,status,expires_at,execution_state,idempotency_key) values (gen_random_uuid(),"
                           "'LEDGER_ADJUSTMENT','{}'::jsonb,repeat('0',64),'ajuste legado',:u,'PENDING',"
                           "now() + interval '1 day','NOT_STARTED','legado-1') returning id", u=ids["seat"])[0][0]
    return ids


def test_upgrade_with_0006_data_revokes_legacy_super_admin_and_audits(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "0006")
        assert q(db, "select count(*) from roles where slug in ('super_admin','financial')")[0][0] == 2
        ids = _insert_0006_dev_like_data(db)
        audit_before = q(db, "select count(*) from audit_logs")[0][0]
        alembic(db, "upgrade", "0007")  # escopo da 0007 (auditoria da 0009 mudaria a contagem)

        def roles_of(uid):
            return sorted(r[0] for r in q(db, "select r.slug from role_assignments ra join roles r on r.id = ra.role_id "
                                              "where ra.user_id = :u", u=uid))

        assert roles_of(ids["legacy"]) == []
        assert roles_of(ids["multi"]) == ["financial"]
        assert roles_of(ids["seat"]) == ["super_admin"]  # titular de assento: intocado
        assert q(db, "select is_active from users where id = :u", u=ids["legacy"]) == [(False,)]
        assert q(db, "select is_active from users where id = :u", u=ids["multi"]) == [(True,)]
        assert q(db, "select revoked_at is not null, revoked_reason from user_sessions where id = :s",
                 s=ids["session"]) == [(True, "migration_0007")]
        # auditoria: legado = 2 linhas (revogado + desativado); multi = só a revogação
        rows = q(db, "select action, entity_id, before_data, after_data from audit_logs where action like "
                     "'migration.0007.%' order by seq")
        by_user = {}
        for action, eid, before, after in rows:
            by_user.setdefault(eid, []).append((action, before, after))
        legacy = by_user[str(ids["legacy"])]
        assert [a for a, _, _ in legacy] == ["migration.0007.super_admin_revoked", "migration.0007.legacy_user_deactivated"]
        assert legacy[0][1]["role"] == "super_admin" and legacy[0][1]["scope_type"] == "GLOBAL"
        assert legacy[1][1] == {"is_active": True, "active_sessions": 1}
        assert legacy[1][2]["is_active"] is False and legacy[1][2]["sessions_revoked"] == 1
        assert [a for a, _, _ in by_user[str(ids["multi"])]] == ["migration.0007.super_admin_revoked"]
        assert str(ids["seat"]) not in by_user
        backfill = [r[3] for r in rows if r[0] == "migration.0007.approval_mode_backfilled"]
        assert backfill == [{"approval_mode": "DUAL", "requests": 1}]
        dump = json.dumps([list(map(str, r)) for r in rows])
        assert "example.com" not in dump  # sem e-mail na auditoria
        assert q(db, "select count(*) from audit_logs")[0][0] == audit_before + 4
        assert q(db, CHAIN_BAD_SQL)[0][0] == 0
        # pedido existente marcado DUAL; nada de modo/Master criado pela migration
        assert q(db, "select approval_mode, self_authorized_by from financial_approval_requests") == [("DUAL", None)]
        assert q(db, "select column_default from information_schema.columns where table_name = "
                     "'financial_approval_requests' and column_name = 'approval_mode'") == [(None,)]
        assert q(db, "select count(*) from instance_approval_mode_history")[0][0] == 0
        assert q(db, "select count(*) from admin_master_seat_events")[0][0] == 0

        # downgrade (só pedidos DUAL): permitido; NÃO devolve o super_admin nem reativa o legado
        alembic(db, "downgrade", "0006")
        assert q(db, "select version_num from alembic_version")[0][0] == "0006"
        assert roles_of(ids["legacy"]) == [] and q(db, "select is_active from users where id = :u",
                                                   u=ids["legacy"]) == [(False,)]
        assert q(db, "select count(*) from audit_logs where action = 'migration.0007.downgrade'")[0][0] == 1
        assert q(db, "select count(*) from financial_approval_requests")[0][0] == 1
        alembic(db, "upgrade", "0007")  # escopo da 0007 (auditoria da 0009 mudaria a contagem)
        assert q(db, "select count(*) from audit_logs where action = 'migration.0007.super_admin_revoked'")[0][0] == 2
        assert q(db, CHAIN_BAD_SQL)[0][0] == 0


@pytest.mark.parametrize("kind", ["single", "mode_change"])
def test_downgrade_refused_with_single_or_mode_change_requests(_test_database, kind):
    with scratch_db() as db:
        alembic(db, "upgrade", "0007")  # recusa da 0007 (o head 0008 voltaria junto)
        uid = q(db, "insert into users (id,email,hashed_password,full_name) values (gen_random_uuid(),"
                    "'m@example.com','x','M') returning id")[0][0]
        mode, action = ("SINGLE", "LEDGER_ADJUSTMENT") if kind == "single" else ("DUAL", "APPROVAL_MODE_CHANGE")
        # linha inserida sem os triggers (o cenário real passa pelos fluxos; aqui só importa a guarda)
        eng = create_engine(_url(db))
        try:
            with eng.begin() as c:
                c.execute(text("SET LOCAL session_replication_role = replica"))
                c.execute(text(
                    "insert into financial_approval_requests (id,action_type,payload,payload_sha256,reason,requested_by,"
                    "status,approval_mode,expires_at,execution_state,idempotency_key) values (:i, :a, '{}'::jsonb, "
                    "repeat('0',64), 'motivo', :u, 'PENDING', :m, now() + interval '1 day', 'NOT_STARTED', 'k')"),
                    {"i": uuid.uuid4(), "a": action, "u": uid, "m": mode})
        finally:
            eng.dispose()
        with pytest.raises(Exception) as ei:
            alembic(db, "downgrade", "0006")
        assert "downgrade 0007 recusado" in str(ei.value)
        assert q(db, "select version_num from alembic_version")[0][0] == "0007"  # transação desfeita
        assert q(db, "select count(*) from instance_approval_mode_history")[0][0] == 0  # tabelas intactas


def test_runtime_role_privileges_applied_by_migration(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "head")
        if not q(db, "select exists (select 1 from pg_roles where rolname = 'rd_app')")[0][0]:
            pytest.skip("papel rd_app inexistente neste cluster (criado pelo db-init)")
        privs = "select has_table_privilege('rd_app', :t, 'SELECT'), has_table_privilege('rd_app', :t, 'INSERT'), " \
                "has_table_privilege('rd_app', :t, 'UPDATE'), has_table_privilege('rd_app', :t, 'DELETE'), " \
                "has_table_privilege('rd_app', :t, 'TRUNCATE')"
        for t in ("instance_approval_mode_history", "admin_master_seat_events"):
            assert q(db, privs, t=t)[0] == (True, False, False, False, False), t
            assert q(db, "select has_sequence_privilege('rd_app', :s, 'USAGE') or "
                         "has_sequence_privilege('rd_app', :s, 'UPDATE')", s=f"{t}_id_seq")[0][0] is False
        assert q(db, privs, t="admin_master_invites")[0] == (True, True, True, False, False)
        from app.modules.security import ddl as sec_ddl

        for sig in sec_ddl.DEFINER_FUNCTIONS:
            assert q(db, "select has_function_privilege('rd_app', :f, 'EXECUTE')", f=sig)[0][0] is True, sig
            assert q(db, "select coalesce(bool_or(a.grantee = 0), false) from pg_proc p, aclexplode(p.proacl) a "
                         "where p.oid = cast(:f as regprocedure)", f=sig)[0][0] is False, sig


def test_harness_cleanup_covers_security_tables(clean_state):
    """O DELETE com ``session_replication_role=replica`` do harness limpa as tabelas novas (append-only)."""
    from app.modules.security import seal
    from tests.conftest import TEST_DB, _cleanup_db, _sync_url

    eng = create_engine(_sync_url(TEST_DB))
    try:
        with eng.begin() as c:
            ms = seal.mode_seal(prev_seal=seal.GENESIS, mode_version=1, from_mode=None, to_mode="DUAL",
                                kind="INITIALIZED", source="BOOTSTRAP_ENV", approval_request_id=None,
                                requested_by=None, authorized_by=None, reason=None, env_value="true")
            c.execute(text("select * from rd_security_init_approval_mode('DUAL', 'BOOTSTRAP_ENV', 'true', :s)"),
                      {"s": ms})
            uid = c.execute(text("insert into users (id,email,hashed_password,full_name,must_change_password) values "
                                 "(gen_random_uuid(),'h@rd-teste.com.br','x','H',true) returning id")).scalar()
            ss = seal.seat_seal(prev_seal=seal.GENESIS, seat=1, user_id=uid, event="ASSIGNED", source="BOOTSTRAP_ENV",
                                invite_id=None, actor_user_id=None)
            c.execute(text("select rd_security_assign_master_seat(:u, cast(1 as smallint), 'BOOTSTRAP_ENV', null, null, "
                           ":p, :s)"), {"u": uid, "p": seal.GENESIS, "s": ss})
            c.execute(text("update users set must_change_password = false, totp_enabled = true, "
                           "financial_password_hash = 'f' where id = :u"), {"u": uid})
            c.execute(text("insert into admin_master_invites (id,seat,email,full_name,token_hash,activation_user_id,"
                           "invited_by,status,expires_at) values (gen_random_uuid(),2,'b@rd-teste.com.br','B',"
                           "repeat('a',64),gen_random_uuid(),:u,'PENDING',now() + interval '72 hours')"), {"u": uid})
    finally:
        eng.dispose()  # _cleanup_db encerra as demais conexões do banco de teste
    _cleanup_db()
    eng = create_engine(_sync_url(TEST_DB))
    try:
        with eng.connect() as c:
            left = c.execute(text("select (select count(*) from instance_approval_mode_history) + "
                                  "(select count(*) from admin_master_seat_events) + "
                                  "(select count(*) from admin_master_invites) + "
                                  "(select count(*) from users)")).scalar()
        assert left == 0
    finally:
        eng.dispose()
