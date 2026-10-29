"""
RD Mídia Indoor — Configurações de negócio (system_settings).

GET /settings (settings.view) · PUT /settings/{key} (settings.manage).
Chaves financeiras → 409 ``SETTING_REQUIRES_DUAL_APPROVAL`` (fluxo do agente financeiro).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.settings_service import get_all_settings, set_setting

router = APIRouter()


@router.get("")
async def list_settings(ctx: AuthContext = Depends(require("settings.view", global_only=True)),
                        db: AsyncSession = Depends(get_db)):
    return {"items": await get_all_settings(db)}


class SettingPut(BaseModel):
    value: Any
    reason: Optional[str] = Field(default=None, max_length=1000)
    expected_version: Optional[int] = Field(default=None, ge=0)


@router.put("/{key}")
async def put_setting(key: str, body: SettingPut,
                      ctx: AuthContext = Depends(require("settings.manage", global_only=True,
                                                         allow_impersonation=False)),
                      db: AsyncSession = Depends(get_db)):
    return await set_setting(db, ctx, key, body.value, reason=body.reason, expected_version=body.expected_version)
