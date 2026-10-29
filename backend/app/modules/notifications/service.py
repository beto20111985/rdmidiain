"""
RD Mídia Indoor — Serviço de notificações (interface entre domínios, plano §4).

    from app.modules.notifications.service import notify, OrgRecipients, StaffRecipients

    await notify(
        db,
        [OrgRecipients(org_id, "campaigns.view")],      # ou user_id / (org_id, "perm") / ("GLOBAL", "perm")
        "campaign.approved",
        {"campaign_name": c.name, "campaign_id": str(c.id)},
        dedupe_key=f"campaign.approved:{c.id}",           # idempotência (por usuário)
        cooldown_s=None,                                  # None = padrão do tipo; 0 = sem cooldown
        channels=None,                                    # None = padrão do tipo (IN_APP/EMAIL/PUSH)
        entity=("campaign", c.id),                        # chave do cooldown (usuário, tipo, entidade)
        org_id=c.org_id,
    )

Regras
- Executa na TRANSAÇÃO do chamador (``db``): se ela falhar, nada é criado e nada é
  enviado. Envios externos (e-mail/push) e o evento tempo real
  ``notification.created`` acontecem SOMENTE após o commit (hook ``after_commit``),
  em tarefa assíncrona no mesmo processo; o que não for concluído (processo caiu,
  worker encerrou o loop) é reenviado pelo job ``retry_pending_deliveries`` (retry
  controlado com backoff — não é polling de UI).
- Dedupe: ``UNIQUE(user_id, dedupe_key)`` + ``ON CONFLICT DO NOTHING``.
- Cooldown: ``notification_cooldowns`` com upsert condicional atômico — duas
  transações concorrentes nunca notificam o mesmo (usuário, tipo, entidade) dentro
  da janela.
- Preferências do usuário por (tipo, canal); IN_APP de tipos críticos não desliga.
- Templates administráveis por (tipo, canal); fallback para o texto do catálogo.
- Push: interface ``PushProvider``; sem provedor → entrega ``UNAVAILABLE``
  ("push: indisponível (dependência externa)"). Nunca marcado como enviado.
- Workers: chame ``await drain()`` antes de encerrar o loop (``asyncio.run``) para não
  deixar o envio para o job de retry.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence, Union

import structlog
from sqlalchemy import and_, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ADMIN_MASTER_ONLY, SCOPE_ESTABLISHMENT, SCOPE_GLOBAL, SCOPE_ORGANIZATION, is_valid_permission
from app.core.timeutils import DEFAULT_TIMEZONE, to_local, utcnow
from app.modules.notifications import templating
from app.modules.notifications.catalog import EMAIL, IN_APP, PUSH, EventType, get_event_type
from app.modules.notifications.push import PUSH_UNAVAILABLE_DETAIL, get_push_provider

log = structlog.get_logger("rd.notifications")

MAX_EMAIL_ATTEMPTS = 5
SENDING_STALE_AFTER = timedelta(minutes=10)
_RETRY_BACKOFF_S = (60, 300, 900, 3600)


# ─── Destinatários ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrgRecipients:
    """Usuários com ``permission`` em escopo ORGANIZATION da org (ou ESTABLISHMENT de um estabelecimento dela)."""

    org_id: Any
    permission: str


@dataclass(frozen=True)
class EstablishmentRecipients:
    """Usuários com ``permission`` no estabelecimento (escopo ESTABLISHMENT) ou na org dona (ORGANIZATION)."""

    establishment_id: Any
    permission: str


@dataclass(frozen=True)
class StaffRecipients:
    """Equipe RD com ``permission`` em escopo GLOBAL (opcional: também CITY da chave informada)."""

    permission: str
    city_key: Optional[str] = None


Recipient = Union[uuid.UUID, str, Any, tuple, OrgRecipients, EstablishmentRecipients, StaffRecipients]


def _as_uuid(v: Any) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def _normalize_recipients(recipients: Union[Recipient, Iterable[Recipient]]) -> list:
    if recipients is None:
        return []
    if isinstance(recipients, (str, uuid.UUID, OrgRecipients, EstablishmentRecipients, StaffRecipients)) or hasattr(
        recipients, "email"
    ):
        recipients = [recipients]
    if isinstance(recipients, tuple) and len(recipients) == 2 and isinstance(recipients[1], str) and "." in recipients[1]:
        recipients = [recipients]
    out = []
    for r in recipients:
        if isinstance(r, (OrgRecipients, EstablishmentRecipients, StaffRecipients)):
            out.append(r)
        elif isinstance(r, tuple) and len(r) == 2:
            scope, perm = r
            if scope is None or (isinstance(scope, str) and scope.upper() == "GLOBAL"):
                out.append(StaffRecipients(perm))
            else:
                out.append(OrgRecipients(_as_uuid(scope), perm))
        elif hasattr(r, "id") and hasattr(r, "email"):
            out.append(_as_uuid(r.id))
        else:
            out.append(_as_uuid(r))
    for r in out:
        if isinstance(r, (OrgRecipients, EstablishmentRecipients, StaffRecipients)) and not is_valid_permission(r.permission):
            raise ValueError(f"permissão fora do catálogo: {r.permission}")
    return out


async def resolve_recipients(db: AsyncSession, recipients: Union[Recipient, Iterable[Recipient]]) -> list[uuid.UUID]:
    """Resolve a lista de destinatários em ids de usuários ATIVOS (ordem estável, sem repetição)."""
    from app.models.establishments import Establishment
    from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
    from app.models.users import User

    items = _normalize_recipients(recipients)
    direct = [r for r in items if isinstance(r, uuid.UUID)]
    specs = [r for r in items if not isinstance(r, uuid.UUID)]
    found: list[uuid.UUID] = list(dict.fromkeys(direct))
    now = utcnow()
    for spec in specs:
        q = (
            select(RoleAssignment.user_id)
            .join(Role, Role.id == RoleAssignment.role_id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .join(User, User.id == RoleAssignment.user_id)
            .where(
                Permission.code == spec.permission,
                or_(RoleAssignment.expires_at.is_(None), RoleAssignment.expires_at > now),
            )
        )
        if spec.permission in ADMIN_MASTER_ONLY:
            q = q.where(User.admin_master_seat.in_((1, 2)), Role.slug == "super_admin",
                        RoleAssignment.scope_type == SCOPE_GLOBAL)
        if isinstance(spec, OrgRecipients):
            org_id = _as_uuid(spec.org_id)
            est_ids = select(Establishment.id).where(Establishment.org_id == org_id)
            q = q.where(
                or_(
                    and_(RoleAssignment.scope_type == SCOPE_ORGANIZATION, RoleAssignment.scope_id == org_id),
                    and_(RoleAssignment.scope_type == SCOPE_ESTABLISHMENT, RoleAssignment.scope_id.in_(est_ids)),
                )
            )
        elif isinstance(spec, EstablishmentRecipients):
            est_id = _as_uuid(spec.establishment_id)
            org_of = select(Establishment.org_id).where(Establishment.id == est_id).scalar_subquery()
            q = q.where(
                or_(
                    and_(RoleAssignment.scope_type == SCOPE_ESTABLISHMENT, RoleAssignment.scope_id == est_id),
                    and_(RoleAssignment.scope_type == SCOPE_ORGANIZATION, RoleAssignment.scope_id == org_of),
                )
            )
        else:  # StaffRecipients
            conds = [RoleAssignment.scope_type == SCOPE_GLOBAL]
            if spec.city_key:
                conds.append(and_(RoleAssignment.scope_type == "CITY", RoleAssignment.scope_value == spec.city_key))
            q = q.where(or_(*conds))
        for (uid,) in (await db.execute(q.distinct())).all():
            if uid not in found:
                found.append(uid)
    if not found:
        return []
    active = set(
        (
            await db.execute(
                select(User.id).where(User.id.in_(found), User.is_active.is_(True), User.anonymized_at.is_(None))
            )
        ).scalars()
    )
    return [u for u in found if u in active]


# ─── Resultado ───────────────────────────────────────────────────────────────

@dataclass
class NotifyResult:
    event_type: str
    recipients: int = 0
    created: list[uuid.UUID] = field(default_factory=list)
    deduped: int = 0
    cooldown_suppressed: int = 0
    skipped_by_preference: int = 0
    email_queued: int = 0
    push_unavailable: int = 0

    @property
    def created_count(self) -> int:
        return len(self.created)


# ─── Formatação de variáveis ─────────────────────────────────────────────────

def _format_vars(data: dict, tz: str = DEFAULT_TIMEZONE) -> dict:
    """Valores CRUS → texto pt-BR (helper único ``templating.format_value``): datas/horas no fuso ``tz``,
    ``*_cents`` em R$, códigos de enum conhecidos traduzidos; ``screen_label`` derivado (nome + código)."""
    from app.core.money import format_brl

    out: dict = {}
    for k, v in (data or {}).items():
        out[k] = str(v) if isinstance(v, uuid.UUID) else templating.format_value(k, v, tz)
    for k, v in (data or {}).items():
        if k.endswith("_cents") and isinstance(v, int) and not isinstance(v, bool):
            base = k[: -len("_cents")]
            if base not in (data or {}):
                out[base] = format_brl(v)
    if ("screen_code" in out or "screen_name" in out) and "screen_label" not in (data or {}):
        out["screen_label"] = templating.screen_label(out.get("screen_name"), out.get("screen_code"))
    return out


async def resolve_timezone(db: AsyncSession, data: dict, entity: Any, recipients: Any) -> str:
    """Fuso para formatar a notificação: estabelecimento do dado (``establishment_id``/``screen_id``/
    entidade tela) ou do destinatário ``EstablishmentRecipients``; senão America/Sao_Paulo."""
    from app.models.establishments import Establishment
    from app.models.screens import Screen

    est_id = data.get("establishment_id") if isinstance(data, dict) else None
    screen_id = data.get("screen_id") if isinstance(data, dict) else None
    if screen_id is None and isinstance(entity, (tuple, list)) and len(entity) == 2 and str(entity[0]) == "screen":
        screen_id = entity[1]
    if est_id is None:
        specs = recipients if isinstance(recipients, (list, tuple, set)) else [recipients]
        ests = [r.establishment_id for r in specs if isinstance(r, EstablishmentRecipients)]
        if len(set(map(str, ests))) == 1:
            est_id = ests[0]
    try:
        if est_id is not None:
            tz = (await db.execute(select(Establishment.timezone).where(Establishment.id == _as_uuid(est_id)))).scalar()
            if tz:
                return tz
        if screen_id is not None:
            tz = (await db.execute(select(Establishment.timezone).join(Screen, Screen.establishment_id == Establishment.id)
                                   .where(Screen.id == _as_uuid(screen_id)))).scalar()
            if tz:
                return tz
    except (ValueError, TypeError):
        pass
    return DEFAULT_TIMEZONE


def _json_safe(data: dict) -> dict:
    from app.core.audit import sanitize

    return sanitize(data or {})


# ─── Preferências / templates ────────────────────────────────────────────────

async def _preferences(db: AsyncSession, user_ids: Sequence[uuid.UUID], event_type: str) -> dict:
    from app.models.notifications import NotificationPreference

    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(NotificationPreference.user_id, NotificationPreference.channel, NotificationPreference.is_enabled).where(
                NotificationPreference.user_id.in_(list(user_ids)), NotificationPreference.event_type == event_type
            )
        )
    ).all()
    return {(u, c): e for u, c, e in rows}


async def load_templates(db: AsyncSession, event_type: str) -> dict[str, tuple[str, str]]:
    from app.models.notifications import NotificationTemplate

    et = get_event_type(event_type)
    out = {ch: (et.title, et.body) for ch in (IN_APP, EMAIL, PUSH)}
    rows = (
        await db.execute(
            select(NotificationTemplate).where(
                NotificationTemplate.event_type == event_type, NotificationTemplate.is_active.is_(True)
            )
        )
    ).scalars()
    for t in rows:
        out[t.channel] = (t.title_template, t.body_template)
    return out


def effective_channel_enabled(et: EventType, prefs: dict, user_id: uuid.UUID, channel: str) -> bool:
    if channel == IN_APP and et.critical:
        return True
    return prefs.get((user_id, channel), True)


# ─── notify ──────────────────────────────────────────────────────────────────

async def _cooldown_allows(db: AsyncSession, user_id: uuid.UUID, event_type: str, entity_key: str, cooldown_s: int) -> bool:
    from app.models.notifications import NotificationCooldown

    now = utcnow()
    tbl = NotificationCooldown.__table__
    stmt = (
        pg_insert(tbl)
        .values(user_id=user_id, event_type=event_type, entity_key=entity_key, last_sent_at=now)
        .on_conflict_do_update(
            index_elements=[tbl.c.user_id, tbl.c.event_type, tbl.c.entity_key],
            set_={"last_sent_at": now},
            where=tbl.c.last_sent_at <= literal(now - timedelta(seconds=cooldown_s)),
        )
        .returning(tbl.c.user_id)
    )
    return (await db.execute(stmt)).first() is not None


def _entity_key(entity: Any, data: dict, dedupe_key: Optional[str]) -> str:
    if entity is not None:
        if isinstance(entity, (tuple, list)):
            return ":".join(str(x) for x in entity)[:200]
        return str(entity)[:200]
    for k in ("entity_id", "screen_id", "campaign_id", "request_id", "ticket_id", "proposal_id"):
        if data.get(k):
            return f"{k}:{data[k]}"[:200]
    return (dedupe_key or "")[:200]


async def notify(
    db: AsyncSession,
    recipients: Union[Recipient, Iterable[Recipient]],
    event_type: str,
    data: Optional[dict] = None,
    dedupe_key: Optional[str] = None,
    cooldown_s: Optional[int] = None,
    channels: Optional[Sequence[str]] = None,
    *,
    entity: Any = None,
    org_id: Any = None,
    link: Optional[str] = None,
    tz: Optional[str] = None,
) -> NotifyResult:
    """Cria notificações (na transação de ``db``) e agenda os envios para após o commit."""
    from app.models.notifications import Notification, NotificationDelivery
    from app.models.users import User

    et = get_event_type(event_type)
    data = dict(data or {})
    requested = tuple(dict.fromkeys(channels or et.default_channels))
    for ch in requested:
        if ch not in (IN_APP, EMAIL, PUSH):
            raise ValueError(f"canal inválido: {ch}")
    if dedupe_key is not None and (not isinstance(dedupe_key, str) or not dedupe_key or len(dedupe_key) > 255):
        raise ValueError("dedupe_key inválida")
    cooldown = et.default_cooldown_s if cooldown_s is None else int(cooldown_s)
    if cooldown < 0:
        raise ValueError("cooldown_s não pode ser negativo")

    result = NotifyResult(event_type=event_type)
    user_ids = await resolve_recipients(db, recipients)
    result.recipients = len(user_ids)
    if not user_ids:
        return result

    users = {
        u.id: u
        for u in (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars()
    }
    prefs = await _preferences(db, user_ids, event_type)
    templates = await load_templates(db, event_type)
    if tz is not None:
        from app.core.timeutils import validate_timezone

        validate_timezone(tz)
    fmt = _format_vars(data, tz or await resolve_timezone(db, data, entity, recipients))
    from app.modules.instance.service import brand_name

    fmt.setdefault("instance_name", await brand_name(db))  # marca da instância (white-label)
    entity_key = _entity_key(entity, fmt, dedupe_key)
    entity_type, entity_id = (None, None)
    if isinstance(entity, (tuple, list)) and len(entity) == 2:
        entity_type, entity_id = str(entity[0])[:50], str(entity[1])[:100]
    rendered_link = templating.render(link or et.link or "", fmt, max_len=500) if (link or et.link) else None
    push = get_push_provider()
    safe_data = _json_safe(data)
    org_uuid = _as_uuid(org_id) if org_id else None

    created_ids: list[uuid.UUID] = []
    pending_delivery_ids: list[uuid.UUID] = []
    ntbl = Notification.__table__

    for uid in user_ids:
        effective = [ch for ch in requested if effective_channel_enabled(et, prefs, uid, ch)]
        if not effective:
            result.skipped_by_preference += 1
            continue
        if dedupe_key is not None:
            exists = (
                await db.execute(select(Notification.id).where(Notification.user_id == uid, Notification.dedupe_key == dedupe_key))
            ).first()
            if exists:
                result.deduped += 1
                continue
        if cooldown > 0 and not await _cooldown_allows(db, uid, event_type, entity_key, cooldown):
            result.cooldown_suppressed += 1
            continue

        title_t, body_t = templates[IN_APP]
        title = templating.render(title_t, fmt, max_len=255)
        body = templating.render(body_t, fmt)
        nid = uuid.uuid4()
        ins = (
            pg_insert(ntbl)
            .values(
                id=nid,
                user_id=uid,
                org_id=org_uuid,
                event_type=event_type,
                title=title,
                body=body,
                data=safe_data,
                entity_type=entity_type,
                entity_id=entity_id,
                link=rendered_link,
                dedupe_key=dedupe_key,
            )
            .on_conflict_do_nothing(constraint="uq_notifications_user_dedupe")
            .returning(ntbl.c.id)
        )
        if (await db.execute(ins)).first() is None:
            result.deduped += 1
            continue
        created_ids.append(nid)

        now = utcnow()
        for ch in effective:
            d = NotificationDelivery(id=uuid.uuid4(), notification_id=nid, user_id=uid, channel=ch)
            if ch == IN_APP:
                d.status, d.sent_at, d.attempts = "SENT", now, 1
            elif ch == EMAIL:
                st, sb = templates[EMAIL]
                d.subject = templating.render(st, fmt, max_len=255)
                d.content = templating.render(sb, fmt)
                if not getattr(users.get(uid), "email", None):
                    d.status, d.detail = "SKIPPED", "usuário sem e-mail"
                else:
                    d.status, d.next_attempt_at = "PENDING", now
                    pending_delivery_ids.append(d.id)
                    result.email_queued += 1
            else:  # PUSH
                pt, pb = templates[PUSH]
                d.subject = templating.render(pt, fmt, max_len=255)
                d.content = templating.render(pb, fmt)
                if push.available:
                    d.status, d.next_attempt_at = "PENDING", now
                    pending_delivery_ids.append(d.id)
                else:
                    d.status, d.detail = "UNAVAILABLE", PUSH_UNAVAILABLE_DETAIL
                    result.push_unavailable += 1
            db.add(d)
    if created_ids:
        await db.flush()
        _schedule_after_commit(db, created_ids, pending_delivery_ids)
    result.created = created_ids
    return result


# ─── Pós-commit: tempo real + envio ──────────────────────────────────────────

_INFLIGHT: set[asyncio.Task] = set()
_PENDING_KEY = "rd_notify_pending"
_HOOK_KEY = "rd_notify_hooked"


def _schedule_after_commit(db: AsyncSession, notification_ids: list, delivery_ids: list) -> None:
    from sqlalchemy import event

    sync = db.sync_session
    pend = sync.info.setdefault(_PENDING_KEY, {"n": [], "d": []})
    pend["n"].extend(notification_ids)
    pend["d"].extend(delivery_ids)
    if not sync.info.get(_HOOK_KEY):
        event.listen(sync, "after_commit", _after_commit)
        event.listen(sync, "after_rollback", _after_rollback)
        sync.info[_HOOK_KEY] = True


def _after_rollback(session) -> None:  # noqa: ANN001
    session.info.pop(_PENDING_KEY, None)


def _after_commit(session) -> None:  # noqa: ANN001
    pend = session.info.pop(_PENDING_KEY, None)
    if not pend or (not pend["n"] and not pend["d"]):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.info("notify_dispatch_deferred", notifications=len(pend["n"]), deliveries=len(pend["d"]))
        return  # sem loop: o job de retry envia
    task = loop.create_task(_dispatch_after_commit(list(pend["n"]), list(pend["d"])))
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)


def publish_event_after_commit(db: AsyncSession, type: str, data: dict, **scope: Any) -> None:
    """Publica um evento do contrato §5 SOMENTE após o commit da transação de ``db`` (rollback = nada).
    Usa o mesmo conjunto de tarefas do ``drain()``."""
    from sqlalchemy import event

    sync = db.sync_session
    pend = sync.info.setdefault("rd_events_pending", [])
    pend.append((type, data, scope))
    if sync.info.get("rd_events_hooked"):
        return

    def _commit(session) -> None:  # noqa: ANN001
        items = session.info.pop("rd_events_pending", [])
        if not items:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _go():
            from app.core import events

            for t, d, sc in items:
                try:
                    await events.publish(t, d, **sc)
                except Exception as exc:  # tempo real é best-effort; o banco é a fonte de verdade
                    log.warning("event_publish_failed", event_type=t, error_type=exc.__class__.__name__)

        task = loop.create_task(_go())
        _INFLIGHT.add(task)
        task.add_done_callback(_INFLIGHT.discard)

    def _rollback(session) -> None:  # noqa: ANN001
        session.info.pop("rd_events_pending", None)

    event.listen(sync, "after_commit", _commit)
    event.listen(sync, "after_rollback", _rollback)
    sync.info["rd_events_hooked"] = True


async def drain(timeout: float = 30.0) -> None:
    """Aguarda os envios pós-commit em andamento neste processo (workers/testes)."""
    pending = [t for t in list(_INFLIGHT) if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)


async def _dispatch_after_commit(notification_ids: list, delivery_ids: list) -> None:
    try:
        await publish_created(notification_ids)
    except Exception as exc:  # tempo real é best-effort; a central é a fonte de verdade
        log.warning("notification_publish_failed", error_type=type(exc).__name__)
    if delivery_ids:
        try:
            await dispatch_deliveries(delivery_ids)
        except Exception as exc:
            log.error("notification_dispatch_failed", error_type=type(exc).__name__)


async def publish_created(notification_ids: Sequence[uuid.UUID], db: Optional[AsyncSession] = None) -> int:
    from app.core import events
    from app.core.database import session_factory
    from app.core.timeutils import isoformat_z
    from app.models.notifications import Notification

    if not notification_ids:
        return 0

    async def _load(s: AsyncSession):
        rows = list((await s.execute(select(Notification).where(Notification.id.in_(list(notification_ids))))).scalars())
        counts = dict(
            (
                await s.execute(
                    select(Notification.user_id, func.count())
                    .where(Notification.user_id.in_({r.user_id for r in rows}), Notification.read_at.is_(None))
                    .group_by(Notification.user_id)
                )
            ).all()
        ) if rows else {}
        return rows, counts

    if db is not None:
        rows, counts = await _load(db)
    else:
        async with session_factory()() as s:
            rows, counts = await _load(s)
    for n in rows:
        await events.publish(
            "notification.created",
            {
                "id": str(n.id),
                "event_type": n.event_type,
                "title": n.title,
                "link": n.link,
                "created_at": isoformat_z(n.created_at) if n.created_at else None,
                "unread_count": int(counts.get(n.user_id, 0)),
            },
            user_id=n.user_id,
        )
    return len(rows)


async def dispatch_deliveries(delivery_ids: Optional[Sequence[uuid.UUID]] = None, *, limit: int = 200,
                              engine=None) -> dict:
    """Envia entregas PENDING (ou SENDING abandonadas). Cada entrega é reivindicada
    atomicamente (``FOR UPDATE SKIP LOCKED``) — dois despachantes nunca enviam a mesma."""
    from app.core.database import session_factory
    from app.models.notifications import NotificationDelivery

    sf = session_factory(engine) if engine is not None else session_factory()
    now = utcnow()
    async with sf() as s:
        q = (
            select(NotificationDelivery.id)
            .where(
                or_(
                    and_(NotificationDelivery.status == "PENDING",
                         or_(NotificationDelivery.next_attempt_at.is_(None), NotificationDelivery.next_attempt_at <= now)),
                    and_(NotificationDelivery.status == "SENDING", NotificationDelivery.claimed_at < now - SENDING_STALE_AFTER),
                )
            )
            .order_by(NotificationDelivery.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if delivery_ids is not None:
            q = q.where(NotificationDelivery.id.in_(list(delivery_ids)))
        ids = list((await s.execute(q)).scalars())
        if ids:
            await s.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id.in_(ids))
                .values(status="SENDING", claimed_at=now, attempts=NotificationDelivery.attempts + 1)
            )
        await s.commit()
    stats = {"claimed": len(ids), "sent": 0, "failed": 0, "retry": 0}
    for did in ids:
        outcome = await _send_one(sf, did)
        stats[outcome] += 1
    return stats


async def _send_one(sf, delivery_id: uuid.UUID) -> str:
    from app.core.config import settings
    from app.core.email import OutgoingEmail, get_email_sender, mask_email
    from app.models.notifications import Notification, NotificationDelivery
    from app.models.users import User

    async with sf() as s:
        d = await s.get(NotificationDelivery, delivery_id)
        if d is None or d.status != "SENDING":
            return "retry"
        n = await s.get(Notification, d.notification_id)
        user = await s.get(User, d.user_id)
        error: Optional[str] = None
        try:
            if user is None or not user.is_active or user.anonymized_at is not None:
                raise _Permanent("usuário inativo")
            if d.channel == EMAIL:
                if not user.email:
                    raise _Permanent("usuário sem e-mail")
                from app.modules.instance.service import email_html, get_brand

                brand = await get_brand(s, with_logo=False)
                brand.logo = b"1" if await _has_logo(s) else None  # só sinaliza: o HTML usa a URL pública da logo
                prefs_url = f"{settings.panel_base_url}/notificacoes/preferencias"
                link = f"{settings.panel_base_url}{n.link}" if n is not None and n.link else None
                footer_txt = (f"{brand.name}. Você recebe este e-mail conforme suas preferências de notificação: "
                              f"{prefs_url}")
                subject = d.subject or (n.title if n else brand.name)
                await get_email_sender().send(
                    OutgoingEmail(to=user.email, subject=subject,
                                  text=(d.content or "") + (f"\n\n{link}" if link else "") + "\n\n—\n" + footer_txt,
                                  html=email_html(brand, subject, d.content or "", link=link, footer=footer_txt),
                                  tags={"notification_id": str(d.notification_id)})
                )
                log.info("notification_email_sent", to=mask_email(user.email), event_type=n.event_type if n else None)
            elif d.channel == PUSH:
                push = get_push_provider()
                if not push.available:
                    raise _Permanent(PUSH_UNAVAILABLE_DETAIL, status="UNAVAILABLE")
                await push.send(str(user.id), d.subject or "", d.content or "", {"notification_id": str(d.notification_id)})
            d.status, d.sent_at, d.detail = "SENT", utcnow(), None
            outcome = "sent"
        except _Permanent as exc:
            d.status, d.detail = exc.status, str(exc)[:500]
            outcome = "failed"
        except Exception as exc:  # falha transitória: backoff
            error = type(exc).__name__
            if d.attempts >= MAX_EMAIL_ATTEMPTS:
                d.status, d.detail = "FAILED", f"falha após {d.attempts} tentativas ({error})"
                outcome = "failed"
            else:
                wait = _RETRY_BACKOFF_S[min(d.attempts - 1, len(_RETRY_BACKOFF_S) - 1)]
                d.status, d.detail = "PENDING", f"tentativa {d.attempts} falhou ({error})"
                d.next_attempt_at = utcnow() + timedelta(seconds=wait)
                outcome = "retry"
            log.warning("notification_delivery_error", channel=d.channel, error_type=error, attempts=d.attempts)
        await s.commit()
        return outcome


async def _has_logo(s: AsyncSession) -> bool:
    from app.modules.instance.service import get_profile

    p = await get_profile(s)
    return bool(p is not None and p.logo_key)


class _Permanent(Exception):
    def __init__(self, msg: str, status: str = "FAILED"):
        super().__init__(msg)
        self.status = status


# ─── Consultas da central ────────────────────────────────────────────────────

async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    from app.models.notifications import Notification

    return int(
        (await db.execute(
            select(func.count()).select_from(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )).scalar_one()
    )


async def mark_read(db: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
    from app.models.notifications import Notification

    n = await db.get(Notification, notification_id)
    if n is None or n.user_id != user_id:
        return False
    if n.read_at is None:
        n.read_at = utcnow()
    return True


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    from app.models.notifications import Notification

    res = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    return int(res.rowcount or 0)


__all__ = [
    "notify",
    "NotifyResult",
    "OrgRecipients",
    "EstablishmentRecipients",
    "StaffRecipients",
    "resolve_recipients",
    "dispatch_deliveries",
    "publish_created",
    "drain",
    "unread_count",
    "mark_read",
    "mark_all_read",
    "load_templates",
]
