"""
RD Mídia Indoor — Saques e chaves PIX (W-FIN). Prefixo ``/api/v1/financial``.

- Saque do estabelecimento: ``POST /withdrawals`` (``withdrawals.request`` no escopo do
  estabelecimento; TOTP + senha financeira; header ``Idempotency-Key``; SEM dupla aprovação).
  Nunca exposto na dashboard (o frontend usa Financeiro → Saques).
- Chave PIX: só ``pix_keys.manage`` (admin/suporte) cria/troca/revoga; o estabelecimento
  apenas visualiza mascarada. Nova chave bloqueada por ``pix.key_cooldown_hours`` (12 h).
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import NotFound
from app.models.financial import EstablishmentPixKey, Withdrawal
from app.modules.financial import withdrawals as svc

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WithdrawalIn(_Body):
    establishment_id: uuid.UUID
    amount_cents: StrictInt = Field(gt=0)
    totp_code: str = Field(min_length=6, max_length=12)
    financial_password: str = Field(min_length=1, max_length=128)
    email_code: Optional[str] = Field(default=None, max_length=12)  # obrigatório (ausente ⇒ 403 REQUIRED)


class SimulateIn(_Body):
    establishment_id: uuid.UUID
    amount_cents: StrictInt = Field(gt=0)


class PixKeyIn(_Body):
    key_type: Literal["cpf", "cnpj", "email", "phone", "random"]
    key_value: str = Field(min_length=3, max_length=120)
    holder_name: str = Field(min_length=3, max_length=200)
    holder_document: str = Field(min_length=11, max_length=20)
    reason: str = Field(min_length=5, max_length=500)


class RevokeIn(_Body):
    reason: str = Field(min_length=5, max_length=300)


async def _establishment_scope(db: AsyncSession, ctx: AuthContext, establishment_id: uuid.UUID, *perms: str):
    from app.models.establishments import Establishment

    est = await db.get(Establishment, establishment_id)
    if est is None or not any(ctx.can(p, org_id=est.org_id, establishment_id=est.id) for p in perms):
        raise NotFound()
    return est


@router.get("/withdrawals")
async def list_withdrawals(
    establishment_id: Optional[uuid.UUID] = None,
    status: Optional[str] = Query(None, max_length=12),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("withdrawals.view")),
    db: AsyncSession = Depends(get_db),
):
    from app.models.establishments import Establishment

    q = select(Withdrawal)
    if not ctx.can("withdrawals.view", global_only=True):
        orgs = ctx.org_ids("withdrawals.view") or set()
        ests = ctx.establishment_ids("withdrawals.view") or set()
        org_ests = select(Establishment.id).where(Establishment.org_id.in_(list(orgs))) if orgs else None
        conds = []
        if ests:
            conds.append(Withdrawal.establishment_id.in_(list(ests)))
        if org_ests is not None:
            conds.append(Withdrawal.establishment_id.in_(org_ests))
        if not conds:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        q = q.where(Withdrawal.kind == "ESTABLISHMENT", or_(*conds))
    elif not ctx.can("finance.view", global_only=True):
        q = q.where(Withdrawal.kind == "ESTABLISHMENT")
    if establishment_id is not None:
        q = q.where(Withdrawal.establishment_id == establishment_id)
    if status:
        q = q.where(Withdrawal.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(Withdrawal.requested_at.desc()).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    return {"items": [svc.withdrawal_public(w) for w in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/withdrawals/{withdrawal_id}")
async def get_withdrawal(withdrawal_id: uuid.UUID, ctx: AuthContext = Depends(require("withdrawals.view")),
                         db: AsyncSession = Depends(get_db)):
    w = await db.get(Withdrawal, withdrawal_id)
    if w is None:
        raise NotFound()
    if w.establishment_id is not None:
        await _establishment_scope(db, ctx, w.establishment_id, "withdrawals.view")
    elif not ctx.can("finance.view", global_only=True):
        raise NotFound()
    return svc.withdrawal_public(w)


@router.post("/withdrawals/simulate")
async def simulate_withdrawal(body: SimulateIn, ctx: AuthContext = Depends(require("withdrawals.request")),
                              db: AsyncSession = Depends(get_db)):
    """Taxa e valor líquido via ``POST /withdrawals/simulate`` da CloudPayPag (somente leitura)."""
    return await svc.simulate(db, ctx, body.establishment_id, body.amount_cents)


@router.post("/withdrawals/email-code", status_code=202)
async def withdrawal_email_code(body: SimulateIn,
                                ctx: AuthContext = Depends(require("withdrawals.request", allow_impersonation=False)),
                                db: AsyncSession = Depends(get_db)):
    """Envia ao e-mail do solicitante o código do saque (vinculado ao estabelecimento e ao valor; 10 min; uso único)."""
    return await svc.send_withdrawal_code(db, ctx, body.establishment_id, body.amount_cents)


@router.post("/withdrawals", status_code=201)
async def request_withdrawal(
    body: WithdrawalIn,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", max_length=120),
    ctx: AuthContext = Depends(require("withdrawals.request", allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    w = await svc.request_withdrawal(db, ctx, body.establishment_id, body.amount_cents, totp_code=body.totp_code,
                                     financial_password=body.financial_password, idempotency_key=idempotency_key,
                                     email_code=body.email_code)
    return svc.withdrawal_public(w)


@router.get("/establishments/{establishment_id}/pix-keys")
async def list_pix_keys(establishment_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                        db: AsyncSession = Depends(get_db)):
    await _establishment_scope(db, ctx, establishment_id, "withdrawals.view", "pix_keys.manage")
    rows = (await db.execute(select(EstablishmentPixKey).where(EstablishmentPixKey.establishment_id == establishment_id)
                             .order_by(EstablishmentPixKey.created_at.desc()))).scalars().all()
    return {"items": [svc.pix_key_public(k) for k in rows], "total": len(rows), "page": 1, "page_size": len(rows)}


@router.post("/establishments/{establishment_id}/pix-keys", status_code=201)
async def set_pix_key(establishment_id: uuid.UUID, body: PixKeyIn,
                      ctx: AuthContext = Depends(require("pix_keys.manage", allow_impersonation=False)),
                      db: AsyncSession = Depends(get_db)):
    k = await svc.set_pix_key(db, ctx, establishment_id, key_type=body.key_type, key_value=body.key_value,
                              holder_name=body.holder_name, holder_document=body.holder_document, reason=body.reason)
    return svc.pix_key_public(k)


@router.post("/pix-keys/{key_id}/revoke")
async def revoke_pix_key(key_id: uuid.UUID, body: RevokeIn,
                         ctx: AuthContext = Depends(require("pix_keys.manage", allow_impersonation=False)),
                         db: AsyncSession = Depends(get_db)):
    k = await svc.revoke_pix_key(db, ctx, key_id, body.reason)
    return svc.pix_key_public(k)


# ─── Devolução de caução do parceiro (dupla aprovação) ───────────────────────

class DepositRefundIn(_Body):
    refund_cents: StrictInt = Field(ge=0)
    retained_cents: StrictInt = Field(default=0, ge=0)
    damage_description: Optional[str] = Field(default=None, max_length=300)
    reason: str = Field(min_length=10, max_length=2000)


@router.get("/establishments/{establishment_id}/deposit")
async def deposit_status(establishment_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context),
                         db: AsyncSession = Depends(get_db)):
    from app.modules.financial import approvals, ledger

    await _establishment_scope(db, ctx, establishment_id, "finance.view", "withdrawals.view", "commissions.view")
    return {"establishment_id": str(establishment_id),
            "held_cents": await ledger.balance_of(db, "EST:DepositHeld", establishment_id),
            "available_for_refund_cents": await approvals.deposit_available(db, establishment_id)}


@router.post("/establishments/{establishment_id}/deposit-refunds", status_code=202)
async def request_deposit_refund(
    establishment_id: uuid.UUID,
    body: DepositRefundIn,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", max_length=120),
    ctx: AuthContext = Depends(require("finance.approve", global_only=True, allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    """Admin Master A solicita a devolução (parcial permitida: retenção por dano vira receita de
    serviço). O outro Admin Master aprova (TOTP + senha financeira + código por e-mail); o PIX OUT vai
    para a chave ATIVA do estabelecimento, sem retry automático."""
    from app.modules.financial import approvals

    payload = {"establishment_id": str(establishment_id), "refund_cents": body.refund_cents,
               "retained_cents": body.retained_cents, "damage_description": body.damage_description}
    r = await approvals.create_request(db, ctx, "DEPOSIT_REFUND", payload, body.reason, idempotency_key=idempotency_key)
    return await approvals.request_public_named(db, r, viewer_id=ctx.user_id)
