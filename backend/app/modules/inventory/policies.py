"""
RD Mídia Indoor — Resolução e gestão das políticas de rotação (W-INV; ADR_006 §2).

- Escopo mais específico vence: TELA > ESTABELECIMENTO > PLANO_PARCEIRO > GLOBAL.
- A política de um dia local de uma tela é a vigente na MEIA-NOITE LOCAL daquele dia
  (mudança vale a partir do primeiro dia local completo da tela após ``effective_from``).
- Sem política GLOBAL cadastrada usa-se o padrão de ``cycle.default_internal_s`` /
  ``cycle.default_external_s`` (settings), sem ``policy_id``.
- Nova versão: só para o futuro; fecha a versão aberta do mesmo escopo; é BLOQUEADA
  se reduzir a capacidade abaixo do já contratado (reempacota as telas afetadas).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, DomainError
from app.core.settings_service import get_setting
from app.core.timeutils import start_of_local_day_utc
from app.models.rotation import SCOPE_RANK, RotationPolicy
from app.modules.inventory.sources import ScreenInfo
from app.modules.scheduler.policy import DEFAULT_ROTATION, PolicyError, RotationSpec

log = structlog.get_logger("rd.inventory.policies")


def spec_of(row: RotationPolicy) -> RotationSpec:
    return RotationSpec(row.internal_seconds, row.external_seconds, bool(row.allow_external_overflow),
                        int(row.overflow_max_seconds), str(row.id))


async def default_spec(db: AsyncSession) -> RotationSpec:
    i = await get_setting(db, "cycle.default_internal_s")
    e = await get_setting(db, "cycle.default_external_s")
    try:
        return RotationSpec(int(i), int(e))
    except PolicyError:
        log.error("default_cycle_setting_invalid")
        return DEFAULT_ROTATION


@dataclass
class _Row:
    scope: str
    scope_id: Optional[uuid.UUID]
    effective_from: datetime
    effective_to: Optional[datetime]
    spec: RotationSpec


class PolicyResolver:
    """Carrega as políticas relevantes UMA vez e resolve por (tela, dia local)."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self._rows: list[_Row] = []
        self._default: Optional[RotationSpec] = None
        self._plan_specs: dict = {}
        self._loaded = False

    async def prepare(self, screens: Iterable[ScreenInfo]) -> "PolicyResolver":
        scr = list(screens)
        ids_s = {s.id for s in scr}
        ids_e = {s.establishment_id for s in scr}
        ids_p = {s.partner_plan_id for s in scr if s.partner_plan_id}
        conds = [RotationPolicy.scope == "GLOBAL"]
        if ids_s:
            conds.append(and_(RotationPolicy.scope == "SCREEN", RotationPolicy.scope_id.in_(ids_s)))
        if ids_e:
            conds.append(and_(RotationPolicy.scope == "ESTABLISHMENT", RotationPolicy.scope_id.in_(ids_e)))
        if ids_p:
            conds.append(and_(RotationPolicy.scope == "PARTNER_PLAN", RotationPolicy.scope_id.in_(ids_p)))
        rows = (await self.db.execute(select(RotationPolicy).where(or_(*conds)))).scalars().all()
        self._rows = [_Row(r.scope, r.scope_id, r.effective_from, r.effective_to, spec_of(r)) for r in rows]
        # rotação sugerida do plano de parceiro (W-ORG ``partner_plans``) — base do escopo
        # PARTNER_PLAN quando não há versão explícita em rotation_policies
        self._plan_specs = {}
        if ids_p:
            try:
                from app.models.contracts import PartnerPlan

                for pp in (await self.db.execute(select(PartnerPlan).where(PartnerPlan.id.in_(ids_p)))).scalars():
                    if pp.default_internal_s is None or pp.default_external_s is None:
                        continue
                    try:
                        self._plan_specs[pp.id] = RotationSpec(int(pp.default_internal_s), int(pp.default_external_s))
                    except PolicyError:
                        log.error("partner_plan_rotation_invalid", partner_plan_id=str(pp.id))
            except ImportError:  # pragma: no cover
                pass
        self._default = await default_spec(self.db)
        self._loaded = True
        return self

    def for_instant(self, screen: ScreenInfo, at: datetime) -> RotationSpec:
        return self.for_instant_scope(screen, at)[0]

    def for_day_scope(self, screen: ScreenInfo, d: date) -> tuple[RotationSpec, Optional[str]]:
        return self.for_instant_scope(screen, start_of_local_day_utc(d, screen.timezone))

    def for_instant_scope(self, screen: ScreenInfo, at: datetime) -> tuple[RotationSpec, Optional[str]]:
        assert self._loaded, "PolicyResolver.prepare() não chamado"
        best: Optional[_Row] = None
        for r in self._rows:
            if not (r.effective_from <= at and (r.effective_to is None or at < r.effective_to)):
                continue
            if r.scope == "SCREEN" and r.scope_id != screen.id:
                continue
            if r.scope == "ESTABLISHMENT" and r.scope_id != screen.establishment_id:
                continue
            if r.scope == "PARTNER_PLAN" and (screen.partner_plan_id is None or r.scope_id != screen.partner_plan_id):
                continue
            if best is None or SCOPE_RANK[r.scope] > SCOPE_RANK[best.scope]:
                best = r
        plan_spec = self._plan_specs.get(screen.partner_plan_id) if screen.partner_plan_id else None
        if plan_spec is not None and (best is None or SCOPE_RANK[best.scope] < SCOPE_RANK["PARTNER_PLAN"]):
            return plan_spec, "PARTNER_PLAN"
        if best is None:
            return self._default, None  # type: ignore[return-value]
        return best.spec, best.scope

    def for_day(self, screen: ScreenInfo, d: date) -> RotationSpec:
        return self.for_instant(screen, start_of_local_day_utc(d, screen.timezone))

    def next_change_after(self, screen: ScreenInfo, d_from: date, days: int) -> Optional[date]:
        """Primeiro dia local (> d_from, até d_from+days) com política diferente da de d_from."""
        base = self.for_day(screen, d_from).key()
        for i in range(1, days + 1):
            d = d_from + timedelta(days=i)
            if self.for_day(screen, d).key() != base:
                return d
        return None


