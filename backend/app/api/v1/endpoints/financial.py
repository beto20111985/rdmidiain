"""
RD Mídia Indoor — API financeira (W-FIN; ADR_009). Prefixo ``/api/v1/financial``.

Faturas do anunciante, comissões do estabelecimento, segurança financeira (senha
financeira), reembolsos, situação da integração CloudPayPag e simulação de webhook
(SOMENTE development + provedor falso). Saques/chaves PIX: ``withdrawals.py``;
dupla aprovação: ``approvals.py``; contabilidade/conciliação: ``accounting.py``.

Escopo: anunciante vê só as próprias faturas; estabelecimento só o próprio saldo/
comissões; recurso fora do escopo ⇒ 404. Valores sempre inteiros em centavos.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context, require
from app.core.config import settings
from app.core.database import get_db
from app.core.errors import DomainError, Forbidden, NotFound
from app.models.financial import PaymentOrder, Refund
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import commission, invoices, payments, refunds, security

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _page(items: list, total: int, page: int, page_size: int) -> dict:
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ─── Situação da integração ──────────────────────────────────────────────────

@router.get("/provider/status")
async def provider_status(ctx: AuthContext = Depends(require("finance.view", global_only=True))):
    """Modo (LIVE/FAKE), capacidades e dependências externas da CloudPayPag (sem segredos)."""
    return cpp.provider_status()


# ─── Segurança financeira (senha financeira própria) ─────────────────────────

class FinancialPasswordIn(_Body):
    login_password: str = Field(min_length=1, max_length=128)
    totp_code: str = Field(min_length=6, max_length=12)
    new_password: str = Field(min_length=8, max_length=128)
    current_financial_password: Optional[str] = Field(default=None, max_length=128)


@router.get("/security/status")
async def security_status(ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    from app.core.timeutils import utcnow

    out = security.security_status(ctx.user)
    until = await security.reset_cooldown_until(db, ctx.user_id)
    out["reset_blocked_until"] = until if until is not None and until > utcnow() else None
    return out


class PasswordResetIn(_Body):
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")
    totp_code: str = Field(min_length=6, max_length=12)
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/password/forgot", status_code=202)
async def financial_password_forgot(ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    """Esqueci a senha financeira: código de 6 dígitos (15 min, uso único) no e-mail do PRÓPRIO usuário."""
    return await security.request_password_reset(db, ctx)


@router.post("/password/reset")
async def financial_password_reset(body: PasswordResetIn, ctx: AuthContext = Depends(get_auth_context),
                                   db: AsyncSession = Depends(get_db)):
    """Nova senha financeira com código do e-mail + TOTP. Saques/aprovações ficam bloqueados por
    ``finance.password_reset_cooldown_hours`` (padrão 24 h). Não existe redefinição por terceiros."""
    return await security.reset_financial_password(db, ctx, code=body.code, totp_code=body.totp_code,
                                                   new_password=body.new_password)


@router.post("/security/financial-password", status_code=204)
async def set_financial_password(body: FinancialPasswordIn, ctx: AuthContext = Depends(get_auth_context),
                                 db: AsyncSession = Depends(get_db)):
    """Define/troca a senha financeira DO PRÓPRIO usuário (login + TOTP [+ senha atual])."""
    await security.set_financial_password(db, ctx, login_password=body.login_password, totp_code=body.totp_code,
                                          new_password=body.new_password,
                                          current_financial_password=body.current_financial_password)
    return Response(status_code=204)


# ─── Faturas do anunciante ───────────────────────────────────────────────────

async def _order_in_scope(db: AsyncSession, ctx: AuthContext, order_id: uuid.UUID, perm: str = "payments.view") -> PaymentOrder:
    o = await db.get(PaymentOrder, order_id)
    if o is None:
        raise NotFound()
    ctx.ensure(perm, org_id=o.org_id)
    return o


@router.get("/invoices")
async def list_invoices(
    status: Optional[str] = Query(None, max_length=24),
    org_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("payments.view")),
    db: AsyncSession = Depends(get_db),
):
    q = select(PaymentOrder).where(ctx.scope_filter("payments.view", org_col=PaymentOrder.org_id))
    if org_id is not None:
        q = q.where(PaymentOrder.org_id == org_id)
    if status:
        q = q.where(PaymentOrder.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(PaymentOrder.created_at.desc()).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    return _page([payments.order_public(o, include_qr=False) for o in rows], total, page, page_size)


@router.get("/invoices/{order_id}")
async def get_invoice(order_id: uuid.UUID, ctx: AuthContext = Depends(require("payments.view")),
                      db: AsyncSession = Depends(get_db)):
    o = await _order_in_scope(db, ctx, order_id)
    return payments.order_public(o, include_qr=o.status in payments.PENDING_LIKE)


@router.get("/invoices/{order_id}/receipt")
async def invoice_receipt(order_id: uuid.UUID, ctx: AuthContext = Depends(require("payments.view")),
                          db: AsyncSession = Depends(get_db)):
    from app.models.organizations import Organization

    o = await _order_in_scope(db, ctx, order_id)
    if o.status not in payments.PAID_LIKE:
        raise DomainError("ORDER_NOT_PAID", "Comprovante disponível somente para cobranças pagas.", status=409)
    org = await db.get(Organization, o.org_id)
    pdf, digest = invoices.receipt_pdf(o, getattr(org, "name", ""))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="comprovante-{o.id}.pdf"',
                             "X-Content-SHA256": digest})


@router.post("/invoices/{order_id}/fiscal-document", status_code=202)
async def request_fiscal_document(order_id: uuid.UUID, ctx: AuthContext = Depends(require("payments.view")),
                                  db: AsyncSession = Depends(get_db)):
    o = await _order_in_scope(db, ctx, order_id)
    r = await invoices.request_fiscal_document(db, ctx, o)
    return {"id": str(r.id), "status": r.status, "detail": r.detail}


# ─── Comissões (estabelecimento / financeiro) ────────────────────────────────

async def _allowed_establishments(db: AsyncSession, ctx: AuthContext, perm: str) -> Optional[set]:
    """None = todos (GLOBAL); senão ids de estabelecimentos no escopo (ESTABLISHMENT + orgs donas)."""
    from app.models.establishments import Establishment

    orgs = ctx.org_ids(perm)
    if orgs is None:
        return None
    ests = set(ctx.establishment_ids(perm) or set())
    if orgs:
        ests |= {r[0] for r in (await db.execute(select(Establishment.id).where(Establishment.org_id.in_(list(orgs)))))}
    return ests


async def _ensure_establishment(db: AsyncSession, ctx: AuthContext, establishment_id: uuid.UUID, *perms: str):
    from app.models.establishments import Establishment

    est = await db.get(Establishment, establishment_id)
    if est is None or not any(ctx.can(p, org_id=est.org_id, establishment_id=est.id) for p in perms):
        raise NotFound()
    return est


@router.get("/commissions/models")
async def commission_models(ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    """Explicação dos modelos A e B com exemplos calculados pelo MESMO algoritmo (P001 §55)."""
    if not any(ctx.has_any(p) for p in ("commissions.view", "finance.view", "settings.view")):
        raise Forbidden(details={"permission": "commissions.view"})
    pool_bps, model = await commission.current_terms(db)
    return commission.models_explanation(pool_bps, model)


@router.get("/commissions")
async def list_commissions(
    establishment_id: Optional[uuid.UUID] = None,
    campaign_id: Optional[uuid.UUID] = None,
    screen_id: Optional[uuid.UUID] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    group_by: Literal["campaign", "screen", "day", "month", "campaign_screen_day"] = "day",
    ctx: AuthContext = Depends(require("commissions.view")),
    db: AsyncSession = Depends(get_db),
):
    """Valor por anúncio/campanha/tela/dia/mês, reproduções válidas e status (pendente/fechado)."""
    allowed = await _allowed_establishments(db, ctx, "commissions.view")
    if establishment_id is not None:
        if allowed is not None and establishment_id not in allowed:
            raise NotFound()
        allowed = {establishment_id}
    items = await commission.accrual_report(db, establishment_ids=allowed, campaign_id=campaign_id,
                                            screen_id=screen_id, start=start, end=end, group_by=group_by)
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.get("/commissions/campaigns/{campaign_id}")
async def campaign_commission_terms(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("commissions.view")),
                                    db: AsyncSession = Depends(get_db)):
    """Termos de comissão POR PAGAMENTO (INITIAL/RENEWAL/CHANGE) da campanha.

    Escopo GLOBAL: visão completa. Estabelecimento: só termos em que participa e só a comissão dele
    (sem cobrança/receita/pool do anunciante). Fora do escopo ⇒ 404."""
    allowed = await _allowed_establishments(db, ctx, "commissions.view")
    out = await commission.campaign_terms_report(db, campaign_id, establishment_ids=allowed)
    if out is None:
        raise NotFound()
    return out


@router.get("/establishments/{establishment_id}/balance")
async def establishment_balance(establishment_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                                db: AsyncSession = Depends(get_db)):
    """Acumulado, pendente, disponível, em saque, pago e saldo negativo (bloqueio)."""
    await _ensure_establishment(db, ctx, establishment_id, "commissions.view", "withdrawals.view", "finance.view")
    return await commission.establishment_balance(db, establishment_id)


@router.get("/commission-settings/establishments/{establishment_id}")
async def establishment_commission_settings(establishment_id: uuid.UUID,
                                            ctx: AuthContext = Depends(require("finance.view", global_only=True)),
                                            db: AsyncSession = Depends(get_db)):
    from app.models.financial import EstablishmentCommissionSetting as ECS

    await _ensure_establishment(db, ctx, establishment_id, "finance.view")
    pool_bps, model = await commission.current_terms(db)
    eff = await commission.effective_terms(db, establishment_id, pool_bps)
    hist = (await db.execute(select(ECS).where(ECS.establishment_id == establishment_id)
                             .order_by(ECS.valid_from.desc()))).scalars().all()
    return {
        "establishment_id": str(establishment_id), "instance_model": model, "instance_pool_bps": pool_bps,
        "effective": {"receives_commission": eff.receives, "weight_milli": eff.weight_milli, "rate_bps": eff.rate_bps,
                      "auto_payout": eff.auto_payout},
        "history": [{"id": str(h.id), "receives_commission": h.receives_commission, "rate_bps": h.rate_bps,
                     "weight_milli": h.weight_milli, "auto_payout": h.auto_payout, "valid_from": h.valid_from,
                     "valid_to": h.valid_to, "approved_by": str(h.approved_by) if h.approved_by else None,
                     "approval_request_id": str(h.approval_request_id) if h.approval_request_id else None}
                    for h in hist],
        "change_requires": "dupla aprovação (ação COMMISSION_EXCEPTION em /financial/approvals)",
    }


@router.post("/commissions/close-due")
async def close_due_competences(ctx: AuthContext = Depends(require("finance.manage", global_only=True,
                                                                   allow_impersonation=False)),
                                db: AsyncSession = Depends(get_db)):
    """Fecha competências encerradas (23:59:59 SP do último dia) e libera no dia 1 (idempotente)."""
    out = []
    for comp in await commission.due_competences(db):
        run = await commission.close_competence(db, comp, created_by=ctx.user_id)
        rel = await commission.release_competence(db, comp, created_by=ctx.user_id)
        out.append({"competence": comp.isoformat(), "commission_cents": run.commission_cents,
                    "residue_cents": run.residue_cents, "released_cents": rel["released"]})
    return {"closed": out}


# ─── Reembolsos ──────────────────────────────────────────────────────────────

class RefundQuoteIn(_Body):
    payment_order_id: uuid.UUID
    campaign_id: Optional[uuid.UUID] = None
    as_of: Optional[date] = None


class RefundIn(RefundQuoteIn):
    reason: str = Field(min_length=5, max_length=2000)
    override_amount_cents: Optional[StrictInt] = Field(default=None, gt=0)


async def _refund_scope(db: AsyncSession, ctx: AuthContext, order_id: uuid.UUID) -> PaymentOrder:
    o = await db.get(PaymentOrder, order_id)
    if o is None or not (ctx.can("finance.manage", global_only=True) or ctx.can("checkout.create", org_id=o.org_id)):
        raise NotFound()
    return o


@router.post("/refunds/quote")
async def refund_quote(body: RefundQuoteIn, ctx: AuthContext = Depends(get_auth_context),
                       db: AsyncSession = Depends(get_db)):
    await _refund_scope(db, ctx, body.payment_order_id)
    return await refunds.quote_refund(db, body.payment_order_id, body.campaign_id,
                                      body.as_of if ctx.can("finance.manage", global_only=True) else None)


@router.post("/refunds", status_code=201)
async def create_refund(body: RefundIn, ctx: AuthContext = Depends(get_auth_context),
                        db: AsyncSession = Depends(get_db)):
    """Registra o reembolso calculado pela política; o dinheiro só sai após dupla aprovação."""
    await _refund_scope(db, ctx, body.payment_order_id)
    staff = ctx.can("finance.manage", global_only=True)
    r = await refunds.request_refund(db, ctx, body.payment_order_id, body.campaign_id, body.reason,
                                     as_of=body.as_of if staff else None,
                                     override_amount_cents=body.override_amount_cents)
    return refunds.refund_public(r)


@router.get("/refunds")
async def list_refunds(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                       ctx: AuthContext = Depends(require("payments.view")), db: AsyncSession = Depends(get_db)):
    q = select(Refund).where(ctx.scope_filter("payments.view", org_col=Refund.org_id))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(Refund.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return _page([refunds.refund_public(r) for r in rows], total, page, page_size)


# ─── Desenvolvimento: webhook ASSINADO pelo caminho real ─────────────────────

class SimulateWebhookIn(_Body):
    payment_order_id: uuid.UUID
    status: Literal["paid", "failed", "cancelled", "expired", "refunded", "partially_refunded", "disputed"] = "paid"
    amount_cents: Optional[StrictInt] = Field(default=None, gt=0)
    fee_cents: StrictInt = Field(default=0, ge=0)


def _dev_only() -> cpp.ProviderConfig:
    cfg = cpp.get_config()
    if settings.ENVIRONMENT != "development" or cfg.mode != "fake":
        raise NotFound()
    return cfg


@router.post("/dev/simulate-webhook")
async def simulate_webhook(body: SimulateWebhookIn,
                           ctx: AuthContext = Depends(require("finance.manage", global_only=True,
                                                              allow_impersonation=False)),
                           db: AsyncSession = Depends(get_db)):
    """SOMENTE development + provedor falso: gera webhook ASSINADO (algoritmo da doc) e o
    entrega ao MESMO ingestor da rota pública (o worker processa). Nunca altera status direto."""
    cfg = _dev_only()
    o = await db.get(PaymentOrder, body.payment_order_id)
    if o is None or not o.provider_external_ref:
        raise NotFound()
    fake = cpp.FakeCloudPayPag(cfg)
    raw, headers = fake.build_webhook(status=body.status, external_ref=o.provider_external_ref, order_id=str(o.id),
                                      amount_cents=body.amount_cents or o.amount_cents, fee_cents=body.fee_cents,
                                      method=o.method)
    res = await payments.ingest_webhook(db, raw, headers, cfg.instance_slug)
    from app.core.audit import audit

    await audit(db, ctx, "dev.webhook_simulated", "payment_order", o.id, org_id=o.org_id,
                after={"status": body.status, "fake": True})
    return JSONResponse(status_code=res.status_code, content={**res.body, "simulated": True, "provider_mode": "FAKE"})


class SimulateWithdrawalIn(_Body):
    withdrawal_id: uuid.UUID
    status: Literal["completed", "failed", "cancelled"]


@router.post("/dev/simulate-withdrawal-status")
async def simulate_withdrawal_status(body: SimulateWithdrawalIn,
                                     ctx: AuthContext = Depends(require("finance.manage", global_only=True,
                                                                        allow_impersonation=False)),
                                     db: AsyncSession = Depends(get_db)):
    """SOMENTE development + provedor falso: muda o estado do saque NO PROVEDOR FALSO; o
    sistema só o aplica pela consulta ``GET /withdrawals/{external_id}`` (caminho real)."""
    from app.models.financial import Withdrawal
    from app.modules.financial import withdrawals

    cfg = _dev_only()
    w = await db.get(Withdrawal, body.withdrawal_id, with_for_update=True)
    if w is None or not w.provider_external_id:
        raise NotFound()
    await cpp.FakeCloudPayPag(cfg).set_withdrawal_status(w.provider_external_id, body.status)
    status = await withdrawals.refresh_withdrawal(db, w)
    return {"withdrawal_id": str(w.id), "status": status, "simulated": True}


# ─── Cobranças ao parceiro/estabelecimento (mensalidade, caução, instalação, upgrade) ─

class PartnerChargeIn(_Body):
    establishment_id: uuid.UUID
    kind: Literal["MONTHLY_FEE", "DEPOSIT", "INSTALLATION_FEE", "UPGRADE_FEE"]
    amount_cents: StrictInt = Field(gt=0)
    description: str = Field(min_length=3, max_length=200)
    source_type: Optional[Literal["partner_plan", "comodato_agreement", "manual"]] = None
    source_id: Optional[uuid.UUID] = None


@router.post("/partner-charges", status_code=201)
async def create_partner_charge(
    body: PartnerChargeIn,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", max_length=120),
    ctx: AuthContext = Depends(require("finance.manage", global_only=True, allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    """Cobrança CloudPayPag ao parceiro (caução = passivo reembolsável; demais = receita de serviços)."""
    ref = {"type": body.source_type, "id": body.source_id} if body.source_type else None
    o = await payments.create_partner_charge(db, ctx, body.establishment_id, body.kind, body.amount_cents,
                                             body.description, ref, idempotency_key=idempotency_key)
    return payments.order_public(o)


async def _establishment_charge_scope(db: AsyncSession, ctx: AuthContext, establishment_id: uuid.UUID):
    return await _ensure_establishment(db, ctx, establishment_id, "finance.view", "commissions.view",
                                       "withdrawals.view", "payments.view")


@router.get("/establishments/{establishment_id}/charges")
async def establishment_charges(establishment_id: uuid.UUID, page: int = Query(1, ge=1),
                                page_size: int = Query(25, ge=1, le=100),
                                ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    """Faturas do estabelecimento (cobranças ao parceiro) — somente o próprio estabelecimento ou a equipe RD."""
    await _establishment_charge_scope(db, ctx, establishment_id)
    q = select(PaymentOrder).where(PaymentOrder.reference_type == payments.REF_PARTNER_CHARGE,
                                   PaymentOrder.reference["establishment_id"].astext == str(establishment_id))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(PaymentOrder.created_at.desc()).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    return _page([payments.order_public(o, include_qr=o.status in payments.PENDING_LIKE) for o in rows], total, page,
                 page_size)


@router.get("/establishments/{establishment_id}/charges/{order_id}/receipt")
async def establishment_charge_receipt(establishment_id: uuid.UUID, order_id: uuid.UUID,
                                       ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    from app.models.organizations import Organization

    await _establishment_charge_scope(db, ctx, establishment_id)
    o = await db.get(PaymentOrder, order_id)
    if (o is None or o.reference_type != payments.REF_PARTNER_CHARGE
            or (o.reference or {}).get("establishment_id") != str(establishment_id)):
        raise NotFound()
    if o.status not in payments.PAID_LIKE:
        raise DomainError("ORDER_NOT_PAID", "Comprovante disponível somente para cobranças pagas.", status=409)
    org = await db.get(Organization, o.org_id)
    pdf, digest = invoices.receipt_pdf(o, getattr(org, "name", ""))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="comprovante-{o.id}.pdf"',
                             "X-Content-SHA256": digest})
