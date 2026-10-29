"""W-ORG — estabelecimentos: timezone IANA, geografia, escopo ORG/ESTABLISHMENT/CITY, segmentos, audiência estimada."""
import uuid

from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org, staff_session


def _est_body(org_id, **kw):
    b = {"org_id": str(org_id), "name": "Mercado Centro", "responsible_name": "Maria", "phone": "62999990000",
         "address": "Rua 1", "number": "100", "neighborhood": "Centro", "city": "Goiânia", "state": "GO",
         "postal_code": "74.000-000"}
    b.update(kw)
    return b


async def test_create_suggests_timezone_by_uf_and_rejects_invalid(client, db, admin):
    org = await mk_org(db)
    h = admin.headers
    r = await client.post("/api/v1/establishments", json=_est_body(org.id, city="Rio Branco", state="AC"), headers=h)
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["timezone"] == "America/Rio_Branco" and b["timezone_source"] == "AUTO"
    assert b["city_key"] == "AC:rio-branco" and b["region"] == "NORTE" and b["postal_code"] == "74000000"
    assert b["onboarding_status"] == "AWAITING_CONTRACT"

    r = await client.post("/api/v1/establishments", json=_est_body(org.id, timezone="America/Brasilia"), headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "TIMEZONE_INVALID"
    r = await client.post("/api/v1/establishments", json=_est_body(org.id, state="XX"), headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "ADDRESS_INVALID"
    r = await client.post("/api/v1/establishments", json=_est_body(org.id, city="Eirunepé", state="AM"), headers=h)
    assert r.json()["timezone"] == "America/Eirunepe"

    # admin corrige o fuso (MANUAL) — auditado
    eid = b["id"]
    r = await client.patch(f"/api/v1/establishments/{eid}", json={"timezone": "America/Manaus"}, headers=h)
    assert r.status_code == 200 and r.json()["timezone"] == "America/Manaus"
    assert r.json()["timezone_source"] == "MANUAL"
    r = await client.patch(f"/api/v1/establishments/{eid}", json={"timezone": "Mars/Olympus"}, headers=h)
    assert r.status_code == 400
    # mudança de cidade NÃO sobrescreve fuso manual
    r = await client.patch(f"/api/v1/establishments/{eid}", json={"city": "Cuiabá", "state": "MT"}, headers=h)
    assert r.json()["timezone"] == "America/Manaus" and r.json()["city_key"] == "MT:cuiaba"
    acts = await audit_actions(db, eid)
    assert acts.count("establishment.updated") == 2 and acts[0] == "establishment.created"


async def test_geo_manual_and_gps(client, db, admin):
    org = await mk_org(db)
    r = await client.post("/api/v1/establishments", json=_est_body(org.id, lat=-16.68), headers=admin.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "COORDINATES_INCOMPLETE"
    r = await client.post("/api/v1/establishments", json=_est_body(org.id), headers=admin.headers)
    eid = r.json()["id"]
    r = await client.put(f"/api/v1/establishments/{eid}/geo",
                         json={"lat": -16.6869, "lng": -49.2648, "source": "GPS", "accuracy_m": 12},
                         headers=admin.headers)
    assert r.status_code == 200
    b = r.json()
    assert b["lat"] == -16.6869 and b["geo_source"] == "GPS" and b["geo_accuracy_m"] == 12 and b["geo_captured_at"]
    r = await client.put(f"/api/v1/establishments/{eid}/geo", json={"lat": 120, "lng": 0, "source": "GPS"},
                         headers=admin.headers)
    assert r.status_code == 422


async def test_scope_org_establishment_city_and_advertiser(client, db):
    org_a, org_b = await mk_org(db), await mk_org(db)
    a1 = await mk_est(db, org_a, "Goiânia", "GO", name="A1")
    a2 = await mk_est(db, org_a, "Anápolis", "GO", name="A2")
    b1 = await mk_est(db, org_b, "Goiânia", "GO", name="B1")
    owner_a = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org_a.id))
    s = await login_as(client, owner_a)
    r = await client.get("/api/v1/establishments", headers=s.headers)
    assert {i["id"] for i in r.json()["items"]} == {str(a1.id), str(a2.id)}
    assert (await client.get(f"/api/v1/establishments/{b1.id}", headers=s.headers)).status_code == 404
    # sem establishments.manage → 403
    assert (await client.patch(f"/api/v1/establishments/{a1.id}", json={"phone": "1"},
                               headers=s.headers)).status_code == 403

    unit_mgr = await make_user(db, role="establishment_owner", scope=("ESTABLISHMENT", a2.id))
    s = await login_as(client, unit_mgr)
    r = await client.get("/api/v1/establishments", headers=s.headers)
    assert {i["id"] for i in r.json()["items"]} == {str(a2.id)}
    assert (await client.get(f"/api/v1/establishments/{a1.id}", headers=s.headers)).status_code == 404

    tech = await staff_session(client, db, "technician", ("CITY", "GO:goiania"))
    r = await client.get("/api/v1/establishments", headers=tech.headers)
    assert {i["id"] for i in r.json()["items"]} == {str(a1.id), str(b1.id)}
    assert (await client.get(f"/api/v1/establishments/{a2.id}", headers=tech.headers)).status_code == 404

    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_b.id))
    s = await login_as(client, adv)
    assert (await client.get("/api/v1/establishments", headers=s.headers)).status_code == 403
    assert (await client.get(f"/api/v1/establishments/{b1.id}", headers=s.headers)).status_code == 403


