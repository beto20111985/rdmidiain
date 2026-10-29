"""
RD Mídia Indoor — Suporte / chamados (AÇÃO 002 / W-BIZ).

Corrige o achado A7-A10 (qualquer autenticado listava TODOS os chamados e escrevia
comentário interno em qualquer chamado):
- listagem e leitura filtradas pelo escopo (``support.view``): organização/estabelecimento
  do cliente; GLOBAL/CITY da equipe; técnico vê os atribuídos a ele. Fora do escopo → 404;
- comentário INTERNO só com ``support.manage`` no escopo do chamado; cliente nunca o vê;
- o estabelecimento abre pedido de RETIRADA informando o ID ``RD-XXXXXX`` que viu na tela
  (ETAPA2 #1086–#1091) e NUNCA recebe a identidade do anunciante; a equipe localiza o
  criativo/campanha/anunciante por busca EXATA do código.
"""
from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Any, Optional

import structlog
from sqlalchemy import and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, city_key as make_city_key
from app.core.errors import DomainError, NotFound
from app.core.ids import CREATIVE_CODE_RE, generate_unique, random_safe
from app.core.settings_service import SettingDef, get_setting, register
from app.core.timeutils import utcnow
from app.models.support import TICKET_CATEGORIES, Ticket, TicketEvent, TicketMessage

log = structlog.get_logger("rd.support")

for _p, _h in (("baixa", 72), ("media", 24), ("alta", 8), ("critica", 4)):
    register(SettingDef(f"support.sla_hours_{_p}", _h, "int", f"SLA de resolução (horas) — prioridade {_p.upper()}.",
                        min=1, max=720, category="suporte"))

TRANSITIONS = {
    "ABERTO": {"EM_ATENDIMENTO", "AGUARDANDO_CLIENTE", "RESOLVIDO", "CANCELADO"},
    "EM_ATENDIMENTO": {"AGUARDANDO_CLIENTE", "RESOLVIDO", "CANCELADO"},
    "AGUARDANDO_CLIENTE": {"EM_ATENDIMENTO", "RESOLVIDO", "CANCELADO"},
    "RESOLVIDO": {"FECHADO", "EM_ATENDIMENTO"},
    "FECHADO": set(),
    "CANCELADO": set(),
}
CLIENT_TRANSITIONS = {("ABERTO", "CANCELADO"), ("RESOLVIDO", "FECHADO"), ("RESOLVIDO", "EM_ATENDIMENTO")}
CLIENT_PRIORITIES = {"BAIXA", "MEDIA", "ALTA"}


