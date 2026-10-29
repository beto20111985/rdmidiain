"""
RD Mídia Indoor — Financeiro completo (W-FIN; A5 D8–D9). Prefixo ``/api/v1/financial``.

Visão geral, plano de contas, extratos (conta/organização), lançamentos, contas a
receber/pagar, despesas (criação/alteração/baixa via dupla aprovação), fluxo de caixa,
DRE, fechamento mensal (snapshot + hash) e conciliação (resolução por ajuste aprovado).
Equipe financeira GLOBAL vê tudo; organizações só os próprios extratos.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import NotFound
from app.models.financial import (
    FinancialPeriodClosing,
    LedgerAccount,
    LedgerPosting,
    LedgerTransaction,
    Payable,
    PaymentOrder,
    ReconciliationItem,
    ReconciliationRun,
)
from app.modules.financial import accounting as svc
from app.modules.financial import approvals, ledger, payments, reconciliation

router = APIRouter()

VIEW = require("finance.view", global_only=True)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _range(start: Optional[date], end: Optional[date]) -> tuple[date, date]:
    end = end or ledger.today_local()
    start = start or ledger.month_start(end)
    return start, end


@router.get("/overview")
async def overview(start: Optional[date] = None, end: Optional[date] = None, ctx: AuthContext = Depends(VIEW),
                   db: AsyncSession = Depends(get_db)):
    s, e = _range(start, end)
    return await svc.overview(db, s, e)


@router.get("/accounts")
async def accounts(code: Optional[str] = Query(None, max_length=120), until: Optional[date] = None,
                   ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    tb = await ledger.trial_balance(db, until=until)
    if code:
        tb = [r for r in tb if r["code"].startswith(code)]
    return {"items": tb, "total": len(tb), "page": 1, "page_size": len(tb),
            "check_sum_cents": sum(r["raw_cents"] for r in tb)}


@router.get("/accounts/{account_id}/statement")
async def account_statement(account_id: uuid.UUID, start: Optional[date] = None, end: Optional[date] = None,
                            page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                            ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    if await db.get(LedgerAccount, account_id) is None:
        raise NotFound()
    return await svc.statement(db, [account_id], start=start, end=end, page=page, page_size=page_size)


@router.get("/organizations/{org_id}/statement")
async def org_statement(org_id: uuid.UUID, start: Optional[date] = None, end: Optional[date] = None,
                        page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                        ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    """Extrato da organização (anunciante: receita diferida/reembolsos; estabelecimento: comissões)."""
    if not (ctx.can("finance.view", global_only=True) or ctx.can("payments.view", org_id=org_id)
            or ctx.can("commissions.view", org_id=org_id)):
        raise NotFound()
    ids = await svc.org_account_ids(db, org_id)
    if not ids:
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "balances": {}}
    return await svc.statement(db, ids, start=start, end=end, page=page, page_size=page_size)


@router.get("/ledger/transactions")
async def ledger_transactions(kind: Optional[str] = Query(None, max_length=40), start: Optional[date] = None,
                              end: Optional[date] = None, page: int = Query(1, ge=1),
                              page_size: int = Query(25, ge=1, le=100), ctx: AuthContext = Depends(VIEW),
                              db: AsyncSession = Depends(get_db)):
    q = select(LedgerTransaction)
    if kind:
        q = q.where(LedgerTransaction.kind == kind)
    if start:
        q = q.where(LedgerTransaction.competence_date >= start)
    if end:
        q = q.where(LedgerTransaction.competence_date <= end)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    txs = (await db.execute(q.order_by(LedgerTransaction.occurred_at.desc()).offset((page - 1) * page_size)
                            .limit(page_size))).scalars().all()
    items = []
    for t in txs:
        posts = (await db.execute(select(LedgerPosting, LedgerAccount).join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
                                  .where(LedgerPosting.transaction_id == t.id))).all()
        items.append({"id": str(t.id), "kind": t.kind, "description": t.description,
                      "competence_date": t.competence_date.isoformat(), "occurred_at": t.occurred_at,
                      "idempotency_key": t.idempotency_key,
                      "reverses_transaction_id": str(t.reverses_transaction_id) if t.reverses_transaction_id else None,
                      "approval_request_id": str(t.approval_request_id) if t.approval_request_id else None,
                      "postings": [{"account_code": a.code, "owner_id": str(a.owner_id) if a.owner_id else None,
                                    "amount_cents": p.amount_cents, "memo": p.memo} for p, a in posts]})
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/receivables")
async def receivables(status: Optional[str] = Query(None, max_length=24), page: int = Query(1, ge=1),
                      page_size: int = Query(25, ge=1, le=100), ctx: AuthContext = Depends(VIEW),
                      db: AsyncSession = Depends(get_db)):
    """Contas a receber = cobranças (pendentes/recebidas)."""
    q = select(PaymentOrder)
    q = q.where(PaymentOrder.status == status) if status else q.where(PaymentOrder.status.in_(payments.PENDING_LIKE))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(PaymentOrder.created_at.desc()).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    pending_sum = (await db.execute(select(func.coalesce(func.sum(PaymentOrder.amount_cents), 0))
                                    .where(PaymentOrder.status.in_(payments.PENDING_LIKE)))).scalar_one()
    return {"items": [payments.order_public(o, include_qr=False) for o in rows], "total": total, "page": page,
            "page_size": page_size, "pending_total_cents": int(pending_sum)}


# ─── Contas a pagar / despesas (dupla aprovação) ─────────────────────────────

class ExpenseIn(_Body):
    description: str = Field(min_length=3, max_length=300)
    category: str = Field(min_length=2, max_length=40)
    amount_cents: StrictInt = Field(gt=0)
    competence_date: date
    due_date: Optional[date] = None
    supplier_name: Optional[str] = Field(default=None, max_length=200)
    supplier_document: Optional[str] = Field(default=None, max_length=20)
    reason: str = Field(min_length=10, max_length=2000)


class ExpenseUpdateIn(_Body):
    description: Optional[str] = Field(default=None, min_length=3, max_length=300)
    category: Optional[str] = Field(default=None, min_length=2, max_length=40)
    amount_cents: Optional[StrictInt] = Field(default=None, gt=0)
    due_date: Optional[date] = None
    reason: str = Field(min_length=10, max_length=2000)


class SettleIn(_Body):
    method: Literal["PIX_OUT", "EXTERNAL"]
    pix_key_type: Optional[Literal["cpf", "cnpj", "email", "phone", "random"]] = None
    pix_key: Optional[str] = Field(default=None, max_length=120)
    reason: str = Field(min_length=10, max_length=2000)


APPROVE = require("finance.approve", global_only=True, allow_impersonation=False)


@router.get("/payables")
async def list_payables(status: Optional[str] = Query(None, max_length=10), page: int = Query(1, ge=1),
                        page_size: int = Query(25, ge=1, le=100), ctx: AuthContext = Depends(VIEW),
                        db: AsyncSession = Depends(get_db)):
    q = select(Payable)
    if status:
        q = q.where(Payable.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(Payable.due_date).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [svc.payable_public(p) for p in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/payables", status_code=202)
async def create_payable(body: ExpenseIn, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key",
                                                                                  max_length=120),
                         ctx: AuthContext = Depends(APPROVE), db: AsyncSession = Depends(get_db)):
    """Despesa manual: cria PEDIDO de dupla aprovação (o lançamento só ocorre quando o outro sócio aprovar)."""
    payload = body.model_dump(exclude={"reason"}, mode="json")
    r = await approvals.create_request(db, ctx, "EXPENSE_CREATE", payload, body.reason, idempotency_key=idempotency_key)
    return await approvals.request_public_named(db, r, viewer_id=ctx.user_id)


@router.patch("/payables/{payable_id}", status_code=202)
async def update_payable(payable_id: uuid.UUID, body: ExpenseUpdateIn, ctx: AuthContext = Depends(APPROVE),
                         db: AsyncSession = Depends(get_db)):
    payload = {"payable_id": str(payable_id), **body.model_dump(exclude={"reason"}, exclude_none=True, mode="json")}
    r = await approvals.create_request(db, ctx, "EXPENSE_UPDATE", payload, body.reason)
    return await approvals.request_public_named(db, r, viewer_id=ctx.user_id)


@router.post("/payables/{payable_id}/settle", status_code=202)
async def settle_payable(payable_id: uuid.UUID, body: SettleIn, ctx: AuthContext = Depends(APPROVE),
                         db: AsyncSession = Depends(get_db)):
    payload: dict[str, Any] = {"payable_id": str(payable_id), "method": body.method}
    if body.method == "PIX_OUT":
        payload.update({"pix_key_type": body.pix_key_type, "pix_key": body.pix_key})
    r = await approvals.create_request(db, ctx, "PAYABLE_SETTLEMENT", payload, body.reason)
    return await approvals.request_public_named(db, r, viewer_id=ctx.user_id)


# ─── Relatórios ──────────────────────────────────────────────────────────────

@router.get("/cash-flow")
async def cash_flow(start: Optional[date] = None, end: Optional[date] = None,
                    granularity: Literal["day", "month"] = "day", ctx: AuthContext = Depends(VIEW),
                    db: AsyncSession = Depends(get_db)):
    s, e = _range(start, end)
    return await svc.cash_flow(db, s, e, granularity)


@router.get("/dre")
async def dre(start: Optional[date] = None, end: Optional[date] = None, ctx: AuthContext = Depends(VIEW),
              db: AsyncSession = Depends(get_db)):
    s, e = _range(start, end)
    return await svc.dre(db, s, e)


# ─── Fechamento mensal ───────────────────────────────────────────────────────

class ClosingIn(_Body):
    period: date  # qualquer dia do mês


@router.get("/closings")
async def list_closings(ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(FinancialPeriodClosing).order_by(FinancialPeriodClosing.period_start.desc()))).scalars().all()
    return {"items": [svc.closing_public(c) for c in rows], "total": len(rows), "page": 1, "page_size": len(rows)}


@router.get("/closings/{closing_id}")
async def get_closing(closing_id: uuid.UUID, ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    c = await db.get(FinancialPeriodClosing, closing_id)
    if c is None:
        raise NotFound()
    return svc.closing_public(c, with_snapshot=True)


@router.post("/closings", status_code=201)
async def close_period(body: ClosingIn, ctx: AuthContext = Depends(require("finance.manage", global_only=True,
                                                                           allow_impersonation=False)),
                       db: AsyncSession = Depends(get_db)):
    """Fecha o mês (snapshot + SHA-256). Irreversível: o banco passa a recusar lançamentos no período."""
    return svc.closing_public(await svc.close_period(db, ctx, body.period))


# ─── Conciliação ─────────────────────────────────────────────────────────────

RECONCILE = require("finance.reconcile", global_only=True, allow_impersonation=False)


@router.get("/reconciliation/runs")
async def recon_runs(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                     ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    q = select(ReconciliationRun)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(ReconciliationRun.started_at.desc()).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    return {"items": [{"id": str(r.id), "kind": r.kind, "status": r.status, "provider_balance_cents": r.provider_balance_cents,
                       "internal_cash_cents": r.internal_cash_cents, "summary": r.summary, "started_at": r.started_at,
                       "finished_at": r.finished_at} for r in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/reconciliation/runs", status_code=201)
async def recon_run_now(ctx: AuthContext = Depends(RECONCILE), db: AsyncSession = Depends(get_db)):
    """Reconsulta agora (saldo e saques na CloudPayPag) e abre itens de divergência."""
    r = await reconciliation.run_daily(db, created_by=ctx.user_id, kind="MANUAL")
    return {"id": str(r.id), "status": r.status, "summary": r.summary,
            "provider_balance_cents": r.provider_balance_cents, "internal_cash_cents": r.internal_cash_cents}


@router.get("/reconciliation/items")
async def recon_items(status: Optional[Literal["DIVERGENT", "RESOLVED"]] = None, page: int = Query(1, ge=1),
                      page_size: int = Query(25, ge=1, le=100), ctx: AuthContext = Depends(VIEW),
                      db: AsyncSession = Depends(get_db)):
    q = select(ReconciliationItem)
    if status:
        q = q.where(ReconciliationItem.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(ReconciliationItem.created_at.desc()).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    return {"items": [reconciliation.item_public(i) for i in rows], "total": total, "page": page, "page_size": page_size}


class ResolutionIn(_Body):
    legs: Optional[list[dict[str, Any]]] = None
    withdrawal_resolution: Optional[dict[str, Any]] = None
    competence_date: Optional[date] = None
    description: Optional[str] = Field(default=None, max_length=300)
    reason: str = Field(min_length=10, max_length=2000)


@router.post("/reconciliation/items/{item_id}/resolution", status_code=202)
async def recon_resolution(item_id: uuid.UUID, body: ResolutionIn, ctx: AuthContext = Depends(APPROVE),
                           db: AsyncSession = Depends(get_db)):
    """Resolução de divergência = PEDIDO de dupla aprovação (ajuste no ledger e/ou desfecho do saque)."""
    payload = {"item_id": str(item_id), **body.model_dump(exclude={"reason"}, exclude_none=True, mode="json")}
    r = await approvals.create_request(db, ctx, "RECONCILIATION_ADJUSTMENT", payload, body.reason)
    return await approvals.request_public_named(db, r, viewer_id=ctx.user_id)
