"""Ledger em partidas dobradas — garantias NO BANCO (PostgreSQL real): append-only,
soma zero por transação (constraint trigger diferida), reversão ligada, período fechado,
idempotência e saldo = Σ partidas."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models.financial import FinancialPeriodClosing, LedgerPosting, LedgerTransaction
from app.modules.financial import ledger
from tests.fin_helpers import fin_env  # noqa: F401


async def _pair(db, amount=1000, key=None):
    cash = await ledger.account(db, "RD:Cash:CloudPayPag")
    rev = await ledger.account(db, "RD:Revenue:Services")
    res = await ledger.post(db, kind="TEST", idempotency_key=key or f"t:{uuid.uuid4()}",
                            legs=[(cash, amount), (rev, -amount)], description="teste")
    await db.commit()
    return res, cash, rev


async def test_balanced_post_and_balance_is_sum_of_postings(db, fin_env):
    res, cash, rev = await _pair(db, 1234)
    await _pair(db, 66)
    assert res.created
    assert await ledger.balance_of(db, "RD:Cash:CloudPayPag") == 1300
    assert await ledger.balance_of(db, "RD:Revenue:Services") == 1300  # receita: saldo credor natural
    raw = (await db.execute(select(LedgerPosting.amount_cents))).scalars().all()
    assert sum(raw) == 0


@pytest.mark.parametrize("stmt", [
    "UPDATE ledger_postings SET amount_cents = amount_cents + 1",
    "DELETE FROM ledger_postings",
    "TRUNCATE ledger_postings CASCADE",
    "UPDATE ledger_transactions SET description = 'x'",
    "DELETE FROM ledger_transactions",
    "TRUNCATE ledger_transactions CASCADE",
])
async def test_ledger_is_append_only_in_the_database(db, fin_env, stmt):
    await _pair(db)
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text(stmt))
        await db.commit()
    await db.rollback()
    assert "RD_APPEND_ONLY" in str(ei.value)
    assert (await db.execute(select(LedgerPosting))).scalars().all()  # nada mudou


async def test_unbalanced_transaction_fails_at_commit(db, fin_env):
    cash = await ledger.account(db, "RD:Cash:CloudPayPag")
    rev = await ledger.account(db, "RD:Revenue:Services")
    await db.commit()
    tx = uuid.uuid4()
    comp = ledger.today_local()
    db.add(LedgerTransaction(id=tx, kind="HACK", idempotency_key=f"h:{tx}", competence_date=comp, description="x"))
    await db.flush()
    db.add_all([LedgerPosting(id=uuid.uuid4(), transaction_id=tx, account_id=cash.id, amount_cents=100, competence_date=comp),
                LedgerPosting(id=uuid.uuid4(), transaction_id=tx, account_id=rev.id, amount_cents=-99, competence_date=comp)])
    await db.flush()  # inserção passa: a checagem é DIFERIDA
    with pytest.raises(DBAPIError) as ei:
        await db.commit()
    await db.rollback()
    assert "RD_LEDGER_UNBALANCED" in str(ei.value)
    assert (await db.execute(select(LedgerTransaction).where(LedgerTransaction.id == tx))).first() is None


async def test_transaction_without_postings_fails_at_commit(db, fin_env):
    tx = uuid.uuid4()
    db.add(LedgerTransaction(id=tx, kind="HACK", idempotency_key=f"h:{tx}", competence_date=ledger.today_local(),
                             description="sem partidas"))
    with pytest.raises(DBAPIError):
        await db.commit()
    await db.rollback()


async def test_service_rejects_unbalanced_float_and_single_leg(db, fin_env):
    cash = await ledger.account(db, "RD:Cash:CloudPayPag")
    rev = await ledger.account(db, "RD:Revenue:Services")
    with pytest.raises(ledger.LedgerError):
        await ledger.post(db, kind="T", idempotency_key="u1", legs=[(cash, 10), (rev, -9)], description="x")
    with pytest.raises(TypeError):
        await ledger.post(db, kind="T", idempotency_key="u2", legs=[(cash, 10.0), (rev, -10.0)], description="x")
    with pytest.raises(ledger.LedgerError):
        await ledger.post(db, kind="T", idempotency_key="u3", legs=[(cash, 10)], description="x")
    with pytest.raises(DBAPIError):  # coluna amount_cents ≠ 0 também no banco
        db.add(LedgerPosting(id=uuid.uuid4(), transaction_id=uuid.uuid4(), account_id=cash.id, amount_cents=0,
                             competence_date=ledger.today_local()))
        await db.flush()
    await db.rollback()


async def test_idempotency_key_returns_same_transaction(db, fin_env):
    r1, cash, _ = await _pair(db, 500, key="same-key")
    r2, _, _ = await _pair(db, 500, key="same-key")
    assert r1.transaction_id == r2.transaction_id and r1.created and not r2.created
    assert await ledger.balance_of(db, "RD:Cash:CloudPayPag") == 500


async def test_reversal_is_linked_new_transaction_and_only_once(db, fin_env):
    r, cash, _ = await _pair(db, 700)
    rev = await ledger.reverse(db, r.transaction_id, reason="lançamento indevido")
    await db.commit()
    assert rev.created
    t = await db.get(LedgerTransaction, rev.transaction_id)
    assert t.reverses_transaction_id == r.transaction_id and t.kind == "REVERSAL"
    assert await ledger.balance_of(db, "RD:Cash:CloudPayPag") == 0
    again = await ledger.reverse(db, r.transaction_id, reason="de novo")
    assert again.transaction_id == rev.transaction_id and not again.created  # nunca reverte duas vezes
    # UNIQUE no banco também impede uma segunda reversão
    with pytest.raises(IntegrityError):
        db.add(LedgerTransaction(id=uuid.uuid4(), kind="REVERSAL", idempotency_key="other", competence_date=ledger.today_local(),
                                 description="x", reverses_transaction_id=r.transaction_id))
        await db.flush()
    await db.rollback()


async def test_closed_period_rejects_postings(db, fin_env):
    last_month_end = ledger.month_start(ledger.today_local()) - timedelta(days=1)
    start = ledger.month_start(last_month_end)
    db.add(FinancialPeriodClosing(id=uuid.uuid4(), period_start=start, period_end=last_month_end, snapshot={},
                                  snapshot_sha256="0" * 64, transaction_count=0))
    await db.commit()
    cash = await ledger.account(db, "RD:Cash:CloudPayPag")
    rev = await ledger.account(db, "RD:Revenue:Services")
    # manual (sem deslocamento): recusado pelo serviço
    with pytest.raises(ledger.LedgerError) as ei:
        await ledger.post(db, kind="T", idempotency_key="closed-1", legs=[(cash, 5), (rev, -5)], description="x",
                          competence_date=start + timedelta(days=3), shift_if_closed=False)
    assert ei.value.code == "LEDGER_PERIOD_CLOSED"
    # e pelo BANCO (inserção direta)
    tx = uuid.uuid4()
    with pytest.raises(DBAPIError) as ei2:
        db.add(LedgerTransaction(id=tx, kind="HACK", idempotency_key="closed-2", competence_date=start, description="x"))
        await db.flush()
    await db.rollback()
    assert "RD_LEDGER_PERIOD_CLOSED" in str(ei2.value)
    # automático: deslocado para o período aberto
    cash = await ledger.account(db, "RD:Cash:CloudPayPag")
    rev = await ledger.account(db, "RD:Revenue:Services")
    res = await ledger.post(db, kind="T", idempotency_key="closed-3", legs=[(cash, 5), (rev, -5)], description="x",
                            competence_date=start)
    await db.commit()
    assert res.competence_date > last_month_end
    # fechamento é imutável
    with pytest.raises(DBAPIError):
        await db.execute(text("DELETE FROM financial_period_closings"))
        await db.commit()
    await db.rollback()


async def test_no_mutable_balance_column_in_financial_tables(db, fin_env):
    rows = (await db.execute(text(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND (table_name LIKE 'ledger%' OR table_name IN "
        "('payment_orders','withdrawals','commission_accruals','commission_closings','refunds','payables'))"))).all()
    names = {(t, c) for t, c, _ in rows}
    assert not [n for n in names if "balance" in n[1]]
    money = [(t, c, ty) for t, c, ty in rows if c.endswith("_cents") or c.endswith("_ucents")]
    assert money and all(ty == "bigint" for _, _, ty in money), money
    assert not [r for r in rows if r[2] in ("double precision", "real", "numeric")]
