"""RD Mídia Indoor — Central de notificações (AÇÃO 002 / W-BIZ).

Rotas (prefixo ``/api/v1/notifications``):
- ``GET    ""``                         lista paginada das MINHAS notificações
- ``GET    /unread-count``              ``{"count": n}`` (invalidado por WS ``notification.created``)
- ``POST   /{id}/read``                 marca uma como lida (de outro usuário → 404)
- ``POST   /read-all``                  marca todas como lidas → ``{"updated": n}``
- ``GET    /preferences`` / ``PUT /preferences``   preferências por tipo × canal
- ``GET    /event-types``               catálogo (equipe com ``notifications.manage``)
- ``GET    /templates`` · ``PUT|DELETE /templates/{event_type}/{channel}`` · ``POST …/preview``
- ``POST   /announcements``             aviso administrativo (``system.announcement``)
"""
from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.core.timeutils import isoformat_z, utcnow
from app.modules.notifications import service as svc
from app.modules.notifications import templating
from app.modules.notifications.catalog import REGISTRY, get_event_type
from app.modules.notifications.push import PUSH_UNAVAILABLE_DETAIL, get_push_provider

router = APIRouter()

Channel = Literal["IN_APP", "EMAIL", "PUSH"]


def _item(n, deliveries: list) -> dict:
    return {
        "id": str(n.id),
        "event_type": n.event_type,
        "title": n.title,
        "body": n.body,
        "data": n.data or {},
        "link": n.link,
        "entity_type": n.entity_type,
        "entity_id": n.entity_id,
        "read": n.read_at is not None,
        "read_at": isoformat_z(n.read_at) if n.read_at else None,
        "created_at": isoformat_z(n.created_at),
        "deliveries": [{"channel": d.channel, "status": d.status, "detail": d.detail} for d in deliveries],
    }


