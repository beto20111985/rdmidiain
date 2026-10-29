"""W-ORG — telas: TEL-UF-XXXX único/imutável, troca de estabelecimento e de hardware com histórico,
manutenção sem sair do inventário, retirada definitiva, escopo, eventos em tempo real."""
import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.ids import is_valid_screen_code
from app.core.timeutils import utcnow
from app.models.equipment import EquipmentItem
from app.models.screens import Screen, ScreenHistory
from app.modules.screens.service import (
    register_screen_removed_listener,
    screen_establishment_at,
    unregister_screen_removed_listener,
)
from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org, staff_session


async def _create_screen(client, h, est_id, **kw):
    body = {"establishment_id": str(est_id), "name": kw.pop("name", "TV Caixa 1"), "inches": 32,
            "hardware_brand": "LG", "hardware_model": "32LQ", "serial_number": "SN-OLD", **kw}
    r = await client.post("/api/v1/screens", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def test_friendly_id_unique_and_immutable(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org, "Goiânia", "GO")
    codes = set()
    for i in range(25):
        s = await _create_screen(client, admin.headers, est.id, name=f"Tela {i}")
        assert is_valid_screen_code(s["friendly_id"]) and s["friendly_id"].startswith("TEL-GO-")
        codes.add(s["friendly_id"])
    assert len(codes) == 25
    sid = s["id"]
    # API: friendly_id não é campo editável (422 — campo proibido)
    r = await client.patch(f"/api/v1/screens/{sid}", json={"friendly_id": "TEL-GO-AAAA"}, headers=admin.headers)
    assert r.status_code == 422
    # Banco: trigger de coluna imutável
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("UPDATE screens SET friendly_id = 'TEL-GO-AAAA' WHERE id = :i"), {"i": uuid.UUID(sid)})
        await db.commit()
    assert "RD_IMMUTABLE_COLUMN" in str(ei.value)
    await db.rollback()
    # UNIQUE no banco
    s1 = (await db.execute(select(Screen.friendly_id, Screen.establishment_id).limit(1))).one()
    db.add(Screen(id=uuid.uuid4(), establishment_id=s1.establishment_id, friendly_id=s1.friendly_id, name="dup"))
    with pytest.raises(Exception):
        await db.commit()
    await db.rollback()


async def test_move_establishment_preserves_id_and_history(client, db, admin):
    org = await mk_org(db)
    gyn = await mk_est(db, org, "Goiânia", "GO", name="Unidade Centro")
    df = await mk_est(db, org, "Brasília", "DF", name="Unidade DF")
    s = await _create_screen(client, admin.headers, gyn.id)
    t_before = utcnow()
    await asyncio.sleep(0.05)
    r = await client.post(f"/api/v1/screens/{s['id']}/move", json={"establishment_id": str(df.id),
                                                                   "reason": "Reinstalada na unidade de Brasília"},
                          headers=admin.headers)
    assert r.status_code == 200, r.text
    moved = r.json()
    assert moved["id"] == s["id"] and moved["friendly_id"] == s["friendly_id"]  # TEL-GO-… não muda
    assert moved["establishment_id"] == str(df.id)
    hist = (await client.get(f"/api/v1/screens/{s['id']}/history", headers=admin.headers)).json()["items"]
    events = [h["event"] for h in hist]
    assert events[0] == "MOVED" and "CREATED" in events
    mv = hist[0]
    assert mv["from_establishment_id"] == str(gyn.id) and mv["establishment_id"] == str(df.id)
    sid = uuid.UUID(s["id"])
    assert await screen_establishment_at(db, sid, t_before) == gyn.id
    assert await screen_establishment_at(db, sid, utcnow() + timedelta(seconds=1)) == df.id
    # histórico é append-only
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("DELETE FROM screen_history WHERE screen_id = :i"), {"i": sid})
        await db.commit()
    assert "RD_APPEND_ONLY" in str(ei.value)
    await db.rollback()
    assert "screen.moved" in await audit_actions(db, s["id"])


