"""W-ORG — complementos: estoque com escopo ORGANIZATION (org A × org B), planos do parceiro (regras
comerciais configuráveis), KYC auditado, troca de TV Box e OpenAPI publicado."""
import uuid

from sqlalchemy import select

from app.models.rbac import Permission, Role, RolePermission
from app.models.screens import Screen
from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org


async def _custom_role(db, slug, perms):
    role = Role(id=uuid.uuid4(), slug=slug, name=slug, is_system=False)
    db.add(role)
    await db.flush()
    for code in perms:
        pid = (await db.execute(select(Permission.id).where(Permission.code == code))).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=pid))
    await db.commit()
    return slug


async def test_org_scoped_equipment_visibility_a_vs_b(client, db, admin):
    h = admin.headers
    org_a, org_b = await mk_org(db), await mk_org(db)
    est_a, est_b = await mk_est(db, org_a), await mk_est(db, org_b)
    sa = Screen(id=uuid.uuid4(), establishment_id=est_a.id, friendly_id="TEL-GO-AB2C", name="A")
    sb = Screen(id=uuid.uuid4(), establishment_id=est_b.id, friendly_id="TEL-GO-AB3C", name="B")
    db.add_all([sa, sb])
    await db.commit()
    ids = {}
    for name, scr in (("a", sa), ("b", sb)):
        r = await client.post("/api/v1/equipment", json={"type": "TV", "name": f"TV {name}", "serial_number": f"S{name}9",
                                                         "warehouse_city_key": "GO:goiania"}, headers=h)
        ids[name] = r.json()["id"]
        r = await client.post(f"/api/v1/equipment/{ids[name]}/movements",
                              json={"movement_type": "INSTALL", "to": {"kind": "SCREEN", "ref_id": str(scr.id)}},
                              headers=h)
        assert r.status_code == 201
    slug = await _custom_role(db, "org_equipment_viewer", ["equipment.view", "screens.view"])
    viewer = await make_user(db, role=slug, scope=("ORGANIZATION", org_a.id))
    s = await login_as(client, viewer)
    r = await client.get("/api/v1/equipment", params={"tracking": "SERIAL"}, headers=s.headers)
    assert {i["id"] for i in r.json()["items"]} == {ids["a"]}
    assert (await client.get(f"/api/v1/equipment/{ids['b']}", headers=s.headers)).status_code == 404
    assert (await client.get(f"/api/v1/equipment/{ids['b']}/movements", headers=s.headers)).status_code == 404
    assert (await client.get(f"/api/v1/screens/{sb.id}/equipment", headers=s.headers)).status_code == 404
    r = await client.get(f"/api/v1/screens/{sa.id}/equipment", headers=s.headers)
    assert [i["id"] for i in r.json()["items"]] == [ids["a"]]
    # sem equipment.manage → 403 ao movimentar
    r = await client.post(f"/api/v1/equipment/{ids['a']}/movements",
                          json={"movement_type": "RETURN", "to": {"kind": "WAREHOUSE"}}, headers=s.headers)
    assert r.status_code == 403


