"""Adapter CloudPayPag — contrato com o snapshot da documentação pública (sem rede real:
``respx`` intercepta o httpx; token de TESTE gerado por execução)."""
import json
import secrets
from decimal import Decimal

import httpx
import pytest
import respx
import structlog

from app.core.config import settings
from app.modules.financial import cloudpaypag as cpp
from tests.fin_helpers import DOC_EXAMPLES

BASE = "https://cloudpaypag.top/api/v1/integration"
TOKEN = "cpg_test_" + secrets.token_hex(10)
PST = "pst_" + secrets.token_hex(10)  # payment_source_token de teste


def client() -> cpp.CloudPayPagClient:
    return cpp.CloudPayPagClient(cpp.ProviderConfig(instance_slug="rd", base_url="https://cloudpaypag.top", token=TOKEN,
                                                    webhook_secret="s", subconta_id=2, mode="live"))


def doc(name: str) -> dict:
    return json.loads(DOC_EXAMPLES[name]["raw"])


CUSTOMER = cpp.Customer("Maria Oliveira", "maria@empresa.com", "11987654321", "CPF", "98765432100")


# ─── Conversões centavos ↔ reais (Decimal, 2 casas) ──────────────────────────

@pytest.mark.parametrize("cents,reais", [(9990, "99.90"), (1, "0.01"), (100_000_000, "1000000.00")])
def test_money_conversion_both_ways(cents, reais):
    assert cpp.dumps_exact({"amount": cpp.to_reais(cents)}) == '{"amount":' + reais + "}"
    assert cpp.from_reais(Decimal(reais)) == cents
    assert cpp.from_reais(reais) == cents


def test_money_conversion_rejects_float_and_extra_decimals():
    with pytest.raises(cpp.ProviderProtocolError):
        cpp.from_reais(99.9)
    with pytest.raises(cpp.ProviderProtocolError):
        cpp.from_reais(Decimal("2.999"))
    with pytest.raises(TypeError):
        cpp.dumps_exact({"amount": 99.9})
    with pytest.raises(Exception):
        cpp.to_reais(0)


# ─── POST /charges ───────────────────────────────────────────────────────────

@respx.mock
async def test_create_charge_pix_request_and_response_match_doc():
    route = respx.post(f"{BASE}/charges").mock(return_value=httpx.Response(200, json=doc("charge_response")))
    res = await client().create_charge(order_id="ORD-2026-001", amount_cents=9990, method="pix", customer=CUSTOMER,
                                       description="Pedido #1234")
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.headers["Content-Type"] == "application/json"
    sent = json.loads(req.content, parse_float=Decimal)
    expected = json.loads(DOC_EXAMPLES["charge_request_pix_full"]["raw"], parse_float=Decimal)
    assert set(sent) == set(expected)
    assert set(sent["customer"]) == set(expected["customer"]) and set(sent["customer"]["document"]) == {"type", "number"}
    assert sent["amount"] == Decimal("99.90") and b'"amount":99.90' in req.content  # exato, sem float
    assert sent["payment_method"] == "pix" and sent["order_id"] == "ORD-2026-001"
    assert "payment_source_token" not in sent
    assert res.transaction_id == 106 and res.external_ref == "sc_2_74adb8287c5a486" and res.status == "pending"
    assert res.amount_cents == 9990 and res.qr_code.startswith("00020101")


@respx.mock
async def test_credit_card_sends_payment_source_token_and_never_logs_it():
    route = respx.post(f"{BASE}/charges").mock(return_value=httpx.Response(200, json=doc("charge_response")))
    with structlog.testing.capture_logs() as logs:
        await client().create_charge(order_id="o1", amount_cents=9990, method="credit_card", customer=CUSTOMER,
                                     description="x", payment_source_token=PST, installments=3)
        respx.post(f"{BASE}/withdrawals").mock(return_value=httpx.Response(401, json={"detail": "token inválido"}))
        with pytest.raises(cpp.ProviderAuthError):
            await client().create_withdrawal(amount_cents=100, pix_key="12345678909", pix_key_type="cpf")
    sent = json.loads(route.calls.last.request.content)
    assert sent["payment_source_token"] == PST and sent["installments"] == 3
    dump = json.dumps(logs, default=str)
    assert logs and TOKEN not in dump and PST not in dump and "Bearer" not in dump
    assert TOKEN not in repr(client().config)


