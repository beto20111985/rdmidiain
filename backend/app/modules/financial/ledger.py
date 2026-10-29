"""
RD Mídia Indoor — Ledger em partidas dobradas (ADR_009 item 2; A5 D1).

    from app.modules.financial import ledger
    cash = await ledger.account(db, "RD:Cash:CloudPayPag")
    deferred = await ledger.account(db, "ADV:DeferredRevenue", owner_type="ADVERTISER", owner_id=org_id, org_id=org_id)
    res = await ledger.post(db, kind="PAYMENT_RECEIVED", idempotency_key="cpp:charge:sc_2_x:paid",
                            legs=[(cash, 9691), (fees, 299), (deferred, -9990)],
                            description="Pagamento confirmado")

Regras:
- Valores ``int`` em centavos; sinal + = débito, − = crédito; Σ = 0; ≥ 2 partidas
  (validado aqui E no banco por constraint trigger diferida).
- Idempotência: ``idempotency_key`` UNIQUE + ``INSERT … ON CONFLICT DO NOTHING``
  (reenvio devolve a transação existente, ``created=False``).
- Imutável: nada de UPDATE/DELETE (trigger). Correção = ``reverse(...)`` (transação
  ligada; uma transação só pode ser revertida uma vez — UNIQUE).
- Saldo = Σ partidas (nenhuma coluna de saldo). ``natural_balance`` devolve o saldo
  no sentido natural da conta (passivo/receita/PL positivos quando credores).
- Competência em período fechado: lançamentos automáticos são deslocados para o
  primeiro dia aberto (``shift_if_closed=True``); ajuste manual em período fechado
  é recusado (o banco também recusa).
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Optional, Sequence, Union

from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, DomainError
from app.core.timeutils import DEFAULT_TIMEZONE, local_date, utcnow
from app.models.financial import (
    AccountKind,
    FinancialPeriodClosing,
    LedgerAccount,
    LedgerPosting,
    LedgerTransaction,
    OwnerType,
)

FINANCIAL_TZ = DEFAULT_TIMEZONE  # competência financeira: America/Sao_Paulo


class LedgerError(DomainError):
    status, code, message = 400, "LEDGER_INVALID", "Lançamento contábil inválido."


@dataclass(frozen=True)
class AccountDef:
    code: str
    name: str
    kind: str
    owner_type: str = OwnerType.RD.value
    is_cash: bool = False
    dre_group: Optional[str] = None


A, L, E, I, X = (AccountKind.ASSET.value, AccountKind.LIABILITY.value, AccountKind.EQUITY.value,
                 AccountKind.INCOME.value, AccountKind.EXPENSE.value)
RD, EST, ADV, SELLER = (OwnerType.RD.value, OwnerType.ESTABLISHMENT.value, OwnerType.ADVERTISER.value,
                        OwnerType.SELLER.value)

# Plano de contas mínimo (A5 D1.4). Contas por dono são criadas sob demanda.
CHART: dict[str, AccountDef] = {d.code: d for d in [
    AccountDef("RD:Cash:CloudPayPag", "Caixa — subconta CloudPayPag", A, is_cash=True),
    AccountDef("RD:Cash:CloudPayPag:Blocked", "Caixa — saldo bloqueado via API", A, is_cash=True),
    AccountDef("RD:Cash:External", "Caixa/banco fora da CloudPayPag", A, is_cash=True),
    AccountDef("RD:Revenue:Media", "Receita de mídia", I, dre_group="RECEITA_MIDIA"),
    AccountDef("RD:Revenue:Services", "Receita de serviços (arte/instalação)", I, dre_group="RECEITA_SERVICOS"),
    AccountDef("RD:Revenue:PartnerFees", "Receita de mensalidades de parceiros", I, dre_group="RECEITA_SERVICOS"),
    AccountDef("RD:ContraRevenue:Refunds", "(−) Reembolsos/cancelamentos", I, dre_group="DEDUCOES"),
    AccountDef("RD:Expense:GatewayFees", "Taxas CloudPayPag", X, dre_group="TAXAS_GATEWAY"),
    AccountDef("RD:Expense:Commission", "Comissão de estabelecimentos (pool)", X, dre_group="COMISSOES"),
    AccountDef("RD:Income:RoundingResidue", "Resíduo do pool de comissão (RD)", I, dre_group="COMISSOES"),
    AccountDef("RD:Expense:SellerCommission", "Comissão de vendedores", X, dre_group="COMISSOES_VENDEDOR"),
    AccountDef("RD:Expense:Chargebacks", "Chargebacks", X, dre_group="DEDUCOES"),
    AccountDef("RD:Expense:Adjustments", "Ajustes (despesa)", X, dre_group="OUTRAS_DESPESAS"),
    AccountDef("RD:Income:Adjustments", "Ajustes (receita)", I, dre_group="OUTRAS_RECEITAS"),
    AccountDef("RD:AccountsPayable", "Contas a pagar", L),
    AccountDef("RD:TaxesPayable", "Impostos a recolher", L),
    AccountDef("RD:Equity:Withdrawals", "Retiradas dos sócios", E),
    AccountDef("RD:Equity:Capital", "Capital", E),
    AccountDef("ADV:DeferredRevenue", "Receita diferida do anunciante", L, owner_type=ADV),
    AccountDef("ADV:RefundPayable", "Reembolso a pagar ao anunciante", L, owner_type=ADV),
    AccountDef("EST:CommissionPayable:Pending", "Comissão pendente", L, owner_type=EST),
    AccountDef("EST:CommissionPayable:Available", "Comissão disponível", L, owner_type=EST),
    AccountDef("EST:CommissionPayable:InTransit", "Saque em andamento", L, owner_type=EST),
    AccountDef("EST:Receivable", "A receber do estabelecimento (saldo negativo)", A, owner_type=EST),
    AccountDef("EST:DepositHeld", "Caução recebida do parceiro (passivo reembolsável)", L, owner_type=EST),
    AccountDef("SELLER:CommissionPayable", "Comissão de vendedor a pagar", L, owner_type=SELLER),
]}
OPEX_PREFIX = "RD:Expense:Opex:"


def account_def(code: str) -> AccountDef:
    d = CHART.get(code)
    if d is not None:
        return d
    if code.startswith(OPEX_PREFIX) and len(code) > len(OPEX_PREFIX):
        cat = code[len(OPEX_PREFIX):]
        if not cat.replace("_", "").replace("-", "").isalnum():
            raise LedgerError("LEDGER_ACCOUNT_INVALID", "Categoria de despesa inválida.")
        return AccountDef(code, f"Despesa — {cat}", X, dre_group="DESPESAS_OPERACIONAIS")
    raise LedgerError("LEDGER_ACCOUNT_UNKNOWN", "Conta contábil fora do plano de contas.", details={"code": code})


def _account_key(code: str, owner_type: str, owner_id: Optional[uuid.UUID]) -> str:
    return f"{code}|{owner_type}|{owner_id or '-'}"


async def account(
    db: AsyncSession,
    code: str,
    *,
    owner_type: Optional[str] = None,
    owner_id: Any = None,
    org_id: Any = None,
) -> LedgerAccount:
    """Get-or-create (concorrência segura: INSERT ON CONFLICT DO NOTHING)."""
    d = account_def(code)
    ot = owner_type or d.owner_type
    if ot != d.owner_type:
        raise LedgerError("LEDGER_ACCOUNT_OWNER", "Dono incompatível com a conta.", details={"code": code})
    oid = None if ot == RD else (owner_id if isinstance(owner_id, uuid.UUID) else uuid.UUID(str(owner_id)))
    if ot != RD and oid is None:
        raise LedgerError("LEDGER_ACCOUNT_OWNER", "Conta exige dono.", details={"code": code})
    key = _account_key(code, ot, oid)
    row = (await db.execute(select(LedgerAccount).where(LedgerAccount.account_key == key))).scalar_one_or_none()
    if row is not None:
        return row
    org = org_id if (org_id is None or isinstance(org_id, uuid.UUID)) else uuid.UUID(str(org_id))
    await db.execute(
        pg_insert(LedgerAccount.__table__)
        .values(id=uuid.uuid4(), account_key=key, code=code, name=d.name, kind=d.kind, owner_type=ot, owner_id=oid,
                org_id=org, is_cash=d.is_cash, dre_group=d.dre_group)
        .on_conflict_do_nothing(constraint="uq_ledger_accounts_account_key")
    )
    return (await db.execute(select(LedgerAccount).where(LedgerAccount.account_key == key))).scalar_one()


async def find_account(db: AsyncSession, code: str, owner_id: Any = None) -> Optional[LedgerAccount]:
    d = account_def(code)
    oid = None if d.owner_type == RD else uuid.UUID(str(owner_id))
    return (
        await db.execute(select(LedgerAccount).where(LedgerAccount.account_key == _account_key(code, d.owner_type, oid)))
    ).scalar_one_or_none()


def today_local() -> date:
    return local_date(utcnow(), FINANCIAL_TZ)


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def local_range_utc(start: date, end: date) -> tuple:
    """[início de ``start``, início do dia seguinte a ``end``) em UTC no fuso financeiro (São Paulo).

    Use para filtrar instantes (``paid_at``, ``completed_at``) por dias locais — nunca ``date(ts)``,
    que depende do TimeZone da sessão (UTC) e desloca a virada do dia em 3 h.
    """
    from app.core.timeutils import local_day_bounds_utc

    return local_day_bounds_utc(start, FINANCIAL_TZ)[0], local_day_bounds_utc(end, FINANCIAL_TZ)[1]


async def last_closed_end(db: AsyncSession) -> Optional[date]:
    return (await db.execute(select(func.max(FinancialPeriodClosing.period_end)))).scalar_one_or_none()


async def is_closed(db: AsyncSession, d: date) -> bool:
    q = select(FinancialPeriodClosing.id).where(
        FinancialPeriodClosing.period_start <= d, FinancialPeriodClosing.period_end >= d
    )
    return (await db.execute(q)).first() is not None


async def resolve_competence(db: AsyncSession, d: Optional[date], *, shift_if_closed: bool = True) -> date:
    d = d or today_local()
    if not await is_closed(db, d):
        return d
    if not shift_if_closed:
        raise LedgerError("LEDGER_PERIOD_CLOSED", "Período contábil fechado: lançamento não permitido.",
                          status=409, details={"competence_date": d.isoformat()})
    end = await last_closed_end(db)
    shifted = max(today_local(), (end or d) + timedelta(days=1))
    while await is_closed(db, shifted):  # períodos fechados fora de ordem
        shifted = month_end(shifted) + timedelta(days=1)
    return shifted


Leg = tuple[Union[LedgerAccount, uuid.UUID], int] | tuple[Union[LedgerAccount, uuid.UUID], int, Optional[str]]


@dataclass(frozen=True)
class PostResult:
    transaction_id: uuid.UUID
    created: bool
    competence_date: date


def _validate_legs(legs: Sequence[Leg]) -> list[tuple[uuid.UUID, int, Optional[str]]]:
    out = []
    for leg in legs:
        acc, amount = leg[0], leg[1]
        memo = leg[2] if len(leg) > 2 else None
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise TypeError("amount_cents deve ser int (centavos)")
        if amount == 0:
            continue
        acc_id = acc.id if isinstance(acc, LedgerAccount) else acc
        out.append((acc_id, amount, memo))
    if len(out) < 2:
        raise LedgerError("LEDGER_TOO_FEW_LEGS", "Lançamento exige ao menos duas partidas.")
    if sum(a for _, a, _ in out) != 0:
        raise LedgerError("LEDGER_UNBALANCED", "Lançamento desbalanceado (débitos ≠ créditos).")
    return out


async def post(
    db: AsyncSession,
    *,
    kind: str,
    idempotency_key: str,
    legs: Sequence[Leg],
    description: str,
    competence_date: Optional[date] = None,
    shift_if_closed: bool = True,
    created_by: Any = None,
    approval_request_id: Any = None,
    reverses_transaction_id: Any = None,
    external_provider: Optional[str] = None,
    external_ref: Optional[str] = None,
    ref_type: Optional[str] = None,
    ref_id: Any = None,
    org_id: Any = None,
    meta: Optional[dict] = None,
) -> PostResult:
    """Grava uma transação balanceada (idempotente por ``idempotency_key``)."""
    if not idempotency_key or len(idempotency_key) > 200:
        raise LedgerError("LEDGER_IDEMPOTENCY_KEY", "Chave de idempotência inválida.")
    clean = _validate_legs(legs)
    existing = (
        await db.execute(select(LedgerTransaction).where(LedgerTransaction.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.kind != kind:
            raise Conflict("LEDGER_IDEMPOTENCY_CONFLICT", "Chave de idempotência já usada por outro lançamento.")
        return PostResult(existing.id, False, existing.competence_date)
    comp = await resolve_competence(db, competence_date, shift_if_closed=shift_if_closed)
    tx_id = uuid.uuid4()
    tt = LedgerTransaction.__table__
    ins = (
        pg_insert(tt)
        .values(
            id=tx_id, kind=kind, idempotency_key=idempotency_key, competence_date=comp,
            description=description[:500], created_by=created_by, approval_request_id=approval_request_id,
            reverses_transaction_id=reverses_transaction_id, external_provider=external_provider,
            external_ref=external_ref, ref_type=ref_type, ref_id=(str(ref_id) if ref_id is not None else None),
            org_id=org_id, metadata=meta,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(tt.c.id)
    )
    if (await db.execute(ins)).first() is None:
        row = (
            await db.execute(select(LedgerTransaction).where(LedgerTransaction.idempotency_key == idempotency_key))
        ).scalar_one()
        if row.kind != kind:
            raise Conflict("LEDGER_IDEMPOTENCY_CONFLICT", "Chave de idempotência já usada por outro lançamento.")
        return PostResult(row.id, False, row.competence_date)
    await db.execute(
        LedgerPosting.__table__.insert(),
        [
            {"id": uuid.uuid4(), "transaction_id": tx_id, "account_id": acc_id, "amount_cents": amount,
             "competence_date": comp, "memo": (memo[:300] if memo else None)}
            for acc_id, amount, memo in clean
        ],
    )
    return PostResult(tx_id, True, comp)


async def reverse(
    db: AsyncSession,
    transaction_id: uuid.UUID,
    *,
    reason: str,
    created_by: Any = None,
    approval_request_id: Any = None,
    competence_date: Optional[date] = None,
) -> PostResult:
    """Reversão ligada: nova transação com partidas negadas (uma única vez por transação)."""
    orig = await db.get(LedgerTransaction, transaction_id)
    if orig is None:
        raise LedgerError("LEDGER_TX_NOT_FOUND", "Transação inexistente.", status=404)
    if orig.kind == "REVERSAL":
        raise LedgerError("LEDGER_REVERSE_REVERSAL", "Não é possível reverter uma reversão; lance novamente.")
    postings = (
        await db.execute(select(LedgerPosting).where(LedgerPosting.transaction_id == transaction_id))
    ).scalars().all()
    legs = [(p.account_id, -p.amount_cents, "reversão") for p in postings]
    return await post(
        db, kind="REVERSAL", idempotency_key=f"reversal:{transaction_id}", legs=legs,
        description=f"Reversão: {reason}"[:500], competence_date=competence_date, created_by=created_by,
        approval_request_id=approval_request_id, reverses_transaction_id=transaction_id,
        ref_type=orig.ref_type, ref_id=orig.ref_id, org_id=orig.org_id,
        meta={"reverses": str(transaction_id), "reason": reason[:300]},
    )


# ─── Saldos (sempre Σ partidas) ──────────────────────────────────────────────

def natural_balance(kind: str, raw: int) -> int:
    return raw if kind in (A, X) else -raw


async def raw_balance(db: AsyncSession, account_id: uuid.UUID, *, until: Optional[date] = None) -> int:
    q = select(func.coalesce(func.sum(LedgerPosting.amount_cents), 0)).where(LedgerPosting.account_id == account_id)
    if until is not None:
        q = q.where(LedgerPosting.competence_date <= until)
    return int((await db.execute(q)).scalar_one())


async def balance_of(db: AsyncSession, code: str, owner_id: Any = None, *, until: Optional[date] = None) -> int:
    """Saldo NATURAL de uma conta (0 se ainda não existe)."""
    acc = await find_account(db, code, owner_id)
    if acc is None:
        return 0
    return natural_balance(acc.kind, await raw_balance(db, acc.id, until=until))


async def owner_balances(db: AsyncSession, owner_type: str, owner_id: Any) -> dict[str, int]:
    """Saldos naturais de todas as contas de um dono: {code: saldo}."""
    oid = uuid.UUID(str(owner_id))
    rows = (
        await db.execute(
            select(LedgerAccount.code, LedgerAccount.kind, func.coalesce(func.sum(LedgerPosting.amount_cents), 0))
            .select_from(LedgerAccount)
            .outerjoin(LedgerPosting, LedgerPosting.account_id == LedgerAccount.id)
            .where(LedgerAccount.owner_type == owner_type, LedgerAccount.owner_id == oid)
            .group_by(LedgerAccount.code, LedgerAccount.kind)
        )
    ).all()
    return {code: natural_balance(kind, int(s)) for code, kind, s in rows}


async def trial_balance(db: AsyncSession, *, until: Optional[date] = None) -> list[dict]:
    q = (
        select(LedgerAccount.id, LedgerAccount.code, LedgerAccount.owner_type, LedgerAccount.owner_id,
               LedgerAccount.kind, func.coalesce(func.sum(LedgerPosting.amount_cents), 0))
        .select_from(LedgerAccount)
        .outerjoin(LedgerPosting, and_(LedgerPosting.account_id == LedgerAccount.id,
                                       *( [LedgerPosting.competence_date <= until] if until else [] )))
        .group_by(LedgerAccount.id)
        .order_by(LedgerAccount.code, LedgerAccount.owner_id)
    )
    return [
        {"account_id": str(i), "code": c, "owner_type": ot, "owner_id": (str(oid) if oid else None), "kind": k,
         "raw_cents": int(s), "balance_cents": natural_balance(k, int(s))}
        for i, c, ot, oid, k, s in (await db.execute(q)).all()
    ]


# ─── Locks ───────────────────────────────────────────────────────────────────

def lock_key(namespace: str, value: Any) -> int:
    h = hashlib.sha256(f"rdfin:{namespace}:{value}".encode()).digest()[:8]
    return int.from_bytes(h, "big", signed=True)


async def advisory_lock(db: AsyncSession, namespace: str, value: Any) -> None:
    """``pg_advisory_xact_lock`` (liberado no fim da transação)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=lock_key(namespace, value)))


def canonical_codes() -> Iterable[str]:
    return CHART.keys()
