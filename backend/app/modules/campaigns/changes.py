"""
RD Mídia Indoor — Alterações pós-compra (ETAPA2 #397–#406, #698–#701; A6 Q20).

Alterar região/telas/datas/duração/frequência/criativo/texto de uma campanha já aprovada
(APROVADA, AGENDADA, SINCRONIZANDO, ATIVA, PAUSADA) cria uma REVISÃO (kind CHANGE):
- preço recalculado no servidor pela tabela ativa; diferença = novo total − preço contratado;
- diferença > 0 ⇒ pedido de alteração (cobrança) e só depois análise;
  diferença ≤ 0 ⇒ análise imediata (crédito a favor do cliente registrado para o financeiro);
- telas/padrão alterados ⇒ nova reserva (W-INV ``hold(..., replaces_hold_id=<vigente>)``),
  a antiga só é liberada quando a nova revisão for APROVADA (troca na mesma transação);
- a campanha vai para EM_ANALISE e a VERSÃO ANTERIOR CONTINUA VALENDO até a aprovação.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.core.settings_service import get_setting
from app.core.timeutils import isoformat_z, utcnow
from app.models.campaigns import (
    Campaign,
    CampaignCreative,
    CampaignRevision,
    CampaignStatus,
    Creative,
    CreativeStatus,
    RevisionKind,
    RevisionStatus,
)
from app.models.pricing import Order, OrderItem, OrderKind, OrderStatus
from app.modules.campaigns import checkout as CK
from app.modules.campaigns import service as S
from app.modules.campaigns import states as ST
from app.modules.campaigns import targeting as TG
from app.modules.pricing import engine as PE
from app.modules.pricing import service as PS

SCHEDULE_FIELDS = ("start_date", "end_date", "period_type", "duration_s", "freq_kind", "freq_value",
                   "specific_times", "min_interval_s", "days_mask", "time_bands", "priority", "exclusivity",
                   "allow_same_advertiser_consecutive")


async def request_change(db: AsyncSession, ctx, campaign_id: uuid.UUID, *, changes: dict,
                         idempotency_key: Optional[str] = None) -> dict:
    c = await S.get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    if c.status not in ST.OPERATIONAL or c.live_revision_id is None:
        raise Conflict("CAMPAIGN_NOT_CHANGEABLE", "Alterações pós-compra só valem para campanhas aprovadas.",
                       details={"status": c.status})
    if c.pending_revision_id is not None:
        pend = await db.get(CampaignRevision, c.pending_revision_id)
        if pend is not None and pend.status in (RevisionStatus.AGUARDANDO_PAGAMENTO.value,
                                                RevisionStatus.EM_ANALISE.value):
            raise Conflict("CHANGE_ALREADY_PENDING", "Já existe uma alteração pendente para esta campanha.")
    live = await db.get(CampaignRevision, c.live_revision_id)
    spec = json.loads(json.dumps(live.spec))
    changed: dict = {}
    for f in SCHEDULE_FIELDS:
        if f in changes and changes[f] is not None:
            v = changes[f]
            if isinstance(v, date):
                v = v.isoformat()
            if f == "time_bands":
                v = [list(b) for b in v]
            if spec.get(f) != v:
                spec[f] = v
                changed[f] = v
    if spec["period_type"] != "AVULSO" and ("start_date" in changed or "period_type" in changed):
        spec["end_date"] = PS.period_end(date.fromisoformat(spec["start_date"]), spec["period_type"]).isoformat()
    S.validate_schedule(start_date=date.fromisoformat(spec["start_date"]), end_date=date.fromisoformat(spec["end_date"]),
                        period_type=spec["period_type"], duration_s=spec["duration_s"], freq_kind=spec["freq_kind"],
                        freq_value=spec["freq_value"], specific_times=spec.get("specific_times"),
                        days_mask=spec["days_mask"], time_bands=spec["time_bands"], min_interval_s=spec["min_interval_s"])
    if "start_date" in changed and date.fromisoformat(spec["start_date"]) < S.today_local() and \
            c.status not in (CampaignStatus.ATIVA.value, CampaignStatus.PAUSADA.value, CampaignStatus.SINCRONIZANDO.value):
        raise DomainError("CAMPAIGN_START_IN_PAST", "A nova data de início já passou.", status=400)
    if changes.get("targets") is not None:
        specs = TG.validate_specs(TG.specs_from(changes["targets"]))
        await S._check_target_permissions(db, ctx, specs)
        new_t = [s.as_dict() for s in specs]
        if new_t != spec["targets"]:
            spec["targets"] = new_t
            changed["targets"] = new_t

    # criativos: substituições (nova versão), inclusões e remoções — ficam PENDENTES até aprovar
    links = await S.links_of(db, c.id)
    linked = {ln.creative_id: ln for ln in links}
    creative_ops = []
    for rep in changes.get("creative_replacements") or []:
        old_id, new_id = uuid.UUID(str(rep["old_creative_id"])), uuid.UUID(str(rep["new_creative_id"]))
        if old_id not in linked or not linked[old_id].is_active:
            raise DomainError("CREATIVE_NOT_IN_CAMPAIGN", "Criativo a substituir não está na versão vigente.", status=400)
        creative_ops.append(("replace", old_id, new_id))
    for nid in changes.get("add_creative_ids") or []:
        creative_ops.append(("add", None, uuid.UUID(str(nid))))
    for rid in changes.get("remove_creative_ids") or []:
        rid = uuid.UUID(str(rid))
        if rid not in linked or not linked[rid].is_active:
            raise DomainError("CREATIVE_NOT_IN_CAMPAIGN", "Criativo a remover não está na versão vigente.", status=400)
        creative_ops.append(("remove", rid, None))
    if not changed and not creative_ops:
        raise DomainError("CHANGE_EMPTY", "Nenhuma alteração informada.", status=400)
    now = utcnow()
    for op, old_id, new_id in creative_ops:
        if new_id is not None:
            cr = await db.get(Creative, new_id)
            if cr is None or cr.org_id != c.org_id:
                raise NotFound()
            if cr.status in (CreativeStatus.SUBSTITUIDO.value, CreativeStatus.ARQUIVADO.value,
                             CreativeStatus.REJEITADO.value):
                raise Conflict("CREATIVE_NOT_USABLE", "Criativo substituído, arquivado ou rejeitado.")
            if cr.duration_seconds != spec["duration_s"]:
                raise DomainError("CREATIVE_DURATION_MISMATCH", "A duração do criativo difere da duração contratada.",
                                  status=400, details={"creative": cr.code, "duration_s": spec["duration_s"]})
            if new_id in linked and linked[new_id].removed_at is None:
                raise Conflict("CREATIVE_ALREADY_LINKED", "Criativo já vinculado à campanha.")
            db.add(CampaignCreative(id=uuid.uuid4(), campaign_id=c.id, creative_id=new_id, order=0, is_active=False,
                                    pending=True, replaces_creative_id=old_id, added_at=now))
        elif op == "remove":
            linked[old_id].pending_removal = True
    await db.flush()
    after = await S.target_creatives(db, c)
    if not after:
        raise DomainError("CREATIVES_REQUIRED", "A campanha precisa manter ao menos um criativo.", status=400)
    bad = [x.code for x in after if x.duration_seconds != spec["duration_s"]]
    if bad:
        raise DomainError("CREATIVE_DURATION_MISMATCH", "Alteração de duração exige novas versões dos criativos.",
                          status=400, details={"creatives": bad, "duration_s": spec["duration_s"]})

    # telas e preço recalculados no servidor
    price_impacting = bool(set(changed) & (set(SCHEDULE_FIELDS) | {"targets"}))
    screens_ids = list(live.screen_ids or [])
    quote = live.quote
    new_total = int(c.contracted_price_cents)
    res = None
    if price_impacting:
        res = await S.eligible_resolution(db, c, specs=TG.specs_from(spec["targets"]), duration_s=spec["duration_s"],
                                          creatives=after, record=True, ctx=ctx)
        if not res.screens:
            raise Conflict("NO_SCREENS_AVAILABLE", "Nenhuma tela disponível para a nova segmentação.")
        screens_ids = [str(s) for s in res.screen_ids]
        req = PS.PriceQuoteRequest(
            org_id=c.org_id, duration_s=spec["duration_s"], freq_kind=spec["freq_kind"], freq_value=spec["freq_value"],
            start_date=date.fromisoformat(spec["start_date"]), end_date=date.fromisoformat(spec["end_date"]),
            period_type=spec["period_type"], screens=res.screens, days_mask=spec["days_mask"],
            time_bands=spec["time_bands"], specific_times=spec.get("specific_times") or [], priority=spec["priority"],
            exclusivity=spec["exclusivity"], fees=(), plan_id=None,
            with_seller=c.sales_mode == "COMMERCIAL" or c.seller_user_id is not None)
        quote = await PS.price_quote(db, ctx, req)
        PS.ensure_margin_allowed(quote)
        new_total = int(quote["total_cents"])
    diff = new_total - int(c.contracted_price_cents)
    rev = CampaignRevision(
        id=uuid.uuid4(), campaign_id=c.id, number=await S.next_revision_number(db, c.id), kind=RevisionKind.CHANGE.value,
        status=RevisionStatus.AGUARDANDO_PAGAMENTO.value if diff > 0 else RevisionStatus.EM_ANALISE.value,
        spec=spec, screen_ids=screens_ids, creative_ids=[str(x.id) for x in after],
        change_summary={"fields": sorted(changed), "creatives": [[op, str(a) if a else None, str(b) if b else None]
                                                                 for op, a, b in creative_ops],
                        "credit_due_cents": -diff if diff < 0 else 0, "previous_price_cents": c.contracted_price_cents},
        quote=quote, quote_hash=(quote or {}).get("quote_hash"), price_cents=new_total, price_diff_cents=diff,
        created_by=ctx.user_id, hold_id=live.hold_id,
    )
    pattern_changed = bool(set(changed) & set(SCHEDULE_FIELDS)) or screens_ids != list(live.screen_ids or [])
    live_holds = S.revision_hold_ids(live)
    if pattern_changed and len(live_holds) > 1:
        # o W-INV substitui UMA reserva por vez (replaces_hold_id); campanha renovada tem várias
        raise Conflict("CHANGE_AFTER_RENEWAL_UNSUPPORTED",
                       "Alteração de telas/período/programação de campanha renovada ainda não é suportada; "
                       "fale com a RD.", details={"holds": len(live_holds)})
    rev.change_summary = {**rev.change_summary, "needs_booking": pattern_changed,
                          "hold_ids": [] if pattern_changed else [str(h) for h in live_holds]}
    db.add(rev)
    await db.flush([rev])
    if pattern_changed and diff > 0:
        # com cobrança: reserva de 2 h (TTL padrão do W-INV) substituindo a vigente só no cálculo; convertida no
        # pagamento. A reserva vigente continua BOOKED até a aprovação (invariante i).
        hold_fn = CK._dep("hold", "app.modules.inventory.service")
        hold = await hold_fn(db, ctx, c.id, [uuid.UUID(s) for s in screens_ids], [S.pattern_from_spec(spec)],
                             f"change:{rev.id}", None, replaces_hold_id=live.hold_id)
        rev.hold_id = CK._attr(hold, "id")
    elif pattern_changed:
        # sem cobrança: NADA é reservado na submissão; hold + conversão acontecem na aprovação (uma transação)
        rev.hold_id = None
    c.pending_revision_id = rev.id
    await audit(db, ctx, "campaign.change_created", "campaign", c.id,
                before={"revision": live.number, "price_cents": c.contracted_price_cents},
                after={"revision": rev.number, "fields": sorted(changed), "price_cents": new_total, "diff_cents": diff},
                org_id=c.org_id)
    out = {"campaign_id": str(c.id), "revision": rev.number, "revision_id": str(rev.id), "status": rev.status,
           "changed_fields": sorted(changed), "previous_price_cents": int(rev.change_summary["previous_price_cents"]),
           "new_price_cents": new_total, "diff_cents": diff, "credit_due_cents": rev.change_summary["credit_due_cents"],
           "quote": PE.public_view(quote) if quote else None, "order": None}
    if diff > 0:
        if not idempotency_key or not CK.IDEMPOTENCY_RE.match(idempotency_key):
            raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "Envie o header Idempotency-Key.", status=400)
        out["order"] = await _change_order(db, ctx, c, rev, diff, quote, idempotency_key)
    else:
        await S._request_review_for_change(db, ctx, c, rev)
        mode, _ = await S.effective_approval(db, c.org_id)
        if mode == "AUTO":
            res = await S.approve(db, S._sys_ctx("system"), c.id, note="aprovação automática da alteração")
            if res.get("error"):
                # sem capacidade para a nova configuração: a alteração não é criada (a versão vigente segue intacta)
                err = res["error"]
                raise DomainError(err["code"], err["message"], status=409, details=err["details"])
    await db.flush()
    await db.refresh(c)
    out["campaign_status"] = c.status
    return out


async def _change_order(db: AsyncSession, ctx, c: Campaign, rev: CampaignRevision, diff: int, quote: dict,
                        idempotency_key: str) -> dict:
    payload = {"campaign_id": str(c.id), "revision": rev.number, "diff": diff}
    rh = CK.request_hash(payload)
    existing = (await db.execute(select(Order).where(Order.org_id == c.org_id, Order.idempotency_key == idempotency_key))
                ).scalar_one_or_none()
    if existing is not None:
        raise Conflict("IDEMPOTENCY_KEY_REUSED", "Esta Idempotency-Key já foi usada.")
    now = utcnow()
    expires_at = now + timedelta(hours=int(await get_setting(db, "inventory.reservation_hours")))
    if rev.hold_id is not None:
        from app.models.inventory import InventoryHold

        h = await db.get(InventoryHold, rev.hold_id)
        if h is not None and h.hold_expires_at is not None:
            expires_at = h.hold_expires_at  # a cobrança vence junto com a reserva (relógio do banco)
    order = Order(id=uuid.uuid4(), org_id=c.org_id, kind=OrderKind.CHANGE.value,
                  status=OrderStatus.AGUARDANDO_PAGAMENTO.value, idempotency_key=idempotency_key, request_hash=rh,
                  media_cents=diff, fees_cents=0, discount_cents=0, credit_applied_cents=0, total_cents=diff,
                  expires_at=expires_at, created_by=ctx.user_id)
    db.add(order)
    await db.flush([order])
    db.add(OrderItem(id=uuid.uuid4(), order_id=order.id, campaign_id=c.id, revision_id=rev.id, media_cents=diff,
                     fees_cents=0, discount_cents=0, amount_cents=diff, quote=quote, quote_hash=quote["quote_hash"],
                     hold_id=rev.hold_id))
    rev.order_id = order.id
    create_payment = CK._dep("create_payment_order", "app.modules.financial.service")
    spec = rev.spec
    payment = await create_payment(db, ctx, c.org_id, [{
        "kind": "MEDIA", "campaign_id": c.id, "description": f"Alteração da campanha {c.name} (rev. {rev.number})",
        "amount_cents": diff, "service_start": date.fromisoformat(spec["start_date"]),
        "service_end": date.fromisoformat(spec["end_date"])}], diff, expires_at,
        {"type": "campaign_checkout", "id": order.id})
    order.payment_order_id = CK._attr(payment, "id")
    resp = {"order_id": str(order.id), "status": order.status, "total_cents": diff,
            "expires_at": isoformat_z(expires_at), "payment": CK.payment_public(payment)}
    order.response = json.loads(json.dumps(resp, default=str))
    await audit(db, ctx, "order.created", "order", order.id, after={"kind": "CHANGE", "total_cents": diff,
                                                                 "campaign_id": str(c.id)}, org_id=c.org_id)
    return resp
