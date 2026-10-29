"""
RD Mídia Indoor — Serviço de telas (W-ORG). Interface estável para outros domínios.

Remoção definitiva (ETAPA2 #1116; P001 §111):
    from app.modules.screens.service import register_screen_removed_listener
    async def on_removed(db, screen_ids: list[UUID], reason: str) -> None: ...
    register_screen_removed_listener(on_removed)      # W-INV registra no import do módulo

``screen_permanently_removed`` é chamado NA MESMA TRANSAÇÃO quando:
- a tela é retirada definitivamente (``reason = "SCREEN_DECOMMISSIONED"``), ou
- o estabelecimento é inativado (``reason = "ESTABLISHMENT_DEACTIVATED"``).
Tela em MANUTENÇÃO não dispara nada e continua vendável.

Histórico: ``screen_establishment_at(db, screen_id, at)`` → estabelecimento em que a
tela estava no instante ``at`` (para PoP/comissão de períodos anteriores a uma mudança).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import DomainError, NotFound
from app.core.ids import generate_unique, new_screen_code
from app.core.timeutils import utcnow
from app.models.establishments import Establishment, OnboardingStatus
from app.models.screens import Screen, ScreenHistory, ScreenHistoryEvent, ScreenStatus

log = structlog.get_logger("rd.screens")

REASON_SCREEN_DECOMMISSIONED = "SCREEN_DECOMMISSIONED"
REASON_ESTABLISHMENT_DEACTIVATED = "ESTABLISHMENT_DEACTIVATED"

ScreenRemovedListener = Callable[[AsyncSession, list[uuid.UUID], str], Awaitable[None]]
_removed_listeners: list[ScreenRemovedListener] = []

HARDWARE_FIELDS = (
    "display_type",
    "player_type",
    "orientation",
    "hardware_brand",
    "hardware_model",
    "inches",
    "resolution_w",
    "resolution_h",
    "serial_number",
    "asset_tag",
    "player_brand",
    "player_model",
    "player_serial_number",
    "tv_owner",
    "installed_at",
)
TV_FIELDS = ("hardware_brand", "hardware_model", "inches", "resolution_w", "resolution_h", "serial_number", "asset_tag",
             "display_type", "tv_owner")
PLAYER_FIELDS = ("player_brand", "player_model", "player_serial_number", "player_type")


def register_screen_removed_listener(fn: ScreenRemovedListener) -> None:
    if fn not in _removed_listeners:
        _removed_listeners.append(fn)


def unregister_screen_removed_listener(fn: ScreenRemovedListener) -> None:
    if fn in _removed_listeners:
        _removed_listeners.remove(fn)


async def screen_permanently_removed(db: AsyncSession, screen_ids: Iterable[uuid.UUID], reason: str) -> None:
    ids = list(dict.fromkeys(screen_ids))
    if not ids:
        return
    for fn in list(_removed_listeners):
        await fn(db, ids, reason)  # falha do listener desfaz a transação inteira (consistência)


async def request_manifest_refresh(db: AsyncSession, screen_ids: Iterable[uuid.UUID], reason: str) -> bool:
    """Pede ao W-INV nova versão do manifesto (mudança de fuso, estabelecimento, orientação, status...).

    Executado num SAVEPOINT: falha na regeneração não desfaz a alteração cadastral já válida;
    é registrada em log (``manifest_refresh_failed``) e o W-INV regenera no próximo gatilho/job.
    Retorna True se a regeneração foi solicitada com sucesso.
    """
    ids = list(dict.fromkeys(screen_ids))
    if not ids:
        return True
    try:
        from app.modules.inventory.service import invalidate_screens
    except ImportError:  # pragma: no cover - W-INV sempre presente nesta versão
        log.error("manifest_refresh_unavailable", screens=len(ids))
        return False
    try:
        async with db.begin_nested():
            await invalidate_screens(db, ids, reason[:64])
        return True
    except Exception as exc:  # noqa: BLE001 — ver docstring
        log.error("manifest_refresh_failed", screens=len(ids), reason=reason[:64], error_type=type(exc).__name__)
        return False


# ─── Utilidades ──────────────────────────────────────────────────────────────

def est_scope(est: Establishment) -> dict:
    return {"org_id": est.org_id, "establishment_id": est.id, "city": est.city_key}


async def lock_screen(db: AsyncSession, screen_id: uuid.UUID) -> Screen:
    s = (await db.execute(select(Screen).where(Screen.id == screen_id).with_for_update()
                      .execution_options(populate_existing=True))).scalar_one_or_none()
    if s is None:
        raise NotFound()
    return s


async def new_friendly_id(db: AsyncSession, uf: str) -> str:
    async def exists(code: str) -> bool:
        return (await db.execute(select(Screen.id).where(Screen.friendly_id == code))).scalar_one_or_none() is not None

    return await generate_unique(lambda: new_screen_code(uf), exists, max_attempts=20)


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        from app.core.timeutils import isoformat_z

        return isoformat_z(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


def snap(obj: Any, fields: Iterable[str]) -> dict:
    return {f: _jsonable(getattr(obj, f)) for f in fields}


async def add_history(
    db: AsyncSession,
    screen: Screen,
    event: ScreenHistoryEvent | str,
    *,
    actor_user_id: Optional[uuid.UUID] = None,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    reason: Optional[str] = None,
    from_establishment_id: Optional[uuid.UUID] = None,
) -> ScreenHistory:
    h = ScreenHistory(
        id=uuid.uuid4(),
        screen_id=screen.id,
        event=event.value if isinstance(event, ScreenHistoryEvent) else str(event),
        establishment_id=screen.establishment_id,
        from_establishment_id=from_establishment_id,
        before_data=before,
        after_data=after,
        reason=reason,
        actor_user_id=actor_user_id,
    )
    db.add(h)
    return h


def compute_sellable(screen: Screen, est: Establishment) -> bool:
    return (
        screen.status in (ScreenStatus.ACTIVE.value, ScreenStatus.MAINTENANCE.value)
        and bool(est.is_active)
        and est.onboarding_status == OnboardingStatus.ACTIVE.value
    )


async def refresh_sellable(
    db: AsyncSession, ctx: Any, screens: Iterable[Screen], est: Establishment, reason: str
) -> list[Screen]:
    """Recalcula ``sellable`` das telas do estabelecimento; registra histórico das mudanças."""
    changed = []
    for s in screens:
        if s.status == ScreenStatus.DECOMMISSIONED.value:
            continue
        new = compute_sellable(s, est)
        if new != s.sellable:
            await add_history(db, s, ScreenHistoryEvent.SELLABLE_CHANGED, actor_user_id=getattr(ctx, "user_id", None),
                              before={"sellable": s.sellable}, after={"sellable": new}, reason=reason)
            s.sellable = new
            changed.append(s)
    return changed


async def screens_of(db: AsyncSession, establishment_id: uuid.UUID, *, lock: bool = False) -> list[Screen]:
    q = select(Screen).where(Screen.establishment_id == establishment_id).order_by(Screen.id)
    if lock:
        q = q.with_for_update()
    return list((await db.execute(q)).scalars().all())


async def screen_establishment_at(db: AsyncSession, screen_id: uuid.UUID, at: datetime) -> Optional[uuid.UUID]:
    """Estabelecimento da tela no instante ``at`` (usa o histórico de mudanças)."""
    row = (
        await db.execute(
            select(ScreenHistory.establishment_id)
            .where(ScreenHistory.screen_id == screen_id, ScreenHistory.created_at <= at,
                   ScreenHistory.event.in_([ScreenHistoryEvent.CREATED.value, ScreenHistoryEvent.MOVED.value]))
            .order_by(ScreenHistory.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    first_move = (
        await db.execute(
            select(ScreenHistory.from_establishment_id)
            .where(ScreenHistory.screen_id == screen_id, ScreenHistory.event == ScreenHistoryEvent.MOVED.value)
            .order_by(ScreenHistory.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if first_move is not None:
        return first_move
    s = await db.get(Screen, screen_id)
    return s.establishment_id if s else None


# ─── Operações ───────────────────────────────────────────────────────────────

async def decommission(db: AsyncSession, ctx: Any, screen: Screen, est: Establishment, reason: str) -> Screen:
    if screen.status == ScreenStatus.DECOMMISSIONED.value:
        raise DomainError("SCREEN_ALREADY_DECOMMISSIONED", "Tela já retirada definitivamente.", status=409)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    before = {"status": screen.status, "sellable": screen.sellable}
    screen.status = ScreenStatus.DECOMMISSIONED.value
    screen.sellable = False
    screen.decommissioned_at = utcnow()
    screen.decommission_reason = reason
    await add_history(db, screen, ScreenHistoryEvent.DECOMMISSIONED, actor_user_id=getattr(ctx, "user_id", None),
                      before=before, after={"status": screen.status, "sellable": False}, reason=reason)
    await db.flush()
    await audit(db, ctx, "screen.decommissioned", "screen", screen.id, before=before,
                after={"status": screen.status, "sellable": False}, reason=reason, org_id=est.org_id)
    await screen_permanently_removed(db, [screen.id], REASON_SCREEN_DECOMMISSIONED)
    return screen


async def set_status(db: AsyncSession, ctx: Any, screen: Screen, est: Establishment, status: str,
                     reason: Optional[str]) -> bool:
    """ACTIVE | MAINTENANCE | INACTIVE (DECOMMISSIONED só por ``decommission``)."""
    if status not in (ScreenStatus.ACTIVE.value, ScreenStatus.MAINTENANCE.value, ScreenStatus.INACTIVE.value):
        raise DomainError("SCREEN_STATUS_INVALID", "Status inválido.", status=400)
    if screen.status == ScreenStatus.DECOMMISSIONED.value:
        raise DomainError("SCREEN_DECOMMISSIONED", "Tela retirada definitivamente não pode ser reativada.", status=409)
    if screen.status == status:
        return False
    before = {"status": screen.status, "sellable": screen.sellable}
    screen.status = status
    screen.sellable = compute_sellable(screen, est)
    after = {"status": status, "sellable": screen.sellable}
    await add_history(db, screen, ScreenHistoryEvent.STATUS_CHANGED, actor_user_id=getattr(ctx, "user_id", None),
                      before=before, after=after, reason=reason)
    await audit(db, ctx, "screen.status_changed", "screen", screen.id, before=before, after=after, reason=reason,
                org_id=est.org_id)
    await db.flush()
    await request_manifest_refresh(db, [screen.id], f"screen_status:{status}")
    return True


async def move_screen(db: AsyncSession, ctx: Any, screen: Screen, old_est: Establishment, new_est: Establishment,
                      reason: str) -> Screen:
    if screen.status == ScreenStatus.DECOMMISSIONED.value:
        raise DomainError("SCREEN_DECOMMISSIONED", "Tela retirada definitivamente não pode ser movida.", status=409)
    if new_est.id == old_est.id:
        raise DomainError("SCREEN_SAME_ESTABLISHMENT", "A tela já está neste estabelecimento.", status=400)
    if not new_est.is_active or new_est.deleted_at is not None:
        raise DomainError("ESTABLISHMENT_INACTIVE", "Estabelecimento de destino inativo.", status=400)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    before = {"establishment_id": str(old_est.id), "org_id": str(old_est.org_id), "city_key": old_est.city_key,
              "sellable": screen.sellable}
    screen.establishment_id = new_est.id
    screen.sellable = compute_sellable(screen, new_est)
    after = {"establishment_id": str(new_est.id), "org_id": str(new_est.org_id), "city_key": new_est.city_key,
             "sellable": screen.sellable}
    await add_history(db, screen, ScreenHistoryEvent.MOVED, actor_user_id=getattr(ctx, "user_id", None),
                      before=before, after=after, reason=reason, from_establishment_id=old_est.id)
    await db.flush()
    await audit(db, ctx, "screen.moved", "screen", screen.id, before=before, after=after, reason=reason,
                org_id=new_est.org_id)
    # Itens de estoque instalados na tela acompanham a tela (projeção de escopo)
    from app.modules.assets.service import follow_screen_move

    await follow_screen_move(db, screen, new_est)
    await request_manifest_refresh(db, [screen.id], "screen_moved")
    return screen


async def deactivate_establishment(db: AsyncSession, ctx: Any, est: Establishment, reason: str) -> list[Screen]:
    if not est.is_active:
        raise DomainError("ESTABLISHMENT_ALREADY_INACTIVE", "Estabelecimento já está inativo.", status=409)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    screens = await screens_of(db, est.id, lock=True)
    est.is_active = False
    est.deactivated_at = utcnow()
    est.deactivation_reason = reason
    changed = await refresh_sellable(db, ctx, screens, est, reason)
    await db.flush()
    await audit(db, ctx, "establishment.deactivated", "establishment", est.id,
                before={"is_active": True}, after={"is_active": False, "screens_unsellable": len(changed)},
                reason=reason, org_id=est.org_id)
    live = [s.id for s in screens if s.status != ScreenStatus.DECOMMISSIONED.value]
    await screen_permanently_removed(db, live, REASON_ESTABLISHMENT_DEACTIVATED)
    return screens


async def reactivate_establishment(db: AsyncSession, ctx: Any, est: Establishment, reason: str) -> list[Screen]:
    if est.is_active:
        raise DomainError("ESTABLISHMENT_ALREADY_ACTIVE", "Estabelecimento já está ativo.", status=409)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    screens = await screens_of(db, est.id, lock=True)
    est.is_active = True
    est.deactivated_at = None
    est.deactivation_reason = None
    await refresh_sellable(db, ctx, screens, est, reason)
    await db.flush()
    await audit(db, ctx, "establishment.reactivated", "establishment", est.id, before={"is_active": False},
                after={"is_active": True}, reason=reason, org_id=est.org_id)
    await request_manifest_refresh(db, [s.id for s in screens if s.status != ScreenStatus.DECOMMISSIONED.value],
                                   "establishment_reactivated")
    return screens


def audience_label(value: Optional[int], method: Optional[str]) -> Optional[dict]:
    if value is None:
        return None
    return {
        "value": value,
        "is_estimate": True,
        "method": method,
        "label": "Audiência estimada (pessoas/dia) — não é medição",
    }
