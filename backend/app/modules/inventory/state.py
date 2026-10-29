"""
RD Mídia Indoor — Estado de inventário por tela: linha de lock e âncora (W-INV).

- ``ensure_states``: cria (idempotente) a linha ``screen_inventory_state`` de cada tela.
- ``lock_screens``: ``SELECT … WHERE screen_id = ANY(:ids) ORDER BY screen_id FOR UPDATE``
  — ordem determinística ⇒ checkouts com conjuntos sobrepostos não entram em deadlock.
  Exige READ COMMITTED (o recálculo depois do lock precisa enxergar o que foi
  confirmado por quem liberou o lock — A6 §10.3) e aplica ``SET LOCAL lock_timeout``.
- Âncora (ADR_006 §3; A6 §10.6; RDPP §5.1): pertence à TELA, write-once (trigger);
  ``set_anchor`` aceita a âncora do player se |âncora − agora_servidor| ≤ 120 s (horário
  do BANCO no recebimento), senão usa o horário do servidor; devolve o valor VIGENTE
  (se outro dispositivo venceu a corrida, devolve o dele). Reboot/troca de TV Box não
  alteram a âncora; reset só por admin com justificativa (função SQL auditada).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import DomainError, NotFound
from app.models.inventory import ScreenAnchorHistory, ScreenInventoryState
from app.modules.inventory.sources import ScreenInfo, load_screens

log = structlog.get_logger("rd.inventory.state")

LOCK_TIMEOUT_MS = 5000
ANCHOR_MAX_SKEW_MS = 120_000


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ms_of(dt: Optional[datetime]) -> Optional[int]:
    """Milissegundos UTC com aritmética INTEIRA (23:59:59.999999 → …999, nunca arredonda para cima)."""
    if dt is None:
        return None
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def dt_of(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=int(ms))


async def db_now(db: AsyncSession) -> datetime:
    return (await db.execute(select(func.now()))).scalar_one()


async def ensure_read_committed(db: AsyncSession) -> None:
    iso = (await db.execute(text("SELECT current_setting('transaction_isolation')"))).scalar_one()
    if iso != "read committed":
        raise DomainError("INVENTORY_ISOLATION", "Operação de inventário exige READ COMMITTED.", status=500,
                          details={"isolation": iso})


async def set_lock_timeout(db: AsyncSession, ms: int = LOCK_TIMEOUT_MS) -> None:
    await db.execute(text(f"SET LOCAL lock_timeout = '{int(ms)}ms'"))


async def ensure_states(db: AsyncSession, screens: dict[uuid.UUID, ScreenInfo]) -> None:
    if not screens:
        return
    rows = [{"screen_id": s.id, "timezone": s.timezone} for s in screens.values()]
    await db.execute(pg_insert(ScreenInventoryState).values(rows).on_conflict_do_nothing(
        index_elements=[ScreenInventoryState.screen_id]))


async def lock_screens(db: AsyncSession, screen_ids: Iterable[Any]) -> dict[uuid.UUID, ScreenInventoryState]:
    ids = sorted({uuid.UUID(str(s)) for s in screen_ids}, key=str)
    if not ids:
        return {}
    await ensure_read_committed(db)
    await set_lock_timeout(db)
    try:
        rows = (await db.execute(
            select(ScreenInventoryState).where(ScreenInventoryState.screen_id.in_(ids))
            .order_by(ScreenInventoryState.screen_id).with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all()
    except DBAPIError as exc:
        if "lock timeout" in str(getattr(exc, "orig", exc)).lower() or "55P03" in str(exc):
            raise DomainError("INVENTORY_BUSY", "Inventário ocupado por outra contratação. Tente novamente.",
                              status=409) from None
        raise
    return {r.screen_id: r for r in rows}


async def get_or_create_state(db: AsyncSession, screen_id: uuid.UUID) -> tuple[ScreenInventoryState, ScreenInfo]:
    info = (await load_screens(db, [screen_id])).get(screen_id)
    if info is None:
        raise NotFound()
    await ensure_states(db, {screen_id: info})
    st = await db.get(ScreenInventoryState, screen_id)
    assert st is not None
    return st, info


# ─── Âncora ──────────────────────────────────────────────────────────────────

async def set_anchor(db: AsyncSession, screen_id: Any, anchor_ms: Optional[int], device_id: Any) -> int:
    """Write-once. Devolve a âncora VIGENTE da tela em ms (UTC)."""
    sid = uuid.UUID(str(screen_id))
    dev = uuid.UUID(str(device_id)) if device_id is not None else None
    _, info = await get_or_create_state(db, sid)
    now = await db_now(db)
    now_ms = ms_of(now)
    source = "PLAYER"
    if isinstance(anchor_ms, bool) or not isinstance(anchor_ms, int) or abs(anchor_ms - now_ms) > ANCHOR_MAX_SKEW_MS:
        candidate_ms = now_ms - now_ms % 1000
        source = "SERVER"
    else:
        candidate_ms = anchor_ms - anchor_ms % 1000
    res = await db.execute(
        update(ScreenInventoryState)
        .where(ScreenInventoryState.screen_id == sid, ScreenInventoryState.anchor_utc.is_(None))
        .values(anchor_utc=dt_of(candidate_ms), anchor_source=source, anchor_device_id=dev,
                anchor_reported_at=func.now(), updated_at=func.now())
        .returning(ScreenInventoryState.anchor_utc)
    )
    row = res.first()
    if row is None:  # já definida (reboot, troca de aparelho ou corrida) → devolve a original
        cur = (await db.execute(
            select(ScreenInventoryState.anchor_utc).where(ScreenInventoryState.screen_id == sid)
        )).scalar_one()
        return ms_of(cur)  # type: ignore[return-value]
    db.add(ScreenAnchorHistory(
        id=uuid.uuid4(), screen_id=sid, old_anchor=None, new_anchor=row[0], source=source,
        reason="Primeiro start válido" + (" (relógio do player divergente: usado o do servidor)"
                                         if source == "SERVER" else ""),
        device_id=dev,
    ))
    from app.core.authz import AuthContext

    await audit(db, AuthContext(kind="device", device_id=dev), "screen.anchor_set", "screen", sid,
                after={"anchor_ms": candidate_ms, "source": source,
                       "reported_ms": anchor_ms if isinstance(anchor_ms, int) else None,
                       "server_ms": now_ms}, org_id=info.org_id)
    await db.flush()
    # novo manifesto com anchor_ms (outros dispositivos/reinstalação adotam a mesma)
    from app.modules.inventory.manifests import invalidate_screens

    await invalidate_screens(db, [sid], "anchor_set")
    return candidate_ms


async def reset_anchor(db: AsyncSession, ctx: Any, screen_id: Any, reason: str,
                       new_anchor_ms: Optional[int] = None) -> Optional[int]:
    """Reset administrativo auditado (função SQL com flag de transação + histórico)."""
    sid = uuid.UUID(str(screen_id))
    if not reason or len(reason.strip()) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    st, info = await get_or_create_state(db, sid)
    before = ms_of(st.anchor_utc)
    new_dt = dt_of(new_anchor_ms) if new_anchor_ms is not None else None
    await db.execute(text("SELECT rd_reset_screen_anchor(CAST(:s AS uuid), :a, :r, CAST(:u AS uuid))"),
                     {"s": str(sid), "a": new_dt, "r": reason.strip(),
                      "u": str(ctx.user_id) if getattr(ctx, "user_id", None) else None})
    await audit(db, ctx, "screen.anchor_reset", "screen", sid, before={"anchor_ms": before},
                after={"anchor_ms": new_anchor_ms}, reason=reason.strip(), org_id=info.org_id)
    db.expire_all()
    from app.modules.inventory.manifests import invalidate_screens

    await invalidate_screens(db, [sid], "anchor_reset")
    return new_anchor_ms


async def anchor_history(db: AsyncSession, screen_id: uuid.UUID) -> list[dict]:
    rows = (await db.execute(select(ScreenAnchorHistory).where(ScreenAnchorHistory.screen_id == screen_id)
                             .order_by(ScreenAnchorHistory.created_at))).scalars().all()
    return [{"id": str(r.id), "old_anchor_ms": ms_of(r.old_anchor), "new_anchor_ms": ms_of(r.new_anchor),
             "source": r.source, "reason": r.reason, "actor_user_id": str(r.actor_user_id) if r.actor_user_id else None,
             "device_id": str(r.device_id) if r.device_id else None, "created_at": r.created_at} for r in rows]
