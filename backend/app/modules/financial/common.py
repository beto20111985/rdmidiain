"""
RD Mídia Indoor — Utilidades comuns do domínio financeiro (W-FIN).

- Efeitos pós-commit: eventos de tempo real e despacho de tasks Celery só depois do
  COMMIT da transação (nunca publicar algo que pode sofrer rollback).
- Despachante de tasks substituível (testes chamam o executor diretamente).
- Tipos de notificação financeiros (registrados no catálogo do W-BIZ).
- Configurações financeiras adicionais (registry do W1; alteração = pedido de autorização financeira
  no modo da instância — dupla aprovação ou aprovação única).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any, Callable, Optional

import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.money import format_brl
from app.core.settings_service import SettingDef, register

log = structlog.get_logger("rd.financial")

# ─── Configurações do domínio (registry W1; financial=True ⇒ autorização financeira) ─
for _d in [
    SettingDef("commission.auto_payout_enabled", True, "bool",
               "Pagamento automático de comissões no dia 1 (saldo disponível ≥ mínimo).",
               financial=True, category="financeiro"),
    SettingDef("commission.auto_payout_min_cents", 100, "int",
               "Mínimo (centavos) para pagamento automático; abaixo acumula sem expiração.",
               financial=True, min=1, max=10_000_000, category="financeiro"),
    SettingDef("finance.approval_expiry_hours", 24, "int",
               "Validade (horas) de um pedido de dupla aprovação.", financial=True, min=1, max=168,
               category="financeiro"),
    SettingDef("finance.password_reset_cooldown_hours", 24, "int",
               "Bloqueio de saques/aprovações após redefinir a senha financeira esquecida (horas).",
               financial=True, min=0, max=168, category="financeiro"),
]:
    register(_d)


# ─── Pós-commit ──────────────────────────────────────────────────────────────

_PENDING = "rd_fin_after_commit"
_HOOKED = "rd_fin_hooked"
_INFLIGHT: set = set()


def _run_pending(session) -> None:  # noqa: ANN001
    items = session.info.pop(_PENDING, None) or []
    for kind, payload in items:
        try:
            if kind == "task":
                name, args = payload
                _dispatcher(name, args)
            elif kind == "event":
                typ, data, scope = payload
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    from app.core import events

                    events.publish_sync(typ, data, **scope)
                    continue
                from app.core import events

                t = loop.create_task(events.publish(typ, data, **scope))
                _INFLIGHT.add(t)
                t.add_done_callback(_INFLIGHT.discard)
        except Exception as exc:  # tempo real/despacho são recuperáveis (sweepers)
            log.warning("financial_after_commit_failed", kind=kind, error_type=type(exc).__name__)


def _drop_pending(session) -> None:  # noqa: ANN001
    session.info.pop(_PENDING, None)


def _queue(db: AsyncSession, kind: str, payload: Any) -> None:
    sync = db.sync_session
    sync.info.setdefault(_PENDING, []).append((kind, payload))
    if not sync.info.get(_HOOKED):
        event.listen(sync, "after_commit", _run_pending)
        event.listen(sync, "after_rollback", _drop_pending)
        sync.info[_HOOKED] = True


def publish_after_commit(db: AsyncSession, type: str, data: dict, **scope: Any) -> None:
    _queue(db, "event", (type, data, scope))


def dispatch_after_commit(db: AsyncSession, task_name: str, *args: Any) -> None:
    _queue(db, "task", (task_name, [str(a) for a in args]))


def _default_dispatcher(task_name: str, args: list) -> None:
    from app.workers.celery_app import celery_app

    celery_app.send_task(task_name, args=args, queue="financial")


_dispatcher: Callable[[str, list], None] = _default_dispatcher


def set_task_dispatcher(fn: Optional[Callable[[str, list], None]]) -> None:
    """Testes: captura o despacho em vez de enviar ao broker."""
    global _dispatcher
    _dispatcher = fn or _default_dispatcher


async def drain() -> None:
    pending = [t for t in list(_INFLIGHT) if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=10)


# ─── Hash canônico ───────────────────────────────────────────────────────────

def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def as_uuid(v: Any) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def brl(cents: int) -> str:
    return format_brl(int(cents))


# ─── Notificações (catálogo do W-BIZ) ────────────────────────────────────────

# Saldo negativo do estabelecimento (estorno de comissão já sacada) — tipo do W-FIN registrado pelo
# mecanismo de extensão do catálogo (``register_event_type``); o catálogo base não tem equivalente.
EVENT_BALANCE_NEGATIVE = "finance.balance_negative"


def _register_event_types() -> None:
    try:
        from app.modules.notifications.catalog import EMAIL, IN_APP, EventType, register_event_type
    except ImportError:  # pragma: no cover - módulo de notificações ausente
        return
    for et in [
        EventType("finance.approval_requested", "Aprovação financeira pendente (sócios)",
                  "Aprovação financeira pendente", "{{requester}} solicitou {{action}} de {{amount}}. Motivo: {{reason}}",
                  ("requester", "action", "amount", "reason", "request_id"), default_channels=(IN_APP, EMAIL),
                  critical=True, audience="staff", link="/financeiro/aprovacoes/{{request_id}}"),
        EventType("finance.approval_decided", "Aprovação financeira decidida", "Pedido financeiro {{decision}}",
                  "O pedido {{action}} de {{amount}} foi {{decision}}.", ("action", "amount", "decision", "request_id"),
                  default_channels=(IN_APP, EMAIL), critical=True, audience="staff",
                  link="/financeiro/aprovacoes/{{request_id}}"),
        EventType("finance.execution_alert", "Alerta de execução financeira", "Atenção: {{title}}",
                  "{{message}}", ("title", "message", "ref"), default_channels=(IN_APP, EMAIL), critical=True,
                  audience="staff", link="/financeiro"),
        EventType("finance.reconciliation_divergent", "Divergência de conciliação", "Divergência de conciliação",
                  "{{message}}", ("message", "item_id"), default_channels=(IN_APP, EMAIL), critical=True,
                  audience="staff", link="/financeiro/conciliacao"),
        EventType("finance.password_reset", "Senha financeira redefinida", "Sua senha financeira foi redefinida",
                  "A senha financeira foi redefinida em {{when}}. Saques e aprovações ficam bloqueados até "
                  "{{blocked_until}}. Se não foi você, contate imediatamente a RD.", ("when", "blocked_until"),
                  default_channels=(IN_APP, EMAIL), critical=True, audience="any", link="/financeiro/seguranca"),
        EventType(EVENT_BALANCE_NEGATIVE, "Saldo negativo (saques bloqueados)", "Saldo negativo: saques bloqueados",
                  "Um estorno de comissão já paga deixou seu saldo negativo em {{amount}}. Novos saques ficam "
                  "bloqueados até a compensação com comissões futuras.", ("amount",),
                  default_channels=(IN_APP, EMAIL), critical=True, audience="client", link="/financeiro"),
    ]:
        try:
            register_event_type(et)
        except ValueError:
            pass


_register_event_types()


async def notify(db: AsyncSession, recipients: Any, event_type: str, data: dict, *, dedupe_key: Optional[str] = None,
                 org_id: Any = None, entity: Any = None) -> Any:
    """Notificação via W-BIZ na MESMA transação (nada é enviado se houver rollback)."""
    from app.modules.notifications.service import notify as biz_notify

    return await biz_notify(db, recipients, event_type, data, dedupe_key=dedupe_key, cooldown_s=0,
                            org_id=org_id, entity=entity)


async def admin_master_ids(db: AsyncSession) -> list[uuid.UUID]:
    """Masters ATIVOS (mesmo predicado de ``rd_is_active_master``: assento + TOTP + senha financeira +
    setup concluído + ``super_admin`` GLOBAL) — destinatários das notificações financeiras."""
    from app.modules.security import masters

    return await masters.active_master_ids(db)


def require_admin_master(ctx: Any) -> None:
    if not getattr(ctx, "is_admin_master", False):
        raise DomainError("ADMIN_MASTER_REQUIRED", "Somente os Admin Master podem executar esta ação.", status=403)
    if getattr(ctx, "impersonation", None) is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
