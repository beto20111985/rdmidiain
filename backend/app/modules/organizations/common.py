"""
RD Mídia Indoor — Utilitários compartilhados pelos endpoints do domínio W-ORG.

- ``paginate``: envelope ``{items,total,page,page_size}`` (contrato §1).
- ``publish_safe``: publica evento de tempo real APÓS o commit (o chamador faz
  ``await db.commit()`` antes); falha de Redis não desfaz a operação já confirmada —
  é registrada em log (o painel refaz as queries na reconexão).
- ``require_any``: dependência que exige QUALQUER uma das permissões (em algum escopo).
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

import structlog
from fastapi import Depends
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.authz import AuthContext, get_auth_context
from app.core.errors import DomainError, Forbidden
from app.core.permissions import is_valid_permission

log = structlog.get_logger("rd.org")

# Eventos de tempo real do domínio (além de screen.updated / establishment.updated do core)
events.register_event_type("organization.updated", "organizations.view")
events.register_event_type("equipment.updated", "equipment.view")
events.register_event_type("maintenance.updated", "maintenance.manage")
events.register_event_type("onboarding.updated", "establishments.manage")

from app.modules.organizations import notifications as _notification_types  # noqa: E402,F401  (registra tipos)

MAX_PAGE_SIZE = 100


async def paginate(
    db: AsyncSession,
    query: Select,
    page: int,
    page_size: int,
    serialize: Callable[[Any], dict],
    *,
    scalars: bool = True,
) -> dict:
    page = max(1, int(page))
    page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
    total = (await db.execute(select(func.count()).select_from(query.order_by(None).subquery()))).scalar_one()
    res = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    rows = res.scalars().all() if scalars else res.all()
    return {"items": [serialize(r) for r in rows], "total": int(total), "page": page, "page_size": page_size}


async def publish_safe(type_: str, data: dict, **scope: Any) -> None:
    try:
        await events.publish(type_, data, **scope)
    except Exception as exc:  # noqa: BLE001 — operação já confirmada; evento é melhor-esforço
        log.error("event_publish_failed", event_type=type_, error_type=type(exc).__name__)


def require_any(*perms: str, allow_impersonation: bool = True) -> Callable[..., Any]:
    for p in perms:
        if not is_valid_permission(p):
            raise ValueError(f"Permissão fora do catálogo: {p}")

    async def _dep(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if ctx.impersonation is not None and not allow_impersonation:
            raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
        if not any(ctx.has_any(p) for p in perms):
            raise Forbidden(details={"permission": list(perms)})
        return ctx

    _dep.__rd_permission__ = perms[0]  # type: ignore[attr-defined]
    _dep.__rd_permissions_any__ = tuple(perms)  # type: ignore[attr-defined]
    return _dep


def changed_fields(obj: Any, updates: dict) -> tuple[dict, dict]:
    """Aplica ``updates`` em ``obj`` e devolve (before, after) só do que mudou."""
    before: dict = {}
    after: dict = {}
    for k, v in updates.items():
        old = getattr(obj, k)
        if old != v:
            before[k] = old
            after[k] = v
            setattr(obj, k, v)
    return before, after


def strip_or_none(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = v.strip()
    return s or None


def iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    from datetime import date, datetime

    from app.core.timeutils import isoformat_z

    if isinstance(dt, datetime):
        return isoformat_z(dt)
    if isinstance(dt, date):
        return dt.isoformat()
    return str(dt)


def uuids(values: Optional[Iterable[Any]]) -> list:
    import uuid

    return [uuid.UUID(str(v)) for v in (values or [])]
