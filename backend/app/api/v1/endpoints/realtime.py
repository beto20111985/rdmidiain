"""
RD Mídia Indoor — Ticket de WebSocket (contrato §5).

POST /realtime/ticket (Bearer) → {"ticket", "expires_in": 30}. Uso único (GETDEL no
Redis). O WS ``/ws/admin?ticket=...`` consome o ticket (``events.consume_ws_ticket``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core import events
from app.core.authz import AuthContext, get_auth_context

router = APIRouter()


@router.post("/ticket")
async def create_ticket(ctx: AuthContext = Depends(get_auth_context)):
    from app.core.redis import ensure_redis_available

    await ensure_redis_available()  # ticket vive no Redis: indisponível → 503 claro
    ticket = await events.create_ws_ticket(ctx)
    return {"ticket": ticket, "expires_in": events.WS_TICKET_TTL_S}
