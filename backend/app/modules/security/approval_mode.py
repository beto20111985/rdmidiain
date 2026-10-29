"""
RD Mídia Indoor — Modo de aprovação financeira da instância (ADR_011 D1/D2/D10; SPEC §4.1, §8, §13).

Fonte da verdade: ``instance_approval_mode_history`` (append-only, selada por HMAC da
aplicação, gravável só pelas funções ``SECURITY DEFINER`` do agente E). **Modo efetivo =
linha de maior ``mode_version``.** O ENV (``RD_BOOTSTRAP_DUAL_MASTER_APPROVAL``) só é lido
pelo ``bootstrap-instance`` enquanto a trilha está vazia (``app.modules.security.bootstrap``).

Interfaces públicas (contrato congelado — SPEC §13)::

    state = await current_state(db)                     # ModeState | None (sem integridade)
    state = await effective_mode(db)                    # integridade; 503 se falhar/não inicializado
    state = await ensure_financial_authorization_allowed(db, exclusive=False)   # §4.1 (lock + 409)
    state, created = await initialize(db, mode="DUAL", source="BOOTSTRAP_ENV", env_value="true")
    state = await apply_transition(db, ctx, req)        # C chama em _execute (APPROVAL_MODE_CHANGE)
    report = await verify_integrity(db)                 # IntegrityReport(ok, problems)

Nunca registra/imprime selo, hash ou segredo — só ids, modo, versão e descrições.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, DomainError, ServiceUnavailable

log = structlog.get_logger("rd.security.approval_mode")

MODES = ("SINGLE", "DUAL")
MODE_SOURCES = ("BOOTSTRAP_ENV", "LEGACY_DUAL_ADOPTED")

SECOND_MASTER_REQUIRED_MSG = (
    "Esta instalação usa dupla aprovação financeira. Cadastre e ative o segundo Administrador Master "
    "para liberar esta operação."
)


@dataclass(frozen=True)
class ModeState:
    mode: str
    version: int
    head_seal: str

    def __repr__(self) -> str:  # nunca expor o selo em logs/tracebacks
        return f"ModeState(mode={self.mode!r}, version={self.version})"


@dataclass
class IntegrityReport:
    ok: bool
    problems: list[str] = field(default_factory=list)


# ─── Erros ───────────────────────────────────────────────────────────────────

def not_initialized() -> DomainError:
    return ServiceUnavailable("INSTANCE_NOT_INITIALIZED", "Instância não inicializada. Contate o responsável técnico.")


def integrity_alert() -> DomainError:
    return ServiceUnavailable(
        "SECURITY_INTEGRITY_ALERT",
        "Verificação de integridade da segurança falhou. Operações financeiras suspensas. "
        "Contate o responsável técnico.",
    )


_RD_CODE = re.compile(r"\b(RD_[A-Z0-9_]+)\b")


def rd_error_code(exc: BaseException) -> Optional[str]:
    """Código ``RD_<CODIGO>`` levantado por função/trigger do banco (``RAISE EXCEPTION 'RD_…: …'``)."""
    orig = getattr(exc, "orig", None)
    for candidate in (orig, exc):
        if candidate is None:
            continue
        m = _RD_CODE.search(str(candidate))
        if m:
            return m.group(1)
    return None


# Códigos do banco → erro da API (nunca 500)
_DB_ERRORS: dict[str, tuple[int, str, str]] = {
    "RD_INSTANCE_NOT_INITIALIZED": (503, "INSTANCE_NOT_INITIALIZED",
                                    "Instância não inicializada. Contate o responsável técnico."),
    "RD_SEAL_CHAIN_MISMATCH": (503, "SECURITY_INTEGRITY_ALERT",
                               "Verificação de integridade da segurança falhou. Operações financeiras suspensas. "
                               "Contate o responsável técnico."),
    "RD_SECOND_MASTER_REQUIRED": (409, "SECOND_MASTER_REQUIRED", SECOND_MASTER_REQUIRED_MSG),
    "RD_APPROVAL_MODE_MISMATCH": (409, "APPROVAL_MODE_CHANGED", "O modo de aprovação mudou; crie uma nova solicitação."),
    "RD_APPROVER_NOT_ACTIVE_MASTER": (409, "APPROVER_NOT_ACTIVE_MASTER",
                                      "Somente um Administrador Master ativo pode aprovar."),
    "RD_SELF_AUTHORIZER_INVALID": (409, "SELF_AUTHORIZER_INVALID",
                                   "Somente o solicitante (Administrador Master ativo) pode autorizar."),
    "RD_REQUESTER_NOT_ACTIVE_MASTER": (409, "REQUESTER_NOT_ACTIVE_MASTER",
                                       "Somente um Administrador Master ativo pode solicitar."),
    "RD_SEAL_INVALID": (503, "SECURITY_INTEGRITY_ALERT",
                        "Verificação de integridade da segurança falhou. Operações financeiras suspensas. "
                        "Contate o responsável técnico."),
    "RD_MASTER_SEAT_OCCUPIED": (409, "ADMIN_MASTER_SEATS_FULL",
                                "Os dois assentos de Administrador Master já estão ocupados."),
    "RD_MASTER_INVITE_INVALID": (400, "MASTER_INVITE_INVALID", "Convite inválido ou expirado."),
    "RD_MASTER_INVITE_INVALID_STATE": (409, "MASTER_INVITE_INVALID_STATE",
                                       "Estado inválido para o convite do segundo Administrador Master."),
    # D1 (0009): escrita sem selo novo/valido — estado inconsistente: falha fechada, nunca 500
    "RD_MASTER_CREDENTIAL_SEAL_REQUIRED": (503, "SECURITY_INTEGRITY_ALERT",
                                           "Verificação de integridade da segurança falhou. Contate o responsável "
                                           "técnico."),
    "RD_MASTER_INVITE_SEAL_REQUIRED": (503, "SECURITY_INTEGRITY_ALERT",
                                       "Verificação de integridade da segurança falhou. Contate o responsável "
                                       "técnico."),
    "RD_MODE_CHANGE_INVALID": (409, "APPROVAL_MODE_CHANGE_INVALID", "Mudança de modo inválida."),
    "RD_MODE_CHANGE_REUSED": (409, "APPROVAL_MODE_CHANGE_INVALID", "Pedido de mudança de modo já utilizado."),
    "RD_APPROVAL_MODE_INVALID": (409, "APPROVAL_MODE_CHANGE_INVALID", "Mudança de modo inválida."),
}


# D2: deadlock / falha de serialização / lock_timeout ⇒ 409 com nova tentativa indicada (nunca 500)
CONCURRENCY_SQLSTATES = {"40P01", "40001", "55P03"}


def sqlstate(exc: BaseException) -> Optional[str]:
    """SQLSTATE da exceção do driver (asyncpg via SQLAlchemy), se houver."""
    seen = set()
    cur: Any = getattr(exc, "orig", None) or exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for attr in ("sqlstate", "pgcode"):
            v = getattr(cur, attr, None)
            if isinstance(v, str) and v:
                return v
        cur = getattr(cur, "__cause__", None) or getattr(cur, "orig", None)
    return None


def concurrency_error(exc: BaseException) -> Optional[DomainError]:
    if sqlstate(exc) in CONCURRENCY_SQLSTATES:
        return Conflict("CONCURRENT_OPERATION", "Operação concorrente em andamento. Tente novamente em instantes.",
                        details={"retry_after_s": 1}, headers={"Retry-After": "1"})
    return None


def map_db_error(exc: BaseException) -> Optional[DomainError]:
    """Converte exceção ``RD_*`` do banco em ``DomainError`` (409/503) e deadlock/serialização/lock_timeout em
    409 ``CONCURRENT_OPERATION`` (com ``Retry-After``). ``None`` se não for do contrato."""
    conc = concurrency_error(exc)
    if conc is not None:
        return conc
    code = rd_error_code(exc)
    if code is None:
        return None
    if code in _DB_ERRORS:
        status, api_code, msg = _DB_ERRORS[code]
        if status == 503:
            return ServiceUnavailable(api_code, msg)
        return DomainError(api_code, msg, status=status)
    return Conflict(code[3:], "Operação recusada pelas regras de segurança da instância.")


# ─── Leitura ─────────────────────────────────────────────────────────────────

def _models():
    from app.models.security import AdminMasterInvite, AdminMasterSeatEvent, InstanceApprovalModeHistory

    return InstanceApprovalModeHistory, AdminMasterSeatEvent, AdminMasterInvite


def _locks():
    from app.modules.security import ddl

    return ddl.LOCK_APPROVAL_MODE, ddl.LOCK_MASTER_SEATS, ddl.LOCK_BOOTSTRAP


async def lock_mode(db: AsyncSession, *, exclusive: bool) -> None:
    lock_mode_id, _, _ = _locks()
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await db.execute(text(f"SELECT {fn}(:k)"), {"k": lock_mode_id})


async def current_state(db: AsyncSession) -> Optional[ModeState]:
    """Modo efetivo (linha de maior versão), SEM verificação de integridade. ``None`` = não inicializado."""
    H, _, _ = _models()
    row = (
        await db.execute(
            select(H.to_mode, H.mode_version, H.app_seal).order_by(H.mode_version.desc()).limit(1)
        )
    ).first()
    if row is None:
        return None
    return ModeState(mode=row[0], version=int(row[1]), head_seal=row[2])


async def history_rows(db: AsyncSession) -> list:
    H, _, _ = _models()
    return list((await db.execute(select(H).order_by(H.mode_version)
                                  .execution_options(populate_existing=True))).scalars().all())


async def effective_mode(db: AsyncSession) -> ModeState:
    """Modo efetivo com integridade verificada. 503 ``INSTANCE_NOT_INITIALIZED`` / ``SECURITY_INTEGRITY_ALERT``."""
    state = await current_state(db)
    if state is None:
        raise not_initialized()
    report = await verify_integrity(db)
    if not report.ok:
        await on_integrity_failure(report, origin="financial_operation")
        raise integrity_alert()
    return state


async def ensure_financial_authorization_allowed(db: AsyncSession, *, exclusive: bool = False) -> ModeState:
    """Prontidão para criar/aprovar/autorizar pedido financeiro (SPEC §4.1).

    Lock consultivo do modo (compartilhado; exclusivo para ``APPROVAL_MODE_CHANGE``) → integridade
    → DUAL com < 2 Masters ativos ⇒ 409 ``SECOND_MASTER_REQUIRED``.
    """
    await lock_mode(db, exclusive=exclusive)
    state = await effective_mode(db)
    if state.mode == "DUAL":
        from app.modules.security import masters

        if await masters.active_master_count(db) < 2:
            raise DomainError(
                "SECOND_MASTER_REQUIRED",
                SECOND_MASTER_REQUIRED_MSG,
                status=409,
                details={"approval_mode": "DUAL", "second_master": await masters.second_master_status(db)},
            )
    return state


# ─── Escrita (somente pelas funções definer do banco) ────────────────────────

def _norm_env(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    return v or None


async def initialize(db: AsyncSession, *, mode: str, source: str, env_value: Optional[str]) -> tuple[ModeState, bool]:
    """Inicializa o modo (versão 1) pela função definer. Idempotente: já inicializado ⇒ ``created=False``.

    Não faz commit. Audita ``security.approval_mode_initialized`` quando cria.
    """
    from app.core.audit import audit
    from app.core.authz import AuthContext
    from app.modules.security import seal

    if mode not in MODES:
        raise ValueError("modo inválido")
    if source not in MODE_SOURCES:
        raise ValueError("origem inválida")
    env_norm = _norm_env(env_value)
    await lock_mode(db, exclusive=True)
    existing = await current_state(db)
    if existing is not None:
        return existing, False
    app_seal = seal.mode_seal(
        prev_seal=seal.GENESIS, mode_version=1, from_mode=None, to_mode=mode, kind="INITIALIZED", source=source,
        approval_request_id=None, requested_by=None, authorized_by=None, reason=None, env_value=env_norm,
    )
    try:
        row = (
            await db.execute(
                text("SELECT approval_mode, mode_version, created FROM rd_security_init_approval_mode("
                     ":mode, :source, :env, :seal)"),
                {"mode": mode, "source": source, "env": env_norm, "seal": app_seal},
            )
        ).first()
    except DBAPIError as exc:
        mapped = map_db_error(exc)
        if mapped is not None:
            raise mapped from None
        raise
    created = bool(row[2])
    state = await current_state(db)
    assert state is not None
    if created:
        await audit(db, AuthContext.system(origin="system"), "security.approval_mode_initialized",
                    "instance_approval_mode", str(state.version),
                    after={"mode": state.mode, "mode_version": state.version, "source": source, "env_value": env_norm},
                    origin="system")
        log.info("approval_mode_initialized", mode=state.mode, mode_version=state.version, source=source)
    return state, created


async def apply_transition(db: AsyncSession, ctx: Any, req: Any) -> ModeState:
    """Aplica a mudança de modo do pedido ``APPROVAL_MODE_CHANGE`` aprovado/autorizado (C chama em ``_execute``).

    Chama ``rd_security_apply_mode_transition`` (que revalida tudo no banco), audita, notifica os Masters
    ativos (dedupe por versão) e publica ``security.status_changed`` após o commit. Não faz commit.
    """
    from app.core.audit import audit
    from app.modules.security import notifications as sec_notifications
    from app.modules.security import seal

    await lock_mode(db, exclusive=True)
    cur = await current_state(db)
    if cur is None:
        raise not_initialized()
    payload = dict(getattr(req, "payload", None) or {})
    target = payload.get("target_mode")
    if target not in MODES or target == cur.mode:
        raise DomainError("APPROVAL_MODE_CHANGE_INVALID", "Mudança de modo inválida.", status=400)
    if getattr(req, "approval_mode", cur.mode) != cur.mode:
        raise DomainError("APPROVAL_MODE_CHANGED", "O modo de aprovação mudou; crie uma nova solicitação.", status=409)
    kind = "SINGLE_TO_DUAL" if cur.mode == "SINGLE" else "DUAL_TO_SINGLE"
    authorized_by = getattr(req, "approved_by", None) or getattr(req, "self_authorized_by", None)
    new_version = cur.version + 1
    app_seal = seal.mode_seal(
        prev_seal=cur.head_seal, mode_version=new_version, from_mode=cur.mode, to_mode=target, kind=kind,
        source="APPROVAL_REQUEST", approval_request_id=req.id, requested_by=req.requested_by,
        authorized_by=authorized_by, reason=req.reason, env_value=None,
    )
    try:
        version = (
            await db.execute(
                text("SELECT rd_security_apply_mode_transition(:rid, :prev, :seal)"),
                {"rid": req.id, "prev": cur.head_seal, "seal": app_seal},
            )
        ).scalar_one()
    except DBAPIError as exc:
        mapped = map_db_error(exc)
        if mapped is not None:
            raise mapped from None
        raise
    state = ModeState(mode=target, version=int(version), head_seal=app_seal)
    await audit(db, ctx, "security.approval_mode_changed", "instance_approval_mode", str(state.version),
                before={"mode": cur.mode, "mode_version": cur.version},
                after={"mode": state.mode, "mode_version": state.version, "kind": kind,
                       "approval_request_id": req.id, "requested_by": req.requested_by,
                       "authorized_by": authorized_by})
    log.warning("approval_mode_changed", from_mode=cur.mode, to_mode=state.mode, mode_version=state.version,
                request_id=str(req.id))
    await sec_notifications.notify_mode_changed(db, from_mode=cur.mode, to_mode=state.mode, version=state.version)
    await sec_notifications.status_changed_for_masters(db, reason="approval_mode_changed")
    return state


# ─── Integridade (SPEC §8) ───────────────────────────────────────────────────

def _eq(a: Optional[str], b: Optional[str]) -> bool:
    from app.core.crypto import constant_time_equals

    return a is not None and b is not None and constant_time_equals(a, b)


async def verify_integrity(db: AsyncSession) -> IntegrityReport:
    """Recalcula as duas cadeias seladas e confere os titulares atuais dos assentos.

    Descrições sem segredo (nunca selo/hash). Tabelas minúsculas: barato o bastante para rodar em
    toda operação financeira.
    """
    from app.models.users import User
    from app.modules.security import seal

    H, E, _ = _models()
    problems: list[str] = []

    prev = seal.GENESIS
    rows = await history_rows(db)
    for i, r in enumerate(rows, start=1):
        if r.mode_version != i:
            problems.append(f"histórico do modo: versões não contíguas (esperada {i}, encontrada {r.mode_version})")
        if not _eq(r.prev_seal, prev):
            problems.append(f"histórico do modo: encadeamento quebrado na versão {r.mode_version}")
        expected = seal.mode_seal(
            prev_seal=r.prev_seal, mode_version=r.mode_version, from_mode=r.from_mode, to_mode=r.to_mode,
            kind=r.kind, source=r.source, approval_request_id=r.approval_request_id, requested_by=r.requested_by,
            authorized_by=r.authorized_by, reason=r.reason, env_value=r.env_value,
        )
        if not _eq(r.app_seal, expected):
            problems.append(f"histórico do modo: selo inválido na versão {r.mode_version}")
        if i > 1 and rows[i - 2].to_mode != r.from_mode:
            problems.append(f"histórico do modo: transição incoerente na versão {r.mode_version}")
        prev = r.app_seal

    events = list((await db.execute(select(E).order_by(E.id).execution_options(populate_existing=True))
                   ).scalars().all())
    if events and not rows:
        problems.append("eventos de assento existem sem histórico de modo")
    prev = seal.GENESIS
    last_assigned: dict[int, uuid.UUID] = {}
    for ev in events:
        if not _eq(ev.prev_seal, prev):
            problems.append(f"eventos de assento: encadeamento quebrado no evento {ev.id}")
        expected = seal.seat_seal(
            prev_seal=ev.prev_seal, seat=ev.seat, user_id=ev.user_id, event=ev.event, source=ev.source,
            invite_id=ev.invite_id, actor_user_id=ev.actor_user_id,
        )
        if not _eq(ev.app_seal, expected):
            problems.append(f"eventos de assento: selo inválido no evento {ev.id}")
        if ev.event == "ASSIGNED":
            if ev.seat in last_assigned:
                problems.append(f"assento {ev.seat}: atribuído mais de uma vez")
            last_assigned[ev.seat] = ev.user_id
        elif ev.event == "RECOVERED":
            if last_assigned.get(ev.seat) != ev.user_id:
                problems.append(f"assento {ev.seat}: recuperação de quem não é o titular (evento {ev.id})")
        prev = ev.app_seal

    holder_rows = list((await db.execute(select(User).where(User.admin_master_seat.is_not(None))
                                         .execution_options(populate_existing=True))).scalars().all())
    holders = {int(u.admin_master_seat): u.id for u in holder_rows}
    # (4) D1: selo de CREDENCIAL de cada titular (NULL = adoção pendente ⇒ também é problema)
    from app.modules.security.credentials import credential_ok, invite_ok

    for u in holder_rows:
        if u.master_credential_seal is None:
            problems.append(f"assento {u.admin_master_seat}: selo de credencial ausente (adoção pendente: "
                            "execute o serviço bootstrap)")
        elif not credential_ok(u):
            problems.append(f"assento {u.admin_master_seat}: selo de credencial do titular inválido")
    # (5) D1: selo de cada convite PENDENTE
    _, _, Inv = _models()
    for inv in (await db.execute(select(Inv).where(Inv.status == "PENDING")
                                 .execution_options(populate_existing=True))).scalars().all():
        if not invite_ok(inv):
            problems.append(f"convite pendente do assento {inv.seat}: selo inválido")
    for s in (1, 2):
        holder, assigned = holders.get(s), last_assigned.get(s)
        if holder is not None and assigned is None:
            problems.append(f"assento {s}: titular sem evento selado")
        elif holder is None and assigned is not None:
            problems.append(f"assento {s}: evento selado sem titular")
        elif holder is not None and holder != assigned:
            problems.append(f"assento {s}: titular diverge do último evento selado")
    return IntegrityReport(ok=not problems, problems=problems)


async def on_integrity_failure(report: IntegrityReport, *, origin: str) -> None:
    """Falha de integridade: log CRITICAL + auditoria e notificação em transação independente (deduplicadas).

    Best-effort: nunca mascara o 503 do chamador.
    """
    log.critical("security_integrity_failed", origin=origin, problems=len(report.problems))
    try:
        from app.modules.security import notifications as sec_notifications

        await sec_notifications.record_integrity_failure(report, origin=origin)
    except Exception as exc:  # pragma: no cover - registro best-effort
        log.error("security_integrity_record_failed", error_type=type(exc).__name__)


__all__ = [
    "ModeState",
    "IntegrityReport",
    "current_state",
    "effective_mode",
    "ensure_financial_authorization_allowed",
    "initialize",
    "apply_transition",
    "verify_integrity",
    "map_db_error",
    "rd_error_code",
]
