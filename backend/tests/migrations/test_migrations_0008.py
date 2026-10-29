"""Migration 0008 (AÇÃO 003 / agente E — search_path seguro + referências qualificadas em todas as funções).

Bancos DESCARTÁVEIS (``rdtest_mig_*``). Cenários:
- base → head e 0007 → head: as 25 funções fora do padrão passam a ter ``search_path = pg_catalog, public,
  pg_temp`` (as 4 definer da 0007 já tinham); OIDs, triggers e ACLs preservados (REVOKE do PUBLIC nas definer);
  ``alembic check`` sem pendências; catálogo do head == harness em ``test_migrations_0004``;
- D3: a 0008 aplica corpos CONGELADOS (``FUNCTIONS_0008``), não mais o código vigente;
- round-trip 0007 → 0008 → 0007 → 0008: o downgrade restaura os corpos da 0007 byte a byte (catálogo idêntico
  ao de um banco recém-migrado até a 0007, inclusive md5 de cada função) e o re-upgrade reproduz o head;
- 0007 COM DADOS (modo inicializado, Masters, pedido, lançamento contábil, auditoria) → 0008: dados intactos,
  cadeia de auditoria íntegra, e a auditoria continua encadeando depois da troca do corpo das funções;
- a cópia CONGELADA da 0008 é idêntica aos constantes históricos (replay 0003/0004/0007).
"""
from sqlalchemy import create_engine, text

from tests.migrations.test_migrations import _url, alembic, q, scratch_db
from tests.migrations.test_migrations_0004 import FULL_CATALOG_SQL, diff

SAFE = "search_path=pg_catalog, public, pg_temp"
FN_STATE_SQL = ("select p.oid::regprocedure::text, p.oid, coalesce(array_to_string(p.proconfig, ';'), ''), "
                "coalesce(array_to_string(p.proacl, ','), '') from pg_proc p where p.pronamespace = 'public'::regnamespace "
                "and p.prokind = 'f' and not exists (select 1 from pg_depend d where d.classid = 'pg_proc'::regclass "
                "and d.objid = p.oid and d.deptype = 'e') order by 1")
CHAIN_BAD_SQL = (
    "with x as (select a.seq, a.prev_hash, a.row_hash, lag(a.row_hash) over (order by a.seq) p, "
    "encode(sha256(convert_to(rd_audit_hash_material(a, a.prev_hash), 'UTF8')), 'hex') c "
    "from audit_logs a) select count(*) filter (where c <> row_hash or prev_hash <> "
    "coalesce(p, repeat('0', 64))) from x"
)


