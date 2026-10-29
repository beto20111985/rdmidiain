"""
RD Mídia Indoor — Checkout (carrinho → pedido → reserva 2 h → cobrança) e callbacks.

Fluxo (uma transação; READ COMMITTED; ADR_006 #5–#6, ADR_009 #7, A6 §10.3):
1. ``Idempotency-Key`` obrigatório; mesma chave + mesmo corpo ⇒ mesma resposta; corpo
   diferente ⇒ 409 ``IDEMPOTENCY_KEY_REUSED``.
2. Campanhas travadas (``FOR UPDATE``, ordem por id) e do escopo do usuário (fora ⇒ 404);
   todas da mesma organização (uma cobrança); status RASCUNHO (ou REJEITADA que exige
   novo pagamento).
3. Telas resolvidas NO SERVIDOR (segmentação × vendáveis × duração × conflito ALLOW;
   decisões gravadas); preço calculado NO SERVIDOR (``price_quote``) e CONGELADO no pedido
   e na revisão; margem negativa bloqueia; cupom consumido atomicamente.
4. ``inventory.hold(...)`` (W-INV) por campanha e ``financial.create_payment_order(...)``
   (W-FIN) com reference ``campaign_checkout``; campanha → AGUARDANDO_PAGAMENTO.
Qualquer falha desfaz tudo (sem reserva/cobrança/cupom órfãos).
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.core.money import allocate
from app.core.settings_service import get_setting
from app.core.timeutils import isoformat_z, utcnow
from app.models.campaigns import (
    Campaign,
    CampaignRevision,
    CampaignStatus,
    RevisionKind,
    RevisionStatus,
)
from app.models.pricing import Order, OrderItem, OrderKind, OrderStatus
from app.modules.campaigns import service as S
from app.modules.campaigns.notify import defer_event
from app.modules.pricing import engine as PE
from app.modules.pricing import service as PS

IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:\-]{8,100}$")
MAX_CART = 20


@dataclass
class CheckoutResult:
    order: Order
    payment: Any
    response: dict
    replayed: bool = False
    warnings: list = field(default_factory=list)
    failed: bool = False  # cobrança não pôde ser criada (pedido cancelado, registro preservado)


def validate_payment_method(method: str, token: Optional[str], installments: int) -> None:
    if method not in ("pix", "credit_card"):
        raise DomainError("PAYMENT_METHOD_INVALID", "Forma de pagamento inválida (pix ou credit_card).", status=400)
    if method == "credit_card" and not token:
        raise DomainError("PAYMENT_SOURCE_TOKEN_REQUIRED", "Pagamento com cartão exige o token do cartão.", status=400)
    if method == "pix" and (token or installments != 1):
        raise DomainError("PAYMENT_METHOD_INVALID", "PIX não aceita token de cartão nem parcelamento.", status=400)


def request_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _dep(name: str, module: str):
    try:
        mod = __import__(module, fromlist=[name])
    except Exception:
        mod = None
    fn = getattr(mod, name, None) if mod is not None else None
    if fn is None:
        raise DomainError("CHECKOUT_DEPENDENCY_UNAVAILABLE",
                          "Checkout indisponível no momento (serviço de inventário/financeiro).", status=503,
                          details={"missing": f"{module}.{name}"})
    return fn


def _attr(obj: Any, name: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def payment_public(payment: Any) -> dict:
    """Dados públicos da cobrança (PIX copia-e-cola/QR etc.), sem segredos."""
    if payment is None:
        return {}
    for m in ("public_dict", "to_public", "as_public"):
        fn = getattr(payment, m, None)
        if callable(fn):
            return fn()
    if isinstance(payment, dict):
        d = payment
    else:
        d = {k: v for k, v in vars(payment).items() if not k.startswith("_")} if hasattr(payment, "__dict__") else {}
    allowed = ("id", "status", "amount_cents", "expires_at", "method", "installments", "pix_copy_paste",
               "pix_qr_code", "pix_qr_code_base64", "checkout_url", "external_ref")
    out = {k: d.get(k) for k in allowed if k in d}
    return {k: (str(v) if isinstance(v, uuid.UUID) else (isoformat_z(v) if hasattr(v, "tzinfo") and v is not None
                                                          and hasattr(v, "isoformat") else v))
            for k, v in out.items()}


async def _validate_campaign_for_checkout(db: AsyncSession, c: Campaign) -> list:
    if not (c.status == CampaignStatus.RASCUNHO.value or
            (c.status == CampaignStatus.REJEITADA.value and c.requires_new_payment)):
        raise Conflict("CAMPAIGN_NOT_CHECKOUTABLE", "Campanha não pode ir para o checkout neste status.",
                       details={"campaign_id": str(c.id), "status": c.status})
    if c.start_date is None or c.end_date is None:
        raise DomainError("CAMPAIGN_INCOMPLETE", "Defina as datas de início e fim.", status=400,
                          details={"campaign_id": str(c.id)})
    if c.start_date < S.today_local():
        raise DomainError("CAMPAIGN_START_IN_PAST", "A data de início já passou.", status=400,
                          details={"campaign_id": str(c.id)})
    targets = await S.targets_of(db, c.id)
    if not any(not t.is_exclude for t in targets):
        raise DomainError("TARGET_REQUIRED", "Defina a segmentação da campanha.", status=400,
                          details={"campaign_id": str(c.id)})
    # criativos presentes precisam ser válidos; sem criativos ⇒ AGUARDANDO_MIDIA após o pagamento
    cl = await S.target_creatives(db, c)
    if cl:
        ready, problems = await S.creatives_ready(db, c, creatives=cl)
        if not ready:
            raise DomainError("CREATIVES_INVALID", "Há criativos inválidos na campanha.", status=400,
                              details={"campaign_id": str(c.id), "problems": problems})
    return targets


async def _quote_for(db: AsyncSession, ctx, c: Campaign, screens: list, *, coupon_code: Optional[str] = None,
                     coupon_fact: Optional[PE.CouponFact] = None, commercial_discount_bps: int = 0) -> dict:
    fees = ("ART",) if c.request_art_service else ()
    req = PS.PriceQuoteRequest(
        org_id=c.org_id, duration_s=c.duration_s, freq_kind=c.freq_kind, freq_value=c.freq_value,
        start_date=c.start_date, end_date=c.end_date, period_type=c.period_type, screens=screens,
        days_mask=c.days_mask, time_bands=c.time_bands, specific_times=list(c.specific_times or []),
        priority=c.priority, exclusivity=c.exclusivity, fees=fees, plan_id=c.plan_id, coupon_code=coupon_code,
        coupon_fact=coupon_fact, commercial_discount_bps=commercial_discount_bps,
        with_seller=c.sales_mode == "COMMERCIAL" or c.seller_user_id is not None,
    )
    return await PS.price_quote(db, ctx, req)


def _coupon_total(q: dict) -> int:
    return -sum(line["amount_cents"] for line in q["lines"] if line["code"] == "COUPON")


def _wfin_items(c: Campaign, q: dict, start=None, end=None) -> list[dict]:
    """Itens da cobrança (contrato W-FIN): Σ(MEDIA + SERVICE) − Σ(DISCOUNT) = valor cobrado.
    MEDIA = mídia bruta (+ ajuste de preço mínimo); DISCOUNT = plano/promoção/comercial/cupom."""
    start = start or c.start_date
    end = end or c.end_date
    media = int(q["media_gross_cents"]) + int(q["min_price_adjustment_cents"])
    items = [{"kind": "MEDIA", "campaign_id": c.id, "description": f"Campanha {c.name}",
              "amount_cents": media, "service_start": start, "service_end": end}]
    if int(q["discount_cents"]) > 0:
        items.append({"kind": "DISCOUNT", "campaign_id": c.id, "amount_cents": int(q["discount_cents"]),
                      "description": ", ".join(line["description"] for line in q["lines"]
                                               if line["kind"] == "DISCOUNT")[:255]})
    for line in q["lines"]:
        if line["kind"] == "SERVICE":
            items.append({"kind": "SERVICE", "campaign_id": c.id, "description": line["description"],
                          "amount_cents": int(line["amount_cents"]), "service_start": start,
                          "service_end": end, "refundable": False})
    return items


async def checkout(
    db: AsyncSession,
    ctx,
    *,
    campaign_ids: Iterable[uuid.UUID],
    idempotency_key: Optional[str],
    coupon_code: Optional[str] = None,
    kind: str = OrderKind.CHECKOUT.value,
    proposal_id: Optional[uuid.UUID] = None,
    commercial_discount_bps: int = 0,
    discount_approved_by: Optional[uuid.UUID] = None,
    method: str = "pix",
    payment_source_token: Optional[str] = None,
    installments: int = 1,
) -> CheckoutResult:
    if not idempotency_key or not IDEMPOTENCY_RE.match(idempotency_key):
        raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "Envie o header Idempotency-Key (8–100 caracteres).", status=400)
    validate_payment_method(method, payment_source_token, installments)
    ids = sorted({uuid.UUID(str(x)) for x in campaign_ids}, key=str)
    if not ids or len(ids) > MAX_CART:
        raise DomainError("CART_INVALID", f"Informe de 1 a {MAX_CART} campanhas.", status=400)
    camps = list((await db.execute(select(Campaign).where(Campaign.id.in_(ids)).order_by(Campaign.id)
                                   .with_for_update().execution_options(populate_existing=True))).scalars())
    if len(camps) != len(ids):
        raise NotFound()
    for c in camps:
        if ctx.kind == "user":
            ctx.ensure("checkout.create", org_id=c.org_id)
    orgs = {c.org_id for c in camps}
    if len(orgs) != 1:
        raise DomainError("CART_MIXED_ORGS", "Todas as campanhas da cobrança devem ser da mesma organização.",
                          status=400)
    org_id = orgs.pop()
    code = (coupon_code or "").strip().upper() or None
    payload = {"campaign_ids": [str(i) for i in ids], "coupon_code": code, "kind": kind,
               "proposal_id": str(proposal_id) if proposal_id else None,
               "commercial_discount_bps": commercial_discount_bps,
               # o token do cartão NÃO entra no hash (é segredo de uso único e nunca é persistido)
               "method": method, "installments": installments}
    rh = request_hash(payload)
    existing = (await db.execute(select(Order).where(Order.org_id == org_id, Order.idempotency_key == idempotency_key))
                ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != rh:
            raise Conflict("IDEMPOTENCY_KEY_REUSED", "Esta Idempotency-Key já foi usada com outro conteúdo.")
        return CheckoutResult(order=existing, payment=None, response=existing.response or {}, replayed=True,
                              failed=bool((existing.response or {}).get("error")))

    now = utcnow()
    hours = int(await get_setting(db, "inventory.reservation_hours"))
    expires_at = now + timedelta(hours=hours)
    order = Order(id=uuid.uuid4(), org_id=org_id, kind=kind, status=OrderStatus.AGUARDANDO_PAGAMENTO.value,
                  idempotency_key=idempotency_key, request_hash=rh, expires_at=expires_at,
                  created_by=getattr(ctx, "user_id", None), proposal_id=proposal_id,
                  commercial_discount_bps=commercial_discount_bps,
                  commercial_discount_approved_by=discount_approved_by, media_cents=0, fees_cents=0,
                  discount_cents=0, credit_applied_cents=0, total_cents=0)
    db.add(order)
    await db.flush([order])

    # 1) resolução de telas + cotação sem cupom
    prepared = []
    for c in camps:
        targets = await _validate_campaign_for_checkout(db, c)
        res = await S.resolve_campaign(db, c, targets=targets, record=True, ctx=ctx)
        if not res.screens:
            raise Conflict("NO_SCREENS_AVAILABLE", "Nenhuma tela disponível para a segmentação desta campanha.",
                           details={"campaign_id": str(c.id), "excluded_by_conflict":
                                    len(res.excluded_conflict_establishments),
                                    "excluded_by_duration": res.excluded_duration})
        q0 = await _quote_for(db, ctx, c, res.screens, commercial_discount_bps=commercial_discount_bps)
        prepared.append((c, targets, res, q0))

    # 2) cupom no nível do pedido (percentual por campanha; valor fixo rateado)
    coupon = None
    if code:
        plan_ids = {c.plan_id for c in camps}
        coupon = await PS.find_coupon(db, code, org_id=org_id, plan_id=next(iter(plan_ids)) if len(plan_ids) == 1
                                      else None, at=now)
        gross = sum(q0["media_gross_cents"] for _, _, _, q0 in prepared)
        if gross < int(coupon.min_order_cents):
            raise DomainError("COUPON_MIN_ORDER", "Valor mínimo do pedido para este cupom não atingido.", status=400,
                              details={"min_order_cents": int(coupon.min_order_cents)})
        if coupon.discount_type == "PERCENT":
            facts = {c.id: PE.CouponFact(str(coupon.id), coupon.code, "PERCENT", discount_bps=coupon.discount_bps)
                     for c, *_ in prepared}
        else:
            alloc = allocate(int(coupon.discount_cents),
                             [(c.id, int(q0["media_cents"])) for c, _, _, q0 in prepared])
            shares = dict(alloc.shares)
            if alloc.residual_cents and shares:
                first = max(shares, key=lambda k: (shares[k], str(k)))
                shares[first] += alloc.residual_cents
            facts = {cid: PE.CouponFact(str(coupon.id), coupon.code, "FIXED", discount_cents=v)
                     for cid, v in shares.items() if v > 0}
        requoted = []
        for c, targets, res, q0 in prepared:
            f = facts.get(c.id)
            q = q0 if f is None else await _quote_for(db, ctx, c, res.screens, coupon_fact=f,
                                                      commercial_discount_bps=commercial_discount_bps)
            requoted.append((c, targets, res, q))
        prepared = requoted

    # 3) congelamento, reservas e transição
    hold_fn = _dep("hold", "app.modules.inventory.service")
    create_payment = _dep("create_payment_order", "app.modules.financial.service")
    warnings: list[str] = []
    wfin_items: list[dict] = []
    resp_items = []
    for c, targets, res, q in prepared:
        warnings += [f"{c.id}:{w}" for w in PS.ensure_margin_allowed(q)]
        if c.plan_id is not None:
            from app.models.pricing import Plan

            plan = await db.get(Plan, c.plan_id)
            if plan is not None and plan.max_screens is not None and len(res.screens) > plan.max_screens:
                raise DomainError("PLAN_LIMIT_SCREENS", "A segmentação excede o limite de telas do plano.", status=409,
                                  details={"max_screens": plan.max_screens, "screens": len(res.screens)})
        spec = S.spec_of(c, targets, await S.segment_ids_of(db, c.id))
        rev = CampaignRevision(
            id=uuid.uuid4(), campaign_id=c.id, number=await S.next_revision_number(db, c.id),
            kind=RevisionKind.INITIAL.value, status=RevisionStatus.AGUARDANDO_PAGAMENTO.value, spec=spec,
            screen_ids=[str(s) for s in res.screen_ids],
            creative_ids=[str(x.id) for x in await S.target_creatives(db, c)], quote=q, quote_hash=q["quote_hash"],
            price_cents=int(q["total_cents"]), price_diff_cents=int(q["total_cents"]), order_id=order.id,
            created_by=getattr(ctx, "user_id", None),
        )
        db.add(rev)
        await db.flush([rev])
        hold = await hold_fn(db, ctx, c.id, res.screen_ids, [S.pattern_from_spec(spec)],
                             f"{order.id}:{c.id}", timedelta(hours=hours))
        rev.hold_id = _attr(hold, "id")
        item = OrderItem(
            id=uuid.uuid4(), order_id=order.id, campaign_id=c.id, revision_id=rev.id,
            media_cents=int(q["media_gross_cents"]) + int(q["min_price_adjustment_cents"]),
            fees_cents=int(q["fees_cents"]), discount_cents=int(q["discount_cents"]),
            amount_cents=int(q["total_cents"]), quote=q, quote_hash=q["quote_hash"], hold_id=rev.hold_id,
        )
        db.add(item)
        order.media_cents += item.media_cents
        order.fees_cents += item.fees_cents
        order.discount_cents += item.discount_cents
        order.total_cents += item.amount_cents
        await S._transition(db, ctx, c, CampaignStatus.AGUARDANDO_PAGAMENTO.value, "checkout",
                            revision_id=rev.id,
                            values={"pending_revision_id": rev.id, "requires_new_payment": False,
                                    "payment_issue": None})
        await audit(db, ctx, "campaign.price_frozen", "campaign", c.id,
                    after={"order_id": str(order.id), "revision": rev.number, "total_cents": item.amount_cents,
                           "quote_hash": q["quote_hash"], "screens": len(res.screens),
                           "margin_bps": q["margin"]["margin_bps"], "margin_status": q["margin"]["status"]},
                    org_id=c.org_id)
        wfin_items += _wfin_items(c, q)
        resp_items.append({"campaign_id": str(c.id), "revision": rev.number, "amount_cents": item.amount_cents,
                           "media_cents": int(q["media_cents"]), "fees_cents": int(q["fees_cents"]),
                           "discount_cents": int(q["discount_cents"]), "screens_count": len(res.screens),
                           "quote_hash": q["quote_hash"], "quote": PE.public_view(q)})
    if order.total_cents <= 0:
        raise DomainError("ORDER_TOTAL_ZERO", "Pedido sem valor a pagar não é permitido.", status=409)
    if coupon is not None:
        disc = sum(_coupon_total(q) for _, _, _, q in prepared)
        order.coupon_id = coupon.id
        await PS.redeem_coupon(db, coupon, org_id=org_id, order_id=order.id, discount_cents=disc)
    await db.flush()
    sellers = {c.seller_user_id for c in camps}
    pay_kw = {"method": method, "installments": installments}
    if method == "credit_card":
        pay_kw["payment_source_token"] = payment_source_token
    if len(sellers) == 1 and None not in sellers:
        pay_kw["seller_user_id"] = next(iter(sellers))
    payment = await create_payment(db, ctx, org_id, wfin_items, int(order.total_cents), expires_at,
                                   {"type": "campaign_checkout", "id": order.id}, **pay_kw)
    order.payment_order_id = _attr(payment, "id")
    failed = str(_attr(payment, "status") or "") == "CREATION_FAILED"
    if failed:
        # gateway recusou criar a cobrança: nada fica reservado/consumido; o registro do W-FIN é preservado
        await _release_order(db, ctx, order, status=OrderStatus.CANCELADO.value, reason="PAYMENT_CREATION_FAILED")
    response = {
        "order_id": str(order.id), "status": order.status, "kind": order.kind,
        "media_cents": order.media_cents, "fees_cents": order.fees_cents, "discount_cents": order.discount_cents,
        "total_cents": order.total_cents, "expires_at": isoformat_z(expires_at),
        "coupon_code": coupon.code if coupon else None, "items": resp_items,
        "payment": payment_public(payment), "warnings": warnings,
    }
    if failed:
        response["error"] = {"code": "PAYMENT_CREATION_FAILED",
                             "message": "Não foi possível gerar a cobrança no meio de pagamento. Tente novamente.",
                             "details": {"order_id": str(order.id)}}
    elif str(_attr(payment, "status") or "") == "CREATION_UNKNOWN":
        response["warnings"] = warnings + ["PAYMENT_CREATION_UNKNOWN"]
    order.response = json.loads(json.dumps(response, default=str))
    await audit(db, ctx, "order.created", "order", order.id,
                after={"total_cents": order.total_cents, "campaigns": [str(c.id) for c in camps],
                       "coupon": code, "payment_order_id": str(order.payment_order_id) if order.payment_order_id
                       else None, "margin_warnings": warnings}, org_id=org_id)
    if warnings:
        await audit(db, ctx, "pricing.margin_below_min", "order", order.id, after={"warnings": warnings},
                    org_id=org_id)
    return CheckoutResult(order=order, payment=payment, response=order.response, warnings=warnings, failed=failed)


# ─── callbacks financeiros ──────────────────────────────────────────────────

def _hold_status(result: Any) -> Optional[str]:
    st = _attr(result, "status")
    return str(st) if st is not None else None


async def on_order_paid(db: AsyncSession, order_id: uuid.UUID, payment_order_id: uuid.UUID, *, ctx=None) -> Order:
    """W-FIN: cobrança ``campaign_checkout`` paga → converte reservas e confirma cada campanha. Idempotente."""
    ctx = ctx or S._sys_ctx("payment")
    order = (await db.execute(select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if order is None:
        raise NotFound()
    if order.status == OrderStatus.PAGO.value:
        return order
    if order.payment_order_id is not None and order.payment_order_id != payment_order_id:
        raise Conflict("PAYMENT_ORDER_MISMATCH", "Cobrança não corresponde ao pedido.")
    if order.status == OrderStatus.CANCELADO.value:
        # pagamento de pedido cancelado: não reativa nada; o financeiro trata estorno
        await audit(db, ctx, "order.paid_after_cancel", "order", order.id, org_id=order.org_id)
        return order
    late = order.status == OrderStatus.EXPIRADO.value
    order.status = OrderStatus.PAGO.value
    order.paid_at = utcnow()
    order.payment_order_id = payment_order_id
    convert = _dep("convert_hold", "app.modules.inventory.service")
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.campaign_id))
             ).scalars().all()
    for it in items:
        issue = None
        if it.hold_id is not None:
            res = await convert(db, it.hold_id)
            if _hold_status(res) == "PAID_WITHOUT_INVENTORY":
                issue = "PAID_WITHOUT_INVENTORY"
        await S.on_payment_confirmed(db, it.campaign_id, payment_order_id, ctx=ctx, order_id=order.id,
                                     payment_issue=issue, revision_id=it.revision_id)
    await audit(db, ctx, "order.paid", "order", order.id, after={"payment_order_id": str(payment_order_id),
                                                               "late_payment": late}, org_id=order.org_id)
    defer_event(db, "payment.updated", {"order_id": str(order.id), "status": order.status}, org_id=order.org_id)
    return order


async def _release_order(db: AsyncSession, ctx, order: Order, *, status: str, reason: str,
                         keep_campaign: Optional[uuid.UUID] = None) -> None:
    order.status = status
    if status == OrderStatus.CANCELADO.value:
        order.cancelled_at = utcnow()
    await PS.release_coupon(db, order.id)
    release = None
    try:
        release = _dep("release_hold", "app.modules.inventory.service")
    except DomainError:
        release = None
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.campaign_id))
             ).scalars().all()
    for it in items:
        if it.hold_id is not None and release is not None:
            await release(db, it.hold_id, reason)
        rev = await db.get(CampaignRevision, it.revision_id) if it.revision_id else None
        if rev is not None and rev.kind == RevisionKind.RENEWAL.value:
            # renovação: a cobrança (2 h) venceu/cancelou, mas a PROPOSTA continua aberta até o fim do período
            if rev.order_id == order.id:
                rev.hold_id = None
            await audit(db, ctx, "campaign.renewal_order_closed", "campaign", it.campaign_id,
                        after={"revision": rev.number, "order_id": str(order.id), "status": status, "reason": reason},
                        org_id=order.org_id)
            continue
        if rev is not None and rev.status == RevisionStatus.AGUARDANDO_PAGAMENTO.value:
            rev.status = RevisionStatus.EXPIRADA.value if status == OrderStatus.EXPIRADO.value \
                else RevisionStatus.CANCELADA.value
        c = (await db.execute(select(Campaign).where(Campaign.id == it.campaign_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
        if rev is not None and rev.kind == RevisionKind.CHANGE.value:
            if c.pending_revision_id == rev.id:
                await S._discard_pending_change(db, ctx, c, rev, status=rev.status, reason=reason)
                c.pending_revision_id = None
            continue
        if c.id == keep_campaign:
            continue
        if c.status == CampaignStatus.AGUARDANDO_PAGAMENTO.value and c.pending_revision_id == it.revision_id:
            trig = "payment_expired" if status == OrderStatus.EXPIRADO.value else "order_cancelled"
            await S._transition(db, ctx, c, CampaignStatus.RASCUNHO.value, trig, reason=reason)


async def on_order_expired(db: AsyncSession, order_id: uuid.UUID, *, ctx=None) -> Order:
    """Cobrança/reserva expirada sem pagamento: libera reservas e cupom; campanha → RASCUNHO.

    A revisão expirada continua referenciada: um pagamento tardio ainda é aceito (A6 Q17)."""
    ctx = ctx or S._sys_ctx("worker")
    order = (await db.execute(select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if order is None:
        raise NotFound()
    if order.status != OrderStatus.AGUARDANDO_PAGAMENTO.value:
        return order
    await _release_order(db, ctx, order, status=OrderStatus.EXPIRADO.value, reason="PAYMENT_EXPIRED")
    await audit(db, ctx, "order.expired", "order", order.id, org_id=order.org_id)
    return order


async def cancel_order(db: AsyncSession, ctx, order_id: uuid.UUID, *, reason: str,
                       keep_campaign: Optional[uuid.UUID] = None, check_scope: bool = True) -> Order:
    order = (await db.execute(select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if order is None:
        raise NotFound()
    if check_scope and ctx is not None and getattr(ctx, "kind", "user") == "user":
        ctx.ensure("checkout.create", org_id=order.org_id)
    if order.status != OrderStatus.AGUARDANDO_PAGAMENTO.value:
        raise Conflict("ORDER_NOT_CANCELLABLE", "Somente pedidos aguardando pagamento podem ser cancelados.",
                       details={"status": order.status})
    cancel_payment = None
    try:
        cancel_payment = _dep("cancel_payment_order", "app.modules.financial.service")
    except DomainError:
        cancel_payment = None
    if cancel_payment is not None and order.payment_order_id is not None:
        await cancel_payment(db, ctx, order.payment_order_id, reason)
    await _release_order(db, ctx, order, status=OrderStatus.CANCELADO.value, reason="ORDER_CANCELLED",
                         keep_campaign=keep_campaign)
    await audit(db, ctx, "order.cancelled", "order", order.id, reason=reason, org_id=order.org_id)
    return order


async def expire_overdue_orders(db: AsyncSession, now=None, *, limit: int = 500) -> list[uuid.UUID]:
    now = now or utcnow()
    ids = (await db.execute(select(Order.id).where(Order.status == OrderStatus.AGUARDANDO_PAGAMENTO.value,
                                                   Order.expires_at <= now).order_by(Order.expires_at).limit(limit))
           ).scalars().all()
    for oid in ids:
        await on_order_expired(db, oid)
    return list(ids)


# ─── proposta CRM → pedido (W-BIZ) ───────────────────────────────────────────

async def create_order_from_proposal(
    db: AsyncSession,
    ctx,
    *,
    proposal_id: uuid.UUID,
    org_id: uuid.UUID,
    campaign_ids: list,
    idempotency_key: str,
    discount_bps: int = 0,
    discount_approved_by: Optional[uuid.UUID] = None,
    coupon_code: Optional[str] = None,
) -> CheckoutResult:
    from app.core.authz import AuthContext, load_grants
    from app.models.users import User

    if isinstance(discount_bps, bool) or not isinstance(discount_bps, int) or not (0 <= discount_bps <= 10000):
        raise DomainError("DISCOUNT_INVALID", "Desconto inválido.", status=400)
    if discount_bps:
        try:
            limit = int(await get_setting(db, "crm.discount_limit_bps"))
        except KeyError:
            limit = 1000
        if discount_bps > limit:
            if discount_approved_by is None:
                raise DomainError("DISCOUNT_APPROVAL_REQUIRED", "Desconto acima do limite exige aprovação.", status=403)
            if discount_approved_by == getattr(ctx, "user_id", None):
                raise DomainError("DISCOUNT_SELF_APPROVAL", "O vendedor não pode aprovar o próprio desconto.",
                                  status=403)
            approver = await db.get(User, discount_approved_by)
            if approver is None or not approver.is_active:
                raise DomainError("DISCOUNT_APPROVER_INVALID", "Aprovador do desconto inválido.", status=403)
            actx = AuthContext(kind="user", user=approver, grants=await load_grants(db, approver.id))
            if not actx.can("crm.discount_approve", org_id=org_id):
                raise DomainError("DISCOUNT_APPROVER_INVALID", "Aprovador sem permissão de aprovar desconto.",
                                  status=403)
    res = await checkout(db, ctx, campaign_ids=campaign_ids, idempotency_key=idempotency_key, coupon_code=coupon_code,
                         kind=OrderKind.PROPOSAL.value, proposal_id=proposal_id, commercial_discount_bps=discount_bps,
                         discount_approved_by=discount_approved_by)
    if res.order.org_id != org_id:
        raise NotFound()
    return res


def order_out(o: Order, *, items: Optional[list] = None, public: bool = True) -> dict:
    out = {
        "id": str(o.id), "org_id": str(o.org_id), "kind": o.kind, "status": o.status,
        "media_cents": o.media_cents, "fees_cents": o.fees_cents, "discount_cents": o.discount_cents,
        "credit_applied_cents": o.credit_applied_cents, "total_cents": o.total_cents,
        "payment_order_id": str(o.payment_order_id) if o.payment_order_id else None,
        "expires_at": o.expires_at, "paid_at": o.paid_at, "created_at": o.created_at,
    }
    if items is not None:
        out["items"] = [{"campaign_id": str(i.campaign_id), "amount_cents": i.amount_cents,
                         "media_cents": i.media_cents, "fees_cents": i.fees_cents, "discount_cents": i.discount_cents,
                         "quote_hash": i.quote_hash, "quote": PE.public_view(i.quote) if public else i.quote}
                        for i in items]
    return out
