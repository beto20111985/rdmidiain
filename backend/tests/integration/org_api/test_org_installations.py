"""W-ORG — instalação pelo técnico: checklist, geolocalização, teste REAL de conexão (presença Redis),
fotos e assinatura (imagens reais validadas), conclusão com requisitos e escopo CITY do técnico."""
import io
import secrets
import time
import uuid

from sqlalchemy import select

from app.core.timeutils import utcnow
from app.models.installations import InstallationPhoto
from app.models.screens import Screen
from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org, staff_session


def png_bytes(w=64, h=48, color=(10, 120, 200)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(w=80, h=60) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


async def _setup(client, db, admin):
    org = await mk_org(db)
    est = await mk_est(db, org, "Goiânia", "GO", lat=-16.6869, lng=-49.2648)
    scr = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=f"TEL-GO-{uuid.uuid4().hex[:4].upper()}",
                 name="TV Entrada")
    db.add(scr)
    await db.commit()
    tech = await make_user(db, role="technician", scope=("CITY", "GO:goiania"))
    r = await client.post("/api/v1/installations", json={"establishment_id": str(est.id), "screen_id": str(scr.id),
                                                         "technician_user_id": str(tech.id),
                                                         "scheduled_date": "2026-10-10"}, headers=admin.headers)
    assert r.status_code == 201, r.text
    return org, est, scr, tech, r.json()


async def test_installation_full_flow_with_waiver(client, db, admin, storage):
    org, est, scr, tech, inst = await _setup(client, db, admin)
    iid = inst["id"]
    assert inst["status"] == "SCHEDULED" and len(inst["checklist"]) >= 7
    ts = await login_as(client, tech)
    base = f"/api/v1/installations/{iid}"
    r = await client.put(f"{base}/checklist", json={"items": [{"key": "power", "done": True}]}, headers=ts.headers)
    assert r.status_code == 409  # ainda não iniciada
    assert (await client.post(f"{base}/start", headers=ts.headers)).status_code == 200
    # outro técnico (mesma cidade) não executa a instalação de outro
    other = await staff_session(client, db, "technician", ("CITY", "GO:goiania"))
    r = await client.put(f"{base}/checklist", json={"items": [{"key": "power", "done": True}]}, headers=other.headers)
    assert r.status_code == 403
    required = [i["key"] for i in inst["checklist"] if i["required"]]
    r = await client.put(f"{base}/checklist", json={"items": [{"key": k, "done": True} for k in required]},
                         headers=ts.headers)
    assert r.status_code == 200
    r = await client.put(f"{base}/geolocation", json={"lat": -16.6872, "lng": -49.2650, "accuracy_m": 8},
                         headers=ts.headers)
    geo = r.json()["geolocation"]
    assert 0 < geo["distance_to_establishment_m"] < 100
    # foto falsa (texto com cabeçalho JPEG) é rejeitada; JPEG/PNG reais aceitos
    fake = b"\xff\xd8\xff" + b"nao sou uma imagem" * 10
    r = await client.post(f"{base}/photos", data={"kind": "TV_WORKING"}, files={"file": ("tv.jpg", fake, "image/jpeg")},
                          headers=ts.headers)
    assert r.status_code == 415 and r.json()["error"]["code"] == "FILE_CORRUPTED"
    photo = jpeg_bytes()
    r = await client.post(f"{base}/photos", data={"kind": "TV_WORKING"},
                          files={"file": ("tv.jpg", photo, "image/jpeg")}, headers=ts.headers)
    assert r.status_code == 201 and r.json()["content_type"] == "image/jpeg" and r.json()["width"] == 80
    photo_id = r.json()["id"]
    r = await client.get(f"{base}/photos/{photo_id}", headers=ts.headers)
    assert r.status_code == 200
    assert r.content == photo  # bytes íntegros vindos do StorageProvider
    # conclusão sem assinatura e sem teste de conexão → bloqueada com a lista do que falta
    r = await client.post(f"{base}/complete", json={}, headers=ts.headers)
    assert r.status_code == 400
    missing = r.json()["error"]["details"]["missing"]
    assert "signature" in missing and "connection_test" in missing
    r = await client.post(f"{base}/signature", data={"signer_name": "Ana Gerente", "signer_role": "Gerente"},
                          files={"file": ("ass.png", png_bytes(300, 120, (255, 255, 255)), "image/png")},
                          headers=ts.headers)
    assert r.status_code == 200 and r.json()["signer"]["name"] == "Ana Gerente"
    # teste de conexão REAL: sem dispositivo pareado → NOT_PAIRED (nunca "online" falso)
    r = await client.post(f"{base}/connection-test", headers=ts.headers)
    assert r.status_code == 200 and r.json()["result"]["status"] == "NOT_PAIRED"
    r = await client.post(f"{base}/complete", json={"connection_test_waiver": "curto"}, headers=ts.headers)
    assert r.status_code == 400
    r = await client.post(f"{base}/complete",
                          json={"connection_test_waiver": "Player será pareado amanhã; internet do local instável"},
                          headers=ts.headers)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "COMPLETED" and done["connection_test_waiver"]
    s = await db.get(Screen, scr.id, populate_existing=True)
    assert s.installed_by == tech.id and s.installed_at is not None
    acts = await audit_actions(db, iid)
    assert acts[0] == "installation.scheduled" and acts[-1] == "installation.completed"
    assert "installation.photo_added" in acts and "installation.signed" in acts
    photos = (await db.execute(select(InstallationPhoto).where(InstallationPhoto.installation_id == uuid.UUID(iid))))
    assert len(photos.scalars().all()) == 1


async def test_connection_test_online_from_real_presence(client, db, admin, storage, redis):
    from app.models.devices import Device
    from app.modules.presence import service as presence

    org, est, scr, tech, inst = await _setup(client, db, admin)
    dev = Device(id=uuid.uuid4(), screen_id=scr.id, public_key="test-spki", key_alg="ES256",
                 key_fingerprint=secrets.token_hex(32), platform="web", status="ACTIVE", paired_at=utcnow(),
                 pairing_method="PANEL")
    db.add(dev)
    await db.commit()
    ts = await login_as(client, tech)
    base = f"/api/v1/installations/{inst['id']}"
    await client.post(f"{base}/start", headers=ts.headers)
    r = await client.post(f"{base}/connection-test", headers=ts.headers)
    assert r.json()["result"]["status"] == "OFFLINE"  # pareado, mas sem presença
    await redis.hset(presence.k_dev(dev.id), mapping={"connected": "1", "last_seen_ms": str(int(time.time() * 1000)),
                                                      "status": "reproduzindo"})
    r = await client.post(f"{base}/connection-test", headers=ts.headers)
    res = r.json()["result"]
    assert res["status"] == "ONLINE" and res["device_id"] == str(dev.id) and res["source"] == "presence"


async def test_installation_scope_and_technician_validation(client, db, admin, storage):
    org, est, scr, tech, inst = await _setup(client, db, admin)
    far = await staff_session(client, db, "technician", ("CITY", "SP:sao-paulo"))
    assert (await client.get(f"/api/v1/installations/{inst['id']}", headers=far.headers)).status_code == 404
    r = await client.get("/api/v1/installations", headers=far.headers)
    assert r.json()["items"] == []
    # técnico informado precisa atender a cidade do estabelecimento
    sp_tech = await make_user(db, role="technician", scope=("CITY", "SP:sao-paulo"))
    r = await client.post("/api/v1/installations", json={"establishment_id": str(est.id),
                                                         "technician_user_id": str(sp_tech.id)}, headers=admin.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "TECHNICIAN_OUT_OF_SCOPE"
