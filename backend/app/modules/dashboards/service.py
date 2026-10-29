"""
RD Mídia Indoor — Dashboards agregados por perfil (AÇÃO 002 / W-BIZ).

Requisitos: ETAPA2 #704–#778; P001 §100–§102; DS §10–§12.

- Consultas AGREGADAS (COUNT/SUM com índices: ``playback_daily_stats`` por dia/tela/org,
  presença dos dispositivos, status de campanhas) — nada de varrer eventos brutos.
- Sem polling: o frontend invalida estas consultas pelos eventos WS (``screen.status``,
  ``campaign.updated``, ``payment.updated``, ``media.processing``, ``approval.pending``…).
- Admin: telas online/offline, campanhas ativas, exibições, anunciantes ativos, receita,
  comissão pendente, telas desatualizadas, aguardando aprovação, mídias com erro, alertas,
  faturamento do mês, projeção, distribuição por estado (+ capacidade em rota própria).
  NÃO: mapa, cidade, top locais/campanhas, ocupação (#712–#717).
- Estabelecimento: suas telas/status/sincronização, externos e internos (quantidade),
  comissão, saldo, exibições, desempenho (e "você deixou de receber ~R$ X por tela
  offline" quando há base). SEM dados comerciais do anunciante e SEM saque (#729–#741).
- Anunciante: campanhas, anúncios, faturas, telas contratadas, período, custo, aprovação,
  comprovantes, regiões que exibiram — sem nomes/endereços e sem reproduções em tempo
  real (#749–#772).
"""
from __future__ import annotations

import calendar
import uuid
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import and_, distinct, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext
from app.core.errors import DomainError, NotFound
from app.core.settings_service import get_setting
from app.core.timeutils import DEFAULT_TIMEZONE, isoformat_z, local_date, utcnow

ACTIVE_CAMPAIGN = ("ATIVA",)


def _today() -> date:
    return local_date(utcnow(), DEFAULT_TIMEZONE)


PERIOD_NOTE = ("Indicadores de ESTADO (telas online/offline, campanhas ativas, aguardando aprovação, telas "
               "desatualizadas, alertas) são do momento atual; indicadores de FLUXO (exibições, receita, "
               "faturamento, comissão, erros de mídia, distribuição de exibições) usam o período.")


def resolve_period(start: Optional[date], end: Optional[date]) -> tuple[date, date, bool]:
    """Período dos widgets (DS §9/§12): dias LOCAIS America/Sao_Paulo. Sem parâmetros = mês corrente até hoje
    (comportamento padrão). Máx. 366 dias."""
    from app.modules.reports.data import parse_period

    p = parse_period(start, end)
    return p.start, p.end, (start is not None or end is not None)


def _period_out(start: date, end: date, explicit: bool) -> dict:
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": DEFAULT_TIMEZONE, "explicit": explicit,
            "note": PERIOD_NOTE}


def _local_bounds(start: date, end: date):
    from app.core.timeutils import start_of_local_day_utc

    return start_of_local_day_utc(start, DEFAULT_TIMEZONE), start_of_local_day_utc(end + timedelta(days=1),
                                                                                   DEFAULT_TIMEZONE)


def _models():
    from app.models.campaigns import Campaign, CampaignRevision, Creative
    from app.models.devices import Device
    from app.models.establishments import Establishment
    from app.models.media import MediaFile
    from app.models.playback import PlaybackDailyStat
    from app.models.screens import Screen

    return Campaign, CampaignRevision, Creative, Device, Establishment, MediaFile, PlaybackDailyStat, Screen


# ─── Admin (equipe RD) ───────────────────────────────────────────────────────

