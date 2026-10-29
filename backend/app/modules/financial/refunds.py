"""
RD Mídia Indoor — Cancelamento/reembolso (ETAPA2 #1041–#1051; P001 §90; P002 item 31; A5 D7).

Política (``refund.window_days`` = 7):
- ANTES do início (dia local SP < início): reembolso integral da mídia + serviços
  marcados ``refundable``.
- Após o início, dentro da janela: proporcional à execução COMPROVADA (PoP válido):
  ``mantido = ceil(R_mídia · Σmin(V,E) / ΣE)``; reembolso = R_mídia − mantido; serviços
  já executados não são reembolsados. Sem plano contratado disponível ⇒ fração por
  dias decorridos (registrada no cálculo).
- Depois da janela: negado (409), salvo decisão do admin (``override_amount_cents``),
  que também passa pela dupla aprovação.

Execução: TODO reembolso tira dinheiro da RD ⇒ dupla aprovação (F3) com a ação
``REFUND_PIX_OUT``: ao aprovar, contabiliza (receita diferida/estorno de receita →
``ADV:RefundPayable``), cancela a apropriação futura de comissão e estorna a comissão
correspondente à parte executada reembolsada (já sacada ⇒ saldo negativo + bloqueio).
O pagamento é PIX OUT ao pagador (sem API de estorno na CloudPayPag) ou fica
``BLOCKED_EXTERNAL_DEPENDENCY`` aguardando estorno pelo provedor.
"""
from __future__ import annotations

import math
import uuid
from datetime import date
from fractions import Fraction
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.core.settings_service import get_setting
from app.core.timeutils import utcnow
from app.models.financial import (
    CommissionAccrual,
    CommissionDayPlan,
    PaymentOrder,
    Refund,
    RefundStatus as RS,
    RevenueSchedule,
    Withdrawal,
)
from app.modules.financial import commission, common, ledger, revenue


async def _executed_ratio(db: AsyncSession, sch: RevenueSchedule, as_of: date) -> tuple[Fraction, int, int, str]:
    """(fração executada, Σmin(V,E), ΣE, método) do TERMO desta cobrança × campanha."""
    t = await commission.terms_for_payment(db, sch.campaign_id, sch.payment_order_id)
    if t is not None and t.expected_total:
        rows = (await db.execute(
            select(CommissionAccrual.screen_id, CommissionAccrual.local_day, func.sum(CommissionAccrual.valid_count))
            .where(CommissionAccrual.terms_id == t.id, CommissionAccrual.local_day <= as_of)
            .group_by(CommissionAccrual.screen_id, CommissionAccrual.local_day)
        )).all()
        executed = 0
        for sid, d, v in rows:
            dp = await db.get(CommissionDayPlan, (t.id, d))
            executed += min(int(v), int(((dp.plan if dp else None) or {}).get(str(sid), 0)))
        expected_total = int(t.expected_total)
        return Fraction(min(executed, expected_total), expected_total), executed, expected_total, "PROOF_OF_PLAY"
    total = (sch.end_date - sch.start_date).days + 1
    elapsed = min(max((as_of - sch.start_date).days + 1, 0), total)
    return Fraction(elapsed, total), elapsed, total, "ELAPSED_DAYS_FALLBACK"


