"""
RD Mídia Indoor — Faturas do anunciante, comprovante e porta de nota fiscal (P001 §92; ETAPA2 #665–#667).

- Fatura = cobrança (``payment_orders``) vista pelo anunciante (somente a própria organização).
- Comprovante: PDF gerado localmente (reportlab) com hash SHA-256 do conteúdo; só para
  cobranças pagas.
- Nota fiscal: porta ``InvoiceProvider`` SEM fornecedor definido (decisão do
  proprietário). Sem provedor ⇒ pedido registrado como BLOCKED_EXTERNAL_DEPENDENCY;
  nunca simula emissão.
"""
from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict
from app.core.money import format_brl
from app.core.timeutils import isoformat_z, utcnow
from app.models.financial import FiscalDocumentRequest, PaymentOrder


@dataclass(frozen=True)
class InvoiceRequestData:
    payment_order_id: uuid.UUID
    org_id: uuid.UUID
    amount_cents: int
    items: list
    payer_name: str
    payer_document: str


@dataclass(frozen=True)
class InvoiceResult:
    provider: str
    provider_ref: str
    status: str  # ISSUED | FAILED


class InvoiceProvider(Protocol):
    name: str

    async def issue(self, data: InvoiceRequestData) -> InvoiceResult: ...


_provider: Optional[InvoiceProvider] = None


def set_invoice_provider(p: Optional[InvoiceProvider]) -> None:
    """Integração futura de NF-e/NFS-e (fornecedor a definir pelo proprietário)."""
    global _provider
    _provider = p


def get_invoice_provider() -> Optional[InvoiceProvider]:
    return _provider


async def request_fiscal_document(db: AsyncSession, ctx: Any, order: PaymentOrder) -> FiscalDocumentRequest:
    if order.status not in ("PAID", "PARTIALLY_REFUNDED", "REFUNDED"):
        raise Conflict("ORDER_NOT_PAID", "Nota fiscal só para cobranças pagas.")
    existing = (await db.execute(select(FiscalDocumentRequest).where(
        FiscalDocumentRequest.payment_order_id == order.id))).scalar_one_or_none()
    if existing is not None:
        return existing
    prov = get_invoice_provider()
    req = FiscalDocumentRequest(id=uuid.uuid4(), payment_order_id=order.id, org_id=order.org_id,
                                requested_by=ctx.user_id, status="REQUESTED")
    if prov is None:
        req.status = "BLOCKED_EXTERNAL_DEPENDENCY"
        req.detail = "Emissor de nota fiscal não configurado (fornecedor a definir)."
    else:
        from app.models.organizations import Organization

        org = await db.get(Organization, order.org_id)
        res = await prov.issue(InvoiceRequestData(order.id, order.org_id, order.amount_cents, order.items,
                                                  org.name, org.document))
        req.status, req.provider, req.provider_ref = res.status, prov.name, res.provider_ref
    db.add(req)
    await db.flush()
    await audit(db, ctx, "fiscal_document.requested", "payment_order", order.id, org_id=order.org_id,
                after={"status": req.status})
    return req


def receipt_pdf(order: PaymentOrder, org_name: str) -> tuple[bytes, str]:
    """Comprovante de pagamento (PDF) + SHA-256 do conteúdo."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    lines = [
        "RD Mídia Indoor — Comprovante de pagamento",
        f"Cobrança: {order.id}",
        f"Cliente: {org_name}",
        f"Valor: {format_brl(order.amount_cents)}",
        f"Método: {order.method.upper()}",
        f"Pago em: {isoformat_z(order.paid_at) if order.paid_at else '-'}",
        f"Referência CloudPayPag: {order.provider_external_ref or '-'}",
        f"Identificador PIX (E2E): {order.pix_end_to_end_id or '-'}",
        f"Taxa do gateway: {format_brl(order.fee_cents or 0)}",
        "Itens:",
    ] + [f"  - {it.get('description') or it.get('kind')}: {format_brl(int(it['amount_cents']))}" for it in order.items]
    if order.refunded_cents:
        lines.append(f"Reembolsado: {format_brl(order.refunded_cents)}")
    lines.append(f"Emitido em: {isoformat_z(utcnow())}")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Comprovante {order.id}")
    y = 800
    for ln in lines:
        c.drawString(50, y, ln[:110])
        y -= 18
    digest_src = "\n".join(lines[:-1]).encode("utf-8")
    digest = hashlib.sha256(digest_src).hexdigest()
    c.setFont("Helvetica", 7)
    c.drawString(50, y - 10, f"SHA-256 do conteúdo: {digest}")
    c.showPage()
    c.save()
    return buf.getvalue(), digest