async def admin(db: AsyncSession, ctx: AuthContext, start: Optional[date] = None, end: Optional[date] = None) -> dict:
    Campaign, _Rev, _Cr, Device, Establishment, MediaFile, PDS, Screen = _models()
    if not ctx.can("dashboard.view", global_only=True):
        cities = ctx.city_keys("dashboard.view")
        if not cities or not ctx.is_rd_staff:
            raise DomainError("FORBIDDEN", "Painel exclusivo da equipe RD.", status=403)
    else:
        cities = None
    city_cond = Establishment.city_key.in_(list(cities)) if cities else true()
    today = _today()
    month_start = today.replace(day=1)
    p_start, p_end, explicit = resolve_period(start, end)

    scr = (await db.execute(
        select(func.count(Screen.id),
               func.count(Screen.device_id),
               func.count().filter(Device.presence == "online"),
               func.count().filter(Device.pending_manifest_version.is_not(None)))
        .select_from(Screen).join(Establishment, Establishment.id == Screen.establishment_id)
        .outerjoin(Device, Device.id == Screen.device_id)
        .where(Screen.status != "DECOMMISSIONED", city_cond))).one()
    total_screens, paired, online, outdated = (int(x or 0) for x in scr)
    offline_days = (await get_setting(db, "player.offline_alert_days"))[0]
    offline_long = int((await db.execute(
        select(func.count()).select_from(Screen).join(Establishment, Establishment.id == Screen.establishment_id)
        .join(Device, Device.id == Screen.device_id)
        .where(Screen.status != "DECOMMISSIONED", city_cond, Device.presence != "online",
               or_(Device.last_seen_at.is_(None), Device.last_seen_at < utcnow() - timedelta(days=offline_days))))).scalar_one())

    camp_q = select(Campaign.status, func.count(), func.count(distinct(Campaign.org_id))).group_by(Campaign.status)
    if cities:
        camp_q = camp_q.where(Campaign.id.in_(
            select(PDS.campaign_id).join(Establishment, Establishment.id == PDS.establishment_id)
            .where(city_cond, PDS.local_day >= p_start, PDS.local_day <= p_end)))
    camp = {s: (int(n), int(o)) for s, n, o in (await db.execute(camp_q)).all()}
    advertisers_active = int((await db.execute(
        select(func.count(distinct(Campaign.org_id))).where(Campaign.status.in_(ACTIVE_CAMPAIGN)))).scalar_one()) \
        if not cities else camp.get("ATIVA", (0, 0))[1]

    lo, hi = min(month_start, p_start), max(today, p_end)
    plays = (await db.execute(
        select(func.coalesce(func.sum(PDS.plays_valid).filter(PDS.local_day == today), 0),
               func.coalesce(func.sum(PDS.plays_valid).filter(PDS.local_day >= month_start, PDS.local_day <= today), 0),
               func.coalesce(func.sum(PDS.plays_valid).filter(PDS.local_day >= p_start, PDS.local_day <= p_end), 0))
        .select_from(PDS).outerjoin(Establishment, Establishment.id == PDS.establishment_id)
        .where(PDS.local_day >= lo, PDS.local_day <= hi, city_cond))).one()
    advertisers_period = int((await db.execute(
        select(func.count(distinct(Campaign.org_id))).select_from(PDS).join(Campaign, Campaign.id == PDS.campaign_id)
        .outerjoin(Establishment, Establishment.id == PDS.establishment_id)
        .where(PDS.local_day >= p_start, PDS.local_day <= p_end, PDS.plays_valid > 0, PDS.kind == "EXTERNAL",
               city_cond))).scalar_one())

    media_errors = int((await db.execute(select(func.count()).select_from(MediaFile).where(
        MediaFile.status.in_(("REJECTED", "FAILED")), MediaFile.created_at >= utcnow() - timedelta(days=30)))).scalar_one())
    b0, b1 = _local_bounds(p_start, p_end)
    media_errors_period = int((await db.execute(select(func.count()).select_from(MediaFile).where(
        MediaFile.status.in_(("REJECTED", "FAILED")), MediaFile.created_at >= b0, MediaFile.created_at < b1))).scalar_one())
    plays_by_state = dict((await db.execute(
        select(Establishment.state, func.coalesce(func.sum(PDS.plays_valid), 0)).select_from(PDS)
        .join(Establishment, Establishment.id == PDS.establishment_id)
        .where(PDS.local_day >= p_start, PDS.local_day <= p_end, city_cond).group_by(Establishment.state))).all())

    by_state = [{"uf": uf, "screens": int(n), "online": int(on), "exhibitions_period": int(plays_by_state.get(uf, 0))}
                for uf, n, on in (await db.execute(
        select(Establishment.state, func.count(Screen.id), func.count().filter(Device.presence == "online"))
        .select_from(Screen).join(Establishment, Establishment.id == Screen.establishment_id)
        .outerjoin(Device, Device.id == Screen.device_id)
        .where(Screen.status != "DECOMMISSIONED", city_cond)
        .group_by(Establishment.state).order_by(Establishment.state))).all()]

    tickets_breached = 0
    try:
        from app.models.support import Ticket

        tickets_breached = int((await db.execute(select(func.count()).select_from(Ticket).where(
            Ticket.status.in_(("ABERTO", "EM_ATENDIMENTO", "AGUARDANDO_CLIENTE")), Ticket.sla_due_at < utcnow()))).scalar_one())
    except Exception:  # pragma: no cover
        pass

    awaiting = camp.get("EM_ANALISE", (0, 0))[0]
    alerts = [a for a in (
        {"type": "telas_offline_prolongado", "label": f"Telas offline há mais de {offline_days} dias", "count": offline_long},
        {"type": "midias_com_erro", "label": "Mídias com erro (período)" if explicit else "Mídias com erro (30 dias)",
         "count": media_errors_period if explicit else media_errors},
        {"type": "campanhas_aguardando_aprovacao", "label": "Campanhas aguardando aprovação", "count": awaiting},
        {"type": "chamados_sla_estourado", "label": "Chamados com SLA estourado", "count": tickets_breached},
        {"type": "telas_desatualizadas", "label": "Telas com programação pendente", "count": outdated},
    ) if a["count"]]

    out = {
        "generated_at": isoformat_z(utcnow()),
        "scope": {"cities": sorted(cities) if cities else None},
        "period": _period_out(p_start, p_end, explicit),
        "kpis": {
            "screens_online": online, "screens_total": total_screens, "players_paired": paired,
            "players_offline": max(0, paired - online), "campaigns_active": camp.get("ATIVA", (0, 0))[0],
            "exhibitions_today": int(plays[0]), "exhibitions_month": int(plays[1]),
            "exhibitions_period": int(plays[2]), "advertisers_active": advertisers_active,
            "advertisers_exhibited_period": advertisers_period,
        },
        "operations": {"screens_outdated": outdated, "campaigns_awaiting_approval": awaiting,
                       "media_errors_30d": media_errors, "media_errors_period": media_errors_period,
                       "screens_offline_long": offline_long},
        "alerts": alerts,
        "distribution_by_state": by_state,
        "capacity_url": "/api/v1/dashboards/admin/capacity",
        "finance": None,
    }
    if ctx.can("finance.view", global_only=True) or ctx.can("reports.financial", global_only=True):
        out["finance"] = await _admin_finance(db, month_start, today, p_start, p_end, explicit)
    return out