def normalize_creative_code(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    code = re.sub(r"\s+", "", raw).upper()
    if not code:
        return None
    if not code.startswith("RD-") and len(code) == 6:
        code = "RD-" + code
    if not CREATIVE_CODE_RE.match(code):
        raise DomainError("CREATIVE_CODE_INVALID", "Código de anúncio inválido (formato RD-XXXXXX).", status=400)
    return code


# ─── Escopo ──────────────────────────────────────────────────────────────────

def is_staff_for(ctx: AuthContext, t: Ticket) -> bool:
    if ctx.can("support.manage", city=t.city_key) if t.city_key else ctx.can("support.manage", global_only=True):
        return True
    return t.assigned_to is not None and t.assigned_to == ctx.user_id and ctx.has_any("support.manage")


def can_view(ctx: AuthContext, t: Ticket) -> bool:
    if is_staff_for(ctx, t):
        return True
    if ctx.can("support.view", org_id=t.org_id, establishment_id=t.establishment_id, city=t.city_key):
        return True
    return ctx.can("support.create", org_id=t.org_id, establishment_id=t.establishment_id) and t.created_by == ctx.user_id


def list_filter(ctx: AuthContext):
    if ctx.can("support.view", global_only=True) or ctx.can("support.manage", global_only=True):
        return true()
    return or_(
        ctx.scope_filter("support.view", org_col=Ticket.org_id, establishment_col=Ticket.establishment_id,
                         city_col=Ticket.city_key),
        ctx.scope_filter("support.manage", city_col=Ticket.city_key),
        and_(Ticket.assigned_to == ctx.user_id),
        and_(Ticket.created_by == ctx.user_id),
    )


async def get_ticket(db: AsyncSession, ctx: AuthContext, ticket_id: uuid.UUID, *, for_update: bool = False) -> Ticket:
    q = select(Ticket).where(Ticket.id == ticket_id)
    if for_update:
        q = q.with_for_update()
    t = (await db.execute(q)).scalar_one_or_none()
    if t is None or not can_view(ctx, t):
        raise NotFound()
    return t


async def _event(db: AsyncSession, ctx: Any, t: Ticket, type_: str, frm: Optional[str], to: Optional[str],
                 internal: bool = False) -> None:
    db.add(TicketEvent(ticket_id=t.id, actor_id=getattr(ctx, "user_id", None), type=type_, from_value=frm,
                       to_value=to, internal=internal))


async def _rt(t: Ticket) -> None:
    from app.core import events

    try:
        await events.publish("ticket.updated", {"id": str(t.id), "number": t.number, "status": t.status},
                             org_id=t.org_id, establishment_id=t.establishment_id, city=t.city_key)
    except Exception as exc:  # best-effort
        log.warning("ticket_event_publish_failed", error_type=type(exc).__name__)


async def _sla_due(db: AsyncSession, priority: str):
    hours = int(await get_setting(db, f"support.sla_hours_{priority.lower()}"))
    return utcnow() + timedelta(hours=hours)


# ─── Busca do criativo (equipe) ──────────────────────────────────────────────

async def resolve_creative(db: AsyncSession, code: str) -> Optional[dict]:
    """Busca EXATA pelo código público. Retorna criativo, anunciante e campanhas."""
    from app.models.campaigns import Campaign, CampaignCreative, Creative
    from app.models.organizations import Organization

    c = (await db.execute(select(Creative).where(Creative.code == code))).scalar_one_or_none()
    if c is None:
        return None
    org = await db.get(Organization, c.org_id)
    camps = (await db.execute(
        select(Campaign).join(CampaignCreative, CampaignCreative.campaign_id == Campaign.id)
        .where(CampaignCreative.creative_id == c.id).order_by(Campaign.created_at.desc())
    )).scalars().all()
    return {
        "creative": {"id": str(c.id), "code": c.code, "name": c.name, "status": c.status,
                     "kind": getattr(c, "kind", None), "duration_seconds": getattr(c, "duration_seconds", None)},
        "advertiser": {"org_id": str(c.org_id), "name": org.name if org else None},
        "campaigns": [{"id": str(k.id), "name": k.name, "status": k.status,
                       "start_date": k.start_date.isoformat() if getattr(k, "start_date", None) else None,
                       "end_date": k.end_date.isoformat() if getattr(k, "end_date", None) else None} for k in camps],
        "_ids": {"creative_id": c.id, "advertiser_org_id": c.org_id, "campaign_id": camps[0].id if camps else None},
    }


# ─── Criação ─────────────────────────────────────────────────────────────────

async def create_ticket(db: AsyncSession, ctx: AuthContext, data: dict) -> Ticket:
    from app.models.establishments import Establishment
    from app.models.organizations import Organization
    from app.modules.notifications.service import StaffRecipients, notify

    org_id = data.get("org_id")
    est_id = data.get("establishment_id")
    est = None
    if est_id is not None:
        est = await db.get(Establishment, est_id)
        if est is None:
            raise NotFound()
        if org_id is None:
            org_id = est.org_id
        elif est.org_id != org_id:
            raise NotFound()
    if org_id is not None and await db.get(Organization, org_id) is None:
        raise NotFound()
    staff = ctx.can("support.manage", global_only=True) or (
        ctx.has_any("support.manage") and est is not None and ctx.can("support.manage", city=_est_city(est)))
    if not staff:
        if org_id is None or not ctx.can("support.create", org_id=org_id, establishment_id=est_id):
            raise NotFound()
    category = data.get("category") or "DUVIDA"
    if category not in TICKET_CATEGORIES:
        raise DomainError("TICKET_CATEGORY_INVALID", "Categoria inválida.", status=400)
    priority = data.get("priority") or "MEDIA"
    if not staff and priority not in CLIENT_PRIORITIES:
        priority = "ALTA"
    code = normalize_creative_code(data.get("creative_code"))
    if category == "RETIRADA_ANUNCIO" and not code:
        raise DomainError("CREATIVE_CODE_REQUIRED", "Informe o ID do anúncio exibido na tela (RD-XXXXXX).", status=400)
    screen_id = data.get("screen_id")
    if screen_id is not None:
        from app.models.screens import Screen

        scr = await db.get(Screen, screen_id)
        if scr is None or (est_id is not None and scr.establishment_id != est_id):
            raise NotFound()
        if est is None:
            est = await db.get(Establishment, scr.establishment_id)
            est_id = est.id if est else None
            if not staff and (est is None or est.org_id != org_id):
                raise NotFound()

    async def exists(n: str) -> bool:
        return (await db.execute(select(Ticket.id).where(Ticket.number == n))).first() is not None

    t = Ticket(
        id=uuid.uuid4(), number=await generate_unique(lambda: f"CH-{random_safe(6)}", exists), org_id=org_id,
        establishment_id=est_id, screen_id=screen_id, creative_code=code, category=category,
        title=data["title"].strip(), description=data["description"].strip(), status="ABERTO", priority=priority,
        created_by=ctx.user_id, city_key=_est_city(est) if est else data.get("city_key"),
        sla_due_at=await _sla_due(db, priority), version=1,
    )
    if code:
        found = await resolve_creative(db, code)
        if found:
            t.creative_id = found["_ids"]["creative_id"]
            t.advertiser_org_id = found["_ids"]["advertiser_org_id"]
            t.campaign_id = found["_ids"]["campaign_id"]
    db.add(t)
    await db.flush()
    await _event(db, ctx, t, "CRIADO", None, t.status)
    await audit(db, ctx, "support.ticket_created", "ticket", t.id,
                after={"number": t.number, "category": category, "creative_code": code}, org_id=org_id)
    await notify(db, [StaffRecipients("support.manage")] +
                 ([StaffRecipients("support.manage", city_key=t.city_key)] if t.city_key else []),
                 "support.ticket_created", {"ticket_number": t.number, "title": t.title, "ticket_id": str(t.id)},
                 dedupe_key=f"support.ticket_created:{t.id}", channels=["IN_APP"])
    await _rt(t)
    return t


def _est_city(est) -> Optional[str]:
    if est is None or not getattr(est, "city", None) or not getattr(est, "state", None):
        return None
    return make_city_key(est.state, est.city)


# ─── Mensagens ───────────────────────────────────────────────────────────────

async def add_message(db: AsyncSession, ctx: AuthContext, ticket_id: uuid.UUID, body: str, is_internal: bool) -> TicketMessage:
    from app.modules.notifications.service import StaffRecipients, notify

    t = await get_ticket(db, ctx, ticket_id, for_update=True)
    staff = is_staff_for(ctx, t)
    if is_internal and not staff:
        raise DomainError("FORBIDDEN", "Comentário interno é exclusivo da equipe RD.", status=403,
                          details={"permission": "support.manage"})
    if t.status in ("FECHADO", "CANCELADO"):
        raise DomainError("TICKET_CLOSED", "Chamado encerrado. Abra um novo chamado.", status=409)
    m = TicketMessage(ticket_id=t.id, author_id=ctx.user_id, body=body.strip(), is_internal=is_internal)
    db.add(m)
    before = t.status
    if staff and not is_internal and t.first_response_at is None:
        t.first_response_at = utcnow()
    if not staff and t.status == "AGUARDANDO_CLIENTE":
        t.status = "EM_ATENDIMENTO"
        await _event(db, ctx, t, "STATUS", before, t.status)
    t.version += 1
    await db.flush()
    excerpt = body.strip()[:200]
    if not is_internal:
        if staff:
            await notify(db, [t.created_by], "support.ticket_reply",
                         {"ticket_number": t.number, "excerpt": excerpt, "ticket_id": str(t.id)},
                         dedupe_key=f"support.reply:{m.id}", org_id=t.org_id)
        else:
            await notify(db, [t.assigned_to] if t.assigned_to else [StaffRecipients("support.manage")],
                         "support.ticket_reply", {"ticket_number": t.number, "excerpt": excerpt, "ticket_id": str(t.id)},
                         dedupe_key=f"support.reply:{m.id}", channels=["IN_APP"])
    await _rt(t)
    return m


async def list_messages(db: AsyncSession, ctx: AuthContext, t: Ticket) -> list[TicketMessage]:
    q = select(TicketMessage).where(TicketMessage.ticket_id == t.id).order_by(TicketMessage.created_at, TicketMessage.id)
    if not is_staff_for(ctx, t):
        q = q.where(TicketMessage.is_internal.is_(False))
    return list((await db.execute(q)).scalars())


# ─── Status / atribuição / prioridade ────────────────────────────────────────

async def change_status(db: AsyncSession, ctx: AuthContext, ticket_id: uuid.UUID, to: str, note: Optional[str]) -> Ticket:
    from app.modules.notifications.service import notify

    t = await get_ticket(db, ctx, ticket_id, for_update=True)
    staff = is_staff_for(ctx, t)
    if to not in TRANSITIONS.get(t.status, set()):
        raise DomainError("TICKET_INVALID_TRANSITION", "Mudança de status não permitida.", status=409,
                          details={"from": t.status, "to": to})
    if not staff and (t.status, to) not in CLIENT_TRANSITIONS:
        raise DomainError("FORBIDDEN", "Somente a equipe RD altera este status.", status=403)
    before = t.status
    t.status = to
    now = utcnow()
    if to == "RESOLVIDO":
        t.resolved_at = now
    if to in ("FECHADO", "CANCELADO"):
        t.closed_at = now
    if to == "EM_ATENDIMENTO" and before == "RESOLVIDO":
        t.resolved_at = None
    t.version += 1
    await _event(db, ctx, t, "STATUS", before, to)
    await audit(db, ctx, "support.ticket_status", "ticket", t.id, before={"status": before}, after={"status": to},
                reason=note, org_id=t.org_id)
    if staff:
        await notify(db, [t.created_by], "support.ticket_updated",
                     {"ticket_number": t.number, "status": to, "ticket_id": str(t.id)},  # código; rótulo na renderização
                     dedupe_key=f"support.status:{t.id}:{t.version}", org_id=t.org_id)
    await _rt(t)
    return t


async def assign(db: AsyncSession, ctx: AuthContext, ticket_id: uuid.UUID, user_id: Optional[uuid.UUID]) -> Ticket:
    from app.modules.notifications.service import StaffRecipients, notify, resolve_recipients

    t = await get_ticket(db, ctx, ticket_id, for_update=True)
    if not is_staff_for(ctx, t):
        raise DomainError("FORBIDDEN", "Somente a equipe RD atribui chamados.", status=403)
    target = user_id or ctx.user_id
    allowed = await resolve_recipients(db, [StaffRecipients("support.manage")] +
                                       ([StaffRecipients("support.manage", city_key=t.city_key)] if t.city_key else []))
    if target not in allowed:
        raise DomainError("ASSIGNEE_INVALID", "O usuário indicado não atende chamados nesta região.", status=400)
    before = str(t.assigned_to) if t.assigned_to else None
    t.assigned_to = target
    if t.status == "ABERTO":
        t.status = "EM_ATENDIMENTO"
        await _event(db, ctx, t, "STATUS", "ABERTO", "EM_ATENDIMENTO")
    t.version += 1
    await _event(db, ctx, t, "ATRIBUICAO", before, str(target), internal=True)
    await audit(db, ctx, "support.ticket_assigned", "ticket", t.id, before={"assigned_to": before},
                after={"assigned_to": target}, org_id=t.org_id)
    if target != ctx.user_id:
        await notify(db, [target], "support.ticket_created", {"ticket_number": t.number, "title": t.title,
                                                              "ticket_id": str(t.id)},
                     dedupe_key=f"support.assigned:{t.id}:{target}", channels=["IN_APP", "EMAIL"])
    await _rt(t)
    return t


async def set_priority(db: AsyncSession, ctx: AuthContext, ticket_id: uuid.UUID, priority: str) -> Ticket:
    t = await get_ticket(db, ctx, ticket_id, for_update=True)
    if not is_staff_for(ctx, t):
        raise DomainError("FORBIDDEN", "Somente a equipe RD altera a prioridade.", status=403)
    before = t.priority
    t.priority = priority
    t.sla_due_at = t.created_at + (await _sla_due(db, priority) - utcnow())
    t.version += 1
    await _event(db, ctx, t, "PRIORIDADE", before, priority)
    await audit(db, ctx, "support.ticket_priority", "ticket", t.id, before={"priority": before},
                after={"priority": priority, "sla_due_at": t.sla_due_at}, org_id=t.org_id)
    return t
