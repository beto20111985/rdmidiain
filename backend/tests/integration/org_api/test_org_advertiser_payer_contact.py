"""AÇÃO 004 (decisão do proprietário) — telefone OBRIGATÓRIO no cadastro de empresa que COMPRA (anunciante).

Achado: o checkout de "PRIMEIRA DO BRITAS BAR" deu 422 ``PAYER_DATA_INCOMPLETE`` (``missing=["phone"]``) porque
o cadastro da empresa foi concluído sem telefone. A fonte de verdade dos dados exigidos do pagador é
``financial.payments.payer_from_org`` (e-mail, telefone com DDD — ≥ 10 dígitos — e CPF/CNPJ). O cadastro de
empresa com papel ADVERTISER passa a exigir o MESMO conjunto (mesma função, sem duplicar a regra).

Não retroativo: empresas antigas sem telefone continuam legíveis/editáveis e só são barradas no pagamento.
PARTNER/AGENCY seguem a regra anterior (quem paga é a organização DONA da campanha — o anunciante).
"""
import uuid

from sqlalchemy import select

from app.models.organizations import Organization
from app.modules.financial import payments
from tests.integration.org_api.helpers import mk_org, new_cnpj

ORGS = "/api/v1/organizations"


def _body(**kw):
    b = {"name": "Britas Teste LTDA", "document": new_cnpj(), "business_roles": ["ADVERTISER"], "city": "Brasília",
         "state": "DF", "email": "financeiro@britas-teste.com.br", "phone": "(61) 99999-0000"}
    b.update(kw)
    return {k: v for k, v in b.items() if v is not None}


def _field_errors(r) -> dict:
    return {".".join(str(p) for p in e["loc"] if p != "body"): e["msg"] for e in r.json()["error"]["details"]["errors"]}


async def test_criar_anunciante_sem_telefone_ou_email_422_com_erro_de_campo(client, db, admin):
    h = admin.headers
    r = await client.post(ORGS, json=_body(phone=None), headers=h)
    assert r.status_code == 422, r.text  # antes: 201 — cadastro concluído sem o dado exigido no pagamento
    err = r.json()["error"]
    assert err["code"] == "ADVERTISER_PAYER_DATA_REQUIRED" and err["details"]["missing"] == ["phone"]
    assert "telefone" in _field_errors(r)["phone"].lower()
    # telefone sem DDD (8 dígitos) não serve para o pagamento ⇒ também barrado no cadastro
    r = await client.post(ORGS, json=_body(phone="9999-0000"), headers=h)
    assert r.status_code == 422 and r.json()["error"]["details"]["missing"] == ["phone"]
    r = await client.post(ORGS, json=_body(email=None), headers=h)
    assert r.status_code == 422 and r.json()["error"]["details"]["missing"] == ["email"]
    assert "e-mail" in _field_errors(r)["email"].lower()
    r = await client.post(ORGS, json=_body(email=None, phone=None, business_roles=["ADVERTISER", "PARTNER"]), headers=h)
    assert r.status_code == 422 and r.json()["error"]["details"]["missing"] == ["email", "phone"]
    assert (await db.execute(select(Organization).where(Organization.name == "Britas Teste LTDA"))).first() is None


async def test_criar_anunciante_completo_201_e_o_pagamento_aceita_o_mesmo_cadastro(client, db, admin):
    r = await client.post(ORGS, json=_body(), headers=admin.headers)
    assert r.status_code == 201, r.text
    org_id = uuid.UUID(r.json()["id"])
    # MESMA regra do pagamento: o cadastro aceito não cai em PAYER_DATA_INCOMPLETE (sem usuário de apoio)
    await payments.ensure_payer_supported(db, org_id, None)


async def test_parceiro_e_agencia_seguem_a_regra_anterior(client, db, admin):
    h = admin.headers
    r = await client.post(ORGS, json=_body(business_roles=["PARTNER"], email=None, phone=None), headers=h)
    assert r.status_code == 201, r.text
    r = await client.post(ORGS, json=_body(business_roles=["AGENCY"], email=None, phone=None), headers=h)
    assert r.status_code == 201, r.text


async def test_atualizar_anunciante_nao_apaga_telefone_e_nao_e_retroativo(client, db, admin):
    h = admin.headers
    org_id = (await client.post(ORGS, json=_body(), headers=h)).json()["id"]
    r = await client.patch(f"{ORGS}/{org_id}", json={"phone": ""}, headers=h)
    assert r.status_code == 422, r.text  # antes: 200 e o telefone virava NULL
    assert r.json()["error"]["details"]["missing"] == ["phone"] and "phone" in _field_errors(r)
    r = await client.patch(f"{ORGS}/{org_id}", json={"phone": "1234"}, headers=h)
    assert r.status_code == 422
    org = (await db.execute(select(Organization).where(Organization.id == uuid.UUID(org_id))
                            .execution_options(populate_existing=True))).scalar_one()
    assert org.phone == "(61) 99999-0000"

    # empresa ANTIGA de anunciante sem telefone e sem e-mail: continua legível e editável…
    legacy = await mk_org(db, roles=("ADVERTISER",))
    assert (await client.get(f"{ORGS}/{legacy.id}", headers=h)).status_code == 200
    r = await client.patch(f"{ORGS}/{legacy.id}", json={"trade_name": "Nome Fantasia"}, headers=h)
    assert r.status_code == 200, r.text
    # …e completar SÓ o telefone (o que o pagamento pediu) não exige outros campos
    r = await client.patch(f"{ORGS}/{legacy.id}", json={"phone": "(61) 3333-4444"}, headers=h)
    assert r.status_code == 200, r.text


async def test_passar_a_ser_anunciante_exige_os_dados_do_pagador(client, db, admin):
    h = admin.headers
    partner = await mk_org(db, roles=("PARTNER",))
    r = await client.patch(f"{ORGS}/{partner.id}", json={"business_roles": ["PARTNER", "ADVERTISER"]}, headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["missing"] == ["email", "phone"]
    r = await client.patch(f"{ORGS}/{partner.id}", json={"business_roles": ["PARTNER", "ADVERTISER"],
                                                         "email": "contato@parceiro.com.br", "phone": "62999990000"},
                           headers=h)
    assert r.status_code == 200, r.text


async def test_cadastro_publico_de_anunciante_exige_telefone_com_ddd(client, db, mail, storage):
    """O auto-cadastro já exigia telefone, mas aceitava 8 dígitos (sem DDD) — que o pagamento recusa."""
    from tests.integration.org_api.test_org_advertiser_signup import HDR, _body as signup_body, _publish_terms, \
        _segments, _start, _token

    terms = await _publish_terms(client, db)
    ids = await _segments(db)
    email = f"adv{uuid.uuid4().hex[:6]}@example.com"
    await _start(client, email)
    tok = _token(mail, email)
    body = signup_body(terms, [ids["segment:snacks"]], phone="9999-8888")
    r = await client.post("/api/v1/advertiser-signup/complete", json=body, headers={HDR: tok})
    assert r.status_code == 422, r.text  # antes: 201 com telefone que o pagamento recusa
    assert r.json()["error"]["details"]["missing"] == ["phone"]
    assert "organization.phone" in _field_errors(r)
    body["organization"]["phone"] = "(62) 99999-8888"  # token não foi consumido pela validação
    r = await client.post("/api/v1/advertiser-signup/complete", json=body, headers={HDR: tok})
    assert r.status_code == 201, r.text