async def _admin_finance(db: AsyncSession, month_start: date, today: date, p_start: date, p_end: date,
                         explicit: bool) -> dict:
    from app.modules.financial import service as fin

    revenue = await fin.recognized_revenue(db, month_start, today)
    billed = await fin.contracted_revenue(db, month_start, today)
    ov = await fin.financial_overview(db, month_start, today)
    if explicit:
        rev_p = await fin.recognized_revenue(db, p_start, p_end)
        billed_p = await fin.contracted_revenue(db, p_start, p_end)
        total_days = (p_end - p_start).days + 1
        if p_start <= today <= p_end:  # período em curso (inclui o último dia)
            elapsed = (today - p_start).days + 1
            projection = rev_p * total_days // elapsed
            method = f"Linear: receita reconhecida em {elapsed} de {total_days} dias do período."
        elif p_end < today:
            projection, method = rev_p, "Período encerrado: projeção = realizado."
        else:
            projection, method = None, "Período futuro: sem base para projeção."
    else:
        rev_p, billed_p = revenue, billed
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        elapsed = (today - month_start).days + 1
        projection = revenue * days_in_month // elapsed if elapsed else None
        method = f"Linear: receita reconhecida em {elapsed} de {days_in_month} dias do mês."
    return {
        "revenue_month_cents": revenue,
        "billing_month_cents": billed["paid_cents"],
        "revenue_period_cents": rev_p,
        "billing_period_cents": billed_p["paid_cents"],
        "pending_receivables_cents": billed["pending_cents"],
        "commission_pending_cents": int(ov.get("commission_payable_cents", 0)),
        "revenue_projection_cents": projection,
        "projection_method": method,
    }


