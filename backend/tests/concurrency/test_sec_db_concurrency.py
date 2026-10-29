"""AÇÃO 003 / agente E — concorrência REAL no banco (conexões e transações independentes, commits reais).

Banco DESCARTÁVEL migrado até o head (0007). Cada cenário dispara N transações ao mesmo tempo
(barreira) e confere o estado final confirmado:
- 10× ``rd_security_init_approval_mode`` em paralelo (modos diferentes) ⇒ 1 linha, 1 ``created``;
- 2× atribuição do assento 2 ⇒ 1 vence (com e sem o advisory lock tomado antes do selo);
- 2× convite PENDENTE para o assento 2 ⇒ 1 vence (índice único parcial);
- 2× bootstrap do assento 1 (usuários diferentes) ⇒ 1 assento, 1 evento;
- 2× transição de modo (pedidos diferentes, mesma versão) ⇒ 1 vence.
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.modules.security import ddl as sec_ddl
from tests.adversarial.test_sec_db_rd_app import TRANSITION_SQL, Sec
from tests.migrations.test_migrations import _url, alembic, scratch_db

RESET_TABLES = ("admin_master_seat_events", "instance_approval_mode_history", "financial_approval_events",
                "financial_approval_requests", "admin_master_invites", "user_recovery_codes", "role_assignments",
                "user_sessions", "users")


@pytest.fixture(scope="module")
def cdb(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "head")
        yield db


@pytest.fixture
def engine(cdb):
    eng = create_engine(_url(cdb), pool_size=12, max_overflow=4)
    try:
        yield eng
    finally:
        with eng.begin() as c:  # limpeza (atravessa triggers append-only)
            c.execute(text("SET LOCAL session_replication_role = replica"))
            for t in RESET_TABLES:
                c.execute(text(f"DELETE FROM {t}"))
        eng.dispose()


def setup(engine, fn):
    with engine.begin() as c:
        return fn(Sec(c))


def race(engine, workers: list, *, hold: float = 0.3) -> list:
    """Executa cada ``worker(sec)`` numa transação própria, todos liberados juntos; ``hold`` segura o COMMIT
    para sobrepor as transações. Retorna, por worker, o resultado ou a exceção."""
    barrier = threading.Barrier(len(workers))
    out: list = [None] * len(workers)

    def run(i, fn):
        try:
            with engine.connect() as conn:
                barrier.wait(timeout=30)
                with conn.begin():
                    res = fn(Sec(conn))
                    time.sleep(hold)
                out[i] = ("ok", res)
        except DBAPIError as exc:
            out[i] = ("err", str(exc.orig))
        except Exception as exc:  # noqa: BLE001 — falha inesperada aparece no assert
            out[i] = ("exc", repr(exc))

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert all(not t.is_alive() for t in threads), "transação travada"
    return out


def count(engine, sql: str, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).scalar()


def test_parallel_mode_initialization_creates_exactly_one_row(engine):
    modes = ["DUAL" if i % 2 else "SINGLE" for i in range(10)]
    results = race(engine, [lambda s, m=m: tuple(s.init_mode(m)) for m in modes], hold=0.05)
    assert all(r[0] == "ok" for r in results), results
    created = [r[1] for r in results if r[1][2]]
    assert len(created) == 1, results
    winner = created[0][0]
    assert {r[1][0] for r in results} == {winner}  # todos devolvem o modo efetivo
    assert count(engine, "SELECT count(*) FROM instance_approval_mode_history") == 1
    assert count(engine, "SELECT rd_security_current_approval_mode()") == winner


@pytest.mark.parametrize("pre_lock", [True, False])
def test_two_concurrent_seat2_assignments_only_one_wins(engine, pre_lock):
    def base(s: Sec):
        s.init_mode("DUAL")
        a = s.user("a")
        s.assign(a, 1, "BOOTSTRAP_ENV")
        s.ready(a)
        return a, s.user("c1"), s.user("c2")

    a, c1, c2 = setup(engine, base)
    head = setup(engine, lambda s: s.seat_head())

    def worker(uid):
        def fn(s: Sec):
            if pre_lock:  # caminho correto (B): lock → cabeça → selo → função
                s.assign(uid, 2, "CLI_BOOTSTRAP")
            else:  # ambos calculam o selo sobre a MESMA cabeça antes do lock
                s.assign(uid, 2, "CLI_BOOTSTRAP", prev=head)
            return uid
        return fn

    results = race(engine, [worker(c1), worker(c2)])
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(oks) == 1 and len(errs) == 1, results
    assert ("RD_MASTER_SEAT_OCCUPIED" if pre_lock else "RD_SEAL_CHAIN_MISMATCH") in errs[0][1], errs
    assert count(engine, "SELECT count(*) FROM users WHERE admin_master_seat = 2") == 1
    assert count(engine, "SELECT count(*) FROM admin_master_seat_events WHERE seat = 2") == 1
    assert count(engine, "SELECT count(*) FROM users WHERE admin_master_seat IS NOT NULL") == 2


def test_two_concurrent_pending_invites_only_one_wins(engine):
    def base(s: Sec):
        s.init_mode("DUAL")
        a = s.user("a")
        s.assign(a, 1, "BOOTSTRAP_ENV")
        s.ready(a)
        return a

    a = setup(engine, base)
    results = race(engine, [lambda s: s.invite(a)[0], lambda s: s.invite(a)[0]])
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(oks) == 1 and len(errs) == 1, results
    assert "uq_admin_master_invites_pending_seat" in errs[0][1]
    assert count(engine, "SELECT count(*) FROM admin_master_invites WHERE status = 'PENDING'") == 1


def test_two_concurrent_bootstraps_produce_one_seat(engine):
    setup(engine, lambda s: s.init_mode("DUAL"))

    def bootstrap(label):
        def fn(s: Sec):
            s.x("SELECT pg_advisory_xact_lock(:k)", k=sec_ddl.LOCK_BOOTSTRAP)  # como o bootstrap-instance
            uid = s.user(label)
            s.assign(uid, 1, "BOOTSTRAP_ENV")
            return uid
        return fn

    def bootstrap_without_lock(label):
        def fn(s: Sec):
            uid = s.user(label)
            s.assign(uid, 1, "BOOTSTRAP_ENV")
            return uid
        return fn

    results = race(engine, [bootstrap("m1"), bootstrap("m2"), bootstrap_without_lock("m3")])
    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 1, results
    for r in results:
        if r[0] == "err":
            assert any(code in r[1] for code in ("RD_MASTER_SEAT_OCCUPIED", "RD_MASTER_SEAT_INVALID_STATE",
                                                 "RD_SEAL_CHAIN_MISMATCH")), r
        else:
            assert r[0] == "ok", r
    assert count(engine, "SELECT count(*) FROM users WHERE admin_master_seat IS NOT NULL") == 1
    assert count(engine, "SELECT count(*) FROM admin_master_seat_events") == 1
    assert count(engine, "SELECT admin_master_seat FROM users WHERE id = :u", u=oks[0][1]) == 1


def test_two_concurrent_mode_transitions_only_one_wins(engine):
    def base(s: Sec):
        a, b = s.dual_two_masters()
        ver, _, _ = s.mode_head()
        rids = []
        for _ in range(2):
            rid = s.mode_change_request(a, target="SINGLE", from_mode="DUAL", version=ver)
            s.x("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :b, approved_at = now() "
                "WHERE id = :r", b=b, r=rid)
            s.event(rid, "APPROVED", b)
            rids.append(rid)
        return rids

    rids = setup(engine, base)

    def transition(rid):
        def fn(s: Sec):
            s.x("SELECT pg_advisory_xact_lock(:k)", k=sec_ddl.LOCK_APPROVAL_MODE)
            return s.one(TRANSITION_SQL, **s.transition_sql_params(rid))
        return fn

    results = race(engine, [transition(r) for r in rids])
    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(oks) == 1 and len(errs) == 1, results
    assert oks[0][1] == 2
    assert "RD_APPROVAL_MODE_MISMATCH" in errs[0][1]  # o perdedor encontra o modo já mudado
    assert count(engine, "SELECT count(*) FROM instance_approval_mode_history") == 2
    assert count(engine, "SELECT rd_security_current_approval_mode()") == "SINGLE"
