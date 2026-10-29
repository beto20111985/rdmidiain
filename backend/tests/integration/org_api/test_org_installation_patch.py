"""W-ORG — B1: instalação agendada sem tela recebe a tela via PATCH e conclui; B2: nomes dos autores
(manutenção/instalação) resolvidos em lote, sem N+1."""
import io
import uuid
from datetime import datetime, timezone

from app.models.screens import Screen
from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org, staff_session
from tests.integration.org_api.test_org_list_names import count_selects


def _png() -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(b, "PNG")
    return b.getvalue()


async def _screen(db, est, code, **kw):
    s = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=code, name=code, **kw)
    db.add(s)
    await db.commit()
    return s


async def test_installation_without_screen_gets_screen_via_patch_and_completes(client, db, admin, storage):
    org = await mk_org(db)
    est = await mk_est(db, org, lat=-16.68, lng=-49.25)
    other_est = await mk_est(db, await mk_org(db))
    scr = await _screen(db, est, "TEL-GO-PA22")
    foreign = await _screen(db, other_est, "TEL-GO-PB22")
    dead = await _screen(db, est, "TEL-GO-PC22", status="DECOMMISSIONED", sellable=False,
                         decommissioned_at=datetime.now(timezone.utc))
    tech = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Tom Técnico")
    r = await client.post("/api/v1/installations", json={"establishment_id": str(est.id),
                                                         "technician_user_id": str(tech.id)}, headers=admin.headers)
    assert r.status_code == 201 and r.json()["screen_id"] is None
    iid = r.json()["id"]
    base = f"/api/v1/installations/{iid}"
    ts = await login_as(client, tech)
    await client.post(f"{base}/start", headers=ts.headers)
    required = [i["key"] for i in r.json()["checklist"] if i["required"]]
    await client.put(f"{base}/checklist", json={"items": [{"key": k, "done": True} for k in required]},
                     headers=ts.headers)
    await client.put(f"{base}/geolocation", json={"lat": -16.68, "lng": -49.25}, headers=ts.headers)
    await client.post(f"{base}/photos", data={"kind": "TV_WORKING"}, files={"file": ("a.png", _png(), "image/png")},
                      headers=ts.headers)
    await client.post(f"{base}/signature", data={"signer_name": "Gerente Ana"},
                      files={"file": ("s.png", _png(), "image/png")}, headers=ts.headers)
    waiver = {"connection_test_waiver": "Player será pareado após a instalação elétrica"}
    r = await client.post(f"{base}/complete", json=waiver, headers=ts.headers)
    assert r.status_code == 400 and r.json()["error"]["details"]["missing"] == ["screen"]
    # vínculo inválido: tela de outro estabelecimento (404, sem vazar) e tela retirada (409)
    r = await client.patch(base, json={"screen_id": str(foreign.id)}, headers=ts.headers)
    assert r.status_code == 404
    r = await client.patch(base, json={"screen_id": str(dead.id)}, headers=ts.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "SCREEN_DECOMMISSIONED"
    r = await client.patch(base, json={"screen_code": "TEL-GO-PA22"}, headers=ts.headers)
    assert r.status_code == 422  # campo desconhecido
    r = await client.patch(base, json={"screen_id": str(scr.id)}, headers=ts.headers)
    assert r.status_code == 200, r.text
    assert r.json()["screen_id"] == str(scr.id) and r.json()["screen_code"] == "TEL-GO-PA22"
    r = await client.post(f"{base}/complete", json=waiver, headers=ts.headers)
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    s = await db.get(Screen, scr.id, populate_existing=True)
    assert s.installed_by == tech.id
    # depois de concluída não muda mais
    r = await client.patch(base, json={"screen_id": None}, headers=ts.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSTALLATION_INVALID_STATE"
    acts = await audit_actions(db, iid)
    assert "installation.updated" in acts and acts[-1] == "installation.completed"


async def test_patch_screen_resets_connection_test_and_reassigns_technician(client, db, admin, storage):
    org = await mk_org(db)
    est = await mk_est(db, org)
    s1, s2 = await _screen(db, est, "TEL-GO-RA22"), await _screen(db, est, "TEL-GO-RB22")
    t1 = await make_user(db, role="technician", scope=("CITY", "GO:goiania"))
    t2 = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Bia Técnica")
    sp = await make_user(db, role="technician", scope=("CITY", "SP:sao-paulo"))
    r = await client.post("/api/v1/installations", json={"establishment_id": str(est.id), "screen_id": str(s1.id),
                                                         "technician_user_id": str(t1.id)}, headers=admin.headers)
    base = f"/api/v1/installations/{r.json()['id']}"
    ts = await login_as(client, t1)
    await client.post(f"{base}/start", headers=ts.headers)
    r = await client.post(f"{base}/connection-test", headers=ts.headers)
    assert r.json()["result"]["status"] == "NOT_PAIRED"
    r = await client.patch(base, json={"screen_id": str(s2.id)}, headers=ts.headers)
    assert r.json()["connection_test"] is None  # o teste era da tela anterior
    r = await client.patch(base, json={"technician_user_id": str(sp.id)}, headers=admin.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "TECHNICIAN_OUT_OF_SCOPE"
    r = await client.patch(base, json={"technician_user_id": None}, headers=admin.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "TECHNICIAN_REQUIRED"
    r = await client.patch(base, json={"technician_user_id": str(t2.id), "scheduled_date": "2026-11-03"},
                           headers=admin.headers)
    assert r.status_code == 200 and r.json()["technician_name"] == "Bia Técnica"
    assert r.json()["scheduled_date"] == "2026-11-03"
    # técnico de outra cidade não enxerga nem altera (404)
    far = await staff_session(client, db, "technician", ("CITY", "SP:sao-paulo"))
    assert (await client.patch(base, json={"notes": "x"}, headers=far.headers)).status_code == 404


async def test_author_names_in_maintenance_and_installation_without_n_plus_1(client, db, storage):
    org = await mk_org(db)
    est = await mk_est(db, org)
    adm_user = await make_user(db, role="admin", mfa=True, name="Adriana Admin")
    adm = await login_as(client, adm_user)
    tech = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Caio Campo")
    scr = await _screen(db, est, "TEL-GO-NA22")
    r = await client.post("/api/v1/maintenance", json={"screen_id": str(scr.id), "component": "TV",
                                                       "description": "Sem sinal HDMI desde ontem",
                                                       "technician_user_id": str(tech.id)}, headers=adm.headers)
    oid = r.json()["id"]
    ts = await login_as(client, tech)
    await client.post(f"/api/v1/maintenance/{oid}/notes", json={"note": "Cabo trocado"}, headers=ts.headers)
    det = (await client.get(f"/api/v1/maintenance/{oid}", headers=adm.headers)).json()
    assert det["opened_by_name"] == "Adriana Admin"
    by_event = {h["event"]: h for h in det["history"]}
    assert by_event["OPENED"]["actor_name"] == "Adriana Admin"
    assert by_event["ASSIGNED"]["technician_name"] == "Caio Campo"
    assert by_event["NOTE"]["actor_name"] == "Caio Campo"
    lst = (await client.get("/api/v1/maintenance", headers=adm.headers)).json()["items"]
    assert lst[0]["opened_by_name"] == "Adriana Admin"
    # histórico maior → mesma quantidade de SELECTs no detalhe (nomes em lote)
    with count_selects() as few:
        await client.get(f"/api/v1/maintenance/{oid}", headers=adm.headers)
    for i in range(5):
        await client.post(f"/api/v1/maintenance/{oid}/notes", json={"note": f"Nota {i}"}, headers=ts.headers)
    with count_selects() as many:
        det = (await client.get(f"/api/v1/maintenance/{oid}", headers=adm.headers)).json()
    assert len(det["history"]) == 8 and len(many) == len(few)
    # instalação: quem agendou e quem enviou a foto
    r = await client.post("/api/v1/installations", json={"establishment_id": str(est.id), "screen_id": str(scr.id),
                                                         "technician_user_id": str(tech.id)}, headers=adm.headers)
    iid = r.json()["id"]
    assert r.json()["created_by_name"] == "Adriana Admin"
    await client.post(f"/api/v1/installations/{iid}/start", headers=ts.headers)
    await client.post(f"/api/v1/installations/{iid}/photos", data={"kind": "SITE"},
                      files={"file": ("a.png", _png(), "image/png")}, headers=ts.headers)
    det = (await client.get(f"/api/v1/installations/{iid}", headers=adm.headers)).json()
    assert det["photos"][0]["uploaded_by_name"] == "Caio Campo"
    lst = (await client.get("/api/v1/installations", headers=adm.headers)).json()["items"]
    assert lst[0]["created_by_name"] == "Adriana Admin"
