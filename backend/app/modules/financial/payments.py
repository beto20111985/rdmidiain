"""
RD Mídia Indoor — Cobranças CloudPayPag e processamento de webhooks (ADR_009 itens 6–7; A5 D5–D6).

Fluxo:
1. ``create_payment_order`` (W-CAMP no checkout): preço JÁ calculado no servidor
   (itens congelados); grava a cobrança e chama ``POST /charges`` com
   ``order_id = payment_orders.id``. Erro 4xx ⇒ CREATION_FAILED; timeout/5xx ⇒
   CREATION_UNKNOWN (+ item de conciliação; nunca repetir o POST).
2. ``ingest_webhook`` (rota pública): exige ``X-Webhook-Signature`` válido
   (algoritmo da doc), confere subconta, grava corpo bruto + headers com
   ``INSERT … ON CONFLICT DO NOTHING`` por (provider, instância, webhook_event_id) e
   responde em < 5 s; o processamento acontece no worker.
3. ``process_webhook_event`` (worker): casa por ``external_ref``/``order_id``, confere
   valor/moeda (divergência ⇒ DIVERGENT + conciliação, sem efeito), aplica a máquina
   de estados monotônica e, no ``paid``: ledger (receita diferida + taxa REAL ``fee``),
   cronogramas de receita, termos de comissão, comissão de vendedor e callbacks
   (``campaign_checkout`` → ``campaigns.checkout.on_order_paid``) — cada callback em
   SAVEPOINT, com registro e reprocessamento (falha ⇒ conciliação + alerta).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.core.money import ensure_cents
from app.core.timeutils import utcnow
from app.models.financial import (
    PaymentOrder,
    PaymentOrderCallback,
    PaymentOrderStatus as POS,
    RevenueSchedule,
    WebhookEvent,
    WebhookEventStatus as WES,
)
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import common, ledger

log = structlog.get_logger("rd.financial.payments")

PROVIDER = "cloudpaypag"
MAX_WEBHOOK_BYTES = 64 * 1024
REF_CAMPAIGN_CHECKOUT = "campaign_checkout"
PENDING_LIKE = (POS.CREATED.value, POS.PENDING.value, POS.CREATION_UNKNOWN.value)
PAID_LIKE = (POS.PAID.value, POS.PARTIALLY_REFUNDED.value, POS.REFUNDED.value)
TERMINAL_UNPAID = (POS.FAILED.value, POS.CANCELLED.value, POS.EXPIRED.value, POS.CREATION_FAILED.value)
_STATUS_RANK = {POS.PAID.value: 1, POS.PARTIALLY_REFUNDED.value: 2, POS.REFUNDED.value: 3}


# ─── Itens ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderItem:
    kind: str  # MEDIA | SERVICE
    amount_cents: int
    description: str
    campaign_id: Optional[uuid.UUID]
    service_start: Optional[date]
    service_end: Optional[date]
    refundable: bool
    service_kind: str = "GENERIC"  # SERVICE: GENERIC | MONTHLY_FEE | DEPOSIT | INSTALLATION_FEE | UPGRADE_FEE

    def as_json(self) -> dict:
        return {
            "kind": self.kind, "amount_cents": self.amount_cents, "description": self.description,
            "campaign_id": str(self.campaign_id) if self.campaign_id else None,
            "service_start": self.service_start.isoformat() if self.service_start else None,
            "service_end": self.service_end.isoformat() if self.service_end else None,
            "refundable": self.refundable, "service_kind": self.service_kind,
        }


SERVICE_KINDS = ("GENERIC", "MONTHLY_FEE", "DEPOSIT", "INSTALLATION_FEE", "UPGRADE_FEE")


def _as_date(v: Any) -> Optional[date]:
    if v is None or isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        raise ValueError("use date (dia local), não datetime")
    return date.fromisoformat(str(v))


def normalize_items(items: Sequence[Any], amount_cents: int) -> tuple[list[OrderItem], int, int]:
    """Valida itens congelados. ``DISCOUNT`` (≥ 0) é abatido da MÍDIA (da campanha indicada ou
    rateado por valor, piso + maior resto, determinístico) ⇒ receita elegível = mídia líquida.
    Σ(MEDIA + SERVICE) − Σ(DISCOUNT) deve ser igual ao valor da cobrança."""
    ensure_cents(amount_cents)
    if amount_cents <= 0:
        raise DomainError("PAYMENT_AMOUNT_INVALID", "Valor da cobrança deve ser maior que zero.")
    if not items:
        raise DomainError("PAYMENT_ITEMS_REQUIRED", "A cobrança precisa de itens.")
    out: list[OrderItem] = []
    discounts: list[tuple[Optional[uuid.UUID], int]] = []
    for raw in items:
        g = raw.get if isinstance(raw, Mapping) else (lambda k, d=None, _r=raw: getattr(_r, k, d))
        kind = str(g("kind", "MEDIA")).upper()
        if kind not in ("MEDIA", "SERVICE", "DISCOUNT"):
            raise DomainError("PAYMENT_ITEM_INVALID", "Tipo de item inválido.", details={"kind": kind})
        amt = g("amount_cents")
        ensure_cents(amt)
        cid = g("campaign_id")
        if kind == "DISCOUNT":
            discounts.append((common.as_uuid(cid) if cid else None, amt))
            continue
        start, end = _as_date(g("service_start")), _as_date(g("service_end"))
        if kind == "MEDIA":
            if cid is None or start is None or end is None or end < start:
                raise DomainError("PAYMENT_ITEM_INVALID", "Item de mídia exige campanha e período (início ≤ fim).")
        skind = str(g("service_kind", "GENERIC") or "GENERIC").upper()
        if skind not in SERVICE_KINDS or (kind == "MEDIA" and skind != "GENERIC"):
            raise DomainError("PAYMENT_ITEM_INVALID", "Tipo de serviço inválido.", details={"service_kind": skind})
        out.append(OrderItem(kind, amt, str(g("description", "") or "")[:200], common.as_uuid(cid) if cid else None,
                             start, end, bool(g("refundable", kind == "MEDIA" or skind == "DEPOSIT")), skind))
    total_discount = sum(a for _, a in discounts)
    if sum(i.amount_cents for i in out) - total_discount != amount_cents:
        raise DomainError("PAYMENT_ITEMS_MISMATCH", "Soma dos itens difere do valor da cobrança.")
    if total_discount:
        media_idx = [k for k, i in enumerate(out) if i.kind == "MEDIA"]
        net = {k: out[k].amount_cents for k in media_idx}
        for cid, amt in discounts:
            targets = [k for k in media_idx if cid is None or out[k].campaign_id == cid] or media_idx
            base = sum(net[k] for k in targets)
            if base < amt:
                raise DomainError("PAYMENT_DISCOUNT_INVALID", "Desconto maior que o valor de mídia.")
            shares = {k: amt * net[k] // base for k in targets} if base else {k: 0 for k in targets}
            rest = amt - sum(shares.values())
            for k in sorted(targets, key=lambda k: (-(amt * net[k] % base) if base else 0, k))[:rest]:
                shares[k] += 1
            for k in targets:
                net[k] -= shares[k]
        out = [OrderItem(i.kind, net.get(k, i.amount_cents), i.description, i.campaign_id, i.service_start,
                         i.service_end, i.refundable, i.service_kind) for k, i in enumerate(out)]
    eligible = sum(i.amount_cents for i in out if i.kind == "MEDIA")
    return out, eligible, amount_cents - eligible


def _reference(reference: Any) -> tuple[str, str, dict]:
    if isinstance(reference, (tuple, list)) and len(reference) == 2:
        rtype, rid = reference
        extra: dict = {}
    elif isinstance(reference, Mapping):
        rtype, rid = reference.get("type"), reference.get("id")
        extra = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in reference.items() if k not in ("type", "id")}
    else:
        raise DomainError("PAYMENT_REFERENCE_INVALID", "Referência da cobrança inválida.")
    if not rtype or not rid:
        raise DomainError("PAYMENT_REFERENCE_INVALID", "Referência da cobrança inválida.")
    return str(rtype)[:40], str(rid)[:64], extra


_DIGITS = re.compile(r"\D")


_PHONE_ALLOWED = re.compile(r"^[0-9\s()+\-.]*$")
PAYER_DOC_NOT_SUPPORTED = "PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY"
PAYER_MIN_PHONE_DIGITS = 10  # DDD + número (sem DDI)


def payer_phone_digits(raw: Optional[str]) -> str:
    """Telefone do pagador como o gateway recebe: só dígitos nacionais (DDD + número), sem DDI 55.
    Caractere fora de ``0-9 ()+-.`` e espaço ⇒ "" (inválido; nunca "limpa" lixo)."""
    raw_phone = (raw or "").strip()
    phone = _DIGITS.sub("", raw_phone) if _PHONE_ALLOWED.match(raw_phone) else ""
    if phone.startswith("55") and len(phone) > 11:
        phone = phone[2:]
    return phone


def payer_missing(email: Optional[str], phone: Optional[str], document: Optional[str]) -> list[str]:
    """FONTE ÚNICA dos dados exigidos do pagador: e-mail, telefone com DDD (≥ 10 dígitos) e CPF/CNPJ.
    Usada no pagamento (``payer_from_org``) e no CADASTRO de quem compra (organizações ADVERTISER e
    auto-cadastro do anunciante — AÇÃO 004). Retorna os campos faltantes, na ordem e-mail, telefone, documento."""
    ph = payer_phone_digits(phone)
    return [k for k, v in {"email": email, "phone": ph if len(ph) >= PAYER_MIN_PHONE_DIGITS else None,
                           "document": document}.items() if not v]


def gateway_payer_document(document: Optional[str], document_type: Optional[str]) -> tuple[str, str]:
    """Documento do pagador no formato da CloudPayPag (doc pública, snapshot L155–L156/L657:
    ``"CPF"`` ou ``"CNPJ"``; "Número sem máscara. CPF=11 dígitos, CNPJ=14 dígitos").

    Remove SOMENTE a máscara (``. - /`` e espaços) — nunca descarta letras/caracteres:
    - CNPJ ALFANUMÉRICO (IN RFB 2.229/2024; válido no cadastro do W-ORG) ⇒ 422
      ``PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY`` (a doc só aceita dígitos; enviar um
      documento adulterado é proibido). BLOQUEADO POR DEPENDÊNCIA EXTERNA até a
      CloudPayPag confirmar o suporte.
    - Qualquer outro caractere/tamanho ⇒ 422 ``PAYER_DOCUMENT_INVALID``.
    """
    from app.modules.organizations.documents import strip_mask

    d = strip_mask(document)
    t = (document_type or "").strip().upper()
    if not d or t not in ("CPF", "CNPJ"):
        raise DomainError("PAYER_DATA_INCOMPLETE", "Complete o cadastro (CPF/CNPJ) para pagar.", status=422,
                          details={"missing": ["document"]})
    if t == "CNPJ" and len(d) == 14 and d[:12].isalnum() and d[12:].isdigit() and not d.isdigit() and d.isascii():
        raise DomainError(
            PAYER_DOC_NOT_SUPPORTED,
            "O meio de pagamento (CloudPayPag) ainda não aceita CNPJ alfanumérico. Seu cadastro está correto; "
            "entre em contato com o suporte da RD para concluir o pagamento.",
            status=422,
            details={"document_type": "CNPJ", "format": "ALPHANUMERIC",
                     "dependency": "CloudPayPag: documentação pública aceita somente CNPJ com 14 dígitos",
                     "status": "BLOQUEADO POR DEPENDÊNCIA EXTERNA"},
        )
    expected = 11 if t == "CPF" else 14
    if len(d) != expected or not d.isdigit() or not d.isascii():
        raise DomainError("PAYER_DOCUMENT_INVALID",
                          f"{t} do cadastro inválido para pagamento ({expected} dígitos). Corrija o cadastro.",
                          status=422, details={"document_type": t})
    return d, t


def _org_member_user(ctx: Any, org_id: uuid.UUID) -> Any:
    """Usuário do contexto SÓ se for MEMBRO da organização pagadora (atribuição com escopo ORGANIZATION dela —
    o anunciante, ou a agência com acesso ao cliente). Equipe RD (GLOBAL/CIDADE) criando a cobrança em nome do
    cliente NÃO empresta e-mail/telefone ao pagador (AÇÃO 004: a cobrança saía com o e-mail do admin)."""
    from app.core.permissions import SCOPE_ORGANIZATION

    user = getattr(ctx, "user", None)
    if user is None:
        return None
    for g in getattr(ctx, "grants", None) or ():
        if getattr(g, "scope_type", None) == SCOPE_ORGANIZATION and str(getattr(g, "scope_id", "")) == str(org_id):
            return user
    return None


async def payer_from_org(db: AsyncSession, org_id: uuid.UUID, ctx: Any = None) -> cpp.Customer:
    """Dados do pagador (doc: enviar sempre). Problema no cadastro ⇒ 422 ANTES de qualquer gravação/chamada."""
    from app.models.organizations import Organization

    org = await db.get(Organization, org_id)
    if org is None:
        raise NotFound()
    user = _org_member_user(ctx, org.id)
    email = org.email or getattr(user, "email", None)
    raw_phone = org.phone or getattr(user, "phone", None)
    phone = payer_phone_digits(raw_phone)
    missing = payer_missing(email, raw_phone, org.document)
    if missing:
        raise DomainError("PAYER_DATA_INCOMPLETE", "Complete o cadastro (e-mail, telefone e CPF/CNPJ) para pagar.",
                          status=422, details={"missing": missing})
    doc, dtype = gateway_payer_document(org.document, org.document_type)
    return cpp.Customer(name=org.name, email=email, phone=phone, document_type=dtype, document_number=doc)


async def ensure_payer_supported(db: AsyncSession, org_id: Any, ctx: Any = None) -> None:
    """Pré-checagem para o checkout (W-CAMP): chame ANTES de criar hold/pedido. Mesmos erros 422
    de ``create_payment_order`` (``PAYER_DATA_INCOMPLETE``, ``PAYER_DOCUMENT_INVALID``,
    ``PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY``); não grava nada."""
    await payer_from_org(db, common.as_uuid(org_id), ctx)


# ─── Criação da cobrança ─────────────────────────────────────────────────────

async def create_payment_order(
    db: AsyncSession,
    ctx: Any,
    org_id: Any,
    items: Sequence[Any],
    amount_cents: int,
    expires_at: Optional[datetime],
    reference: Any,
    *,
    method: str = "pix",
    payment_source_token: Optional[str] = None,
    installments: int = 1,
    idempotency_key: Optional[str] = None,
    customer: Optional[cpp.Customer] = None,
    seller_user_id: Any = None,
    description: Optional[str] = None,
) -> PaymentOrder:
    """Cria a cobrança (PIX copia-e-cola ou cartão). Sem commit próprio (sessão do chamador).

    Não lança em falha do provedor depois de gravar: devolve a cobrança com status
    ``CREATION_FAILED``/``CREATION_UNKNOWN`` para o chamador decidir (não faça rollback
    de ``CREATION_UNKNOWN``: a cobrança pode existir e será conciliada pelo webhook).
    ``payment_source_token`` (cartão) nunca é logado nem persistido.
    """
    org_uuid = common.as_uuid(org_id)
    if method not in ("pix", "credit_card"):
        raise cpp.capability_unavailable(f"charge_{method}")
    norm, eligible, services = normalize_items(items, amount_cents)
    rtype, rid, extra = _reference(reference)
    if idempotency_key:
        prev = (await db.execute(select(PaymentOrder).where(PaymentOrder.idempotency_key == idempotency_key))).scalar_one_or_none()
        if prev is not None:
            if prev.amount_cents != amount_cents or prev.reference_id != rid or prev.org_id != org_uuid:
                raise Conflict("IDEMPOTENCY_KEY_REUSED", "Chave de idempotência já usada com outro conteúdo.")
            return prev
    payer = customer or await payer_from_org(db, org_uuid, ctx)  # falha cedo (422), nada gravado
    cfg = cpp.get_config()
    order = PaymentOrder(
        id=uuid.uuid4(), org_id=org_uuid, reference_type=rtype, reference_id=rid, reference=extra,
        items=[i.as_json() for i in norm], amount_cents=amount_cents, eligible_revenue_cents=eligible,
        services_cents=services, method=method, installments=installments, status=POS.CREATED.value,
        instance_slug=cfg.instance_slug, idempotency_key=idempotency_key, expires_at=expires_at,
        seller_user_id=common.as_uuid(seller_user_id) if seller_user_id else None,
        created_by=getattr(ctx, "user_id", None),
    )
    db.add(order)
    await db.flush()
    provider = cpp.get_provider()
    try:
        res = await provider.create_charge(
            order_id=str(order.id), amount_cents=amount_cents, method=method, customer=payer,
            description=(description or f"RD Mídia Indoor — pedido {rid[:12]}"),
            payment_source_token=payment_source_token, installments=installments,
        )
    except (cpp.ProviderTimeout, cpp.ProviderTemporaryError, cpp.ProviderUnavailable, cpp.ProviderProtocolError) as exc:
        order.status = POS.CREATION_UNKNOWN.value
        order.provider_error = exc.code
        from app.modules.financial import reconciliation

        await reconciliation.open_item(db, "CHARGE_CREATION_UNKNOWN", f"order:{order.id}", internal_ref=str(order.id),
                                       internal_amount_cents=amount_cents, detail={"error": exc.code})
    except cpp.ProviderError as exc:
        order.status = POS.CREATION_FAILED.value
        order.provider_error = exc.code
    else:
        if res.amount_cents != amount_cents:
            order.status = POS.CREATION_UNKNOWN.value
            order.provider_error = "AMOUNT_MISMATCH_ON_CREATE"
        else:
            order.status = POS.FAILED.value if res.status == "failed" else POS.PENDING.value
        order.provider_transaction_id = res.transaction_id
        order.provider_external_ref = res.external_ref
        order.provider_processor_ref = res.processor_reference_id
        order.provider_status = res.status
        order.pix_qr_code = res.qr_code
    await audit(db, ctx, "payment_order.created", "payment_order", order.id, org_id=org_uuid,
                after={"amount_cents": amount_cents, "status": order.status, "method": method,
                       "reference": f"{rtype}:{rid}", "provider_mode": cfg.mode})
    await db.flush()
    common.publish_after_commit(db, "payment.updated", payment_event(order), org_id=org_uuid)
    return order


PARTNER_CHARGE_KINDS = ("MONTHLY_FEE", "DEPOSIT", "INSTALLATION_FEE", "UPGRADE_FEE")
REF_PARTNER_CHARGE = "partner_charge"


async def create_partner_charge(
    db: AsyncSession,
    ctx: Any,
    establishment_id: Any,
    kind: str,
    amount_cents: int,
    description: str,
    reference: Any = None,
    *,
    idempotency_key: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    method: str = "pix",
) -> PaymentOrder:
    """Cobrança ao PARCEIRO (ETAPA2 #1417–#1424; P001 §23): mensalidade, caução, instalação, upgrade.

    100 % pela CloudPayPag via ``create_payment_order`` com item SERVICE (fora da receita
    elegível de comissão). No ``paid``: caução ⇒ passivo reembolsável ``EST:DepositHeld``
    (NUNCA receita); mensalidade ⇒ ``RD:Revenue:PartnerFees``; instalação/upgrade ⇒
    ``RD:Revenue:Services``. ``reference``: origem no W-ORG, ex. ``{"type": "partner_plan" |
    "comodato_agreement", "id": <uuid>}``. Pagador = organização dona do estabelecimento.
    """
    from app.models.establishments import Establishment

    if kind not in PARTNER_CHARGE_KINDS:
        raise DomainError("PARTNER_CHARGE_KIND_INVALID", "Tipo de cobrança ao parceiro inválido.",
                          details={"allowed": list(PARTNER_CHARGE_KINDS)})
    est = await db.get(Establishment, common.as_uuid(establishment_id))
    if est is None or getattr(est, "deleted_at", None) is not None:
        raise NotFound()
    if not description or len(description.strip()) < 3:
        raise DomainError("DESCRIPTION_REQUIRED", "Descreva a cobrança.")
    src_type, src_id = None, None
    if isinstance(reference, Mapping):
        src_type, src_id = reference.get("type"), reference.get("id")
    elif isinstance(reference, (tuple, list)) and len(reference) == 2:
        src_type, src_id = reference
    ref = {"type": REF_PARTNER_CHARGE, "id": str(src_id or uuid.uuid4()), "establishment_id": str(est.id),
           "charge_kind": kind, "source_type": (str(src_type)[:40] if src_type else None)}
    items = [{"kind": "SERVICE", "service_kind": kind, "description": description.strip()[:200],
              "amount_cents": amount_cents, "refundable": kind == "DEPOSIT"}]
    return await create_payment_order(db, ctx, est.org_id, items, amount_cents, expires_at, ref,
                                      method=method, idempotency_key=idempotency_key,
                                      description=f"RD Mídia Indoor — {description.strip()[:120]}")


async def cancel_payment_order(db: AsyncSession, ctx: Any, payment_order_id: Any, reason: str) -> PaymentOrder:
    """Cancela LOCALMENTE uma cobrança ainda não paga (W-CAMP ``cancel_order``).

    A API pública NÃO tem cancelamento de cobrança (dependência externa): o QR pode
    continuar pagável na CloudPayPag; se o ``paid`` chegar depois, o pagamento é
    contabilizado e vira item de conciliação ``PAID_AFTER_EXPIRY`` (estorno manual).
    """
    order = (await db.execute(
        select(PaymentOrder).where(PaymentOrder.id == common.as_uuid(payment_order_id)).with_for_update()
    )).scalar_one_or_none()
    if order is None:
        raise NotFound()
    if order.status in TERMINAL_UNPAID:
        return order
    if order.status not in PENDING_LIKE:
        raise Conflict("PAYMENT_ORDER_NOT_CANCELLABLE", "Cobrança já paga: use o fluxo de reembolso.",
                       details={"status": order.status})
    before = order.status
    order.status = POS.CANCELLED.value
    await audit(db, ctx, "payment_order.cancelled", "payment_order", order.id, org_id=order.org_id, reason=reason,
                before={"status": before}, after={"status": order.status,
                                                  "provider_cancel": "BLOQUEADO POR DEPENDÊNCIA EXTERNA"})
    common.publish_after_commit(db, "payment.updated", payment_event(order), org_id=order.org_id)
    return order


def payment_event(o: PaymentOrder) -> dict:
    return {"id": str(o.id), "status": o.status, "amount_cents": o.amount_cents,
            "reference_type": o.reference_type, "reference_id": o.reference_id}


def order_public(o: PaymentOrder, *, include_qr: bool = True) -> dict:
    return {
        "id": str(o.id), "org_id": str(o.org_id), "reference_type": o.reference_type, "reference_id": o.reference_id,
        "items": o.items, "amount_cents": o.amount_cents, "eligible_revenue_cents": o.eligible_revenue_cents,
        "services_cents": o.services_cents, "currency": o.currency, "method": o.method,
        "installments": o.installments, "status": o.status, "dispute_status": o.dispute_status,
        "provider": o.provider, "provider_external_ref": o.provider_external_ref,
        "provider_transaction_id": o.provider_transaction_id, "pix_qr_code": o.pix_qr_code if include_qr else None,
        "pix_end_to_end_id": o.pix_end_to_end_id, "paid_amount_cents": o.paid_amount_cents,
        "fee_cents": o.fee_cents, "net_cents": o.net_cents, "refunded_cents": o.refunded_cents,
        "expires_at": o.expires_at, "paid_at": o.paid_at, "created_at": o.created_at,
        "provider_mode_fake": cpp.get_config().mode == "fake",
    }


async def update_service_period(db: AsyncSession, payment_order_id: Any, campaign_id: Any, start: date, end: date) -> None:
    """W-CAMP: campanha reagendada → cronograma de receita acompanha (só antes de concluir)."""
    if end < start:
        raise DomainError("SERVICE_PERIOD_INVALID", "Período inválido.")
    sch = (await db.execute(
        select(RevenueSchedule).where(RevenueSchedule.payment_order_id == common.as_uuid(payment_order_id),
                                      RevenueSchedule.campaign_id == common.as_uuid(campaign_id)).with_for_update()
    )).scalar_one_or_none()
    if sch is None:
        raise NotFound()
    if sch.status != "ACTIVE":
        raise Conflict("REVENUE_SCHEDULE_CLOSED", "Cronograma de receita encerrado.")
    sch.start_date, sch.end_date = start, end


# ─── Webhook: ingestão (rota pública, < 5 s) ─────────────────────────────────

@dataclass
class IngestResult:
    status_code: int
    body: dict
    event_id: Optional[uuid.UUID] = None


def _safe_headers(headers: Mapping[str, str]) -> dict:
    keep = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in ("authorization", "cookie"):
            continue
        keep[kl] = str(v)[:500]
    return keep


async def ingest_webhook(db: AsyncSession, raw_body: bytes, headers: Mapping[str, str],
                         instance_slug: Optional[str] = None) -> IngestResult:
    """Autentica e persiste o webhook. Nada de efeito financeiro aqui (worker processa)."""
    if len(raw_body) > MAX_WEBHOOK_BYTES:
        return IngestResult(413, {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Webhook muito grande.", "details": {}}})
    cfg = cpp.get_config(instance_slug)
    if not cfg.webhook_secret:
        log.error("webhook_secret_not_configured", instance=cfg.instance_slug)
        return IngestResult(503, {"error": {"code": "WEBHOOK_SECRET_NOT_CONFIGURED",
                                            "message": "Webhook sem segredo configurado.", "details": {}}})
    sig = None
    for k, v in headers.items():
        if k.lower() == cpp.SIGNATURE_HEADER.lower():
            sig = v
            break
    if not sig:
        return IngestResult(401, {"error": {"code": "WEBHOOK_SIGNATURE_MISSING",
                                            "message": "Assinatura do webhook ausente.", "details": {}}})
    if cpp.verify_signature(raw_body, sig, cfg.webhook_secret) is None:
        log.warning("webhook_signature_invalid", instance=cfg.instance_slug)
        return IngestResult(401, {"error": {"code": "WEBHOOK_SIGNATURE_INVALID",
                                            "message": "Assinatura do webhook inválida.", "details": {}}})
    try:
        wp = cpp.parse_webhook(raw_body)
    except cpp.ProviderError as exc:
        return IngestResult(400, {"error": {"code": exc.code, "message": exc.message, "details": {}}})
    subconta_ok = cfg.subconta_id is None or wp.subconta_id == cfg.subconta_id
    status = WES.RECEIVED.value if subconta_ok else WES.REJECTED.value
    tbl = WebhookEvent.__table__
    ins = (
        pg_insert(tbl)
        .values(id=uuid.uuid4(), provider=PROVIDER, instance_slug=cfg.instance_slug,
                webhook_event_id=wp.webhook_event_id, raw_body=raw_body.decode("utf-8", "replace"),
                headers=_safe_headers(headers), signature_valid=True, external_ref=wp.external_ref,
                order_ref=wp.order_id, provider_status=wp.status, subconta_id=wp.subconta_id, status=status,
                last_error=None if subconta_ok else "subconta divergente da instância")
        .on_conflict_do_nothing(constraint="uq_webhook_events_provider_event")
        .returning(tbl.c.id)
    )
    row = (await db.execute(ins)).first()
    if not subconta_ok:
        log.warning("webhook_subconta_mismatch", instance=cfg.instance_slug)
        return IngestResult(403, {"error": {"code": "WEBHOOK_SUBCONTA_MISMATCH",
                                            "message": "Webhook de outra subconta.", "details": {}}})
    if row is None:
        return IngestResult(200, {"ok": True, "duplicate": True})
    event_id = row[0]
    common.dispatch_after_commit(db, "app.workers.tasks.financial.process_webhook_event", event_id)
    return IngestResult(200, {"ok": True}, event_id)


# ─── Webhook: processamento (worker) ─────────────────────────────────────────

PaymentCallback = Callable[[AsyncSession, PaymentOrder], Awaitable[None]]
_PAID_CALLBACKS: dict[str, list[tuple[str, PaymentCallback]]] = {}
_EXPIRED_CALLBACKS: dict[str, list[tuple[str, PaymentCallback]]] = {}


def register_paid_callback(reference_type: str, name: str, fn: PaymentCallback) -> None:
    lst = _PAID_CALLBACKS.setdefault(reference_type, [])
    lst[:] = [(n, f) for n, f in lst if n != name] + [(name, fn)]


def register_expired_callback(reference_type: str, name: str, fn: PaymentCallback) -> None:
    lst = _EXPIRED_CALLBACKS.setdefault(reference_type, [])
    lst[:] = [(n, f) for n, f in lst if n != name] + [(name, fn)]


async def _campaign_order_paid(db: AsyncSession, order: PaymentOrder) -> None:
    from app.modules.campaigns import checkout  # W-CAMP (converte holds + on_payment_confirmed; idempotente)

    await checkout.on_order_paid(db, uuid.UUID(order.reference_id), order.id)


async def _campaign_order_expired(db: AsyncSession, order: PaymentOrder) -> None:
    from app.modules.campaigns import checkout

    await checkout.on_order_expired(db, uuid.UUID(order.reference_id))


register_paid_callback(REF_CAMPAIGN_CHECKOUT, "campaigns.checkout.on_order_paid", _campaign_order_paid)
register_expired_callback(REF_CAMPAIGN_CHECKOUT, "campaigns.checkout.on_order_expired", _campaign_order_expired)


async def _run_callbacks(db: AsyncSession, order: PaymentOrder, callbacks: list[tuple[str, PaymentCallback]]) -> list[str]:
    failed: list[str] = []
    for name, fn in callbacks:
        cb = (await db.execute(
            select(PaymentOrderCallback).where(PaymentOrderCallback.payment_order_id == order.id,
                                               PaymentOrderCallback.name == name).with_for_update()
        )).scalar_one_or_none()
        if cb is None:
            cb = PaymentOrderCallback(id=uuid.uuid4(), payment_order_id=order.id, name=name, status="PENDING")
            db.add(cb)
            await db.flush()
        if cb.status == "DONE":
            continue
        cb.attempts = (cb.attempts or 0) + 1
        try:
            async with db.begin_nested():
                await fn(db, order)
        except Exception as exc:  # efeito de negócio falhou: registra, concilia e alerta (nunca silencioso)
            from app.modules.financial import reconciliation

            cb.last_error = f"{type(exc).__name__}: {getattr(exc, 'code', '')} {str(exc)[:300]}"[:500]
            failed.append(name)
            business = isinstance(exc, DomainError) and exc.status < 500
            log.error("payment_callback_failed", callback=name, order=str(order.id), error_type=type(exc).__name__,
                      business_rejection=business)
            if business:
                # recusa determinística (ex.: 409 ORDER_MISMATCH — pedido superado): dinheiro recebido SEM
                # serviço correspondente ⇒ divergência para estorno/análise; nunca reprocessar.
                cb.status = "REJECTED"
                await reconciliation.open_item(db, "PAID_WITHOUT_SERVICE", f"callback:{order.id}:{name}",
                                               internal_ref=str(order.id), external_ref=order.provider_external_ref,
                                               internal_amount_cents=order.amount_cents,
                                               detail={"callback": name, "code": getattr(exc, "code", None),
                                                       "action": "analisar e estornar (reembolso com dupla aprovação)"})
                await _alert(db, "Pagamento sem serviço correspondente",
                             f"A cobrança {order.id} foi paga, mas o pedido foi recusado ({getattr(exc, 'code', '')}). "
                             "Analise o estorno.", str(order.id))
            else:
                cb.status = "FAILED"
                await reconciliation.open_item(db, "PAYMENT_CALLBACK_FAILED", f"callback:{order.id}:{name}",
                                               internal_ref=str(order.id), internal_amount_cents=order.amount_cents,
                                               detail={"callback": name, "error": cb.last_error})
        else:
            cb.status, cb.done_at, cb.last_error = "DONE", utcnow(), None
    return failed


async def retry_failed_callbacks(db: AsyncSession, limit: int = 50) -> int:
    rows = (await db.execute(
        select(PaymentOrderCallback).where(PaymentOrderCallback.status.in_(("FAILED", "PENDING")),
                                           PaymentOrderCallback.attempts < 10).limit(limit)
    )).scalars().all()
    n = 0
    for cb in rows:
        order = await db.get(PaymentOrder, cb.payment_order_id)
        if order is None:
            continue
        registry = _PAID_CALLBACKS if order.status in PAID_LIKE else _EXPIRED_CALLBACKS
        cbs = [(nm, fn) for nm, fn in registry.get(order.reference_type, []) if nm == cb.name]
        if cbs and not await _run_callbacks(db, order, cbs):
            n += 1
    return n


async def _mark(db: AsyncSession, ev: WebhookEvent, status: str, error: Optional[str] = None,
                order: Optional[PaymentOrder] = None) -> str:
    ev.status = status
    ev.last_error = error[:500] if error else None
    ev.processed_at = utcnow()
    if order is not None:
        ev.payment_order_id = order.id
    await db.flush()
    return status


async def _find_order(db: AsyncSession, wp: cpp.WebhookPayload) -> Optional[PaymentOrder]:
    order = None
    if wp.external_ref:
        order = (await db.execute(
            select(PaymentOrder).where(PaymentOrder.provider_external_ref == wp.external_ref).with_for_update()
        )).scalar_one_or_none()
    if order is None and wp.order_id:
        try:
            oid = uuid.UUID(wp.order_id)
        except ValueError:
            return None
        order = (await db.execute(select(PaymentOrder).where(PaymentOrder.id == oid).with_for_update())).scalar_one_or_none()
        if order is not None and order.provider_external_ref and wp.external_ref and \
                order.provider_external_ref != wp.external_ref:
            return None  # order_id aponta para cobrança diferente: não casar
    return order


async def process_webhook_event(db: AsyncSession, event_id: Any) -> str:
    """Processa um webhook persistido (idempotente; reentrante; sem commit próprio)."""
    from app.modules.financial import reconciliation

    ev = (await db.execute(
        select(WebhookEvent).where(WebhookEvent.id == common.as_uuid(event_id)).with_for_update()
    )).scalar_one_or_none()
    if ev is None:
        raise NotFound()
    if ev.status not in (WES.RECEIVED.value, WES.FAILED.value, WES.PROCESSING.value):
        return ev.status
    ev.attempts = (ev.attempts or 0) + 1
    try:
        wp = cpp.parse_webhook(ev.raw_body.encode("utf-8"))
    except cpp.ProviderError as exc:
        return await _mark(db, ev, WES.FAILED.value, exc.code)
    order = await _find_order(db, wp)
    if order is None:
        await reconciliation.open_item(db, "WEBHOOK_UNMATCHED", f"webhook:{ev.webhook_event_id}",
                                       external_ref=wp.external_ref, external_amount_cents=wp.amount_cents,
                                       detail={"order_id": wp.order_id, "status": wp.status})
        return await _mark(db, ev, WES.DIVERGENT.value, "cobrança não encontrada")
    if order.provider_external_ref is None and wp.external_ref:
        order.provider_external_ref = wp.external_ref
    if wp.currency not in (None, "BRL") or (wp.amount_cents is not None and wp.amount_cents != order.amount_cents):
        await reconciliation.open_item(db, "CHARGE_AMOUNT_MISMATCH", f"charge:{order.id}:{ev.webhook_event_id}",
                                       internal_ref=str(order.id), external_ref=wp.external_ref,
                                       internal_amount_cents=order.amount_cents, external_amount_cents=wp.amount_cents,
                                       detail={"currency": wp.currency, "status": wp.status})
        await _alert(db, "Webhook com valor divergente", f"Cobrança {order.id} recebeu {wp.status} com valor divergente.",
                     str(order.id))
        return await _mark(db, ev, WES.DIVERGENT.value, "valor/moeda divergente", order)

    st = wp.status
    if st == "paid":
        return await _apply_paid(db, ev, order, wp)
    if st in ("refunded", "partially_refunded"):
        if order.status in PENDING_LIKE or order.status in TERMINAL_UNPAID:
            r = await _apply_paid(db, ev, order, wp, mark=False)
            if r == WES.DIVERGENT.value:
                return r
        target = POS.REFUNDED.value if st == "refunded" else POS.PARTIALLY_REFUNDED.value
        if _STATUS_RANK.get(order.status, 0) >= _STATUS_RANK[target]:
            return await _mark(db, ev, WES.IGNORED.value, "fora de ordem/duplicado", order)
        order.status = target
        order.provider_status = st
        await reconciliation.open_item(db, "EXTERNAL_REFUND", f"refund:{order.id}:{st}", internal_ref=str(order.id),
                                       external_ref=wp.external_ref, internal_amount_cents=order.amount_cents,
                                       detail={"status": st, "note": "estorno feito fora do sistema; formato do "
                                               "webhook de estorno não documentado — ajuste exige dupla aprovação"})
        await _alert(db, "Estorno externo", f"A CloudPayPag informou '{st}' para a cobrança {order.id}.", str(order.id))
        common.publish_after_commit(db, "payment.updated", payment_event(order), org_id=order.org_id)
        return await _mark(db, ev, WES.PROCESSED.value, None, order)
    if st == "disputed":
        if order.dispute_status != "DISPUTED":
            order.dispute_status = "DISPUTED"
            await reconciliation.open_item(db, "CHARGE_DISPUTED", f"dispute:{order.id}", internal_ref=str(order.id),
                                           external_ref=wp.external_ref, internal_amount_cents=order.amount_cents,
                                           detail={"note": "fluxo de chargeback não documentado pela CloudPayPag"})
            await _alert(db, "Contestação (chargeback)", f"Cobrança {order.id} contestada.", str(order.id))
        return await _mark(db, ev, WES.PROCESSED.value, None, order)
    if st in ("failed", "cancelled", "expired"):
        if order.status not in PENDING_LIKE:
            return await _mark(db, ev, WES.IGNORED.value, f"{st} após {order.status}", order)
        order.status = {"failed": POS.FAILED, "cancelled": POS.CANCELLED, "expired": POS.EXPIRED}[st].value
        order.provider_status = st
        await _run_callbacks(db, order, _EXPIRED_CALLBACKS.get(order.reference_type, []))
        await audit(db, None, f"payment_order.{order.status.lower()}", "payment_order", order.id, org_id=order.org_id,
                    after={"provider_status": st}, origin="worker")
        common.publish_after_commit(db, "payment.updated", payment_event(order), org_id=order.org_id)
        return await _mark(db, ev, WES.PROCESSED.value, None, order)
    # pending
    if order.status == POS.CREATION_UNKNOWN.value:
        order.status = POS.PENDING.value
    return await _mark(db, ev, WES.IGNORED.value if order.status != POS.PENDING.value else WES.PROCESSED.value,
                       None, order)


async def _alert(db: AsyncSession, title: str, message: str, ref: str) -> None:
    from app.modules.notifications.service import StaffRecipients

    await common.notify(db, [StaffRecipients("finance.reconcile")], "finance.execution_alert",
                        {"title": title, "message": message, "ref": ref}, dedupe_key=f"fin:alert:{ref}:{title}"[:255])


async def _apply_paid(db: AsyncSession, ev: WebhookEvent, order: PaymentOrder, wp: cpp.WebhookPayload,
                      *, mark: bool = True) -> str:
    from app.modules.financial import reconciliation

    fee = wp.fee_cents
    if fee is None or fee < 0 or fee > order.amount_cents or (wp.net_cents is not None and wp.net_cents != order.amount_cents - fee):
        await reconciliation.open_item(db, "CHARGE_FEE_INVALID", f"fee:{order.id}:{ev.webhook_event_id}",
                                       internal_ref=str(order.id), external_ref=wp.external_ref,
                                       internal_amount_cents=order.amount_cents, external_amount_cents=wp.amount_cents,
                                       detail={"fee_cents": fee, "net_cents": wp.net_cents})
        return await _mark(db, ev, WES.DIVERGENT.value, "taxa/líquido inconsistentes", order)
    already_paid = order.status in PAID_LIKE
    if already_paid and mark:
        # paid repetido (outro webhook_event_id): a chave do ledger impede duplicar receita
        return await _mark(db, ev, WES.IGNORED.value, "paid já processado", order)
    late = order.status in TERMINAL_UNPAID
    order.status = POS.PAID.value if not already_paid else order.status
    order.provider_status = "paid"
    order.paid_amount_cents = wp.amount_cents if wp.amount_cents is not None else order.amount_cents
    order.fee_cents, order.net_cents = fee, order.amount_cents - fee
    order.pix_end_to_end_id = wp.pix_end_to_end_id or order.pix_end_to_end_id
    order.paid_at = order.paid_at or utcnow()
    await book_payment_received(db, order)
    await _after_paid(db, order)
    if late:
        await reconciliation.open_item(db, "PAID_AFTER_EXPIRY", f"late:{order.id}", internal_ref=str(order.id),
                                       external_ref=order.provider_external_ref,
                                       internal_amount_cents=order.amount_cents,
                                       detail={"note": "pagamento recebido após expiração/cancelamento local"})
    if mark:
        return await _mark(db, ev, WES.PROCESSED.value, None, order)
    return WES.PROCESSED.value


async def book_payment_received(db: AsyncSession, order: PaymentOrder) -> ledger.PostResult:
    """D Caixa (líquido) + D Taxas (fee real) / C Receita diferida (mídia) + C Receita de serviços."""
    cash = await ledger.account(db, "RD:Cash:CloudPayPag")
    fees = await ledger.account(db, "RD:Expense:GatewayFees")
    legs: list = [(cash, order.amount_cents - (order.fee_cents or 0), "valor líquido"),
                  (fees, order.fee_cents or 0, "taxa CloudPayPag")]
    if order.eligible_revenue_cents:
        deferred = await ledger.account(db, "ADV:DeferredRevenue", owner_id=order.org_id, org_id=order.org_id)
        legs.append((deferred, -order.eligible_revenue_cents, "receita de mídia diferida"))
    if order.services_cents:
        by_kind: dict[str, int] = {}
        for it in order.items:
            if it.get("kind") == "SERVICE":
                k = it.get("service_kind") or "GENERIC"
                by_kind[k] = by_kind.get(k, 0) + int(it["amount_cents"])
        if sum(by_kind.values()) != order.services_cents:  # itens antigos sem service_kind
            by_kind = {"GENERIC": order.services_cents}
        for k, cents in sorted(by_kind.items()):
            if not cents:
                continue
            if k == "DEPOSIT":  # caução: passivo reembolsável do parceiro, nunca receita
                est_id = (order.reference or {}).get("establishment_id")
                if not est_id:
                    raise DomainError("PARTNER_CHARGE_INVALID", "Caução sem estabelecimento de referência.")
                acc = await ledger.account(db, "EST:DepositHeld", owner_id=est_id, org_id=order.org_id)
                legs.append((acc, -cents, "caução (reembolsável)"))
            elif k == "MONTHLY_FEE":
                legs.append((await ledger.account(db, "RD:Revenue:PartnerFees"), -cents, "mensalidade do parceiro"))
            else:
                legs.append((await ledger.account(db, "RD:Revenue:Services"), -cents, f"serviços ({k.lower()})"))
    return await ledger.post(
        db, kind="PAYMENT_RECEIVED", idempotency_key=f"cpp:charge:{order.provider_external_ref or order.id}:paid",
        legs=legs, description=f"Pagamento confirmado (CloudPayPag) — cobrança {order.id}",
        competence_date=ledger.local_date(order.paid_at or utcnow(), ledger.FINANCIAL_TZ),
        external_provider=PROVIDER, external_ref=order.provider_external_ref, ref_type="payment_order",
        ref_id=order.id, org_id=order.org_id,
        meta={"amount_cents": order.amount_cents, "fee_cents": order.fee_cents},
    )


async def _after_paid(db: AsyncSession, order: PaymentOrder) -> None:
    from app.modules.financial import commission

    pool_bps, model = await commission.current_terms(db)
    by_campaign: dict[uuid.UUID, dict] = {}
    for it in order.items:
        if it.get("kind") != "MEDIA":
            continue
        cid = uuid.UUID(it["campaign_id"])
        d = by_campaign.setdefault(cid, {"amount": 0, "start": date.fromisoformat(it["service_start"]),
                                         "end": date.fromisoformat(it["service_end"])})
        d["amount"] += int(it["amount_cents"])
        d["start"] = min(d["start"], date.fromisoformat(it["service_start"]))
        d["end"] = max(d["end"], date.fromisoformat(it["service_end"]))
    for cid, d in by_campaign.items():
        await db.execute(
            pg_insert(RevenueSchedule.__table__).values(
                id=uuid.uuid4(), payment_order_id=order.id, campaign_id=cid, org_id=order.org_id,
                eligible_cents=d["amount"], refunded_cents=0, start_date=d["start"], end_date=d["end"], status="ACTIVE",
            ).on_conflict_do_nothing(constraint="uq_revenue_schedules_order_campaign")
        )
        # termo de comissão DESTA cobrança (inicial, renovação ou alteração paga) — correção (h)
        await commission.register_terms(
            db, campaign_id=cid, payment_order_id=order.id, org_id=order.org_id, eligible_cents=d["amount"],
            start=d["start"], end=d["end"], kind=await _terms_kind(db, order, cid, d["start"], d["end"]),
            paid_on=ledger.local_date(order.paid_at or utcnow(), ledger.FINANCIAL_TZ), pool_bps=pool_bps, model=model)
    await db.flush()
    await _accrue_seller_commission(db, order)
    await _run_callbacks(db, order, _PAID_CALLBACKS.get(order.reference_type, []))
    await audit(db, None, "payment_order.paid", "payment_order", order.id, org_id=order.org_id, origin="worker",
                after={"amount_cents": order.amount_cents, "fee_cents": order.fee_cents,
                       "external_ref": order.provider_external_ref})
    from app.modules.notifications.service import OrgRecipients

    await common.notify(db, [OrgRecipients(order.org_id, "payments.view")], "payment.approved",
                        {"reference": order.reference_id[:12], "amount": common.brl(order.amount_cents),
                         "campaign_name": ""}, dedupe_key=f"payment.approved:{order.id}", org_id=order.org_id,
                        entity=("payment_order", order.id))
    common.publish_after_commit(db, "payment.updated", payment_event(order), org_id=order.org_id)


async def _terms_kind(db: AsyncSession, order: PaymentOrder, campaign_id: uuid.UUID, start: date, end: date) -> str:
    """INITIAL | RENEWAL | CHANGE: pelo tipo do pedido do W-CAMP; sem ele, por sobreposição de período."""
    if order.reference_type == REF_CAMPAIGN_CHECKOUT:
        try:
            from app.models.pricing import Order

            o = await db.get(Order, uuid.UUID(order.reference_id))
        except (ImportError, ValueError):
            o = None
        kind = str(getattr(o, "kind", "") or "")
        if kind in ("RENEWAL", "CHANGE"):
            return kind
        if kind in ("CHECKOUT", "PROPOSAL"):
            return "INITIAL"
    from app.modules.financial import commission

    prev = [t for t in await commission.terms_of(db, campaign_id) if t.payment_order_id != order.id]
    if not prev:
        return "INITIAL"
    return "CHANGE" if any(t.start_date <= end and start <= t.end_date for t in prev) else "RENEWAL"


async def _accrue_seller_commission(db: AsyncSession, order: PaymentOrder) -> None:
    """Comissão de vendedor (W-BIZ ``sales_attributions``): passivo SELLER no pagamento."""
    try:
        from app.models.crm import SalesAttribution
    except ImportError:  # pragma: no cover
        return
    if order.reference_type != REF_CAMPAIGN_CHECKOUT:
        return
    try:
        oid = uuid.UUID(order.reference_id)
    except ValueError:
        return
    rows = (await db.execute(
        select(SalesAttribution).where(SalesAttribution.order_id == oid, SalesAttribution.status != "CANCELADA")
    )).scalars().all()
    for sa in rows:
        if not sa.commission_cents:
            continue
        exp = await ledger.account(db, "RD:Expense:SellerCommission")
        pay = await ledger.account(db, "SELLER:CommissionPayable", owner_id=sa.seller_id)
        await ledger.post(db, kind="SELLER_COMMISSION", idempotency_key=f"seller:attribution:{sa.id}:accrued",
                          legs=[(exp, sa.commission_cents), (pay, -sa.commission_cents)],
                          description=f"Comissão de vendedor — pedido {oid}", ref_type="sales_attribution",
                          ref_id=sa.id, meta={"commission_bps": sa.commission_bps, "seller_id": str(sa.seller_id)})


async def pending_webhook_ids(db: AsyncSession, *, older_than_s: int = 30, limit: int = 100) -> list[uuid.UUID]:
    from datetime import timedelta

    cutoff = utcnow() - timedelta(seconds=older_than_s)
    rows = await db.execute(
        select(WebhookEvent.id).where(WebhookEvent.status.in_((WES.RECEIVED.value, WES.FAILED.value)),
                                      WebhookEvent.received_at < cutoff, WebhookEvent.attempts < 20)
        .order_by(WebhookEvent.received_at).limit(limit)
    )
    return [r[0] for r in rows]