async def test_undocumented_capabilities_are_unavailable():
    c = client()
    for coro in (c.refund_charge("x"), c.get_charge("x"), c.list_charges(), c.create_boleto(), c.open_dispute()):
        with pytest.raises(cpp.ProviderCapabilityUnavailable) as ei:
            await coro
        assert ei.value.status == 501 and ei.value.details["status"] == "BLOQUEADO POR DEPENDÊNCIA EXTERNA"
    with pytest.raises(cpp.ProviderCapabilityUnavailable):
        await c.create_charge(order_id="o", amount_cents=100, method="boleto", customer=CUSTOMER, description="x")


# ─── Erros tipados / retry ───────────────────────────────────────────────────

@pytest.mark.parametrize("status,exc", [
    (400, cpp.ProviderBadRequest), (401, cpp.ProviderAuthError), (403, cpp.ProviderForbidden),
    (409, cpp.ProviderConflict), (502, cpp.ProviderTemporaryError), (503, cpp.ProviderUnavailable),
])
@respx.mock
async def test_error_mapping_and_post_never_retried(status, exc):
    route = respx.post(f"{BASE}/withdrawals").mock(return_value=httpx.Response(status, json={"detail": "erro"}))
    with pytest.raises(exc) as ei:
        await client().create_withdrawal(amount_cents=10000, pix_key="12345678909", pix_key_type="cpf")
    assert ei.value.provider_status == status
    assert route.call_count == 1  # POST sem retry automático


@respx.mock
async def test_post_timeout_is_unknown_and_not_retried():
    route = respx.post(f"{BASE}/withdrawals").mock(side_effect=httpx.ReadTimeout("timeout"))
    with pytest.raises(cpp.ProviderTimeout):
        await client().create_withdrawal(amount_cents=10000, pix_key="12345678909", pix_key_type="cpf")
    assert route.call_count == 1


@respx.mock
async def test_get_is_retried_on_503_and_network_errors(monkeypatch):
    monkeypatch.setattr(cpp, "GET_BACKOFF_S", (0, 0))
    route = respx.get(f"{BASE}/saldo").mock(side_effect=[httpx.Response(503), httpx.ConnectError("x"),
                                                         httpx.Response(200, json=doc("saldo_response"))])
    bal = await client().get_balance()
    assert route.call_count == 3
    assert (bal.available_cents, bal.blocked_cents, bal.total_cents) == (85000, 5000, 90000)


# ─── Saques, simulação, bloqueio, identidade ─────────────────────────────────

@respx.mock
async def test_withdrawal_simulate_query_block_me_parse_doc_examples():
    wd = respx.post(f"{BASE}/withdrawals").mock(return_value=httpx.Response(200, json=doc("withdrawal_response")))
    respx.post(f"{BASE}/withdrawals/simulate").mock(return_value=httpx.Response(200, json=doc("simulate_response")))
    respx.get(f"{BASE}/withdrawals/wd_sc2_4fa3b2c1").mock(
        return_value=httpx.Response(200, json=doc("withdrawal_get_response")))
    blk = respx.post(f"{BASE}/balance/block").mock(return_value=httpx.Response(200, json=doc("block_response")))
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(200, json=doc("me_response")))
    c = client()
    w = await c.create_withdrawal(amount_cents=10000, pix_key="12345678909", pix_key_type="cpf")
    body = json.loads(wd.calls.last.request.content, parse_float=Decimal)
    assert body == {"valor": Decimal("100.00"), "pix_key": "12345678909", "pix_key_type": "cpf"}
    assert (w.external_id, w.status, w.amount_cents, w.fee_cents, w.net_cents) == ("wd_sc2_4fa3b2c1", "processing",
                                                                                  10000, 200, 9800)
    s = await c.simulate_withdrawal(amount_cents=10000, pix_key="12345678909", pix_key_type="cpf")
    assert (s.fee_cents, s.net_cents, s.available_cents, s.sufficient) == (200, 9800, 85000, True)
    g = await c.get_withdrawal("wd_sc2_4fa3b2c1")
    assert g.status == "completed" and g.net_cents == 9800
    b = await c.block_balance(amount_cents=10000, external_id="reserva-2026-09-01", description="Reserva diária")
    assert json.loads(blk.calls.last.request.content)["external_id"] == "reserva-2026-09-01"
    assert b["api_blocked_cents"] == 10000 and b["idempotent_replay"] is False
    me = await c.get_me()
    assert me == {"subconta_id": 2, "external_id": "abc123xyz", "email": "contato@empresa.com.br", "status": "aprovada"}
    with pytest.raises(ValueError):
        await c.get_withdrawal("../saldo")
    with pytest.raises(ValueError):
        await c.create_withdrawal(amount_cents=1, pix_key="x", pix_key_type="bitcoin")


