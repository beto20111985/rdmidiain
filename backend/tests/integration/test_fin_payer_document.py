"""Correção pós-janela (W-DB): documento do pagador / chaves PIX sem "strip" silencioso.

A documentação pública da CloudPayPag (snapshot L155–L156, L657) aceita ``customer.document``
apenas como CPF=11 dígitos / CNPJ=14 dígitos, sem máscara. CNPJ ALFANUMÉRICO (IN RFB
2.229/2024, válido no cadastro do W-ORG) NÃO é enviado adulterado: falha cedo e claro
(422 ``PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY``) antes de qualquer gravação/chamada.
Regressão: CPF e CNPJ numérico continuam funcionando (com e sem máscara)."""
import json
import secrets
import uuid

import httpx
import pytest
import respx
from sqlalchemy import func, select, update

from app.core.authz import AuthContext
from app.core.errors import DomainError
from app.models.financial import PaymentOrder
from app.models.organizations import Organization
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import ledger, payments
from app.modules.financial.withdrawals import normalize_pix_key
from app.modules.organizations.documents import make_cnpj, make_cpf
from tests.fin_helpers import fin_env, media_item, provider_config  # noqa: F401

CPF = make_cpf("123456789")                       # 12345678909
CNPJ_NUM = make_cnpj("112223330001")              # 14 dígitos
CNPJ_ALNUM = make_cnpj("12ABC345" + "01DE")       # alfanumérico válido (DV numérico)
BASE = "https://cloudpaypag.top/api/v1/integration"


def _masked_cnpj(d: str) -> str:
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


@pytest.mark.parametrize("raw,dtype,expected", [
    (CPF, "CPF", CPF), (f"{CPF[:3]}.{CPF[3:6]}.{CPF[6:9]}-{CPF[9:]}", "CPF", CPF),
    (CNPJ_NUM, "CNPJ", CNPJ_NUM), (_masked_cnpj(CNPJ_NUM), "CNPJ", CNPJ_NUM),
])
def test_cpf_and_numeric_cnpj_keep_working(raw, dtype, expected):
    assert payments.gateway_payer_document(raw, dtype) == (expected, dtype)


@pytest.mark.parametrize("raw", [CNPJ_ALNUM, _masked_cnpj(CNPJ_ALNUM), _masked_cnpj(CNPJ_ALNUM).lower()])
def test_alphanumeric_cnpj_is_not_sent_mutilated(raw):
    with pytest.raises(DomainError) as ei:
        payments.gateway_payer_document(raw, "CNPJ")
    assert ei.value.code == "PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY" and ei.value.status == 422
    assert ei.value.details["status"] == "BLOQUEADO POR DEPENDÊNCIA EXTERNA"


@pytest.mark.parametrize("raw,dtype", [(CPF + "X", "CPF"), ("1234567890A", "CPF"), (CNPJ_NUM[:-1] + "Z", "CNPJ"),
                                       (CNPJ_NUM + "9", "CNPJ")])
def test_other_characters_are_rejected_not_stripped(raw, dtype):
    with pytest.raises(DomainError) as ei:
        payments.gateway_payer_document(raw, dtype)
    assert ei.value.code == "PAYER_DOCUMENT_INVALID"


def test_pix_keys_no_silent_strip():
    assert normalize_pix_key("cpf", f"{CPF[:3]}.{CPF[3:6]}.{CPF[6:9]}-{CPF[9:]}") == CPF
    assert normalize_pix_key("cnpj", _masked_cnpj(CNPJ_NUM)) == CNPJ_NUM
    assert normalize_pix_key("phone", "+55 (62) 99999-0001") == "+5562999990001"
    with pytest.raises(DomainError) as ei:
        normalize_pix_key("cnpj", _masked_cnpj(CNPJ_ALNUM))
    assert ei.value.code == "PIX_KEY_CNPJ_ALPHANUMERIC_NOT_SUPPORTED" and ei.value.status == 422
    for t, v in (("cnpj", CNPJ_NUM + "X"), ("cpf", "123.456.789-0A"), ("phone", "62 9999A0001")):
        with pytest.raises(DomainError) as ei:
            normalize_pix_key(t, v)
        assert ei.value.code == "PIX_KEY_INVALID", (t, v)


async def _org(db, document, dtype):
    org = Organization(id=uuid.uuid4(), name=f"Pagador {secrets.token_hex(3)}", document=document, document_type=dtype,
                       email=f"p{secrets.token_hex(3)}@ex.com", phone="(62) 99999-0001", business_roles=["ADVERTISER"],
                       is_active=True)
    db.add(org)
    await db.commit()
    return org


