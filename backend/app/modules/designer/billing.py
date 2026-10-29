"""Cobrança dos serviços de design (criação de arte e revisão extra) — adaptador fino.

A cobrança é SEMPRE pelo financeiro (W-FIN → CloudPayPag). Este módulo só traduz a
chamada; se o serviço financeiro não estiver disponível, a operação falha com 503
(nunca registra pagamento falso). A confirmação chega pelos callbacks
``designer.service.on_payment_confirmed`` / ``on_extra_revision_paid`` registrados no
financeiro para as referências ``design_request`` e ``design_extra_revision``.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.timeutils import utcnow

PAYMENT_TTL = timedelta(days=3)


async def create_charge(db: AsyncSession, ctx: Any, *, org_id: uuid.UUID, amount_cents: int, reference_type: str,
                        reference_id: uuid.UUID, description: str) -> Optional[uuid.UUID]:
    try:
        from app.modules.financial import service as fin
    except ImportError:
        fin = None
    create = getattr(fin, "create_payment_order", None) if fin is not None else None
    if create is None:
        raise DomainError("PAYMENT_UNAVAILABLE",
                          "Cobrança indisponível no momento (serviço financeiro não configurado).", status=503)
    order = await create(db, ctx, org_id, [{"kind": "SERVICE", "description": description,
                                            "amount_cents": amount_cents, "refundable": False}],
                         amount_cents, utcnow() + PAYMENT_TTL, {"type": reference_type, "id": str(reference_id)},
                         idempotency_key=f"{reference_type}:{reference_id}", description=description)
    status = getattr(order, "status", None)
    if status in ("CREATION_FAILED", "FAILED"):
        raise DomainError("PAYMENT_CREATION_FAILED", "Não foi possível gerar a cobrança. Tente novamente.", status=502,
                          details={"payment_order_id": str(order.id)})
    return getattr(order, "id", None)


def register_callbacks() -> None:
    """Registra no financeiro os callbacks de pagamento confirmado (idempotente)."""
    try:
        from app.modules.financial.payments import register_paid_callback
    except ImportError:  # pragma: no cover - financeiro ausente
        return

    async def _design_paid(db, order):  # noqa: ANN001
        from app.modules.designer.service import on_payment_confirmed

        await on_payment_confirmed(db, uuid.UUID(order.reference_id), order.id)

    async def _extra_paid(db, order):  # noqa: ANN001
        from app.modules.designer.service import on_extra_revision_paid

        await on_extra_revision_paid(db, uuid.UUID(order.reference_id), order.id)

    register_paid_callback("design_request", "w-biz.design_request_paid", _design_paid)
    register_paid_callback("design_extra_revision", "w-biz.design_extra_revision_paid", _extra_paid)
