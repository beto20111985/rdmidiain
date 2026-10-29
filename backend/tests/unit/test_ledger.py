"""Guardas do ledger em partidas dobradas (validação ANTES de qualquer acesso ao banco).

AÇÃO 002 (W-FIN): reescrito para o ledger novo (``app.modules.financial.ledger``). O
``LedgerService`` legado (entrada simples por organização) e a calculadora de margem
com comissão de 30 % foram REMOVIDOS (regra oficial: pool padrão 5 %, configurável —
P002 itens 5 e 30). As garantias no banco (append-only, soma zero diferida, período
fechado) estão em ``tests/integration/test_fin_ledger.py``.
"""
import uuid

import pytest

from app.modules.financial import ledger


class _ForbiddenDB:
    def __getattr__(self, name):
        raise AssertionError(f"o banco não deveria ser acessado (atributo {name})")


A, B = uuid.uuid4(), uuid.uuid4()


async def test_post_rejects_float_before_touching_db():
    with pytest.raises(TypeError):
        await ledger.post(_ForbiddenDB(), kind="T", idempotency_key="k", legs=[(A, 10.5), (B, -10.5)], description="x")


async def test_post_rejects_bool_amounts():
    with pytest.raises(TypeError):
        await ledger.post(_ForbiddenDB(), kind="T", idempotency_key="k", legs=[(A, True), (B, -1)], description="x")


async def test_post_rejects_unbalanced_before_touching_db():
    with pytest.raises(ledger.LedgerError) as ei:
        await ledger.post(_ForbiddenDB(), kind="T", idempotency_key="k", legs=[(A, 100), (B, -99)], description="x")
    assert ei.value.code == "LEDGER_UNBALANCED"


async def test_post_rejects_zero_and_single_leg():
    for legs in ([(A, 0), (B, 0)], [(A, 100)], [(A, 50), (B, 0)]):
        with pytest.raises(ledger.LedgerError) as ei:
            await ledger.post(_ForbiddenDB(), kind="T", idempotency_key="k", legs=legs, description="x")
        assert ei.value.code == "LEDGER_TOO_FEW_LEGS"


def test_chart_has_minimum_accounts_and_no_unknown_codes():
    for code in ("RD:Cash:CloudPayPag", "ADV:DeferredRevenue", "RD:Revenue:Media", "RD:Expense:GatewayFees",
                 "RD:Expense:Commission", "EST:CommissionPayable:Pending", "EST:CommissionPayable:Available",
                 "EST:Receivable", "RD:ContraRevenue:Refunds", "RD:Income:RoundingResidue", "RD:AccountsPayable",
                 "SELLER:CommissionPayable", "RD:Equity:Withdrawals"):
        assert ledger.account_def(code).code == code
    assert ledger.account_def("RD:Expense:Opex:aluguel").dre_group == "DESPESAS_OPERACIONAIS"
    with pytest.raises(ledger.LedgerError):
        ledger.account_def("RD:Inventada")


def test_natural_balance_sign():
    assert ledger.natural_balance("ASSET", 100) == 100
    assert ledger.natural_balance("LIABILITY", -100) == 100
    assert ledger.natural_balance("INCOME", -5) == 5
    assert ledger.natural_balance("EXPENSE", 7) == 7
