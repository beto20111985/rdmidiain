"""
RD Mídia Indoor — Consulta e exportação da auditoria (contrato §4).

GET /audit (audit.view) · GET /audit/export (audit.export, CSV) · GET /audit/verify
Escopo: quem tem a permissão em GLOBAL vê tudo; com escopo de organização vê apenas
registros com ``org_id`` das suas organizações.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit, verify_chain
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.models.audit import AuditLog

router = APIRouter()

EXPORT_MAX_ROWS = 50_000


def _filtered(ctx: AuthContext, perm: str, actor_user_id, action, entity_type, entity_id, org_id, date_from, date_to,
              impersonated: Optional[bool] = None):
    q = select(AuditLog).where(ctx.scope_filter(perm, org_col=AuditLog.org_id))
    if actor_user_id:
        q = q.where((AuditLog.actor_user_id == actor_user_id) | (AuditLog.real_user_id == actor_user_id))
    if action:
        q = q.where(AuditLog.action.like(action.replace("*", "%")) if "*" in action else AuditLog.action == action)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    if org_id:
        q = q.where(AuditLog.org_id == org_id)
    if date_from:
        q = q.where(AuditLog.created_at >= date_from)
    if date_to:
        q = q.where(AuditLog.created_at < date_to)
    if impersonated is True:  # ações feitas durante impersonation (usuário real registrado)
        q = q.where(AuditLog.real_user_id.is_not(None))
    elif impersonated is False:
        q = q.where(AuditLog.real_user_id.is_(None))
    return q


def _row(a: AuditLog) -> dict:
    return {
        "id": str(a.id),
        "seq": a.seq,
        "created_at": a.created_at,
        "actor_user_id": str(a.actor_user_id) if a.actor_user_id else None,
        "real_user_id": str(a.real_user_id) if a.real_user_id else None,
        "actor_device_id": str(a.actor_device_id) if a.actor_device_id else None,
        "org_id": str(a.org_id) if a.org_id else None,
        "action": a.action,
        "entity_type": a.entity_type,
        "entity_id": a.entity_id,
        "before": a.before_data,
        "after": a.after_data,
        "reason": a.reason,
        "ip": a.ip_address,
        "user_agent": a.user_agent,
        "origin": a.origin,
        "request_id": a.request_id,
        "row_hash": a.row_hash,
    }


@router.get("")
async def list_audit(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    actor_user_id: Optional[uuid.UUID] = None,
    action: Optional[str] = Query(None, max_length=100),
    entity_type: Optional[str] = Query(None, max_length=100),
    entity_id: Optional[str] = Query(None, max_length=255),
    org_id: Optional[uuid.UUID] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    impersonated: Optional[bool] = Query(None, description="true = só ações durante impersonation; false = só fora"),
    ctx: AuthContext = Depends(require("audit.view")),
    db: AsyncSession = Depends(get_db),
):
    q = _filtered(ctx, "audit.view", actor_user_id, action, entity_type, entity_id, org_id, date_from, date_to,
                  impersonated)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(AuditLog.seq.desc()).offset((page - 1) * page_size).limit(page_size))).scalars()
    return {"items": [_row(a) for a in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/export")
async def export_audit(
    actor_user_id: Optional[uuid.UUID] = None,
    action: Optional[str] = Query(None, max_length=100),
    entity_type: Optional[str] = Query(None, max_length=100),
    entity_id: Optional[str] = Query(None, max_length=255),
    org_id: Optional[uuid.UUID] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    impersonated: Optional[bool] = Query(None, description="true = só ações durante impersonation; false = só fora"),
    ctx: AuthContext = Depends(require("audit.export", allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    q = _filtered(ctx, "audit.export", actor_user_id, action, entity_type, entity_id, org_id, date_from, date_to,
                  impersonated)
    rows = (await db.execute(q.order_by(AuditLog.seq).limit(EXPORT_MAX_ROWS))).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["seq", "created_at", "actor_user_id", "real_user_id", "actor_device_id", "org_id", "action",
            "entity_type", "entity_id", "before", "after", "reason", "ip", "user_agent", "origin", "request_id",
            "row_hash"]
    w.writerow(cols)
    for a in rows:
        r = _row(a)
        out = []
        for c in cols:
            v = r[c]
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False, default=str)
            elif isinstance(v, datetime):
                v = v.isoformat()
            s = "" if v is None else str(v)
            if s[:1] in ("=", "+", "-", "@"):  # proteção contra injeção de fórmula (CSV)
                s = "'" + s
            out.append(s)
        w.writerow(out)
    await audit(db, ctx, "audit.exported", "audit_log", None, after={"rows": len(rows)})
    data = buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        iter([data]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="auditoria.csv"'},
    )


@router.get("/verify")
async def verify_audit_chain(ctx: AuthContext = Depends(require("audit.view", global_only=True)),
                             db: AsyncSession = Depends(get_db)):
    rep = await verify_chain(db)
    return {"ok": rep.ok, "rows_checked": rep.rows_checked, "first_bad_seq": rep.first_bad_seq, "problem": rep.problem}