async def test_employee_city_scope_cannot_create_outside_city(client, db):
    org = await mk_org(db)
    emp = await staff_session(client, db, "employee", ("CITY", "GO:goiania"))
    r = await client.post("/api/v1/establishments", json=_est_body(org.id, city="Anápolis"), headers=emp.headers)
    assert r.status_code == 404
    r = await client.post("/api/v1/establishments", json=_est_body(org.id), headers=emp.headers)
    assert r.status_code == 201
    # mover para fora da cidade de atuação é negado
    r = await client.patch(f"/api/v1/establishments/{r.json()['id']}", json={"city": "Anápolis"},
                           headers=emp.headers)
    assert r.status_code == 403


async def test_segments_products_and_audience_estimate(client, db, admin):
    from app.modules.taxonomy.service import ensure_reference_taxonomy

    ids = await ensure_reference_taxonomy(db)
    await db.commit()
    org = await mk_org(db)
    cat = ids["distribuidora-de-bebidas"]
    r = await client.post("/api/v1/establishments",
                          json=_est_body(org.id, category_id=str(cat), segment_ids=[str(ids["segment:bebidas"])]),
                          headers=admin.headers)
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert r.json()["segment_ids"] == [str(ids["segment:bebidas"])]
    sector = ids["alimentacao"]
    r = await client.patch(f"/api/v1/establishments/{eid}", json={"category_id": str(sector)}, headers=admin.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "CATEGORY_TOO_BROAD"
    r = await client.put(f"/api/v1/establishments/{eid}/segments", json={"ids": [str(uuid.uuid4())]},
                         headers=admin.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "SEGMENT_NOT_FOUND"
    r = await client.put(f"/api/v1/establishments/{eid}/segments",
                         json={"ids": [str(ids["segment:bebidas"]), str(ids["segment:snacks"])]}, headers=admin.headers)
    assert r.status_code == 200 and len(r.json()["segment_ids"]) == 2

    r = await client.post(f"/api/v1/establishments/{eid}/audience-estimates",
                          json={"method": "FLOW_ESTIMATE", "daily_visitors": 2000, "visibility_bps": 2500},
                          headers=admin.headers)
    assert r.status_code == 201
    a = r.json()
    assert a["daily_people"] == 500 and a["is_estimate"] is True and a["method"] == "FLOW_ESTIMATE"
    assert a["basis"]["daily_visitors"] == 2000
    r = await client.get(f"/api/v1/establishments/{eid}", headers=admin.headers)
    v = r.json()["estimated_daily_visitors"]
    assert v["value"] == 500 and v["is_estimate"] is True and "não é medição" in v["label"]
    r = await client.post(f"/api/v1/establishments/{eid}/audience-estimates",
                          json={"method": "FLOW_ESTIMATE", "daily_visitors": 2000}, headers=admin.headers)
    assert r.status_code == 400


async def test_timezone_suggestion_endpoint(client, db, admin):
    r = await client.get("/api/v1/establishments/timezone-suggestion", params={"state": "AC"}, headers=admin.headers)
    assert r.status_code == 200 and r.json()["timezone"] == "America/Rio_Branco"
    r = await client.get("/api/v1/establishments/timezone-suggestion", params={"state": "AM", "city": "Lábrea"},
                         headers=admin.headers)
    assert r.json()["timezone"] == "America/Eirunepe"