async def test_replace_tv_with_stock_item_preserves_history_and_ledger(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org)
    s = await _create_screen(client, admin.headers, est.id, tv_owner="RD")
    h = admin.headers

    async def stock_tv(serial, inches):
        r = await client.post("/api/v1/equipment", json={"type": "TV", "name": f"TV {inches}", "brand": "Samsung",
                                                         "model": f"UN{inches}", "serial_number": serial,
                                                         "asset_tag": f"PAT-{serial}", "inches": inches,
                                                         "warehouse_city_key": "GO:goiania"}, headers=h)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    tv_a, tv_b = await stock_tv("SA-1", 32), await stock_tv("SB-2", 43)
    r = await client.post(f"/api/v1/screens/{s['id']}/replace-hardware",
                          json={"component": "TV", "new_equipment_item_id": tv_a, "reason": "Instalação da TV RD 32"},
                          headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/v1/screens/{s['id']}/replace-hardware",
                          json={"component": "TV", "new_equipment_item_id": tv_b, "old_item_destination": "MAINTENANCE",
                                "reason": "Upgrade para 43 polegadas solicitado"}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["screen"]["hardware"]["serial_number"] == "SB-2" and out["screen"]["hardware"]["inches"] == 43
    assert out["before"]["serial_number"] == "SA-1"
    grp = out["movement_group_id"]
    assert {m["group_id"] for m in out["movements"]} == {grp}
    assert {m["movement_type"] for m in out["movements"]} == {"MAINTENANCE_IN", "INSTALL"}
    inst_mv = [m for m in out["movements"] if m["movement_type"] == "INSTALL"][0]
    assert inst_mv["replaced_item_id"] == tv_a
    a = await db.get(EquipmentItem, uuid.UUID(tv_a), populate_existing=True)
    b = await db.get(EquipmentItem, uuid.UUID(tv_b), populate_existing=True)
    assert a.status == "IN_MAINTENANCE" and a.location_screen_id is None
    assert b.status == "INSTALLED" and str(b.location_screen_id) == s["id"]
    hist = (await client.get(f"/api/v1/screens/{s['id']}/history", headers=h)).json()["items"]
    assert [x["event"] for x in hist][:2] == ["TV_REPLACED", "TV_REPLACED"]
    assert hist[0]["before"]["serial_number"] == "SA-1" and hist[0]["after"]["serial_number"] == "SB-2"
    # mesma tela (ID e código) — histórico de reprodução continua ligado a ela
    assert out["screen"]["id"] == s["id"] and out["screen"]["friendly_id"] == s["friendly_id"]
    # equipamento instalado aparece no detalhe
    det = (await client.get(f"/api/v1/screens/{s['id']}", headers=h)).json()
    assert [e["id"] for e in det["equipment"]] == [tv_b]


async def test_maintenance_keeps_screen_sellable_and_restores_active(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org)
    s = await _create_screen(client, admin.headers, est.id)
    assert s["sellable"] is True
    tech = await make_user(db, role="technician", scope=("CITY", "GO:goiania"))
    r = await client.post("/api/v1/maintenance", json={"screen_id": s["id"], "component": "TV", "priority": "HIGH",
                                                       "description": "TV não liga após queda de energia",
                                                       "technician_user_id": str(tech.id)}, headers=admin.headers)
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    scr = (await client.get(f"/api/v1/screens/{s['id']}", headers=admin.headers)).json()
    assert scr["status"] == "MAINTENANCE" and scr["sellable"] is True  # não sai do inventário (#1115)
    ts = await login_as(client, tech)
    r = await client.get("/api/v1/maintenance", params={"mine": True}, headers=ts.headers)
    assert [o["id"] for o in r.json()["items"]] == [oid]
    r = await client.post(f"/api/v1/maintenance/{oid}/status", json={"status": "IN_PROGRESS"}, headers=ts.headers)
    assert r.status_code == 200
    r = await client.post(f"/api/v1/maintenance/{oid}/status", json={"status": "RESOLVED", "note": "ok"},
                          headers=ts.headers)
    assert r.status_code == 400  # resolução exige descrição
    r = await client.post(f"/api/v1/maintenance/{oid}/status",
                          json={"status": "RESOLVED", "note": "Fonte da TV substituída"}, headers=ts.headers)
    assert r.status_code == 200
    body = r.json()
    assert [e["event"] for e in body["history"]] == ["OPENED", "ASSIGNED", "STATUS_CHANGED", "RESOLVED"]
    r = await client.post(f"/api/v1/maintenance/{oid}/status", json={"status": "IN_PROGRESS"}, headers=ts.headers)
    assert r.status_code == 409
    scr = (await client.get(f"/api/v1/screens/{s['id']}", headers=admin.headers)).json()
    assert scr["status"] == "ACTIVE" and scr["sellable"] is True
    # técnico de outra cidade não vê a ordem
    other = await staff_session(client, db, "technician", ("CITY", "SP:sao-paulo"))
    assert (await client.get(f"/api/v1/maintenance/{oid}", headers=other.headers)).status_code == 404


async def test_decommission_is_terminal_and_notifies_listener(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org)
    s = await _create_screen(client, admin.headers, est.id)
    calls = []

    async def listener(_db, ids, reason):
        calls.append(([str(i) for i in ids], reason))

    register_screen_removed_listener(listener)
    try:
        r = await client.post(f"/api/v1/screens/{s['id']}/decommission",
                              json={"reason": "TV furtada, estabelecimento sem reposição"}, headers=admin.headers)
        assert r.status_code == 200
        assert r.json()["status"] == "DECOMMISSIONED" and r.json()["sellable"] is False
        r = await client.post(f"/api/v1/screens/{s['id']}/decommission",
                              json={"reason": "TV furtada, estabelecimento sem reposição"}, headers=admin.headers)
        assert r.status_code == 409
    finally:
        unregister_screen_removed_listener(listener)
    assert calls == [([s["id"]], "SCREEN_DECOMMISSIONED")]
    r = await client.post(f"/api/v1/screens/{s['id']}/status", json={"status": "ACTIVE", "reason": "tentativa indevida"},
                          headers=admin.headers)
    assert r.status_code == 409
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("UPDATE screens SET status='ACTIVE', sellable=true, decommissioned_at=NULL WHERE id=:i"),
                         {"i": uuid.UUID(s["id"])})
        await db.commit()
    assert "RD_SCREEN_DECOMMISSIONED" in str(ei.value)
    await db.rollback()


