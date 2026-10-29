"""AÇÃO 003 / agente E — migration 0008: sombra por tabela TEMPORÁRIA do ``rd_app`` (achado do agente H).

O ``rd_app`` tem TEMPORARY; sem ``pg_temp`` no ``search_path`` o PostgreSQL pesquisa o esquema temporário
PRIMEIRO para tabelas/sequências/tipos. Cada ataque abaixo roda como ``rd_app`` (privilégios de produção) em
DOIS bancos descartáveis: um parado na 0007 (prova de que a sombra FUNCIONAVA) e outro no head (0008 —
recusado). Guards sem nenhuma referência a tabela (saque, webhook, append-only, coluna imutável, aprovação)
são exercitados com a mesma tentativa de sombra nas duas revisões: recusados nas duas.

Inventário (head): toda função de ``public`` (fora extensões) tem ``search_path = pg_catalog, public, pg_temp``
e nenhuma referência NÃO qualificada a tabela/sequência de ``public`` — teste vivo: função nova fora do padrão
quebra a suíte.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from sqlalchemy import create_engine, text

from tests.adversarial.test_sec_db_rd_app import Sec, grant_runtime_like_production
from tests.migrations.test_migrations import _url, alembic, q, scratch_db

SAFE = "search_path=pg_catalog, public, pg_temp"
CLOSED_DAY = "2020-06-15"
OPEN_DAY = "2021-01-10"


@pytest.fixture(scope="module", params=["0007", "head"])
def shadow_db(request, _test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", request.param)
        if not grant_runtime_like_production(db):
            pytest.skip("papel rd_app inexistente neste cluster (criado pelo db-init)")
        yield request.param, db


@pytest.fixture
def ctx(shadow_db):
    rev, db = shadow_db
    eng = create_engine(_url(db))
    conn = eng.connect()
    trans = conn.begin()
    try:
        yield rev, Sec(conn)
    finally:
        trans.rollback()
        conn.close()
        eng.dispose()


def hardened(rev: str) -> bool:
    return rev != "0007"


# ─── preparação (dono) ───────────────────────────────────────────────────────

def ledger_setup(s: Sec) -> tuple[uuid.UUID, uuid.UUID]:
    """Duas contas e o período de junho/2020 FECHADO."""
    accts = []
    for code in ("caixa", "receita"):
        aid = uuid.uuid4()
        s.x("INSERT INTO ledger_accounts (id, account_key, code, name, kind, owner_type) VALUES "
            "(:i, :k, :c, :c, 'ASSET', 'RD')", i=aid, k=f"{code}|RD|{aid.hex[:6]}", c=code)
        accts.append(aid)
    s.x("INSERT INTO financial_period_closings (id, period_start, period_end, snapshot, snapshot_sha256, "
        "transaction_count) VALUES (gen_random_uuid(), '2020-06-01', '2020-06-30', '{}'::jsonb, :h, 0)", h="0" * 64)
    return accts[0], accts[1]


def ledger_insert(s: Sec, day: str, postings: list[tuple[uuid.UUID, int]], *, posting_day: str | None = None) -> uuid.UUID:
    tx = uuid.uuid4()
    s.x("INSERT INTO public.ledger_transactions (id, kind, idempotency_key, competence_date, description) VALUES "
        "(:i, 'MANUAL', :k, CAST(:d AS date), 'teste de sombra')", i=tx, k=f"sombra-{tx}", d=day)
    for acc, amount in postings:
        s.x("INSERT INTO public.ledger_postings (id, transaction_id, account_id, amount_cents, competence_date) VALUES "
            "(gen_random_uuid(), :t, :a, :v, CAST(:d AS date))", t=tx, a=acc, v=amount, d=posting_day or day)
    return tx


# ═════════════════════════════════════════════════════════════════════════════
# Inventário
# ═════════════════════════════════════════════════════════════════════════════

def test_function_inventory_search_path_and_qualified_references(shadow_db):
    rev, db = shadow_db
    rels = [r[0] for r in q(db, "select relname from pg_class where relnamespace = 'public'::regnamespace "
                                "and relkind in ('r','p','v','m','S')")]
    rel_re = re.compile(r"(?<![\w.])(" + "|".join(sorted(map(re.escape, rels), key=len, reverse=True)) + r")\b")
    fns = q(db, "select p.oid::regprocedure::text, coalesce(array_to_string(p.proconfig, ';'), ''), p.prosrc "
                "from pg_proc p where p.pronamespace = 'public'::regnamespace and p.prokind = 'f' and not exists "
                "(select 1 from pg_depend d where d.classid = 'pg_proc'::regclass and d.objid = p.oid and d.deptype = 'e')")
    problems = {}
    for sig, cfg, src in fns:
        body = re.sub(r"--[^\n]*", "", src)
        issues = []
        if cfg != SAFE:
            issues.append(f"search_path={cfg or '-'}")
        unq = sorted({m.group(1) for m in rel_re.finditer(re.sub(r"'(?:[^']|'')*'", "''", body))})
        if unq:
            issues.append(f"tabelas não qualificadas={unq}")
        lits = [x for x in re.findall(r"(?:nextval|currval|setval|to_regclass)\('([^']+)'", body) if "." not in x]
        if lits:
            issues.append(f"regclass não qualificado={lits}")
        if issues:
            problems[sig] = issues
    assert len(fns) == 29, [f[0] for f in fns]
    if hardened(rev):
        assert problems == {}, problems
    else:
        # prova do estado anterior: 25 funções sem search_path; 5 com tabela/sequência sem qualificação
        assert len(problems) == 25, sorted(problems)
        unqualified = {s for s, i in problems.items() if any("não qualificad" in x for x in i)}
        assert unqualified == {"rd_audit_chain()", "rd_ledger_check_balanced()", "rd_ledger_posting_guard()",
                               "rd_ledger_transaction_guard()",
                               "rd_reset_screen_anchor(uuid,timestamp with time zone,text,uuid)"}, unqualified


# ═════════════════════════════════════════════════════════════════════════════
# Ataques que FUNCIONAVAM na 0007
# ═════════════════════════════════════════════════════════════════════════════

def test_closed_period_cannot_be_bypassed_with_temp_closings(ctx):
    rev, s = ctx
    cash, revenue = ledger_setup(s)
    s.as_app()
    # controle: sem sombra, período fechado é recusado nas duas revisões
    s.denied("INSERT INTO public.ledger_transactions (id, kind, idempotency_key, competence_date, description) VALUES "
             "(gen_random_uuid(), 'MANUAL', 'ctl', CAST(:d AS date), 'x')", "RD_LEDGER_PERIOD_CLOSED", d=CLOSED_DAY)
    s.allowed("CREATE TEMP TABLE financial_period_closings (LIKE public.financial_period_closings INCLUDING DEFAULTS)")
    sql = ("INSERT INTO public.ledger_transactions (id, kind, idempotency_key, competence_date, description) VALUES "
           "(gen_random_uuid(), 'MANUAL', 'sombra', CAST(:d AS date), 'x')")
    if hardened(rev):
        s.denied(sql, "RD_LEDGER_PERIOD_CLOSED", d=CLOSED_DAY)
    else:
        s.allowed(sql, d=CLOSED_DAY)  # 0007: o guard leu a tabela temporária vazia


def test_closed_period_postings_cannot_be_bypassed(ctx):
    rev, s = ctx
    cash, revenue = ledger_setup(s)
    s.as_app()
    s.allowed("CREATE TEMP TABLE financial_period_closings (LIKE public.financial_period_closings INCLUDING DEFAULTS)")
    sp = s.c.begin_nested()
    try:
        ledger_insert(s, CLOSED_DAY, [(cash, 500), (revenue, -500)])
        s.x("SET CONSTRAINTS ALL IMMEDIATE")
        accepted = True
    except Exception as exc:  # noqa: BLE001
        accepted = False
        assert "RD_LEDGER_PERIOD_CLOSED" in str(exc), exc
    sp.rollback()
    assert accepted is (not hardened(rev))


def test_posting_competence_mismatch_cannot_be_bypassed_with_temp_transactions(ctx):
    rev, s = ctx
    cash, revenue = ledger_setup(s)
    s.as_app()
    tx = uuid.uuid4()
    s.x("INSERT INTO public.ledger_transactions (id, kind, idempotency_key, competence_date, description) VALUES "
        "(:i, 'MANUAL', :k, CAST(:d AS date), 'x')", i=tx, k=f"k-{tx}", d=OPEN_DAY)
    # sombra: a "transação" vista pelo guard tem a competência que o atacante quiser
    s.allowed("CREATE TEMP TABLE ledger_transactions (LIKE public.ledger_transactions INCLUDING DEFAULTS)")
    s.allowed("INSERT INTO ledger_transactions (id, kind, idempotency_key, competence_date, description) VALUES "
              "(:i, 'MANUAL', 'temp', CAST('2021-02-01' AS date), 'x')", i=tx)
    sql = ("INSERT INTO public.ledger_postings (id, transaction_id, account_id, amount_cents, competence_date) VALUES "
           "(gen_random_uuid(), :t, :a, 100, CAST('2021-02-01' AS date))")
    if hardened(rev):
        s.denied(sql, "RD_LEDGER_COMPETENCE_MISMATCH", t=tx, a=cash)
    else:
        s.allowed(sql, t=tx, a=cash)
    # partida órfã (transação só na tabela temporária)
    ghost = uuid.uuid4()
    s.allowed("INSERT INTO ledger_transactions (id, kind, idempotency_key, competence_date, description) VALUES "
              "(:i, 'MANUAL', 'ghost', CAST(:d AS date), 'x')", i=ghost, d=OPEN_DAY)
    orphan = ("INSERT INTO public.ledger_postings (id, transaction_id, account_id, amount_cents, competence_date) VALUES "
              "(gen_random_uuid(), :t, :a, 100, CAST(:d AS date))")
    # sem a transação real, a FK recusa de qualquer forma — o guard recusava antes da FK só no head
    msg = s.denied(orphan, None, t=ghost, a=cash, d=OPEN_DAY)
    if hardened(rev):
        assert "RD_LEDGER_ORPHAN_POSTING" in msg, msg
    else:
        assert "fk_ledger_postings_transaction_id_ledger_transactions" in msg, msg


def test_unbalanced_transaction_cannot_be_bypassed_with_temp_postings(ctx):
    rev, s = ctx
    cash, revenue = ledger_setup(s)
    s.as_app()
    tx = uuid.uuid4()
    s.allowed("CREATE TEMP TABLE ledger_postings (LIKE public.ledger_postings INCLUDING DEFAULTS)")
    s.allowed("INSERT INTO ledger_postings (id, transaction_id, account_id, amount_cents, competence_date) VALUES "
              "(gen_random_uuid(), :t, :a, 50, CAST(:d AS date)), (gen_random_uuid(), :t, :a, -50, CAST(:d AS date))",
              t=tx, a=cash, d=OPEN_DAY)
    sp = s.c.begin_nested()
    try:
        s.x("INSERT INTO public.ledger_transactions (id, kind, idempotency_key, competence_date, description) VALUES "
            "(:i, 'MANUAL', :k, CAST(:d AS date), 'desbalanceada')", i=tx, k=f"k-{tx}", d=OPEN_DAY)
        s.x("INSERT INTO public.ledger_postings (id, transaction_id, account_id, amount_cents, competence_date) VALUES "
            "(gen_random_uuid(), :t, :a, 100000, CAST(:d AS date))", t=tx, a=cash, d=OPEN_DAY)
        s.x("SET CONSTRAINTS ALL IMMEDIATE")  # dispara o constraint trigger diferido (o mesmo que roda no COMMIT)
        accepted = True
    except Exception as exc:  # noqa: BLE001
        accepted = False
        assert "RD_LEDGER_UNBALANCED" in str(exc), exc
    sp.rollback()
    assert accepted is (not hardened(rev))


def test_audit_chain_cannot_be_forged_with_temp_table_and_sequence(ctx):
    rev, s = ctx
    s.x("INSERT INTO audit_logs (id, action, origin, created_at) VALUES (gen_random_uuid(), 'teste.base', 'system', now())")
    real_head, real_seq = s.x("SELECT row_hash, seq FROM public.audit_logs ORDER BY seq DESC LIMIT 1").one()
    s.as_app()
    forged = "f" * 64
    s.allowed("CREATE TEMP TABLE audit_logs (LIKE public.audit_logs INCLUDING DEFAULTS)")
    s.allowed("INSERT INTO audit_logs (id, seq, prev_hash, row_hash, action, origin, created_at) VALUES "
              "(gen_random_uuid(), 1, :h, :h, 'forjado', 'system', now())", h=forged)
    s.allowed("CREATE TEMP SEQUENCE rd_audit_seq START 900000")
    s.allowed("INSERT INTO public.audit_logs (id, action, origin, created_at) VALUES "
              "(gen_random_uuid(), 'teste.sombra', 'system', now())")
    prev, seq = s.x("SELECT prev_hash, seq FROM public.audit_logs WHERE action = 'teste.sombra'").one()
    if hardened(rev):
        assert prev == real_head and seq == real_seq + 1
    else:
        assert prev == forged and seq == 900000  # 0007: encadeamento e sequência controlados pelo atacante


def test_anchor_reset_cannot_be_redirected_to_temp_tables(ctx):
    rev, s = ctx
    s.as_app()
    screen = uuid.uuid4()
    s.allowed("CREATE TEMP TABLE screen_inventory_state (LIKE public.screen_inventory_state INCLUDING DEFAULTS)")
    s.allowed("CREATE TEMP TABLE screen_anchor_history (LIKE public.screen_anchor_history INCLUDING DEFAULTS)")
    s.allowed("INSERT INTO screen_inventory_state (screen_id, timezone) VALUES (:s, 'America/Sao_Paulo')", s=screen)
    sql = "SELECT rd_reset_screen_anchor(:s, now(), 'justificativa de teste', NULL)"
    if hardened(rev):
        s.denied(sql, "RD_ANCHOR_RESET_NOT_FOUND", s=screen)  # tela inexistente na tabela REAL
    else:
        s.allowed(sql, s=screen)  # 0007: "reset" gravado só nas tabelas temporárias do atacante
        assert s.one("SELECT count(*) FROM public.screen_anchor_history") == 0


def committed_screen_with_anchor(db: str) -> uuid.UUID:
    """Tela + estado de inventário com âncora já definida, CONFIRMADOS (linhas de outra transação não
    re-checam FK no UPDATE). Sem organização/estabelecimento reais (modo réplica só nesta preparação)."""
    import secrets as _secrets

    from sqlalchemy import func

    from app.models.inventory import ScreenInventoryState
    from app.models.screens import Screen

    sid = uuid.uuid4()
    code = "".join(_secrets.choice("ABCDEFGHJMNPQRSTUVYZ23456789") for _ in range(4))
    eng = create_engine(_url(db))
    try:
        with eng.begin() as c:
            c.execute(text("SET LOCAL session_replication_role = replica"))
            c.execute(Screen.__table__.insert().values(id=sid, establishment_id=uuid.uuid4(), friendly_id=f"TEL-GO-{code}",
                                                       name="Tela de teste da âncora"))
            c.execute(ScreenInventoryState.__table__.insert().values(
                screen_id=sid, timezone="America/Sao_Paulo", anchor_utc=func.now(), anchor_source="ADMIN_RESET"))
    finally:
        eng.dispose()
    return sid


def test_anchor_write_once_cannot_be_bypassed_with_guc_and_reset_still_works(ctx, shadow_db):
    rev, s = ctx
    sid = committed_screen_with_anchor(shadow_db[1])
    reset_sig = "rd_reset_screen_anchor(uuid,timestamp with time zone,text,uuid)"
    s.as_app()
    upd = "UPDATE public.screen_inventory_state SET anchor_utc = anchor_utc - interval '1 hour' WHERE screen_id = :s"
    s.denied(upd, "RD_ANCHOR_WRITE_ONCE", s=sid)  # controle: sem o GUC, recusado nas duas revisões
    s.x("SET LOCAL rd.anchor_reset = 'on'")  # qualquer sessão define um GUC personalizado
    if hardened(rev):
        s.denied(upd, "RD_ANCHOR_WRITE_ONCE", s=sid)
    else:
        s.allowed(upd, s=sid)  # 0007: âncora trocada sem justificativa nem histórico
    s.x("SET LOCAL rd.anchor_reset = 'off'")
    # reset LEGÍTIMO pela função, como rd_app: âncora trocada + histórico real (nas duas revisões)
    hist = "SELECT count(*) FROM public.screen_anchor_history WHERE screen_id = :s"
    n0 = s.one(hist, s=sid)
    s.denied("SELECT rd_reset_screen_anchor(:s, now(), 'curta', NULL)", "RD_ANCHOR_RESET_REASON", s=sid)
    new = s.one("SELECT rd_reset_screen_anchor(:s, now() - interval '2 days', 'Reinstalação do player na loja', NULL)",
                s=sid)
    assert new is not None
    assert s.one(hist, s=sid) == n0 + 1
    assert s.x("SELECT anchor_utc = :n, anchor_source FROM public.screen_inventory_state WHERE screen_id = :s",
               n=new, s=sid).one() == (True, "ADMIN_RESET")
    assert s.one("SELECT current_setting('rd.anchor_reset', true)") == "off"
    s.denied(upd, "RD_ANCHOR_WRITE_ONCE", s=sid)  # depois do reset, UPDATE direto segue bloqueado
    definer, public_exec = s.x(
        "SELECT p.prosecdef, coalesce((SELECT bool_or(a.grantee = 0) FROM aclexplode(p.proacl) a), true) "
        "FROM pg_proc p WHERE p.oid = CAST(:f AS regprocedure)", f=reset_sig).one()
    assert (definer, public_exec) == ((True, False) if hardened(rev) else (False, True))
    if hardened(rev) and s.one("SELECT exists (SELECT 1 FROM pg_roles WHERE rolname = 'rd_backup')"):
        assert s.one("SELECT has_function_privilege('rd_backup', CAST(:f AS regprocedure), 'EXECUTE')", f=reset_sig) is False


# ═════════════════════════════════════════════════════════════════════════════
# Guards sem referência a tabela: a sombra não tem o que desviar (nas duas revisões)
# ═════════════════════════════════════════════════════════════════════════════

def test_withdrawal_guard_holds_with_temp_withdrawals(ctx):
    rev, s = ctx
    a, b = s.dual_two_masters()
    rid = s.request(a, "DUAL", action="RD_WITHDRAWAL")
    wid = uuid.uuid4()
    s.x("INSERT INTO withdrawals (id, kind, origin, pix_key_type, destination_enc, destination_masked, amount_cents, "
        "status, approval_request_id, idempotency_key) VALUES (:i, 'RD', 'MANUAL', 'email', 'v1:x', 'a***@x', 1000, "
        "'REQUESTED', :r, :k)", i=wid, r=rid, k=f"w-{wid}")
    s.as_app()
    s.allowed("CREATE TEMP TABLE withdrawals (LIKE public.withdrawals)")
    s.denied("UPDATE public.withdrawals SET amount_cents = 999999 WHERE id = :i", "RD_IMMUTABLE", i=wid)
    s.denied("UPDATE public.withdrawals SET status = 'REQUESTED', destination_enc = 'v1:atacante' WHERE id = :i",
             "RD_IMMUTABLE", i=wid)
    s.allowed("UPDATE public.withdrawals SET status = 'EXECUTING' WHERE id = :i", i=wid)
    s.denied("UPDATE public.withdrawals SET status = 'REQUESTED' WHERE id = :i", "RD_WITHDRAWAL_TRANSITION", i=wid)
    s.denied("DELETE FROM public.withdrawals WHERE id = :i", "permission denied", i=wid)


def test_append_only_and_approval_guards_hold_with_temp_tables(ctx):
    rev, s = ctx
    a, b = s.dual_two_masters()
    s.x("INSERT INTO audit_logs (id, action, origin, created_at) VALUES (gen_random_uuid(), 'teste.base', 'system', now())")
    # dono (sem o REVOKE): o trigger append-only decide — com sombra temporária presente
    s.x("CREATE TEMP TABLE audit_logs (LIKE public.audit_logs INCLUDING DEFAULTS)")
    s.denied("UPDATE public.audit_logs SET action = 'adulterado'", "RD_APPEND_ONLY")
    s.denied("DELETE FROM public.audit_logs", "RD_APPEND_ONLY")
    s.as_app()
    rid = s.request(a, "DUAL")  # antes das tabelas temporárias (o helper usa nomes não qualificados)
    s.allowed("CREATE TEMP TABLE instance_approval_mode_history (LIKE public.instance_approval_mode_history)")
    s.allowed("CREATE TEMP TABLE financial_approval_requests (LIKE public.financial_approval_requests)")
    s.denied("INSERT INTO public.financial_approval_requests (id, action_type, payload, payload_sha256, reason, "
             "requested_by, status, approval_mode, expires_at, execution_state, idempotency_key) VALUES "
             "(gen_random_uuid(), 'RD_WITHDRAWAL', '{}', :h, 'motivo do teste', :a, 'PENDING', 'SINGLE', "
             "now() + interval '1 day', 'NOT_STARTED', 'k-sombra')", "RD_APPROVAL_MODE_MISMATCH", h="0" * 64, a=a)
    s.denied("UPDATE public.financial_approval_requests SET payload = :p WHERE id = :r", "RD_IMMUTABLE",
             p=json.dumps({"x": 2}), r=rid)
    s.denied("UPDATE public.financial_approval_requests SET status = 'APPROVED', approved_by = :a, approved_at = now() "
             "WHERE id = :r", "RD_APPROVER_NOT_ACTIVE_MASTER", a=a, r=rid)


def test_security_guards_hold_with_temp_tables(ctx):
    rev, s = ctx
    a, b = s.dual_two_masters()
    s.as_app()
    for t in ("users", "roles", "role_assignments", "admin_master_invites"):
        s.allowed(f"CREATE TEMP TABLE {t} (LIKE public.{t})")
    assert s.one("SELECT public.rd_active_master_count()") == 2
    assert s.one("SELECT public.rd_security_privileged()") is False
    s.denied("UPDATE public.users SET totp_enabled = false WHERE id = :b", "RD_MASTER_TOTP_PROTECTED", b=b)
    s.denied("DELETE FROM public.role_assignments WHERE user_id = :b", "RD_MASTER_ROLE_PROTECTED", b=b)
