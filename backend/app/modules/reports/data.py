"""
RD Mídia Indoor — Fontes de dados dos relatórios (AÇÃO 002 / W-BIZ).

Fontes (W-PLAY, estáveis): ``playback_daily_stats`` (agregados diários preservados após a
retenção dos brutos — base de TODOS os relatórios históricos de exibição) e
``playback_events`` (brutos ≤ 90 dias — usados para horário/falhas e para o comprovante,
somente ``valid = true``). Exibição ≠ pessoas: audiência é estimativa separada.

Público × visibilidade (ETAPA2 #721–#772, P001 §101–§102):
- EQUIPE RD (``reports.view`` GLOBAL/CITY): tudo no seu escopo.
- ANUNCIANTE (``reports.view`` na organização anunciante): só as próprias campanhas;
  telas por CÓDIGO + REGIÃO (UF) — nunca nome/endereço/cidade do estabelecimento.
- ESTABELECIMENTO (``reports.view`` na organização/estabelecimento dono das telas): só as
  suas telas; exibições por tipo (externo/interno) — nunca anunciante, campanha ou ID
  do anúncio (#732–#734).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import Integer, and_, case, cast, distinct, func, literal, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext
from app.core.errors import DomainError, NotFound
from app.core.timeutils import DEFAULT_TIMEZONE, local_date, utcnow
from app.modules.reports.exporters import Column, Dataset

MAX_PERIOD_DAYS = 366
RAW_RETENTION_DAYS = 90
STAFF, ADVERTISER, ESTABLISHMENT = "EQUIPE", "ANUNCIANTE", "ESTABELECIMENTO"
REGION_LABEL = {"NORTE": "Norte", "NORDESTE": "Nordeste", "CENTRO_OESTE": "Centro-Oeste", "SUDESTE": "Sudeste",
                "SUL": "Sul"}
KIND_LABEL = {"EXTERNAL": "Externo (anúncios)", "INTERNAL": "Interno (estabelecimento)", "INSTITUTIONAL": "Institucional RD",
              "FALLBACK": "Padrão RD", "EMERGENCY": "Emergência"}


@dataclass
class Scope:
    audience: str
    org_id: Optional[uuid.UUID] = None  # anunciante ou dono das telas
    establishment_ids: Optional[set] = None  # restrição ESTABLISHMENT
    city_keys: Optional[set] = None  # equipe com escopo CITY
    financial: bool = False
    label: str = ""

    @property
    def is_staff(self) -> bool:
        return self.audience == STAFF


@dataclass
class Period:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def parse_period(start: Optional[date], end: Optional[date]) -> Period:
    today = local_date(utcnow(), DEFAULT_TIMEZONE)
    end = end or today
    start = start or (end.replace(day=1))
    if end < start:
        raise DomainError("PERIOD_INVALID", "Data final anterior à inicial.", status=400)
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise DomainError("PERIOD_TOO_LONG", f"Período máximo de {MAX_PERIOD_DAYS} dias.", status=400)
    return Period(start, end)


def resolve_scope(ctx: AuthContext, *, audience: Optional[str], org_id: Optional[uuid.UUID]) -> Scope:
    """Decide o público do relatório a partir das permissões (nunca do pedido do cliente)."""
    fin = ctx.can("reports.financial", global_only=True)
    if audience in (None, STAFF):
        if ctx.can("reports.view", global_only=True):
            return Scope(STAFF, financial=fin, label="Equipe interna")
        cities = ctx.city_keys("reports.view")
        if cities:
            return Scope(STAFF, city_keys=set(cities), financial=False, label="Equipe interna (regional)")
        if audience == STAFF:
            raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403)
    if org_id is None:
        raise DomainError("ORG_REQUIRED", "Informe a organização do relatório.", status=400)
    if audience == ADVERTISER or audience is None:
        if ctx.can("reports.view", org_id=org_id):
            return Scope(ADVERTISER if audience == ADVERTISER else _guess(ctx, org_id), org_id=org_id)
        raise NotFound()
    if audience == ESTABLISHMENT:
        if ctx.can("reports.view", org_id=org_id):
            return Scope(ESTABLISHMENT, org_id=org_id)
        ests = ctx.establishment_ids("reports.view")
        if ests:
            return Scope(ESTABLISHMENT, org_id=org_id, establishment_ids=set(ests))
        raise NotFound()
    raise DomainError("AUDIENCE_INVALID", "Público inválido.", status=400)


def _guess(ctx: AuthContext, org_id) -> str:
    # perfis de cliente: estabelecimento quando tem permissão de comissão (dono de telas)
    return ESTABLISHMENT if ctx.can("commissions.view", org_id=org_id) else ADVERTISER


# ─── Tabelas ─────────────────────────────────────────────────────────────────

def _t():
    from app.models.campaigns import Campaign, Creative
    from app.models.establishments import Establishment
    from app.models.organizations import Organization
    from app.models.playback import PlaybackDailyStat, PlaybackEvent
    from app.models.screens import Screen

    return PlaybackDailyStat, PlaybackEvent, Screen, Establishment, Campaign, Creative, Organization


def _scope_conds(scope: Scope, PDS_or_PE, Establishment, Campaign) -> list:
    conds = []
    if scope.audience == ADVERTISER:
        conds.append(Campaign.org_id == scope.org_id)
        conds.append(PDS_or_PE.kind == "EXTERNAL")
    elif scope.audience == ESTABLISHMENT:
        if scope.establishment_ids:
            conds.append(PDS_or_PE.establishment_id.in_(list(scope.establishment_ids)))
        else:
            conds.append(Establishment.org_id == scope.org_id)
    elif scope.city_keys:
        conds.append(Establishment.city_key.in_(list(scope.city_keys)))
    return conds


ALLOWED = {
    STAFF: {"campanha", "anuncio", "tela", "estabelecimento", "cidade", "regiao", "dia", "horario", "tipo", "falhas",
            "uptime", "sincronizacao", "downloads", "sincronizacoes"},
    ADVERTISER: {"campanha", "anuncio", "tela", "regiao", "dia"},
    ESTABLISHMENT: {"tela", "dia", "horario", "tipo", "falhas", "uptime", "sincronizacao", "downloads",
                    "sincronizacoes"},
}


def check_allowed(scope: Scope, group: str) -> None:
    if group not in ALLOWED[scope.audience]:
        raise DomainError("REPORT_NOT_AVAILABLE", "Relatório não disponível para o seu perfil.", status=403,
                          details={"report": group, "audience": scope.audience})


def _meta(scope: Scope, p: Period, extra: Optional[list] = None) -> list:
    out = [("Período", f"{p.start.strftime('%d/%m/%Y')} a {p.end.strftime('%d/%m/%Y')} (dia local da tela)"),
           ("Público", scope.label or scope.audience.title()),
           ("Gerado em", utcnow().strftime("%d/%m/%Y %H:%M UTC"))]
    return out + (extra or [])


EXHIBITION_NOTE = ("Exibições = reproduções comprovadas por proof-of-play VÁLIDO (100% da duração). Exibição não é "
                   "pessoa: audiência é estimativa separada. A programação planejada nunca é contada como execução.")


# ─── Exibições (agregados diários) ───────────────────────────────────────────

async def exhibitions(db: AsyncSession, scope: Scope, p: Period, group: str, *, campaign_id: Any = None,
                      screen_id: Any = None, kind: Optional[str] = None, limit: int = 5000) -> Dataset:
    check_allowed(scope, group)
    PDS, _PE, Screen, Establishment, Campaign, Creative, Organization = _t()
    base_conds = [PDS.local_day >= p.start, PDS.local_day <= p.end] + _scope_conds(scope, PDS, Establishment, Campaign)
    if campaign_id is not None:
        if scope.audience == ESTABLISHMENT:
            raise DomainError("REPORT_NOT_AVAILABLE", "Filtro indisponível para o seu perfil.", status=403)
        base_conds.append(PDS.campaign_id == campaign_id)
    if screen_id is not None:
        base_conds.append(PDS.screen_id == screen_id)
    if kind is not None:
        base_conds.append(PDS.kind == kind)
    valid = func.coalesce(func.sum(PDS.plays_valid), 0).label("valid")
    invalid = func.coalesce(func.sum(PDS.plays_invalid + PDS.plays_rejected), 0).label("invalid")
    valid_ms = func.coalesce(func.sum(PDS.valid_ms), 0).label("valid_ms")
    screens_n = func.count(distinct(PDS.screen_id)).label("screens")
    j = (select().select_from(PDS)
         .join(Screen, Screen.id == PDS.screen_id)
         .outerjoin(Establishment, Establishment.id == PDS.establishment_id)
         .outerjoin(Campaign, Campaign.id == PDS.campaign_id))

    if group == "campanha":
        q = j.add_columns(Campaign.id, Campaign.name, Campaign.org_id, valid, invalid, valid_ms, screens_n,
                          func.min(PDS.local_day), func.max(PDS.local_day)) \
             .where(*base_conds, PDS.campaign_id.is_not(None)).group_by(Campaign.id, Campaign.name, Campaign.org_id)
        rows = (await db.execute(q.order_by(valid.desc()).limit(limit))).all()
        names = await _org_names(db, {r[2] for r in rows}) if scope.is_staff else {}
        cols = [Column("campaign", "Campanha")] + ([Column("advertiser", "Anunciante")] if scope.is_staff else []) + [
            Column("valid", "Exibições válidas", "int"), Column("invalid", "Inválidas/rejeitadas", "int"),
            Column("valid_hours", "Horas válidas"), Column("screens", "Telas", "int"),
            Column("first_day", "Primeiro dia", "date"), Column("last_day", "Último dia", "date")]
        data = [{"campaign": r[1], "advertiser": names.get(r[2]), "valid": int(r[3]), "invalid": int(r[4]),
                 "valid_hours": _hours(r[5]), "screens": int(r[6]), "first_day": r[7], "last_day": r[8]} for r in rows]
        title = "Exibições por campanha"
    elif group == "anuncio":
        q = j.add_columns(PDS.creative_code, Campaign.name, valid, invalid, valid_ms, screens_n) \
             .where(*base_conds).group_by(PDS.creative_code, Campaign.name)
        rows = (await db.execute(q.order_by(valid.desc()).limit(limit))).all()
        cols = [Column("creative_code", "Anúncio (ID)"), Column("campaign", "Campanha"),
                Column("valid", "Exibições válidas", "int"), Column("invalid", "Inválidas/rejeitadas", "int"),
                Column("valid_hours", "Horas válidas"), Column("screens", "Telas", "int")]
        data = [{"creative_code": r[0], "campaign": r[1], "valid": int(r[2]), "invalid": int(r[3]),
                 "valid_hours": _hours(r[4]), "screens": int(r[5])} for r in rows]
        title = "Exibições por anúncio"
    elif group == "tela":
        q = j.add_columns(Screen.friendly_id, Screen.name, Establishment.name, Establishment.city, Establishment.state,
                          Establishment.region, valid, invalid, valid_ms) \
             .where(*base_conds).group_by(Screen.friendly_id, Screen.name, Establishment.name, Establishment.city,
                                          Establishment.state, Establishment.region)
        rows = (await db.execute(q.order_by(Screen.friendly_id).limit(limit))).all()
        if scope.audience == ADVERTISER:  # somente código + região (#767–#769)
            cols = [Column("screen", "Tela (código)"), Column("region", "Região"), Column("valid", "Exibições válidas", "int"),
                    Column("valid_hours", "Horas válidas")]
            data = [{"screen": r[0], "region": _region(r[4], r[5]), "valid": int(r[6]), "valid_hours": _hours(r[8])}
                    for r in rows]
        else:
            cols = [Column("screen", "Tela (código)"), Column("screen_name", "Nome da tela")]
            if scope.is_staff:
                cols += [Column("establishment", "Estabelecimento"), Column("city", "Cidade/UF")]
            cols += [Column("valid", "Exibições válidas", "int"), Column("invalid", "Inválidas/rejeitadas", "int"),
                     Column("valid_hours", "Horas válidas")]
            data = [{"screen": r[0], "screen_name": r[1], "establishment": r[2], "city": f"{r[3]}/{r[4]}" if r[3] else None,
                     "valid": int(r[6]), "invalid": int(r[7]), "valid_hours": _hours(r[8])} for r in rows]
        title = "Exibições por tela"
    elif group == "estabelecimento":
        q = j.add_columns(Establishment.name, Establishment.city, Establishment.state, valid, invalid, valid_ms,
                          screens_n).where(*base_conds) \
             .group_by(Establishment.name, Establishment.city, Establishment.state)
        rows = (await db.execute(q.order_by(valid.desc()).limit(limit))).all()
        cols = [Column("establishment", "Estabelecimento"), Column("city", "Cidade/UF"), Column("screens", "Telas", "int"),
                Column("valid", "Exibições válidas", "int"), Column("invalid", "Inválidas/rejeitadas", "int"),
                Column("valid_hours", "Horas válidas")]
        data = [{"establishment": r[0], "city": f"{r[1]}/{r[2]}", "screens": int(r[6]), "valid": int(r[3]),
                 "invalid": int(r[4]), "valid_hours": _hours(r[5])} for r in rows]
        title = "Exibições por estabelecimento"
    elif group == "cidade":
        q = j.add_columns(Establishment.city, Establishment.state, valid, invalid, screens_n,
                          func.count(distinct(PDS.establishment_id))).where(*base_conds) \
             .group_by(Establishment.city, Establishment.state)
        rows = (await db.execute(q.order_by(valid.desc()).limit(limit))).all()
        cols = [Column("city", "Cidade/UF"), Column("establishments", "Estabelecimentos", "int"),
                Column("screens", "Telas", "int"), Column("valid", "Exibições válidas", "int"),
                Column("invalid", "Inválidas/rejeitadas", "int")]
        data = [{"city": f"{r[0]}/{r[1]}", "establishments": int(r[5]), "screens": int(r[4]), "valid": int(r[2]),
                 "invalid": int(r[3])} for r in rows]
        title = "Exibições por cidade"
    elif group == "regiao":
        q = j.add_columns(Establishment.state, Establishment.region, valid, screens_n).where(*base_conds) \
             .group_by(Establishment.state, Establishment.region)
        rows = (await db.execute(q.order_by(Establishment.state).limit(limit))).all()
        cols = [Column("region", "Região (UF)"), Column("screens", "Telas", "int"), Column("valid", "Exibições válidas", "int")]
        data = [{"region": _region(r[0], r[1]), "screens": int(r[3]), "valid": int(r[2])} for r in rows]
        title = "Exibições por região"
    elif group == "dia":
        q = j.add_columns(PDS.local_day, valid, invalid, valid_ms, screens_n).where(*base_conds).group_by(PDS.local_day)
        rows = (await db.execute(q.order_by(PDS.local_day).limit(limit))).all()
        cols = [Column("day", "Dia", "date"), Column("valid", "Exibições válidas", "int")] + (
            [] if scope.audience == ADVERTISER else [Column("invalid", "Inválidas/rejeitadas", "int")]) + [
            Column("valid_hours", "Horas válidas"), Column("screens", "Telas com exibição", "int")]
        data = [{"day": r[0], "valid": int(r[1]), "invalid": int(r[2]), "valid_hours": _hours(r[3]),
                 "screens": int(r[4])} for r in rows]
        title = "Exibições por dia"
    elif group == "tipo":
        q = j.add_columns(PDS.kind, valid, valid_ms, screens_n).where(*base_conds).group_by(PDS.kind)
        rows = (await db.execute(q.order_by(PDS.kind).limit(limit))).all()
        cols = [Column("kind", "Tipo de conteúdo"), Column("valid", "Exibições válidas", "int"),
                Column("valid_hours", "Horas válidas"), Column("screens", "Telas", "int")]
        data = [{"kind": KIND_LABEL.get(r[0], r[0]), "valid": int(r[1]), "valid_hours": _hours(r[2]),
                 "screens": int(r[3])} for r in rows]
        title = "Exibições por tipo de conteúdo"
    elif group == "horario":
        return await by_hour(db, scope, p, campaign_id=campaign_id, screen_id=screen_id)
    elif group == "falhas":
        return await failures(db, scope, p)
    elif group == "uptime":
        return await uptime(db, scope, p)
    elif group == "sincronizacao":
        return await sync_status(db, scope)
    elif group == "downloads":
        return await downloads(db, scope, p)
    elif group == "sincronizacoes":
        return await sync_history(db, scope, p)
    else:  # pragma: no cover
        raise DomainError("REPORT_UNKNOWN", "Relatório inexistente.", status=404)
    total = sum(d.get("valid", 0) for d in data)
    return Dataset(title=title, columns=cols, rows=data,
                   meta=_meta(scope, p, [("Total de exibições válidas", f"{total:,}".replace(",", "."))]),
                   notes=[EXHIBITION_NOTE])


def _hours(ms: Any) -> str:
    ms = int(ms or 0)
    return f"{ms / 3_600_000:.2f}".replace(".", ",")


def _region(uf: Optional[str], region: Optional[str]) -> str:
    return f"{uf} ({REGION_LABEL.get(region or '', region or '—')})" if uf else "—"


async def _org_names(db: AsyncSession, ids: set) -> dict:
    from app.models.organizations import Organization

    ids = {i for i in ids if i}
    if not ids:
        return {}
    return dict((await db.execute(select(Organization.id, Organization.name).where(Organization.id.in_(ids)))).all())


# ─── Brutos (≤ 90 dias): horário e falhas ────────────────────────────────────

def _raw_period(p: Period) -> tuple[Period, Optional[str]]:
    oldest = local_date(utcnow(), DEFAULT_TIMEZONE) - timedelta(days=RAW_RETENTION_DAYS - 1)
    if p.start >= oldest:
        return p, None
    return Period(max(p.start, oldest), p.end), (
        f"Eventos brutos são mantidos por {RAW_RETENTION_DAYS} dias; o período foi limitado a partir de "
        f"{oldest.strftime('%d/%m/%Y')}.")


async def by_hour(db: AsyncSession, scope: Scope, p: Period, *, campaign_id: Any = None, screen_id: Any = None) -> Dataset:
    check_allowed(scope, "horario")
    _PDS, PE, Screen, Establishment, Campaign, _C, _O = _t()
    rp, note = _raw_period(p)
    hour = cast(func.extract("hour", func.timezone(Establishment.timezone, PE.started_at)), Integer).label("h")
    conds = [PE.local_day >= rp.start, PE.local_day <= rp.end, PE.valid.is_(True)] + \
        _scope_conds(scope, PE, Establishment, Campaign)
    if campaign_id is not None and scope.audience != ESTABLISHMENT:
        conds.append(PE.campaign_id == campaign_id)
    if screen_id is not None:
        conds.append(PE.screen_id == screen_id)
    q = (select(hour, func.count()).select_from(PE)
         .join(Establishment, Establishment.id == PE.establishment_id)
         .outerjoin(Campaign, Campaign.id == PE.campaign_id)
         .where(*conds).group_by(hour).order_by(hour))
    counts = dict((await db.execute(q)).all())
    rows = [{"hour": f"{h:02d}:00–{h:02d}:59", "valid": int(counts.get(h, 0))} for h in range(24)]
    return Dataset(title="Exibições válidas por horário (local)",
                   columns=[Column("hour", "Horário"), Column("valid", "Exibições válidas", "int")],
                   rows=rows, meta=_meta(scope, rp), notes=[EXHIBITION_NOTE] + ([note] if note else []))


async def failures(db: AsyncSession, scope: Scope, p: Period) -> Dataset:
    check_allowed(scope, "falhas")
    _PDS, PE, Screen, Establishment, Campaign, _C, _O = _t()
    rp, note = _raw_period(p)
    conds = [PE.local_day >= rp.start, PE.local_day <= rp.end, PE.valid.is_(False)] + \
        _scope_conds(scope, PE, Establishment, Campaign)
    reason = func.coalesce(PE.reason, PE.result).label("reason")
    q = (select(reason, PE.status, func.count(), func.count(distinct(PE.screen_id))).select_from(PE)
         .outerjoin(Establishment, Establishment.id == PE.establishment_id)
         .outerjoin(Campaign, Campaign.id == PE.campaign_id)
         .where(*conds).group_by(reason, PE.status).order_by(func.count().desc()))
    rows = [{"reason": r[0], "status": "Rejeitado" if r[1] == "REJECTED" else "Aceito, não válido",
             "count": int(r[2]), "screens": int(r[3])} for r in (await db.execute(q)).all()]
    return Dataset(title="Falhas de exibição (eventos não válidos)",
                   columns=[Column("reason", "Motivo"), Column("status", "Situação"), Column("count", "Eventos", "int"),
                            Column("screens", "Telas", "int")],
                   rows=rows, meta=_meta(scope, rp), notes=([note] if note else []))


# ─── Telas: uptime e sincronização ───────────────────────────────────────────

def _screen_conds(scope: Scope, Screen, Establishment) -> list:
    conds = [Screen.status != "DECOMMISSIONED"]
    if scope.audience == ESTABLISHMENT:
        if scope.establishment_ids:
            conds.append(Screen.establishment_id.in_(list(scope.establishment_ids)))
        else:
            conds.append(Establishment.org_id == scope.org_id)
    elif scope.city_keys:
        conds.append(Establishment.city_key.in_(list(scope.city_keys)))
    return conds


async def uptime(db: AsyncSession, scope: Scope, p: Period) -> Dataset:
    check_allowed(scope, "uptime")
    from app.core.timeutils import end_of_local_day_utc, start_of_local_day_utc
    from app.models.devices import Device, DeviceTelemetry

    PDS, _PE, Screen, Establishment, _Camp, _C, _O = _t()
    start_utc = start_of_local_day_utc(p.start, DEFAULT_TIMEZONE)
    end_utc = min(end_of_local_day_utc(p.end, DEFAULT_TIMEZONE), utcnow())
    span_s = max(1, int((end_utc - start_utc).total_seconds()))
    samples = (select(DeviceTelemetry.screen_id.label("sid"), func.count().label("n"))
               .where(DeviceTelemetry.recorded_at >= start_utc, DeviceTelemetry.recorded_at <= end_utc)
               .group_by(DeviceTelemetry.screen_id).subquery())
    played = (select(PDS.screen_id.label("sid"), func.count(distinct(PDS.local_day)).label("d"))
              .where(PDS.local_day >= p.start, PDS.local_day <= p.end, PDS.plays_valid > 0)
              .group_by(PDS.screen_id).subquery())
    q = (select(Screen.friendly_id, Screen.name, Establishment.name, Device.presence, Device.last_seen_at,
                func.coalesce(samples.c.n, 0), func.coalesce(played.c.d, 0))
         .select_from(Screen).join(Establishment, Establishment.id == Screen.establishment_id)
         .outerjoin(Device, Device.id == Screen.device_id)
         .outerjoin(samples, samples.c.sid == Screen.id).outerjoin(played, played.c.sid == Screen.id)
         .where(*_screen_conds(scope, Screen, Establishment)).order_by(Screen.friendly_id).limit(5000))
    rows = []
    for code, name, est, presence, last_seen, n, d in (await db.execute(q)).all():
        bps = min(10000, int(n) * 300 * 10000 // span_s)
        rows.append({"screen": code, "screen_name": name, "establishment": est if scope.is_staff else None,
                     "presence": "Online" if presence == "online" else "Offline", "last_seen": last_seen,
                     "uptime_bps": bps, "days_with_plays": int(d), "period_days": p.days})
    cols = [Column("screen", "Tela"), Column("screen_name", "Nome")] + (
        [Column("establishment", "Estabelecimento")] if scope.is_staff else []) + [
        Column("presence", "Agora"), Column("last_seen", "Último sinal", "datetime"),
        Column("uptime_bps", "Uptime estimado", "bps"), Column("days_with_plays", "Dias com exibição válida", "int"),
        Column("period_days", "Dias no período", "int")]
    return Dataset(title="Uptime das telas", columns=cols, rows=rows, meta=_meta(scope, p),
                   notes=["Uptime estimado pelas amostras de telemetria do player (1 a cada 5 min quando conectado); "
                          "fora da retenção de telemetria o valor fica subestimado."])


async def sync_status(db: AsyncSession, scope: Scope) -> Dataset:
    check_allowed(scope, "sincronizacao")
    from app.models.devices import Device

    _PDS, _PE, Screen, Establishment, _Camp, _C, _O = _t()
    q = (select(Screen.friendly_id, Screen.name, Establishment.name, Device.applied_manifest_version,
                Device.pending_manifest_version, Device.last_sync_at, Device.presence, Device.app_version)
         .select_from(Screen).join(Establishment, Establishment.id == Screen.establishment_id)
         .outerjoin(Device, Device.id == Screen.device_id)
         .where(*_screen_conds(scope, Screen, Establishment)).order_by(Screen.friendly_id).limit(5000))
    rows = []
    for code, name, est, applied, pending, last_sync, presence, app_version in (await db.execute(q)).all():
        status = "Sem player" if presence is None else ("Sincronizando" if pending is not None else
                                                        ("Sincronizada" if applied is not None else "Nunca sincronizou"))
        rows.append({"screen": code, "screen_name": name, "establishment": est if scope.is_staff else None,
                     "status": status, "applied": applied, "pending": pending, "last_sync": last_sync,
                     "app_version": app_version})
    cols = [Column("screen", "Tela"), Column("screen_name", "Nome")] + (
        [Column("establishment", "Estabelecimento")] if scope.is_staff else []) + [
        Column("status", "Sincronização"), Column("applied", "Programação aplicada (versão)", "int"),
        Column("pending", "Programação pendente (versão)", "int"), Column("last_sync", "Última sincronização", "datetime"),
        Column("app_version", "Versão do player")]
    return Dataset(title="Sincronização das telas", columns=cols, rows=rows,
                   meta=[("Gerado em", utcnow().strftime("%d/%m/%Y %H:%M UTC"))])


# ─── Downloads e sincronizações (W-PLAY ``device_sync_events``; ETAPA2 #798–#799) ──

SYNC_NOTE = ("Fonte: histórico de sincronização dos players (W-PLAY) — eventos gravados nas TRANSIÇÕES "
             "(programação recebida/aplicada/falha, download iniciado/concluído, erro de mídia). Retenção "
             "configurável (player.sync_events_retention_days).")


async def _sync_filter(db: AsyncSession, scope: Scope) -> tuple[Optional[list], Any]:
    """(screen_ids, org_id) para o ``sync_report`` do W-PLAY conforme o escopo do leitor."""
    _PDS, _PE, Screen, Establishment, _Camp, _C, _O = _t()
    if scope.audience == ESTABLISHMENT:
        if scope.establishment_ids:
            ids = list((await db.execute(select(Screen.id).where(
                Screen.establishment_id.in_(list(scope.establishment_ids))))).scalars())
            return ids, None
        return None, scope.org_id  # org da tela NA DATA do evento
    if scope.city_keys:
        ids = list((await db.execute(select(Screen.id).join(Establishment, Establishment.id == Screen.establishment_id)
                                     .where(Establishment.city_key.in_(list(scope.city_keys))))).scalars())
        return ids, None
    return None, None


def _bounds(p: Period):
    from app.core.timeutils import start_of_local_day_utc

    return start_of_local_day_utc(p.start, DEFAULT_TIMEZONE), start_of_local_day_utc(p.end + timedelta(days=1),
                                                                                      DEFAULT_TIMEZONE)


def _mb(b: Any) -> str:
    return f"{int(b or 0) / (1024 * 1024):.1f}".replace(".", ",")


def _sec(ms: Any) -> Optional[str]:
    return None if ms is None else f"{int(ms) / 1000:.1f}".replace(".", ",")


async def downloads(db: AsyncSession, scope: Scope, p: Period) -> Dataset:
    check_allowed(scope, "downloads")
    from app.modules.playback.syncevents import sync_report

    _PDS, _PE, Screen, Establishment, _Camp, _C, _O = _t()
    screen_ids, org_id = await _sync_filter(db, scope)
    start, end = _bounds(p)
    rep = await sync_report(db, screen_ids=screen_ids, org_id=org_id, start=start, end=end, group_by="screen",
                            tz=DEFAULT_TIMEZONE)
    keys = [uuid.UUID(g["key"]) for g in rep["groups"] if g.get("key")]
    names = {}
    if keys:
        names = {sid: (code, sname, est) for sid, code, sname, est in (await db.execute(
            select(Screen.id, Screen.friendly_id, Screen.name, Establishment.name)
            .join(Establishment, Establishment.id == Screen.establishment_id).where(Screen.id.in_(keys)))).all()}
    rows = []
    for g in rep["groups"]:
        if not g.get("key"):
            continue
        sid = uuid.UUID(g["key"])
        code, sname, est = names.get(sid, ("—", None, None))
        c, t = g["counts"], g["time_to_apply_ms"]
        rows.append({"screen": code, "screen_name": sname, "establishment": est if scope.is_staff else None,
                     "dl_started": c.get("DOWNLOAD_STARTED", 0), "dl_completed": c.get("DOWNLOAD_COMPLETED", 0),
                     "volume_mb": _mb(g.get("bytes_downloaded")), "received": c.get("MANIFEST_RECEIVED", 0),
                     "applied": c.get("MANIFEST_APPLIED", 0), "failed": c.get("MANIFEST_FAILED", 0),
                     "media_errors": c.get("MEDIA_ERROR", 0), "p50_s": _sec(t.get("p50")), "p95_s": _sec(t.get("p95"))})
    rows.sort(key=lambda r: r["screen"])
    cols = [Column("screen", "Tela"), Column("screen_name", "Nome")] + (
        [Column("establishment", "Estabelecimento")] if scope.is_staff else []) + [
        Column("dl_started", "Downloads iniciados", "int"), Column("dl_completed", "Downloads concluídos", "int"),
        Column("volume_mb", "Volume baixado (MB)"), Column("received", "Programações recebidas", "int"),
        Column("applied", "Programações aplicadas", "int"), Column("failed", "Falhas de programação", "int"),
        Column("media_errors", "Erros de mídia", "int"), Column("p50_s", "Tempo até aplicar p50 (s)"),
        Column("p95_s", "Tempo até aplicar p95 (s)")]
    extra = [("Downloads concluídos", str(sum(r["dl_completed"] for r in rows))),
             ("Volume total baixado (MB)", _mb(sum(int(g.get("bytes_downloaded") or 0) for g in rep["groups"])))]
    if scope.is_staff:  # mídia com erro identificada só para a equipe (pode ser de anunciante)
        extra.append(("Mídias distintas com erro", str(len(rep["media_failures"]))))
    return Dataset(title="Downloads de mídia por tela", columns=cols, rows=rows, meta=_meta(scope, p, extra),
                   notes=[SYNC_NOTE])


async def sync_history(db: AsyncSession, scope: Scope, p: Period) -> Dataset:
    check_allowed(scope, "sincronizacoes")
    from app.modules.playback.syncevents import sync_report

    screen_ids, org_id = await _sync_filter(db, scope)
    start, end = _bounds(p)
    rep = await sync_report(db, screen_ids=screen_ids, org_id=org_id, start=start, end=end, group_by="day",
                            tz=DEFAULT_TIMEZONE)
    from datetime import date as _date

    rows = []
    for g in rep["groups"]:
        if not g.get("key"):
            continue
        c, t = g["counts"], g["time_to_apply_ms"]
        rows.append({"day": _date.fromisoformat(g["key"]), "received": c.get("MANIFEST_RECEIVED", 0),
                     "applied": c.get("MANIFEST_APPLIED", 0), "failed": c.get("MANIFEST_FAILED", 0),
                     "dl_completed": c.get("DOWNLOAD_COMPLETED", 0), "volume_mb": _mb(g.get("bytes_downloaded")),
                     "media_errors": c.get("MEDIA_ERROR", 0), "avg_s": _sec(t.get("avg")), "p95_s": _sec(t.get("p95"))})
    cols = [Column("day", "Dia", "date"), Column("received", "Programações recebidas", "int"),
            Column("applied", "Programações aplicadas", "int"), Column("failed", "Falhas de programação", "int"),
            Column("dl_completed", "Downloads concluídos", "int"), Column("volume_mb", "Volume baixado (MB)"),
            Column("media_errors", "Erros de mídia", "int"), Column("avg_s", "Tempo médio até aplicar (s)"),
            Column("p95_s", "Tempo até aplicar p95 (s)")]
    return Dataset(title="Sincronizações por dia", columns=cols, rows=rows, meta=_meta(scope, p), notes=[SYNC_NOTE])


# ─── Financeiro (W-FIN) ──────────────────────────────────────────────────────

async def commission(db: AsyncSession, ctx: AuthContext, scope: Scope, p: Period, *, group_by: str = "day") -> Dataset:
    from app.models.establishments import Establishment
    from app.modules.financial import service as fin

    if scope.audience == ADVERTISER:
        raise DomainError("REPORT_NOT_AVAILABLE", "Relatório não disponível para o seu perfil.", status=403)
    est_ids: Optional[set] = None
    if scope.audience == ESTABLISHMENT:
        if not ctx.can("commissions.view", org_id=scope.org_id):
            raise NotFound()
        est_ids = scope.establishment_ids or set(
            (await db.execute(select(Establishment.id).where(Establishment.org_id == scope.org_id))).scalars())
    elif not (ctx.can("commissions.view", global_only=True) or scope.financial):
        raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403,
                          details={"permission": "commissions.view"})
    if scope.audience == ESTABLISHMENT and group_by not in ("day", "month", "screen"):
        # o estabelecimento não vê campanha/anunciante (#732–#734)
        raise DomainError("REPORT_NOT_AVAILABLE", "Agrupamento indisponível para o seu perfil.", status=403)
    rows = await fin.commission_report(db, establishment_ids=est_ids, start=p.start, end=p.end, group_by=group_by)
    rows = [{k: v for k, v in r.items() if k != "amount_ucents"} for r in rows]
    keys = list(rows[0].keys()) if rows else [{"day": "local_day", "month": "competence"}.get(group_by, group_by),
                                              "valid_plays", "expected_plays", "amount_cents", "status"]
    cols = [Column(k, k.replace("_cents", "").replace("_", " ").capitalize(),
                   "money" if k.endswith("_cents") else ("int" if k.endswith("_plays") else "text"))
            for k in keys]
    return Dataset(title="Comissão", columns=cols, rows=rows, meta=_meta(scope, p),
                   notes=["Comissão apurada sobre exibições válidas (pool padrão de 5% da receita elegível)."])


async def financial(db: AsyncSession, scope: Scope, p: Period) -> Dataset:
    from app.modules.financial import service as fin

    if not (scope.is_staff and scope.financial):
        raise DomainError("FORBIDDEN", "Relatório financeiro exige reports.financial.", status=403,
                          details={"permission": "reports.financial"})
    ov = await fin.financial_overview(db, p.start, p.end)
    rev = await fin.recognized_revenue(db, p.start, p.end)
    contracted = await fin.contracted_revenue(db, p.start, p.end)
    flat = {"receita_reconhecida_cents": rev, **{f"contratado_{k}": v for k, v in contracted.items()}}
    for k, v in (ov or {}).items():
        if isinstance(v, int) and not isinstance(v, bool):
            flat[k] = v
    rows = [{"metric": k, "value": v} for k, v in flat.items()]
    return Dataset(title="Financeiro", columns=[Column("metric", "Indicador"), Column("value", "Valor (centavos)", "int")],
                   rows=rows, meta=_meta(scope, p), notes=["Valores em centavos de real (inteiros)."])


REPORTS = {
    "exibicoes_por_campanha": ("campanha", "Exibições por campanha"),
    "exibicoes_por_anuncio": ("anuncio", "Exibições por anúncio"),
    "exibicoes_por_tela": ("tela", "Exibições por tela"),
    "exibicoes_por_estabelecimento": ("estabelecimento", "Exibições por estabelecimento"),
    "exibicoes_por_cidade": ("cidade", "Exibições por cidade"),
    "exibicoes_por_regiao": ("regiao", "Exibições por região (UF)"),
    "exibicoes_por_dia": ("dia", "Exibições por dia"),
    "exibicoes_por_horario": ("horario", "Exibições por horário"),
    "exibicoes_por_tipo": ("tipo", "Exibições por tipo de conteúdo"),
    "falhas": ("falhas", "Falhas de exibição"),
    "uptime": ("uptime", "Uptime das telas"),
    "sincronizacao": ("sincronizacao", "Sincronização das telas"),
    "downloads": ("downloads", "Downloads de mídia por tela"),
    "sincronizacoes_por_dia": ("sincronizacoes", "Sincronizações por dia"),
    "comissao": ("comissao", "Comissão"),
    "financeiro": ("financeiro", "Financeiro (receita)"),
}


async def build(db: AsyncSession, ctx: AuthContext, key: str, scope: Scope, p: Period, params: dict) -> Dataset:
    if key not in REPORTS:
        raise NotFound("REPORT_UNKNOWN", "Relatório inexistente.")
    group = REPORTS[key][0]
    if group == "comissao":
        return await commission(db, ctx, scope, p, group_by=params.get("group_by") or "day")
    if group == "financeiro":
        return await financial(db, scope, p)
    cid = params.get("campaign_id")
    if cid is not None:
        await _check_campaign(db, ctx, scope, cid)
    return await exhibitions(db, scope, p, group, campaign_id=cid, screen_id=params.get("screen_id"),
                             kind=params.get("kind"))


async def _check_campaign(db: AsyncSession, ctx: AuthContext, scope: Scope, campaign_id: Any) -> None:
    from app.models.campaigns import Campaign

    c = await db.get(Campaign, campaign_id)
    if c is None:
        raise NotFound()
    if scope.audience == ADVERTISER and c.org_id != scope.org_id:
        raise NotFound()


def available_reports(scope: Scope) -> list[dict]:
    out = []
    for key, (group, label) in REPORTS.items():
        if group in ("comissao",):
            ok = scope.audience != ADVERTISER
        elif group == "financeiro":
            ok = scope.is_staff and scope.financial
        else:
            ok = group in ALLOWED[scope.audience]
        if ok:
            out.append({"key": key, "label": label})
    return out