@router.get("")
async def list_notifications(
    unread_only: bool = Query(False),
    event_type: Optional[str] = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    from app.models.notifications import Notification, NotificationDelivery

    cond = [Notification.user_id == ctx.user_id]
    if unread_only:
        cond.append(Notification.read_at.is_(None))
    if event_type:
        cond.append(Notification.event_type == event_type)
    total = (await db.execute(select(func.count()).select_from(Notification).where(*cond))).scalar_one()
    rows = list(
        (
            await db.execute(
                select(Notification)
                .where(*cond)
                .order_by(Notification.created_at.desc(), Notification.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
    )
    deliveries: dict = {}
    if rows:
        for d in (
            await db.execute(select(NotificationDelivery).where(NotificationDelivery.notification_id.in_([r.id for r in rows])))
        ).scalars():
            deliveries.setdefault(d.notification_id, []).append(d)
    return {
        "items": [_item(r, sorted(deliveries.get(r.id, []), key=lambda d: d.channel)) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/unread-count")
async def get_unread_count(ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    return {"count": await svc.unread_count(db, ctx.user_id)}


@router.post("/read-all")
async def read_all(ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    return {"updated": await svc.mark_all_read(db, ctx.user_id)}


@router.post("/{notification_id}/read", status_code=204)
async def read_one(notification_id: UUID, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    if not await svc.mark_read(db, ctx.user_id, notification_id):
        raise NotFound()
    return Response(status_code=204)


# ─── Preferências ────────────────────────────────────────────────────────────

class PreferenceItem(BaseModel):
    event_type: str = Field(max_length=100)
    channel: Channel
    enabled: bool


class PreferencesUpdate(BaseModel):
    items: list[PreferenceItem] = Field(min_length=1, max_length=500)


@router.get("/preferences")
async def get_preferences(ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    from app.models.notifications import NotificationPreference

    stored = {
        (p.event_type, p.channel): p.is_enabled
        for p in (await db.execute(select(NotificationPreference).where(NotificationPreference.user_id == ctx.user_id))).scalars()
    }
    push_ok = get_push_provider().available
    items = []
    for key, et in sorted(REGISTRY.items()):
        if et.audience == "staff" and not ctx.is_rd_staff:
            continue
        chans = []
        for ch in ("IN_APP", "EMAIL", "PUSH"):
            chans.append(
                {
                    "channel": ch,
                    "enabled": True if (ch == "IN_APP" and et.critical) else stored.get((key, ch), True),
                    "locked": ch == "IN_APP" and et.critical,
                    "default_channel": ch in et.default_channels,
                    "available": push_ok if ch == "PUSH" else True,
                    "unavailable_reason": None if (ch != "PUSH" or push_ok) else PUSH_UNAVAILABLE_DETAIL,
                }
            )
        items.append({"event_type": key, "label": et.label, "critical": et.critical, "channels": chans})
    return {"items": items}


@router.put("/preferences")
async def put_preferences(body: PreferencesUpdate, ctx: AuthContext = Depends(get_auth_context),
                          db: AsyncSession = Depends(get_db)):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.notifications import NotificationPreference

    tbl = NotificationPreference.__table__
    for it in body.items:
        try:
            et = get_event_type(it.event_type)
        except ValueError:
            raise DomainError("NOTIFICATION_TYPE_UNKNOWN", "Tipo de notificação desconhecido.", status=400,
                              details={"event_type": it.event_type}) from None
        if et.critical and it.channel == "IN_APP" and not it.enabled:
            raise DomainError("NOTIFICATION_PREFERENCE_LOCKED",
                              "Esta notificação é obrigatória na central e não pode ser desativada.", status=400,
                              details={"event_type": it.event_type})
        await db.execute(
            pg_insert(tbl)
            .values(user_id=ctx.user_id, event_type=it.event_type, channel=it.channel, is_enabled=it.enabled)
            .on_conflict_do_update(
                constraint="uq_notification_preferences_user_event_channel",
                set_={"is_enabled": it.enabled, "updated_at": utcnow()},
            )
        )
    return await get_preferences(ctx, db)


# ─── Catálogo e templates (equipe) ───────────────────────────────────────────

def _et_out(et) -> dict:
    return {
        "event_type": et.key,
        "label": et.label,
        "variables": list(et.variables) + list(templating.GLOBAL_VARIABLES),
        "default_channels": list(et.default_channels),
        "default_cooldown_s": et.default_cooldown_s,
        "critical": et.critical,
        "audience": et.audience,
        "default_title": et.title,
        "default_body": et.body,
    }


@router.get("/event-types")
async def list_event_types(ctx: AuthContext = Depends(require("notifications.manage", global_only=True))):
    return {"items": [_et_out(et) for _, et in sorted(REGISTRY.items())]}


@router.get("/templates")
async def list_templates(ctx: AuthContext = Depends(require("notifications.manage", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    from app.models.notifications import NotificationTemplate

    custom = {(t.event_type, t.channel): t for t in (await db.execute(select(NotificationTemplate))).scalars()}
    items = []
    for key, et in sorted(REGISTRY.items()):
        for ch in ("IN_APP", "EMAIL", "PUSH"):
            t = custom.get((key, ch))
            items.append(
                {
                    "event_type": key,
                    "channel": ch,
                    "custom": t is not None,
                    "is_active": t.is_active if t else True,
                    "title_template": t.title_template if t else et.title,
                    "body_template": t.body_template if t else et.body,
                    "version": t.version if t else 0,
                    "variables": list(et.variables) + list(templating.GLOBAL_VARIABLES),
                }
            )
    return {"items": items}


class TemplateIn(BaseModel):
    title_template: str = Field(min_length=1, max_length=255)
    body_template: str = Field(min_length=1, max_length=4000)
    is_active: bool = True
    expected_version: Optional[int] = None


def _get_et_or_404(event_type: str):
    try:
        return get_event_type(event_type)
    except ValueError:
        raise NotFound("NOTIFICATION_TYPE_UNKNOWN", "Tipo de notificação desconhecido.") from None


@router.put("/templates/{event_type}/{channel}")
async def put_template(event_type: str, channel: Channel, body: TemplateIn,
                       ctx: AuthContext = Depends(require("notifications.manage", global_only=True)),
                       db: AsyncSession = Depends(get_db)):
    from app.models.notifications import NotificationTemplate

    et = _get_et_or_404(event_type)
    templating.validate_template(body.title_template, et.variables, max_len=255)
    templating.validate_template(body.body_template, et.variables)
    t = (
        await db.execute(
            select(NotificationTemplate)
            .where(NotificationTemplate.event_type == event_type, NotificationTemplate.channel == channel)
            .with_for_update()
        )
    ).scalar_one_or_none()
    before = None
    current_version = t.version if t else 0
    if body.expected_version is not None and body.expected_version != current_version:
        raise DomainError("TEMPLATE_VERSION_CONFLICT", "O template foi alterado por outra pessoa. Recarregue.", status=409,
                          details={"current_version": current_version})
    if t is None:
        t = NotificationTemplate(event_type=event_type, channel=channel, title_template=body.title_template,
                                 body_template=body.body_template, is_active=body.is_active, version=1,
                                 updated_by=ctx.user_id)
        db.add(t)
    else:
        before = {"title": t.title_template, "body": t.body_template, "is_active": t.is_active, "version": t.version}
        t.title_template, t.body_template, t.is_active = body.title_template, body.body_template, body.is_active
        t.version += 1
        t.updated_by = ctx.user_id
    await db.flush()
    await audit(db, ctx, "notification_template.updated", "notification_template", f"{event_type}:{channel}",
                before=before, after={"title": t.title_template, "body": t.body_template, "is_active": t.is_active,
                                      "version": t.version})
    return {"event_type": event_type, "channel": channel, "custom": True, "version": t.version,
            "title_template": t.title_template, "body_template": t.body_template, "is_active": t.is_active}


@router.delete("/templates/{event_type}/{channel}", status_code=204)
async def reset_template(event_type: str, channel: Channel,
                         ctx: AuthContext = Depends(require("notifications.manage", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    from app.models.notifications import NotificationTemplate

    _get_et_or_404(event_type)
    t = (
        await db.execute(
            select(NotificationTemplate).where(NotificationTemplate.event_type == event_type,
                                               NotificationTemplate.channel == channel)
        )
    ).scalar_one_or_none()
    if t is None:
        raise NotFound()
    await audit(db, ctx, "notification_template.reset", "notification_template", f"{event_type}:{channel}",
                before={"title": t.title_template, "body": t.body_template, "version": t.version})
    await db.delete(t)
    return Response(status_code=204)


class PreviewIn(BaseModel):
    title_template: Optional[str] = Field(None, max_length=255)
    body_template: Optional[str] = Field(None, max_length=4000)
    data: dict = Field(default_factory=dict)


@router.post("/templates/{event_type}/{channel}/preview")
async def preview_template(event_type: str, channel: Channel, body: PreviewIn,
                           ctx: AuthContext = Depends(require("notifications.manage", global_only=True)),
                           db: AsyncSession = Depends(get_db)):
    et = _get_et_or_404(event_type)
    current = (await svc.load_templates(db, event_type))[channel]
    title_t = body.title_template if body.title_template is not None else current[0]
    body_t = body.body_template if body.body_template is not None else current[1]
    templating.validate_template(title_t, et.variables, max_len=255)
    templating.validate_template(body_t, et.variables)
    from app.modules.instance.service import brand_name

    sample = {v: body.data.get(v, f"<{v}>") for v in et.variables}
    sample["instance_name"] = await brand_name(db)
    return {"title": templating.render(title_t, sample, max_len=255), "body": templating.render(body_t, sample)}


# ─── Aviso administrativo ────────────────────────────────────────────────────

class AnnouncementIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=3, max_length=2000)
    org_id: Optional[UUID] = None
    permission: str = Field("dashboard.view", max_length=100)
    channels: list[Channel] = Field(default_factory=lambda: ["IN_APP"])
    dedupe_key: Optional[str] = Field(None, max_length=200)


@router.post("/announcements", status_code=201)
async def announce(body: AnnouncementIn,
                   ctx: AuthContext = Depends(require("notifications.manage", global_only=True)),
                   db: AsyncSession = Depends(get_db)):
    from app.core.permissions import is_valid_permission

    if not is_valid_permission(body.permission):
        raise DomainError("PERMISSION_UNKNOWN", "Permissão inexistente.", status=400)
    recipients = [svc.OrgRecipients(body.org_id, body.permission)] if body.org_id else [svc.StaffRecipients(body.permission)]
    res = await svc.notify(db, recipients, "system.announcement", {"title": body.title, "message": body.message},
                           dedupe_key=body.dedupe_key, cooldown_s=0, channels=body.channels, org_id=body.org_id)
    await audit(db, ctx, "notification.announcement", "notification", None,
                after={"title": body.title, "org_id": body.org_id, "permission": body.permission,
                       "recipients": res.recipients, "created": res.created_count}, org_id=body.org_id)
    return {"recipients": res.recipients, "created": res.created_count, "deduped": res.deduped,
            "skipped_by_preference": res.skipped_by_preference, "push_unavailable": res.push_unavailable}
