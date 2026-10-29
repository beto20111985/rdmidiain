"""
RD Mídia Indoor — Segmentação (targeting) → conjunto de telas (A6 §10.5.2; P001 §45–§46).

Semântica (determinística):
- Inclusões geográficas/categoria: AND entre TIPOS, OR dentro do mesmo tipo
  (ex.: CITY=Goiânia + CATEGORY=Academias ⇒ academias de Goiânia).
- ESTABLISHMENT (estabelecimentos específicos) SOMA ao conjunto geográfico.
- Exclusões (qualquer tipo) removem SÓ quem casa a exclusão: estabelecimento sem o campo (bairro/CEP/
  categoria nulos) não é removido — antes, ``NOT (NULL)`` do SQL o fazia sumir (corrigido na AÇÃO 004).
- Sem nenhuma inclusão ⇒ erro (nunca "todas as telas" por omissão — A6 E17).
- Nunca TV individual: não existe alvo por tela (ETAPA2 #134/#144).
- Só telas vendáveis (``screens.sellable`` e não DESCOMISSIONADA; estabelecimento ativo,
  não excluído); tela em MANUTENÇÃO continua no inventário (ETAPA2 #1115).
- Elegibilidade de duração: bloco externo da tela ≥ duração (W-INV; padrão do ciclo).
- Orientação: telas de orientação diferente recebem letterbox (ETAPA2 #179–#181; nunca esticar).
- Conflitos: só estabelecimentos com decisão ALLOW (motor de conflitos).
- Diagnóstico de exclusão (``diagnose_exclusions``, AÇÃO 004): SÓ para a RD — contagens por motivo das
  telas que casam a segmentação e ficaram fora; nunca altera o conjunto elegível.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import and_, false, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import city_key as make_city_key
from app.core.errors import DomainError
from app.core.ids import BR_UFS
from app.core.settings_service import get_setting
from app.models.campaigns import TargetType

REGIONS = ("NORTE", "NORDESTE", "CENTRO_OESTE", "SUDESTE", "SUL")
_REGION_ALIASES = {"N": "NORTE", "NE": "NORDESTE", "CO": "CENTRO_OESTE", "SE": "SUDESTE", "S": "SUL",
                   "CENTRO-OESTE": "CENTRO_OESTE", "CENTROOESTE": "CENTRO_OESTE"}


def _slug(text: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _bad(message: str, **details) -> DomainError:
    return DomainError("TARGET_INVALID", message, status=400, details=details)


def normalize_target(target_type: str, value: Any) -> str:
    """Normaliza o valor do alvo para a forma canônica gravada em ``campaign_targets``."""
    t = (target_type or "").upper()
    if t not in TargetType.__members__:
        raise _bad("Tipo de segmentação inválido.", target_type=target_type)
    v = str(value or "").strip()
    if not v:
        raise _bad("Valor de segmentação vazio.", target_type=t)
    if t == "COUNTRY":
        v = v.upper()
        if v in ("BR", "BRASIL", "BRAZIL"):
            v = "BRA"
        if v != "BRA":
            raise _bad("País não atendido.", value=v)
        return v
    if t == "REGION":
        u = v.upper().replace(" ", "_")
        u = _REGION_ALIASES.get(u, u)
        if u not in REGIONS:
            raise _bad("Região inválida.", value=v)
        return u
    if t == "STATE":
        u = v.upper()
        if u not in BR_UFS:
            raise _bad("UF inválida.", value=v)
        return u
    if t == "CITY":
        if v.isdigit():
            if len(v) != 7:
                raise _bad("Código IBGE de município deve ter 7 dígitos.", value=v)
            return v
        uf, sep, name = v.partition(":")
        if not sep or uf.strip().upper() not in BR_UFS or not name.strip():
            raise _bad("Cidade deve ser 'UF:Nome' ou código IBGE.", value=v)
        return make_city_key(uf, name)
    if t == "NEIGHBORHOOD":
        parts = v.split(":")
        if len(parts) != 3 or parts[0].strip().upper() not in BR_UFS or not parts[1].strip() or not parts[2].strip():
            raise _bad("Bairro deve ser 'UF:Cidade:Bairro'.", value=v)
        return f"{make_city_key(parts[0], parts[1])}:{_slug(parts[2])}"
    if t == "POSTAL_CODE":
        d = re.sub(r"\D", "", v)
        if not (2 <= len(d) <= 8):
            raise _bad("CEP (ou prefixo) deve ter de 2 a 8 dígitos.", value=v)
        return d
    # CATEGORY / ESTABLISHMENT: UUID
    try:
        return str(uuid.UUID(v))
    except ValueError:
        raise _bad("Identificador inválido.", target_type=t) from None


@dataclass
class TargetSpec:
    target_type: str
    value: str
    exclude: bool = False

    def as_dict(self) -> dict:
        return {"type": self.target_type, "value": self.value, "exclude": self.exclude}


def specs_from(items: Iterable[Any]) -> list[TargetSpec]:
    out = []
    for it in items:
        if isinstance(it, TargetSpec):
            out.append(it)
        elif isinstance(it, dict):
            out.append(TargetSpec(str(it.get("type") or it.get("target_type")).upper(), str(it.get("value") or
                                  it.get("target_value")), bool(it.get("exclude", it.get("is_exclude", False)))))
        else:  # CampaignTarget
            out.append(TargetSpec(it.target_type, it.target_value, bool(it.is_exclude)))
    return out


def _conditions(specs: list[TargetSpec], E, T):
    """Monta (inclusão_geo, inclusão_estabelecimentos, exclusões) em SQLAlchemy."""
    by_type: dict[str, list[str]] = {}
    exclusions = []
    est_includes = []
    for s in specs:
        if s.exclude:
            exclusions.append(s)
        elif s.target_type == "ESTABLISHMENT":
            est_includes.append(uuid.UUID(s.value))
        else:
            by_type.setdefault(s.target_type, []).append(s.value)

    def cond(t: str, values: list[str]):
        if t == "COUNTRY":
            return E.country.in_(values)
        if t == "REGION":
            return E.region.in_(values)
        if t == "STATE":
            return E.state.in_(values)
        if t == "CITY":
            keys = [v for v in values if not v.isdigit()]
            codes = [v for v in values if v.isdigit()]
            parts = []
            if keys:
                parts.append(E.city_key.in_(keys))
            if codes:
                parts.append(E.city_ibge_code.in_(codes))
            return or_(*parts)
        if t == "NEIGHBORHOOD":
            parts = []
            for v in values:
                ck, _, nk = v.rpartition(":")
                parts.append(and_(E.city_key == ck, E.neighborhood_key == nk))
            return or_(*parts)
        if t == "POSTAL_CODE":
            return or_(*[E.postal_code.like(f"{v}%") for v in values])
        if t == "CATEGORY":
            # categoria escolhida + todas as descendentes (path materializado '/<hex>/.../')
            likes = [T.path.like(func.concat(select(T.path).where(T.id == uuid.UUID(v)).scalar_subquery(), "%"))
                     for v in values]
            return E.category_id.in_(select(T.id).where(or_(*likes)))
        if t == "ESTABLISHMENT":
            return E.id.in_([uuid.UUID(v) for v in values])
        raise _bad("Tipo de segmentação inválido.", target_type=t)

    geo = and_(*[cond(t, vals) for t, vals in sorted(by_type.items())]) if by_type else None
    est = E.id.in_(est_includes) if est_includes else None
    exc_by_type: dict[str, list[str]] = {}
    for s in exclusions:
        exc_by_type.setdefault(s.target_type, []).append(s.value)
    exc = or_(*[cond(t, vals) for t, vals in sorted(exc_by_type.items())]) if exc_by_type else None
    return geo, est, exc


def _match(specs: list[TargetSpec], E, T):
    """(inclusão, manter) da segmentação — a MESMA para ``resolve`` e ``diagnose_exclusions``.

    ``manter`` = não casa nenhuma exclusão. COALESCE(…, falso): estabelecimento sem o campo da exclusão
    (bairro/CEP/categoria nulos) NÃO casa a exclusão; sem isso ``NOT (NULL)`` é NULL e ele sumia."""
    geo, est_inc, exc = _conditions(specs, E, T)
    include = or_(*(c for c in (geo, est_inc) if c is not None)) if (geo is not None or est_inc is not None) else false()
    keep = not_(func.coalesce(exc, false())) if exc is not None else None
    return include, keep


@dataclass
class ResolvedScreen:
    screen_id: uuid.UUID
    establishment_id: uuid.UUID
    org_id: uuid.UUID
    orientation: str
    screen_class: str
    venue_class: str
    region: Optional[str]
    state: Optional[str]
    city_key: Optional[str]
    timezone: str
    external_s: int
    letterbox: bool = False
    city: Optional[str] = None  # nome de exibição (com acento) — só para a agregação por cidade


@dataclass
class Resolution:
    screens: list[ResolvedScreen] = field(default_factory=list)
    candidates: int = 0
    excluded_duration: int = 0
    excluded_conflict_establishments: dict = field(default_factory=dict)  # est_id → Decision
    conflict_screens: dict = field(default_factory=dict)  # est_id → nº de telas que saíram por conflito
    letterbox: int = 0

    @property
    def screen_ids(self) -> list[uuid.UUID]:
        return [s.screen_id for s in self.screens]


async def _external_seconds(db: AsyncSession, screen_ids: list[uuid.UUID]) -> dict:
    """Bloco externo por tela (W-INV, política de rotação); padrão = ciclo global."""
    default = int(await get_setting(db, "cycle.default_external_s"))
    try:
        inv = __import__("app.modules.inventory.service", fromlist=["external_seconds_by_screen"])
        fn = getattr(inv, "external_seconds_by_screen", None)
    except Exception:  # pragma: no cover - módulo do W-INV ausente
        fn = None
    if fn is not None and screen_ids:
        got = await fn(db, screen_ids)
        return {sid: int(got.get(sid, default)) for sid in screen_ids}
    return {sid: default for sid in screen_ids}


def validate_specs(specs: list[TargetSpec]) -> list[TargetSpec]:
    norm = []
    seen = set()
    for s in specs:
        t = s.target_type.upper()
        if t == "SCREEN":
            raise DomainError("TARGET_SCREEN_NOT_ALLOWED", "Não é permitido escolher TV individual; use segmentação.",
                              status=400)
        v = normalize_target(t, s.value)
        key = (t, v, bool(s.exclude))
        if key in seen:
            continue
        seen.add(key)
        norm.append(TargetSpec(t, v, bool(s.exclude)))
    if not any(not s.exclude for s in norm):
        raise DomainError("TARGET_REQUIRED", "Informe ao menos uma inclusão de segmentação (ex.: país, estado ou cidade).",
                          status=400)
    return norm


async def resolve(
    db: AsyncSession,
    specs: list[TargetSpec],
    *,
    duration_s: int,
    creative_orientations: Iterable[str] = (),
    campaign=None,
    check_conflicts: bool = True,
    record: bool = False,
    ctx=None,
) -> Resolution:
    from app.models.establishments import Establishment as E
    from app.models.screens import Screen
    from app.models.taxonomy import TaxonomyNode as T

    specs = validate_specs(specs)
    include, keep = _match(specs, E, T)
    q = (
        select(Screen.id, Screen.establishment_id, Screen.orientation, Screen.screen_class, E.org_id, E.venue_class,
               E.region, E.state, E.city_key, E.timezone, E.city)
        .join(E, E.id == Screen.establishment_id)
        .where(Screen.sellable.is_(True), Screen.status != "DECOMMISSIONED", E.is_active.is_(True),
               E.deleted_at.is_(None), include)
    )
    if keep is not None:
        q = q.where(keep)
    rows = (await db.execute(q.order_by(Screen.id))).all()
    res = Resolution(candidates=len(rows))
    if not rows:
        return res
    ext = await _external_seconds(db, [r[0] for r in rows])
    orientations = {o for o in creative_orientations if o}
    eligible = []
    for sid, est_id, orient, sclass, org_id, vclass, region, state, ck, tz, city in rows:
        if ext[sid] < duration_s:
            res.excluded_duration += 1
            continue
        lb = bool(orientations) and any(o != orient for o in orientations)
        eligible.append(ResolvedScreen(sid, est_id, org_id, orient, sclass or "STANDARD", vclass or "STANDARD",
                                       region, state, ck, tz or "America/Sao_Paulo", ext[sid], lb, city))
    if check_conflicts and campaign is not None and eligible:
        from app.modules.conflicts.service import evaluate_campaign_establishments

        decisions = await evaluate_campaign_establishments(
            db, campaign, {s.establishment_id for s in eligible}, record=record, ctx=ctx)
        blocked = {eid: d for eid, d in decisions.items() if not d.allowed}
        res.excluded_conflict_establishments = blocked
        for s in eligible:
            if s.establishment_id in blocked:
                res.conflict_screens[s.establishment_id] = res.conflict_screens.get(s.establishment_id, 0) + 1
        eligible = [s for s in eligible if s.establishment_id not in blocked and s.establishment_id in decisions]
    res.screens = eligible
    res.letterbox = sum(1 for s in eligible if s.letterbox)
    return res


def availability_summary(res: Resolution) -> dict:
    """Resumo SEM nomes/ids de estabelecimentos (P001 §46; ETAPA2 #142)."""
    by_state: dict[str, int] = {}
    by_city: dict[str, int] = {}
    city_name: dict[str, str] = {}  # city_key → nome de exibição (cidade não é dado sensível; nunca o estabelecimento)
    by_region: dict[str, int] = {}
    ests = set()
    for s in res.screens:
        ests.add(s.establishment_id)
        if s.state:
            by_state[s.state] = by_state.get(s.state, 0) + 1
        if s.city_key:
            by_city[s.city_key] = by_city.get(s.city_key, 0) + 1
            if s.city and (s.city_key not in city_name or s.city < city_name[s.city_key]):
                city_name[s.city_key] = s.city  # determinístico: a menor grafia cadastrada para a chave
        if s.region:
            by_region[s.region] = by_region.get(s.region, 0) + 1
    return {
        "screens_available": len(res.screens),
        "establishments_count": len(ests),
        "screens_letterbox": res.letterbox,
        "excluded_by_duration": res.excluded_duration,
        "excluded_by_conflict_establishments": len(res.excluded_conflict_establishments),
        "by_region": [{"region": k, "screens": v} for k, v in sorted(by_region.items())],
        "by_state": [{"state": k, "screens": v} for k, v in sorted(by_state.items())],
        "by_city": [{"city_key": k, "city": city_name.get(k), "screens": v} for k, v in sorted(by_city.items())],
    }


# ─── Diagnóstico de exclusão — SÓ para a RD (AÇÃO 004, item 14: nada de filtro silencioso) ────────────────

OUT_OF_INVENTORY_REASONS = ("ESTABLISHMENT_INACTIVE", "AWAITING_CONTRACT", "SCREEN_INACTIVE", "NOT_SELLABLE")


def out_of_inventory_reason(screen_status: str, est_active: bool, est_deleted: bool, onboarding_status: str) -> str:
    """Motivo (um só, por precedência) de uma tela que casa a segmentação mas está FORA do inventário vendável.

    Derivado dos CAMPOS que ``screens.service.compute_sellable`` usa, sem recalcular a regra (a tela já saiu
    pelo ``screens.sellable``/estabelecimento): estabelecimento inativo/excluído › aguardando o aceite do
    contrato de parceria › tela fora de ACTIVE/MAINTENANCE › outro (``sellable`` falso sem nenhum dos três)."""
    from app.models.establishments import OnboardingStatus
    from app.models.screens import ScreenStatus

    if est_deleted or not est_active:
        return "ESTABLISHMENT_INACTIVE"
    if onboarding_status != OnboardingStatus.ACTIVE.value:
        return "AWAITING_CONTRACT"
    if screen_status not in (ScreenStatus.ACTIVE.value, ScreenStatus.MAINTENANCE.value):
        return "SCREEN_INACTIVE"
    return "NOT_SELLABLE"


def _by_code(decisions: dict, screens_per_est: dict) -> list[dict]:
    """Decisões BLOCK por ``reason_code`` → [{reason_code, screens, establishments}] (sem ids)."""
    ests: dict[str, int] = {}
    scr: dict[str, int] = {}
    for eid, d in decisions.items():
        ests[d.reason_code] = ests.get(d.reason_code, 0) + 1
        scr[d.reason_code] = scr.get(d.reason_code, 0) + screens_per_est.get(eid, 0)
    return [{"reason_code": k, "screens": scr[k], "establishments": ests[k]} for k in sorted(ests)]


async def diagnose_exclusions(db: AsyncSession, specs: list[TargetSpec], res: Resolution, *, campaign=None) -> dict:
    """Para a MESMA segmentação de ``res``: quantas telas casariam e ficaram fora, por motivo e na ordem do
    funil (inventário vendável → duração do bloco → conflito). SOMENTE CONTAGENS — sem nomes/ids de
    estabelecimento ou tela (para a RD, os nomes seguem no painel de conflitos). Não altera ``res`` nem regra.

    ``out_of_inventory.also_conflict``: das telas fora do inventário, quantas AINDA cairiam por conflito se
    entrassem no inventário (motor de conflitos com ``record=False`` — só orienta o próximo passo)."""
    from app.models.establishments import Establishment as E
    from app.models.screens import Screen, ScreenStatus
    from app.models.taxonomy import TaxonomyNode as T

    include, keep = _match(validate_specs(specs), E, T)
    q = (
        select(Screen.status, Screen.establishment_id, E.is_active, E.deleted_at, E.onboarding_status)
        .join(E, E.id == Screen.establishment_id)
        .where(Screen.status != ScreenStatus.DECOMMISSIONED.value, include,
               not_(and_(Screen.sellable.is_(True), E.is_active.is_(True), E.deleted_at.is_(None))))
    )
    if keep is not None:
        q = q.where(keep)
    screens: dict[str, int] = {}
    ests: dict[str, set] = {}
    per_est: dict = {}
    for status, est_id, active, deleted_at, onboarding in (await db.execute(q)).all():
        reason = out_of_inventory_reason(status, bool(active), deleted_at is not None, onboarding)
        screens[reason] = screens.get(reason, 0) + 1
        ests.setdefault(reason, set()).add(est_id)
        per_est[est_id] = per_est.get(est_id, 0) + 1
    also: list[dict] = []
    if campaign is not None and per_est:
        from app.modules.conflicts.service import evaluate_campaign_establishments

        decisions = await evaluate_campaign_establishments(db, campaign, set(per_est), record=False)
        also = _by_code({e: d for e, d in decisions.items() if not d.allowed}, per_est)
    ooi = sum(screens.values())
    conflict = sum(res.conflict_screens.values())
    return {
        "screens_total": ooi + res.excluded_duration + conflict,
        "out_of_inventory": {
            "screens": ooi,
            "establishments": len(per_est),
            "by_reason": [{"reason": r, "screens": screens[r], "establishments": len(ests[r])}
                          for r in OUT_OF_INVENTORY_REASONS if r in screens],
            "also_conflict": also,
        },
        "duration": {"screens": res.excluded_duration},
        "conflict": {"screens": conflict, "establishments": len(res.excluded_conflict_establishments),
                     "by_reason": _by_code(res.excluded_conflict_establishments, res.conflict_screens)},
    }