CAPACITY_NOTE = "Espaço livre para anúncios hoje nas telas disponíveis para venda, em blocos de 30 segundos."


async def admin_capacity(db: AsyncSession, ctx: AuthContext, day: Optional[date] = None) -> dict:
    """Capacidade DISPONÍVEL (livre) hoje — W-INV ``availability`` (sem anunciantes, sem ocupação).

    Contrato: ``{date, screens, free_units, unit: "SLOT_30S", unit_seconds: 30, free_seconds, truncated, note}``.
    NÃO expõe ocupação nem capacidade total/usada (de onde a ocupação seria derivada) — E2#717."""
    _C, _R, _Cr, _D, Establishment, _M, _P, Screen = _models()
    if not ctx.can("dashboard.view", global_only=True):
        raise DomainError("FORBIDDEN", "Painel exclusivo da equipe RD.", status=403)
    from app.modules.inventory.service import MAX_AVAIL_SCREENS, availability

    d = day or _today()
    ids = list((await db.execute(select(Screen.id).where(Screen.status != "DECOMMISSIONED", Screen.sellable.is_(True))
                                 .order_by(Screen.id).limit(MAX_AVAIL_SCREENS + 1))).scalars())
    truncated = len(ids) > MAX_AVAIL_SCREENS
    unit_s, screens, free = 30, 0, 0
    if ids:
        av = await availability(db, screen_ids=ids[:MAX_AVAIL_SCREENS], date_from=d, date_to=d)
        day_row = (av.get("days") or [{}])[0]
        unit_s = int(av.get("unit_seconds", 30))
        screens = int(av.get("sellable_screens", 0))
        free = int(day_row.get("free_units", 0))
    return {"date": d.isoformat(), "screens": screens, "free_units": free, "unit": "SLOT_30S", "unit_seconds": unit_s,
            "free_seconds": free * unit_s, "truncated": truncated, "note": CAPACITY_NOTE}


# ─── Estabelecimento ─────────────────────────────────────────────────────────

async def establishment(db: AsyncSession, ctx: AuthContext, org_id: uuid.UUID, start: Optional[date] = None,
                        end: Optional[date] = None) -> dict:
    Campaign, _Rev, _Cr, Device, Establishment, _M, PDS, Screen = _models()
    if not ctx.can("dashboard.view", org_id=org_id):
        ests_scope = ctx.establishment_ids("dashboard.view")
        if not ests_scope:
            raise NotFound()
    else:
        ests_scope = None
    est_q = select(Establishment).where(Establishment.org_id == org_id)
    if ests_scope is not None:
        est_q = est_q.where(Establishment.id.in_(list(ests_scope)))
    ests = list((await db.execute(est_q.order_by(Establishment.name))).scalars())
    if not ests:
        raise NotFound()
    est_ids = [e.id for e in ests]
    today = _today()
    month_start = today.replace(day=1)
    p_start, p_end, explicit = resolve_period(start, end)
    screens = []
    for sc, dev in (await db.execute(
        select(Screen, Device).outerjoin(Device, Device.id == Screen.device_id)
        .where(Screen.establishment_id.in_(est_ids), Screen.status != "DECOMMISSIONED").order_by(Screen.friendly_id))).all():
        screens.append({
            "id": str(sc.id), "code": sc.friendly_id, "name": sc.name, "establishment_id": str(sc.establishment_id),
            "status": sc.status, "online": bool(dev and dev.presence == "online"),
            "last_seen_at": isoformat_z(dev.last_seen_at) if dev and dev.last_seen_at else None,
            "sync": None if dev is None else ("SINCRONIZANDO" if dev.pending_manifest_version is not None else
                                              ("SINCRONIZADA" if dev.applied_manifest_version is not None else "NUNCA")),
        })
    kinds = {k: (int(v), int(c)) for k, v, c in (await db.execute(
        select(PDS.kind, func.coalesce(func.sum(PDS.plays_valid), 0), func.count(distinct(PDS.creative_id)))
        .where(PDS.establishment_id.in_(est_ids), PDS.local_day >= month_start, PDS.local_day <= today)
        .group_by(PDS.kind))).all()}
    today_kinds = {k: int(c) for k, c in (await db.execute(
        select(PDS.kind, func.count(distinct(PDS.creative_id)))
        .where(PDS.establishment_id.in_(est_ids), PDS.local_day == today, PDS.plays_valid > 0)
        .group_by(PDS.kind))).all()}
    if explicit:
        pk = {k: int(v) for k, v in (await db.execute(
            select(PDS.kind, func.coalesce(func.sum(PDS.plays_valid), 0))
            .where(PDS.establishment_id.in_(est_ids), PDS.local_day >= p_start, PDS.local_day <= p_end)
            .group_by(PDS.kind))).all()}
    else:
        pk = {k: v for k, (v, _c) in kinds.items()}
    finance = await _establishment_finance(db, est_ids, month_start, today, p_start, p_end, explicit)
    online = sum(1 for s in screens if s["online"])
    return {
        "generated_at": isoformat_z(utcnow()),
        "org_id": str(org_id),
        "period": _period_out(p_start, p_end, explicit),
        "establishments": [{"id": str(e.id), "name": e.name, "city": e.city, "state": e.state} for e in ests],
        "screens": screens,
        "kpis": {"screens_total": len(screens), "screens_online": online, "screens_offline": len(screens) - online,
                 "exhibitions_month": sum(v for v, _ in kinds.values()),
                 "external_exhibitions_month": kinds.get("EXTERNAL", (0, 0))[0],
                 "internal_exhibitions_month": kinds.get("INTERNAL", (0, 0))[0],
                 "exhibitions_period": sum(pk.values()), "external_exhibitions_period": pk.get("EXTERNAL", 0),
                 "internal_exhibitions_period": pk.get("INTERNAL", 0)},
        "programming": {"external_ads_running_today": today_kinds.get("EXTERNAL", 0),
                        "internal_contents_today": today_kinds.get("INTERNAL", 0),
                        "note": "Quantidades apenas — sem identificação de anunciantes (política comercial)."},
        "commission": finance,
    }


