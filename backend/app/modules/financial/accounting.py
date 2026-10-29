"""
RD Mídia Indoor — Financeiro completo (P001 §85, §91; ETAPA2 #671–#684; A5 D9).

- Contas a receber: cobranças (``payment_orders``) pendentes/recebidas.
- Contas a pagar / despesas: criação, alteração e baixa SÓ por dupla aprovação
  (``EXPENSE_CREATE``/``EXPENSE_UPDATE``/``PAYABLE_SETTLEMENT``); despesa = D
  ``RD:Expense:Opex:<categoria>`` / C ``RD:AccountsPayable``.
- Fluxo de caixa: partidas nas contas de caixa (``is_cash``).
- DRE: mapeamento ``ledger_accounts.dre_group``.
- Fechamento mensal: snapshot (balancete) + SHA-256; período fechado imutável
  (trigger rejeita lançamentos).
- Extratos por conta e por organização (escopo aplicado nos endpoints).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.core.timeutils import utcnow
from app.models.financial import (
    CommissionCompetenceRun,
    FinancialPeriodClosing,
    LedgerAccount,
    LedgerPosting,
    LedgerTransaction,
    Payable,
    PaymentOrder,
    Withdrawal,
)
from app.modules.financial import common, ledger

DRE_ORDER = [
    ("RECEITA_MIDIA", "Receita de mídia", 1),
    ("RECEITA_SERVICOS", "Receita de serviços", 1),
    ("DEDUCOES", "(−) Reembolsos, cancelamentos e chargebacks", 1),
    ("TAXAS_GATEWAY", "(−) Taxas CloudPayPag", 1),
    ("COMISSOES", "(−) Comissões de estabelecimentos (líquidas do resíduo RD)", 1),
    ("COMISSOES_VENDEDOR", "(−) Comissões de vendedores", 1),
    ("DESPESAS_OPERACIONAIS", "(−) Despesas operacionais", 1),
    ("OUTRAS_RECEITAS", "Outras receitas", 1),
    ("OUTRAS_DESPESAS", "(−) Outras despesas", 1),
]


def _category(cat: str) -> str:
    c = (cat or "").strip().lower().replace(" ", "_")
    if not c or len(c) > 40 or not c.replace("_", "").replace("-", "").isalnum():
        raise DomainError("EXPENSE_CATEGORY_INVALID", "Categoria de despesa inválida.")
    return c


def validate_expense_payload(p: dict) -> dict:
    amount = p.get("amount_cents")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise DomainError("EXPENSE_AMOUNT_INVALID", "Valor da despesa inválido.")
    comp = date.fromisoformat(str(p.get("competence_date")))
    due = date.fromisoformat(str(p.get("due_date") or p.get("competence_date")))
    desc = str(p.get("description") or "").strip()
    if len(desc) < 3:
        raise DomainError("EXPENSE_DESCRIPTION_REQUIRED", "Descreva a despesa.")
    return {"description": desc[:300], "category": _category(p.get("category", "")), "amount_cents": amount,
            "competence_date": comp.isoformat(), "due_date": due.isoformat(),
            "supplier_name": (str(p["supplier_name"])[:200] if p.get("supplier_name") else None),
            "supplier_document": (str(p["supplier_document"])[:20] if p.get("supplier_document") else None)}


def _authorization_meta(approval: Any, authorizer_id: Any) -> dict:
    from app.modules.financial.approvals import authorization_meta

    return authorization_meta(approval, authorizer_id)


async def create_expense(db: AsyncSession, payload: dict, approval: Any, approver_id: Any) -> Payable:
    p = validate_expense_payload(payload)
    comp = date.fromisoformat(p["competence_date"])
    exp = await ledger.account(db, f"{ledger.OPEX_PREFIX}{p['category']}")
    ap = await ledger.account(db, "RD:AccountsPayable")
    pay = Payable(id=uuid.uuid4(), description=p["description"], category=p["category"],
                  supplier_name=p["supplier_name"], supplier_document=p["supplier_document"],
                  amount_cents=p["amount_cents"], competence_date=comp, due_date=date.fromisoformat(p["due_date"]),
                  status="OPEN", approval_request_id=approval.id)
    db.add(pay)
    await db.flush()
    res = await ledger.post(db, kind="EXPENSE", idempotency_key=f"expense:{pay.id}:created",
                            legs=[(exp, p["amount_cents"], p["description"]), (ap, -p["amount_cents"], "a pagar")],
                            description=f"Despesa: {p['description']}", competence_date=comp, shift_if_closed=False,
                            approval_request_id=approval.id, created_by=approver_id, ref_type="payable", ref_id=pay.id,
                            meta=_authorization_meta(approval, approver_id))
    pay.ledger_transaction_id = res.transaction_id
    await db.flush()
    return pay


async def update_expense(db: AsyncSession, payload: dict, approval: Any, approver_id: Any) -> Payable:
    pay = await db.get(Payable, common.as_uuid(payload.get("payable_id")), with_for_update=True)
    if pay is None:
        raise NotFound()
    if pay.status != "OPEN":
        raise Conflict("PAYABLE_NOT_OPEN", "Somente contas em aberto podem ser alteradas.")
    merged = {"description": pay.description, "category": pay.category, "amount_cents": pay.amount_cents,
              "competence_date": pay.competence_date.isoformat(), "due_date": pay.due_date.isoformat(),
              "supplier_name": pay.supplier_name, "supplier_document": pay.supplier_document}
    merged.update({k: v for k, v in payload.items() if k in merged and v is not None})
    p = validate_expense_payload(merged)
    if pay.ledger_transaction_id:
        await ledger.reverse(db, pay.ledger_transaction_id, reason=f"alteração de despesa ({approval.id})",
                             created_by=approver_id, approval_request_id=approval.id)
    comp = date.fromisoformat(p["competence_date"])
    exp = await ledger.account(db, f"{ledger.OPEX_PREFIX}{p['category']}")
    ap = await ledger.account(db, "RD:AccountsPayable")
    res = await ledger.post(db, kind="EXPENSE", idempotency_key=f"expense:{pay.id}:update:{approval.id}",
                            legs=[(exp, p["amount_cents"], p["description"]), (ap, -p["amount_cents"], "a pagar")],
                            description=f"Despesa (alterada): {p['description']}", competence_date=comp,
                            shift_if_closed=False, approval_request_id=approval.id, created_by=approver_id,
                            ref_type="payable", ref_id=pay.id)
    pay.description, pay.category, pay.amount_cents = p["description"], p["category"], p["amount_cents"]
    pay.competence_date, pay.due_date = comp, date.fromisoformat(p["due_date"])
    pay.supplier_name, pay.supplier_document = p["supplier_name"], p["supplier_document"]
    pay.ledger_transaction_id = res.transaction_id
    await db.flush()
    return pay


async def settle_payable_external(db: AsyncSession, pay: Payable, approval: Any, approver_id: Any) -> None:
    """Baixa paga FORA da CloudPayPag (conta bancária externa)."""
    ap = await ledger.account(db, "RD:AccountsPayable")
    cash = await ledger.account(db, "RD:Cash:External")
    await ledger.post(db, kind="PAYABLE_SETTLEMENT", idempotency_key=f"payable:{pay.id}:settled",
                      legs=[(ap, pay.amount_cents, "baixa"), (cash, -pay.amount_cents, "pagamento externo")],
                      description=f"Baixa de conta a pagar {pay.description}", approval_request_id=approval.id,
                      created_by=approver_id, ref_type="payable", ref_id=pay.id)
    pay.status = "PAID"
    pay.settled_at = utcnow()
    pay.settlement_approval_id = approval.id


async def on_payable_payout_terminal(db: AsyncSession, w: Withdrawal) -> None:
    pay = await db.get(Payable, common.as_uuid(w.ref_id), with_for_update=True)
    if pay is None:
        return
    if w.status == "COMPLETED":
        pay.status = "PAID"
        pay.settled_at = utcnow()


def payable_public(p: Payable) -> dict:
    return {"id": str(p.id), "description": p.description, "category": p.category, "supplier_name": p.supplier_name,
            "amount_cents": p.amount_cents, "competence_date": p.competence_date.isoformat(),
            "due_date": p.due_date.isoformat(), "status": p.status, "settled_at": p.settled_at,
            "overdue": p.status == "OPEN" and p.due_date < ledger.today_local(),
            "approval_request_id": str(p.approval_request_id)}


# ─── Relatórios ──────────────────────────────────────────────────────────────

async def cash_flow(db: AsyncSession, start: date, end: date, granularity: str = "day") -> dict:
    if granularity not in ("day", "month"):
        raise DomainError("INVALID_GRANULARITY", "Use day ou month.")
    bucket = LedgerPosting.competence_date if granularity == "day" else func.date_trunc("month", LedgerPosting.competence_date)
    q = (select(bucket.label("b"),
                func.coalesce(func.sum(case((LedgerPosting.amount_cents > 0, LedgerPosting.amount_cents), else_=0)), 0),
                func.coalesce(func.sum(case((LedgerPosting.amount_cents < 0, -LedgerPosting.amount_cents), else_=0)), 0))
         .join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
         .where(LedgerAccount.is_cash.is_(True), LedgerPosting.competence_date.between(start, end))
         .group_by("b").order_by("b"))
    opening = int((await db.execute(
        select(func.coalesce(func.sum(LedgerPosting.amount_cents), 0)).join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
        .where(LedgerAccount.is_cash.is_(True), LedgerPosting.competence_date < start)
    )).scalar_one())
    rows, bal = [], opening
    for b, inflow, outflow in (await db.execute(q)).all():
        inflow, outflow = int(inflow), int(outflow)
        bal += inflow - outflow
        rows.append({"period": (b.date() if isinstance(b, datetime) else b).isoformat(), "inflow_cents": inflow,
                     "outflow_cents": outflow, "net_cents": inflow - outflow, "balance_cents": bal})
    return {"start": start.isoformat(), "end": end.isoformat(), "opening_balance_cents": opening, "rows": rows,
            "closing_balance_cents": bal}


async def dre(db: AsyncSession, start: date, end: date) -> dict:
    rows = (await db.execute(
        select(LedgerAccount.dre_group, LedgerAccount.kind, func.coalesce(func.sum(LedgerPosting.amount_cents), 0))
        .join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
        .where(LedgerAccount.dre_group.isnot(None), LedgerPosting.competence_date.between(start, end))
        .group_by(LedgerAccount.dre_group, LedgerAccount.kind)
    )).all()
    groups: dict[str, int] = {}
    for g, _kind, raw in rows:
        groups[g] = groups.get(g, 0) + (-int(raw))  # receita (crédito) positiva; despesa (débito) negativa
    lines = [{"group": g, "label": label, "amount_cents": groups.get(g, 0)} for g, label, _ in DRE_ORDER]
    gross = groups.get("RECEITA_MIDIA", 0) + groups.get("RECEITA_SERVICOS", 0)
    net_revenue = gross + groups.get("DEDUCOES", 0)
    result = sum(groups.values())
    return {"start": start.isoformat(), "end": end.isoformat(), "lines": lines, "gross_revenue_cents": gross,
            "net_revenue_cents": net_revenue, "result_cents": result,
            "margin_bps": (result * 10_000 // net_revenue) if net_revenue > 0 else None}


async def statement(db: AsyncSession, account_ids: list[uuid.UUID], *, start: Optional[date] = None,
                    end: Optional[date] = None, page: int = 1, page_size: int = 50) -> dict:
    base = select(LedgerPosting, LedgerTransaction, LedgerAccount).join(
        LedgerTransaction, LedgerTransaction.id == LedgerPosting.transaction_id).join(
        LedgerAccount, LedgerAccount.id == LedgerPosting.account_id).where(LedgerPosting.account_id.in_(account_ids))
    if start:
        base = base.where(LedgerPosting.competence_date >= start)
    if end:
        base = base.where(LedgerPosting.competence_date <= end)
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
    rows = (await db.execute(base.order_by(LedgerPosting.competence_date.desc(), LedgerTransaction.occurred_at.desc(),
                                           LedgerPosting.id).offset((page - 1) * page_size).limit(page_size))).all()
    items = []
    for p, t, a in rows:
        items.append({"posting_id": str(p.id), "transaction_id": str(t.id), "kind": t.kind, "description": t.description,
                      "competence_date": p.competence_date.isoformat(), "occurred_at": t.occurred_at,
                      "account_code": a.code, "account_name": a.name, "owner_id": str(a.owner_id) if a.owner_id else None,
                      "amount_cents": p.amount_cents, "natural_amount_cents": ledger.natural_balance(a.kind, p.amount_cents),
                      "memo": p.memo, "reverses_transaction_id": str(t.reverses_transaction_id) if t.reverses_transaction_id else None})
    balances = {}
    for aid in account_ids:
        acc = await db.get(LedgerAccount, aid)
        if acc is not None:
            balances[acc.code + (f"|{acc.owner_id}" if acc.owner_id else "")] = ledger.natural_balance(
                acc.kind, await ledger.raw_balance(db, aid))
    return {"items": items, "total": total, "page": page, "page_size": page_size, "balances": balances}


async def org_account_ids(db: AsyncSession, org_id: uuid.UUID) -> list[uuid.UUID]:
    return [r[0] for r in (await db.execute(select(LedgerAccount.id).where(LedgerAccount.org_id == org_id))).all()]


async def overview(db: AsyncSession, start: date, end: date) -> dict:
    """Visão do admin (ETAPA2 #651): recebido, pendente, estornado, comissões, a pagar, saques, taxas."""
    t0, t1 = ledger.local_range_utc(start, end)
    received = int((await db.execute(select(func.coalesce(func.sum(PaymentOrder.amount_cents), 0)).where(
        PaymentOrder.status.in_(("PAID", "PARTIALLY_REFUNDED", "REFUNDED")),
        PaymentOrder.paid_at >= t0, PaymentOrder.paid_at < t1)
    )).scalar_one())
    pending = int((await db.execute(select(func.coalesce(func.sum(PaymentOrder.amount_cents), 0)).where(
        PaymentOrder.status.in_(("PENDING", "CREATED", "CREATION_UNKNOWN"))))).scalar_one())
    refunded = int((await db.execute(select(func.coalesce(func.sum(PaymentOrder.refunded_cents), 0)))).scalar_one())
    fees = await _sum_code(db, "RD:Expense:GatewayFees", start, end)
    commission_exp = await _sum_code(db, "RD:Expense:Commission", start, end)
    residue = -await _sum_code(db, "RD:Income:RoundingResidue", start, end)
    payable_est = 0
    for code in ("EST:CommissionPayable:Pending", "EST:CommissionPayable:Available", "EST:CommissionPayable:InTransit"):
        payable_est += -int((await db.execute(select(func.coalesce(func.sum(LedgerPosting.amount_cents), 0)).join(
            LedgerAccount, LedgerAccount.id == LedgerPosting.account_id).where(LedgerAccount.code == code))).scalar_one())
    withdrawn = int((await db.execute(select(func.coalesce(func.sum(Withdrawal.amount_cents), 0)).where(
        Withdrawal.status == "COMPLETED", Withdrawal.completed_at >= t0, Withdrawal.completed_at < t1))).scalar_one())
    cash = await ledger.balance_of(db, "RD:Cash:CloudPayPag")
    ap = await ledger.balance_of(db, "RD:AccountsPayable")
    return {"start": start.isoformat(), "end": end.isoformat(), "received_cents": received,
            "pending_receivables_cents": pending, "refunded_cents": refunded, "gateway_fees_cents": fees,
            "commission_expense_cents": commission_exp - residue, "commission_payable_cents": payable_est,
            "withdrawals_paid_cents": withdrawn, "cash_cloudpaypag_cents": cash, "accounts_payable_cents": ap}


async def _sum_code(db: AsyncSession, code: str, start: date, end: date) -> int:
    return int((await db.execute(
        select(func.coalesce(func.sum(LedgerPosting.amount_cents), 0)).join(
            LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
        .where(LedgerAccount.code == code, LedgerPosting.competence_date.between(start, end))
    )).scalar_one())


# ─── Fechamento mensal ───────────────────────────────────────────────────────

async def close_period(db: AsyncSession, ctx: Any, period: date) -> FinancialPeriodClosing:
    period = ledger.month_start(period)
    end = ledger.month_end(period)
    if ledger.today_local() <= end:
        raise DomainError("PERIOD_NOT_ENDED", "O mês ainda não terminou (America/Sao_Paulo).", status=409)
    await ledger.advisory_lock(db, "period_close", "global")
    if (await db.execute(select(FinancialPeriodClosing.id).where(FinancialPeriodClosing.period_start == period))).first():
        raise Conflict("PERIOD_ALREADY_CLOSED", "Período já fechado.")
    if (await db.get(CommissionCompetenceRun, period)) is None:
        from app.models.financial import CommissionAccrual

        pending = (await db.execute(select(CommissionAccrual.id).where(CommissionAccrual.competence == period).limit(1))).first()
        if pending:
            raise Conflict("COMMISSION_COMPETENCE_OPEN", "Feche a competência de comissões antes do período contábil.")
    tb = await ledger.trial_balance(db, until=end)
    tx_count = int((await db.execute(select(func.count(LedgerTransaction.id)).where(
        LedgerTransaction.competence_date.between(period, end)))).scalar_one())
    dre_month = await dre(db, period, end)
    snapshot = {"period": period.isoformat(), "trial_balance": tb, "dre": dre_month, "transaction_count": tx_count,
                "generated_at": utcnow().isoformat()}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    c = FinancialPeriodClosing(id=uuid.uuid4(), period_start=period, period_end=end, closed_by=ctx.user_id,
                               snapshot=snapshot, snapshot_sha256=digest, transaction_count=tx_count)
    db.add(c)
    await db.flush()
    await audit(db, ctx, "finance.period_closed", "financial_period", period.isoformat(),
                after={"sha256": digest, "transactions": tx_count})
    return c


def closing_public(c: FinancialPeriodClosing, *, with_snapshot: bool = False) -> dict:
    d = {"id": str(c.id), "period_start": c.period_start.isoformat(), "period_end": c.period_end.isoformat(),
         "closed_at": c.closed_at, "closed_by": str(c.closed_by) if c.closed_by else None,
         "snapshot_sha256": c.snapshot_sha256, "transaction_count": c.transaction_count}
    if with_snapshot:
        d["snapshot"] = c.snapshot
    return d
