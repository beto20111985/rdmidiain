"""
RD Mídia Indoor — Adapter CloudPayPag (ADR_009 item 6; A5 D5; mapa ``A5_cloudpaypag_api_map``).

Implementa SOMENTE as operações documentadas em https://cloudpaypag.top/api-docs
(snapshot 2026-10-07): ``GET /me``, ``GET /saldo``, ``GET /balance/blocks``,
``POST /charges`` (pix | credit_card), ``POST /withdrawals``,
``POST /withdrawals/simulate``, ``GET /withdrawals/{external_id}``,
``POST /withdrawals/copypaste``, ``POST /balance/block`` e ``/balance/unblock``.
Base: ``https://cloudpaypag.top/api/v1/integration``; ``Authorization: Bearer``.

NÃO documentado ⇒ ``ProviderCapabilityUnavailable`` (exibido no painel como
"BLOQUEADO POR DEPENDÊNCIA EXTERNA"): boleto, estorno/reembolso, consulta e
listagem de cobranças, chargeback/disputa, cadastro de chaves PIX.

Regras de transporte:
- Credenciais POR INSTÂNCIA (``ProviderConfig``) vindas de segredo
  (``CLOUDPAYPAG_TOKEN_FILE`` / ``CLOUDPAYPAG_API_KEY[_FILE]``); nunca logadas; o
  ``repr`` mascara. ``payment_source_token`` nunca é logado nem persistido.
- httpx com timeouts (5 s conexão / 15 s leitura). Retry com backoff SOMENTE em GET
  (erro de rede, 502, 503). POST nunca é repetido (o provedor não oferece
  idempotência em ``/charges`` e ``/withdrawals``) — timeout ⇒ ``ProviderTimeout``
  e o chamador grava estado UNKNOWN.
- Dinheiro: centavos (int) ↔ reais ``Decimal`` com 2 casas; o JSON enviado é
  serializado a partir do ``Decimal`` (sem float). Respostas são lidas com
  ``parse_float=Decimal`` e valores com mais de 2 casas são rejeitados.
- Webhook: HMAC-SHA256 com o ``webhook_secret`` sobre
  ``json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))`` (como a
  doc), header ``X-Webhook-Signature: sha256=<hex>``, ``hmac.compare_digest``.

Mock: ``FakeCloudPayPag`` existe SOMENTE para ENVIRONMENT=development/test; emite
webhooks ASSINADOS pelo mesmo algoritmo que passam pelo MESMO processador. Em
qualquer outro ambiente ``get_provider`` recusa o modo fake (e a configuração de
produção já recusa ``MOCK_PAYMENT=true``).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, Protocol

import httpx
import structlog

from app.core.config import settings
from app.core.errors import DomainError
from app.core.money import MoneyError, cents_to_reais, ensure_cents, reais_to_cents

log = structlog.get_logger("rd.financial.cloudpaypag")

API_PREFIX = "/api/v1/integration"
DEFAULT_BASE = "https://cloudpaypag.top"
SIGNATURE_HEADER = "X-Webhook-Signature"
TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
GET_RETRIES = 2
GET_BACKOFF_S = (0.5, 1.5)

CHARGE_STATUSES = ("pending", "paid", "failed", "cancelled", "expired", "refunded", "partially_refunded", "disputed")
WITHDRAWAL_STATUSES = ("processing", "completed", "failed", "cancelled")
PIX_KEY_TYPES = ("cpf", "cnpj", "email", "phone", "random")

# Capacidades da API pública (para o painel). False = não documentado ⇒ dependência externa.
CAPABILITIES: dict[str, bool] = {
    "identity": True,
    "balance": True,
    "charge_pix": True,
    "charge_credit_card": True,
    "charge_boleto": False,
    "charge_query": False,
    "charge_list": False,
    "charge_refund": False,
    "charge_cancel": False,
    "chargeback_dispute": False,
    "withdrawal": True,
    "withdrawal_simulate": True,
    "withdrawal_query": True,
    "withdrawal_copypaste": True,
    "withdrawal_idempotency": False,
    "balance_block": True,
    "pix_key_registry": False,
    "webhook_withdrawal_format": False,
}


# ─── Erros tipados ───────────────────────────────────────────────────────────

class ProviderError(DomainError):
    status, code, message = 502, "PROVIDER_ERROR", "Falha na comunicação com a CloudPayPag."
    provider_status: Optional[int] = None

    def __init__(self, code=None, message=None, status=None, details=None, *, provider_status=None):
        super().__init__(code, message, status, details)
        self.provider_status = provider_status


class ProviderBadRequest(ProviderError):
    status, code, message = 400, "PROVIDER_REJECTED", "A CloudPayPag recusou a operação."


class ProviderAuthError(ProviderError):
    status, code, message = 502, "PROVIDER_AUTH_ERROR", "Credencial da CloudPayPag inválida ou ausente."


class ProviderForbidden(ProviderError):
    status, code, message = 502, "PROVIDER_FORBIDDEN", "Subconta CloudPayPag sem permissão para a operação."


class ProviderConflict(ProviderError):
    status, code, message = 409, "PROVIDER_CONFLICT", "Conflito de idempotência na CloudPayPag."


class ProviderTemporaryError(ProviderError):
    status, code, message = 502, "PROVIDER_TEMPORARY_ERROR", "Falha temporária na CloudPayPag."


class ProviderUnavailable(ProviderError):
    status, code, message = 503, "PROVIDER_UNAVAILABLE", "Processamento indisponível na CloudPayPag."


class ProviderTimeout(ProviderError):
    """Resultado desconhecido (rede/timeout): NUNCA repetir POST automaticamente."""

    status, code, message = 504, "PROVIDER_TIMEOUT", "Sem resposta da CloudPayPag (resultado desconhecido)."


class ProviderProtocolError(ProviderError):
    status, code, message = 502, "PROVIDER_PROTOCOL_ERROR", "Resposta da CloudPayPag fora do contrato."


class ProviderCapabilityUnavailable(ProviderError):
    status, code, message = 501, "PROVIDER_CAPABILITY_UNAVAILABLE", (
        "Operação não oferecida pela API pública da CloudPayPag (bloqueado por dependência externa)."
    )


class ProviderConfigError(ProviderError):
    status, code, message = 503, "PROVIDER_NOT_CONFIGURED", "Integração CloudPayPag não configurada."


def capability_unavailable(name: str) -> ProviderCapabilityUnavailable:
    return ProviderCapabilityUnavailable(details={"capability": name, "status": "BLOQUEADO POR DEPENDÊNCIA EXTERNA"})


_STATUS_MAP: dict[int, type[ProviderError]] = {
    400: ProviderBadRequest,
    401: ProviderAuthError,
    403: ProviderForbidden,
    409: ProviderConflict,
    502: ProviderTemporaryError,
    503: ProviderUnavailable,
}


# ─── Dinheiro e JSON sem float ───────────────────────────────────────────────

def to_reais(amount_cents: int) -> Decimal:
    ensure_cents(amount_cents)
    if amount_cents <= 0:
        raise MoneyError("valor deve ser maior que zero")
    return cents_to_reais(amount_cents)


def from_reais(value: Any, *, field_name: str = "amount") -> int:
    """Converte valor recebido (Decimal/str/int; nunca float) para centavos."""
    if value is None:
        raise ProviderProtocolError(details={"field": field_name})
    if isinstance(value, float):  # nunca deve ocorrer (parse_float=Decimal)
        raise ProviderProtocolError(details={"field": field_name, "reason": "float"})
    try:
        return reais_to_cents(value)
    except (MoneyError, TypeError, InvalidOperation):
        raise ProviderProtocolError(details={"field": field_name, "reason": "valor monetário inválido"}) from None


def dumps_exact(obj: Any) -> str:
    """JSON compacto em que ``Decimal`` é emitido como número exato (sem passar por float)."""
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError("Decimal não finito")
        return format(obj, "f")
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        raise TypeError("float não é aceito no payload financeiro")
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + dumps_exact(v) for k, v in obj.items()) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(dumps_exact(v) for v in obj) + "]"
    raise TypeError(f"tipo não serializável: {type(obj).__name__}")


def loads_exact(raw: bytes | str) -> Any:
    return json.loads(raw, parse_float=Decimal)


# ─── Assinatura do webhook (algoritmo da documentação) ───────────────────────

def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def sign_payload(payload: dict, secret: str) -> str:
    """Igual ao ``sign_payload`` da documentação (hexdigest, sem prefixo)."""
    return hmac.new(secret.encode("utf-8"), canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()


def signature_header_value(payload: dict, secret: str) -> str:
    return "sha256=" + sign_payload(payload, secret)


def verify_signature(raw_body: bytes, signature_header: Optional[str], secret: str) -> Optional[dict]:
    """Valida ``X-Webhook-Signature``; devolve o payload (parse padrão) ou ``None``.

    Segue a doc: parse do corpo e re-serialização canônica (sort_keys, separadores
    compactos) antes do HMAC; comparação em tempo constante. Corpo inválido ⇒ None.
    """
    if not secret:
        return None
    if not signature_header or not signature_header.startswith("sha256="):
        return None
    received = signature_header[len("sha256="):].strip()
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expected = sign_payload(payload, secret)
    if not hmac.compare_digest(received.encode("ascii", "replace"), expected.encode("ascii")):
        return None
    return payload


@dataclass(frozen=True)
class WebhookPayload:
    """Campos do webhook de cobrança (exemplo PAID da doc). Valores em centavos."""

    webhook_event_id: int
    status: str
    external_ref: Optional[str]
    order_id: Optional[str]
    transaction_id: Optional[str]
    internal_transaction_id: Optional[int]
    subconta_id: Optional[int]
    method: Optional[str]
    amount_cents: Optional[int]
    fee_cents: Optional[int]
    net_cents: Optional[int]
    currency: Optional[str]
    pix_end_to_end_id: Optional[str]
    version: Optional[int]


def parse_webhook(raw_body: bytes | str) -> WebhookPayload:
    """Parse com ``Decimal`` (valores) do corpo JÁ autenticado."""
    data = loads_exact(raw_body)
    if not isinstance(data, dict):
        raise ProviderProtocolError(details={"reason": "corpo não é objeto"})
    meta = data.get("cloudpaypag") or {}
    if not isinstance(meta, dict):
        raise ProviderProtocolError(details={"field": "cloudpaypag"})
    ev = meta.get("webhook_event_id")
    if isinstance(ev, bool) or not isinstance(ev, int) or ev <= 0:
        raise ProviderProtocolError("WEBHOOK_EVENT_ID_MISSING", "cloudpaypag.webhook_event_id ausente ou inválido.",
                                    status=400)
    status = data.get("status")
    if not isinstance(status, str) or status not in CHARGE_STATUSES:
        raise ProviderProtocolError("WEBHOOK_STATUS_INVALID", "status do webhook ausente ou desconhecido.", status=400)

    def money(name: str) -> Optional[int]:
        v = data.get(name)
        return None if v is None else from_reais(v, field_name=name)

    def opt_int(v: Any) -> Optional[int]:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    def opt_str(v: Any, n: int = 120) -> Optional[str]:
        return str(v)[:n] if v is not None else None

    return WebhookPayload(
        webhook_event_id=ev,
        status=status,
        external_ref=opt_str(data.get("external_ref"), 80),
        order_id=opt_str(data.get("order_id"), 64),
        transaction_id=opt_str(data.get("transaction_id")),
        internal_transaction_id=opt_int(meta.get("internal_transaction_id")),
        subconta_id=opt_int(meta.get("subconta_id")),
        method=opt_str(data.get("method"), 20),
        amount_cents=money("amount"),
        fee_cents=money("fee"),
        net_cents=money("net_value"),
        currency=opt_str(data.get("currency"), 3),
        pix_end_to_end_id=opt_str(data.get("pix_end_to_end_id"), 80),
        version=opt_int(meta.get("version")),
    )


# ─── Configuração por instância ──────────────────────────────────────────────

@dataclass(frozen=True)
class ProviderConfig:
    instance_slug: str
    base_url: str
    token: str = field(repr=False, default="")
    webhook_secret: str = field(repr=False, default="")
    subconta_id: Optional[int] = None
    mode: str = "live"  # live | fake

    def __repr__(self) -> str:  # nunca expor segredos
        return (f"ProviderConfig(instance_slug={self.instance_slug!r}, base_url={self.base_url!r}, "
                f"token={'***' if self.token else ''!r}, subconta_id={self.subconta_id!r}, mode={self.mode!r})")

    @property
    def api_base(self) -> str:
        return self.base_url.rstrip("/") + API_PREFIX


def _read_secret_file(var: str) -> str:
    path = os.environ.get(var)
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        raise ProviderConfigError(details={"variable": var}) from None


def default_instance_slug() -> str:
    return (os.environ.get("RD_INSTANCE_SLUG") or "rd").strip().lower()


def _env_config() -> ProviderConfig:
    token = _read_secret_file("CLOUDPAYPAG_TOKEN_FILE") or (settings.CLOUDPAYPAG_API_KEY or "")
    secret = _read_secret_file("CLOUDPAYPAG_WEBHOOK_SECRET_FILE") or (settings.CLOUDPAYPAG_WEBHOOK_SECRET or "")
    sub = os.environ.get("CLOUDPAYPAG_SUBCONTA_ID")
    sub_id = int(sub) if sub and sub.strip().isdigit() else None
    mode = "fake" if settings.MOCK_PAYMENT else "live"
    return ProviderConfig(
        instance_slug=default_instance_slug(),
        base_url=(settings.CLOUDPAYPAG_BASE_URL or DEFAULT_BASE),
        token=token,
        webhook_secret=secret,
        subconta_id=sub_id,
        mode=mode,
    )


_config_override: Optional[ProviderConfig] = None


def set_config_override(cfg: Optional[ProviderConfig]) -> None:
    """Somente testes/control plane: substitui a configuração da instância."""
    global _config_override
    _config_override = cfg


def get_config(instance_slug: Optional[str] = None) -> ProviderConfig:
    """Configuração da instância. Cada deploy (white-label) tem a sua subconta (P001 §77)."""
    cfg = _config_override or _env_config()
    if instance_slug is not None and instance_slug.lower() != cfg.instance_slug:
        raise ProviderConfigError("PROVIDER_INSTANCE_UNKNOWN", "Instância de pagamento desconhecida.", status=404)
    return cfg


def fake_allowed() -> bool:
    return settings.ENVIRONMENT in ("development", "test")


# ─── Interface ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Customer:
    name: str
    email: str
    phone: str
    document_type: str  # CPF | CNPJ
    document_number: str

    def as_payload(self) -> dict:
        return {
            "name": self.name[:200],
            "email": self.email,
            "phone": self.phone,
            "document": {"type": self.document_type, "number": self.document_number},
        }


@dataclass(frozen=True)
class ChargeResult:
    transaction_id: int
    order_id: Optional[str]
    external_ref: str
    processor_reference_id: Optional[str]
    payment_method: str
    status: str
    amount_cents: int
    pix_end_to_end_id: Optional[str]
    qr_code: Optional[str]
    created_at: Optional[str]


@dataclass(frozen=True)
class WithdrawalResult:
    withdrawal_id: Optional[int]
    external_id: str
    status: str
    amount_cents: int
    fee_cents: Optional[int]
    net_cents: Optional[int]
    processor_reference_id: Optional[str] = None
    failure_reason: Optional[str] = None
    comprovante_url: Optional[str] = None


@dataclass(frozen=True)
class SimulationResult:
    amount_cents: int
    fee_cents: int
    net_cents: int
    available_cents: int
    sufficient_balance: bool
    operational: bool
    sufficient: bool
    own_key: bool


@dataclass(frozen=True)
class BalanceResult:
    subconta_id: Optional[int]
    available_cents: int
    blocked_cents: int
    total_cents: int
    api_blocked_cents: Optional[int] = None


class PaymentProvider(Protocol):
    config: ProviderConfig

    async def get_me(self) -> dict: ...
    async def get_balance(self) -> BalanceResult: ...
    async def get_api_blocks(self) -> BalanceResult: ...
    async def create_charge(self, *, order_id: str, amount_cents: int, method: str, customer: Customer,
                            description: str, payment_source_token: Optional[str] = None,
                            installments: int = 1) -> ChargeResult: ...
    async def simulate_withdrawal(self, *, amount_cents: int, pix_key: str, pix_key_type: str) -> SimulationResult: ...
    async def create_withdrawal(self, *, amount_cents: int, pix_key: str, pix_key_type: str) -> WithdrawalResult: ...
    async def get_withdrawal(self, external_id: str) -> WithdrawalResult: ...
    async def create_withdrawal_copypaste(self, *, pix_payload: str, amount_cents: Optional[int] = None,
                                          withdrawal_password: Optional[str] = None) -> WithdrawalResult: ...
    async def block_balance(self, *, amount_cents: int, external_id: str, description: str) -> dict: ...
    async def unblock_balance(self, *, amount_cents: int, external_id: str, description: str) -> dict: ...


def _validate_external_id(external_id: str) -> str:
    if not external_id or len(external_id) > 64 or not all(c.isalnum() or c in "._-" for c in external_id):
        raise ValueError("external_id inválido (≤64, [A-Za-z0-9._-])")
    return external_id


def _parse_charge(d: dict) -> ChargeResult:
    try:
        return ChargeResult(
            transaction_id=int(d["transaction_id"]),
            order_id=d.get("order_id"),
            external_ref=str(d["external_ref"]),
            processor_reference_id=d.get("processor_reference_id"),
            payment_method=str(d.get("payment_method") or ""),
            status=str(d["status"]),
            amount_cents=from_reais(d.get("amount")),
            pix_end_to_end_id=d.get("pix_end_to_end_id"),
            qr_code=(d.get("method_data") or {}).get("qr_code") if isinstance(d.get("method_data"), dict) else None,
            created_at=d.get("created_at"),
        )
    except (KeyError, TypeError, ValueError):
        raise ProviderProtocolError(details={"operation": "create_charge"}) from None


def _parse_withdrawal(d: dict) -> WithdrawalResult:
    try:
        status = str(d["status"])
        return WithdrawalResult(
            withdrawal_id=(int(d["withdrawal_id"]) if d.get("withdrawal_id") is not None else None),
            external_id=str(d["external_id"]),
            status=status,
            amount_cents=from_reais(d.get("valor"), field_name="valor"),
            fee_cents=(from_reais(d["taxa"], field_name="taxa") if d.get("taxa") is not None else None),
            net_cents=(from_reais(d["valor_liquido"], field_name="valor_liquido")
                       if d.get("valor_liquido") is not None else None),
            processor_reference_id=d.get("processor_reference_id"),
            failure_reason=(str(d["failure_reason"])[:300] if d.get("failure_reason") else None),
            comprovante_url=d.get("comprovante_url"),
        )
    except (KeyError, TypeError, ValueError):
        raise ProviderProtocolError(details={"operation": "withdrawal"}) from None


def _parse_balance(d: dict) -> BalanceResult:
    try:
        return BalanceResult(
            subconta_id=d.get("subconta_id"),
            available_cents=from_reais(d.get("saldo_disponivel"), field_name="saldo_disponivel"),
            blocked_cents=from_reais(d.get("saldo_bloqueado"), field_name="saldo_bloqueado"),
            total_cents=from_reais(d.get("saldo_total"), field_name="saldo_total"),
            api_blocked_cents=(from_reais(d["api_blocked_total"], field_name="api_blocked_total")
                               if d.get("api_blocked_total") is not None else None),
        )
    except (KeyError, TypeError):
        raise ProviderProtocolError(details={"operation": "balance"}) from None


def _check_pix_key_type(t: str) -> str:
    if t not in PIX_KEY_TYPES:
        raise ValueError("tipo de chave PIX inválido")
    return t


# ─── Cliente real (httpx) ────────────────────────────────────────────────────

class CloudPayPagClient:
    """Cliente HTTP da API pública. NÃO VALIDADO EXTERNAMENTE (sem token sandbox)."""

    def __init__(self, config: ProviderConfig, *, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.config = config
        self._transport = transport

    def _headers(self) -> dict:
        if not self.config.token:
            raise ProviderConfigError(details={"missing": "token"})
        return {"Authorization": f"Bearer {self.config.token}", "Content-Type": "application/json",
                "Accept": "application/json"}

    async def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = self.config.api_base + path
        headers = self._headers()
        content = dumps_exact(body).encode("utf-8") if body is not None else None
        attempts = 1 + (GET_RETRIES if method == "GET" else 0)
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                    resp = await client.request(method, url, headers=headers, content=content)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                log.warning("cloudpaypag_transport_error", method=method, path=path, attempt=attempt + 1,
                            error_type=type(exc).__name__)
                last_exc = exc
                if method == "GET" and attempt + 1 < attempts:
                    await asyncio.sleep(GET_BACKOFF_S[min(attempt, len(GET_BACKOFF_S) - 1)])
                    continue
                raise ProviderTimeout(details={"operation": f"{method} {path}"}) from None
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log.info("cloudpaypag_request", method=method, path=path, status=resp.status_code, elapsed_ms=elapsed_ms)
            if resp.status_code in (502, 503) and method == "GET" and attempt + 1 < attempts:
                await asyncio.sleep(GET_BACKOFF_S[min(attempt, len(GET_BACKOFF_S) - 1)])
                continue
            if resp.status_code >= 400:
                cls = _STATUS_MAP.get(resp.status_code)
                detail = _safe_error_detail(resp)
                if cls is None:
                    cls = ProviderTemporaryError if resp.status_code >= 500 else ProviderBadRequest
                raise cls(details={"operation": f"{method} {path}", "provider_message": detail},
                          provider_status=resp.status_code)
            try:
                return loads_exact(resp.content)
            except ValueError:
                raise ProviderProtocolError(details={"operation": f"{method} {path}"}) from None
        raise ProviderTimeout(details={"operation": f"{method} {path}"}) from last_exc

    # ── documentados ──
    async def get_me(self) -> dict:
        d = await self._request("GET", "/me")
        if not isinstance(d, dict) or "subconta_id" not in d:
            raise ProviderProtocolError(details={"operation": "me"})
        return {"subconta_id": d.get("subconta_id"), "external_id": d.get("external_id"),
                "email": d.get("email"), "status": d.get("status")}

    async def get_balance(self) -> BalanceResult:
        return _parse_balance(await self._request("GET", "/saldo"))

    async def get_api_blocks(self) -> BalanceResult:
        return _parse_balance(await self._request("GET", "/balance/blocks"))

    async def create_charge(self, *, order_id: str, amount_cents: int, method: str, customer: Customer,
                            description: str, payment_source_token: Optional[str] = None,
                            installments: int = 1) -> ChargeResult:
        if method not in ("pix", "credit_card"):
            raise capability_unavailable(f"charge_{method}")
        if len(order_id) > 64:
            raise ValueError("order_id > 64")
        body: dict = {
            "payment_method": method,
            "amount": to_reais(amount_cents),
            "description": description[:255],
            "order_id": order_id,
            "customer": customer.as_payload(),
        }
        if method == "credit_card":
            if not payment_source_token:
                raise ProviderBadRequest("PAYMENT_SOURCE_TOKEN_REQUIRED", "Cartão exige payment_source_token.")
            if not 1 <= installments <= 12:
                raise ValueError("installments fora de 1..12")
            body["payment_source_token"] = payment_source_token
            body["installments"] = installments
        return _parse_charge(await self._request("POST", "/charges", body))

    async def simulate_withdrawal(self, *, amount_cents: int, pix_key: str, pix_key_type: str) -> SimulationResult:
        d = await self._request("POST", "/withdrawals/simulate",
                                {"valor": to_reais(amount_cents), "pix_key": pix_key,
                                 "pix_key_type": _check_pix_key_type(pix_key_type)})
        try:
            return SimulationResult(
                amount_cents=from_reais(d.get("valor"), field_name="valor"),
                fee_cents=from_reais(d.get("taxa"), field_name="taxa"),
                net_cents=from_reais(d.get("valor_liquido"), field_name="valor_liquido"),
                available_cents=from_reais(d.get("saldo_disponivel"), field_name="saldo_disponivel"),
                sufficient_balance=bool(d.get("suficiente_saldo_subconta")),
                operational=bool(d.get("saque_operacionalmente_apto")),
                sufficient=bool(d.get("suficiente")),
                own_key=bool(d.get("chave_propria")),
            )
        except (TypeError, AttributeError):
            raise ProviderProtocolError(details={"operation": "simulate"}) from None

    async def create_withdrawal(self, *, amount_cents: int, pix_key: str, pix_key_type: str) -> WithdrawalResult:
        return _parse_withdrawal(await self._request(
            "POST", "/withdrawals",
            {"valor": to_reais(amount_cents), "pix_key": pix_key, "pix_key_type": _check_pix_key_type(pix_key_type)},
        ))

    async def get_withdrawal(self, external_id: str) -> WithdrawalResult:
        _validate_external_id(external_id)
        return _parse_withdrawal(await self._request("GET", f"/withdrawals/{external_id}"))

    async def create_withdrawal_copypaste(self, *, pix_payload: str, amount_cents: Optional[int] = None,
                                          withdrawal_password: Optional[str] = None) -> WithdrawalResult:
        body: dict = {"pix_payload": pix_payload}
        if amount_cents is not None:
            body["amount"] = to_reais(amount_cents)
        if withdrawal_password:
            body["withdrawal_password"] = withdrawal_password
        return _parse_withdrawal(await self._request("POST", "/withdrawals/copypaste", body))

    async def block_balance(self, *, amount_cents: int, external_id: str, description: str) -> dict:
        return await self._block("/balance/block", amount_cents, external_id, description)

    async def unblock_balance(self, *, amount_cents: int, external_id: str, description: str) -> dict:
        return await self._block("/balance/unblock", amount_cents, external_id, description)

    async def _block(self, path: str, amount_cents: int, external_id: str, description: str) -> dict:
        d = await self._request("POST", path, {"amount": to_reais(amount_cents),
                                               "external_id": _validate_external_id(external_id),
                                               "description": description[:255]})
        try:
            return {
                "block_id": d.get("block_id"), "external_id": d.get("external_id"), "operation": d.get("operation"),
                "amount_cents": from_reais(d.get("amount")), "status": d.get("status"),
                "idempotent_replay": bool(d.get("idempotent_replay")),
                "available_cents": from_reais(d.get("saldo_disponivel"), field_name="saldo_disponivel"),
                "blocked_cents": from_reais(d.get("saldo_bloqueado"), field_name="saldo_bloqueado"),
                "api_blocked_cents": from_reais(d.get("api_blocked_total"), field_name="api_blocked_total"),
            }
        except (TypeError, AttributeError):
            raise ProviderProtocolError(details={"operation": path}) from None

    # ── NÃO documentados ⇒ dependência externa ──
    async def refund_charge(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("charge_refund")

    async def get_charge(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("charge_query")

    async def list_charges(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("charge_list")

    async def create_boleto(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("charge_boleto")

    async def open_dispute(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("chargeback_dispute")


def _safe_error_detail(resp: httpx.Response) -> Optional[str]:
    """Mensagem de erro do provedor, truncada (formato do corpo de erro é DESCONHECIDO)."""
    try:
        d = resp.json()
    except ValueError:
        return None
    if isinstance(d, dict):
        for k in ("detail", "error", "message"):
            v = d.get(k)
            if isinstance(v, str):
                return v[:200]
    return None


# ─── Fake (SOMENTE development/test) ─────────────────────────────────────────

FAKE_WITHDRAWAL_FEE_CENTS = 200  # valor fictício do FAKE (documentação: exemplo de taxa R$ 2,00)


class FakeCloudPayPag:
    """Provedor FALSO para desenvolvimento/testes. Nunca usado em produção.

    - Respostas no formato documentado (marcadas ``FAKE-DEV``), sem dinheiro real.
    - ``build_webhook`` gera corpo + header ASSINADOS pelo algoritmo da doc com o
      ``webhook_secret`` da instância; o webhook deve ser entregue ao endpoint real.
    - Estado de saques no Redis (compartilhado entre API e worker em dev).
    - NÃO confirma pagamentos sozinho (nenhum ``get_charge`` → PAID).
    - Saldo (``/saldo``) DERIVADO dos registros do próprio provedor falso — nunca um contador:
      ver ``_statement`` (BUG-QA-02).
    """

    def __init__(self, config: ProviderConfig) -> None:
        if not fake_allowed():
            raise ProviderConfigError("PROVIDER_FAKE_FORBIDDEN", "Provedor falso proibido neste ambiente.")
        self.config = config

    async def _redis(self):
        from app.core.redis import get_redis

        return await get_redis()

    def _key(self, *parts: Any) -> str:
        from app.core.redis import rkey

        return rkey("fakecpp", self.config.instance_slug, *parts)

    async def get_me(self) -> dict:
        return {"subconta_id": self.config.subconta_id or 0, "external_id": "FAKE-DEV", "email": None,
                "status": "aprovada"}

    async def get_balance(self) -> BalanceResult:
        total, blocked = await self._statement()
        return BalanceResult(self.config.subconta_id, total - blocked, blocked, total)

    async def get_api_blocks(self) -> BalanceResult:
        b = await self.get_balance()
        return BalanceResult(b.subconta_id, b.available_cents, b.blocked_cents, b.total_cents, 0)

    async def _statement(self) -> tuple[int, int]:
        """Extrato FALSO ``(saldo_total, saldo_bloqueado)`` com a semântica da doc de ``GET /saldo``
        ("créditos − débitos − taxas − reservas"; bloqueado = saques pendentes e contestações).

        Derivado dos registros do provedor falso (BUG-QA-02 — antes era um contador nunca alimentado):
        - COBRANÇAS: as mensagens que o próprio fake assinou e entregou (``webhook_events`` desta instância
          com assinatura válida e mesma subconta — o fake não tem outro armazenamento de cobranças, e assim
          os pagamentos já feitos no dev entram). Por cobrança (``external_ref``), em ordem de chegada:
          1º ``paid`` (ou refunded/partially_refunded/disputed sem paid antes) ⇒ + ``net_value``
          (= amount − fee do próprio webhook; repetições não creditam de novo); ``refunded`` ⇒ − amount;
          ``disputed`` ⇒ amount vai para o bloqueado (total igual); ``partially_refunded`` não informa o
          valor estornado na doc ⇒ sem débito (divergência fica para a conciliação manual).
        - SAQUES/PIX OUT (inclusive reembolsos pagos por PIX OUT): os saques que o fake criou
          (``wd_fake_*``; índice no Redis ∪ ids gravados pela plataforma), com o ESTADO do fake
          (Redis; sem ele, o último estado conhecido): ``completed`` ⇒ − valor; ``processing`` ⇒ valor
          bloqueado (reserva); ``failed``/``cancelled`` ⇒ nada.
        Bloqueios da API (``/balance/block``) não alteram o total.
        """
        from sqlalchemy import select

        from app.core.database import session_factory
        from app.models.financial import Withdrawal, WebhookEvent

        r = await self._redis()
        async with session_factory()() as s:
            events = (await s.execute(
                select(WebhookEvent.raw_body).where(
                    WebhookEvent.instance_slug == self.config.instance_slug, WebhookEvent.signature_valid.is_(True))
                .order_by(WebhookEvent.received_at, WebhookEvent.id))).scalars().all()
            known = {ext: (sent if sent is not None else amount, st) for ext, sent, amount, st in (await s.execute(
                select(Withdrawal.provider_external_id, Withdrawal.sent_cents, Withdrawal.amount_cents, Withdrawal.status)
                .where(Withdrawal.provider_external_id.like(r"wd\_fake\_%")))).all()}
        total = blocked = 0
        credited: set[str] = set()
        refunded: set[str] = set()
        disputed: dict[str, int] = {}
        for raw in events:
            try:
                wp = parse_webhook(raw)
            except ProviderError:
                continue
            ref = wp.external_ref
            if not ref or (self.config.subconta_id is not None and wp.subconta_id not in (None, self.config.subconta_id)):
                continue
            amount = int(wp.amount_cents or 0)
            if wp.status in ("paid", "refunded", "partially_refunded", "disputed") and ref not in credited:
                credited.add(ref)
                total += wp.net_cents if wp.net_cents is not None else amount - int(wp.fee_cents or 0)
            if wp.status == "refunded" and ref not in refunded:
                refunded.add(ref)
                total -= amount
                disputed.pop(ref, None)
            elif wp.status == "disputed" and ref not in refunded:
                disputed[ref] = amount
        blocked += sum(disputed.values())
        db_status = {"COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "cancelled"}
        for ext in sorted(set(known) | set(await r.smembers(self._key("wd_index")))):
            h = await r.hgetall(self._key("wd", ext))
            if h:
                valor, st = int(h["valor"]), h["status"]
            elif ext in known:
                valor, st = int(known[ext][0]), db_status.get(known[ext][1], "processing")
            else:
                continue
            if st == "completed":
                total -= valor
            elif st == "processing":
                blocked += valor
        return total, blocked

    async def create_charge(self, *, order_id: str, amount_cents: int, method: str, customer: Customer,
                            description: str, payment_source_token: Optional[str] = None,
                            installments: int = 1) -> ChargeResult:
        if method not in ("pix", "credit_card"):
            raise capability_unavailable(f"charge_{method}")
        to_reais(amount_cents)
        if method == "credit_card" and not payment_source_token:
            raise ProviderBadRequest("PAYMENT_SOURCE_TOKEN_REQUIRED", "Cartão exige payment_source_token.")
        sub = self.config.subconta_id or 0
        return ChargeResult(
            transaction_id=secrets.randbelow(10**9) + 1,
            order_id=order_id,
            external_ref=f"sc_{sub}_{uuid.uuid4().hex[:15]}",
            processor_reference_id=f"fake_{uuid.uuid4().hex[:20]}",
            payment_method=method,
            status="pending",
            amount_cents=amount_cents,
            pix_end_to_end_id=None,
            qr_code=(f"FAKE-DEV-PIX-{uuid.uuid4().hex}" if method == "pix" else None),
            created_at=None,
        )

    async def simulate_withdrawal(self, *, amount_cents: int, pix_key: str, pix_key_type: str) -> SimulationResult:
        _check_pix_key_type(pix_key_type)
        bal = (await self.get_balance()).available_cents
        fee = FAKE_WITHDRAWAL_FEE_CENTS
        ok = bal >= amount_cents
        return SimulationResult(amount_cents, fee, max(amount_cents - fee, 0), bal, ok, True, ok, False)

    async def create_withdrawal(self, *, amount_cents: int, pix_key: str, pix_key_type: str) -> WithdrawalResult:
        _check_pix_key_type(pix_key_type)
        ext = f"wd_fake_{uuid.uuid4().hex[:16]}"
        fee = FAKE_WITHDRAWAL_FEE_CENTS
        res = WithdrawalResult(secrets.randbelow(10**6) + 1, ext, "processing", amount_cents, fee,
                               max(amount_cents - fee, 0), f"fake_transfer_{uuid.uuid4().hex[:8]}")
        r = await self._redis()
        await r.hset(self._key("wd", ext), mapping={"status": "processing", "valor": amount_cents, "taxa": fee})
        await r.sadd(self._key("wd_index"), ext)  # registro do fake (extrato do ``/saldo``); sem expiração
        return res

    async def get_withdrawal(self, external_id: str) -> WithdrawalResult:
        r = await self._redis()
        h = await r.hgetall(self._key("wd", _validate_external_id(external_id)))
        if not h:
            raise ProviderBadRequest(details={"provider_message": "saque inexistente (fake)"}, provider_status=404)
        amount, fee = int(h["valor"]), int(h["taxa"])
        return WithdrawalResult(None, external_id, h["status"], amount, fee, max(amount - fee, 0),
                                failure_reason=h.get("failure_reason"))

    async def set_withdrawal_status(self, external_id: str, status: str, failure_reason: Optional[str] = None) -> None:
        if status not in WITHDRAWAL_STATUSES:
            raise ValueError("status inválido")
        r = await self._redis()
        await r.hset(self._key("wd", external_id), mapping={"status": status, **(
            {"failure_reason": failure_reason} if failure_reason else {})})

    async def create_withdrawal_copypaste(self, *, pix_payload: str, amount_cents: Optional[int] = None,
                                          withdrawal_password: Optional[str] = None) -> WithdrawalResult:
        raise ProviderForbidden(details={"provider_message": "copypaste exige gateway PAYGATE (fake)"},
                                provider_status=403)

    async def block_balance(self, *, amount_cents: int, external_id: str, description: str) -> dict:
        _validate_external_id(external_id)
        return {"block_id": f"apiblk_fake_{external_id}", "external_id": external_id, "operation": "block",
                "amount_cents": amount_cents, "status": "completed", "idempotent_replay": False}

    async def unblock_balance(self, *, amount_cents: int, external_id: str, description: str) -> dict:
        _validate_external_id(external_id)
        return {"block_id": f"apiblk_fake_{external_id}", "external_id": external_id, "operation": "unblock",
                "amount_cents": amount_cents, "status": "completed", "idempotent_replay": False}

    async def refund_charge(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("charge_refund")

    async def get_charge(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("charge_query")

    async def list_charges(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("charge_list")

    async def create_boleto(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("charge_boleto")

    async def open_dispute(self, *args: Any, **kw: Any) -> Any:
        raise capability_unavailable("chargeback_dispute")

    # ── webhook assinado (mesmo algoritmo da doc) ──
    def build_webhook(
        self,
        *,
        status: str,
        external_ref: str,
        order_id: Optional[str],
        amount_cents: int,
        fee_cents: int,
        webhook_event_id: Optional[int] = None,
        transaction_id: Optional[str] = None,
        internal_transaction_id: Optional[int] = None,
        method: str = "pix",
        subconta_id: Optional[int] = None,
    ) -> tuple[bytes, dict]:
        if status not in CHARGE_STATUSES:
            raise ValueError("status inválido")
        if not self.config.webhook_secret:
            raise ProviderConfigError(details={"missing": "webhook_secret"})
        payload = build_charge_webhook_payload(
            status=status, external_ref=external_ref, order_id=order_id, amount_cents=amount_cents,
            fee_cents=fee_cents, webhook_event_id=webhook_event_id or (secrets.randbelow(2**52) + 1),
            transaction_id=transaction_id or f"fake_{uuid.uuid4().hex[:20]}",
            internal_transaction_id=internal_transaction_id or (secrets.randbelow(10**9) + 1),
            method=method, subconta_id=self.config.subconta_id if subconta_id is None else subconta_id,
        )
        body = dumps_exact(payload)
        # a CloudPayPag assina o dict já desserializado; reproduzimos parseando o corpo
        signed = signature_header_value(json.loads(body), self.config.webhook_secret)
        return body.encode("utf-8"), {"Content-Type": "application/json", SIGNATURE_HEADER: signed}


def build_charge_webhook_payload(*, status: str, external_ref: str, order_id: Optional[str], amount_cents: int,
                                 fee_cents: int, webhook_event_id: int, transaction_id: str,
                                 internal_transaction_id: int, method: str, subconta_id: Optional[int]) -> dict:
    """Payload no formato do exemplo PAID da documentação (valores em reais, ``Decimal``)."""
    amount = cents_to_reais(amount_cents)
    fee = cents_to_reais(fee_cents)
    payload: dict = {
        "transaction_id": transaction_id,
        "external_ref": external_ref,
        "order_id": order_id,
        "method": method,
        "amount": amount,
        "fee": fee,
        "net_value": amount - fee,
        "currency": "BRL",
        "status": status,
        "pix_end_to_end_id": (f"E{secrets.token_hex(15).upper()}"[:32] if status == "paid" and method == "pix" else None),
        "cloudpaypag": {
            "version": 1,
            "webhook_event_id": webhook_event_id,
            "internal_transaction_id": internal_transaction_id,
            "subconta_id": subconta_id,
        },
    }
    if order_id is None:
        payload.pop("order_id")
    return payload


# ─── Fábrica ─────────────────────────────────────────────────────────────────

_provider_override: Optional[Any] = None


def set_provider_override(provider: Optional[Any]) -> None:
    """Somente testes: injeta um provedor (ex.: cliente real com ``transport`` do respx)."""
    global _provider_override
    _provider_override = provider


def get_provider(instance_slug: Optional[str] = None) -> PaymentProvider:
    if _provider_override is not None:
        return _provider_override
    cfg = get_config(instance_slug)
    if cfg.mode == "fake":
        if not fake_allowed():
            raise ProviderConfigError("PROVIDER_FAKE_FORBIDDEN", "Provedor falso proibido neste ambiente.")
        return FakeCloudPayPag(cfg)
    return CloudPayPagClient(cfg)


def provider_status() -> dict:
    """Situação da integração para o painel (sem segredos)."""
    cfg = get_config()
    return {
        "provider": "cloudpaypag",
        "instance_slug": cfg.instance_slug,
        "mode": cfg.mode.upper(),
        "base_url": cfg.api_base,
        "token_configured": bool(cfg.token),
        "webhook_secret_configured": bool(cfg.webhook_secret),
        "subconta_id": cfg.subconta_id,
        "external_validation": "NÃO VALIDADA EXTERNAMENTE (credencial de sandbox não configurada)",
        "capabilities": {k: ("DISPONÍVEL" if v else "BLOQUEADO POR DEPENDÊNCIA EXTERNA") for k, v in CAPABILITIES.items()},
        "fake_warning": ("PROVEDOR FALSO (desenvolvimento) — nenhuma movimentação real" if cfg.mode == "fake" else None),
    }
