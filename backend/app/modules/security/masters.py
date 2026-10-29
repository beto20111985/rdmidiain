"""
RD Mídia Indoor — Assentos de Administrador Master (ADR_011 D5/D7/D8; SPEC §2.3, §13).

Assento só muda pelas funções ``SECURITY DEFINER`` do banco (o ``rd_app`` não altera
``admin_master_seat`` diretamente; trigger ``rd_users_master_guard``). Cada atribuição/recuperação
grava um evento selado (cadeia HMAC única por ``id`` em ``admin_master_seat_events``).

Interfaces públicas (SPEC §13)::

    is_active_master_user(user, grants=None) -> bool    # mesmo predicado de rd_is_active_master
    await active_master_ids(db) -> list[UUID]           # notificações (C usa em common.admin_master_ids)
    await active_master_count(db) -> int
    await assign_seat(db, user_id=..., seat=1, source="BOOTSTRAP_ENV", invite_id=None, actor_user_id=None)
    await record_recovery(db, user_id=..., new_password_hash=..., actor_user_id=None) -> int (assento)

Nenhuma função aqui faz commit.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import SCOPE_GLOBAL
from app.core.timeutils import utcnow

log = structlog.get_logger("rd.security.masters")

SEAT_SOURCES = ("BOOTSTRAP_ENV", "MASTER_INVITE", "CLI_BOOTSTRAP", "CLI_RECOVERY", "LEGACY_ADOPTED")
SEATS = (1, 2)


def _has_super_admin_global(grants: Iterable[Any]) -> bool:
    return any(getattr(g, "role_slug", None) == "super_admin" and getattr(g, "scope_type", None) == SCOPE_GLOBAL
               for g in grants)


def is_active_master_user(user: Any, grants: Optional[Iterable[Any]] = None) -> bool:
    """Mesmo predicado de ``rd_is_active_master`` (sem consulta ao banco).

    ``user`` pode ser um ``User`` ou um ``AuthContext`` (usa ``ctx.user`` e ``ctx.grants``). Com
    ``grants`` (ou contexto), exige também ``super_admin`` GLOBAL vigente (``load_grants`` já descarta
    atribuições expiradas).
    """
    if hasattr(user, "grants") and hasattr(user, "user") and grants is None:
        grants, user = user.grants, user.user
    if user is None:
        return False
    ok = (
        getattr(user, "admin_master_seat", None) in SEATS
        and bool(getattr(user, "is_active", False))
        and getattr(user, "anonymized_at", None) is None
        and bool(getattr(user, "totp_enabled", False))
        and getattr(user, "financial_password_hash", None) is not None
        and not bool(getattr(user, "must_change_password", False))
    )
    if ok and grants is not None:
        ok = _has_super_admin_global(grants)
    return ok


async def active_master_ids(db: AsyncSession) -> list[uuid.UUID]:
    """Ids dos Masters ATIVOS (``rd_is_active_master``), por assento."""
    from app.models.users import User

    rows = await db.execute(
        select(User.id)
        .where(User.admin_master_seat.is_not(None), text("rd_is_active_master(users.id)"))
        .order_by(User.admin_master_seat)
    )
    return [r[0] for r in rows]


async def seat_holder_ids(db: AsyncSession) -> list[uuid.UUID]:
    """Todos os titulares de assento (ativos ou com setup pendente), por assento."""
    from app.models.users import User

    rows = await db.execute(select(User.id).where(User.admin_master_seat.is_not(None)).order_by(User.admin_master_seat))
    return [r[0] for r in rows]


async def active_master_count(db: AsyncSession) -> int:
    await db.flush()  # SQL puro não dispara autoflush: o predicado precisa ver mudanças pendentes da sessão
    return int((await db.execute(text("SELECT rd_active_master_count()"))).scalar_one() or 0)


async def seat_holders(db: AsyncSession) -> dict[int, Any]:
    """``{assento: User}`` dos titulares atuais."""
    from app.models.users import User

    rows = (await db.execute(select(User).where(User.admin_master_seat.is_not(None)))).scalars().all()
    return {int(u.admin_master_seat): u for u in rows}


async def active_flags(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, bool]:
    ids = list(user_ids)
    if not ids:
        return {}
    await db.flush()
    rows = await db.execute(
        text("SELECT u.id, rd_is_active_master(u.id) FROM users u WHERE u.id = ANY(:ids)"), {"ids": ids}
    )
    return {r[0]: bool(r[1]) for r in rows}


async def pending_invite(db: AsyncSession, *, for_update: bool = False) -> Any:
    """Convite ``PENDING`` ainda válido (não expirado) — no máximo um por construção."""
    from app.models.security import AdminMasterInvite as Inv

    q = (select(Inv).where(Inv.status == "PENDING", Inv.expires_at > utcnow())
         .order_by(Inv.created_at.desc()).limit(1))
    if for_update:
        q = q.with_for_update()
    return (await db.execute(q)).scalar_one_or_none()


async def second_master_status(db: AsyncSession, *, viewer_id: Optional[uuid.UUID] = None) -> str:
    """``DONE`` | ``INVITE_PENDING`` | ``OTHER_MASTER_SETUP_PENDING`` | ``NOT_INVITED`` (modo DUAL).

    ``DONE`` quando o outro assento é de um Master ativo (o setup pendente do próprio visualizador é
    tratado pelos passos bloqueantes).
    """
    holders = await seat_holders(db)
    flags = await active_flags(db, [u.id for u in holders.values()])
    if sum(1 for v in flags.values() if v) >= 2:
        return "DONE"
    if viewer_id is not None:
        others = [u for u in holders.values() if u.id != viewer_id]
        if any(flags.get(u.id) for u in others):
            return "DONE"
    else:
        others = list(holders.values())
    if await pending_invite(db) is not None:
        return "INVITE_PENDING"
    if (viewer_id is not None and others) or len(holders) >= 2:
        return "OTHER_MASTER_SETUP_PENDING"
    return "NOT_INVITED"


# ─── Escrita (funções SECURITY DEFINER) ──────────────────────────────────────

async def _lock_seats(db: AsyncSession) -> None:
    from app.modules.security import ddl

    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ddl.LOCK_MASTER_SEATS})


async def _head_seal(db: AsyncSession) -> str:
    from app.models.security import AdminMasterSeatEvent as E
    from app.modules.security import seal

    head = (await db.execute(select(E.app_seal).order_by(E.id.desc()).limit(1))).scalar_one_or_none()
    return head or seal.GENESIS


def _raise_mapped(exc: DBAPIError) -> None:
    from app.modules.security.approval_mode import map_db_error

    mapped = map_db_error(exc)
    if mapped is not None:
        raise mapped from None
    raise exc


async def assign_seat(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    seat: int,
    source: str,
    invite_id: Optional[uuid.UUID] = None,
    actor_user_id: Optional[uuid.UUID] = None,
) -> None:
    """Atribui (ou adota, ``LEGACY_ADOPTED``) o assento pela função ``rd_security_assign_master_seat``.

    O selo é calculado sob o lock de assentos (cadeia única); a função revalida a máquina de estados,
    concede ``super_admin`` GLOBAL e grava o evento ``ASSIGNED``. Não faz commit nem audita (o chamador
    audita com o contexto da operação: bootstrap, convite, CLI, adoção).
    """
    from app.modules.security import seal

    if seat not in SEATS:
        raise ValueError("assento inválido")
    if source not in SEAT_SOURCES or source == "CLI_RECOVERY":
        raise ValueError("origem inválida")
    await _lock_seats(db)
    prev = await _head_seal(db)
    app_seal = seal.seat_seal(prev_seal=prev, seat=seat, user_id=user_id, event="ASSIGNED", source=source,
                              invite_id=invite_id, actor_user_id=actor_user_id)
    try:
        await db.execute(
            text("SELECT rd_security_assign_master_seat(:uid, CAST(:seat AS smallint), :source, :invite, :actor, "
                 ":prev, :seal)"),
            {"uid": user_id, "seat": seat, "source": source, "invite": invite_id, "actor": actor_user_id,
             "prev": prev, "seal": app_seal},
        )
    except DBAPIError as exc:
        _raise_mapped(exc)
    log.warning("master_seat_assigned", seat=seat, source=source, user_id=str(user_id))
    # D1: selo de credencial do novo titular na MESMA transação (adoção legada nunca lava selo adulterado)
    from app.modules.security.credentials import reseal_after_definer

    await reseal_after_definer(db, user_id, require_valid_or_null=(source == "LEGACY_ADOPTED"),
                               origin=f"assign_seat:{source}")


async def record_recovery(
    db: AsyncSession, *, user_id: uuid.UUID, new_password_hash: str, actor_user_id: Optional[uuid.UUID] = None
) -> int:
    """Recuperação controlada (``rd_security_recover_master``): zera TOTP/códigos/senha financeira, senha
    temporária com troca obrigatória, evento selado ``RECOVERED``. Mesmo titular. Retorna o assento.

    Revogação de sessões/cache, auditoria e notificações: chamador (CLI ``recover-master``).
    """
    from app.models.users import User
    from app.modules.security import seal

    await _lock_seats(db)
    seat = (await db.execute(select(User.admin_master_seat).where(User.id == user_id))).scalar_one_or_none()
    if seat is None:
        raise ValueError("usuário não ocupa assento de Admin Master")
    prev = await _head_seal(db)
    app_seal = seal.seat_seal(prev_seal=prev, seat=int(seat), user_id=user_id, event="RECOVERED",
                              source="CLI_RECOVERY", invite_id=None, actor_user_id=actor_user_id)
    try:
        out = (
            await db.execute(
                text("SELECT rd_security_recover_master(:uid, :pwh, :actor, :prev, :seal)"),
                {"uid": user_id, "pwh": new_password_hash, "actor": actor_user_id, "prev": prev, "seal": app_seal},
            )
        ).scalar_one()
    except DBAPIError as exc:
        _raise_mapped(exc)
    log.warning("master_recovered", seat=int(out), user_id=str(user_id))
    # D1: a recuperação (operador com shell + chave) é o remédio também para selo inválido: re-sela sempre
    from app.modules.security.credentials import reseal_after_definer

    await reseal_after_definer(db, user_id, origin="recover_master")
    return int(out)


__all__ = [
    "is_active_master_user",
    "active_master_ids",
    "active_master_count",
    "seat_holders",
    "seat_holder_ids",
    "second_master_status",
    "pending_invite",
    "assign_seat",
    "record_recovery",
]
