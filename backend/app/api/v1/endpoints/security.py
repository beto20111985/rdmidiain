"""
RD Mídia Indoor — Segurança da instância (AÇÃO 003; ADR_011; SPEC §5.2).

GET /security/approval-mode · GET /security/masters · POST /security/masters/invites ·
POST /security/masters/invites/{id}/resend · DELETE /security/masters/invites/{id} · GET /security/integrity

- O modo de aprovação NÃO tem switch: só muda por pedido ``APPROVAL_MODE_CHANGE`` no motor de aprovações.
- ``/security/masters`` mostra nome/estado dos assentos (nunca e-mail completo do outro Master).
- O token do convite vai SOMENTE no e-mail ao convidado; nunca nesta API, auditoria ou log.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Response
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context
from app.core.database import get_db
from app.core.errors import DomainError, Forbidden
from app.core.timeutils import utcnow
from app.models.users import User
from app.modules.auth import service as svc
from app.modules.security import approval_mode as am
from app.modules.security import invites
from app.modules.security import masters
from app.modules.security import notifications as _sec_notifications  # noqa: F401  (registra tipos/eventos)

router = APIRouter()


def _admin_master_required() -> DomainError:
    return DomainError("ADMIN_MASTER_REQUIRED", "Somente os Administradores Master podem acessar esta área.",
                       status=403)


def _is_seat_holder(ctx: AuthContext) -> bool:
    return ctx.impersonation is None and getattr(ctx.user, "admin_master_seat", None) is not None and ctx.is_super_admin


def _privileged_viewer(ctx: AuthContext) -> bool:
    return _is_seat_holder(ctx) or ctx.can("audit.view", global_only=True)


async def _mode_viewer(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if not (ctx.can("settings.view", global_only=True) or ctx.can("finance.view", global_only=True)):
        raise Forbidden(details={"permission": "settings.view"})
    return ctx


async def _seat_holder(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if not _is_seat_holder(ctx):
        raise _admin_master_required()
    return ctx


async def _privileged(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if not _privileged_viewer(ctx):
        raise Forbidden(details={"permission": "audit.view"})
    return ctx


@asynccontextmanager
async def _db_errors():
    """D2: deadlock/serialização/lock_timeout e códigos ``RD_*`` do banco ⇒ 409/503 (nunca 500)."""
    try:
        yield
    except DBAPIError as exc:
        mapped = am.map_db_error(exc)
        if mapped is not None:
            raise mapped from None
        raise


async def _names(db: AsyncSession, ids) -> dict:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {uid: name for uid, name in (await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))).all()}


# ─── Modo de aprovação ───────────────────────────────────────────────────────

@router.get("/approval-mode")
async def get_approval_mode(ctx: AuthContext = Depends(_mode_viewer), db: AsyncSession = Depends(get_db)):
    from app.models.financial import FinancialApprovalRequest as Req
    from app.modules.security.bootstrap import env_status

    state = await am.current_state(db)
    if state is None:
        raise am.not_initialized()
    rows = await am.history_rows(db)
    active = await masters.active_master_count(db)
    now = utcnow()
    pending = (
        await db.execute(
            select(Req).where(Req.action_type == "APPROVAL_MODE_CHANGE", Req.status == "PENDING", Req.expires_at > now)
            .order_by(Req.requested_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    names = await _names(db, [pending.requested_by if pending else None,
                              *[r.requested_by for r in rows], *[r.authorized_by for r in rows]])
    out: dict = {
        "mode": state.mode,
        "mode_version": state.version,
        "initialized_at": rows[0].created_at if rows else None,
        "initialized_source": rows[0].source if rows else None,
        "dual_approval_ready": (active >= 2) if state.mode == "DUAL" else None,
        "active_masters": active,
        "pending_change_request": None if pending is None else {
            "id": str(pending.id),
            "target_mode": (pending.payload or {}).get("target_mode"),
            "requested_by_name": names.get(pending.requested_by),
            "requested_at": pending.requested_at,
            "expires_at": pending.expires_at,
        },
        "history": None,
        "integrity": None,
        "env": None,
    }
    if _privileged_viewer(ctx):
        out["history"] = [
            {"mode_version": r.mode_version, "kind": r.kind, "from_mode": r.from_mode, "to_mode": r.to_mode,
             "source": r.source, "requested_by_name": names.get(r.requested_by),
             "authorized_by_name": names.get(r.authorized_by), "reason": r.reason,
             "approval_request_id": str(r.approval_request_id) if r.approval_request_id else None,
             "created_at": r.created_at}
            for r in rows
        ]
        out["integrity"] = {"ok": (await am.verify_integrity(db)).ok}
        out["env"] = await env_status(state)
    return out


# ─── Assentos e convite do 2º Master ─────────────────────────────────────────

@router.get("/masters")
async def get_masters(ctx: AuthContext = Depends(_seat_holder), db: AsyncSession = Depends(get_db)):
    await invites.expire_stale(db, ctx)
    state = await am.current_state(db)
    holders = await masters.seat_holders(db)
    flags = await masters.active_flags(db, [u.id for u in holders.values()])
    seats = []
    for s in (1, 2):
        u = holders.get(s)
        seats.append({
            "seat": s,
            "name": u.full_name if u else None,
            "status": "VACANT" if u is None else ("ACTIVE" if flags.get(u.id) else "SETUP_PENDING"),
            "is_self": bool(u is not None and u.id == ctx.user_id),
        })
    inv = await masters.pending_invite(db)
    can_invite = bool(
        state is not None and state.mode == "DUAL" and len(holders) < 2 and inv is None
        and masters.is_active_master_user(ctx)
    )
    return {
        "approval_mode": state.mode if state else None,
        "seats": seats,
        "invite": await invites.invite_public(db, inv) if inv is not None else None,
        "can_invite": can_invite,
    }


class MasterInviteIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    totp_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class InviteRevokeIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=300)


async def _count_failure(db: AsyncSession, user_id: uuid.UUID, kind: str) -> None:
    """Falha de TOTP do convidante conta no lockout (transação própria; desfaz a da request antes)."""
    from app.core.config import settings

    await db.rollback()
    fr = await svc.register_failure(user_id, kind)
    if fr.just_locked:
        svc.raise_locked(settings.LOGIN_LOCKOUT_MINUTES * 60)


@router.post("/masters/invites", status_code=201)
async def create_master_invite(body: MasterInviteIn, background: BackgroundTasks,
                               ctx: AuthContext = Depends(_seat_holder), db: AsyncSession = Depends(get_db)):
    async def count_failure(user_id, kind):
        await _count_failure(db, user_id, kind)

    async with _db_errors():
        inv, mail = await invites.create_invite(db, ctx, full_name=body.full_name, email=body.email,
                                                totp_code=body.totp_code, count_failure=count_failure)
    background.add_task(invites._send, mail)  # depois do commit da request; token só no e-mail ao convidado
    return await invites.invite_public(db, inv)


@router.post("/masters/invites/{invite_id}/resend")
async def resend_master_invite(invite_id: uuid.UUID, background: BackgroundTasks,
                               ctx: AuthContext = Depends(_seat_holder), db: AsyncSession = Depends(get_db)):
    async with _db_errors():
        inv, mail = await invites.resend_invite(db, ctx, invite_id)
    background.add_task(invites._send, mail)
    return {"sent": True, "expires_at": inv.expires_at}


@router.delete("/masters/invites/{invite_id}", status_code=204)
async def revoke_master_invite(invite_id: uuid.UUID, body: Optional[InviteRevokeIn] = Body(default=None),
                               ctx: AuthContext = Depends(_seat_holder), db: AsyncSession = Depends(get_db)):
    async with _db_errors():
        await invites.revoke_invite(db, ctx, invite_id, body.reason if body else None)
    return Response(status_code=204)


# ─── Integridade ─────────────────────────────────────────────────────────────

@router.get("/integrity")
async def get_integrity(ctx: AuthContext = Depends(_privileged), db: AsyncSession = Depends(get_db)):
    report = await am.verify_integrity(db)
    return {"ok": report.ok, "problems": list(report.problems)}
