"""
RD Mídia Indoor — Interface pública do domínio financeiro (plano §4; W-FIN).

Outros domínios importam SOMENTE daqui:

- W-CAMP (checkout): ``create_payment_order(db, ctx, org_id, items, amount_cents, expires_at,
  {"type": "campaign_checkout", "id": order_id})``; ``cancel_payment_order``;
  ``update_service_period``; ``quote_refund``; ``request_refund``.
  Callback no ``paid``: ``app.modules.campaigns.checkout.on_order_paid(db, order_id, payment_order_id)``;
  na expiração/cancelamento pelo provedor: ``on_order_expired(db, order_id)``.
- W-ORG (planos de parceiro/comodato): ``create_partner_charge(db, ctx, establishment_id, kind ∈
  {MONTHLY_FEE, DEPOSIT, INSTALLATION_FEE, UPGRADE_FEE}, amount_cents, description, reference)``;
  callback opcional no ``paid``: ``payments.register_paid_callback("partner_charge", nome, fn)``.
- W-PLAY: ``accrue_commission_for_events(db, events)`` (PoP VÁLIDO EXTERNAL; idempotente
  por ``playback_uuid``; sem commit próprio; ``CommissionPlanUnavailable`` ⇒ reentregar).
- W-INV: fornece ``app.modules.inventory.service.contracted_exhibitions(db, campaign_id)``.
- W-BIZ (relatórios/dashboards): ``recognized_revenue``, ``contracted_revenue``,
  ``establishment_balance``, ``commission_report``, ``advertiser_invoices``, ``financial_overview``.

Itens de ``create_payment_order``: ``{"kind": "MEDIA"|"SERVICE", "campaign_id", "description",
"amount_cents", "service_start": date, "service_end": date, "refundable": bool}``;
Σ amount_cents = valor da cobrança. Receita elegível de comissão (F1) = itens MEDIA.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.financial import PaymentOrder
from app.modules.financial.commission import (  # noqa: F401
    CommissionPlanUnavailable,
    accrue_commission_for_events,
    compute_shares,
    models_explanation,
)
from app.modules.financial.payments import (  # noqa: F401
    PARTNER_CHARGE_KINDS,
    cancel_payment_order,
    create_partner_charge,
    create_payment_order,
    ensure_payer_supported,
    gateway_payer_document,
    order_public,
    process_webhook_event,
    update_service_period,
)
from app.modules.financial.refunds import quote_refund, request_refund  # noqa: F401


async def recognized_revenue(db: AsyncSession, start: date, end: date, *, org_id: Any = None) -> int:
    """Receita de mídia reconhecida (centavos) no período (competência SP)."""
    from app.modules.financial.revenue import recognized_by_period

    return await recognized_by_period(db, start, end, org_id=org_id)


async def contracted_revenue(db: AsyncSession, start: date, end: date, *, org_id: Any = None) -> dict:
    """Valor contratado (cobranças pagas) por data de pagamento; e pendente de pagamento."""
    from app.modules.financial.ledger import local_range_utc

    t0, t1 = local_range_utc(start, end)
    q = select(func.coalesce(func.sum(PaymentOrder.amount_cents), 0)).where(
        PaymentOrder.status.in_(("PAID", "PARTIALLY_REFUNDED", "REFUNDED")),
        PaymentOrder.paid_at >= t0, PaymentOrder.paid_at < t1)
    p = select(func.coalesce(func.sum(PaymentOrder.amount_cents), 0)).where(
        PaymentOrder.status.in_(("PENDING", "CREATED", "CREATION_UNKNOWN")))
    if org_id is not None:
        q = q.where(PaymentOrder.org_id == org_id)
        p = p.where(PaymentOrder.org_id == org_id)
    paid = int((await db.execute(q)).scalar_one())
    refunded_q = select(func.coalesce(func.sum(PaymentOrder.refunded_cents), 0)).where(
        PaymentOrder.paid_at >= t0, PaymentOrder.paid_at < t1)
    if org_id is not None:
        refunded_q = refunded_q.where(PaymentOrder.org_id == org_id)
    return {"paid_cents": paid, "refunded_cents": int((await db.execute(refunded_q)).scalar_one()),
            "pending_cents": int((await db.execute(p)).scalar_one())}


async def establishment_balance(db: AsyncSession, establishment_id: Any) -> dict:
    """Pendente (provisório + fechado), disponível, em saque, saldo negativo, pago, acumulado."""
    from app.modules.financial.commission import establishment_balance as _eb
    from app.modules.financial.common import as_uuid

    return await _eb(db, as_uuid(establishment_id))


async def commission_report(db: AsyncSession, *, establishment_ids: Optional[set] = None, campaign_id: Any = None,
                            start: Optional[date] = None, end: Optional[date] = None, group_by: str = "day") -> list[dict]:
    from app.modules.financial.commission import accrual_report

    return await accrual_report(db, establishment_ids=establishment_ids, campaign_id=campaign_id, start=start, end=end,
                                group_by=group_by)


async def advertiser_invoices(db: AsyncSession, org_id: Any, *, limit: int = 100) -> list[dict]:
    from app.modules.financial.common import as_uuid

    rows = (await db.execute(select(PaymentOrder).where(PaymentOrder.org_id == as_uuid(org_id))
                             .order_by(PaymentOrder.created_at.desc()).limit(limit))).scalars().all()
    return [order_public(o, include_qr=False) for o in rows]


async def financial_overview(db: AsyncSession, start: date, end: date) -> dict:
    from app.modules.financial.accounting import overview

    return await overview(db, start, end)
