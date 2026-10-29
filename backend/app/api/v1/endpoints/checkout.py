"""
RD Mídia Indoor — Checkout e pedidos (W-CAMP, AÇÃO 002).

``POST /checkout`` (``checkout.create``; header ``Idempotency-Key`` obrigatório): carrinho
com 1..20 campanhas da mesma organização → um pedido, uma cobrança (W-FIN), reserva de
inventário de 2 h (W-INV). O corpo NÃO aceita preço/valor (campo desconhecido ⇒ 422):
o servidor calcula e congela o preço.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import Forbidden, NotFound
from app.models.pricing import Order, OrderItem
from app.modules.campaigns import checkout as CK
from app.modules.campaigns import schemas as SC

router = APIRouter()


@router.post("", status_code=201)
async def create_checkout(body: SC.CheckoutIn, response: Response,
                          idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
                          ctx: AuthContext = Depends(require("checkout.create")),
                          db: AsyncSession = Depends(get_db)):
    res = await CK.checkout(db, ctx, campaign_ids=body.campaign_ids, idempotency_key=idempotency_key,
                            coupon_code=body.coupon_code, method=body.method,
                            payment_source_token=body.payment_source_token, installments=body.installments)
    if res.failed:
        # resposta de erro SEM exceção: a transação confirma o cancelamento e o registro da tentativa
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=502, content={"error": res.response["error"]})
    if res.replayed:
        response.status_code = 200
    return res.response


def _order_filter(ctx: AuthContext):
    return or_(ctx.scope_filter("checkout.create", org_col=Order.org_id),
               ctx.scope_filter("payments.view", org_col=Order.org_id))


@router.get("/orders")
async def list_orders(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                      status: Optional[str] = Query(None, max_length=60),
                      ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    if not (ctx.has_any("checkout.create") or ctx.has_any("payments.view")):
        raise Forbidden(details={"permission": "checkout.create"})
    base = select(Order).where(_order_filter(ctx))
    if status:
        base = base.where(Order.status.in_([s.strip().upper() for s in status.split(",")]))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Order.created_at.desc(), Order.id).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    return {"items": [CK.order_out(o) for o in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/orders/{order_id}")
async def get_order(order_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                    db: AsyncSession = Depends(get_db)):
    o = await db.get(Order, order_id)
    if o is None or not (ctx.can("checkout.create", org_id=o.org_id) or ctx.can("payments.view", org_id=o.org_id)):
        raise NotFound()
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == o.id).order_by(OrderItem.campaign_id))
             ).scalars().all()
    staff = ctx.can("pricing.view", global_only=True)
    out = CK.order_out(o, items=list(items), public=not staff)
    out["payment"] = (o.response or {}).get("payment")
    return out


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: uuid.UUID, ctx: AuthContext = Depends(require("checkout.create")),
                       db: AsyncSession = Depends(get_db)):
    o = await CK.cancel_order(db, ctx, order_id, reason="cancelado pelo cliente")
    return CK.order_out(o)
