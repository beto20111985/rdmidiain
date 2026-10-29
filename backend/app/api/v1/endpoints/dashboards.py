"""RD Mídia Indoor — Dashboards por perfil (AÇÃO 002 / W-BIZ). Prefixo ``/api/v1/dashboards``.

Período comum dos widgets (DS §9/§12): ``start``/``end`` (datas, dia local America/Sao_Paulo; máx.
366 dias). Sem parâmetros = mês corrente até hoje (comportamento padrão). Indicadores de ESTADO
(online/offline, ativas, aguardando aprovação…) são do momento atual; os de FLUXO usam o período
(campos ``*_period``; os ``*_month``/``*_today`` continuam como antes). A resposta traz ``period``.
Sem polling: o frontend invalida estas consultas nos eventos WS do contrato §5.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.modules.dashboards import service as svc

router = APIRouter()


@router.get("/admin")
async def admin(start: Optional[date] = None, end: Optional[date] = None,
                ctx: AuthContext = Depends(require("dashboard.view")), db: AsyncSession = Depends(get_db)):
    return await svc.admin(db, ctx, start, end)


@router.get("/admin/capacity")
async def admin_capacity(day: Optional[date] = None, ctx: AuthContext = Depends(require("dashboard.view", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    return await svc.admin_capacity(db, ctx, day)


@router.get("/establishment")
async def establishment(org_id: UUID, start: Optional[date] = None, end: Optional[date] = None,
                        ctx: AuthContext = Depends(require("dashboard.view")), db: AsyncSession = Depends(get_db)):
    return await svc.establishment(db, ctx, org_id, start, end)


@router.get("/advertiser")
async def advertiser(org_id: UUID, start: Optional[date] = None, end: Optional[date] = None,
                     ctx: AuthContext = Depends(require("dashboard.view")), db: AsyncSession = Depends(get_db)):
    return await svc.advertiser(db, ctx, org_id, start, end)
