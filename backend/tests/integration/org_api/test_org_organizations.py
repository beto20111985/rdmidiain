"""W-ORG — organizações: papéis explícitos, CPF/CNPJ, escopo, cascata de inativação, agência, auditoria."""
import uuid

from sqlalchemy import select

from app.models.organizations import AgencyClient, Organization, OrganizationTypeFlag
from app.models.rbac import RoleAssignment
from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org, new_cnpj, new_cpf, staff_session


async def test_create_org_validates_documents_and_roles(client, db, admin):
    h = admin.headers
    # AÇÃO 004: anunciante exige no cadastro os dados do pagador (e-mail e telefone com DDD)
    base = {"name": "Mercado Bom Preço LTDA", "business_roles": ["ADVERTISER", "PARTNER"], "city": "Goiânia",
            "state": "GO", "email": "contato@bompreco.com.br", "phone": "(62) 99999-0000"}
    r = await client.post("/api/v1/organizations", json={**base, "document": "11.222.333/0001-80"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "DOCUMENT_INVALID"
    r = await client.post("/api/v1/organizations", json={**base, "document": "111.111.111-11"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "DOCUMENT_INVALID"
    r = await client.post("/api/v1/organizations", json={**base, "document": new_cnpj(), "business_roles": ["FOO"]},
                          headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "BUSINESS_ROLE_INVALID"
    r = await client.post("/api/v1/organizations", json={**base, "document": new_cnpj(), "business_roles": []},
                          headers=h)
    assert r.status_code == 422

    doc = new_cnpj()
    masked = f"{doc[:2]}.{doc[2:5]}.{doc[5:8]}/{doc[8:12]}-{doc[12:]}"
    r = await client.post("/api/v1/organizations", json={**base, "document": masked}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["document"] == doc and body["document_type"] == "CNPJ" and body["person_type"] == "PJ"
    assert body["business_roles"] == ["ADVERTISER", "PARTNER"]
    assert body["business_role_labels"] == ["Anunciante", "Parceiro (estabelecimento)"]
    assert body["city_key"] == "GO:goiania"
    # duplicado
    r = await client.post("/api/v1/organizations", json={**base, "document": doc}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_ALREADY_REGISTERED"
    # PF com CPF
    cpf = new_cpf()
    r = await client.post("/api/v1/organizations", json={"name": "João da Silva", "document": cpf,
                                                         "business_roles": ["ADVERTISER"],
                                                         "email": "joao@exemplo.com.br", "phone": "62999990001"},
                          headers=h)
    assert r.status_code == 201 and r.json()["person_type"] == "PF"
    # alfanumérico (Receita 2026)
    r = await client.post("/api/v1/organizations", json={"name": "Nova Empresa", "document": "12.ABC.345/01DE-35",
                                                         "business_roles": ["AGENCY"]}, headers=h)
    assert r.status_code == 201 and r.json()["document"] == "12ABC34501DE35"
    assert "organization.created" in await audit_actions(db, body["id"])


async def test_legacy_bitmask_shim_maps_to_explicit_roles(db):
    o = Organization(id=uuid.uuid4(), name="Legado", document=new_cnpj(), document_type="CNPJ",
                     type=OrganizationTypeFlag.IS_ADVERTISER.value | OrganizationTypeFlag.IS_ESTABLISHMENT_OWNER.value)
    db.add(o)
    await db.commit()
    o2 = await db.get(Organization, o.id, populate_existing=True)
    assert o2.business_roles == ["ADVERTISER", "PARTNER"]
    assert o2.type == 3  # leitura do shim: 1 (anunciante) | 2 (parceiro)


async def test_business_roles_check_constraint_in_database(db):
    import pytest
    from sqlalchemy.exc import IntegrityError

    db.add(Organization(id=uuid.uuid4(), name="X", document=new_cnpj(), document_type="CNPJ",
                        business_roles=["ADVERTISER", "HACKER"]))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_city_scoped_sales_sees_only_its_city_and_404_elsewhere(client, db):
    gyn = await mk_org(db, "Org Goiânia", city="Goiânia", state="GO")
    anp = await mk_org(db, "Org Anápolis", city="Anápolis", state="GO")
    sales = await staff_session(client, db, "sales", ("CITY", "GO:goiania"))
    r = await client.get("/api/v1/organizations", headers=sales.headers)
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    assert ids == {str(gyn.id)}
    assert (await client.get(f"/api/v1/organizations/{anp.id}", headers=sales.headers)).status_code == 404
    assert (await client.patch(f"/api/v1/organizations/{anp.id}", json={"phone": "62999990000"},
                               headers=sales.headers)).status_code == 404
    assert (await client.get(f"/api/v1/organizations/{gyn.id}", headers=sales.headers)).status_code == 200
    # cria só na própria cidade
    r = await client.post("/api/v1/organizations", json={"name": "Fora", "document": new_cnpj(),
                                                         "business_roles": ["ADVERTISER"], "city": "Anápolis",
                                                         "state": "GO"}, headers=sales.headers)
    assert r.status_code == 403
    r = await client.post("/api/v1/organizations", json={"name": "Dentro", "document": new_cnpj(),
                                                         "business_roles": ["ADVERTISER"], "city": "Goiânia",
                                                         "state": "GO", "email": "dentro@exemplo.com.br",
                                                         "phone": "62999990002"}, headers=sales.headers)
    assert r.status_code == 201


async def test_advertiser_has_no_org_listing_and_org_a_cannot_touch_org_b(client, db):
    org_a, org_b = await mk_org(db, roles=("ADVERTISER",)), await mk_org(db, roles=("ADVERTISER",))
    adv_a = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_a.id))
    s = await login_as(client, adv_a)
    assert (await client.get("/api/v1/organizations", headers=s.headers)).status_code == 403
    assert (await client.get(f"/api/v1/organizations/{org_b.id}", headers=s.headers)).status_code == 403


async def test_deactivate_org_cascades_to_establishments_and_removal_listener(client, db, admin):
    from app.models.screens import Screen
    from app.modules.screens.service import register_screen_removed_listener, unregister_screen_removed_listener

    org = await mk_org(db)
    est = await mk_est(db, org)
    scr = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id="TEL-GO-ZZ22", name="T1")
    db.add(scr)
    await db.commit()
    calls = []

    async def listener(_db, ids, reason):
        calls.append((list(ids), reason))

    register_screen_removed_listener(listener)
    try:
        r = await client.post(f"/api/v1/organizations/{org.id}/deactivate", json={"reason": "curto"},
                              headers=admin.headers)
        assert r.status_code == 422
        r = await client.post(f"/api/v1/organizations/{org.id}/deactivate",
                              json={"reason": "Encerramento do contrato de parceria"}, headers=admin.headers)
        assert r.status_code == 200, r.text
        assert r.json()["establishments_deactivated"] == [str(est.id)]
    finally:
        unregister_screen_removed_listener(listener)
    assert calls == [([scr.id], "ESTABLISHMENT_DEACTIVATED")]
    s2 = await db.get(Screen, scr.id, populate_existing=True)
    assert s2.sellable is False
    acts = await audit_actions(db)
    assert "organization.deactivated" in acts and "establishment.deactivated" in acts


async def test_agency_link_grants_and_revokes_scope(client, db, admin):
    agency = await mk_org(db, "Agência X", roles=("AGENCY",))
    client_org = await mk_org(db, "Cliente Y", roles=("ADVERTISER",))
    not_adv = await mk_org(db, "Só parceiro", roles=("PARTNER",))
    ag_user = await make_user(db, role="advertiser", scope=("ORGANIZATION", agency.id))
    outsider = await make_user(db, role="advertiser", scope=("ORGANIZATION", client_org.id))
    h = admin.headers
    r = await client.post(f"/api/v1/organizations/{agency.id}/clients", json={"client_org_id": str(not_adv.id)},
                          headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "ORG_NOT_ADVERTISER"
    r = await client.post(f"/api/v1/organizations/{agency.id}/clients", json={"client_org_id": str(client_org.id)},
                          headers=h)
    assert r.status_code == 201, r.text
    link_id = r.json()["id"]
    r = await client.post(f"/api/v1/organizations/{agency.id}/clients", json={"client_org_id": str(client_org.id)},
                          headers=h)
    assert r.status_code == 409
    # usuário que não é da agência
    r = await client.post(f"/api/v1/organizations/{agency.id}/clients/{link_id}/grants",
                          json={"user_id": str(outsider.id)}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "USER_NOT_AGENCY_MEMBER"
    r = await client.post(f"/api/v1/organizations/{agency.id}/clients/{link_id}/grants",
                          json={"user_id": str(ag_user.id)}, headers=h)
    assert r.status_code == 201, r.text
    s = await login_as(client, ag_user)
    me = (await client.get("/api/v1/me", headers=s.headers)).json()
    scopes = {(a["role"], a["scope_type"], a["scope_id"]) for a in me["assignments"]}
    assert ("agency", "ORGANIZATION", str(client_org.id)) in scopes
    # encerra → atribuição removida
    r = await client.post(f"/api/v1/organizations/{agency.id}/clients/{link_id}/end",
                          json={"reason": "Fim do contrato com a agência"}, headers=h)
    assert r.status_code == 200 and r.json()["assignments_revoked"] == 1
    left = (await db.execute(select(RoleAssignment).where(RoleAssignment.user_id == ag_user.id,
                                                          RoleAssignment.scope_id == client_org.id))).all()
    assert left == []
    link = await db.get(AgencyClient, uuid.UUID(link_id), populate_existing=True)
    assert link.status == "ENDED"
    acts = await audit_actions(db, link_id)
    assert acts == ["agency.client_linked", "agency.access_granted", "agency.client_unlinked"]


async def test_establishment_owner_of_org_a_gets_404_on_org_b_establishment_via_org_endpoints(client, db):
    org_a, org_b = await mk_org(db), await mk_org(db)
    await mk_est(db, org_a)
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org_a.id))
    s = await login_as(client, owner)
    # perfil de estabelecimento não lista organizações (sem organizations.view)
    assert (await client.get(f"/api/v1/organizations/{org_b.id}", headers=s.headers)).status_code == 403
