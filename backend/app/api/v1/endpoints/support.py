"""RD Mídia Indoor — Suporte / chamados (AÇÃO 002 / W-BIZ). Prefixo ``/api/v1/support``.

Rotas: ``GET|POST /tickets``, ``GET /tickets/{id}``, ``GET /assignees``, ``GET|POST /tickets/{id}/messages``,
``POST /tickets/{id}/status``, ``POST /tickets/{id}/assign``, ``POST /tickets/{id}/priority``,
``GET /creatives/{code}`` (equipe: busca EXATA do criativo/campanha/anunciante pelo RD-XXXXXX).

Removidas (legado inseguro, A7-A10): ``GET|POST /support`` sem escopo,
``PATCH /support/{id}/status``, ``POST /support/{id}/messages`` aceitando ``is_internal`` de qualquer um.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.core.timeutils import isoformat_z, utcnow
from app.models.support import Ticket, TicketEvent
from app.modules.support import service as svc

router = APIRouter()

Category = Literal["DUVIDA", "PROBLEMA_TELA", "RETIRADA_ANUNCIO", "CAMPANHA", "FINANCEIRO", "INSTALACAO",
                   "MANUTENCAO", "OUTRO"]
Priority = Literal["BAIXA", "MEDIA", "ALTA", "CRITICA"]
Status = Literal["ABERTO", "EM_ATENDIMENTO", "AGUARDANDO_CLIENTE", "RESOLVIDO", "FECHADO", "CANCELADO"]


def _z(dt: Optional[datetime]) -> Optional[str]:
    return isoformat_z(dt) if dt else None


def _s(v) -> Optional[str]:
    return str(v) if v is not None else None


def _out(t: Ticket, staff: bool) -> dict:
    out = {
        "id": str(t.id), "number": t.number, "org_id": _s(t.org_id), "establishment_id": _s(t.establishment_id),
        "screen_id": _s(t.screen_id), "creative_code": t.creative_code, "category": t.category, "title": t.title,
        "description": t.description, "status": t.status, "priority": t.priority, "created_by": str(t.created_by),
        "assigned": t.assigned_to is not None, "sla_due_at": _z(t.sla_due_at),
        "sla_breached": bool(t.sla_due_at and t.status not in ("RESOLVIDO", "FECHADO", "CANCELADO") and utcnow() > t.sla_due_at),
        "first_response_at": _z(t.first_response_at), "resolved_at": _z(t.resolved_at), "closed_at": _z(t.closed_at),
        "created_at": _z(t.created_at), "updated_at": _z(t.updated_at),
    }
    if staff:  # identidade comercial só para a equipe RD (nunca para o estabelecimento)
        out.update({"assigned_to": _s(t.assigned_to), "creative_id": _s(t.creative_id), "campaign_id": _s(t.campaign_id),
                    "advertiser_org_id": _s(t.advertiser_org_id), "city_key": t.city_key,
                    "creative_found": t.creative_id is not None if t.creative_code else None})
    return out


class TicketIn(BaseModel):
    org_id: Optional[UUID] = None
    establishment_id: Optional[UUID] = None
    screen_id: Optional[UUID] = None
    creative_code: Optional[str] = Field(None, max_length=12)
    category: Category = "DUVIDA"
    priority: Priority = "MEDIA"
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=3, max_length=10000)


@router.get("/tickets")
async def list_tickets(
    status: Optional[Status] = None, category: Optional[Category] = None, assigned_to_me: bool = False,
    creative_code: Optional[str] = Query(None, max_length=12),
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db),
):
    if not any(ctx.has_any(p) for p in ("support.view", "support.create", "support.manage")):
        raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403,
                          details={"permission": "support.view"})
    cond = [svc.list_filter(ctx)]
    if status:
        cond.append(Ticket.status == status)
    if category:
        cond.append(Ticket.category == category)
    if assigned_to_me:
        cond.append(Ticket.assigned_to == ctx.user_id)
    if creative_code:
        cond.append(Ticket.creative_code == svc.normalize_creative_code(creative_code))
    total = (await db.execute(select(func.count()).select_from(Ticket).where(*cond))).scalar_one()
    rows = (await db.execute(select(Ticket).where(*cond).order_by(Ticket.created_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars()
    return {"items": [_out(t, svc.is_staff_for(ctx, t)) for t in rows], "total": total, "page": page,
            "page_size": page_size}


@router.post("/tickets", status_code=201)
async def create_ticket(body: TicketIn, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    if not (ctx.has_any("support.create") or ctx.has_any("support.manage")):
        raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403,
                          details={"permission": "support.create"})
    t = await svc.create_ticket(db, ctx, body.model_dump())
    return _out(t, svc.is_staff_for(ctx, t))


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: UUID, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    t = await svc.get_ticket(db, ctx, ticket_id)
    staff = svc.is_staff_for(ctx, t)
    out = _out(t, staff)
    q = select(TicketEvent).where(TicketEvent.ticket_id == t.id).order_by(TicketEvent.created_at, TicketEvent.id)
    if not staff:
        q = q.where(TicketEvent.internal.is_(False))
    out["history"] = [{"type": e.type, "from": e.from_value, "to": e.to_value, "at": _z(e.created_at)}
                      for e in (await db.execute(q)).scalars()]
    out["messages"] = await _messages_out(db, ctx, await svc.list_messages(db, ctx, t), staff)
    return out


async def _messages_out(db: AsyncSession, ctx: AuthContext, msgs: list, viewer_is_staff: bool) -> list[dict]:
    """Autor exibido conforme a regra de ``app.modules.support.people`` (cliente vê "Equipe RD")."""
    from app.modules.support.people import describe_authors

    authors = await describe_authors(db, {m.author_id for m in msgs}, viewer_is_staff=viewer_is_staff,
                                     viewer_user_id=ctx.user_id)
    out = []
    for m in msgs:
        a = authors.get(m.author_id, {"author_name": "Usuário", "author_is_staff": False})
        hide_id = a["author_is_staff"] and not viewer_is_staff and m.author_id != ctx.user_id
        out.append({"id": str(m.id), "author_id": None if hide_id else str(m.author_id),
                    "author_name": a["author_name"], "author_is_staff": a["author_is_staff"], "body": m.body,
                    "is_internal": m.is_internal, "created_at": _z(m.created_at)})
    return out


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    is_internal: bool = False


@router.get("/tickets/{ticket_id}/messages")
async def list_messages(ticket_id: UUID, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    t = await svc.get_ticket(db, ctx, ticket_id)
    return {"items": await _messages_out(db, ctx, await svc.list_messages(db, ctx, t), svc.is_staff_for(ctx, t))}


@router.post("/tickets/{ticket_id}/messages", status_code=201)
async def add_message(ticket_id: UUID, body: MessageIn, ctx: AuthContext = Depends(get_auth_context),
                      db: AsyncSession = Depends(get_db)):
    m = await svc.add_message(db, ctx, ticket_id, body.body, body.is_internal)
    t = await svc.get_ticket(db, ctx, ticket_id)
    return (await _messages_out(db, ctx, [m], svc.is_staff_for(ctx, t)))[0]


class StatusIn(BaseModel):
    status: Status
    note: Optional[str] = Field(None, max_length=1000)


@router.post("/tickets/{ticket_id}/status")
async def change_status(ticket_id: UUID, body: StatusIn, ctx: AuthContext = Depends(get_auth_context),
                        db: AsyncSession = Depends(get_db)):
    t = await svc.change_status(db, ctx, ticket_id, body.status, body.note)
    return _out(t, svc.is_staff_for(ctx, t))


class AssignIn(BaseModel):
    user_id: Optional[UUID] = None


@router.post("/tickets/{ticket_id}/assign")
async def assign(ticket_id: UUID, body: AssignIn, ctx: AuthContext = Depends(require("support.manage")),
                 db: AsyncSession = Depends(get_db)):
    return _out(await svc.assign(db, ctx, ticket_id, body.user_id), True)


class PriorityIn(BaseModel):
    priority: Priority


@router.post("/tickets/{ticket_id}/priority")
async def set_priority(ticket_id: UUID, body: PriorityIn, ctx: AuthContext = Depends(require("support.manage")),
                       db: AsyncSession = Depends(get_db)):
    return _out(await svc.set_priority(db, ctx, ticket_id, body.priority), True)


@router.get("/assignees")
async def list_assignees(ticket_id: Optional[UUID] = None, ctx: AuthContext = Depends(require("support.manage")),
                         db: AsyncSession = Depends(get_db)):
    """Quem pode receber atribuição (``support.manage``): equipe GLOBAL + regional da cidade do chamado
    (ou das cidades do solicitante). Somente id, nome, escopos e carga — sem dados pessoais."""
    from app.models.support import Ticket as T
    from app.modules.support.people import users_with_permission

    if ticket_id is not None:
        t = await svc.get_ticket(db, ctx, ticket_id)
        if not svc.is_staff_for(ctx, t):
            raise NotFound()
        cands = await users_with_permission(db, "support.manage", cities={t.city_key} if t.city_key else None)
    elif ctx.can("support.manage", global_only=True):
        cands = await users_with_permission(db, "support.manage", all_cities=True)
    else:
        cands = await users_with_permission(db, "support.manage", cities=set(ctx.city_keys("support.manage") or ()))
    load = {}
    if cands:
        load = dict((await db.execute(
            select(T.assigned_to, func.count()).where(
                T.assigned_to.in_([c.user_id for c in cands]),
                T.status.in_(("ABERTO", "EM_ATENDIMENTO", "AGUARDANDO_CLIENTE"))).group_by(T.assigned_to))).all())
    return {"items": [{"user_id": str(c.user_id), "name": c.name, "scopes": c.scopes, "roles": c.roles,
                       "open_tickets": int(load.get(c.user_id, 0))} for c in cands]}


@router.get("/creatives/{code}")
async def lookup_creative(code: str, ctx: AuthContext = Depends(require("support.manage")),
                          db: AsyncSession = Depends(get_db)):
    """Equipe RD: localiza criativo, campanhas e anunciante pelo ID exibido na tela (busca exata)."""
    norm = svc.normalize_creative_code(code)
    found = await svc.resolve_creative(db, norm)
    if found is None:
        raise NotFound("CREATIVE_NOT_FOUND", "Nenhum anúncio com este ID.")
    found.pop("_ids", None)
    tickets = (await db.execute(select(Ticket).where(Ticket.creative_code == norm).where(svc.list_filter(ctx))
                                .order_by(Ticket.created_at.desc()).limit(50))).scalars()
    found["tickets"] = [{"id": str(t.id), "number": t.number, "status": t.status, "category": t.category,
                         "created_at": _z(t.created_at)} for t in tickets]
    return found
