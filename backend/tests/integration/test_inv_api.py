"""W-INV — API admin/cliente do inventário, scheduler e conteúdo interno (auth, escopo, envelope)."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest_asyncio

from app.core.authz import AuthContext
from app.modules.inventory import service as inv
from tests.factories import login_as, make_user
from tests.inv_support import db_today, link, mk_campaign, mk_creative, mk_establishment, mk_org, mk_screen, pattern

SYS = AuthContext.system("system")


@pytest_asyncio.fixture
async def world(db, client):
    owner = await mk_org(db, "Mercado Dono")
    est = await mk_establishment(db, owner)
    scr = await mk_screen(db, est)
    adv_a, adv_b = await mk_org(db, "Anunciante A"), await mk_org(db, "Anunciante B")
    admin = await make_user(db, role="super_admin", mfa=True)
    ua = await make_user(db, role="advertiser", scope=("ORGANIZATION", adv_a.id))
    ub = await make_user(db, role="advertiser", scope=("ORGANIZATION", adv_b.id))
    uo = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", owner.id))
    today = await db_today(db)
    s_admin = await login_as(client, admin)
    s_a = await login_as(client, ua)
    s_b = await login_as(client, ub)
    s_o = await login_as(client, uo)

    class W:
        pass

    w = W()
    w.owner, w.est, w.scr, w.adv_a, w.adv_b = owner, est, scr, adv_a, adv_b
    w.admin, w.a, w.b, w.o = s_admin.headers, s_a.headers, s_b.headers, s_o.headers
    w.d1 = today + timedelta(days=1)
    return w


async def test_policies_api(client, world):
    bad = await client.post("/api/v1/inventory/policies", headers=world.admin, json={
        "scope": "GLOBAL", "internal_seconds": 120, "external_seconds": 150,
        "effective_from": "2099-01-01T00:00:00Z", "reason": "ciclo inválido"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "ROTATION_CYCLE_NOT_DIVISOR"
    ok = await client.post("/api/v1/inventory/policies", headers=world.admin, json={
        "scope": "ESTABLISHMENT", "scope_id": str(world.est.id), "internal_seconds": 180, "external_seconds": 60,
        "effective_from": "2099-01-01T03:00:00Z", "reason": "parceiro premium 3+1"})
    assert ok.status_code == 201, ok.text
    body = ok.json()
    assert body["units_per_hour"] == 30 and body["eligible_120s"] is False
    assert body["effective_from"].endswith("Z")
    lst = await client.get("/api/v1/inventory/policies", headers=world.admin)
    assert lst.status_code == 200 and lst.json()["total"] == 1 and lst.json()["default"]["units_per_hour"] == 60
    assert (await client.get("/api/v1/inventory/policies", headers=world.a)).status_code == 403


async def test_screen_state_scope_and_anchor_reset(client, db, world):
    url = f"/api/v1/inventory/screens/{world.scr.id}/state"
    r = await client.get(url, headers=world.admin)
    assert r.status_code == 200 and r.json()["policy"]["cycle_seconds"] == 240 and r.json()["anchor_ms"] is None
    assert (await client.get(url, headers=world.a)).status_code == 404  # tela de outra org: fora do escopo
    assert (await client.get(url, headers=world.o)).status_code == 403  # dono sem inventory.view
    await inv.set_anchor(db, world.scr.id, None, None)
    await db.commit()
    reset = f"/api/v1/inventory/screens/{world.scr.id}/anchor/reset"
    assert (await client.post(reset, headers=world.a, json={"reason": "x" * 20})).status_code == 403
    assert (await client.post(reset, headers=world.admin, json={"reason": "curta"})).status_code == 422
    r = await client.post(reset, headers=world.admin, json={"reason": "Reinstalação com troca de grade"})
    assert r.status_code == 200 and r.json()["anchor_ms"] is None
    h = await client.get(f"/api/v1/inventory/screens/{world.scr.id}/anchor/history", headers=world.admin)
    assert [x["source"] for x in h.json()["items"]] == ["SERVER", "ADMIN_RESET"]


async def test_holds_are_tenant_scoped_and_release_only_unpaid(client, db, world):
    ca = await mk_campaign(db, world.adv_a)
    cb = await mk_campaign(db, world.adv_b)
    ha = await inv.hold(db, SYS, ca.id, [world.scr.id], [pattern(world.d1, world.d1)], "ka")
    hb = await inv.hold(db, SYS, cb.id, [world.scr.id], [pattern(world.d1, world.d1)], "kb")
    ha_id, hb_id = ha.id, hb.id
    await db.commit()
    r = await client.get("/api/v1/inventory/holds", headers=world.a)
    assert [x["id"] for x in r.json()["items"]] == [str(ha_id)]
    assert (await client.get(f"/api/v1/inventory/holds/{hb_id}", headers=world.a)).status_code == 404
    got = (await client.get(f"/api/v1/inventory/holds/{ha_id}", headers=world.a)).json()
    assert got["screen_ids"] == [str(world.scr.id)] and got["hold_expires_at"].endswith("Z")
    assert (await client.post(f"/api/v1/inventory/holds/{hb_id}/release", headers=world.a,
                              json={"reason": "desisti"})).status_code == 404
    r = await client.post(f"/api/v1/inventory/holds/{ha_id}/release", headers=world.a, json={"reason": "desisti"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    await inv.convert_hold(db, hb_id)
    await db.commit()
    r = await client.post(f"/api/v1/inventory/holds/{hb_id}/release", headers=world.b, json={"reason": "desisti"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "HOLD_NOT_HELD"


async def test_quote_and_availability_expose_no_third_party_names(client, db, world):
    ca = await mk_campaign(db, world.adv_a)
    cb = await mk_campaign(db, world.adv_b)
    h = await inv.hold(db, SYS, cb.id, [world.scr.id], [pattern(world.d1, world.d1, duration_units=4, freq_value=15)],
                       "full")
    await inv.convert_hold(db, h.id)
    await db.commit()
    body = {"screen_ids": [str(world.scr.id)], "patterns": [pattern(world.d1, world.d1)], "campaign_id": str(cb.id)}
    assert (await client.post("/api/v1/inventory/quote", headers=world.a, json=body)).status_code == 404
    body["campaign_id"] = str(ca.id)
    r = await client.post("/api/v1/inventory/quote", headers=world.a, json=body)
    assert r.status_code == 200
    q = r.json()
    assert q["feasible"] is False and q["available"] == 0 and "details" not in q["screens"][0]
    assert str(cb.id) not in r.text and "Anunciante B" not in r.text
    av = await client.get(f"/api/v1/inventory/availability?state=GO&date_from={world.d1}&date_to={world.d1}",
                          headers=world.a)
    assert av.status_code == 200
    day = av.json()["days"][0]
    assert day["capacity_units"] == 60 * 24 and day["used_units"] == 60 * 24 and day["occupancy_bps"] == 10000
    assert str(world.scr.id) not in av.text


async def test_internal_content_rd_manages_establishment_only_reorders(client, db, world):
    p1 = await mk_creative(db, world.owner, kind="INTERNAL", duration=60)
    p2 = await mk_creative(db, world.owner, kind="INTERNAL", duration=30)
    ext = await mk_creative(db, world.owner, kind="EXTERNAL")
    payload = {"establishment_id": str(world.est.id), "creative_id": str(p1.id)}
    assert (await client.post("/api/v1/internal-content", headers=world.o, json=payload)).status_code == 403
    r1 = await client.post("/api/v1/internal-content", headers=world.admin, json=payload)
    assert r1.status_code == 201, r1.text
    r2 = await client.post("/api/v1/internal-content", headers=world.admin,
                           json={**payload, "creative_id": str(p2.id)})
    bad = await client.post("/api/v1/internal-content", headers=world.admin, json={**payload, "creative_id": str(ext.id)})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "CREATIVE_KIND_INVALID"
    i1, i2 = r1.json()["id"], r2.json()["id"]
    lst = await client.get(f"/api/v1/internal-content?establishment_id={world.est.id}", headers=world.o)
    assert [x["id"] for x in lst.json()["items"]] == [i1, i2]
    order = {"establishment_id": str(world.est.id), "item_ids": [i2, i1]}
    r = await client.put("/api/v1/internal-content/order", headers=world.o, json=order)
    assert r.status_code == 200 and [x["id"] for x in r.json()["items"]] == [i2, i1]
    # conjunto incompleto é recusado; outro estabelecimento/organização não enxerga
    assert (await client.put("/api/v1/internal-content/order", headers=world.o,
                             json={**order, "item_ids": [i2]})).status_code == 400
    assert (await client.get(f"/api/v1/internal-content?establishment_id={world.est.id}",
                             headers=world.a)).status_code == 403
    m = (await inv.current_manifest(db, world.scr.id)).manifest
    await db.commit()
    first = m["internal_pattern"][0][0]["c"]
    assert m["creatives"][first]["creative_id"] == str(p2.id)  # ordem definida pelo estabelecimento


async def test_emergency_api_and_grid_preview(client, db, world):
    inst = await mk_creative(db, world.owner, kind="INSTITUTIONAL")
    body = {"title": "Aviso", "creative_id": str(inst.id), "starts_at": "2026-10-07T00:00:00Z",
            "ends_at": "2099-01-01T00:00:00Z", "target_kind": "ALL", "target_ids": [],
            "reason": "Comunicado urgente da defesa civil"}
    assert (await client.post("/api/v1/scheduler/emergencies", headers=world.a, json=body)).status_code == 403
    r = await client.post("/api/v1/scheduler/emergencies", headers=world.admin, json=body)
    assert r.status_code == 201, r.text
    assert r.json()["screens"] == 1
    g = await client.get(f"/api/v1/scheduler/screens/{world.scr.id}/grid?hours=2", headers=world.admin)
    assert g.status_code == 200 and g.json()["emergency"][0]["id"] == r.json()["id"]
    assert (await client.get(f"/api/v1/scheduler/screens/{world.scr.id}/grid", headers=world.a)).status_code == 403
    ms = await client.get(f"/api/v1/scheduler/screens/{world.scr.id}/manifests", headers=world.admin)
    assert ms.status_code == 200 and ms.json()["total"] >= 1
    v = ms.json()["items"][0]["version"]
    one = await client.get(f"/api/v1/scheduler/screens/{world.scr.id}/manifests/{v}", headers=world.admin)
    env = one.json()["envelope"]
    from app.modules.playback import protocol as P

    keys = (await client.get("/api/v1/scheduler/public-keys", headers=world.admin)).json()["keys"]
    assert P.verify_es256(keys[0]["public_key"], env["manifest"], env["signature"])
    c = await client.post(f"/api/v1/scheduler/emergencies/{r.json()['id']}/cancel", headers=world.admin,
                          json={"reason": "Encerrado pela coordenação"})
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
