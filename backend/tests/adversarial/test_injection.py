"""Adversarial: injeção, IDOR, valores negativos, upload malicioso, mock de pagamento.

Reescrito na AÇÃO 002 (W1): o original dependia de uma fixture ``admin_token`` que
forjava JWT com módulos inexistentes e usava asserções permissivas
(``in (404, 403, 422)``). Agora: login real de Super Admin (com 2FA) e códigos exatos.
Falhas causadas por defeitos de DOMÍNIO conhecidos ficam como ``xfail(strict=True)``
com referência ao achado — quando o domínio for corrigido o teste passa a XPASS e o
marcador DEVE ser removido (strict faz a suíte falhar até isso). Histórico: o xfail do
upload executável foi removido quando o upload novo do W-MEDIA passou a rejeitar o
arquivo (XPASS confirmado em 2026-10-07).
"""
from io import BytesIO
from uuid import uuid4

import pytest

from tests.factories import login_as, make_user


@pytest.fixture
async def admin(client, db):
    u = await make_user(db, role="super_admin", mfa=True)
    return await login_as(client, u)


async def test_sql_injection_in_query_params_is_harmless(client, admin):
    for payload in ["'; DROP TABLE campaigns; --", "1 OR 1=1", "' UNION SELECT * FROM users --"]:
        r = await client.get("/api/v1/campaigns", params={"campaign_status": payload}, headers=admin.headers)
        assert r.status_code in (200, 422), (payload, r.status_code)
        r = await client.get("/api/v1/users", params={"search": payload}, headers=admin.headers)
        assert r.status_code == 200 and r.json()["total"] == 0
        r = await client.get("/api/v1/audit", params={"action": payload}, headers=admin.headers)
        assert r.status_code == 200


async def test_uuid_idor_nonexistent_resource_is_404(client, admin):
    rid = uuid4()
    for path in (f"/api/v1/campaigns/{rid}", f"/api/v1/screens/{rid}", f"/api/v1/users/{rid}"):
        r = await client.get(path, headers=admin.headers)
        assert r.status_code == 404, (path, r.status_code)


async def test_malformed_uuid_is_422_envelope(client, admin):
    r = await client.get("/api/v1/users/not-a-uuid", headers=admin.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_negative_amount_cents_rejected(client, admin):
    """Contrato novo (W-FIN/W-CAMP): a cobrança só nasce no checkout com preço calculado no
    servidor — não existe rota que aceite valor do cliente; valores monetários de entrada
    (saque/despesa) só aceitam inteiro > 0 (negativo, zero, float, texto, bool ⇒ 422)."""
    # rota legada que aceitava org_id/amount_cents livres: removida
    r = await client.post("/api/v1/financial/payments", json={
        "org_id": str(uuid4()), "amount_cents": -100, "description": "hack attempt",
    }, headers=admin.headers)
    assert r.status_code == 404
    # checkout: preço enviado pelo cliente é REJEITADO (schema estrito), nunca usado
    r = await client.post(f"/api/v1/campaigns/{uuid4()}/checkout", json={"amount_cents": 1}, headers=admin.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    for bad in (-100, 0, 10.5, "100", True):
        r = await client.post("/api/v1/financial/withdrawals", json={
            "establishment_id": str(uuid4()), "amount_cents": bad, "totp_code": "123456", "financial_password": "x",
        }, headers={**admin.headers, "Idempotency-Key": str(uuid4())})
        assert r.status_code == 422, (bad, r.status_code)
        r = await client.post("/api/v1/financial/withdrawals/simulate", json={
            "establishment_id": str(uuid4()), "amount_cents": bad}, headers=admin.headers)
        assert r.status_code == 422, (bad, r.status_code)


async def test_executable_upload_rejected(client, admin):
    r = await client.post(
        "/api/v1/media/upload", params={"org_id": str(uuid4())},
        files={"file": ("hack.exe", BytesIO(b"MZ\x90\x00"), "application/x-msdownload")}, headers=admin.headers,
    )
    assert r.status_code in (400, 415, 422)


async def test_mock_confirm_blocked_when_mock_disabled(client, admin, db, monkeypatch):
    """Contrato novo (W-FIN): não existe confirmação de pagamento por mock. A simulação de
    webhook só existe em ENVIRONMENT=development + provedor falso + permissão GLOBAL, e
    um webhook com assinatura falsa é recusado (401) mesmo em development."""
    from app.core.config import settings
    from app.modules.financial import cloudpaypag as cpp
    from tests.factories import make_org

    # rota legada removida
    r = await client.post(f"/api/v1/financial/payments/{uuid4()}/mock-confirm", headers=admin.headers)
    assert r.status_code == 404
    # fora de development: a rota de simulação não existe para ninguém (nem super admin)
    r = await client.post("/api/v1/financial/dev/simulate-webhook", json={"payment_order_id": str(uuid4())},
                          headers=admin.headers)
    assert r.status_code == 404
    org = await make_org(db)
    adv = await login_as(client, await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id)))
    cpp.set_config_override(cpp.ProviderConfig(instance_slug="rd", base_url="https://cloudpaypag.top",
                                               token="cpg_test_x", webhook_secret="whsec_" + uuid4().hex,
                                               subconta_id=2, mode="fake"))
    try:
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        # usuário sem permissão global → 403 mesmo em development
        r = await client.post("/api/v1/financial/dev/simulate-webhook", json={"payment_order_id": str(uuid4())},
                              headers=adv.headers)
        assert r.status_code == 403
        # webhook forjado (assinatura falsa) → 401 mesmo em development com provedor falso
        body = b'{"status":"paid","amount":99.9,"cloudpaypag":{"webhook_event_id":42,"subconta_id":2}}'
        r = await client.post("/api/v1/webhooks/cloudpaypag", content=body,
                              headers={"Content-Type": "application/json", "X-Webhook-Signature": "sha256=" + "0" * 64})
        assert r.status_code == 401
        r = await client.post("/api/v1/webhooks/cloudpaypag", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 401
    finally:
        cpp.set_config_override(None)
