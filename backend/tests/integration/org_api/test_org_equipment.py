"""W-ORG — estoque/patrimônio: ledger append-only, saldo derivado, comodato/empréstimo/devolução/RMA,
escopo e concorrência (duas saídas simultâneas do mesmo item → só uma vence)."""
import asyncio
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import create_engine, session_factory
from app.core.errors import DomainError
from app.models.equipment import EquipmentItem, EquipmentMovement
from app.models.screens import Screen
from app.modules.assets import service as assets
from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org


async def _item(client, h, **kw):
    body = {"type": "TV", "name": "TV 32", "brand": "LG", "model": "32LQ", "serial_number": uuid.uuid4().hex[:10],
            "warehouse_city_key": "GO:goiania", **kw}
    r = await client.post("/api/v1/equipment", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def _screen(db, est, code=None):
    s = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=code or f"TEL-GO-{uuid.uuid4().hex[:4].upper()}",
               name="S")
    db.add(s)
    await db.commit()
    return s


async def _mv(client, h, item_id, **body):
    return await client.post(f"/api/v1/equipment/{item_id}/movements", json=body, headers=h)


async def test_serial_lifecycle_entry_install_return_maintenance_rma_writeoff(client, db, admin):
    h = admin.headers
    org = await mk_org(db)
    est = await mk_est(db, org)
    scr = await _screen(db, est)
    it = await _item(client, h, asset_tag="PAT-0001")
    assert it["status"] == "IN_STOCK" and it["balances"] == [
        {"kind": "WAREHOUSE", "ref_id": None, "city_key": "GO:goiania", "quantity": 1}]
    iid = it["id"]
    # duplicidade de patrimônio
    r = await client.post("/api/v1/equipment", json={"type": "TV", "name": "TV duplicada", "asset_tag": "PAT-0001"}, headers=h)
    assert r.status_code == 409
    r = await _mv(client, h, iid, movement_type="INSTALL", to={"kind": "SCREEN", "ref_id": str(scr.id)})
    assert r.status_code == 201, r.text
    item = r.json()["item"]
    assert item["status"] == "INSTALLED" and item["location"]["screen_id"] == str(scr.id)
    assert item["balances"] == [{"kind": "SCREEN", "ref_id": str(scr.id), "city_key": None, "quantity": 1}]
    # não pode instalar de novo (já está na tela)
    r = await _mv(client, h, iid, movement_type="INSTALL", to={"kind": "SCREEN", "ref_id": str(scr.id)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "EQUIPMENT_INVALID_MOVEMENT"
    r = await _mv(client, h, iid, movement_type="MAINTENANCE_IN", to={"kind": "MAINTENANCE", "city_key": "GO:goiania"})
    assert r.status_code == 201 and r.json()["item"]["status"] == "IN_MAINTENANCE"
    r = await _mv(client, h, iid, movement_type="RMA_OUT", to={"kind": "RMA"}, reason="Defeito de fábrica")
    assert r.status_code == 400 and r.json()["error"]["code"] == "RMA_REFERENCE_REQUIRED"
    r = await _mv(client, h, iid, movement_type="RMA_OUT", to={"kind": "RMA"}, reason="Defeito de fábrica",
                  reference="RMA-2026-0042")
    assert r.status_code == 201 and r.json()["item"]["status"] == "IN_RMA"
    r = await _mv(client, h, iid, movement_type="RMA_RETURN", to={"kind": "WAREHOUSE", "city_key": "GO:goiania"})
    assert r.status_code == 201 and r.json()["item"]["status"] == "IN_STOCK"
    r = await _mv(client, h, iid, movement_type="WRITE_OFF", to={"kind": "DISPOSED"})
    assert r.status_code == 400  # baixa exige motivo
    r = await _mv(client, h, iid, movement_type="WRITE_OFF", to={"kind": "DISPOSED"}, reason="Tela quebrada sem reparo")
    assert r.status_code == 201 and r.json()["item"]["status"] == "WRITTEN_OFF"
    r = await _mv(client, h, iid, movement_type="RETURN", to={"kind": "WAREHOUSE"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "EQUIPMENT_WRITTEN_OFF"
    # histórico completo e coerente; projeção == derivação do ledger
    movs = (await client.get(f"/api/v1/equipment/{iid}/movements", headers=h)).json()["items"]
    assert [m["movement_type"] for m in reversed(movs)] == [
        "ENTRY", "INSTALL", "MAINTENANCE_IN", "RMA_OUT", "RMA_RETURN", "WRITE_OFF"]
    derived = await assets.derive_serial_location(db, uuid.UUID(iid))
    assert derived["kind"] == "DISPOSED"
    acts = await audit_actions(db, iid)
    assert acts[0] == "equipment.created" and "equipment.movement.write_off" in acts


async def test_movements_are_append_only(client, db, admin):
    it = await _item(client, admin.headers)
    iid = uuid.UUID(it["id"])
    for stmt in ("UPDATE equipment_movements SET quantity = 5 WHERE item_id = :i",
                 "DELETE FROM equipment_movements WHERE item_id = :i"):
        with pytest.raises(DBAPIError) as ei:
            await db.execute(text(stmt), {"i": iid})
            await db.commit()
        assert "RD_APPEND_ONLY" in str(ei.value)
        await db.rollback()
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("UPDATE equipment_items SET type = 'CABLE' WHERE id = :i"), {"i": iid})
        await db.commit()
    assert "RD_IMMUTABLE_COLUMN" in str(ei.value)
    await db.rollback()


async def test_bulk_balance_is_derived_and_insufficient_balance_rejected(client, db, admin):
    h = admin.headers
    org = await mk_org(db)
    est = await mk_est(db, org)
    scr = await _screen(db, est)
    r = await client.post("/api/v1/equipment", json={"type": "CABLE", "tracking": "BULK", "name": "Cabo HDMI 2 m",
                                                     "quantity": 50, "warehouse_city_key": "GO:goiania"}, headers=h)
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["status"] == "BULK" and r.json()["total_quantity"] == 50
    wh = {"kind": "WAREHOUSE", "city_key": "GO:goiania"}
    r = await _mv(client, h, cid, movement_type="INSTALL", to={"kind": "SCREEN", "ref_id": str(scr.id)}, quantity=2)
    assert r.status_code == 400 and r.json()["error"]["code"] == "SOURCE_REQUIRED"
    r = await _mv(client, h, cid, movement_type="INSTALL", to={"kind": "SCREEN", "ref_id": str(scr.id)}, quantity=2,
                  **{"from": wh})
    assert r.status_code == 201, r.text
    r = await _mv(client, h, cid, movement_type="ENTRY", to=wh, quantity=10, **{"from": {"kind": "EXTERNAL"}},
                  reference="NF 1234")
    assert r.status_code == 201
    r = await _mv(client, h, cid, movement_type="INSTALL", to={"kind": "SCREEN", "ref_id": str(scr.id)}, quantity=59,
                  **{"from": wh})
    assert r.status_code == 409 and r.json()["error"]["code"] == "EQUIPMENT_INSUFFICIENT_BALANCE"
    assert r.json()["error"]["details"] == {"available": 58, "requested": 59}
    r = await _mv(client, h, cid, movement_type="ADJUSTMENT", to={"kind": "DISPOSED"}, quantity=3,
                  **{"from": wh}, reason="Inventário: 3 cabos danificados")
    assert r.status_code == 201
    bal = (await client.get(f"/api/v1/equipment/{cid}", headers=h)).json()["balances"]
    by = {(b["kind"], b["city_key"]): b["quantity"] for b in bal}
    assert by == {("WAREHOUSE", "GO:goiania"): 55, ("SCREEN", None): 2, ("DISPOSED", None): 3}
    # saldo bate com a soma bruta do ledger (derivação independente)
    rows = (await db.execute(select(EquipmentMovement).where(EquipmentMovement.item_id == uuid.UUID(cid)))).scalars().all()
    wh_in = sum(m.quantity for m in rows if m.to_kind == "WAREHOUSE")
    wh_out = sum(m.quantity for m in rows if m.from_kind == "WAREHOUSE")
    assert wh_in - wh_out == 55
    summ = (await client.get("/api/v1/equipment/summary", headers=h)).json()
    assert {"type": "CABLE", "name": "Cabo HDMI 2 m", "quantity": 55} in summ["bulk_in_warehouse"]


async def test_loan_comodato_and_return(client, db, admin):
    h = admin.headers
    org = await mk_org(db)
    est = await mk_est(db, org)
    other_est = await mk_est(db, await mk_org(db))
    it = await _item(client, h, type="TVBOX", name="TV Box X96")
    iid = it["id"]
    r = await _mv(client, h, iid, movement_type="LOAN", to={"kind": "ESTABLISHMENT", "ref_id": str(est.id)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "RETURN_DATE_REQUIRED"
    due = (date.today() + timedelta(days=15)).isoformat()
    r = await _mv(client, h, iid, movement_type="LOAN", to={"kind": "ESTABLISHMENT", "ref_id": str(est.id)},
                  expected_return_at=due)
    assert r.status_code == 201 and r.json()["item"]["status"] == "LOANED"
    assert r.json()["item"]["expected_return_at"] == due
    r = await _mv(client, h, iid, movement_type="RETURN", to={"kind": "WAREHOUSE", "city_key": "GO:goiania"})
    assert r.status_code == 201 and r.json()["item"]["status"] == "IN_STOCK"
    # comodato exige acordo ativo do MESMO estabelecimento
    r = await _mv(client, h, iid, movement_type="COMODATO", to={"kind": "ESTABLISHMENT", "ref_id": str(est.id)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "COMODATO_AGREEMENT_REQUIRED"
    r = await client.post("/api/v1/comodato-agreements", json={
        "establishment_id": str(est.id), "deposit_required": False, "deposit_cents": 5000,
        "installation_fee_required": False, "upgrade_policy": "NEGOTIATED"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "DEPOSIT_INCONSISTENT"
    r = await client.post("/api/v1/comodato-agreements", json={
        "establishment_id": str(est.id), "deposit_required": True, "deposit_cents": 30000,
        "installation_fee_required": False, "upgrade_policy": "PAID", "upgrade_fee_cents": 15000}, headers=h)
    assert r.status_code == 201, r.text
    ag = r.json()
    assert ag["maintenance_paid_by"] == "RD" and ag["breakage_paid_by"] == "ESTABLISHMENT"
    r2 = await client.post("/api/v1/comodato-agreements", json={
        "establishment_id": str(other_est.id), "deposit_required": False, "installation_fee_required": True,
        "installation_fee_cents": 10000, "upgrade_policy": "FREE"}, headers=h)
    r = await _mv(client, h, iid, movement_type="COMODATO", to={"kind": "ESTABLISHMENT", "ref_id": str(est.id)},
                  comodato_agreement_id=r2.json()["id"])
    assert r.status_code == 400 and r.json()["error"]["code"] == "COMODATO_AGREEMENT_MISMATCH"
    r = await _mv(client, h, iid, movement_type="COMODATO", to={"kind": "ESTABLISHMENT", "ref_id": str(est.id)},
                  comodato_agreement_id=ag["id"])
    assert r.status_code == 201 and r.json()["item"]["status"] == "COMODATO"
    # não encerra o comodato com equipamento pendente
    r = await client.post(f"/api/v1/comodato-agreements/{ag['id']}/end", json={"reason": "Cancelamento da parceria"},
                          headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "COMODATO_ITEMS_PENDING"
    r = await _mv(client, h, iid, movement_type="RETURN", to={"kind": "WAREHOUSE", "city_key": "GO:goiania"},
                  reason="Retirada no cancelamento")
    assert r.status_code == 201 and r.json()["item"]["comodato_agreement_id"] is None
    r = await client.post(f"/api/v1/comodato-agreements/{ag['id']}/end", json={"reason": "Cancelamento da parceria"},
                          headers=h)
    assert r.status_code == 200 and r.json()["status"] == "ENDED"


async def test_equipment_scope_city_technician_and_org(client, db, admin):
    h = admin.headers
    org_a = await mk_org(db)
    est_a = await mk_est(db, org_a, "Goiânia", "GO")
    gyn = await _item(client, h, name="TV Goiânia")
    sp = await _item(client, h, name="TV SP", warehouse_city_key="SP:sao-paulo")
    central = await _item(client, h, name="TV central", warehouse_city_key=None)
    tech = await make_user(db, role="technician", scope=("CITY", "GO:goiania"))
    ts = await login_as(client, tech)
    r = await client.get("/api/v1/equipment", params={"tracking": "SERIAL"}, headers=ts.headers)
    assert {i["id"] for i in r.json()["items"]} == {gyn["id"]}
    for other in (sp["id"], central["id"]):
        assert (await client.get(f"/api/v1/equipment/{other}", headers=ts.headers)).status_code == 404
        r = await _mv(client, ts.headers, other, movement_type="TRANSFER",
                      to={"kind": "TECHNICIAN", "ref_id": str(tech.id)})
        assert r.status_code == 404
    # técnico pega item do estoque da sua cidade
    r = await _mv(client, ts.headers, gyn["id"], movement_type="TRANSFER",
                  to={"kind": "TECHNICIAN", "ref_id": str(tech.id)})
    assert r.status_code == 201 and r.json()["item"]["status"] == "WITH_TECHNICIAN"
    # e ainda o enxerga (está com ele)
    assert (await client.get(f"/api/v1/equipment/{gyn['id']}", headers=ts.headers)).status_code == 200
    # não pode instalar em tela de outra cidade
    org_b = await mk_org(db)
    est_b = await mk_est(db, org_b, "Anápolis", "GO")
    scr_b = await _screen(db, est_b)
    r = await _mv(client, ts.headers, gyn["id"], movement_type="INSTALL", to={"kind": "SCREEN", "ref_id": str(scr_b.id)})
    assert r.status_code == 404
    scr_a = await _screen(db, est_a)
    r = await _mv(client, ts.headers, gyn["id"], movement_type="INSTALL", to={"kind": "SCREEN", "ref_id": str(scr_a.id)})
    assert r.status_code == 201
    # perfil de estabelecimento não tem acesso ao estoque
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org_a.id))
    os_ = await login_as(client, owner)
    assert (await client.get("/api/v1/equipment", headers=os_.headers)).status_code == 403


async def test_patch_rejects_type_change(client, db, admin):
    it = await _item(client, admin.headers)
    r = await client.patch(f"/api/v1/equipment/{it['id']}", json={"type": "CABLE"}, headers=admin.headers)
    assert r.status_code == 422
    r = await client.patch(f"/api/v1/equipment/{it['id']}", json={"condition": "DAMAGED", "notes": "Risco na tela"},
                           headers=admin.headers)
    assert r.status_code == 200 and r.json()["condition"] == "DAMAGED"


# ─── Concorrência (PostgreSQL real, duas transações) ─────────────────────────

class _Ctx:
    user_id = None
    kind = "system"
    origin = "test"


async def _concurrent_exits(db, item_id, targets, *, quantity=1, source=None):
    eng = create_engine(nullpool=True)
    sf = session_factory(eng)
    barrier = asyncio.Event()

    async def worker(target):
        async with sf() as s:
            await barrier.wait()
            try:
                it = await assets.lock_item(s, item_id)
                await assets.move(s, _Ctx(), it, "INSTALL", assets.Location("SCREEN", target), source=source,
                                  quantity=quantity, audit_it=False)
                await asyncio.sleep(0.2)  # segura o lock: a outra transação precisa esperar
                await s.commit()
                return "ok"
            except DomainError as e:
                await s.rollback()
                return e.code

    tasks = [asyncio.create_task(worker(t)) for t in targets]
    await asyncio.sleep(0.05)
    barrier.set()
    res = await asyncio.gather(*tasks)
    await eng.dispose()
    return res


async def test_two_simultaneous_exits_of_same_serial_item_only_one_wins(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org)
    s1, s2 = await _screen(db, est), await _screen(db, est)
    it = await _item(client, admin.headers)
    iid = uuid.UUID(it["id"])
    res = await _concurrent_exits(db, iid, [s1.id, s2.id])
    assert sorted(res) == ["EQUIPMENT_INVALID_MOVEMENT", "ok"]
    rows = (await db.execute(select(EquipmentMovement).where(EquipmentMovement.item_id == iid))).scalars().all()
    assert [m.movement_type for m in sorted(rows, key=lambda m: m.seq)] == ["ENTRY", "INSTALL"]
    derived = await assets.derive_serial_location(db, iid)
    item = await db.get(EquipmentItem, iid, populate_existing=True)
    assert derived["kind"] == "SCREEN" and derived["ref_id"] == str(item.location_screen_id)


async def test_two_simultaneous_bulk_exits_cannot_overdraw(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org)
    s1, s2 = await _screen(db, est), await _screen(db, est)
    r = await client.post("/api/v1/equipment", json={"type": "REMOTE", "tracking": "BULK", "name": "Controle",
                                                     "quantity": 10, "warehouse_city_key": "GO:goiania"},
                          headers=admin.headers)
    cid = uuid.UUID(r.json()["id"])
    res = await _concurrent_exits(db, cid, [s1.id, s2.id], quantity=6,
                                  source=assets.Location("WAREHOUSE", None, "GO:goiania"))
    assert sorted(res) == ["EQUIPMENT_INSUFFICIENT_BALANCE", "ok"]
    bal = {(b["kind"], b["city_key"]): b["quantity"] for b in await assets.balances(db, cid)}
    assert bal[("WAREHOUSE", "GO:goiania")] == 4
    assert sum(v for (k, _), v in bal.items() if k == "SCREEN") == 6