async def quote_refund(db: AsyncSession, payment_order_id: Any, campaign_id: Any = None, as_of: Optional[date] = None,
                       *, override_amount_cents: Optional[int] = None) -> dict:
    order = await db.get(PaymentOrder, common.as_uuid(payment_order_id))
    if order is None:
        raise NotFound()
    if order.status not in ("PAID", "PARTIALLY_REFUNDED"):
        raise Conflict("REFUND_ORDER_NOT_PAID", "Somente cobranças pagas podem ser reembolsadas.",
                       details={"status": order.status})
    as_of = as_of or ledger.today_local()
    window = int(await get_setting(db, "refund.window_days"))
    q = select(RevenueSchedule).where(RevenueSchedule.payment_order_id == order.id)
    if campaign_id is not None:
        q = q.where(RevenueSchedule.campaign_id == common.as_uuid(campaign_id))
    schedules = (await db.execute(q)).scalars().all()
    if campaign_id is not None and not schedules:
        raise NotFound()
    parts = []
    media_refund = 0
    policy = None
    allowed = True
    for sch in schedules:
        net = sch.eligible_cents - sch.refunded_cents
        if sch.status == "CANCELLED" or net <= 0:
            continue
        if as_of < sch.start_date:
            p, refund_m, ratio, exe, exp_, method = "FULL_BEFORE_START", net, Fraction(0), 0, 0, "NOT_STARTED"
        elif (as_of - sch.start_date).days < window:
            ratio, exe, exp_, method = await _executed_ratio(db, sch, as_of)
            kept = math.ceil(net * ratio)
            p, refund_m = "PROPORTIONAL", net - kept
        else:
            ratio, exe, exp_, method = await _executed_ratio(db, sch, as_of)
            p, refund_m, allowed = "WINDOW_EXPIRED", 0, False
        policy = p if policy in (None, p) else "MIXED"
        media_refund += refund_m
        parts.append({"campaign_id": str(sch.campaign_id), "schedule_id": str(sch.id), "policy": p,
                      "eligible_net_cents": net, "executed_ratio": f"{ratio.numerator}/{ratio.denominator}",
                      "executed_valid": exe, "executed_expected": exp_, "method": method,
                      "media_refund_cents": refund_m, "start_date": sch.start_date.isoformat()})
    services_refund = 0
    if policy == "FULL_BEFORE_START":
        cids = {p["campaign_id"] for p in parts}
        for it in order.items:
            if it.get("kind") == "SERVICE" and it.get("refundable") and (
                    campaign_id is None or it.get("campaign_id") in cids):
                services_refund += int(it["amount_cents"])
        services_refund = min(services_refund, order.amount_cents - order.refunded_cents - media_refund)
    total = media_refund + services_refund
    out = {"payment_order_id": str(order.id), "campaign_id": str(campaign_id) if campaign_id else None,
           "as_of": as_of.isoformat(), "window_days": window, "policy": policy, "allowed": allowed and total > 0,
           "media_refund_cents": media_refund, "services_refund_cents": services_refund, "refund_cents": total,
           "parts": parts, "override": False}
    if override_amount_cents is not None:
        max_refundable = order.amount_cents - order.refunded_cents
        if not 0 < override_amount_cents <= max_refundable:
            raise DomainError("REFUND_OVERRIDE_INVALID", "Valor de reembolso fora do permitido.",
                              details={"max_cents": max_refundable})
        media_avail = sum(p["eligible_net_cents"] for p in parts)
        out.update({"policy": "ADMIN_OVERRIDE", "allowed": True, "override": True, "refund_cents": override_amount_cents,
                    "media_refund_cents": min(override_amount_cents, media_avail),
                    "services_refund_cents": override_amount_cents - min(override_amount_cents, media_avail)})
    return out