def _migration_module():
    import importlib.util
    import os

    from tests.migrations.test_migrations import ALEMBIC_INI

    path = os.path.join(os.path.dirname(ALEMBIC_INI), "migrations", "versions", "0008_function_search_path_hardening.py")
    spec = importlib.util.spec_from_file_location("rd_mig_0008", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_0007_to_0008_every_function_has_safe_search_path(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "0007")
        before = {r[0]: r for r in q(db, FN_STATE_SQL)}
        trg_before = q(db, "select count(*) from pg_trigger where not tgisinternal")
        alembic(db, "upgrade", "0008")
        after = {r[0]: r for r in q(db, FN_STATE_SQL)}
        assert set(before) == set(after) and len(after) == 29
        assert {s for s, r in before.items() if r[2] != SAFE} == {s for s in before if not s.startswith(
            ("rd_security_init_approval_mode", "rd_security_apply_mode_transition", "rd_security_assign_master_seat",
             "rd_security_recover_master"))}
        assert all(r[2] == SAFE for r in after.values()), {s: r[2] for s, r in after.items() if r[2] != SAFE}
        reset = "rd_reset_screen_anchor(uuid,timestamp with time zone,text,uuid)"
        for sig in after:  # mesmos OIDs (CREATE OR REPLACE) e mesmas ACLs (REVOKE do PUBLIC preservado)
            assert after[sig][1] == before[sig][1], sig
            if sig != reset:
                assert after[sig][3] == before[sig][3], sig
        # reset de âncora: SECURITY DEFINER, sem EXECUTE para PUBLIC; rd_app (se existir no cluster) com EXECUTE
        assert before[reset][3] == "" and q(db, "select prosecdef from pg_proc where oid = cast(:f as regprocedure)",
                                            f=reset)[0][0] is True
        assert q(db, "select coalesce(bool_or(a.grantee = 0), false) from pg_proc p, aclexplode(p.proacl) a "
                     "where p.oid = cast(:f as regprocedure)", f=reset)[0][0] is False
        if q(db, "select exists (select 1 from pg_roles where rolname = 'rd_app')")[0][0]:
            assert q(db, "select has_function_privilege('rd_app', cast(:f as regprocedure), 'EXECUTE')", f=reset)[0][0]
        assert q(db, "select count(*) from pg_trigger where not tgisinternal") == trg_before


def test_round_trip_0007_0008_0007_0008(_test_database):
    with scratch_db() as ref0007, scratch_db() as db:
        alembic(ref0007, "upgrade", "0007")
        alembic(db, "upgrade", "0008")
        head_catalog = q(db, FULL_CATALOG_SQL)
        alembic(db, "downgrade", "0007")
        assert q(db, "select version_num from alembic_version")[0][0] == "0007"
        only_down, only_ref = diff(q(db, FULL_CATALOG_SQL), q(ref0007, FULL_CATALOG_SQL))
        assert only_down == [] and only_ref == [], (only_down, only_ref)
        assert q(db, FN_STATE_SQL) and {r[2] for r in q(db, FN_STATE_SQL)} == {r[2] for r in q(ref0007, FN_STATE_SQL)}
        alembic(db, "upgrade", "0008")
        assert q(db, FULL_CATALOG_SQL) == head_catalog


def test_upgrade_0007_with_data_keeps_data_and_audit_chain(_test_database):
    from app.modules.security import seal

    with scratch_db() as db:
        alembic(db, "upgrade", "0007")
        eng = create_engine(_url(db))
        try:
            with eng.begin() as c:
                ms = seal.mode_seal(prev_seal=seal.GENESIS, mode_version=1, from_mode=None, to_mode="SINGLE",
                                    kind="INITIALIZED", source="BOOTSTRAP_ENV", approval_request_id=None,
                                    requested_by=None, authorized_by=None, reason=None, env_value="false")
                c.execute(text("select * from rd_security_init_approval_mode('SINGLE', 'BOOTSTRAP_ENV', 'false', :s)"),
                          {"s": ms})
                uid = c.execute(text("insert into users (id,email,hashed_password,full_name,must_change_password) values "
                                     "(gen_random_uuid(),'m@rd-teste.com.br','x','M',true) returning id")).scalar()
                ss = seal.seat_seal(prev_seal=seal.GENESIS, seat=1, user_id=uid, event="ASSIGNED", source="BOOTSTRAP_ENV",
                                    invite_id=None, actor_user_id=None)
                c.execute(text("select rd_security_assign_master_seat(:u, cast(1 as smallint), 'BOOTSTRAP_ENV', null, "
                               "null, :p, :s)"), {"u": uid, "p": seal.GENESIS, "s": ss})
                c.execute(text("update users set must_change_password = false, totp_enabled = true, "
                               "financial_password_hash = 'f' where id = :u"), {"u": uid})
                c.execute(text("insert into financial_approval_requests (id,action_type,payload,payload_sha256,reason,"
                               "requested_by,status,approval_mode,expires_at,execution_state,idempotency_key) values "
                               "(gen_random_uuid(),'LEDGER_ADJUSTMENT','{}',repeat('0',64),'motivo',:u,'PENDING','SINGLE',"
                               "now() + interval '1 day','NOT_STARTED','k1')"), {"u": uid})
                acc = [c.execute(text("insert into ledger_accounts (id,account_key,code,name,kind,owner_type) values "
                                      "(gen_random_uuid(), :k, :k, :k, 'ASSET', 'RD') returning id"), {"k": k}).scalar()
                       for k in ("a", "b")]
                tx = c.execute(text("insert into ledger_transactions (id,kind,idempotency_key,competence_date,description) "
                                    "values (gen_random_uuid(),'MANUAL','t1','2026-10-01','x') returning id")).scalar()
                for a_, v in zip(acc, (100, -100)):
                    c.execute(text("insert into ledger_postings (id,transaction_id,account_id,amount_cents,competence_date) "
                                   "values (gen_random_uuid(), :t, :a, :v, '2026-10-01')"), {"t": tx, "a": a_, "v": v})
                c.execute(text("insert into audit_logs (id,action,origin,created_at) values "
                               "(gen_random_uuid(),'antes.0008','system',now())"))
        finally:
            eng.dispose()
        snap = ("select (select count(*) from instance_approval_mode_history), (select count(*) from admin_master_seat_events), "
                "(select count(*) from financial_approval_requests), (select count(*) from ledger_postings), "
                "(select count(*) from audit_logs), rd_security_current_approval_mode(), rd_active_master_count()")
        before = q(db, snap)
        alembic(db, "upgrade", "0008")
        assert q(db, snap) == before
        assert q(db, CHAIN_BAD_SQL)[0][0] == 0
        q(db, "insert into audit_logs (id,action,origin,created_at) values (gen_random_uuid(),'depois.0008','system',now())")
        assert q(db, CHAIN_BAD_SQL)[0][0] == 0  # o corpo novo da cadeia continua o encadeamento
        # guard endurecido segue recusando (balanceamento no COMMIT)
        try:
            q(db, "with t as (insert into ledger_transactions (id,kind,idempotency_key,competence_date,description) values "
                  "(gen_random_uuid(),'MANUAL','t2','2026-10-01','x') returning id) insert into ledger_postings "
                  "(id,transaction_id,account_id,amount_cents,competence_date) select gen_random_uuid(), t.id, "
                  "(select id from ledger_accounts limit 1), 5, '2026-10-01' from t")
            raise AssertionError("transação desbalanceada aceita")
        except Exception as exc:  # noqa: BLE001
            assert "RD_LEDGER_UNBALANCED" in str(exc), exc


LEGACY_INIT_DB_SQL = [  # exatamente como em backend/scripts/init-db.sql (initdb do container de desenvolvimento)
    """CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW() AT TIME ZONE 'UTC';
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;""",
    """CREATE OR REPLACE FUNCTION normalize_search(text_input TEXT)
RETURNS TEXT AS $$
BEGIN
    RETURN lower(unaccent(text_input));
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;""",
]
LEGACY_STATE_SQL = ("select p.proname, coalesce(array_to_string(p.proconfig, ';'), ''), md5(p.prosrc) from pg_proc p "
                    "where p.pronamespace = 'public'::regnamespace and p.proname in ('set_updated_at','normalize_search') "
                    "order by 1")


def test_legacy_init_db_functions_are_hardened_and_unknown_function_aborts(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "0007")
        for stmt in LEGACY_INIT_DB_SQL:
            q(db, stmt)
        before = q(db, LEGACY_STATE_SQL)
        assert [r[1] for r in before] == ["", ""]
        alembic(db, "upgrade", "head")
        after = q(db, LEGACY_STATE_SQL)
        assert [r[1] for r in after] == [SAFE, SAFE] and [r[2] for r in after] == [r[2] for r in before]
        alembic(db, "downgrade", "0007")
        assert q(db, LEGACY_STATE_SQL) == before
        # função DESCONHECIDA fora do padrão: a 0008 falha alto e nada é aplicado
        q(db, "CREATE FUNCTION rd_funcao_estranha() RETURNS int LANGUAGE sql AS $$ SELECT count(*)::int FROM users $$")
        try:
            alembic(db, "upgrade", "head")
            raise AssertionError("0008 aceitou função desconhecida sem search_path")
        except RuntimeError as exc:
            assert "rd_funcao_estranha()" in str(exc), exc
        assert q(db, "select version_num from alembic_version")[0][0] == "0007"
        assert q(db, LEGACY_STATE_SQL) == before


def test_frozen_0007_bodies_match_historical_constants():
    from app.db import ddl

    mod = _migration_module()
    frozen = [body for _sig, body in mod.FUNCTIONS_0007]
    hardened = [s for s in mod.FUNCTIONS_0008 if "CREATE OR REPLACE FUNCTION" in s]  # D3: congelados na 0008
    assert len(frozen) == len(hardened) == 25
    # reset de âncora vigente = histórico endurecido + SECURITY DEFINER (corpo idêntico, só qualificado)
    from app.modules.inventory import ddl as inv

    derived = ddl.harden_function(inv.ANCHOR_RESET_FN, [
        ("FROM screen_inventory_state s", "FROM public.screen_inventory_state s"),
        ("UPDATE screen_inventory_state", "UPDATE public.screen_inventory_state"),
        ("INSERT INTO screen_anchor_history", "INSERT INTO public.screen_anchor_history"),
    ]).replace("LANGUAGE plpgsql\nSET search_path", "LANGUAGE plpgsql\nSECURITY DEFINER\nSET search_path")
    assert inv.ANCHOR_RESET_FN_V2 == derived
    # cada corpo endurecido = corpo histórico + SET search_path (+ qualificações) — mesma função, na mesma ordem
    for old, new in zip(frozen, hardened):
        name = old.split("FUNCTION", 1)[1].split("(", 1)[0].strip()
        assert name == new.split("FUNCTION", 1)[1].split("(", 1)[0].strip()
        assert "SET search_path = pg_catalog, public, pg_temp AS $$" in new and "SET search_path" not in old
    from app.modules.financial import ddl as fin
    from app.modules.security import ddl as sec

    assert ddl.FORBID_MUTATION_FN in frozen and ddl.AUDIT_CHAIN_FN in frozen
    assert fin.APPROVAL_GUARD_FN in frozen and fin.APPROVAL_GUARD_FN_V1 not in frozen  # estado da 0007, não da 0004
    assert sec.USERS_GUARD_FN in frozen
