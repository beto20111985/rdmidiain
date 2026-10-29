"""W-ORG — follow-ups do frontend (FE-OPS): filtro ``partner_plan_id`` em estabelecimentos e nomes nas
listas de manutenção/instalações/comodato (estabelecimento, código da tela, técnico) sem N+1 e com escopo."""
import uuid
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.models.contracts import ComodatoAgreement
from app.models.installations import Installation, MaintenanceOrder
from app.models.screens import Screen
from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import mk_est, mk_org, staff_session


@contextmanager
def count_selects():
    seen: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", _before)


async def _screen(db, est, code):
    s = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=code, name=f"Tela {code}")
    db.add(s)
    await db.commit()
    return s


async def test_establishments_filter_by_partner_plan(client, db, admin):
    h = admin.headers
    plan = (await client.post("/api/v1/partner-plans", json={
        "name": "Plano Filtro", "tv_provision": "OWN_TV", "deposit_required": False,
        "installation_fee_required": False, "upgrade_policy": "FREE"}, headers=h)).json()
    org = await mk_org(db)
    with_plan = await mk_est(db, org, name="Com plano", partner_plan_id=uuid.UUID(plan["id"]))
    await mk_est(db, org, name="Sem plano")
    r = await client.get("/api/v1/establishments", params={"partner_plan_id": plan["id"]}, headers=h)
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [str(with_plan.id)] and r.json()["total"] == 1
    r = await client.get("/api/v1/establishments", params={"partner_plan_id": str(uuid.uuid4())}, headers=h)
    assert r.json()["items"] == [] and r.json()["total"] == 0
    r = await client.get("/api/v1/establishments", params={"partner_plan_id": "nao-e-uuid"}, headers=h)
    assert r.status_code == 422


async def test_maintenance_list_has_names_without_n_plus_1(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org, name="Mercado Alfa")
    tech = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Téo Técnico")
    scr = await _screen(db, est, "TEL-GO-MN22")
    db.add(MaintenanceOrder(id=uuid.uuid4(), establishment_id=est.id, screen_id=scr.id, component="TV",
                            description="TV sem imagem na entrada", technician_user_id=tech.id))
    await db.commit()
    with count_selects() as one:
        r = await client.get("/api/v1/maintenance", headers=admin.headers)
    assert r.status_code == 200
    it = r.json()["items"][0]
    assert it["establishment_name"] == "Mercado Alfa" and it["screen_code"] == "TEL-GO-MN22"
    assert it["technician_name"] == "Téo Técnico"
    # mais 5 ordens (algumas sem tela/técnico) → mesma quantidade de SELECTs
    for i in range(5):
        s = await _screen(db, est, f"TEL-GO-M{i}A2")
        db.add(MaintenanceOrder(id=uuid.uuid4(), establishment_id=est.id, screen_id=s.id if i % 2 else None,
                                component="TVBOX", description=f"Ordem número {i} de teste",
                                technician_user_id=tech.id if i % 2 else None))
    await db.commit()
    with count_selects() as many:
        r = await client.get("/api/v1/maintenance", headers=admin.headers)
    items = r.json()["items"]
    assert len(items) == 6
    assert len(many) == len(one), (len(one), len(many))
    # o contador enxerga as consultas da API (a listagem é 1 SELECT com JOINs + 1 COUNT)
    listing = [q for q in many if "FROM maintenance_orders" in q]
    assert len(listing) == 2 and any("JOIN establishments" in q and "LEFT OUTER JOIN screens" in q for q in listing)
    no_screen = [i for i in items if i["screen_id"] is None]
    assert no_screen and all(i["screen_code"] is None and i["technician_name"] is None for i in no_screen)
    # técnico designado vê os nomes das suas ordens; técnico de outra cidade não vê nada
    ts = await login_as(client, tech)
    r = await client.get("/api/v1/maintenance", params={"mine": True}, headers=ts.headers)
    assert {i["establishment_name"] for i in r.json()["items"]} == {"Mercado Alfa"}
    far = await staff_session(client, db, "technician", ("CITY", "SP:sao-paulo"))
    assert (await client.get("/api/v1/maintenance", headers=far.headers)).json()["items"] == []
    oid = items[0]["id"]
    det = (await client.get(f"/api/v1/maintenance/{oid}", headers=admin.headers)).json()
    assert det["establishment_name"] == "Mercado Alfa" and "history" in det


async def test_installations_list_has_names_without_n_plus_1(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org, name="Academia Beta")
    tech = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Ivo Instalador")
    scr = await _screen(db, est, "TEL-GO-IN22")
    db.add(Installation(id=uuid.uuid4(), establishment_id=est.id, screen_id=scr.id, technician_user_id=tech.id,
                        status="SCHEDULED", checklist=[]))
    await db.commit()
    with count_selects() as one:
        r = await client.get("/api/v1/installations", headers=admin.headers)
    it = r.json()["items"][0]
    assert (it["establishment_name"], it["screen_code"], it["technician_name"]) == (
        "Academia Beta", "TEL-GO-IN22", "Ivo Instalador")
    for _ in range(4):
        db.add(Installation(id=uuid.uuid4(), establishment_id=est.id, screen_id=None, technician_user_id=tech.id,
                            status="SCHEDULED", checklist=[]))
    await db.commit()
    with count_selects() as many:
        r = await client.get("/api/v1/installations", headers=admin.headers)
    assert len(r.json()["items"]) == 5 and len(many) == len(one)
    det = (await client.get(f"/api/v1/installations/{it['id']}", headers=admin.headers)).json()
    assert det["screen_code"] == "TEL-GO-IN22" and det["technician_name"] == "Ivo Instalador"
    # escopo: dono de outra organização não lista (sem equipment.view) e técnico de outra cidade não vê
    far = await staff_session(client, db, "technician", ("CITY", "SP:sao-paulo"))
    assert (await client.get("/api/v1/installations", headers=far.headers)).json()["items"] == []


async def test_comodato_list_has_establishment_name_and_scope(client, db, admin):
    org_a, org_b = await mk_org(db), await mk_org(db)
    ea = await mk_est(db, org_a, name="Padaria Gama")
    eb = await mk_est(db, org_b, name="Farmácia Delta")
    for est in (ea, eb):
        db.add(ComodatoAgreement(id=uuid.uuid4(), org_id=est.org_id, establishment_id=est.id, deposit_required=False,
                                 deposit_cents=0, installation_fee_required=False, installation_fee_cents=0,
                                 upgrade_policy="FREE", status="ACTIVE"))
    await db.commit()
    with count_selects() as q:
        r = await client.get("/api/v1/comodato-agreements", headers=admin.headers)
    assert {i["establishment_name"] for i in r.json()["items"]} == {"Padaria Gama", "Farmácia Delta"}
    assert len(q) < 10
    owner_a = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org_a.id))
    s = await login_as(client, owner_a)
    r = await client.get("/api/v1/comodato-agreements", headers=s.headers)
    assert [i["establishment_name"] for i in r.json()["items"]] == ["Padaria Gama"]
    ag_id = r.json()["items"][0]["id"]
    det = (await client.get(f"/api/v1/comodato-agreements/{ag_id}", headers=s.headers)).json()
    assert det["establishment_name"] == "Padaria Gama"