async def test_payment_order_three_formats_and_no_artifact_for_alphanumeric(db, fin_env):
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    today = ledger.today_local()
    with respx.mock:
        route = respx.post(f"{BASE}/charges").mock(side_effect=lambda req: httpx.Response(200, json={
            "transaction_id": secrets.randbelow(10**6) + 1, "order_id": json.loads(req.content)["order_id"],
            "external_ref": f"sc_2_{uuid.uuid4().hex[:12]}", "processor_reference_id": None, "payment_method": "pix",
            "status": "pending", "amount": 10.00, "pix_end_to_end_id": None, "created_at": None, "next_action": None,
            "attempts_count": 1, "method_data": {"qr_code": "000201"}}))
        for doc, dtype, expected in ((f"{CPF[:3]}.{CPF[3:6]}.{CPF[6:9]}-{CPF[9:]}", "CPF", CPF),
                                     (CNPJ_NUM, "CNPJ", CNPJ_NUM)):
            org = await _org(db, doc, dtype)
            o = await payments.create_payment_order(db, AuthContext.system("system"), org.id,
                                                    [media_item(uuid.uuid4(), 1000, today, today)], 1000, None,
                                                    ("test_ref", uuid.uuid4().hex))
            await db.commit()
            assert o.status == "PENDING"
            sent = json.loads(route.calls.last.request.content)
            assert sent["customer"]["document"] == {"type": dtype, "number": expected}
            assert sent["customer"]["phone"] == "62999990001"
        calls_before = route.call_count
        org = await _org(db, _masked_cnpj(CNPJ_ALNUM), "CNPJ")
        with pytest.raises(DomainError) as ei:
            await payments.create_payment_order(db, AuthContext.system("system"), org.id,
                                                [media_item(uuid.uuid4(), 1000, today, today)], 1000, None,
                                                ("test_ref", uuid.uuid4().hex))
        await db.rollback()
        assert ei.value.code == "PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY"
        assert route.call_count == calls_before  # nada enviado à CloudPayPag
    assert (await db.execute(select(func.count()).select_from(PaymentOrder).where(
        PaymentOrder.org_id == org.id))).scalar_one() == 0
    with pytest.raises(DomainError):
        await payments.ensure_payer_supported(db, org.id)


async def test_checkout_with_alphanumeric_cnpj_fails_early_without_hold_order_or_charge(db, client, fin_env):
    from app.models.inventory import InventoryHold
    from app.models.pricing import Order
    from tests.integration.test_camp_lifecycle import checkout, new_campaign, status_of, world

    t, org, ests, screens, s = await world(db, client)
    await db.execute(update(Organization).where(Organization.id == org.id).values(
        email="fin@anunciante.com.br", phone="62999990001", document=CNPJ_ALNUM, document_type="CNPJ"))
    await db.commit()
    cid = await new_campaign(client, s, db=db, org=org)
    r = await checkout(client, s, cid)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY" and "suporte" in err["message"]
    assert (await status_of(db, cid)).status == "RASCUNHO"
    for model in (Order, InventoryHold, PaymentOrder):
        assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0, model.__name__


async def test_pix_holder_with_alphanumeric_cnpj_is_supported(db, client, fin_env):
    from tests.factories import login_as, make_establishment, make_org, make_user

    est = await make_establishment(db, await make_org(db, business_roles=["PARTNER"]))
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    url = f"/api/v1/financial/establishments/{est.id}/pix-keys"
    base = {"key_type": "email", "key_value": "parceiro@ex.com", "holder_name": "Parceiro Ltda",
            "reason": "cadastro inicial"}
    r = await client.post(url, json={**base, "holder_document": _masked_cnpj(CNPJ_ALNUM).lower()}, headers=sa.headers)
    assert r.status_code == 201, r.text
    assert r.json()["holder_document_masked"].endswith(CNPJ_ALNUM[-2:])
    bad = CNPJ_ALNUM[:-1] + str((int(CNPJ_ALNUM[-1]) + 1) % 10)
    r = await client.post(url, json={**base, "holder_document": bad}, headers=sa.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "PIX_HOLDER_DOCUMENT_INVALID"
    r = await client.post(url, json={**base, "key_type": "cnpj", "key_value": CNPJ_ALNUM, "holder_document": CNPJ_ALNUM},
                          headers=sa.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "PIX_KEY_CNPJ_ALPHANUMERIC_NOT_SUPPORTED"