async def request_refund(db: AsyncSession, ctx: Any, payment_order_id: Any, campaign_id: Any = None,
                         reason: str = "", *, as_of: Optional[date] = None,
                         override_amount_cents: Optional[int] = None) -> Refund:
    """Registra o reembolso calculado (AWAITING_APPROVAL). Dinheiro só sai após dupla aprovação."""
    order = await db.get(PaymentOrder, common.as_uuid(payment_order_id), with_for_update=True)
    if order is None:
        raise NotFound()
    staff = ctx.can("finance.manage", global_only=True)
    if not staff:
        ctx.ensure("checkout.create", org_id=order.org_id)
        if override_amount_cents is not None:
            raise DomainError("REFUND_OVERRIDE_FORBIDDEN", "Somente o financeiro da RD define reembolso fora da política.",
                              status=403)
    if not reason or len(reason.strip()) < 5:
        raise DomainError("REASON_REQUIRED", "Informe o motivo do cancelamento/reembolso.")
    q = await quote_refund(db, order.id, campaign_id, as_of, override_amount_cents=override_amount_cents)
    if not q["allowed"]:
        raise Conflict("REFUND_WINDOW_EXPIRED" if q["policy"] == "WINDOW_EXPIRED" else "REFUND_NOT_ALLOWED",
                       "Prazo de cancelamento com reembolso encerrado (somente decisão do admin com dupla aprovação).",
                       details={"window_days": q["window_days"]})
    open_ = (await db.execute(select(Refund.id).where(
        Refund.payment_order_id == order.id,
        Refund.status.in_((RS.AWAITING_APPROVAL.value, RS.APPROVED.value, RS.PAYING.value)),
        *( [Refund.campaign_id == common.as_uuid(campaign_id)] if campaign_id else [] ),
    ))).first()
    if open_:
        raise Conflict("REFUND_ALREADY_OPEN", "Já existe reembolso em andamento para esta cobrança.")
    ref = Refund(
        id=uuid.uuid4(), payment_order_id=order.id, campaign_id=common.as_uuid(campaign_id) if campaign_id else None,
        org_id=order.org_id, policy="ADMIN_OVERRIDE" if q["override"] else
        ("FULL_BEFORE_START" if q["policy"] == "FULL_BEFORE_START" else "PROPORTIONAL"),
        as_of=date.fromisoformat(q["as_of"]), refund_cents=q["refund_cents"], media_refund_cents=q["media_refund_cents"],
        services_refund_cents=q["services_refund_cents"],
        executed_valid=sum(p["executed_valid"] for p in q["parts"]),
        executed_expected=sum(p["executed_expected"] for p in q["parts"]),
        calculation=q, status=RS.AWAITING_APPROVAL.value, reason=reason.strip()[:2000], requested_by=ctx.user_id,
    )
    db.add(ref)
    await db.flush()
    await audit(db, ctx, "refund.requested", "refund", ref.id, org_id=order.org_id, reason=reason,
                after={"refund_cents": ref.refund_cents, "policy": ref.policy})
    return ref


def refund_public(r: Refund) -> dict:
    return {"id": str(r.id), "payment_order_id": str(r.payment_order_id),
            "campaign_id": str(r.campaign_id) if r.campaign_id else None, "policy": r.policy,
            "as_of": r.as_of.isoformat(), "refund_cents": r.refund_cents, "media_refund_cents": r.media_refund_cents,
            "services_refund_cents": r.services_refund_cents, "status": r.status, "reason": r.reason,
            "calculation": r.calculation, "approval_request_id": str(r.approval_request_id) if r.approval_request_id else None,
            "created_at": r.created_at}


