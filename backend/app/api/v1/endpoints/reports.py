"""RD Mídia Indoor — Relatórios, exportações e comprovante de veiculação (AÇÃO 002 / W-BIZ).

Prefixo ``/api/v1/reports``:
- ``GET  /catalog``                     relatórios disponíveis para o perfil (equipe/anunciante/estabelecimento)
- ``GET  /data/{report_key}``           dados (JSON) — ``start``, ``end``, ``audience``, ``org_id``, filtros
- ``POST /exports``                     gera CSV/XLSX/PDF reais (StorageProvider) e, opcionalmente, envia por e-mail
- ``GET  /exports`` / ``GET /exports/{id}/download``  (somente do solicitante)
- ``POST /proofs``                      emite comprovante de veiculação assinado (somente PoP válido)
- ``GET  /proofs`` / ``GET /proofs/{id}/pdf``
- ``GET  /proofs/{code}/verification``  (público) dados para verificação offline da assinatura
- ``POST /proofs/verify``               (público) envia o PDF e confere hash + assinatura

Removidos (legado quebrado): ``/dashboard`` (→ ``/dashboards/*``), ``/playback``, ``/financial``,
``/campaigns/{id}/performance``, ``/screens/{id}/performance`` (usavam colunas inexistentes).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.core.timeutils import isoformat_z, utcnow
from app.models.reports import DeliveryProof, ReportExport
from app.modules.reports import data as rdata
from app.modules.reports import service as svc

router = APIRouter()
Audience = Literal["EQUIPE", "ANUNCIANTE", "ESTABELECIMENTO"]


def _z(dt: Optional[datetime]) -> Optional[str]:
    return isoformat_z(dt) if dt else None


@router.get("/catalog")
async def catalog(audience: Optional[Audience] = None, org_id: Optional[UUID] = None,
                  ctx: AuthContext = Depends(require("reports.view"))):
    scope = rdata.resolve_scope(ctx, audience=audience, org_id=org_id)
    return {"audience": scope.audience, "items": rdata.available_reports(scope)}


@router.get("/data/{report_key}")
async def report_data(
    report_key: str, start: Optional[date] = None, end: Optional[date] = None,
    audience: Optional[Audience] = None, org_id: Optional[UUID] = None,
    campaign_id: Optional[UUID] = None, screen_id: Optional[UUID] = None,
    kind: Optional[Literal["EXTERNAL", "INTERNAL", "INSTITUTIONAL", "FALLBACK"]] = None,
    group_by: Optional[str] = Query(None, max_length=20),
    ctx: AuthContext = Depends(require("reports.view")), db: AsyncSession = Depends(get_db),
):
    scope = rdata.resolve_scope(ctx, audience=audience, org_id=org_id)
    period = rdata.parse_period(start, end)
    ds = await rdata.build(db, ctx, report_key, scope, period,
                           {"campaign_id": campaign_id, "screen_id": screen_id, "kind": kind, "group_by": group_by})
    return {"title": ds.title, "audience": scope.audience,
            "columns": [{"key": c.key, "label": c.label, "kind": c.kind} for c in ds.columns],
            "rows": ds.rows, "meta": [{"label": k, "value": v} for k, v in ds.meta], "notes": ds.notes,
            "total": len(ds.rows)}


class ExportIn(BaseModel):
    report_key: str = Field(min_length=3, max_length=60)
    format: Literal["csv", "xlsx", "pdf"]
    start: Optional[date] = None
    end: Optional[date] = None
    audience: Optional[Audience] = None
    org_id: Optional[UUID] = None
    campaign_id: Optional[UUID] = None
    screen_id: Optional[UUID] = None
    kind: Optional[Literal["EXTERNAL", "INTERNAL", "INSTITUTIONAL", "FALLBACK"]] = None
    group_by: Optional[str] = Field(None, max_length=20)
    email_to_me: bool = False


def _export_out(e: ReportExport) -> dict:
    return {"id": str(e.id), "report_key": e.report_key, "format": e.format, "status": e.status,
            "filename": e.filename, "size_bytes": e.size_bytes, "sha256": e.sha256, "row_count": e.row_count,
            "emailed_to": e.emailed_to, "params": e.params, "created_at": _z(e.created_at),
            "expires_at": _z(e.expires_at), "download_url": f"/api/v1/reports/exports/{e.id}/download"}


@router.post("/exports", status_code=201)
async def create_export(body: ExportIn, ctx: AuthContext = Depends(require("reports.view")),
                        db: AsyncSession = Depends(get_db)):
    scope = rdata.resolve_scope(ctx, audience=body.audience, org_id=body.org_id)
    period = rdata.parse_period(body.start, body.end)
    e, _ = await svc.create_export(db, ctx, key=body.report_key, scope=scope, period=period,
                                   params={"campaign_id": body.campaign_id, "screen_id": body.screen_id,
                                           "kind": body.kind, "group_by": body.group_by},
                                   fmt=body.format, email_to_me=body.email_to_me)
    return _export_out(e)


@router.get("/exports")
async def list_exports(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                       ctx: AuthContext = Depends(require("reports.view")), db: AsyncSession = Depends(get_db)):
    cond = [ReportExport.requested_by == ctx.user_id]
    total = (await db.execute(select(func.count()).select_from(ReportExport).where(*cond))).scalar_one()
    rows = (await db.execute(select(ReportExport).where(*cond).order_by(ReportExport.created_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars()
    return {"items": [_export_out(e) for e in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/exports/{export_id}/download")
async def download_export(export_id: UUID, ctx: AuthContext = Depends(require("reports.view")),
                          db: AsyncSession = Depends(get_db)):
    from app.core.storage import get_storage

    e = await db.get(ReportExport, export_id)
    if e is None or e.requested_by != ctx.user_id or e.storage_key is None:
        raise NotFound()
    if e.expires_at and e.expires_at < utcnow():
        raise DomainError("EXPORT_EXPIRED", "Arquivo expirado; gere novamente.", status=410)
    body = await get_storage().open_range(e.storage_key, 0, e.size_bytes - 1)
    return StreamingResponse(body, media_type=e.content_type, headers={
        "Content-Disposition": f'attachment; filename="{e.filename}"', "Content-Length": str(e.size_bytes),
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


# ─── Comprovante de veiculação ───────────────────────────────────────────────

class ProofIn(BaseModel):
    campaign_id: UUID
    start: Optional[date] = None
    end: Optional[date] = None


def _proof_out(p: DeliveryProof) -> dict:
    return {"id": str(p.id), "code": p.code, "campaign_id": str(p.campaign_id), "org_id": str(p.org_id),
            "period_start": p.period_start.isoformat(), "period_end": p.period_end.isoformat(),
            "total_valid_plays": p.total_valid_plays, "source": p.source, "pdf_sha256": p.pdf_sha256,
            "payload_sha256": p.payload_sha256, "kid": p.kid, "created_at": _z(p.created_at),
            "pdf_url": f"/api/v1/reports/proofs/{p.id}/pdf"}


@router.post("/proofs", status_code=201)
async def create_proof(body: ProofIn, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    proof, _pdf = await svc.create_proof(db, ctx, body.campaign_id, body.start, body.end)
    return _proof_out(proof)


@router.get("/proofs")
async def list_proofs(campaign_id: Optional[UUID] = None, ctx: AuthContext = Depends(get_auth_context),
                      db: AsyncSession = Depends(get_db)):
    cond = [ctx.scope_filter("reports.view", org_col=DeliveryProof.org_id)]
    if campaign_id:
        cond.append(DeliveryProof.campaign_id == campaign_id)
    rows = (await db.execute(select(DeliveryProof).where(*cond).order_by(DeliveryProof.created_at.desc()).limit(200))).scalars()
    items = [_proof_out(p) for p in rows]
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.get("/proofs/{proof_id}/pdf")
async def proof_pdf(proof_id: UUID, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    from app.core.storage import get_storage

    p = await db.get(DeliveryProof, proof_id)
    if p is None or not (ctx.can("reports.view", org_id=p.org_id) or ctx.can("campaigns.view", org_id=p.org_id)):
        raise NotFound()
    data = await get_storage().read_bytes(p.pdf_key, max_bytes=50 * 1024 * 1024)
    return Response(content=data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="comprovante-{p.code}.pdf"', "Cache-Control": "private, no-store"})


@router.get("/proofs/{code}/verification")
async def proof_verification(code: str, db: AsyncSession = Depends(get_db)):
    """Público: dados mínimos para verificação offline (sem dados comerciais além do que o PDF já contém)."""
    p = (await db.execute(select(DeliveryProof).where(DeliveryProof.code == code.upper()))).scalar_one_or_none()
    if p is None:
        raise NotFound()
    return {"code": p.code, "issued_at": p.payload.get("issued_at"), "payload_sha256": p.payload_sha256,
            "payload_signature": p.payload_signature, "pdf_sha256": p.pdf_sha256, "pdf_signature": p.pdf_signature,
            "kid": p.kid, "alg": "ES256", "public_keys_url": "/api/v1/instance/signing-keys",
            "note": "Assinatura eletrônica ES256 da instância; não é certificado ICP-Brasil."}


@router.post("/proofs/verify")
async def verify_proof(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    data = await file.read(50 * 1024 * 1024 + 1)
    if len(data) > 50 * 1024 * 1024:
        raise DomainError("FILE_TOO_LARGE", "Arquivo grande demais.", status=413)
    return await svc.verify_pdf(db, data)