# ─── Webhook: assinatura conforme a doc ──────────────────────────────────────

def _doc_sign(payload: dict, secret: str) -> str:
    """Cópia literal do algoritmo da documentação (sign_payload)."""
    import hashlib
    import hmac as _h

    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _h.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


@pytest.mark.parametrize("example", ["webhook_paid_example", "hmac_simulator_example"])
def test_signature_of_doc_payload_verifies_and_tampering_fails(example):
    secret = "seu_webhook_secret"
    raw = DOC_EXAMPLES[example]["raw"].encode()  # corpo EXATO do exemplo (indentado, não canônico)
    sig = "sha256=" + _doc_sign(json.loads(raw), secret)
    assert cpp.signature_header_value(json.loads(raw), secret) == sig
    assert cpp.verify_signature(raw, sig, secret) == json.loads(raw)
    assert cpp.verify_signature(raw.replace(b"99.90", b"99.91"), sig, secret) is None  # corpo alterado
    assert cpp.verify_signature(raw, sig, "outro_segredo") is None
    assert cpp.verify_signature(raw, sig[len("sha256="):], secret) is None  # sem prefixo
    assert cpp.verify_signature(raw, None, secret) is None
    raw_sig = "sha256=" + __import__("hmac").new(secret.encode(), raw, "sha256").hexdigest()
    assert cpp.verify_signature(raw, raw_sig, secret) is None  # erro nº 1 da doc: assinar o corpo bruto


def test_parse_doc_paid_webhook_in_cents():
    wp = cpp.parse_webhook(DOC_EXAMPLES["webhook_paid_example"]["raw"].encode())
    assert (wp.webhook_event_id, wp.status, wp.external_ref, wp.order_id) == (42, "paid", "sc_2_74adb8287c5a486",
                                                                              "ORD-2026-001")
    assert (wp.amount_cents, wp.fee_cents, wp.net_cents, wp.currency) == (9990, 299, 9691, "BRL")
    assert (wp.subconta_id, wp.internal_transaction_id, wp.transaction_id) == (2, 106, "cmnyvoo2s0f0y7707k3yjxfch")
    with pytest.raises(cpp.ProviderProtocolError):
        cpp.parse_webhook(b'{"status":"paid","cloudpaypag":{}}')
    with pytest.raises(cpp.ProviderProtocolError):
        cpp.parse_webhook(b'{"status":"weird","cloudpaypag":{"webhook_event_id":1}}')


def test_fake_provider_refused_outside_development_and_test(monkeypatch):
    cfg = cpp.ProviderConfig(instance_slug="rd", base_url="x", token="t", webhook_secret="s", mode="fake")
    cpp.set_config_override(cfg)
    try:
        for env in ("production", "staging"):
            monkeypatch.setattr(settings, "ENVIRONMENT", env)
            with pytest.raises(cpp.ProviderConfigError):
                cpp.FakeCloudPayPag(cfg)
            with pytest.raises(cpp.ProviderConfigError):
                cpp.get_provider()
    finally:
        cpp.set_config_override(None)


def test_production_config_refuses_mock_payment():
    from app.core.config import Settings

    with pytest.raises(Exception) as ei:
        Settings(ENVIRONMENT="production", SECRET_KEY="a" * 40, JWT_SECRET_KEY="b" * 40, POSTGRES_PASSWORD="p",
                 REDIS_PASSWORD="r", MOCK_PAYMENT=True, ACCESS_TOKEN_EXPIRE_MINUTES=15, COOKIE_SECURE=True, OPENAPI_ENABLED=False,
                 CORS_ORIGINS=["https://rd.example"])
    assert "MOCK_PAYMENT" in str(ei.value)