async def apply_refund(db: AsyncSession, refund: Refund, approval: Any, approver_id: Any) -> dict:
    """Executada na APROVAÇÃO (dupla): contabiliza, cancela cronogramas/comissão e estorna comissão."""
    if refund.status != RS.AWAITING_APPROVAL.value:
        raise Conflict("REFUND_NOT_AWAITING", "Reembolso não está aguardando aprovação.")
    order = await db.get(PaymentOrder, refund.payment_order_id, with_for_update=True)
    deferred = await ledger.account(db, "ADV:DeferredRevenue", owner_id=order.org_id, org_id=order.org_id)
    contra = await ledger.account(db, "RD:ContraRevenue:Refunds")
    media_acc = await ledger.account(db, "RD:Revenue:Media")
    payable = await ledger.account(db, "ADV:RefundPayable", owner_id=order.org_id, org_id=order.org_id)
    legs: list = []
    remaining_media = refund.media_refund_cents
    clawbacks = []
    calc_parts = {p["schedule_id"]: p for p in (refund.calculation or {}).get("parts", [])}
    q = select(RevenueSchedule).where(RevenueSchedule.payment_order_id == order.id).with_for_update()
    if refund.campaign_id:
        q = q.where(RevenueSchedule.campaign_id == refund.campaign_id)
    for sch in (await db.execute(q)).scalars().all():
        if sch.status == "CANCELLED":
            continue
        net = sch.eligible_cents - sch.refunded_cents
        part = calc_parts.get(str(sch.id))
        m_ref = min(remaining_media, part["media_refund_cents"] if part and refund.policy != "ADMIN_OVERRIDE" else net)
        remaining_media -= m_ref
        keep = net - m_ref
        recognized = await revenue.recognized_cents(db, sch.id)
        if recognized <= keep:
            catch_up = keep - recognized
            if catch_up:
                legs += [(deferred, catch_up, "receita executada (cancelamento)"), (media_acc, -catch_up, "receita de mídia")]
            if m_ref:
                legs.append((deferred, m_ref, "reembolso de parte não executada"))
        else:
            if net - recognized:
                legs.append((deferred, net - recognized, "reembolso de receita diferida"))
            legs.append((contra, recognized - keep, "estorno de receita reconhecida"))
        sch.refunded_cents += m_ref
        sch.status = "CANCELLED"
        sch.cancelled_on = refund.as_of
        # comissão: para de apropriar após o cancelamento; estorna a parte executada reembolsada
        ratio = Fraction(part["executed_ratio"]) if part else Fraction(0)
        executed_value = net * ratio
        refunded_executed = max(Fraction(0), Fraction(m_ref) - (net - executed_value))
        frac = (refunded_executed / executed_value) if executed_value > 0 else Fraction(0)
        # comissão: só o TERMO desta cobrança (estorno de um pagamento afeta só a sua parcela)
        cc = await commission.terms_for_payment(db, sch.campaign_id, order.id)
        if cc is not None:
            cc.pool_cents = max(((sch.eligible_cents - sch.refunded_cents) * cc.pool_bps) // 10_000, 0)
            clawbacks.append(await commission.clawback_terms(
                db, cc, fraction=min(frac, Fraction(1)), key=f"refund:{refund.id}",
                reason=f"reembolso {refund.id}", cancel_from=refund.as_of, approval_request_id=approval.id,
                created_by=approver_id))
    if refund.services_refund_cents:
        legs.append((contra, refund.services_refund_cents, "reembolso de serviços"))
    if remaining_media > 0:  # override acima do diferido disponível
        legs.append((contra, remaining_media, "reembolso adicional (decisão do admin)"))
    legs.append((payable, -refund.refund_cents, "reembolso a pagar ao anunciante"))
    res = await ledger.post(db, kind="REFUND", idempotency_key=f"refund:{refund.id}:approved", legs=legs,
                            description=f"Reembolso aprovado {refund.id} ({refund.policy})", approval_request_id=approval.id,
                            created_by=approver_id, ref_type="refund", ref_id=refund.id, org_id=order.org_id,
                            meta={"policy": refund.policy, "refund_cents": refund.refund_cents})
    refund.ledger_transaction_id = res.transaction_id
    refund.status = RS.APPROVED.value
    refund.approval_request_id = approval.id
    order.refunded_cents += refund.refund_cents
    order.status = "REFUNDED" if order.refunded_cents >= order.amount_cents else "PARTIALLY_REFUNDED"
    await db.flush()
    await audit(db, None, "refund.approved", "refund", refund.id, org_id=order.org_id, actor_user_id=approver_id,
                after={"refund_cents": refund.refund_cents, "clawbacks": clawbacks})
    return {"transaction_id": str(res.transaction_id), "clawbacks": clawbacks}


async def on_payout_terminal(db: AsyncSession, w: Withdrawal) -> None:
    ref = await db.get(Refund, common.as_uuid(w.ref_id), with_for_update=True)
    if ref is None:
        return
    if w.status == "COMPLETED":
        ref.status = RS.PAID.value
    elif w.status in ("FAILED", "CANCELLED"):
        ref.status = RS.FAILED.value  # passivo continua em ADV:RefundPayable; novo pedido de aprovação
    ref.updated_at = utcnow()
