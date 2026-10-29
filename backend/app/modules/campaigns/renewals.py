"""
RD Mídia Indoor — Renovação de campanha (ETAPA2 #509–#517, #774–#776; P001 §54).

Regras (decisão do coordenador após a janela integrada — "renovação SEM reserva prioritária",
a confirmar pelo proprietário):
- Só períodos RECORRENTES (mensal, semestral, anual); avulso não renova.
- Status permitido: APROVADA, AGENDADA, SINCRONIZANDO, ATIVA, PAUSADA (versão vigente existente),
  sem outra alteração/renovação pendente e antes do fim do período vigente (23:59:59 local).
- ``renew`` cria a PROPOSTA (revisão ``RENEWAL`` em AGUARDANDO_PAGAMENTO) com cotação do período
  adicional — mesmas telas vigentes que continuem vendáveis e sem conflito, mesmos criativos e
  programação — e avisa o anunciante. **Não reserva inventário** (manual ou automática D-N).
- ``renewal_checkout`` é o checkout da renovação: recota (tabela ativa), reserva de 2 h pelo W-INV
  (``hold`` com o TTL padrão do inventário — a MESMA regra de qualquer compra; P002 #6), pedido e
  cobrança (W-FIN). Sem capacidade ⇒ 409 ``INVENTORY_UNAVAILABLE`` e a proposta continua aberta.
  Reserva/cobrança vencidas liberam o inventário; a proposta segue aberta até o fim do período.
  Pagamento tardio passa pela recaptura do W-INV (sem capacidade ⇒ PAID_WITHOUT_INVENTORY ⇒
  renovação recusada + revisão de reembolso).
- **Sem reaprovação de conteúdo**: o pagamento aplica a extensão imediatamente (status inalterado,
  sem interrupção). Qualquer outra mudança usa o pedido de alteração (com reaprovação).
- Sem pagamento até o vencimento: a proposta EXPIRA e a campanha encerra (FINALIZADA) às 23:59:59
  locais (sem tolerância, #512–#517); pagamento depois disso ⇒ 409 ``RENEWAL_NOT_PENDING``.
- Renovação automática (``campaigns.auto_renew``; padrão ligado em planos mensais): a task gera a
  PROPOSTA D-N (``campaign.renewal_notice_days``, padrão 5) e notifica (``campaign.renewal_available``).
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError
from app.core.settings_service import get_setting
from app.core.timeutils import isoformat_z, utcnow
from app.models.campaigns import (
    Campaign,
    CampaignRevision,
    RevisionKind,
    RevisionStatus,
)
from app.models.pricing import Order, OrderItem, OrderKind, OrderStatus, Plan, PlanStatus
from app.modules.campaigns import checkout as CK
from app.modules.campaigns import service as S
from app.modules.campaigns import states as ST
from app.modules.campaigns import targeting as TG
from app.modules.campaigns.notify import defer_event, mark, rollback_to
from app.modules.pricing import engine as PE
from app.modules.pricing import service as PS

log = structlog.get_logger("rd.campaigns.renewals")

MAX_PERIODS = 12
MAX_EXTENSION_DAYS = 400  # limite do padrão de contratação do W-INV


def extension_end(current_end: date, period_type: str, periods: int) -> date:
    end = current_end
    for _ in range(periods):
        end = PS.period_end(end + timedelta(days=1), period_type)
    return end


async def _live_screens(db: AsyncSession, c: Campaign, live: CampaignRevision) -> list:
    """Telas da versão vigente que continuam vendáveis e com conflito ALLOW (nada é adicionado)."""
    from app.models.establishments import Establishment as E
    from app.models.screens import Screen

    ids = [uuid.UUID(s) for s in (live.screen_ids or [])]
    if not ids:
        return []
    rows = (await db.execute(
        select(Screen.id, Screen.establishment_id, Screen.orientation, Screen.screen_class, E.org_id, E.venue_class,
               E.region, E.state, E.city_key, E.timezone)
        .join(E, E.id == Screen.establishment_id)
        .where(Screen.id.in_(ids), Screen.sellable.is_(True), Screen.status != "DECOMMISSIONED",
               E.is_active.is_(True), E.deleted_at.is_(None))
        .order_by(Screen.id))).all()
    screens = [TG.ResolvedScreen(sid, est, org, orient, sclass or "STANDARD", vclass or "STANDARD", region, state, ck,
                                 tz or S.DEFAULT_TZ, 0) for sid, est, orient, sclass, org, vclass, region, state, ck, tz
               in rows]
    if not screens:
        return []
    decisions = await S.evaluate_campaign_establishments(db, c, {s.establishment_id for s in screens}, record=True,
                                                         creatives=await S.creatives_of(db, c.id, active_only=True))
    return [s for s in screens if decisions.get(s.establishment_id) is not None
            and decisions[s.establishment_id].allowed]


async def _usable_plan_id(db: AsyncSession, spec: dict) -> Optional[uuid.UUID]:
    pid = spec.get("plan_id")
    if not pid:
        return None
    p = await db.get(Plan, uuid.UUID(pid))
    return p.id if p is not None and p.status == PlanStatus.ATIVO.value else None


def renewal_public(c: Campaign, rev: CampaignRevision, *, window_end, order: Optional[Order] = None) -> dict:
    cs = rev.change_summary or {}
    q = rev.quote or {}
    open_order = order is not None and order.status == OrderStatus.AGUARDANDO_PAGAMENTO.value
    return {
        "campaign_id": str(c.id), "revision_id": str(rev.id), "revision": rev.number,
        "stage": ("APLICADA" if rev.status == RevisionStatus.APROVADA.value else
                  "AGUARDANDO_PAGAMENTO" if open_order else
                  "PROPOSTA" if rev.status == RevisionStatus.AGUARDANDO_PAGAMENTO.value else rev.status),
        "revision_status": rev.status, "auto": bool(cs.get("auto")), "periods": cs.get("periods"),
        "previous_end_date": cs.get("previous_end_date"), "new_end_date": cs.get("new_end_date"),
        "extension_start": cs.get("extension_start"), "screens_count": len(rev.screen_ids or []),
        "total_cents": int(rev.price_diff_cents), "deadline": isoformat_z(window_end) if window_end else None,
        "quote": PE.public_view(q) if q else None,
        "order": (order.response if open_order and order.response else None),
    }


async def _pending_renewal(db: AsyncSession, c: Campaign) -> Optional[CampaignRevision]:
    if c.pending_revision_id is None:
        return None
    rev = await db.get(CampaignRevision, c.pending_revision_id)
    if rev is not None and rev.kind == RevisionKind.RENEWAL.value and \
            rev.status == RevisionStatus.AGUARDANDO_PAGAMENTO.value:
        return rev
    return None


async def _quote_extension(db: AsyncSession, ctx, c: Campaign, live: CampaignRevision, ext_start: date,
                           new_end: date) -> tuple[list, dict, list]:
    screens = await _live_screens(db, c, live)
    if not screens:
        raise Conflict("NO_SCREENS_AVAILABLE", "Nenhuma tela da campanha continua disponível para renovação.")
    spec = live.spec
    req = PS.PriceQuoteRequest(
        org_id=c.org_id, duration_s=spec["duration_s"], freq_kind=spec["freq_kind"], freq_value=spec["freq_value"],
        start_date=ext_start, end_date=new_end, period_type=spec["period_type"], screens=screens,
        days_mask=spec["days_mask"], time_bands=spec["time_bands"], specific_times=spec.get("specific_times") or [],
        priority=spec["priority"], exclusivity=spec["exclusivity"], fees=(), plan_id=await _usable_plan_id(db, spec),
        with_seller=c.sales_mode == "COMMERCIAL" or c.seller_user_id is not None)
    q = await PS.price_quote(db, ctx, req)
    warnings = [f"{c.id}:{w}" for w in PS.ensure_margin_allowed(q)]
    if int(q["total_cents"]) <= 0:
        raise DomainError("ORDER_TOTAL_ZERO", "Renovação sem valor a pagar não é permitida.", status=409)
    return screens, q, warnings


async def renew(db: AsyncSession, ctx, campaign_id: uuid.UUID, *, periods: int = 1,
                idempotency_key: Optional[str] = None, auto: bool = False) -> dict:
    """Cria (ou devolve, se já existe com os mesmos períodos) a PROPOSTA de renovação. Sem reserva."""
    if isinstance(periods, bool) or not isinstance(periods, int) or not (1 <= periods <= MAX_PERIODS):
        raise DomainError("RENEWAL_PERIODS_INVALID", f"Informe de 1 a {MAX_PERIODS} períodos.", status=400)
    c = await S.get_campaign(db, ctx, campaign_id, perm="checkout.create", for_update=True)
    if c.status not in ST.OPERATIONAL or c.live_revision_id is None:
        raise Conflict("RENEWAL_NOT_ALLOWED", "Só campanhas aprovadas e vigentes podem ser renovadas.",
                       details={"status": c.status})
    if c.period_type not in S.RECURRING_PERIODS:
        raise Conflict("RENEWAL_NOT_RECURRING", "Somente planos mensal, semestral ou anual são renováveis.",
                       details={"period_type": c.period_type})
    live = await db.get(CampaignRevision, c.live_revision_id)
    window_end = await S.campaign_end_utc(db, c, live)
    now = utcnow()
    if window_end is None or now > window_end:
        raise Conflict("RENEWAL_WINDOW_CLOSED", "O período vigente já terminou; contrate uma nova campanha.")
    pend = await _pending_renewal(db, c)
    if pend is not None:
        if (pend.change_summary or {}).get("periods") == periods:
            order = await db.get(Order, pend.order_id) if pend.order_id else None
            return {**renewal_public(c, pend, window_end=window_end, order=order), "replayed": True}
        raise Conflict("RENEWAL_ALREADY_PENDING", "Já existe uma renovação pendente para esta campanha.",
                       details={"revision": pend.number, "periods": (pend.change_summary or {}).get("periods")})
    if c.pending_revision_id is not None:
        other = await db.get(CampaignRevision, c.pending_revision_id)
        if other is not None and other.status in (RevisionStatus.AGUARDANDO_PAGAMENTO.value,
                                                  RevisionStatus.EM_ANALISE.value):
            raise Conflict("CHANGE_ALREADY_PENDING", "Há uma alteração pendente; conclua-a antes de renovar.",
                           details={"revision": other.number})
    current_end = date.fromisoformat(live.spec["end_date"])
    ext_start = current_end + timedelta(days=1)
    new_end = extension_end(current_end, c.period_type, periods)
    if (new_end - ext_start).days + 1 > MAX_EXTENSION_DAYS:
        raise DomainError("RENEWAL_TOO_LONG", f"Renovação máxima de {MAX_EXTENSION_DAYS} dias por vez.", status=400)
    screens, q, warnings = await _quote_extension(db, ctx, c, live, ext_start, new_end)
    spec = json.loads(json.dumps(live.spec))
    spec["end_date"] = new_end.isoformat()
    kept = [str(x.screen_id) for x in screens]
    rev = CampaignRevision(
        id=uuid.uuid4(), campaign_id=c.id, number=await S.next_revision_number(db, c.id),
        kind=RevisionKind.RENEWAL.value, status=RevisionStatus.AGUARDANDO_PAGAMENTO.value, spec=spec,
        screen_ids=kept, creative_ids=[str(x.id) for x in await S.creatives_of(db, c.id, active_only=True)],
        change_summary={"renewal": True, "periods": periods, "auto": auto, "previous_end_date": current_end.isoformat(),
                        "extension_start": ext_start.isoformat(), "new_end_date": new_end.isoformat(),
                        "screens_removed": sorted(set(live.screen_ids or []) - set(kept)),
                        "hold_ids": [str(h) for h in S.revision_hold_ids(live)],
                        "previous_price_cents": int(c.contracted_price_cents), "orders": []},
        quote=q, quote_hash=q["quote_hash"], price_cents=int(c.contracted_price_cents) + int(q["total_cents"]),
        price_diff_cents=int(q["total_cents"]), created_by=getattr(ctx, "user_id", None),
    )
    db.add(rev)
    await db.flush([rev])
    c.pending_revision_id = rev.id
    await audit(db, ctx, "campaign.renewal_created", "campaign", c.id,
                after={"revision": rev.number, "new_end_date": new_end.isoformat(), "total_cents": int(q["total_cents"]),
                       "auto": auto, "margin_status": q["margin"]["status"], "warnings": warnings},
                org_id=c.org_id)
    rec = await S._org_recipients(c.org_id, "checkout.create")
    if rec:
        await S._notify(db, rec, "campaign.renewal_available", {
            "campaign_name": c.name, "campaign_id": str(c.id), "new_end_date": new_end,
            "amount_cents": int(q["total_cents"]), "expires_at": window_end},
            dedupe_key=f"campaign.renewal_available:{c.id}:{rev.id}", entity=("campaign", c.id), org_id=c.org_id)
    defer_event(db, "campaign.updated", {"id": str(c.id), "status": c.status, "trigger": "renewal_created"},
                org_id=c.org_id)
    return {**renewal_public(c, rev, window_end=window_end), "replayed": False}


async def renewal_checkout(db: AsyncSession, ctx, campaign_id: uuid.UUID, *, idempotency_key: Optional[str],
                           method: str = "pix", payment_source_token: Optional[str] = None,
                           installments: int = 1) -> CK.CheckoutResult:
    """Checkout da renovação: recota, RESERVA DE 2 H (W-INV, regra padrão), pedido e cobrança."""
    if not idempotency_key or not CK.IDEMPOTENCY_RE.match(idempotency_key):
        raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "Envie o header Idempotency-Key (8–100 caracteres).", status=400)
    CK.validate_payment_method(method, payment_source_token, installments)
    c = await S.get_campaign(db, ctx, campaign_id, perm="checkout.create", for_update=True)
    payload = {"campaign_id": str(c.id), "kind": "RENEWAL_CHECKOUT", "method": method, "installments": installments}
    rh = CK.request_hash(payload)
    existing = (await db.execute(select(Order).where(Order.org_id == c.org_id, Order.idempotency_key == idempotency_key))
                ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != rh:
            raise Conflict("IDEMPOTENCY_KEY_REUSED", "Esta Idempotency-Key já foi usada com outro conteúdo.")
        return CK.CheckoutResult(order=existing, payment=None, response=existing.response or {}, replayed=True,
                                 failed=bool((existing.response or {}).get("error")))
    rev = await _pending_renewal(db, c)
    if rev is None:
        raise Conflict("RENEWAL_NOT_PENDING", "Não há proposta de renovação aberta para esta campanha.")
    if c.status not in ST.OPERATIONAL:
        raise Conflict("RENEWAL_CAMPAIGN_ENDED", "A campanha não está mais vigente para renovação.",
                       details={"status": c.status})
    if rev.order_id is not None:
        current = await db.get(Order, rev.order_id)
        if current is not None and current.status == OrderStatus.AGUARDANDO_PAGAMENTO.value:
            # cobrança da renovação já aberta: devolve a mesma (nunca duas reservas/cobranças simultâneas)
            return CK.CheckoutResult(order=current, payment=None, response=current.response or {}, replayed=True,
                                     failed=bool((current.response or {}).get("error")))
    live = await db.get(CampaignRevision, c.live_revision_id)
    now = utcnow()
    window_end = await S.campaign_end_utc(db, c, live)
    if window_end is None or now > window_end:
        raise Conflict("RENEWAL_WINDOW_CLOSED", "O período vigente já terminou; contrate uma nova campanha.")
    cs = rev.change_summary or {}
    ext_start, new_end = date.fromisoformat(cs["extension_start"]), date.fromisoformat(cs["new_end_date"])
    screens, q, warnings = await _quote_extension(db, ctx, c, live, ext_start, new_end)
    kept = [str(x.screen_id) for x in screens]
    order = Order(id=uuid.uuid4(), org_id=c.org_id, kind=OrderKind.RENEWAL.value,
                  status=OrderStatus.AGUARDANDO_PAGAMENTO.value, idempotency_key=idempotency_key, request_hash=rh,
                  media_cents=int(q["media_gross_cents"]) + int(q["min_price_adjustment_cents"]), fees_cents=0,
                  discount_cents=int(q["discount_cents"]), credit_applied_cents=0, total_cents=int(q["total_cents"]),
                  expires_at=window_end, created_by=getattr(ctx, "user_id", None))
    db.add(order)
    await db.flush([order])
    ext_spec = {**rev.spec, "start_date": ext_start.isoformat(), "end_date": new_end.isoformat()}
    hold_fn = CK._dep("hold", "app.modules.inventory.service")
    # TTL padrão do W-INV (inventory.reservation_hours = 2 h): nenhuma reserva longa de renovação
    hold = await hold_fn(db, ctx, c.id, [x.screen_id for x in screens], [S.pattern_from_spec(ext_spec)],
                         f"renewal:{order.id}", None, order_id=order.id)
    hold_id = CK._attr(hold, "id")
    hold_exp = CK._attr(hold, "hold_expires_at")
    if hold_exp is not None and hold_exp < order.expires_at:
        order.expires_at = hold_exp
    db.add(OrderItem(id=uuid.uuid4(), order_id=order.id, campaign_id=c.id, revision_id=rev.id,
                     media_cents=order.media_cents, fees_cents=0, discount_cents=order.discount_cents,
                     amount_cents=order.total_cents, quote=q, quote_hash=q["quote_hash"], hold_id=hold_id))
    # a proposta passa a refletir o preço/telas desta cobrança (cotação do servidor no momento do checkout)
    rev.quote, rev.quote_hash, rev.screen_ids = q, q["quote_hash"], kept
    rev.price_cents = int(c.contracted_price_cents) + int(q["total_cents"])
    rev.price_diff_cents = int(q["total_cents"])
    rev.order_id, rev.hold_id = order.id, hold_id
    rev.change_summary = {**cs, "orders": [*(cs.get("orders") or []), str(order.id)]}
    await db.flush()
    create_payment = CK._dep("create_payment_order", "app.modules.financial.service")
    pay_kw = {"method": method, "installments": installments}
    if method == "credit_card":
        pay_kw["payment_source_token"] = payment_source_token
    payment = await create_payment(db, ctx, c.org_id, CK._wfin_items(c, q, ext_start, new_end), int(order.total_cents),
                                   order.expires_at, {"type": "campaign_checkout", "id": order.id}, **pay_kw)
    order.payment_order_id = CK._attr(payment, "id")
    failed = str(CK._attr(payment, "status") or "") == "CREATION_FAILED"
    if failed:
        await CK._release_order(db, ctx, order, status=OrderStatus.CANCELADO.value, reason="PAYMENT_CREATION_FAILED")
    response = {
        "order_id": str(order.id), "status": order.status, "kind": order.kind, "campaign_id": str(c.id),
        "revision": rev.number, "previous_end_date": cs["previous_end_date"], "new_end_date": new_end.isoformat(),
        "periods": cs.get("periods"), "screens_count": len(kept), "media_cents": order.media_cents,
        "discount_cents": order.discount_cents, "total_cents": order.total_cents,
        "expires_at": isoformat_z(order.expires_at), "quote": PE.public_view(q), "payment": CK.payment_public(payment),
        "warnings": warnings,
    }
    if failed:
        response["error"] = {"code": "PAYMENT_CREATION_FAILED",
                             "message": "Não foi possível gerar a cobrança no meio de pagamento. Tente novamente.",
                             "details": {"order_id": str(order.id)}}
    order.response = json.loads(json.dumps(response, default=str))
    await audit(db, ctx, "campaign.renewal_checkout", "campaign", c.id,
                after={"revision": rev.number, "order_id": str(order.id), "hold_id": str(hold_id),
                       "total_cents": order.total_cents, "expires_at": order.expires_at}, org_id=c.org_id)
    defer_event(db, "campaign.updated", {"id": str(c.id), "status": c.status, "trigger": "renewal_checkout"},
                org_id=c.org_id)
    return CK.CheckoutResult(order=order, payment=payment, response=order.response, warnings=warnings, failed=failed)


async def get_renewal(db: AsyncSession, ctx, campaign_id: uuid.UUID) -> Optional[dict]:
    c = await S.get_campaign(db, ctx, campaign_id)
    rev = await _pending_renewal(db, c)
    if rev is None:
        return None
    live = await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None
    order = await db.get(Order, rev.order_id) if rev.order_id else None
    return renewal_public(c, rev, window_end=await S.campaign_end_utc(db, c, live), order=order)


async def confirm_renewal(db: AsyncSession, ctx, c: Campaign, rev: CampaignRevision, payment_order_id: uuid.UUID, *,
                          payment_issue: Optional[str] = None) -> Campaign:
    """Pagamento da renovação: aplica a extensão SEM reaprovação (só o período mudou). Idempotente."""
    if rev.status == RevisionStatus.APROVADA.value:
        if (rev.change_summary or {}).get("payment_order_id") == str(payment_order_id):
            return c
        # outra cobrança da MESMA renovação já foi aplicada: pagamento em duplicidade ⇒ estorno (W-FIN)
        raise Conflict("RENEWAL_ALREADY_PAID", "Esta renovação já foi paga por outra cobrança.",
                       details={"revision": rev.number})
    if rev.status != RevisionStatus.AGUARDANDO_PAGAMENTO.value or c.pending_revision_id != rev.id:
        # pedido expirado/cancelado: nada é reativado — o financeiro trata a divergência/estorno
        raise Conflict("RENEWAL_NOT_PENDING", "Renovação não está mais aguardando pagamento.",
                       details={"revision_status": rev.status})
    if c.status not in ST.OPERATIONAL:
        raise Conflict("RENEWAL_CAMPAIGN_ENDED", "A campanha não está mais vigente para renovação.",
                       details={"status": c.status})
    now = utcnow()
    if payment_issue is not None:
        rev.status = RevisionStatus.REJEITADA.value
        rev.decided_at, rev.decision_reason = now, payment_issue
        rev.change_summary = {**(rev.change_summary or {}), "requires_refund_review": True,
                              "payment_order_id": str(payment_order_id)}
        c.pending_revision_id = None
        c.payment_issue = f"RENEWAL_{payment_issue}"
        await audit(db, ctx, "campaign.renewal_without_inventory", "campaign", c.id,
                    after={"revision": rev.number, "payment_order_id": str(payment_order_id)}, org_id=c.org_id)
        staff = await S._staff_recipients("campaigns.approve")
        if staff:
            await S._notify(db, staff, "campaign.awaiting_approval", {"campaign_name": c.name, "campaign_id": str(c.id)},
                            dedupe_key=f"renewal.no_inventory:{rev.id}", entity=("campaign", c.id))
        return c
    item = (await db.execute(select(OrderItem).join(Order, Order.id == OrderItem.order_id).where(
        OrderItem.revision_id == rev.id, Order.payment_order_id == payment_order_id))).scalars().first()
    if item is not None and item.hold_id is not None:
        rev.hold_id = item.hold_id  # a reserva efetivamente paga (recapturada, se tardia)
    previous = await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None
    rev.status = RevisionStatus.APROVADA.value
    rev.decided_at = now
    rev.decision_reason = "renovação paga (somente período; sem reaprovação de conteúdo)"
    hold_ids = [str(h) for h in S.revision_hold_ids(previous)]
    if rev.hold_id is not None and str(rev.hold_id) not in hold_ids:
        hold_ids.append(str(rev.hold_id))
    rev.change_summary = {**(rev.change_summary or {}), "hold_ids": hold_ids,
                          "payment_order_id": str(payment_order_id), "paid_at": now.isoformat()}
    before = {"end_date": c.end_date.isoformat() if c.end_date else None,
              "contracted_price_cents": c.contracted_price_cents, "live_revision": previous.number if previous else None}
    if previous is not None:
        previous.status = RevisionStatus.SUBSTITUIDA.value
    c.live_revision_id = rev.id
    c.pending_revision_id = None
    c.end_date = date.fromisoformat(rev.spec["end_date"])
    c.contracted_price_cents = int(rev.price_cents)
    c.version += 1
    c.updated_at = now
    await audit(db, ctx, "campaign.renewed", "campaign", c.id, before=before,
                after={"end_date": c.end_date.isoformat(), "contracted_price_cents": c.contracted_price_cents,
                       "live_revision": rev.number, "payment_order_id": str(payment_order_id)}, org_id=c.org_id)
    defer_event(db, "campaign.updated", {"id": str(c.id), "status": c.status, "trigger": "renewed",
                                         "end_date": c.end_date.isoformat()}, org_id=c.org_id)
    open_orders = (await db.execute(select(Order.id).join(OrderItem, OrderItem.order_id == Order.id).where(
        OrderItem.revision_id == rev.id, Order.status == OrderStatus.AGUARDANDO_PAGAMENTO.value,
        Order.payment_order_id != payment_order_id))).scalars().all()
    for oid in open_orders:
        await CK.cancel_order(db, ctx, oid, reason="RENEWAL_PAID_BY_OTHER_ORDER", keep_campaign=c.id, check_scope=False)
    if rev.screen_ids:
        await db.flush()  # autoflush=False: nova data final/versão vigente no banco ANTES de regerar o manifesto
        await S._inventory_call("invalidate_screens", db, [uuid.UUID(s) for s in rev.screen_ids],
                                f"campaign_renewed:{c.id}")
    rec = await S._org_recipients(c.org_id, "payments.view")
    if rec:
        await S._notify(db, rec, "payment.approved", {"campaign_name": c.name, "campaign_id": str(c.id),
                                                       "amount_cents": int(rev.price_diff_cents)},
                        dedupe_key=f"payment.approved:{c.id}:{payment_order_id}", entity=("campaign", c.id),
                        org_id=c.org_id)
    return c


async def set_auto_renew(db: AsyncSession, ctx, campaign_id: uuid.UUID, value: bool) -> Campaign:
    c = await S.get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    if c.status in ST.TERMINAL:
        raise Conflict("CAMPAIGN_NOT_EDITABLE", "Campanha encerrada.", details={"status": c.status})
    if value and c.period_type not in S.RECURRING_PERIODS:
        raise DomainError("RENEWAL_NOT_RECURRING", "Renovação automática só para períodos mensal, semestral ou anual.",
                          status=400)
    if c.auto_renew != value:
        before = c.auto_renew
        c.auto_renew = value
        c.version += 1
        await audit(db, ctx, "campaign.auto_renew_set", "campaign", c.id, before={"auto_renew": before},
                    after={"auto_renew": value}, org_id=c.org_id)
    return c


async def generate_auto_renewals(db: AsyncSession, now: Optional[datetime] = None, *, ctx=None) -> dict:
    """Gera a PROPOSTA de renovação D-N (sem reserva) das campanhas com ``auto_renew``; idempotente."""
    ctx = ctx or S._sys_ctx("worker")
    now = now or utcnow()
    days = int(await get_setting(db, "campaign.renewal_notice_days"))
    today = now.date()
    rows = (await db.execute(select(Campaign.id).where(
        Campaign.auto_renew.is_(True), Campaign.status.in_(sorted(ST.OPERATIONAL)),
        Campaign.period_type.in_(S.RECURRING_PERIODS), Campaign.live_revision_id.is_not(None),
        Campaign.pending_revision_id.is_(None), Campaign.end_date.is_not(None),
        Campaign.end_date <= today + timedelta(days=days + 1), Campaign.end_date >= today - timedelta(days=1),
    ).order_by(Campaign.id))).scalars().all()
    created, skipped, failed = [], [], []
    for cid in rows:
        c = await db.get(Campaign, cid)
        end_utc = await S.campaign_end_utc(db, c)
        if end_utc is None or now > end_utc or end_utc - now > timedelta(days=days):
            skipped.append(str(cid))
            continue
        pos = mark(db)
        try:
            async with db.begin_nested():
                res = await renew(db, ctx, cid, periods=1, auto=True)
            (skipped if res.get("replayed") else created).append(str(cid))
        except (DomainError, LookupError) as exc:
            rollback_to(db, pos)
            code = getattr(exc, "code", type(exc).__name__)
            failed.append({"campaign_id": str(cid), "code": code})
            log.warning("auto_renew_failed", campaign_id=str(cid), code=code)
            await audit(db, ctx, "campaign.auto_renew_failed", "campaign", cid, after={"code": code},
                        org_id=c.org_id)
    return {"created": created, "skipped": skipped, "failed": failed}