async def test_partner_plans_configurable_rules(client, db, admin):
    h = admin.headers
    base = {"name": "Plano Inicial TV RD 32", "tv_provision": "RD_TV_COMODATO", "default_tv_inches": 32,
            "deposit_required": False, "installation_fee_required": False, "upgrade_policy": "NEGOTIATED"}
    r = await client.post("/api/v1/partner-plans", json={**base, "deposit_cents": 100}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "DEPOSIT_INCONSISTENT"
    r = await client.post("/api/v1/partner-plans", json={**base, "default_internal_s": 120, "default_external_s": 90},
                          headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "ROTATION_INVALID"
    r = await client.post("/api/v1/partner-plans", json={**base, "default_internal_s": 120}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "ROTATION_INCOMPLETE"
    r = await client.post("/api/v1/partner-plans", json={**base, "default_internal_s": 120, "default_external_s": 120},
                          headers=h)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["is_free"] is True and p["monthly_fee_cents"] == 0
    r = await client.post("/api/v1/partner-plans", json={**base, "name": "Plano TV Própria com mensalidade",
                                                         "tv_provision": "OWN_TV", "monthly_fee_cents": 4990,
                                                         "deposit_required": True, "deposit_cents": 20000,
                                                         "installation_fee_required": True,
                                                         "installation_fee_cents": 15000, "upgrade_policy": "PAID",
                                                         "upgrade_fee_cents": 30000}, headers=h)
    assert r.status_code == 201 and r.json()["is_free"] is False
    r = await client.patch(f"/api/v1/partner-plans/{p['id']}", json={"monthly_fee_cents": 1990}, headers=h)
    assert r.status_code == 200 and r.json()["monthly_fee_cents"] == 1990
    assert "partner_plan.updated" in await audit_actions(db, p["id"])
    # vínculo no estabelecimento
    org = await mk_org(db)
    r = await client.post("/api/v1/establishments", json={"org_id": str(org.id), "name": "Unidade", "city": "Goiânia",
                                                          "state": "GO", "partner_plan_id": p["id"]}, headers=h)
    assert r.status_code == 201 and r.json()["partner_plan_id"] == p["id"]
    r = await client.post("/api/v1/establishments", json={"org_id": str(org.id), "name": "Unidade 2", "city": "Goiânia",
                                                          "state": "GO", "partner_plan_id": str(uuid.uuid4())},
                          headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "PARTNER_PLAN_INVALID"
    # equipe com escopo de cidade não cria plano (regra comercial da RD)
    from tests.integration.org_api.helpers import staff_session

    emp = await staff_session(client, db, "employee", ("CITY", "GO:goiania"))
    assert (await client.post("/api/v1/partner-plans", json=base, headers=emp.headers)).status_code == 403


async def test_kyc_review_is_audited_and_rd_only(client, db, admin):
    org = await mk_org(db)
    r = await client.post(f"/api/v1/organizations/{org.id}/kyc", json={"status": "APPROVED",
                                                                       "notes": "Contrato social conferido"},
                          headers=admin.headers)
    assert r.status_code == 200 and r.json()["kyc_status"] == "APPROVED"
    assert "organization.kyc_reviewed" in await audit_actions(db, org.id)
    from tests.integration.org_api.helpers import staff_session

    sales = await staff_session(client, db, "sales", ("CITY", "GO:goiania"))
    r = await client.post(f"/api/v1/organizations/{org.id}/kyc", json={"status": "REJECTED"}, headers=sales.headers)
    assert r.status_code == 403


async def test_tvbox_replacement_updates_player_snapshot(client, db, admin):
    h = admin.headers
    org = await mk_org(db)
    est = await mk_est(db, org)
    r = await client.post("/api/v1/screens", json={"establishment_id": str(est.id), "name": "Tela"}, headers=h)
    sid = r.json()["id"]
    r = await client.post("/api/v1/equipment", json={"type": "TVBOX", "name": "TV Box", "brand": "Xiaomi",
                                                     "model": "Mi Box S", "serial_number": "BOX-1",
                                                     "warehouse_city_key": "GO:goiania"}, headers=h)
    box = r.json()["id"]
    r = await client.post(f"/api/v1/screens/{sid}/replace-hardware",
                          json={"component": "PLAYER", "new_equipment_item_id": box, "reason": "TV Box queimada trocada"},
                          headers=h)
    assert r.status_code == 200, r.text
    hw = r.json()["screen"]["hardware"]
    assert hw["player_brand"] == "Xiaomi" and hw["player_serial_number"] == "BOX-1"
    # item de outro tipo é recusado
    r = await client.post("/api/v1/equipment", json={"type": "TV", "name": "TV", "serial_number": "TV-77",
                                                     "warehouse_city_key": "GO:goiania"}, headers=h)
    r = await client.post(f"/api/v1/screens/{sid}/replace-hardware",
                          json={"component": "PLAYER", "new_equipment_item_id": r.json()["id"],
                                "reason": "Tentativa com item errado"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "EQUIPMENT_TYPE_MISMATCH"


async def test_openapi_includes_org_routes_without_legacy_player_routes(client):
    r = await client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    for p in ("/api/v1/catalog/availability", "/api/v1/taxonomy/segments", "/api/v1/contracts/accept",
              "/api/v1/onboarding/public/signup", "/api/v1/installations/{inst_id}/complete",
              "/api/v1/maintenance", "/api/v1/equipment/{item_id}/movements", "/api/v1/screens/{screen_id}/move",
              "/api/v1/partner-plans", "/api/v1/comodato-agreements"):
        assert p in paths, p
    for legacy in ("/api/v1/screens/{screen_id}/manifest", "/api/v1/screens/{screen_id}/manifest/generate",
                   "/api/v1/screens/{screen_id}/sync", "/api/v1/screens/{screen_id}/command"):
        assert legacy not in paths, legacy
    # /status agora é só a mudança de status de cadastro (POST); presença/conectividade é do W-PLAY
    assert set(paths["/api/v1/screens/{screen_id}/status"]) == {"post"}
