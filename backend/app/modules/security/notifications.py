"""
RD Mídia Indoor — Notificações, auditoria deduplicada e evento de tempo real da segurança da
instância (SPEC §7).

- Tipos de notificação (todos ``critical=True``, ``audience="staff"``): ``security.second_master_activated``,
  ``security.master_activation_same_device``, ``security.approval_mode_changed``,
  ``security.master_recovered``, ``security.integrity_alert``, ``security.env_divergence`` (só IN_APP).
- Evento WS ``security.status_changed`` (entregue ao ``user_id``): publicado APÓS o commit para cada
  titular de assento em transição de modo, convite criado/revogado/expirado, ativação e recuperação.
- Nunca inclui token, senha, hash, segredo TOTP ou código — só nomes, modos, versões e e-mails mascarados.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutils import utcnow

log = structlog.get_logger("rd.security.notifications")

STATUS_CHANGED = "security.status_changed"
MODE_LABEL = {"SINGLE": "aprovação única", "DUAL": "dupla aprovação"}
_SECURITY_LINK = "/configuracoes/seguranca"


def _register() -> None:
    from app.core import events

    try:
        events.register_event_type(STATUS_CHANGED, None)
    except ValueError:  # pragma: no cover - já registrado com a mesma permissão
        pass
    try:
        from app.modules.notifications.catalog import EMAIL, IN_APP, EventType, register_event_type
    except ImportError:  # pragma: no cover
        return
    for et in [
        EventType("security.second_master_activated", "Segundo Administrador Master ativado",
                  "Segundo Administrador Master ativado",
                  "{{name}} concluiu a ativação do assento {{seat}} de Administrador Master. A dupla aprovação "
                  "financeira está liberada.", ("name", "seat"), default_channels=(IN_APP, EMAIL), critical=True,
                  audience="staff", link=_SECURITY_LINK),
        EventType("security.master_activation_same_device", "Ativação de Master pelo mesmo dispositivo",
                  "Alerta: ativação do Master pelo mesmo dispositivo do convidante",
                  "A ativação do assento {{seat}} ({{name}}) partiu do mesmo endereço IP e navegador da sessão mais "
                  "recente de quem enviou o convite. Confirme que são pessoas distintas.", ("name", "seat"),
                  default_channels=(IN_APP, EMAIL), critical=True, audience="staff", link=_SECURITY_LINK),
        EventType("security.approval_mode_changed", "Modo de aprovação financeira alterado",
                  "Modo de aprovação financeira alterado",
                  "O modo de aprovação financeira mudou de {{from_mode}} para {{to_mode}} (versão {{version}}).",
                  ("from_mode", "to_mode", "version"), default_channels=(IN_APP, EMAIL), critical=True,
                  audience="staff", link=_SECURITY_LINK),
        EventType("security.master_recovered", "Credenciais de Master recuperadas",
                  "Credenciais do Administrador Master (assento {{seat}}) recuperadas",
                  "As credenciais do assento {{seat}} foram recuperadas pela CLI em {{when}}. Motivo: {{reason}}. "
                  "TOTP e senha financeira foram zerados; operações financeiras desse Master ficam bloqueadas "
                  "temporariamente. Se não foi combinado, contate imediatamente o responsável técnico.",
                  ("seat", "when", "reason"), default_channels=(IN_APP, EMAIL), critical=True, audience="staff",
                  link=_SECURITY_LINK),
        EventType("security.master_seat_assigned", "Assento de Master atribuído pela CLI",
                  "Assento {{seat}} de Administrador Master atribuído pela CLI",
                  "O assento {{seat}} foi atribuído a {{name}} pela CLI (automação/recuperação de instalação). "
                  "O novo titular precisa trocar a senha e configurar TOTP e senha financeira no primeiro acesso.",
                  ("seat", "name"), default_channels=(IN_APP, EMAIL), critical=True, audience="staff",
                  link=_SECURITY_LINK),
        EventType("security.integrity_alert", "Alerta de integridade da segurança",
                  "ALERTA: integridade da segurança da instância",
                  "A verificação de integridade (modo de aprovação e assentos de Master) falhou: {{summary}}. "
                  "Operações financeiras estão suspensas até a correção.", ("summary",),
                  default_channels=(IN_APP, EMAIL), critical=True, audience="staff", link=_SECURITY_LINK),
        EventType("security.env_divergence", "Variável de ambiente divergente do modo efetivo",
                  "Configuração divergente do modo de aprovação",
                  "A variável RD_BOOTSTRAP_DUAL_MASTER_APPROVAL={{env_value}} diverge do modo efetivo "
                  "({{mode}}, versão {{version}}). A variável só vale na primeira inicialização e foi ignorada; "
                  "o modo só muda por solicitação aprovada.", ("env_value", "mode", "version"),
                  default_channels=(IN_APP,), critical=True, audience="staff", link=_SECURITY_LINK),
    ]:
        try:
            register_event_type(et)
        except ValueError:  # pragma: no cover
            pass


_register()


async def _notify(db: AsyncSession, recipients: Iterable[uuid.UUID], event_type: str, data: dict, *,
                  dedupe_key: Optional[str] = None) -> None:
    from app.modules.notifications.service import notify

    ids = list(dict.fromkeys(recipients))
    if not ids:
        return
    await notify(db, ids, event_type, data, dedupe_key=dedupe_key, cooldown_s=0)


async def status_changed_for_masters(db: AsyncSession, *, reason: str, extra_user_ids: Iterable[Any] = ()) -> None:
    """Agenda ``security.status_changed`` (após o commit) para cada titular de assento (+ extras)."""
    from app.modules.notifications.service import publish_event_after_commit
    from app.modules.security.masters import seat_holder_ids

    ids = list(dict.fromkeys([*await seat_holder_ids(db), *extra_user_ids]))
    for uid in ids:
        publish_event_after_commit(db, STATUS_CHANGED, {"reason": reason}, user_id=uid)


async def notify_mode_changed(db: AsyncSession, *, from_mode: str, to_mode: str, version: int) -> None:
    from app.modules.security.masters import active_master_ids, seat_holder_ids

    recipients = await active_master_ids(db) or await seat_holder_ids(db)
    await _notify(db, recipients, "security.approval_mode_changed",
                  {"from_mode": MODE_LABEL.get(from_mode, from_mode), "to_mode": MODE_LABEL.get(to_mode, to_mode),
                   "version": version}, dedupe_key=f"security.approval_mode_changed:v{version}")


async def notify_second_master_activated(db: AsyncSession, *, name: str, seat: int, invite_id: Any) -> None:
    from app.modules.security.masters import active_master_ids

    await _notify(db, await active_master_ids(db), "security.second_master_activated", {"name": name, "seat": seat},
                  dedupe_key=f"security.second_master_activated:{invite_id}")


async def notify_same_device(db: AsyncSession, *, name: str, seat: int, invite_id: Any) -> None:
    from app.modules.security.masters import active_master_ids

    await _notify(db, await active_master_ids(db), "security.master_activation_same_device",
                  {"name": name, "seat": seat}, dedupe_key=f"security.master_activation_same_device:{invite_id}")


async def notify_seat_assigned_cli(db: AsyncSession, *, seat: int, name: str, user_id: Any) -> None:
    from app.modules.security.masters import seat_holder_ids

    await _notify(db, await seat_holder_ids(db), "security.master_seat_assigned", {"seat": seat, "name": name},
                  dedupe_key=f"security.master_seat_assigned:{seat}:{user_id}")


async def notify_master_recovered(db: AsyncSession, *, seat: int, reason: str) -> None:
    from app.modules.security.masters import seat_holder_ids

    now = utcnow()
    await _notify(db, await seat_holder_ids(db), "security.master_recovered",
                  {"seat": seat, "when": now, "reason": reason[:300]},
                  dedupe_key=f"security.master_recovered:{seat}:{now.isoformat()}")


async def notify_env_divergence(db: AsyncSession, *, env_value: str, mode: str, version: int) -> None:
    from app.modules.security.masters import seat_holder_ids

    await _notify(db, await seat_holder_ids(db), "security.env_divergence",
                  {"env_value": env_value, "mode": MODE_LABEL.get(mode, mode), "version": version},
                  dedupe_key=f"security.env_divergence:v{version}:{env_value}")


async def _redis_once(key_parts: tuple, ttl_s: int) -> bool:
    """True na primeira vez da janela (SET NX). Redis fora ⇒ True (registrar é melhor que perder)."""
    try:
        from app.core.redis import get_redis, rkey

        r = await get_redis()
        return bool(await r.set(rkey("security", *key_parts), "1", nx=True, ex=ttl_s))
    except Exception:
        return True


async def record_integrity_failure(report: Any, *, origin: str, db: Optional[AsyncSession] = None) -> None:
    """Auditoria ``security.integrity_check_failed`` (1×/hora) + notificação ``security.integrity_alert``
    aos titulares (dedupe diário). Sem ``db``: transação independente (a request que falhou será desfeita)."""
    from app.core.database import independent_session

    now = utcnow()
    if not await _redis_once(("integrity_audit", now.strftime("%Y%m%d%H")), 3600):
        return
    if db is not None:
        await _write_integrity_failure(db, report, origin=origin)
        return
    async with independent_session() as s:
        await _write_integrity_failure(s, report, origin=origin)


async def _write_integrity_failure(db: AsyncSession, report: Any, *, origin: str) -> None:
    from app.core.audit import audit
    from app.core.authz import AuthContext
    from app.modules.security.masters import seat_holder_ids

    now = utcnow()
    summary = "; ".join(report.problems[:3]) + (" …" if len(report.problems) > 3 else "")
    await audit(db, AuthContext.system(origin="system"), "security.integrity_check_failed", "instance_security",
                None, after={"problems": list(report.problems)[:20], "origin": origin}, origin="system")
    await _notify(db, await seat_holder_ids(db), "security.integrity_alert", {"summary": summary[:500]},
                  dedupe_key=f"security.integrity_alert:{now.strftime('%Y-%m-%d')}")


async def record_seal_violation(kind: str, *, origin: str, user_id: Any = None, seat: Any = None,
                                invite_id: Any = None) -> None:
    """D1: selo de credencial/convite inválido ⇒ auditoria (1×/hora por alvo), notificação crítica
    ``security.integrity_alert`` (dedupe diário) e ``rd_security_integrity_ok=0``. Transação independente;
    best-effort (a resposta genérica de falha já foi dada)."""
    from app.core.audit import audit
    from app.core.authz import AuthContext
    from app.core.database import independent_session
    from app.modules.security.masters import seat_holder_ids

    try:
        from app.modules.security.metrics import mark_integrity_failed

        await mark_integrity_failed()
    except Exception as exc:  # pragma: no cover
        log.warning("security_metric_mark_failed", error_type=type(exc).__name__)
    target = user_id if kind == "credential" else invite_id
    now = utcnow()
    if not await _redis_once(("seal_violation", kind, str(target), now.strftime("%Y%m%d%H")), 3600):
        return
    action = "security.master_credential_seal_invalid" if kind == "credential" else "security.master_invite_seal_invalid"
    entity = ("user", user_id) if kind == "credential" else ("admin_master_invite", invite_id)
    what = (f"selo de credencial inválido no titular do assento {seat}" if kind == "credential"
            else f"selo inválido no convite de Administrador Master (assento {seat})")
    try:
        async with independent_session(lock_timeout_ms=10000) as s:
            await audit(s, AuthContext.system(origin="system"), action, entity[0], entity[1],
                        after={"seat": seat, "origin": origin}, origin="system")
            await _notify(s, await seat_holder_ids(s), "security.integrity_alert", {"summary": what},
                          dedupe_key=f"security.integrity_alert:{now.strftime('%Y-%m-%d')}")
    except Exception as exc:
        log.error("security_seal_violation_record_failed", kind=kind, error_type=type(exc).__name__)
