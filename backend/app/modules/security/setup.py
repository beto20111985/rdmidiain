"""
RD Mídia Indoor — Máquina de estados do primeiro acesso (ADR_011 D6; SPEC §3.1).

Passos BLOQUEANTES, nesta ordem, calculados (nada novo armazenado):
``PASSWORD_CHANGE`` (``must_change_password``) → ``TOTP`` (perfil com 2FA obrigatório sem TOTP) →
``FINANCIAL_PASSWORD`` (titular de assento sem senha financeira). Passo NÃO bloqueante:
``SECOND_MASTER`` (modo DUAL, titular de assento, enquanto não houver 2 Masters ativos).

O backend é a autoridade (``app.core.authz._check_unlocked`` usa ``blocking_step``); o frontend só
melhora a UX. ``SetupState`` nunca contém segredo, token ou e-mail completo de terceiros.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD_CHANGE = "PASSWORD_CHANGE"
TOTP = "TOTP"
FINANCIAL_PASSWORD = "FINANCIAL_PASSWORD"
SECOND_MASTER = "SECOND_MASTER"
BLOCKING_STEPS = (PASSWORD_CHANGE, TOTP, FINANCIAL_PASSWORD)

GUARD_ERRORS = {
    PASSWORD_CHANGE: ("PASSWORD_CHANGE_REQUIRED", "Defina uma nova senha para continuar."),
    TOTP: ("MFA_SETUP_REQUIRED", "Configure a verificação em duas etapas para continuar."),
    FINANCIAL_PASSWORD: ("FINANCIAL_PASSWORD_SETUP_REQUIRED", "Crie sua senha financeira para continuar."),
}


def _mfa_required(grants: Iterable[Any]) -> bool:
    from app.core.permissions import MFA_REQUIRED_ROLES

    return any(getattr(g, "role_mfa_required", False) or getattr(g, "role_slug", None) in MFA_REQUIRED_ROLES
               for g in grants)


def is_seat_holder(user: Any) -> bool:
    return getattr(user, "admin_master_seat", None) is not None


def blocking_step(user: Any, grants: Iterable[Any]) -> Optional[str]:
    """Primeiro passo bloqueante pendente (ou ``None``)."""
    if user is None:
        return None
    if bool(getattr(user, "must_change_password", False)):
        return PASSWORD_CHANGE
    if _mfa_required(grants) and not bool(getattr(user, "totp_enabled", False)):
        return TOTP
    if is_seat_holder(user) and not getattr(user, "financial_password_hash", None):
        return FINANCIAL_PASSWORD
    return None


async def setup_state(db: AsyncSession, user: Any, grants: Iterable[Any]) -> dict:
    """``SetupState`` (SPEC §3.1) — mesmo formato em ``/auth/setup/status``, ``/me.security_setup`` e login."""
    grants = tuple(grants)
    mfa_required = _mfa_required(grants)
    seat_holder = is_seat_holder(user)
    steps = {
        "password_change": "PENDING" if getattr(user, "must_change_password", False) else "DONE",
        "totp": ("DONE" if getattr(user, "totp_enabled", False) else "PENDING") if mfa_required else "NOT_REQUIRED",
        "financial_password": (
            ("DONE" if getattr(user, "financial_password_hash", None) else "PENDING") if seat_holder else "NOT_REQUIRED"
        ),
        "second_master": "NOT_REQUIRED",
    }
    approval_mode: Optional[str] = None
    dual_ready: Optional[bool] = None
    if seat_holder:
        from app.modules.security import approval_mode as am
        from app.modules.security import masters

        state = await am.current_state(db)
        approval_mode = state.mode if state else None
        if approval_mode == "DUAL":
            dual_ready = await masters.active_master_count(db) >= 2
            steps["second_master"] = (
                "DONE" if dual_ready else await masters.second_master_status(db, viewer_id=getattr(user, "id", None))
            )
    blocking = blocking_step(user, grants)
    next_step = blocking
    if next_step is None and steps["second_master"] in ("NOT_INVITED", "INVITE_PENDING", "OTHER_MASTER_SETUP_PENDING"):
        next_step = SECOND_MASTER
    return {
        "required": blocking is not None,
        "next_step": next_step,
        "steps": steps,
        "is_admin_master": seat_holder,
        "approval_mode": approval_mode,
        "dual_approval_ready": dual_ready,
    }


__all__ = ["blocking_step", "setup_state", "GUARD_ERRORS", "BLOCKING_STEPS"]
