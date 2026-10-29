"""
RD Mídia Indoor — Autorização financeira dos Admin Master (W-FIN; ADR_009 item 4; ADR_011 D2–D4).
Prefixo ``/api/v1/financial/approvals``.

Solicitar, aprovar e autorizar exigem ``finance.approve`` (efetiva SOMENTE para os Admin
Master; negada em impersonation). O pedido nasce no modo EFETIVO da instância:

- DUAL: o OUTRO Master aprova (``/approve``); o solicitante nunca aprova o próprio (403);
  ``/authorize`` ⇒ 409 ``APPROVAL_MODE_DUAL``.
- SINGLE: o PRÓPRIO solicitante autoriza (``/authorize``); ``/approve``/``/reject`` ⇒ 409
  ``APPROVAL_MODE_SINGLE``.

Aprovar/autorizar exige TOTP + senha financeira + código por e-mail e o ``payload_sha256``
revisado. ``APPROVAL_MODE_CHANGE`` em DUAL exige ``step_up`` (TOTP + senha financeira) já
na solicitação.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import Forbidden, NotFound
from app.models.financial import ApprovalAction, FinancialApprovalEvent, FinancialApprovalRequest
from app.modules.financial import approvals as svc

router = APIRouter()

APPROVE = require("finance.approve", global_only=True, allow_impersonation=False)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StepUpIn(_Body):
    totp_code: str = Field(min_length=6, max_length=12)
    financial_password: str = Field(min_length=1, max_length=128)


class ApprovalIn(_Body):
    action_type: str = Field(min_length=3, max_length=40)
    payload: dict[str, Any]
    reason: str = Field(min_length=10, max_length=2000)
    # obrigatório para APPROVAL_MODE_CHANGE em DUAL (ausente/errado ⇒ 403 FINANCIAL_STEP_UP_INVALID)
    step_up: Optional[StepUpIn] = None


class ApproveIn(_Body):
    totp_code: str = Field(min_length=6, max_length=12)
    financial_password: str = Field(min_length=1, max_length=128)
    payload_sha256: str = Field(min_length=64, max_length=64)
    email_code: Optional[str] = Field(default=None, max_length=12)  # obrigatório (ausente ⇒ 403 REQUIRED)


class RejectIn(_Body):
    reason: str = Field(min_length=5, max_length=2000)


def _viewer(ctx: AuthContext) -> None:
    if not (ctx.can("finance.view", global_only=True) or ctx.can("finance.approve", global_only=True)):
        raise Forbidden(details={"permission": "finance.view"})


@router.get("/actions")
async def actions(ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    from app.modules.security import approval_mode

    _viewer(ctx)
    state = await approval_mode.current_state(db)  # leitura (sem lock/integridade): só informa o modo
    return {"approval_mode": state.mode if state else None,
            "items": [{"action_type": a.value, "label": svc.ACTION_LABELS[a.value],
                       "pix_out": a.value in ("RD_WITHDRAWAL", "REFUND_PIX_OUT", "PAYABLE_SETTLEMENT"),
                       "is_mode_change": a.value == svc.MODE_CHANGE}
                      for a in ApprovalAction]}


@router.get("")
async def list_requests(status: Optional[str] = Query(None, max_length=10), page: int = Query(1, ge=1),
                        page_size: int = Query(25, ge=1, le=100), ctx: AuthContext = Depends(get_auth_context),
                        db: AsyncSession = Depends(get_db)):
    _viewer(ctx)
    q = select(FinancialApprovalRequest)
    if status:
        q = q.where(FinancialApprovalRequest.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(FinancialApprovalRequest.requested_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": await svc.requests_public(db, rows, viewer_id=ctx.user_id), "total": total, "page": page,
            "page_size": page_size}


@router.get("/{request_id}")
async def get_request(request_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                      db: AsyncSession = Depends(get_db)):
    _viewer(ctx)
    r = await db.get(FinancialApprovalRequest, request_id)
    if r is None:
        raise NotFound()
    return await svc.request_public_named(db, r, viewer_id=ctx.user_id)


@router.get("/{request_id}/events")
async def request_events(request_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                         db: AsyncSession = Depends(get_db)):
    _viewer(ctx)
    rows = (await db.execute(select(FinancialApprovalEvent).where(FinancialApprovalEvent.request_id == request_id)
                             .order_by(FinancialApprovalEvent.created_at))).scalars().all()
    return {"items": [{"id": str(e.id), "event": e.event, "actor_user_id": str(e.actor_user_id) if e.actor_user_id else None,
                       "data": e.data, "created_at": e.created_at} for e in rows]}


@router.post("", status_code=201)
async def create_request(body: ApprovalIn, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key",
                                                                                   max_length=120),
                         ctx: AuthContext = Depends(APPROVE), db: AsyncSession = Depends(get_db)):
    r = await svc.create_request(db, ctx, body.action_type, body.payload, body.reason, idempotency_key=idempotency_key,
                                 step_up=body.step_up.model_dump() if body.step_up else None)
    return await svc.request_public_named(db, r, viewer_id=ctx.user_id)


@router.post("/{request_id}/email-code")
async def email_code(request_id: uuid.UUID, ctx: AuthContext = Depends(APPROVE), db: AsyncSession = Depends(get_db)):
    return await svc.request_email_code(db, ctx, request_id)


@router.post("/{request_id}/approve")
async def approve(request_id: uuid.UUID, body: ApproveIn, ctx: AuthContext = Depends(APPROVE),
                  db: AsyncSession = Depends(get_db)):
    r = await svc.approve(db, ctx, request_id, totp_code=body.totp_code, financial_password=body.financial_password,
                          payload_sha256=body.payload_sha256, email_code=body.email_code)
    return await svc.request_public_named(db, r, viewer_id=ctx.user_id)


@router.post("/{request_id}/authorize")
async def authorize(request_id: uuid.UUID, body: ApproveIn, ctx: AuthContext = Depends(APPROVE),
                    db: AsyncSession = Depends(get_db)):
    """Aprovação ÚNICA (modo SINGLE): o próprio solicitante autoriza com os três fatores."""
    r = await svc.authorize(db, ctx, request_id, totp_code=body.totp_code, financial_password=body.financial_password,
                            payload_sha256=body.payload_sha256, email_code=body.email_code)
    return await svc.request_public_named(db, r, viewer_id=ctx.user_id)


@router.post("/{request_id}/reject")
async def reject(request_id: uuid.UUID, body: RejectIn, ctx: AuthContext = Depends(APPROVE),
                 db: AsyncSession = Depends(get_db)):
    r = await svc.reject(db, ctx, request_id, body.reason)
    return await svc.request_public_named(db, r, viewer_id=ctx.user_id)


@router.post("/{request_id}/cancel")
async def cancel(request_id: uuid.UUID, ctx: AuthContext = Depends(APPROVE), db: AsyncSession = Depends(get_db)):
    r = await svc.cancel(db, ctx, request_id)
    return await svc.request_public_named(db, r, viewer_id=ctx.user_id)