async def test_maintenance_does_not_call_removal_listener(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org)
    s = await _create_screen(client, admin.headers, est.id)
    calls = []

    async def listener(_db, ids, reason):
        calls.append(reason)

    register_screen_removed_listener(listener)
    try:
        r = await client.post(f"/api/v1/screens/{s['id']}/status",
                              json={"status": "MAINTENANCE", "reason": "TV Box com defeito"}, headers=admin.headers)
        assert r.status_code == 200 and r.json()["sellable"] is True
    finally:
        unregister_screen_removed_listener(listener)
    assert calls == []


async def test_screen_scope_cross_org_and_city(client, db, admin):
    org_a, org_b = await mk_org(db), await mk_org(db)
    ea = await mk_est(db, org_a, "Goiânia", "GO")
    eb = await mk_est(db, org_b, "Anápolis", "GO")
    sa = await _create_screen(client, admin.headers, ea.id)
    sb = await _create_screen(client, admin.headers, eb.id)
    owner_a = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org_a.id))
    s = await login_as(client, owner_a)
    r = await client.get("/api/v1/screens", headers=s.headers)
    assert {i["id"] for i in r.json()["items"]} == {sa["id"]}
    for path in (f"/api/v1/screens/{sb['id']}", f"/api/v1/screens/{sb['id']}/history"):
        assert (await client.get(path, headers=s.headers)).status_code == 404
    assert (await client.patch(f"/api/v1/screens/{sa['id']}", json={"name": "x"}, headers=s.headers)).status_code == 403
    tech = await staff_session(client, db, "technician", ("CITY", "GO:anapolis"))
    r = await client.get("/api/v1/screens", headers=tech.headers)
    assert {i["id"] for i in r.json()["items"]} == {sb["id"]}
    assert (await client.get(f"/api/v1/screens/{sa['id']}", headers=tech.headers)).status_code == 404
    # cadastrar tela em estabelecimento de outra organização (sem escopo) → 404
    emp_gyn = await staff_session(client, db, "employee", ("CITY", "GO:goiania"))
    r = await client.post("/api/v1/screens", json={"establishment_id": str(eb.id), "name": "Invasora"},
                          headers=emp_gyn.headers)
    assert r.status_code == 404