async def _establishment_finance(db: AsyncSession, est_ids: list, month_start: date, today: date, p_start: date,
                                 p_end: date, explicit: bool) -> Optional[dict]:
    try:
        from app.modules.financial import service as fin
    except ImportError:  # pragma: no cover
        return None
    bal = {"pending_cents": 0, "available_cents": 0, "accumulated_cents": 0}
    for eid in est_ids:
        b = await fin.establishment_balance(db, eid)
        bal["pending_cents"] += int(b.get("pending_provisional_cents", 0)) + int(b.get("pending_closed_cents", 0))
        bal["available_cents"] += int(b.get("available_cents", 0))
        bal["accumulated_cents"] += int(b.get("accumulated_cents", 0))
    month = await fin.commission_report(db, establishment_ids=set(est_ids), start=month_start, end=today, group_by="month")
    month_amount = sum(int(r.get("amount_cents", 0)) for r in month)
    month_valid = sum(int(r.get("valid_plays", 0)) for r in month)
    month_expected = sum(int(r.get("expected_plays", 0)) for r in month)
    if explicit:
        per = await fin.commission_report(db, establishment_ids=set(est_ids), start=p_start, end=p_end, group_by="day")
        valid = sum(int(r.get("valid_plays", 0)) for r in per)
        expected = sum(int(r.get("expected_plays", 0)) for r in per)
        amount = sum(int(r.get("amount_cents", 0)) for r in per)
    else:
        valid, expected, amount = month_valid, month_expected, month_amount
    performance_bps = (valid * 10000 // expected) if expected else None
    lost = None
    if expected > valid and valid > 0 and amount > 0:
        lost = (expected - valid) * amount // valid
    return {
        **bal, "commission_month_cents": month_amount, "valid_plays_month": month_valid,
        "expected_plays_month": month_expected,
        "commission_period_cents": amount, "valid_plays_period": valid, "expected_plays_period": expected,
        "performance_bps": performance_bps,
        "estimated_lost_cents": lost,
        "estimated_lost_note": ("Estimativa: exibições contratadas que não ocorreram (tela offline/sem sincronizar) × "
                                "valor médio por exibição válida no período.") if lost is not None else None,
        "withdrawals": "Saques ficam em Financeiro → Saques (não no painel).",
    }


# ─── Anunciante ──────────────────────────────────────────────────────────────

async def advertiser(db: AsyncSession, ctx: AuthContext, org_id: uuid.UUID, start: Optional[date] = None,
                     end: Optional[date] = None) -> dict:
    Campaign, CampaignRevision, Creative, _D, Establishment, _M, PDS, Screen = _models()
    if not ctx.can("dashboard.view", org_id=org_id) or not ctx.can("campaigns.view", org_id=org_id):
        raise NotFound()
    p_start, p_end, explicit = resolve_period(start, end)
    camp_q = select(Campaign).where(Campaign.org_id == org_id)
    if explicit:  # campanhas cujo período se sobrepõe ao período escolhido (ou ainda sem datas)
        camp_q = camp_q.where(or_(Campaign.start_date.is_(None), Campaign.end_date.is_(None),
                                  and_(Campaign.start_date <= p_end, Campaign.end_date >= p_start)))
    camps = list((await db.execute(camp_q.order_by(Campaign.created_at.desc()).limit(200))).scalars())
    rev_ids = [c.live_revision_id for c in camps if getattr(c, "live_revision_id", None)]
    screens_by_rev = {}
    if rev_ids:
        screens_by_rev = {r.id: len(r.screen_ids or []) for r in (await db.execute(
            select(CampaignRevision).where(CampaignRevision.id.in_(rev_ids)))).scalars()}
    regions = [{"uf": uf, "screens": int(n)} for uf, n in (await db.execute(
        select(Establishment.state, func.count(distinct(PDS.screen_id))).select_from(PDS)
        .join(Campaign, Campaign.id == PDS.campaign_id).join(Establishment, Establishment.id == PDS.establishment_id)
        .where(Campaign.org_id == org_id, PDS.plays_valid > 0, PDS.kind == "EXTERNAL",
               *([PDS.local_day >= p_start, PDS.local_day <= p_end] if explicit else []))
        .group_by(Establishment.state).order_by(Establishment.state))).all()]
    creatives = [{"code": c.code, "name": c.name, "status": c.status} for c in (await db.execute(
        select(Creative).where(Creative.org_id == org_id).order_by(Creative.created_at.desc()).limit(200))).scalars()]
    invoices = []
    try:
        from app.modules.financial import service as fin

        invoices = await fin.advertiser_invoices(db, org_id, limit=50)
    except ImportError:  # pragma: no cover
        pass
    from app.models.reports import DeliveryProof

    proofs = [{"id": str(p.id), "code": p.code, "campaign_id": str(p.campaign_id), "period_start": p.period_start.isoformat(),
               "period_end": p.period_end.isoformat(), "total_valid_plays": p.total_valid_plays,
               "pdf_url": f"/api/v1/reports/proofs/{p.id}/pdf"}
              for p in (await db.execute(select(DeliveryProof).where(
                  DeliveryProof.org_id == org_id,
                  *([DeliveryProof.period_start <= p_end, DeliveryProof.period_end >= p_start] if explicit else []))
                  .order_by(DeliveryProof.created_at.desc()).limit(50))).scalars()]
    by_status: dict = {}
    for c in camps:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    return {
        "generated_at": isoformat_z(utcnow()),
        "org_id": str(org_id),
        "period": _period_out(p_start, p_end, explicit),
        "kpis": {"campaigns_active": by_status.get("ATIVA", 0), "campaigns_awaiting_approval": by_status.get("EM_ANALISE", 0),
                 "campaigns_total": len(camps), "creatives_total": len(creatives)},
        "campaigns": [{"id": str(c.id), "name": c.name, "status": c.status,
                       "start_date": c.start_date.isoformat() if c.start_date else None,
                       "end_date": c.end_date.isoformat() if c.end_date else None,
                       "contracted_screens": screens_by_rev.get(getattr(c, "live_revision_id", None)),
                       "cost_cents": int(c.contracted_price_cents or 0),
                       "approval": {"approved_at": isoformat_z(c.approved_at) if c.approved_at else None,
                                    "rejection_reason": c.rejection_reason}} for c in camps],
        "creatives": creatives,
        "invoices": invoices,
        "proofs": proofs,
        "regions_exhibited": regions,
        "notes": ["Telas exibidas apenas por região (UF); nomes e endereços de estabelecimentos não são divulgados.",
                  "Contagem de exibições disponível no comprovante de veiculação (proof-of-play válido)."],
    }
