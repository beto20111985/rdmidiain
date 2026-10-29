"""
RD Mídia Indoor — Selo de CREDENCIAL do titular de assento e selo do CONVITE (AÇÃO 003 — D1 do revisor I).

Ameaça: quem tem o papel de runtime (``rd_app``) mas NÃO a chave da aplicação conseguia
(a) inserir um convite com ``token_hash`` escolhido e ativar um Master-fantoche pela API pública, ou
(b) trocar ``hashed_password``/``must_change_password`` de um titular e fazer o primeiro acesso com fatores
próprios. Defesa: HMAC (chave derivada da aplicação, ``app.modules.security.seal``) sobre os campos que
definem a credencial/convite; sem a chave, nenhuma escrita direta produz selo válido.

- **Re-selagem automática** (``before_flush`` na sessão síncrona, registrado para TODA ``Session``): titular de
  assento novo ou com campo selado alterado pelo ORM recebe novo ``master_credential_seal`` no MESMO UPDATE.
  Anti-lavagem: só re-sela a partir de um estado cujo selo ARMAZENADO confere (lido do banco na própria
  transação); selo inválido ⇒ ``flush`` abortado (401 genérico) + alerta. Escritas fora do ORM (funções
  definer de assento/recuperação) são re-seladas explicitamente por ``reseal_after_definer``.
- **Conferência (falha FECHADA)**: ``credential_ok``/``ensure_master_credential`` (login antes da senha, mfa/verify,
  refresh, toda request autenticada em ``authz._resolve``, step-up/senha financeira, reset de senha, ativação);
  ``invite_ok`` (inspect/totp/complete/reenvio). Inválido ⇒ resposta genérica + alerta
  (auditoria ``security.master_credential_seal_invalid``/``security.master_invite_seal_invalid`` 1×/hora,
  notificação ``security.integrity_alert`` deduplicada, métrica ``rd_security_integrity_ok=0``).
- **Adoção TOFU** (uma vez): ``adopt_null_credential_seals`` no ``bootstrap-instance`` (titulares com selo NULL —
  instâncias anteriores à 0009). Janela entre a migration e o bootstrap = risco residual documentado.

Nunca registra valores de campos (hash/segredo) — só ids, assento e origem.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Mapping, Optional

import structlog
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.errors import DomainError, Unauthorized

log = structlog.get_logger("rd.security.credentials")

SEALED_FIELDS = (
    "admin_master_seat", "email", "hashed_password", "must_change_password", "totp_enabled", "totp_secret_enc",
    "financial_password_hash", "is_active",
)


class MasterCredentialSealError(Unauthorized):
    """Selo de credencial do titular inválido/ausente (resposta genérica; o motivo vai só para o alerta)."""

    def __init__(self) -> None:
        super().__init__("INVALID_TOKEN", "Token inválido.")


def generic_unauthorized() -> DomainError:
    return Unauthorized("INVALID_TOKEN", "Token inválido.")


# ─── Cálculo e conferência ───────────────────────────────────────────────────

def compute_credential(values: Mapping[str, Any], user_id: Any) -> str:
    from app.modules.security import seal

    return seal.credential_seal(
        user_id=user_id, admin_master_seat=values.get("admin_master_seat"), email=values.get("email") or "",
        hashed_password=values.get("hashed_password"), must_change_password=values.get("must_change_password"),
        totp_enabled=values.get("totp_enabled"), totp_secret_enc=values.get("totp_secret_enc"),
        financial_password_hash=values.get("financial_password_hash"), is_active=values.get("is_active"),
    )


def compute_user(user: Any) -> str:
    return compute_credential({f: getattr(user, f, None) for f in SEALED_FIELDS}, user.id)


def _eq(a: Optional[str], b: Optional[str]) -> bool:
    from app.core.crypto import constant_time_equals

    return a is not None and b is not None and constant_time_equals(a, b)


def credential_ok(user: Any) -> bool:
    """Titular de assento: selo armazenado confere com os campos atuais. Não titular: sempre True."""
    if user is None or getattr(user, "admin_master_seat", None) is None:
        return True
    return _eq(getattr(user, "master_credential_seal", None), compute_user(user))


def compute_invite(inv: Any) -> str:
    from app.modules.security import seal

    return seal.invite_seal(invite_id=inv.id, seat=int(inv.seat), email=inv.email, invited_by=inv.invited_by,
                            token_hash=inv.token_hash, expires_at=inv.expires_at)


def invite_ok(inv: Any) -> bool:
    return inv is not None and _eq(getattr(inv, "seal", None), compute_invite(inv))


# ─── Alertas (tarefa em segundo plano: espera a transação da request liberar os locks) ───────

def schedule_violation_alert(kind: str, *, origin: str, user_id: Any = None, seat: Any = None,
                             invite_id: Any = None) -> None:
    """Agenda o alerta (auditoria + notificação + métrica) numa tarefa do conjunto drenado por
    ``notifications.service.drain()``. Nunca levanta."""
    log.critical("security_seal_invalid", kind=kind, origin=origin, user_id=str(user_id) if user_id else None,
                 seat=seat, invite_id=str(invite_id) if invite_id else None)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        from app.modules.notifications.service import _INFLIGHT

        from app.modules.security.notifications import record_seal_violation

        async def _run() -> None:
            before = set(_INFLIGHT)
            await record_seal_violation(kind, origin=origin, user_id=user_id, seat=seat, invite_id=invite_id)
            # os envios da notificação são agendados após o commit (novas tarefas): espera-os aqui para que
            # ``drain()`` (que aguarda ESTA tarefa) não deixe trabalho órfão
            me = asyncio.current_task()
            spawned = [t for t in list(_INFLIGHT) if t not in before and t is not me and not t.done()]
            if spawned:
                await asyncio.wait(spawned, timeout=30)

        task = loop.create_task(_run())
        _INFLIGHT.add(task)
        task.add_done_callback(_INFLIGHT.discard)
    except Exception as exc:  # pragma: no cover - alerta best-effort (o log CRITICAL já saiu)
        log.error("security_seal_alert_schedule_failed", error_type=type(exc).__name__)


async def ensure_master_credential(user: Any, *, origin: str, error: Optional[DomainError] = None) -> None:
    """Falha FECHADA: titular com selo de credencial inválido/ausente ⇒ alerta + erro genérico (401)."""
    if credential_ok(user):
        return
    schedule_violation_alert("credential", origin=origin, user_id=getattr(user, "id", None),
                             seat=getattr(user, "admin_master_seat", None))
    raise error or MasterCredentialSealError()


def check_invite(inv: Any, *, origin: str) -> bool:
    """True se o selo do convite confere; senão agenda o alerta e devolve False."""
    if invite_ok(inv):
        return True
    schedule_violation_alert("invite", origin=origin, invite_id=getattr(inv, "id", None),
                             seat=getattr(inv, "seat", None))
    return False


# ─── Re-selagem ──────────────────────────────────────────────────────────────

def _db_row(session: Session, user_id: Any) -> Optional[Mapping[str, Any]]:
    from app.models.users import User

    cols = [getattr(User, f) for f in SEALED_FIELDS] + [User.master_credential_seal]
    with session.no_autoflush:
        row = session.connection().execute(select(*cols).where(User.id == user_id)).first()
    return None if row is None else dict(row._mapping)


def _before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    from sqlalchemy import inspect as sa_inspect

    from app.models.users import User

    for obj in list(session.new):
        if isinstance(obj, User) and obj.admin_master_seat is not None:
            if obj.id is None:  # o default Python do id só seria aplicado no INSERT (depois deste evento)
                obj.id = uuid.uuid4()
            obj.master_credential_seal = compute_user(obj)
    for obj in list(session.dirty):
        if not isinstance(obj, User):
            continue
        st = sa_inspect(obj)
        if st.attrs.master_credential_seal.history.has_changes():
            continue  # selagem explícita feita pela aplicação (reseal_after_definer / adoção)
        if not any(st.attrs[f].history.has_changes() for f in SEALED_FIELDS):
            continue
        old = _db_row(session, obj.id)
        if old is None:
            continue
        if old.get("admin_master_seat") is None:
            # não era titular (a transição para titular só ocorre pelas funções definer + reseal explícito;
            # superusuário de teste: o gatilho do banco é o portão)
            if obj.admin_master_seat is not None:
                obj.master_credential_seal = compute_user(obj)
            continue
        if not _eq(old.get("master_credential_seal"), compute_credential(old, obj.id)):
            # anti-lavagem: nunca re-selar um estado adulterado
            schedule_violation_alert("credential", origin="flush", user_id=obj.id, seat=old.get("admin_master_seat"))
            raise MasterCredentialSealError()
        obj.master_credential_seal = compute_user(obj)


def _bulk_keys(state: Any) -> set[str]:
    keys: set[str] = set()
    stmt = state.statement
    for attr in ("_values", "_ordered_values"):
        vals = getattr(stmt, attr, None)
        if vals:
            items = vals.items() if hasattr(vals, "items") else vals
            for k, _v in items:
                keys.add(str(getattr(k, "key", k)))
    params = state.parameters
    if isinstance(params, dict):
        keys |= {str(k) for k in params}
    elif isinstance(params, (list, tuple)):
        for p in params:
            if isinstance(p, dict):
                keys |= {str(k) for k in p}
    return keys


def _do_orm_execute(state: Any) -> None:
    """UPDATE em massa pelo ORM (``update(User)``) não passa pelo ``before_flush``: se tocar campo selado, a
    aplicação o RESTRINGE a não titulares (credencial de titular só muda por objeto ORM re-selado ou por função
    definer + ``reseal_after_definer``). SQL textual em ``users``: o banco (0009) exige selo novo e recusa."""
    if not getattr(state, "is_update", False):
        return
    from app.models.users import User

    if getattr(getattr(state.statement, "table", None), "name", None) != "users":
        return
    if not (_bulk_keys(state) & set(SEALED_FIELDS)):
        return
    if isinstance(state.parameters, (list, tuple)) and state.parameters:
        log.critical("bulk_update_of_sealed_master_fields_refused")
        raise MasterCredentialSealError()
    log.warning("bulk_update_sealed_fields_restricted_to_non_masters")
    state.statement = state.statement.where(User.admin_master_seat.is_(None))


_REGISTERED = False


def register_listeners() -> None:
    """Idempotente: registra ``before_flush`` e ``do_orm_execute`` em TODA ``Session`` (API, workers, CLI, testes)."""
    global _REGISTERED
    if _REGISTERED:
        return
    if not event.contains(Session, "before_flush", _before_flush):
        event.listen(Session, "before_flush", _before_flush)
    if not event.contains(Session, "do_orm_execute", _do_orm_execute):
        event.listen(Session, "do_orm_execute", _do_orm_execute)
    _REGISTERED = True


async def reseal_after_definer(db: Any, user_id: uuid.UUID, *, require_valid_or_null: bool = False,
                               origin: str = "definer") -> bool:
    """Re-sela o titular logo depois de uma função definer (assento/recuperação) — UPDATE só do selo.

    ``require_valid_or_null``: adoção de assento legado — um selo já existente precisa conferir (senão
    alerta + 503; nunca lava adulteração). Devolve True se o selo era NULL antes (adoção TOFU).
    """
    from app.models.users import User

    user = (await db.execute(select(User).where(User.id == user_id).execution_options(populate_existing=True))
            ).scalar_one()
    if user.admin_master_seat is None:
        return False
    was_null = user.master_credential_seal is None
    if require_valid_or_null and not was_null and not credential_ok(user):
        schedule_violation_alert("credential", origin=origin, user_id=user.id, seat=user.admin_master_seat)
        from app.modules.security.approval_mode import integrity_alert

        raise integrity_alert()
    new = compute_user(user)
    if user.master_credential_seal != new:
        user.master_credential_seal = new
        await db.flush()
    return was_null


async def adopt_null_credential_seals(db: Any, ctx: Any) -> list[int]:
    """TOFU (uma vez): sela titulares com ``master_credential_seal`` NULL; auditoria por titular."""
    from app.core.audit import audit
    from app.models.users import User

    rows = (await db.execute(select(User).where(User.admin_master_seat.is_not(None),
                                                User.master_credential_seal.is_(None)))).scalars().all()
    adopted: list[int] = []
    for u in rows:
        u.master_credential_seal = compute_user(u)
        adopted.append(int(u.admin_master_seat))
        await audit(db, ctx, "security.master_credential_seal_adopted", "user", u.id,
                    after={"seat": int(u.admin_master_seat), "mode": "TOFU"}, origin="system")
        log.warning("master_credential_seal_adopted", seat=int(u.admin_master_seat), user_id=str(u.id))
    if rows:
        await db.flush()
    return adopted


register_listeners()
