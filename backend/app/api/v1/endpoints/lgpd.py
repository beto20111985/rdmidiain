"""
RD Mídia Indoor — Processamento LGPD (lgpd.manage).

GET  /lgpd/deletion-requests
POST /lgpd/deletion-requests/{id}/process  → ANONIMIZA o titular
POST /lgpd/deletion-requests/{id}/reject

Anonimização: nome/e-mail/telefone substituídos, senha invalidada, 2FA e códigos
removidos, sessões revogadas, atribuições removidas, conta inativa. PRESERVADOS
(obrigação legal / exercício regular de direitos): registros financeiros, trilha de
auditoria (append-only) e registros de consentimento — vinculados ao id
pseudonimizado.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import Conflict, DomainError, NotFound
from app.core.passwords import random_unusable_hash
from app.core.timeutils import utcnow
from app.models.lgpd import DataDeletionRequest
from app.models.rbac import RoleAssignment
from app.models.users import PasswordResetCode, User, UserInvite, UserRecoveryCode
from app.modules.auth import service as svc

router = APIRouter()


@router.get("/deletion-requests")
async def list_requests(
    status: Optional[str] = Query(None, pattern="^(PENDING|COMPLETED|REJECTED|CANCELLED)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("lgpd.manage", global_only=True)),
    db: AsyncSession = Depends(get_db),
):
    q = select(DataDeletionRequest, User.full_name).join(User, User.id == DataDeletionRequest.user_id)
    if status:
        q = q.where(DataDeletionRequest.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(DataDeletionRequest.requested_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).all()
    return {
        "items": [
            {"id": str(r.id), "user_id": str(r.user_id), "user_name": name, "status": r.status,
             "reason": r.reason, "requested_at": r.requested_at, "processed_at": r.processed_at}
            for r, name in rows
        ],
        "total": total, "page": page, "page_size": page_size,
    }


class ProcessIn(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=2000)


async def anonymize_user(db: AsyncSession, user: User) -> list:
    now = utcnow()
    old_email = user.email
    user.full_name = "Titular anonimizado"
    user.email = f"anon-{user.id}@anonimizado.invalid"
    user.phone = None
    user.hashed_password = random_unusable_hash()
    user.financial_password_hash = None
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_pending_secret_enc = None
    user.is_active = False
    user.anonymized_at = now
    await db.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id))
    await db.execute(delete(PasswordResetCode).where(PasswordResetCode.user_id == user.id))
    await db.execute(delete(RoleAssignment).where(RoleAssignment.user_id == user.id))
    for inv in (await db.execute(select(UserInvite).where(UserInvite.email == old_email, UserInvite.accepted_at.is_(None)))).scalars():
        inv.revoked_at = now
    return await svc.revoke_all_user_sessions(db, user.id, "lgpd_anonymized")


@router.post("/deletion-requests/{request_id}/process")
async def process_request(request_id: uuid.UUID, body: ProcessIn,
                          ctx: AuthContext = Depends(require("lgpd.manage", global_only=True,
                                                             allow_impersonation=False)),
                          db: AsyncSession = Depends(get_db)):
    req = (await db.execute(select(DataDeletionRequest).where(DataDeletionRequest.id == request_id)
                            .with_for_update())).scalar_one_or_none()
    if req is None:
        raise NotFound()
    if req.status != "PENDING":
        raise Conflict("REQUEST_NOT_PENDING", "Solicitação já processada.")
    user = (await db.execute(select(User).where(User.id == req.user_id).with_for_update())).scalar_one()
    if user.is_admin_master:
        raise DomainError("ADMIN_MASTER_PROTECTED", "Admin Master não pode ser anonimizado por esta rota.", status=403)
    if user.id == ctx.user_id:
        raise DomainError("SELF_PROCESSING", "Outra pessoa deve processar sua solicitação.", status=403)
    revoked = await anonymize_user(db, user)
    req.status = "COMPLETED"
    req.processed_by = ctx.user_id
    req.processed_at = utcnow()
    req.resolution_notes = body.notes
    await audit(db, ctx, "lgpd.user_anonymized", "user", user.id, reason=body.notes,
                after={"deletion_request_id": req.id})
    await svc.after_commit_revocations(revoked)
    return {"id": str(req.id), "status": req.status}


@router.post("/deletion-requests/{request_id}/reject")
async def reject_request(request_id: uuid.UUID, body: ProcessIn,
                         ctx: AuthContext = Depends(require("lgpd.manage", global_only=True,
                                                            allow_impersonation=False)),
                         db: AsyncSession = Depends(get_db)):
    req = (await db.execute(select(DataDeletionRequest).where(DataDeletionRequest.id == request_id)
                            .with_for_update())).scalar_one_or_none()
    if req is None:
        raise NotFound()
    if req.status != "PENDING":
        raise Conflict("REQUEST_NOT_PENDING", "Solicitação já processada.")
    if not body.notes or len(body.notes.strip()) < 10:
        raise DomainError("REASON_REQUIRED", "Informe a justificativa (mín. 10 caracteres).", status=400)
    req.status = "REJECTED"
    req.processed_by = ctx.user_id
    req.processed_at = utcnow()
    req.resolution_notes = body.notes
    await audit(db, ctx, "lgpd.deletion_rejected", "data_deletion_request", req.id, reason=body.notes)
    return {"id": str(req.id), "status": req.status}
