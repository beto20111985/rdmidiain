"""
RD Mídia Indoor — SHIM DE COMPATIBILIDADE para rotas legadas.

Rotas novas devem usar ``app.core.authz`` (``require``, ``AuthContext``, escopos).

Este módulo mantém as assinaturas antigas enquanto os agentes de domínio reescrevem
os endpoints:
- ``get_current_user`` → valida o access token (sessão não revogada, 2FA
  obrigatório configurado) e devolve o ``User``.
- ``require_permission("recurso:acao")`` → traduz o código legado via
  ``LEGACY_PERMISSION_MAP`` e exige a permissão em escopo **GLOBAL** (as rotas
  legadas não filtram por organização; exigir GLOBAL impede vazamento entre
  organizações — corrige A2 E7/A7 C2 até a reescrita).
- ``require_super_admin()`` → exige perfil super_admin em escopo GLOBAL.
O antigo ``_check_user_permission`` (que ignorava a organização) foi removido.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tokens
from app.core.authz import AuthContext, _resolve, get_auth_context
from app.core.database import get_db
from app.core.errors import DomainError, Forbidden
from app.core.permissions import LEGACY_PERMISSION_MAP


async def get_current_user(ctx: AuthContext = Depends(get_auth_context)):
    return ctx.user


async def get_current_user_optional(request: Request, db: AsyncSession = Depends(get_db)):
    if not (request.headers.get("authorization") or "").lower().startswith("bearer "):
        return None
    try:
        ctx = await _resolve(request, db, allowed_typs=(tokens.TYP_ACCESS,))
    except DomainError:
        return None
    return ctx.user


def require_permission(permission_code: str):
    perm = LEGACY_PERMISSION_MAP.get(permission_code)
    if perm is None:
        raise ValueError(f"Código legado sem mapeamento no catálogo: {permission_code}")

    async def check_permission(ctx: AuthContext = Depends(get_auth_context)):
        if not ctx.can(perm, global_only=True):
            raise Forbidden(details={"permission": perm})
        return ctx.user

    check_permission.__rd_permission__ = perm  # type: ignore[attr-defined]
    check_permission.__rd_legacy_code__ = permission_code  # type: ignore[attr-defined]
    check_permission.__rd_global_only__ = True  # type: ignore[attr-defined]
    return check_permission


def require_super_admin():
    async def check_super_admin(ctx: AuthContext = Depends(get_auth_context)):
        if not ctx.is_super_admin:
            raise Forbidden("SUPER_ADMIN_REQUIRED", "Acesso restrito a Super Administradores.")
        return ctx.user

    return check_super_admin


__all__ = ["get_current_user", "get_current_user_optional", "require_permission", "require_super_admin"]
