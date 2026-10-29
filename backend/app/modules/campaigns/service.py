"""
RD Mídia Indoor — Serviço de campanhas (W-CAMP, AÇÃO 002).

INTERFACES PÚBLICAS (plano §4 — nomes estáveis consumidos por W-INV/W-FIN/W-PLAY/W-BIZ):

- ``on_order_paid(db, order_id, payment_order_id)``           ← W-FIN (reference campaign_checkout)
- ``on_payment_confirmed(db, campaign_id, payment_order_id)``  ← (por item do pedido)
- ``on_order_expired(db, order_id)``                           ← W-FIN/W-INV (cobrança/reserva expirada)
- ``resolve_target_screens(db, campaign) -> list[UUID]``       ← W-INV
- ``evaluate_conflict / are_competitors / competitor_pairs``    ← W-INV (reexportados do motor)
- ``price_quote(db, ctx, request)``                            ← preço SEMPRE no servidor
- ``mark_scheduled / mark_syncing / mark_active_from_ack / finalize_expired`` ← W-INV / W-PLAY / task
- ``playable_campaign_specs(db, screen_id=None)``             ← W-INV (versão APROVADA vigente)
- ``attributed_revenue_by_screen(db, campaign_id)``            ← W-FIN (modelo B de comissão)
- ``playing_state(db, campaign)``                              → REPRODUZINDO derivado de PoP recente

Regras: transições com guarda ``UPDATE … WHERE status = :esperado`` + histórico +
auditoria + evento ``campaign.updated`` (após o commit). Pagamento NUNCA aprova quando
o modo é MANUAL (padrão): vai para EM_ANALISE (ou AGUARDANDO_MIDIA).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.errors import Conflict, DomainError, NotFound
from app.core.settings_service import get_setting
from app.core.timeutils import end_of_local_day_utc, local_date, utcnow
from app.models.campaigns import (
    ApprovalMode,
    ApprovalSource,
    Campaign,
    CampaignCreative,
    CampaignRevision,
    CampaignSegment,
    CampaignStatus,
    CampaignStatusHistory,
    CampaignTarget,
    Creative,
    CreativeStatus,
    OrganizationApprovalPolicy,
    RevisionKind,
    RevisionStatus,
    TechnicalStatus,
)
from app.modules.campaigns import config as _config  # noqa: F401  (registra settings do domínio)
from app.modules.campaigns import creatives as creative_svc
from app.modules.campaigns import states as ST
from app.modules.campaigns import targeting as TG
from app.modules.campaigns.notify import defer_event
from app.modules.conflicts.service import (  # noqa: F401  (reexport — plano §4)
    are_competitors,
    competitor_pairs,
    evaluate_campaign_establishments,
    evaluate_conflict,
)
from app.modules.pricing.service import PriceQuoteRequest, price_quote  # noqa: F401  (reexport)

log = structlog.get_logger("rd.campaigns")

PLAYABLE_STATUSES = ST.PLAYABLE
DEFAULT_TZ = "America/Sao_Paulo"

_ACTIONS = {
    "checkout": "campaign.checkout",
    "payment_expired": "campaign.payment_expired",
    "order_cancelled": "campaign.order_cancelled",
    "payment_confirmed": "campaign.payment_confirmed",
    "media_submitted": "campaign.media_submitted",
    "approve": "campaign.approved",
    "reject": "campaign.rejected",
    "resubmit": "campaign.resubmitted",
    "change_requested": "campaign.change_requested",
    "change_rejected": "campaign.change_rejected",
    "mark_scheduled": "campaign.scheduled",
    "mark_syncing": "campaign.syncing",
    "ack_active": "campaign.activated",
    "pause": "campaign.paused",
    "kill": "campaign.killed",
    "resume": "campaign.resumed",
    "expired": "campaign.finalized",
    "cancel": "campaign.cancelled",
}


# ─── utilidades ──────────────────────────────────────────────────────────────

_AUDIT_ORIGINS = ("worker", "player", "system", "cli")


def _sys_ctx(origin: str = "system") -> AuthContext:
    """Contexto de sistema; ``origin`` restrito aos valores aceitos por ``audit_logs`` (CHECK)."""
    return AuthContext.system(origin if origin in _AUDIT_ORIGINS else "system")


def today_local(tz: str = DEFAULT_TZ) -> date:
    return local_date(utcnow(), tz)


async def _optional(module: str, name: str):
    try:
        mod = __import__(module, fromlist=[name])
    except Exception:  # pragma: no cover - domínio ainda ausente
        return None
    return getattr(mod, name, None)


async def _notify(db: AsyncSession, recipients, event_type: str, data: dict, **kw) -> None:
    """Notificação (W-BIZ) em SAVEPOINT: falha de notificação nunca desfaz a operação de negócio."""
    fn = await _optional("app.modules.notifications.service", "notify")
    if fn is None:
        return
    try:
        async with db.begin_nested():
            await fn(db, recipients, event_type, data, **kw)
    except Exception as exc:  # pragma: no cover - registrado; não bloqueia
        log.error("notify_failed", event_type=event_type, error_type=type(exc).__name__)


async def _org_recipients(org_id, perm: str = "campaigns.view"):
    cls = await _optional("app.modules.notifications.service", "OrgRecipients")
    return cls(org_id, perm) if cls else None


async def _staff_recipients(perm: str = "campaigns.approve"):
    cls = await _optional("app.modules.notifications.service", "StaffRecipients")
    return cls(perm) if cls else None


async def fresh(db: AsyncSession, obj):
    """Garante atributos carregados após flushes (``updated_at`` com onupdate expira) — evita I/O implícito."""
    from sqlalchemy import inspect as sa_inspect

    await db.flush()
    if sa_inspect(obj).expired_attributes:
        await db.refresh(obj)
    return obj


async def get_campaign(db: AsyncSession, ctx, campaign_id: uuid.UUID, *, perm: str = "campaigns.view",
                       for_update: bool = False) -> Campaign:
    q = select(Campaign).where(Campaign.id == campaign_id)
    if for_update:
        q = q.with_for_update().execution_options(populate_existing=True)
    c = (await db.execute(q)).scalar_one_or_none()
    if c is None:
        raise NotFound()
    if ctx is not None and getattr(ctx, "kind", "user") == "user":
        ctx.ensure(perm, org_id=c.org_id)
    return c


async def _transition(db: AsyncSession, ctx, c: Campaign, to: str, trigger: str, *, reason: Optional[str] = None,
                      values: Optional[dict] = None, revision_id: Optional[uuid.UUID] = None) -> Campaign:
    frm = c.status
    if not ST.is_allowed(trigger, frm, to):
        raise Conflict("CAMPAIGN_INVALID_TRANSITION", "Transição de status não permitida para esta campanha.",
                       details={"from": frm, "to": to, "trigger": trigger})
    await db.flush()
    now = utcnow()
    vals = {"status": to, "updated_at": now, **(values or {})}
    res = await db.execute(
        update(Campaign).where(Campaign.id == c.id, Campaign.status == frm).values(**vals)
        .returning(Campaign.id).execution_options(synchronize_session=False)
    )
    if res.first() is None:
        raise Conflict("CAMPAIGN_STATE_CHANGED", "A campanha mudou de status em outra operação. Recarregue.",
                       details={"expected": frm})
    await db.refresh(c)
    db.add(CampaignStatusHistory(
        id=uuid.uuid4(), campaign_id=c.id, from_status=frm, to_status=to, trigger=trigger,
        actor_user_id=getattr(ctx, "user_id", None), real_user_id=getattr(ctx, "real_user_id", None),
        origin=getattr(ctx, "origin", "api") if getattr(ctx, "kind", "user") != "user" else "api",
        reason=reason, revision_id=revision_id, created_at=now,
    ))
    await audit(db, ctx, _ACTIONS.get(trigger, "campaign.status_changed"), "campaign", c.id,
                before={"status": frm}, after={"status": to, **(values or {})}, reason=reason, org_id=c.org_id)
    defer_event(db, "campaign.updated", {"id": str(c.id), "status": to, "previous_status": frm, "trigger": trigger},
                org_id=c.org_id)
    return c


async def effective_approval(db: AsyncSession, org_id: uuid.UUID) -> tuple[str, str]:
    """(modo, origem): exceção da organização > configuração global (padrão MANUAL)."""
    pol = await db.get(OrganizationApprovalPolicy, org_id)
    if pol is not None:
        if pol.mode == ApprovalMode.AUTO.value:
            return ApprovalMode.AUTO.value, ApprovalSource.AUTO_TRUSTED_CLIENT.value
        return ApprovalMode.MANUAL.value, ApprovalSource.MANUAL.value
    g = await get_setting(db, "campaign.approval_mode")
    if g == ApprovalMode.AUTO.value:
        return ApprovalMode.AUTO.value, ApprovalSource.AUTO_GLOBAL.value
    return ApprovalMode.MANUAL.value, ApprovalSource.MANUAL.value


async def review_due(db: AsyncSession, now: datetime) -> datetime:
    return now + timedelta(hours=int(await get_setting(db, "campaign.review_sla_hours")))


# ─── especificação / revisões ────────────────────────────────────────────────

async def targets_of(db: AsyncSession, campaign_id: uuid.UUID) -> list[CampaignTarget]:
    return list((await db.execute(select(CampaignTarget).where(CampaignTarget.campaign_id == campaign_id)
                                  .order_by(CampaignTarget.target_type, CampaignTarget.target_value,
                                            CampaignTarget.is_exclude))).scalars())


async def segment_ids_of(db: AsyncSession, campaign_id: uuid.UUID) -> list[uuid.UUID]:
    return list((await db.execute(select(CampaignSegment.segment_id).where(CampaignSegment.campaign_id == campaign_id)
                                  .order_by(CampaignSegment.segment_id))).scalars())


async def links_of(db: AsyncSession, campaign_id: uuid.UUID, *, include_removed: bool = False) -> list[CampaignCreative]:
    q = select(CampaignCreative).where(CampaignCreative.campaign_id == campaign_id)
    if not include_removed:
        q = q.where(CampaignCreative.removed_at.is_(None))
    return list((await db.execute(q.order_by(CampaignCreative.order, CampaignCreative.added_at))).scalars())


async def creatives_of(db: AsyncSession, campaign_id: uuid.UUID, *, active_only: bool = False,
                       include_pending: bool = True) -> list[Creative]:
    q = (select(Creative).join(CampaignCreative, CampaignCreative.creative_id == Creative.id)
         .where(CampaignCreative.campaign_id == campaign_id, CampaignCreative.removed_at.is_(None)))
    if active_only:
        q = q.where(CampaignCreative.is_active.is_(True))
    if not include_pending:
        q = q.where(CampaignCreative.pending.is_(False))
    return list((await db.execute(q.order_by(CampaignCreative.order, Creative.code))).scalars())


def spec_of(c: Campaign, targets: Iterable[Any], segment_ids: Iterable[uuid.UUID]) -> dict:
    return {
        "name": c.name,
        "period_type": c.period_type,
        "start_date": c.start_date.isoformat() if c.start_date else None,
        "end_date": c.end_date.isoformat() if c.end_date else None,
        "duration_s": c.duration_s,
        "freq_kind": c.freq_kind,
        "freq_value": c.freq_value,
        "specific_times": list(c.specific_times or []),
        "min_interval_s": c.min_interval_s,
        "days_mask": c.days_mask,
        "time_bands": [list(b) for b in (c.time_bands or [[0, 1440]])],
        "priority": c.priority,
        "exclusivity": c.exclusivity,
        "allow_same_advertiser_consecutive": c.allow_same_advertiser_consecutive,
        "request_art_service": c.request_art_service,
        "plan_id": str(c.plan_id) if c.plan_id else None,
        "product_id": str(c.product_id) if c.product_id else None,
        "brand_id": str(c.brand_id) if c.brand_id else None,
        "segment_ids": sorted(str(s) for s in segment_ids),
        "targets": [t.as_dict() for t in TG.specs_from(targets)],
    }


def pattern_from_spec(spec: dict) -> dict:
    """Padrão de contratação para o W-INV (chaves A6 §10.2; contrato com ``PatternSpec.parse``)."""
    return {
        "date_from": spec["start_date"],
        "date_to": spec["end_date"],
        "days_mask": spec["days_mask"],
        "holiday_mode": "INCLUDE",
        "time_bands": spec["time_bands"],
        "duration_units": spec["duration_s"] // 30,
        "freq_kind": spec["freq_kind"],
        "freq_value": spec["freq_value"],
        "specific_times": spec.get("specific_times") or [],
        "min_interval_seconds": spec["min_interval_s"],
        "priority": spec["priority"],
        "allow_same_advertiser_consecutive": spec["allow_same_advertiser_consecutive"],
        "exclusivity": spec["exclusivity"],
    }


def apply_spec(c: Campaign, spec: dict) -> None:
    c.period_type = spec["period_type"]
    c.start_date = date.fromisoformat(spec["start_date"]) if spec.get("start_date") else None
    c.end_date = date.fromisoformat(spec["end_date"]) if spec.get("end_date") else None
    c.duration_s = spec["duration_s"]
    c.freq_kind = spec["freq_kind"]
    c.freq_value = spec["freq_value"]
    c.specific_times = list(spec.get("specific_times") or []) or None
    c.min_interval_s = spec["min_interval_s"]
    c.days_mask = spec["days_mask"]
    c.time_bands = [list(b) for b in spec["time_bands"]]
    c.priority = spec["priority"]
    c.exclusivity = spec["exclusivity"]
    c.allow_same_advertiser_consecutive = spec["allow_same_advertiser_consecutive"]
    c.request_art_service = spec.get("request_art_service", False)


async def replace_targets(db: AsyncSession, campaign_id: uuid.UUID, specs: list[TG.TargetSpec]) -> None:
    await db.execute(delete(CampaignTarget).where(CampaignTarget.campaign_id == campaign_id))
    for s in specs:
        db.add(CampaignTarget(id=uuid.uuid4(), campaign_id=campaign_id, target_type=s.target_type,
                              target_value=s.value, is_exclude=s.exclude))


async def next_revision_number(db: AsyncSession, campaign_id: uuid.UUID) -> int:
    n = (await db.execute(select(func.max(CampaignRevision.number)).where(CampaignRevision.campaign_id == campaign_id)
                          )).scalar()
    return int(n or 0) + 1


# ─── validação da especificação ──────────────────────────────────────────────

def validate_schedule(*, start_date: Optional[date], end_date: Optional[date], period_type: str, duration_s: int,
                      freq_kind: str, freq_value: int, specific_times: Optional[list], days_mask: int,
                      time_bands: list, min_interval_s: int) -> None:
    def bad(code: str, msg: str, **d) -> DomainError:
        return DomainError(code, msg, status=400, details=d)

    if duration_s not in (30, 60, 120):
        raise bad("DURATION_NOT_ALLOWED", "Duração permitida: 30, 60 ou 120 segundos.", duration_s=duration_s)
    if period_type not in ("AVULSO", "MENSAL", "SEMESTRAL", "ANUAL"):
        raise bad("PERIOD_NOT_ALLOWED", "Período permitido: avulso, mensal, semestral ou anual (sem trimestral).",
                  period_type=period_type)
    if freq_kind not in ("PER_HOUR", "PER_DAY", "SPECIFIC_TIME"):
        raise bad("FREQUENCY_INVALID", "Frequência inválida.", freq_kind=freq_kind)
    if not (1 <= freq_value <= 1440):
        raise bad("FREQUENCY_INVALID", "Frequência deve ser positiva.")
    if not (1 <= days_mask <= 127):
        raise bad("DAYS_INVALID", "Selecione ao menos um dia da semana.")
    if not (0 <= min_interval_s <= 86400):
        raise bad("MIN_INTERVAL_INVALID", "Intervalo mínimo fora de 0..86400 s.")
    bands = []
    for b in time_bands or []:
        if not isinstance(b, (list, tuple)) or len(b) != 2:
            raise bad("TIME_BAND_INVALID", "Faixa horária inválida.")
        s, e = int(b[0]), int(b[1])
        if not (0 <= s < e <= 1440) or s % 60 or e % 60:
            raise bad("TIME_BAND_INVALID", "Faixas horárias devem estar alinhadas à hora (00:00–24:00).", band=[s, e])
        bands.append((s, e))
    if not bands:
        raise bad("TIME_BAND_INVALID", "Informe ao menos uma faixa horária.")
    bands.sort()
    for (s1, e1), (s2, e2) in zip(bands, bands[1:]):
        if s2 < e1:
            raise bad("TIME_BAND_INVALID", "Faixas horárias sobrepostas.")
    if freq_kind == "SPECIFIC_TIME":
        times = list(specific_times or [])
        if not times or len(set(times)) != len(times):
            raise bad("SPECIFIC_TIME_INVALID", "Informe horários específicos sem repetição.")
        from app.modules.pricing.engine import PricingError, parse_hhmm

        for t in times:
            try:
                m = parse_hhmm(t)
            except PricingError as exc:
                raise bad(exc.code, exc.message, value=t) from None
            if not any(s <= m < e for s, e in bands):
                raise bad("SPECIFIC_TIME_OUTSIDE_BAND", "Horário específico fora das faixas contratadas.", time=t)
        if freq_value != len(times):
            raise bad("FREQUENCY_INVALID", "freq_value deve ser igual à quantidade de horários específicos.")
    if start_date and end_date and end_date < start_date:
        raise bad("DATES_INVALID", "Data final anterior à inicial.")


# ─── criação / edição ────────────────────────────────────────────────────────

EDITABLE_FIELDS = {
    "name", "description", "period_type", "start_date", "end_date", "period_days", "duration_s", "freq_kind",
    "freq_value", "specific_times", "min_interval_s", "days_mask", "time_bands", "priority", "exclusivity",
    "allow_same_advertiser_consecutive", "request_art_service", "plan_id", "product_id", "brand_id",
    "sales_mode", "seller_user_id", "auto_renew",
}
PRICE_FIELDS = EDITABLE_FIELDS - {"name", "description", "product_id", "brand_id", "auto_renew"}
RECURRING_PERIODS = ("MENSAL", "SEMESTRAL", "ANUAL")


async def _apply_plan_defaults(db: AsyncSession, c: Campaign, data: dict) -> None:
    from app.modules.pricing.service import usable_plan

    plan = await usable_plan(db, data["plan_id"], c.org_id)
    if plan.max_campaigns is not None and c.plan_id != plan.id:
        used = (await db.execute(select(func.count()).select_from(Campaign).where(
            Campaign.org_id == c.org_id, Campaign.plan_id == plan.id, Campaign.id != c.id,
            Campaign.status.notin_(["CANCELADA", "FINALIZADA"])))).scalar_one()
        if used >= plan.max_campaigns:
            raise DomainError("PLAN_LIMIT_CAMPAIGNS", "Limite de campanhas do plano atingido.", status=409,
                              details={"max_campaigns": plan.max_campaigns})
    for f in ("period_type", "duration_s", "freq_kind", "freq_value", "min_interval_s", "days_mask", "priority",
              "exclusivity"):
        if f not in data or data[f] is None:
            data[f] = getattr(plan, f)
    if "time_bands" not in data or data["time_bands"] is None:
        data["time_bands"] = plan.time_bands
    if "specific_times" not in data or data["specific_times"] is None:
        data["specific_times"] = plan.specific_times
    if plan.period_type == "AVULSO" and not data.get("period_days"):
        data["period_days"] = plan.period_days


async def _apply_fields(db: AsyncSession, ctx, c: Campaign, data: dict) -> dict:
    from app.modules.pricing.service import period_end

    data = {k: v for k, v in data.items() if k in EDITABLE_FIELDS}
    if data.get("plan_id"):
        await _apply_plan_defaults(db, c, data)
    if "product_id" in data or "brand_id" in data:
        await creative_svc.validate_classification(db, data.get("product_id", c.product_id),
                                                   data.get("brand_id", c.brand_id))
    if data.get("sales_mode") == "COMMERCIAL" and not ctx.can("campaigns.manage", global_only=True) \
            and not ctx.has_any("crm.manage"):
        raise DomainError("SALES_MODE_FORBIDDEN", "Somente o comercial/RD define a venda assistida.", status=403)
    if data.get("seller_user_id") and not (ctx.can("campaigns.manage", global_only=True) or ctx.has_any("crm.manage")):
        raise DomainError("SALES_MODE_FORBIDDEN", "Somente o comercial/RD define o vendedor.", status=403)
    changed = {}
    for k, v in data.items():
        if k == "period_days":
            continue
        if getattr(c, k) != v:
            changed[k] = v
            setattr(c, k, v)
    if "auto_renew" in data:
        if data["auto_renew"] and c.period_type not in RECURRING_PERIODS:
            raise DomainError("RENEWAL_NOT_RECURRING",
                              "Renovação automática só para períodos mensal, semestral ou anual.", status=400)
    elif "period_type" in changed:
        # padrão: ligado para planos mensais (ETAPA2 #509); desligado nos demais
        c.auto_renew = c.period_type == "MENSAL"
    if c.auto_renew and c.period_type not in RECURRING_PERIODS:
        c.auto_renew = False
    if c.period_type != "AVULSO" and c.start_date:
        c.end_date = period_end(c.start_date, c.period_type)
    elif c.period_type == "AVULSO" and data.get("period_days") and c.start_date:
        c.end_date = period_end(c.start_date, "AVULSO", int(data["period_days"]))
    validate_schedule(start_date=c.start_date, end_date=c.end_date, period_type=c.period_type,
                      duration_s=c.duration_s, freq_kind=c.freq_kind, freq_value=c.freq_value,
                      specific_times=c.specific_times, days_mask=c.days_mask, time_bands=c.time_bands,
                      min_interval_s=c.min_interval_s)
    return changed


async def create_campaign(db: AsyncSession, ctx, *, org_id: uuid.UUID, data: dict, targets: Optional[list] = None,
                          segment_ids: Optional[list] = None) -> Campaign:
    ctx.ensure("campaigns.manage", org_id=org_id)
    c = Campaign(id=uuid.uuid4(), org_id=org_id, name=str(data.get("name") or "").strip(),
                 status=CampaignStatus.RASCUNHO.value, created_by=ctx.user_id, updated_by=ctx.user_id, version=1,
                 time_bands=[[0, 1440]], days_mask=127, duration_s=30, freq_kind="PER_HOUR", freq_value=1,
                 min_interval_s=0, priority="STANDARD", exclusivity="NONE", period_type="AVULSO",
                 sales_mode="SELF_SERVICE", allow_same_advertiser_consecutive=False, request_art_service=False,
                 auto_renew=False,
                 contracted_price_cents=0, rejection_count=0, requires_new_payment=False)
    if len(c.name) < 3:
        raise DomainError("NAME_INVALID", "Nome deve ter pelo menos 3 caracteres.", status=400)
    db.add(c)
    await _apply_fields(db, ctx, c, data)
    await db.flush()
    if targets:
        specs = TG.validate_specs(TG.specs_from(targets))
        await _check_target_permissions(db, ctx, specs)
        await replace_targets(db, c.id, specs)
    if segment_ids:
        await set_campaign_segments(db, c.id, segment_ids)
    await audit(db, ctx, "campaign.created", "campaign", c.id,
                after={"name": c.name, "status": c.status, "org_id": str(org_id)}, org_id=org_id)
    defer_event(db, "campaign.updated", {"id": str(c.id), "status": c.status, "trigger": "created"}, org_id=org_id)
    return c


def _ensure_editable(c: Campaign, fields: Iterable[str]) -> None:
    fields = set(fields)
    if c.status == CampaignStatus.RASCUNHO.value:
        return
    if c.status == CampaignStatus.REJEITADA.value:
        if c.requires_new_payment or not (fields & PRICE_FIELDS):
            return
        raise Conflict("CAMPAIGN_PRICE_FIELDS_LOCKED",
                       "Campanha paga: corrija criativos/textos e reenvie; mudanças de preço exigem pedido de alteração.",
                       details={"fields": sorted(fields & PRICE_FIELDS)})
    raise Conflict("CAMPAIGN_NOT_EDITABLE",
                   "Campanha não editável neste status. Após a aprovação use o pedido de alteração.",
                   details={"status": c.status})


async def duplicate_campaign(db: AsyncSession, ctx, campaign_id: uuid.UUID, *, name: Optional[str] = None) -> Campaign:
    """Nova campanha em RASCUNHO com a mesma especificação/segmentação/criativos utilizáveis
    (ETAPA2 #773 — duplicar). Preço/pagamento/aprovação NÃO são copiados."""
    src = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage")
    data = {f: getattr(src, f) for f in ("description", "period_type", "duration_s", "freq_kind", "freq_value",
                                          "specific_times", "min_interval_s", "days_mask", "time_bands", "priority",
                                          "exclusivity", "allow_same_advertiser_consecutive", "request_art_service",
                                          "product_id", "brand_id")}
    data["name"] = (name or f"{src.name} (cópia)")[:255]
    c = await create_campaign(db, ctx, org_id=src.org_id, data=data,
                              targets=[t.as_dict() for t in TG.specs_from(await targets_of(db, src.id))] or None,
                              segment_ids=await segment_ids_of(db, src.id))
    for i, cr in enumerate(await creatives_of(db, src.id, active_only=True)):
        if cr.status not in (CreativeStatus.SUBSTITUIDO.value, CreativeStatus.ARQUIVADO.value,
                             CreativeStatus.REJEITADO.value):
            db.add(CampaignCreative(id=uuid.uuid4(), campaign_id=c.id, creative_id=cr.id, order=i, is_active=True,
                                    pending=False))
    await audit(db, ctx, "campaign.duplicated", "campaign", c.id, after={"from": str(src.id)}, org_id=c.org_id)
    await db.flush()
    return c


async def update_campaign(db: AsyncSession, ctx, campaign_id: uuid.UUID, data: dict,
                          expected_version: Optional[int]) -> Campaign:
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    if expected_version is not None and expected_version != c.version:
        raise Conflict("CAMPAIGN_VERSION_CONFLICT", "A campanha foi alterada por outra pessoa. Recarregue.",
                       details={"current_version": c.version})
    _ensure_editable(c, data.keys())
    before = {k: getattr(c, k, None) for k in data if k in EDITABLE_FIELDS and k != "period_days"}
    changed = await _apply_fields(db, ctx, c, data)
    if changed:
        c.version += 1
        c.updated_by = ctx.user_id
        await audit(db, ctx, "campaign.updated", "campaign", c.id, before=before, after=changed, org_id=c.org_id)
        defer_event(db, "campaign.updated", {"id": str(c.id), "status": c.status, "trigger": "edited"},
                    org_id=c.org_id)
    return c


async def _check_target_permissions(db: AsyncSession, ctx, specs: list[TG.TargetSpec]) -> None:
    """ESTABLISHMENT (incluir/excluir estabelecimento específico) só para a RD ou quando a revelação de
    nomes estiver liberada (ETAPA2 #142). CATEGORY deve existir."""
    from app.models.establishments import Establishment
    from app.models.taxonomy import TaxonomyNode

    est = [uuid.UUID(s.value) for s in specs if s.target_type == "ESTABLISHMENT"]
    if est:
        reveal = bool(await get_setting(db, "targeting.reveal_establishment_names"))
        staff = ctx.can("campaigns.manage", global_only=True)
        if not (reveal or staff):
            raise DomainError("TARGET_ESTABLISHMENT_NOT_ALLOWED",
                              "A escolha de estabelecimentos específicos não está disponível; use região/categoria.",
                              status=400)
        found = set((await db.execute(select(Establishment.id).where(Establishment.id.in_(est),
                                                                     Establishment.deleted_at.is_(None)))).scalars())
        if set(est) - found:
            raise DomainError("TARGET_INVALID", "Estabelecimento inexistente.", status=400)
    cats = [uuid.UUID(s.value) for s in specs if s.target_type == "CATEGORY"]
    if cats:
        found = set((await db.execute(select(TaxonomyNode.id).where(TaxonomyNode.id.in_(cats)))).scalars())
        if set(cats) - found:
            raise DomainError("TARGET_INVALID", "Categoria inexistente.", status=400)


async def set_targets(db: AsyncSession, ctx, campaign_id: uuid.UUID, targets: list) -> list[CampaignTarget]:
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    _ensure_editable(c, {"targets"} | PRICE_FIELDS)
    specs = TG.validate_specs(TG.specs_from(targets))
    await _check_target_permissions(db, ctx, specs)
    before = [t.as_dict() for t in TG.specs_from(await targets_of(db, c.id))]
    await replace_targets(db, c.id, specs)
    c.version += 1
    c.updated_by = ctx.user_id
    await audit(db, ctx, "campaign.targets_updated", "campaign", c.id, before={"targets": before},
                after={"targets": [s.as_dict() for s in specs]}, org_id=c.org_id)
    await db.flush()
    return await targets_of(db, c.id)


async def set_campaign_segments(db: AsyncSession, campaign_id: uuid.UUID, segment_ids: Iterable) -> list[uuid.UUID]:
    from app.models.taxonomy import Segment

    ids = sorted({uuid.UUID(str(s)) for s in segment_ids}, key=str)
    if ids:
        found = set((await db.execute(select(Segment.id).where(Segment.id.in_(ids), Segment.is_active.is_(True))
                                      )).scalars())
        if set(ids) - found:
            raise DomainError("SEGMENT_NOT_FOUND", "Segmento inexistente ou inativo.", status=400)
    await db.execute(delete(CampaignSegment).where(CampaignSegment.campaign_id == campaign_id))
    for sid in ids:
        db.add(CampaignSegment(campaign_id=campaign_id, segment_id=sid))
    return ids


async def update_segments(db: AsyncSession, ctx, campaign_id: uuid.UUID, segment_ids: list) -> list[uuid.UUID]:
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    _ensure_editable(c, {"segments"})
    before = [str(s) for s in await segment_ids_of(db, c.id)]
    ids = await set_campaign_segments(db, c.id, segment_ids)
    c.version += 1
    await audit(db, ctx, "campaign.segments_updated", "campaign", c.id, before={"segment_ids": before},
                after={"segment_ids": [str(s) for s in ids]}, org_id=c.org_id)
    return ids


# ─── criativos da campanha ───────────────────────────────────────────────────

async def _plan_creative_limit(db: AsyncSession, c: Campaign, count: int) -> None:
    if c.plan_id is None:
        return
    from app.models.pricing import Plan

    p = await db.get(Plan, c.plan_id)
    if p is not None and p.max_creatives is not None and count > p.max_creatives:
        raise DomainError("PLAN_LIMIT_CREATIVES", "Limite de criativos do plano atingido.", status=409,
                          details={"max_creatives": p.max_creatives})


async def add_creative(db: AsyncSession, ctx, campaign_id: uuid.UUID, creative_id: uuid.UUID,
                       order: int = 0) -> CampaignCreative:
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    if c.status not in (CampaignStatus.RASCUNHO.value, CampaignStatus.AGUARDANDO_MIDIA.value,
                        CampaignStatus.REJEITADA.value):
        raise Conflict("CAMPAIGN_NOT_EDITABLE",
                       "Inclusão de criativo após a aprovação deve ser feita por pedido de alteração.",
                       details={"status": c.status})
    cr = await creative_svc.get_creative(db, ctx, creative_id, perm="creatives.view")
    if cr.org_id != c.org_id:
        raise NotFound()
    if cr.status in (CreativeStatus.SUBSTITUIDO.value, CreativeStatus.ARQUIVADO.value, CreativeStatus.REJEITADO.value):
        raise Conflict("CREATIVE_NOT_USABLE", "Criativo substituído, arquivado ou rejeitado.")
    if cr.duration_seconds != c.duration_s:
        raise DomainError("CREATIVE_DURATION_MISMATCH", "A duração do criativo difere da duração contratada.",
                          status=400, details={"creative_s": cr.duration_seconds, "campaign_s": c.duration_s})
    existing = (await db.execute(select(CampaignCreative).where(CampaignCreative.campaign_id == c.id,
                                                                CampaignCreative.creative_id == cr.id))).scalar_one_or_none()
    if existing is not None and existing.removed_at is None:
        return existing
    count = len(await links_of(db, c.id)) + 1
    await _plan_creative_limit(db, c, count)
    if existing is not None:
        existing.removed_at = None
        existing.is_active = True
        existing.pending = False
        existing.order = order
        link = existing
    else:
        link = CampaignCreative(id=uuid.uuid4(), campaign_id=c.id, creative_id=cr.id, order=order, is_active=True,
                                pending=False)
        db.add(link)
    c.version += 1
    await audit(db, ctx, "campaign.creative_added", "campaign", c.id, after={"creative": cr.code}, org_id=c.org_id)
    await db.flush()
    return link


async def remove_creative(db: AsyncSession, ctx, campaign_id: uuid.UUID, creative_id: uuid.UUID) -> None:
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    if c.status not in (CampaignStatus.RASCUNHO.value, CampaignStatus.AGUARDANDO_MIDIA.value,
                        CampaignStatus.REJEITADA.value):
        raise Conflict("CAMPAIGN_NOT_EDITABLE", "Remoção de criativo após a aprovação exige pedido de alteração.")
    link = (await db.execute(select(CampaignCreative).where(CampaignCreative.campaign_id == c.id,
                                                            CampaignCreative.creative_id == creative_id,
                                                            CampaignCreative.removed_at.is_(None)))).scalar_one_or_none()
    if link is None:
        raise NotFound()
    link.removed_at = utcnow()
    link.is_active = False
    c.version += 1
    await audit(db, ctx, "campaign.creative_removed", "campaign", c.id, after={"creative_id": str(creative_id)},
                org_id=c.org_id)


async def target_creatives(db: AsyncSession, c: Campaign) -> list[Creative]:
    """Criativos que valerão após a aprovação: vínculos vigentes + pendentes, menos os
    marcados para remoção e os substituídos por uma nova versão pendente."""
    links = await links_of(db, c.id)
    replaced = {ln.replaces_creative_id for ln in links if ln.pending and ln.replaces_creative_id}
    ids = [ln.creative_id for ln in links if not ln.pending_removal and ln.creative_id not in replaced]
    if not ids:
        return []
    by_id = {x.id: x for x in (await db.execute(select(Creative).where(Creative.id.in_(ids)))).scalars()}
    return [by_id[i] for i in ids if i in by_id]


async def creatives_ready(db: AsyncSession, c: Campaign, *, creatives: Optional[list] = None,
                          duration_s: Optional[int] = None) -> tuple[bool, list]:
    cl = creatives if creatives is not None else await target_creatives(db, c)
    duration_s = duration_s or c.duration_s
    await creative_svc.refresh_technical(db, cl)
    problems = []
    for cr in cl:
        if cr.technical_status != TechnicalStatus.VALIDO.value:
            problems.append({"creative": cr.code, "problem": cr.technical_error or cr.technical_status})
        elif cr.duration_seconds != duration_s:
            problems.append({"creative": cr.code, "problem": "CREATIVE_DURATION_MISMATCH"})
        elif cr.status in (CreativeStatus.REJEITADO.value, CreativeStatus.ARQUIVADO.value,
                           CreativeStatus.SUBSTITUIDO.value):
            problems.append({"creative": cr.code, "problem": f"CREATIVE_{cr.status}"})
    return (bool(cl) and not problems), problems


# ─── targeting / disponibilidade ─────────────────────────────────────────────

async def eligible_resolution(db: AsyncSession, c: Campaign, *, specs: Optional[list] = None,
                              duration_s: Optional[int] = None, creatives: Optional[list] = None,
                              check_conflicts: bool = True, record: bool = False, ctx=None) -> TG.Resolution:
    """FUNÇÃO ÚNICA de telas elegíveis de uma campanha — usada por checkout, aprovação, alteração, cotação
    (``/pricing/quote`` com ``campaign_id``) e disponibilidade (``/campaigns/availability`` e
    ``/inventory/availability`` com ``campaign_id``): vendáveis × segmentação × bloco externo ≥ duração ×
    orientação dos criativos × CONFLITO avaliado com a campanha real (anunciante + criativos e seus segmentos,
    inclusive pendentes). Estimativa = valor cobrado."""
    if specs is None:
        specs = TG.specs_from(await targets_of(db, c.id))
    cl = creatives if creatives is not None else await creatives_of(db, c.id)
    return await TG.resolve(db, specs, duration_s=int(duration_s or c.duration_s),
                            creative_orientations=[x.orientation for x in cl], campaign=c,
                            check_conflicts=check_conflicts, record=record, ctx=ctx)


async def eligible_screen_ids_among(db: AsyncSession, ctx, campaign_id: uuid.UUID, screen_ids: Iterable,
                                    *, duration_s: Optional[int] = None) -> list[uuid.UUID]:
    """Das telas candidatas (ex.: filtro regional do W-INV), as que a campanha REALMENTE pode comprar —
    mesma regra de ``eligible_resolution``. Escopo: ``campaigns.view`` na organização da campanha."""
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.view")
    cand = {uuid.UUID(str(s)) for s in screen_ids}
    if not cand:
        return []
    from app.models.screens import Screen

    est_ids = sorted({e for (e,) in (await db.execute(select(Screen.establishment_id).where(
        Screen.id.in_(cand)))).all()}, key=str)
    specs = [TG.TargetSpec("ESTABLISHMENT", str(e)) for e in est_ids]
    if not specs:
        return []
    res = await eligible_resolution(db, c, specs=specs, duration_s=duration_s)
    return sorted((s for s in res.screen_ids if s in cand), key=str)


async def resolve_campaign(db: AsyncSession, c: Campaign, *, targets: Optional[list] = None, record: bool = False,
                           ctx=None, check_conflicts: bool = True) -> TG.Resolution:
    specs = TG.specs_from(targets if targets is not None else await targets_of(db, c.id))
    return await eligible_resolution(db, c, specs=specs, check_conflicts=check_conflicts, record=record, ctx=ctx)


async def resolve_target_screens(db: AsyncSession, campaign) -> list[uuid.UUID]:
    """Interface §4: telas vendáveis × segmentação × duração × conflito (ALLOW)."""
    if not isinstance(campaign, Campaign):
        campaign = await db.get(Campaign, campaign)
        if campaign is None:
            raise NotFound()
    res = await resolve_campaign(db, campaign)
    return res.screen_ids


async def estimate_availability(db: AsyncSession, ctx, *, org_id: uuid.UUID, targets: list, duration_s: int,
                                campaign: Optional[Campaign] = None, diagnose: bool = False) -> dict:
    """Disponibilidade antes da compra SEM revelar estabelecimentos (contagem por região).

    ``diagnose`` (o endpoint decide: SÓ equipe RD) acrescenta ``exclusions`` — contagens por motivo das telas
    da MESMA segmentação que ficaram fora (AÇÃO 004). Sem ele, a resposta é a de sempre."""
    specs = TG.validate_specs(TG.specs_from(targets))
    await _check_target_permissions(db, ctx, specs)
    if campaign is not None:  # mesma regra do checkout/aprovação (criativos e segmentos reais da campanha)
        subject = campaign
        res = await eligible_resolution(db, campaign, specs=specs, duration_s=duration_s)
    else:
        # campanha efêmera (não persistida) apenas para avaliar conflitos pela classificação do anunciante
        subject = Campaign(id=uuid.uuid4(), org_id=org_id, name="estimate", status="RASCUNHO", duration_s=duration_s)
        res = await TG.resolve(db, specs, duration_s=duration_s, campaign=subject, check_conflicts=True)
    out = TG.availability_summary(res)
    if diagnose:
        out["exclusions"] = await TG.diagnose_exclusions(db, specs, res, campaign=subject)
    return out


# ─── aprovação / ciclo de vida ───────────────────────────────────────────────

async def _pending_revision(db: AsyncSession, c: Campaign) -> Optional[CampaignRevision]:
    if c.pending_revision_id is None:
        return None
    return await db.get(CampaignRevision, c.pending_revision_id)


async def _inventory_call(name: str, *args, **kwargs):
    fn = await _optional("app.modules.inventory.service", name)
    if fn is None:
        return None
    return await fn(*args, **kwargs)


async def _after_approval(db: AsyncSession, ctx, c: Campaign, rev: CampaignRevision,
                          previous: Optional[CampaignRevision]) -> None:
    now = utcnow()
    rev.status = RevisionStatus.APROVADA.value
    rev.decided_by = getattr(ctx, "user_id", None)
    rev.decided_at = now
    # criativos da revisão aprovados; links pendentes passam a valer; substituídos saem
    links = await links_of(db, c.id)
    approved_ids = {uuid.UUID(x) for x in rev.creative_ids}
    replaced: set[uuid.UUID] = set()
    for ln in links:
        if ln.pending_removal:
            ln.is_active = False
            ln.removed_at = now
            ln.pending_removal = False
            continue
        if ln.creative_id in approved_ids:
            if ln.replaces_creative_id:
                replaced.add(ln.replaces_creative_id)
            ln.is_active = True
            ln.pending = False
            ln.replaces_creative_id = None
    for ln in links:
        if ln.creative_id in replaced:
            ln.is_active = False
            ln.removed_at = now
    if approved_ids:
        for cr in (await db.execute(select(Creative).where(Creative.id.in_(approved_ids)))).scalars():
            if creative_svc.can_review(cr.status):
                creative_svc.mark_reviewed(cr, approved=True, ctx=ctx)
    if replaced:
        await db.execute(update(Creative).where(Creative.id.in_(replaced), Creative.status == "APROVADO")
                         .values(status=CreativeStatus.SUBSTITUIDO.value).execution_options(synchronize_session=False))
    screens = set(rev.screen_ids or [])
    if previous is not None and previous.id != rev.id:
        previous.status = RevisionStatus.SUBSTITUIDA.value
        screens |= set(previous.screen_ids or [])
        keep = set(revision_hold_ids(rev))
        for hid in revision_hold_ids(previous):
            if hid not in keep:  # a nova contratação já está BOOKED (invariante i)
                await _inventory_call("release_hold", db, hid, "SUPERSEDED_BY_REVISION")
        apply_spec(c, rev.spec)
        c.contracted_price_cents = int(rev.price_cents)
        await replace_targets(db, c.id, TG.specs_from(rev.spec["targets"]))
    if screens:
        # autoflush=False: revisão/links/criativos/spec alterados precisam estar no banco ANTES de o W-INV
        # regerar o manifesto na mesma transação (não depender de objetos reaproveitados da sessão)
        await db.flush()
        await _inventory_call("invalidate_screens", db, [uuid.UUID(s) for s in sorted(screens)],
                              f"campaign_approved:{c.id}")
    rec = await _org_recipients(c.org_id)
    if rec:
        await _notify(db, rec, "campaign.approved", {"campaign_name": c.name, "campaign_id": str(c.id)},
                      dedupe_key=f"campaign.approved:{c.id}:{rev.id}", entity=("campaign", c.id), org_id=c.org_id)


async def _enter_review(db: AsyncSession, ctx, c: Campaign, to: str, trigger: str, rev: Optional[CampaignRevision],
                        *, extra: Optional[dict] = None, reason: Optional[str] = None) -> Campaign:
    now = utcnow()
    values = dict(extra or {})
    if to == CampaignStatus.EM_ANALISE.value:
        values.update(submitted_at=now, review_due_at=await review_due(db, now))
    await _transition(db, ctx, c, to, trigger, values=values, revision_id=rev.id if rev else None, reason=reason)
    if to == CampaignStatus.EM_ANALISE.value:
        if rev is not None:
            rev.status = RevisionStatus.EM_ANALISE.value
        defer_event(db, "approval.pending", {"campaign_id": str(c.id), "name": c.name, "org_id": str(c.org_id),
                                             "review_due_at": c.review_due_at.isoformat() if c.review_due_at else None})
        staff = await _staff_recipients()
        if staff:
            await _notify(db, staff, "campaign.awaiting_approval", {"campaign_name": c.name, "campaign_id": str(c.id)},
                          dedupe_key=f"campaign.awaiting_approval:{c.id}:{rev.id if rev else ''}",
                          entity=("campaign", c.id))
    return c


async def _auto_or_review(db: AsyncSession, ctx, c: Campaign, trigger: str, rev: CampaignRevision,
                          extra: dict, *, force_manual: bool = False) -> Campaign:
    ready, problems = await creatives_ready(db, c, duration_s=rev.spec.get("duration_s"))
    if not ready:
        if trigger == "payment_confirmed":
            rev.status = RevisionStatus.AGUARDANDO_MIDIA.value
            return await _transition(db, ctx, c, CampaignStatus.AGUARDANDO_MIDIA.value, trigger, values=extra,
                                     revision_id=rev.id)
        raise Conflict("CREATIVES_NOT_READY", "Envie ao menos um criativo válido (mídia pronta e duração correta).",
                       details={"problems": problems})
    mode, source = await effective_approval(db, c.org_id)
    rev.creative_ids = [str(x.id) for x in await target_creatives(db, c)]
    if mode == ApprovalMode.AUTO.value and not force_manual:
        now = utcnow()
        previous = await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None
        values = {**extra, "approved_at": now, "approved_by": None, "approval_source": source,
                  "live_revision_id": rev.id, "pending_revision_id": None, "submitted_at": now,
                  "review_due_at": None, "rejection_reason": None}
        await _transition(db, ctx, c, CampaignStatus.APROVADA.value, trigger, values=values, revision_id=rev.id,
                          reason=f"aprovação automática ({source})")
        await _after_approval(db, ctx, c, rev, previous)
        return c
    return await _enter_review(db, ctx, c, CampaignStatus.EM_ANALISE.value, trigger, rev, extra=extra)


async def on_payment_confirmed(db: AsyncSession, campaign_id: uuid.UUID, payment_order_id: uuid.UUID, *,
                               ctx=None, order_id: Optional[uuid.UUID] = None,
                               payment_issue: Optional[str] = None,
                               revision_id: Optional[uuid.UUID] = None) -> Campaign:
    """Pagamento confirmado → EM_ANALISE (MANUAL) | APROVADA (AUTO/cliente confiável) | AGUARDANDO_MIDIA.

    NUNCA aprova direto quando o modo é MANUAL. Idempotente.
    """
    ctx = ctx or _sys_ctx("payment")
    c = (await db.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if c is None:
        raise NotFound()
    if revision_id is not None:
        target = await db.get(CampaignRevision, revision_id)
        if target is not None and target.campaign_id == c.id and target.kind == RevisionKind.RENEWAL.value:
            from app.modules.campaigns.renewals import confirm_renewal

            return await confirm_renewal(db, ctx, c, target, payment_order_id, payment_issue=payment_issue)
    if c.payment_order_id == payment_order_id and c.paid_at is not None:
        return c  # já processado (replay do webhook)
    rev = await _pending_revision(db, c)
    if rev is not None and rev.kind == RevisionKind.CHANGE.value and             (rev.change_summary or {}).get("diff_payment_order_id") == str(payment_order_id):
        return c  # diferença de alteração já processada (replay)
    if rev is None or rev.status not in (RevisionStatus.AGUARDANDO_PAGAMENTO.value, RevisionStatus.EXPIRADA.value):
        raise Conflict("CAMPAIGN_NOT_AWAITING_PAYMENT", "Campanha não aguarda pagamento.",
                       details={"status": c.status})
    if order_id is not None and rev.order_id is not None and rev.order_id != order_id:
        raise Conflict("ORDER_MISMATCH", "Pagamento não corresponde ao pedido vigente da campanha.")
    now = utcnow()
    if rev.kind == RevisionKind.CHANGE.value:
        # alteração paga (diferença): a versão vigente continua; a nova vai para análise.
        # O preço contratado só muda quando a alteração for APROVADA.
        rev.change_summary = {**(rev.change_summary or {}), "diff_payment_order_id": str(payment_order_id),
                              "diff_paid_at": now.isoformat(),
                              "hold_converted": bool(rev.hold_id) and payment_issue is None}
        if payment_issue is not None:
            # pago sem capacidade para a nova configuração: alteração descartada, vigente intacta, reembolso
            await _discard_pending_change(db, ctx, c, rev, status=RevisionStatus.REJEITADA.value, reason=payment_issue)
            rev.change_summary = {**(rev.change_summary or {}), "requires_refund_review": True}
            c.pending_revision_id = None
            c.payment_issue = f"CHANGE_{payment_issue}"
            await audit(db, ctx, "campaign.change_paid_without_inventory", "campaign", c.id,
                        after={"revision": rev.number, "payment_order_id": str(payment_order_id)}, org_id=c.org_id)
            return c
        await _request_review_for_change(db, ctx, c, rev, extra={"payment_issue": None})
        mode, _src = await effective_approval(db, c.org_id)
        if mode == ApprovalMode.AUTO.value:
            await approve(db, _sys_ctx("system"), c.id, note="aprovação automática da alteração")
        return c
    extra = {"paid_at": now, "payment_order_id": payment_order_id, "paid_order_id": rev.order_id,
             "contracted_price_cents": int(rev.price_cents), "requires_new_payment": False,
             "payment_issue": payment_issue}
    if c.status == CampaignStatus.RASCUNHO.value:
        # pagamento tardio (reserva expirou): vale o que foi pago — restaura a especificação congelada
        apply_spec(c, rev.spec)
        await replace_targets(db, c.id, TG.specs_from(rev.spec["targets"]))
        await audit(db, ctx, "campaign.late_payment_spec_restored", "campaign", c.id,
                    after={"revision": rev.number}, org_id=c.org_id)
    rec = await _org_recipients(c.org_id, "payments.view")
    if rec:
        await _notify(db, rec, "payment.approved", {"campaign_name": c.name, "campaign_id": str(c.id),
                                                     "amount_cents": int(rev.price_cents)},
                      dedupe_key=f"payment.approved:{c.id}:{payment_order_id}", entity=("campaign", c.id),
                      org_id=c.org_id)
    return await _auto_or_review(db, ctx, c, "payment_confirmed", rev, extra,
                                 force_manual=payment_issue is not None)


async def submit_media(db: AsyncSession, ctx, campaign_id: uuid.UUID) -> Campaign:
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    if c.status != CampaignStatus.AGUARDANDO_MIDIA.value:
        raise Conflict("CAMPAIGN_INVALID_TRANSITION", "Campanha não aguarda mídia.", details={"status": c.status})
    rev = await _pending_revision(db, c)
    if rev is None:
        raise Conflict("CAMPAIGN_NO_PENDING_REVISION", "Campanha sem versão pendente.")
    return await _auto_or_review(db, ctx, c, "media_submitted", rev, {})


CHANGE_NO_CAPACITY = "CHANGE_NO_CAPACITY"


async def _ensure_change_booking(db: AsyncSession, ctx, c: Campaign, rev: CampaignRevision,
                                 previous: Optional[CampaignRevision]) -> Optional[dict]:
    """Alteração que muda telas/programação: garante a NOVA contratação BOOKED antes de aprovar.

    - Com cobrança (diff > 0): a reserva foi convertida no pagamento (``hold_converted``) — nada a fazer.
    - Sem cobrança (diff ≤ 0): ``hold`` (TTL padrão, ``replaces_hold_id`` = vigente: sem contagem dupla) +
      ``convert_hold`` AGORA, num SAVEPOINT. Sem capacidade ⇒ devolve o erro; a reserva vigente não é tocada.
    A vigente só é liberada depois, em ``_after_approval`` (invariante: nunca sem BOOKED)."""
    cs = rev.change_summary or {}
    if rev.kind != RevisionKind.CHANGE.value or not cs.get("needs_booking") or cs.get("hold_converted"):
        return None
    hold_fn = await _optional("app.modules.inventory.service", "hold")
    convert = await _optional("app.modules.inventory.service", "convert_hold")
    if hold_fn is None or convert is None:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Aprovação indisponível (inventário).", status=503)
    try:
        async with db.begin_nested():
            h = await hold_fn(db, ctx, c.id, [uuid.UUID(s) for s in rev.screen_ids], [pattern_from_spec(rev.spec)],
                              f"change:{rev.id}", None, replaces_hold_id=previous.hold_id if previous else None)
            res = await convert(db, h.id)
            st = getattr(res, "status", None) if not isinstance(res, dict) else res.get("status")
            if st != "CONVERTED":
                raise Conflict("INVENTORY_UNAVAILABLE", "Sem capacidade para a nova configuração.",
                               details={"hold_status": st})
    except DomainError as exc:
        if exc.code not in ("INVENTORY_UNAVAILABLE", "HOLD_ALREADY_ACTIVE"):
            raise
        return {"code": CHANGE_NO_CAPACITY,
                "message": "Não há capacidade nas telas para a nova configuração; a versão vigente continua.",
                "details": {"cause": exc.code, **({"inventory": exc.details} if exc.details else {})}}
    rev.hold_id = h.id
    rev.change_summary = {**cs, "hold_converted": True, "hold_ids": [str(h.id)]}
    return None


async def approve(db: AsyncSession, ctx, campaign_id: uuid.UUID, *, note: Optional[str] = None) -> dict:
    c = (await db.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if c is None:
        raise NotFound()
    if c.status != CampaignStatus.EM_ANALISE.value:
        raise Conflict("CAMPAIGN_INVALID_TRANSITION", "Somente campanhas EM_ANALISE podem ser aprovadas.",
                       details={"status": c.status})
    rev = await _pending_revision(db, c)
    if rev is None:
        raise Conflict("CAMPAIGN_NO_PENDING_REVISION", "Campanha sem versão pendente de análise.")
    ready, problems = await creatives_ready(db, c, duration_s=rev.spec.get("duration_s"))
    if not ready:
        raise Conflict("CREATIVES_NOT_READY", "Há criativos inválidos ou ausentes.", details={"problems": problems})
    # reavalia conflitos nas telas congeladas (decisões gravadas e explicáveis)
    est_ids = set()
    if rev.screen_ids:
        from app.models.screens import Screen

        est_ids = set((await db.execute(select(Screen.establishment_id).where(
            Screen.id.in_([uuid.UUID(s) for s in rev.screen_ids])))).scalars())
    decisions = await evaluate_campaign_establishments(db, c, est_ids, record=True, ctx=ctx) if est_ids else {}
    blocked = sorted(str(e) for e, d in decisions.items() if not d.allowed)
    previous = await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None
    booking_error = await _ensure_change_booking(db, ctx, c, rev, previous)
    if booking_error is not None:
        await audit(db, ctx, "campaign.change_approval_failed", "campaign", c.id,
                    after={"revision": rev.number, **booking_error}, org_id=c.org_id)
        rec = await _org_recipients(c.org_id)
        if rec:
            await _notify(db, rec, "campaign.change_no_capacity", {"campaign_name": c.name, "campaign_id": str(c.id)},
                          dedupe_key=f"campaign.change_no_capacity:{rev.id}", entity=("campaign", c.id),
                          org_id=c.org_id)
        return {"campaign": c, "conflicts_blocked_establishments": 0, "error": booking_error}
    now = utcnow()
    rev.creative_ids = [str(x.id) for x in await target_creatives(db, c)]
    await _transition(db, ctx, c, CampaignStatus.APROVADA.value, "approve", reason=note, revision_id=rev.id,
                      values={"approved_at": now, "approved_by": ctx.user_id,
                              "approval_source": ApprovalSource.MANUAL.value, "live_revision_id": rev.id,
                              "pending_revision_id": None, "status_before_review": None, "rejection_reason": None,
                              "review_due_at": None})
    await _after_approval(db, ctx, c, rev, previous)
    return {"campaign": c, "conflicts_blocked_establishments": len(blocked)}


REJECTION_MIN_LEN = 10


async def reject(db: AsyncSession, ctx, campaign_id: uuid.UUID, *, reason: str,
                 creative_ids: Optional[list] = None) -> Campaign:
    reason = (reason or "").strip()
    if len(reason) < REJECTION_MIN_LEN:
        raise DomainError("REJECTION_REASON_REQUIRED", f"Informe o motivo da rejeição (mín. {REJECTION_MIN_LEN} "
                          "caracteres).", status=400)
    c = (await db.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if c is None:
        raise NotFound()
    if c.status != CampaignStatus.EM_ANALISE.value:
        raise Conflict("CAMPAIGN_INVALID_TRANSITION", "Somente campanhas EM_ANALISE podem ser rejeitadas.",
                       details={"status": c.status})
    rev = await _pending_revision(db, c)
    now = utcnow()
    if creative_ids:
        ids = [uuid.UUID(str(x)) for x in creative_ids]
        for cr in (await db.execute(select(Creative).join(CampaignCreative, CampaignCreative.creative_id == Creative.id)
                                    .where(CampaignCreative.campaign_id == c.id, Creative.id.in_(ids)))).scalars():
            if creative_svc.can_review(cr.status):  # o criativo vigente já aprovado não é afetado
                creative_svc.mark_reviewed(cr, approved=False, ctx=ctx, reason=reason)
    if c.live_revision_id is not None and rev is not None and rev.kind == RevisionKind.CHANGE.value:
        # alteração rejeitada: a versão vigente continua; volta ao estado operacional anterior
        back = c.status_before_review or CampaignStatus.APROVADA.value
        await _discard_pending_change(db, ctx, c, rev, status=RevisionStatus.REJEITADA.value, reason=reason)
        await _transition(db, ctx, c, back, "change_rejected", reason=reason, revision_id=rev.id,
                          values={"pending_revision_id": None, "status_before_review": None,
                                  "review_due_at": None, "rejection_reason": reason, "rejected_at": now,
                                  "rejected_by": ctx.user_id})
    else:
        if rev is not None:
            rev.status = RevisionStatus.REJEITADA.value
            rev.decided_by, rev.decided_at, rev.decision_reason = ctx.user_id, now, reason
        await _transition(db, ctx, c, CampaignStatus.REJEITADA.value, "reject", reason=reason,
                          revision_id=rev.id if rev else None,
                          values={"rejected_at": now, "rejected_by": ctx.user_id, "rejection_reason": reason,
                                  "rejection_count": c.rejection_count + 1, "review_due_at": None})
    rec = await _org_recipients(c.org_id)
    if rec:
        await _notify(db, rec, "campaign.rejected", {"campaign_name": c.name, "campaign_id": str(c.id),
                                                      "reason": reason},
                      dedupe_key=f"campaign.rejected:{c.id}:{now.isoformat()}", entity=("campaign", c.id),
                      org_id=c.org_id)
    return c


async def resubmit(db: AsyncSession, ctx, campaign_id: uuid.UUID) -> Campaign:
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    if c.status != CampaignStatus.REJEITADA.value:
        raise Conflict("CAMPAIGN_INVALID_TRANSITION", "Somente campanhas REJEITADAS podem ser reenviadas.",
                       details={"status": c.status})
    if c.paid_at is None or c.requires_new_payment:
        raise Conflict("CAMPAIGN_REQUIRES_PAYMENT", "Esta campanha precisa passar pelo checkout novamente.")
    rev = await db.get(CampaignRevision, c.pending_revision_id) if c.pending_revision_id else None
    if rev is None:
        rev = (await db.execute(select(CampaignRevision).where(CampaignRevision.campaign_id == c.id)
                                .order_by(CampaignRevision.number.desc()).limit(1))).scalar_one_or_none()
    if rev is None:
        raise Conflict("CAMPAIGN_NO_PENDING_REVISION", "Campanha sem versão para reenvio.")
    links = await links_of(db, c.id)
    rejected = [ln for ln in links if ln.is_active]
    crs = {x.id: x for x in await creatives_of(db, c.id)}
    if any(crs.get(ln.creative_id) and crs[ln.creative_id].status == CreativeStatus.REJEITADO.value for ln in rejected):
        raise Conflict("CREATIVE_REJECTED_STILL_LINKED", "Substitua ou remova os criativos rejeitados antes de reenviar.")
    c.pending_revision_id = rev.id
    rev.decided_by = rev.decided_at = rev.decision_reason = None
    return await _auto_or_review(db, ctx, c, "resubmit", rev, {"pending_revision_id": rev.id})


async def pause(db: AsyncSession, ctx, campaign_id: uuid.UUID, *, reason: Optional[str] = None,
                kill: bool = False) -> Campaign:
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.kill" if kill else "campaigns.manage",
                           for_update=True)
    trigger = "kill" if kill else "pause"
    if kill and len((reason or "").strip()) < 10:
        raise DomainError("REASON_REQUIRED", "Informe o motivo da interrupção (mín. 10 caracteres).", status=400)
    await _transition(db, ctx, c, CampaignStatus.PAUSADA.value, trigger, reason=reason,
                      values={"paused_from": c.status, "paused_at": utcnow()})
    if kill:
        await _kill_creatives(db, ctx, c, reason or "")
    await _invalidate_live(db, c, f"campaign_{trigger}:{c.id}")
    return c


def _kill_tag(c: Campaign) -> str:
    return f"[campanha {c.id}]"


async def _kill_creatives(db: AsyncSession, ctx, c: Campaign, reason: str) -> list:
    """Kill de CAMPANHA = corte IMEDIATO nos players online (ETAPA2 #1237–#1239; P001 §75):
    um kill por criativo (vigente ou pendente) nas telas da versão vigente, persistido pelo
    W-PLAY (``revoked`` no próximo manifesto) e publicado como ``kill`` (RDPP) após o commit."""
    create_kill = await _optional("app.modules.playback.kills", "create_kill")
    if create_kill is None:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Kill imediato indisponível (playback).", status=503)
    live = await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None
    screens = [uuid.UUID(s) for s in ((live.screen_ids if live else None) or [])]
    creatives = await creatives_of(db, c.id)
    if not screens or not creatives:
        return []
    kills = []
    for cr in creatives:
        kills += await create_kill(db, ctx, cr.id, screens, f"{_kill_tag(c)} {reason}".strip())
    from app.modules.campaigns.notify import defer_player_message

    defer_player_message(db, screens, "kill", {"creative_ids": [str(x.id) for x in creatives],
                                                "campaign_id": str(c.id), "reason": reason})
    await audit(db, ctx, "campaign.creatives_killed", "campaign", c.id,
                after={"creatives": [x.code for x in creatives], "screens": len(screens), "kills": len(kills)},
                reason=reason, org_id=c.org_id)
    return kills


async def _active_campaign_kills(db: AsyncSession, c: Campaign) -> list:
    try:
        from app.models.devices import CreativeKill
    except Exception:  # pragma: no cover
        return []
    ids = [x.id for x in await creatives_of(db, c.id)]
    if not ids:
        return []
    return list((await db.execute(select(CreativeKill).where(
        CreativeKill.creative_id.in_(ids), CreativeKill.lifted_at.is_(None),
        CreativeKill.reason.like(f"{_kill_tag(c)}%")))).scalars())


async def resume(db: AsyncSession, ctx, campaign_id: uuid.UUID) -> Campaign:
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    if c.status != CampaignStatus.PAUSADA.value:
        raise Conflict("CAMPAIGN_INVALID_TRANSITION", "Campanha não está pausada.", details={"status": c.status})
    kills = await _active_campaign_kills(db, c)
    if kills:
        # interrompida pela RD (kill): só a RD com campaigns.kill global retoma
        if not ctx.can("campaigns.kill", global_only=True):
            raise DomainError("CAMPAIGN_KILLED", "Campanha interrompida pela RD; somente a RD pode retomá-la.",
                              status=403)
        lift = await _optional("app.modules.playback.kills", "lift_kill")
        for k in kills:
            await lift(db, ctx, k.id, f"campanha retomada {c.id}")
    back = c.paused_from or CampaignStatus.ATIVA.value
    await _transition(db, ctx, c, back, "resume", values={"paused_from": None, "paused_at": None})
    await _invalidate_live(db, c, f"campaign_resumed:{c.id}")
    return c


async def _invalidate_live(db: AsyncSession, c: Campaign, reason: str) -> None:
    rev = await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None
    if rev is not None and rev.screen_ids:
        await db.flush()  # autoflush=False: o gerador do W-INV lê o banco na mesma transação
        await _inventory_call("invalidate_screens", db, [uuid.UUID(s) for s in rev.screen_ids], reason)


NON_BLOCKING_REFUND_CODES = frozenset({"REFUND_WINDOW_EXPIRED", "REFUND_NOT_ALLOWED", "REFUND_ORDER_NOT_PAID",
                                       "REFUND_ALREADY_OPEN"})


async def paid_payment_order_ids(db: AsyncSession, c: Campaign) -> list[uuid.UUID]:
    """Cobranças PAGAS ligadas à campanha: compra inicial + renovações + diferenças de alteração."""
    out: list[uuid.UUID] = []
    if c.paid_at is not None and c.payment_order_id is not None:
        out.append(c.payment_order_id)
    revs = (await db.execute(select(CampaignRevision).where(CampaignRevision.campaign_id == c.id)
                             .order_by(CampaignRevision.number))).scalars().all()
    for r in revs:
        cs = r.change_summary or {}
        pid = None
        if r.kind == RevisionKind.RENEWAL.value and r.status in (RevisionStatus.APROVADA.value,
                                                                 RevisionStatus.SUBSTITUIDA.value,
                                                                 RevisionStatus.REJEITADA.value):
            pid = cs.get("payment_order_id")
        elif r.kind == RevisionKind.CHANGE.value:
            pid = cs.get("diff_payment_order_id")
        if pid:
            u = uuid.UUID(str(pid))
            if u not in out:
                out.append(u)
    return out


async def cancel_quote(db: AsyncSession, ctx, campaign_id: uuid.UUID) -> dict:
    """Prévia do cancelamento (diálogo do frontend): reembolso calculado pelo financeiro por cobrança paga."""
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage")
    can_cancel = ST.is_allowed("cancel", c.status, CampaignStatus.CANCELADA.value)
    out = {"campaign_id": str(c.id), "status": c.status, "can_cancel": can_cancel, "paid": c.paid_at is not None,
           "payments": [], "refund_total_cents": 0}
    pids = await paid_payment_order_ids(db, c)
    if not pids:
        out["note"] = "Campanha sem pagamento: cancelamento não gera reembolso."
        return out
    quote = await _optional("app.modules.financial.service", "quote_refund")
    if quote is None:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Cotação de reembolso indisponível (financeiro).", status=503)
    total = 0
    for pid in pids:
        try:
            q = await quote(db, pid, c.id, None)
        except DomainError as exc:
            out["payments"].append({"payment_order_id": str(pid), "error": exc.code, "refund_cents": 0})
            continue
        total += int(q.get("refund_cents") or 0)
        out["payments"].append(q)
    out["refund_total_cents"] = total
    return out


async def cancel(db: AsyncSession, ctx, campaign_id: uuid.UUID, *, reason: str) -> Campaign:
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise DomainError("REASON_REQUIRED", "Informe o motivo do cancelamento.", status=400)
    c = await get_campaign(db, ctx, campaign_id, perm="campaigns.manage", for_update=True)
    if not ST.is_allowed("cancel", c.status, CampaignStatus.CANCELADA.value):
        raise Conflict("CAMPAIGN_INVALID_TRANSITION", "Campanha não pode ser cancelada neste status.",
                       details={"status": c.status})
    now = utcnow()
    values = {"cancelled_at": now, "cancelled_by": ctx.user_id, "cancel_reason": reason}
    invalidate = False
    if c.status == CampaignStatus.AGUARDANDO_PAGAMENTO.value:
        from app.modules.campaigns import checkout as CK

        rev = await _pending_revision(db, c)
        if rev is not None and rev.order_id is not None:
            await CK.cancel_order(db, ctx, rev.order_id, reason=f"campaign_cancelled:{c.id}", keep_campaign=c.id)
            await db.flush()
            await db.refresh(c)
    elif c.paid_at is not None and c.status not in (CampaignStatus.RASCUNHO.value,):
        # campanha paga: cancelamento dispara a política de reembolso do financeiro (ADR_009 #8)
        req = await _optional("app.modules.financial.service", "request_refund")
        if req is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Cancelamento de campanha paga indisponível (financeiro).",
                              status=503)
        pend = await db.get(CampaignRevision, c.pending_revision_id) if c.pending_revision_id else None
        if pend is not None and pend.order_id is not None and pend.status == RevisionStatus.AGUARDANDO_PAGAMENTO.value:
            # renovação/alteração ainda não paga: cancela a cobrança pendente (o PIX deixa de valer)
            from app.modules.campaigns import checkout as CK

            await CK.cancel_order(db, ctx, pend.order_id, reason=f"campaign_cancelled:{c.id}", keep_campaign=c.id,
                                  check_scope=False)
            await db.flush()
            await db.refresh(c)
        refunds = []
        for pid in await paid_payment_order_ids(db, c):
            try:
                async with db.begin_nested():
                    r = await req(db, ctx, pid, c.id, reason)
                refunds.append({"payment_order_id": str(pid), "refund_id": str(getattr(r, "id", "") or "")})
            except DomainError as exc:
                if exc.code not in NON_BLOCKING_REFUND_CODES:
                    raise
                refunds.append({"payment_order_id": str(pid), "skipped": exc.code})
        await audit(db, ctx, "campaign.cancel_refunds", "campaign", c.id, after={"refunds": refunds}, org_id=c.org_id)
        for rid in {c.live_revision_id, c.pending_revision_id} - {None}:
            r = await db.get(CampaignRevision, rid)
            for hid in revision_hold_ids(r):
                await _inventory_call("release_hold", db, hid, "CAMPAIGN_CANCELLED")
            if r is not None and r.status not in (RevisionStatus.SUBSTITUIDA.value,):
                r.status = RevisionStatus.CANCELADA.value
        invalidate = True
    await _transition(db, ctx, c, CampaignStatus.CANCELADA.value, "cancel", reason=reason, values=values)
    if invalidate:
        # DEPOIS da transição: o manifesto é regerado com a campanha já CANCELADA no banco (mesmo sem hold)
        await _invalidate_live(db, c, f"campaign_cancelled:{c.id}")
    return c


# ─── transições acionadas por W-INV / W-PLAY / tarefa ────────────────────────

async def mark_scheduled(db: AsyncSession, campaign_id: uuid.UUID, *, ctx=None) -> bool:
    """APROVADA → AGENDADA (plano futuro gerado pelo W-INV). Idempotente; devolve se mudou."""
    ctx = ctx or _sys_ctx("inventory")
    c = (await db.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if c is None:
        raise NotFound()
    if c.status != CampaignStatus.APROVADA.value:
        return False
    await _transition(db, ctx, c, CampaignStatus.AGENDADA.value, "mark_scheduled", values={"scheduled_at": utcnow()})
    return True


async def mark_syncing(db: AsyncSession, campaign_id: uuid.UUID, *, ctx=None) -> bool:
    """APROVADA/AGENDADA → SINCRONIZANDO (manifestos com a campanha publicados)."""
    ctx = ctx or _sys_ctx("inventory")
    c = (await db.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if c is None:
        raise NotFound()
    if c.status not in (CampaignStatus.APROVADA.value, CampaignStatus.AGENDADA.value):
        return False
    await _transition(db, ctx, c, CampaignStatus.SINCRONIZANDO.value, "mark_syncing", values={"syncing_at": utcnow()})
    return True


async def _first_local_start_reached(db: AsyncSession, c: Campaign, screen_id: Optional[uuid.UUID]) -> bool:
    if c.start_date is None:
        return False
    tz = DEFAULT_TZ
    if screen_id is not None:
        from app.models.establishments import Establishment
        from app.models.screens import Screen

        tz = (await db.execute(select(Establishment.timezone).join(Screen, Screen.establishment_id == Establishment.id)
                               .where(Screen.id == screen_id))).scalar() or DEFAULT_TZ
    return local_date(utcnow(), tz) >= c.start_date


async def mark_active_from_ack(db: AsyncSession, campaign_ids: Iterable[uuid.UUID], *, screen_id: uuid.UUID,
                               manifest_version: int, ctx=None) -> list[uuid.UUID]:
    """W-PLAY: player confirmou (manifest_applied) um manifesto com estas campanhas → ATIVA."""
    ctx = ctx or _sys_ctx("player")
    changed = []
    for cid in sorted({uuid.UUID(str(x)) for x in campaign_ids}, key=str):
        c = (await db.execute(select(Campaign).where(Campaign.id == cid).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if c is None or c.status not in (CampaignStatus.SINCRONIZANDO.value, CampaignStatus.AGENDADA.value,
                                         CampaignStatus.APROVADA.value):
            continue
        if not await _first_local_start_reached(db, c, screen_id):
            continue
        await _transition(db, ctx, c, CampaignStatus.ATIVA.value, "ack_active",
                          reason=f"ack tela {screen_id} manifesto v{manifest_version}",
                          values={"activated_at": c.activated_at or utcnow()})
        rec = await _org_recipients(c.org_id)
        if rec:
            await _notify(db, rec, "campaign.started", {"campaign_name": c.name, "campaign_id": str(c.id)},
                          dedupe_key=f"campaign.started:{c.id}", entity=("campaign", c.id), org_id=c.org_id)
        changed.append(cid)
    return changed


async def campaign_end_utc(db: AsyncSession, c: Campaign, rev: Optional[CampaignRevision] = None) -> Optional[datetime]:
    """Último instante de veiculação: 23:59:59 local do ``end_date`` no fuso de CADA tela (o maior)."""
    if c.end_date is None:
        return None
    rev = rev or (await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None)
    tzs = {DEFAULT_TZ}
    if rev is not None and rev.screen_ids:
        from app.models.establishments import Establishment
        from app.models.screens import Screen

        tzs = set((await db.execute(select(Establishment.timezone).join(Screen, Screen.establishment_id == Establishment.id)
                                    .where(Screen.id.in_([uuid.UUID(s) for s in rev.screen_ids])))).scalars()) or tzs
    return max(end_of_local_day_utc(c.end_date, tz) for tz in tzs)


async def _expire_open_renewal(db: AsyncSession, ctx, c: Campaign) -> None:
    """Fim do período sem pagamento da renovação: a proposta EXPIRA e cobranças abertas são encerradas."""
    if c.pending_revision_id is None:
        return
    rev = await db.get(CampaignRevision, c.pending_revision_id)
    if rev is None or rev.kind != RevisionKind.RENEWAL.value or rev.status != RevisionStatus.AGUARDANDO_PAGAMENTO.value:
        return
    from app.models.pricing import Order, OrderItem, OrderStatus
    from app.modules.campaigns import checkout as CK

    open_orders = (await db.execute(select(Order.id).join(OrderItem, OrderItem.order_id == Order.id).where(
        OrderItem.revision_id == rev.id, Order.status == OrderStatus.AGUARDANDO_PAGAMENTO.value))).scalars().all()
    for oid in open_orders:
        await CK.on_order_expired(db, oid, ctx=ctx)
    rev.status = RevisionStatus.EXPIRADA.value
    rev.decided_at, rev.decision_reason = utcnow(), "renovação não paga até o vencimento"
    c.pending_revision_id = None
    await audit(db, ctx, "campaign.renewal_expired", "campaign", c.id, after={"revision": rev.number},
                org_id=c.org_id)


async def finalize_expired(db: AsyncSession, now: Optional[datetime] = None, *, ctx=None) -> list[uuid.UUID]:
    """Finaliza campanhas cuja última tela passou de 23:59:59 local do último dia (sem tolerância)."""
    ctx = ctx or _sys_ctx("worker")
    now = now or utcnow()
    # condição necessária (fusos do Brasil são UTC−2..−5): data UTC atual ≥ end_date
    rows = (await db.execute(select(Campaign.id).where(
        Campaign.status.in_(sorted(ST.OPERATIONAL)), Campaign.end_date.is_not(None),
        Campaign.end_date <= now.date()).order_by(Campaign.id))).scalars().all()
    done = []
    for cid in rows:
        c = (await db.execute(select(Campaign).where(Campaign.id == cid).with_for_update(skip_locked=True).execution_options(populate_existing=True))
             ).scalar_one_or_none()
        if c is None or c.status not in ST.OPERATIONAL:
            continue
        end = await campaign_end_utc(db, c)
        if end is None or now <= end:
            continue
        await _expire_open_renewal(db, ctx, c)
        await _transition(db, ctx, c, CampaignStatus.FINALIZADA.value, "expired",
                          values={"finalized_at": now, "paused_from": None},
                          reason=f"vencimento local {c.end_date.isoformat()} 23:59:59")
        rec = await _org_recipients(c.org_id)
        if rec:
            await _notify(db, rec, "campaign.ended", {"campaign_name": c.name, "campaign_id": str(c.id)},
                          dedupe_key=f"campaign.ended:{c.id}", entity=("campaign", c.id), org_id=c.org_id)
        done.append(cid)
    return done


# ─── consultas para W-INV / W-FIN / UI ───────────────────────────────────────

@dataclass
class LiveSpec:
    campaign_id: uuid.UUID
    org_id: uuid.UUID
    status: str
    revision_id: uuid.UUID
    revision_number: int
    spec: dict
    pattern: dict
    screen_ids: list
    creative_ids: list
    hold_id: Optional[uuid.UUID]
    has_pending_change: bool = False
    hold_ids: list = field(default_factory=list)  # reservas em vigor (inicial/alteração + renovações)


def revision_hold_ids(rev: Optional[CampaignRevision]) -> list[uuid.UUID]:
    """Reservas em vigor de uma revisão: a própria + as herdadas (renovação não substitui o período anterior)."""
    if rev is None:
        return []
    ids = [uuid.UUID(str(x)) for x in ((rev.change_summary or {}).get("hold_ids") or [])]
    if rev.hold_id is not None and rev.hold_id not in ids:
        ids.append(rev.hold_id)
    return ids


def is_campaign_playable(c: Campaign) -> bool:
    if c.live_revision_id is None:
        return False
    if c.status in ST.PLAYABLE:
        return True
    # alteração em análise: a versão anterior continua tocando
    return c.status == CampaignStatus.EM_ANALISE.value and c.status_before_review in ST.PLAYABLE


async def playable_campaign_specs(db: AsyncSession, screen_id: Optional[uuid.UUID] = None) -> list[LiveSpec]:
    q = select(Campaign).where(Campaign.live_revision_id.is_not(None),
                               Campaign.status.in_(list(ST.PLAYABLE) + [CampaignStatus.EM_ANALISE.value]))
    out = []
    for c in (await db.execute(q.order_by(Campaign.id))).scalars():
        if not is_campaign_playable(c):
            continue
        rev = await db.get(CampaignRevision, c.live_revision_id)
        if rev is None:
            continue
        if screen_id is not None and str(screen_id) not in (rev.screen_ids or []):
            continue
        active = [str(ln.creative_id) for ln in await links_of(db, c.id) if ln.is_active and not ln.pending]
        out.append(LiveSpec(c.id, c.org_id, c.status, rev.id, rev.number, rev.spec, pattern_from_spec(rev.spec),
                            list(rev.screen_ids or []), active, rev.hold_id,
                            has_pending_change=c.pending_revision_id is not None, hold_ids=revision_hold_ids(rev)))
    return out


async def attributed_revenue_by_screen(db: AsyncSession, campaign_id: uuid.UUID) -> dict:
    """Receita de MÍDIA atribuída por tela (linha de preço por tela/classe) — modelo B de comissão."""
    from app.core.money import allocate

    c = await db.get(Campaign, campaign_id)
    if c is None:
        raise NotFound()
    rev = await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None
    if rev is None or not rev.quote:
        return {}
    weights = rev.quote.get("per_screen_weights") or {}
    media = int(rev.quote.get("media_cents", 0))
    if not weights or media <= 0:
        return {}
    alloc = allocate(media, sorted((k, int(v)) for k, v in weights.items()))
    shares = dict(alloc.shares)
    if alloc.residual_cents:
        first = sorted(shares)[0]
        shares[first] += alloc.residual_cents
    return {uuid.UUID(k): v for k, v in shares.items()}


async def playing_states(db: AsyncSession, campaigns: Iterable[Campaign]) -> dict:
    """REPRODUZINDO em LOTE (listagem): UMA consulta para a página inteira, mesma regra do detalhe
    (``playing_state``): status ATIVA e PoP VÁLIDO com início nos últimos ``campaign.playing_window_minutes``.
    A janela (minutos) é muito menor que a retenção dos brutos (90 dias), então os eventos brutos bastam
    (o agregado diário do W-PLAY só importa para datas antigas). Devolve {campaign_id: bool}."""
    camps = list(campaigns)
    out = {c.id: False for c in camps}
    active = [c.id for c in camps if c.status == CampaignStatus.ATIVA.value]
    if not active:
        return out
    try:
        from app.models.playback import PlaybackEvent
    except Exception:  # pragma: no cover - domínio de playback ausente
        return {cid: None for cid in out}
    window = int(await get_setting(db, "campaign.playing_window_minutes"))
    since = utcnow() - timedelta(minutes=window)
    rows = (await db.execute(
        select(PlaybackEvent.campaign_id).where(PlaybackEvent.campaign_id.in_(active), PlaybackEvent.valid.is_(True),
                                                PlaybackEvent.started_at >= since)
        .group_by(PlaybackEvent.campaign_id))).scalars().all()
    for cid in rows:
        out[cid] = True
    return out


def display_status(status: str, reproducing: Optional[bool]) -> str:
    """Status exibido: REPRODUZINDO (derivado) quando ATIVA com PoP recente; senão o status armazenado."""
    return "REPRODUZINDO" if status == CampaignStatus.ATIVA.value and reproducing else status


async def playing_state(db: AsyncSession, c: Campaign) -> dict:
    """REPRODUZINDO = PoP válido nos últimos N min (W-PLAY). Sem serviço/tabela ⇒ ``None`` (não inventa)."""
    window = int(await get_setting(db, "campaign.playing_window_minutes"))
    fn = await _optional("app.modules.playback.service", "last_valid_play_at")
    last = None
    source = "unavailable"
    if fn is not None:
        last = await fn(db, c.id)
        source = "playback_service"
    else:
        try:
            from app.models.playback import PlaybackEvent

            if hasattr(PlaybackEvent, "campaign_id") and hasattr(PlaybackEvent, "valid"):
                last = (await db.execute(select(func.max(PlaybackEvent.started_at)).where(
                    PlaybackEvent.campaign_id == c.id, PlaybackEvent.valid.is_(True)))).scalar()
                source = "playback_events"
        except Exception:  # pragma: no cover
            last, source = None, "unavailable"
    if source == "unavailable":
        return {"playing": None, "last_valid_play_at": None, "source": source}
    playing = bool(last and c.status == CampaignStatus.ATIVA.value and utcnow() - last <= timedelta(minutes=window))
    return {"playing": playing, "last_valid_play_at": last, "source": source, "window_minutes": window}


# ─── pedido de alteração (pós-aprovação) ─────────────────────────────────────

async def _discard_pending_change(db: AsyncSession, ctx, c: Campaign, rev: CampaignRevision, *, status: str,
                                  reason: Optional[str]) -> None:
    now = utcnow()
    rev.status = status
    rev.decided_by, rev.decided_at, rev.decision_reason = getattr(ctx, "user_id", None), now, reason
    for ln in await links_of(db, c.id):
        if ln.pending:
            ln.removed_at = now
            ln.is_active = False
            ln.pending = False
            ln.replaces_creative_id = None
        if ln.pending_removal:
            ln.pending_removal = False
    live = await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None
    live_holds = set(revision_hold_ids(live))
    if rev.hold_id and rev.hold_id not in live_holds:
        cs = rev.change_summary or {}
        if cs.get("hold_converted"):
            # alteração PAGA e rejeitada: libera a nova contratação (a vigente fica intacta) e pede o reembolso
            await _inventory_call("release_hold", db, rev.hold_id, "REFUNDED")
            refund = None
            pid = cs.get("diff_payment_order_id")
            req = await _optional("app.modules.financial.service", "request_refund")
            if pid and req is not None:
                try:
                    async with db.begin_nested():
                        r = await req(db, ctx, uuid.UUID(pid), c.id, reason or "alteração rejeitada")
                    refund = str(getattr(r, "id", "") or "")
                except DomainError as exc:
                    if exc.code not in NON_BLOCKING_REFUND_CODES:
                        raise
                    refund = f"skipped:{exc.code}"
            rev.change_summary = {**cs, "requires_refund_review": True, "refund": refund}
        else:
            await _inventory_call("release_hold", db, rev.hold_id, "CHANGE_REJECTED")


async def _request_review_for_change(db: AsyncSession, ctx, c: Campaign, rev: CampaignRevision, *,
                                     extra: Optional[dict] = None) -> Campaign:
    values = {"pending_revision_id": rev.id, "status_before_review": c.status, **(extra or {})}
    return await _enter_review(db, ctx, c, CampaignStatus.EM_ANALISE.value, "change_requested", rev, extra=values,
                               reason="alteração pós-aprovação")