async def resolve_for_screens(db: AsyncSession, screens: Iterable[ScreenInfo]) -> PolicyResolver:
    return await PolicyResolver(db).prepare(screens)


def policy_dict(r: RotationPolicy) -> dict:
    spec = spec_of(r)
    return {
        "id": str(r.id),
        "scope": r.scope,
        "scope_id": str(r.scope_id) if r.scope_id else None,
        "version": r.version,
        "effective_from": r.effective_from,
        "effective_to": r.effective_to,
        "reason": r.reason,
        "created_by": str(r.created_by) if r.created_by else None,
        "created_at": r.created_at,
        **spec.summary(),
    }


async def create_policy(
    db: AsyncSession,
    ctx: Any,
    *,
    scope: str,
    scope_id: Optional[uuid.UUID],
    internal_seconds: int,
    external_seconds: int,
    effective_from: datetime,
    reason: str,
    allow_external_overflow: bool = False,
    overflow_max_seconds: int = 0,
    now: Optional[datetime] = None,
) -> RotationPolicy:
    """Cria nova versão (futuro) — fecha a vigente do mesmo escopo; valida o contratado."""
    from app.core.audit import audit
    from app.modules.inventory import booking  # import tardio (ciclo)

    if scope not in SCOPE_RANK:
        raise DomainError("POLICY_SCOPE_INVALID", "Escopo de política inválido.", status=400)
    if (scope == "GLOBAL") != (scope_id is None):
        raise DomainError("POLICY_SCOPE_INVALID", "scope_id é obrigatório (e só) para escopos não globais.",
                          status=400)
    try:
        spec = RotationSpec(internal_seconds, external_seconds, allow_external_overflow, overflow_max_seconds)
    except PolicyError as exc:
        raise DomainError(exc.code, exc.message, status=400) from None
    if not reason or len(reason.strip()) < 5:
        raise DomainError("REASON_REQUIRED", "Informe a justificativa (mínimo 5 caracteres).", status=400)
    db_now = now or (await db.execute(select(func.now()))).scalar_one()
    if effective_from.tzinfo is None:
        raise DomainError("POLICY_EFFECTIVE_INVALID", "effective_from deve ter fuso (UTC).", status=400)
    if effective_from < db_now:
        raise DomainError("POLICY_NOT_RETROACTIVE", "Mudança de política só vale para o futuro.", status=400)

    # serializa versões do mesmo escopo
    current = (await db.execute(
        select(RotationPolicy).where(
            RotationPolicy.scope == scope,
            (RotationPolicy.scope_id == scope_id) if scope_id is not None else RotationPolicy.scope_id.is_(None),
            or_(RotationPolicy.effective_to.is_(None), RotationPolicy.effective_to > effective_from),
        ).order_by(RotationPolicy.effective_from).with_for_update()
    )).scalars().all()
    later = [r for r in current if r.effective_from >= effective_from]
    if later:
        raise Conflict("POLICY_FUTURE_VERSION_EXISTS",
                       "Já existe versão futura para este escopo; cancele-a antes.",
                       details={"policy_id": str(later[0].id)})
    version = 1 + max([r.version for r in current], default=0)
    if not current:
        prev_max = (await db.execute(
            select(func.max(RotationPolicy.version)).where(
                RotationPolicy.scope == scope,
                (RotationPolicy.scope_id == scope_id) if scope_id is not None else RotationPolicy.scope_id.is_(None))
        )).scalar_one_or_none()
        version = (prev_max or 0) + 1

    # capacidade contratada: reempacota as telas afetadas com a nova política
    await booking.assert_policy_change_keeps_contracts(db, scope=scope, scope_id=scope_id, new_spec=spec,
                                                       effective_from=effective_from)
    for r in current:
        r.effective_to = effective_from
    await db.flush()
    row = RotationPolicy(
        id=uuid.uuid4(), scope=scope, scope_id=scope_id, internal_seconds=internal_seconds,
        external_seconds=external_seconds, allow_external_overflow=allow_external_overflow,
        overflow_max_seconds=overflow_max_seconds, effective_from=effective_from, effective_to=None,
        version=version, reason=reason.strip(), created_by=getattr(ctx, "user_id", None),
    )
    db.add(row)
    await db.flush()
    await audit(db, ctx, "rotation_policy.created", "rotation_policy", row.id,
                before=[policy_dict(r) for r in current] or None, after=policy_dict(row), reason=reason)
    return row