async def test_screen_without_contract_is_not_sellable_and_audience_is_estimate(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org, onboarding_status="AWAITING_CONTRACT")
    s = await _create_screen(client, admin.headers, est.id)
    assert s["sellable"] is False
    r = await client.post(f"/api/v1/screens/{s['id']}/audience-estimates",
                          json={"method": "MANUAL", "daily_people": 350, "note": "Contagem informada pelo gerente"},
                          headers=admin.headers)
    assert r.status_code == 201 and r.json()["is_estimate"] is True
    det = (await client.get(f"/api/v1/screens/{s['id']}", headers=admin.headers)).json()
    assert det["estimated_daily_audience"]["value"] == 350
    assert det["estimated_daily_audience"]["is_estimate"] is True


async def test_screen_updated_event_published_to_org_channel(client, db, admin, redis):
    from app.core.events import channel_org

    org = await mk_org(db)
    est = await mk_est(db, org)
    ps = redis.pubsub()
    await ps.subscribe(channel_org(org.id))
    await ps.get_message(timeout=1.0)  # confirmação do subscribe
    s = await _create_screen(client, admin.headers, est.id)
    msg = None
    for _ in range(20):
        m = await ps.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if m and m.get("type") == "message":
            env = json.loads(m["data"])
            if env["type"] == "screen.updated":
                msg = env
                break
    await ps.unsubscribe()
    await ps.aclose()
    assert msg is not None
    assert msg["data"]["id"] == s["id"] and msg["data"]["change"] == "created"
    assert msg["scope"]["establishment_id"] == str(est.id) and msg["scope"]["city"] == "GO:goiania"


async def test_move_and_timezone_change_regenerate_manifest_via_inventory(client, db, admin):
    from app.models.manifests import ManifestVersion

    org = await mk_org(db)
    gyn = await mk_est(db, org, "Goiânia", "GO")
    ac = await mk_est(db, org, "Rio Branco", "AC", tz="America/Rio_Branco")
    s = await _create_screen(client, admin.headers, gyn.id)
    sid = uuid.UUID(s["id"])

    async def versions():
        return (await db.execute(select(ManifestVersion.version).where(ManifestVersion.screen_id == sid)
                                 .order_by(ManifestVersion.version))).scalars().all()

    before = await versions()
    r = await client.post(f"/api/v1/screens/{s['id']}/move", json={"establishment_id": str(ac.id),
                                                                   "reason": "Mudança para o Acre (outro fuso)"},
                          headers=admin.headers)
    assert r.status_code == 200
    after_move = await versions()
    assert len(after_move) == len(before) + 1  # W-INV gerou nova versão (fuso/estabelecimento novos)
    r = await client.patch(f"/api/v1/establishments/{ac.id}", json={"timezone": "America/Sao_Paulo"},
                           headers=admin.headers)
    assert r.status_code == 200
    assert len(await versions()) == len(after_move) + 1


async def test_history_rows_written_for_creation(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org)
    s = await _create_screen(client, admin.headers, est.id)
    rows = (await db.execute(select(ScreenHistory.event).where(ScreenHistory.screen_id == uuid.UUID(s["id"])))).all()
    assert [r[0] for r in rows] == ["CREATED"]
    assert "screen.created" in await audit_actions(db, s["id"])
