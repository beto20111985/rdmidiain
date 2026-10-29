"""RDPP/1 §3–§6, §8 — manifesto (gerador REAL do W-INV, ETag/304, assinatura), mídia com
Range/If-Range autorizada só pelo manifesto da tela, âncora write-once, atualização do app
por ondas/fila, PIN técnico (PBKDF2) e /metrics sem consulta por scrape."""
from __future__ import annotations

import hashlib
import secrets
import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.models.audit import AuditLog
from app.models.devices import DeviceCommand
from app.modules.playback import protocol as proto
from app.modules.playback import vectors
from tests.factories import assign, login_as, make_establishment, make_org, make_user
from tests.player_support import SimDevice, make_screen, now_ms, pair_via_panel, store_manifest


async def _paired(client, db, role="employee"):
    org = await make_org(db)
    est = await make_establishment(db, org)
    screen = await make_screen(db, est)
    u = await make_user(db, role=role)
    if role == "employee":
        # REQ-RBAC-006: comandos remotos só admin/técnico — o operador de teste acumula técnico
        await assign(db, u, "technician")
    s = await login_as(client, u)
    dev = SimDevice()
    dev = await pair_via_panel(client, s.headers, screen.id, dev)
    return org, est, screen, s, dev


async def test_manifest_from_inventory_etag_304_and_signature(client, db):
    org, est, screen, s, dev = await _paired(client, db)
    r = await client.get(f"/api/v1/player/pairing/requests/{dev.request_id}/result", headers=dev.result_headers())
    keys = {k["key_id"]: k["public_key"] for k in r.json()["server_keys"]}
    r = await client.get("/api/v1/player/manifest", headers=dev.headers)
    assert r.status_code == 200, r.text
    assert "x-server-time-ms" in r.headers
    env = r.json()
    m = env["manifest"]
    assert env["alg"] == "ES256" and env["key_id"] in keys
    assert proto.verify_es256(keys[env["key_id"]], m, env["signature"])  # player verifica com server_keys do pareamento
    assert m["schema"] == "rd.manifest/1" and m["screen_id"] == str(screen.id)
    etag = r.headers["etag"]
    assert etag == f'"{m["version"]}"'
    r2 = await client.get("/api/v1/player/manifest", headers={**dev.headers, "If-None-Match": etag})
    assert r2.status_code == 304 and r2.headers["etag"] == etag and "x-server-time-ms" in r2.headers and not r2.content
    r3 = await client.get("/api/v1/player/manifest", headers={**dev.headers, "If-None-Match": '"999999"'})
    assert r3.status_code == 200
    # adulteração detectável
    tampered = dict(m, screen_code="TEL-SP-ZZZZ")
    assert not proto.verify_es256(keys[env["key_id"]], tampered, env["signature"])


@pytest.fixture
def storage(tmp_path):
    from app.core import storage as core_storage

    st = core_storage.LocalStorageProvider(tmp_path / "media-root")
    core_storage.set_storage(st)
    yield st
    core_storage.set_storage(None)


async def _ready_media(db, storage, org, data: bytes):
    from app.models.media import MediaFile

    sha = hashlib.sha256(data).hexdigest()
    key = f"media/{uuid.uuid4()}.mp4"

    async def chunks():
        yield data

    await storage.put_stream(key, chunks(), "video/mp4")
    mf = MediaFile(id=uuid.uuid4(), org_id=org.id, name="spot", original_filename="spot.mp4", kind="VIDEO",
                   source_format="mp4", source_mime="video/mp4", source_size_bytes=len(data), source_sha256=sha,
                   status="READY", storage_key=key, mime_type="video/mp4", size_bytes=len(data), sha256=sha,
                   width=1920, height=1080)
    db.add(mf)
    await db.commit()
    return mf


async def test_media_range_if_range_and_authorization_by_manifest(client, db, storage):
    org, est, screen, s, dev = await _paired(client, db)
    data = secrets.token_bytes(4096)
    mf = await _ready_media(db, storage, org, data)
    m = vectors.base_manifest(version=1000)
    m["manifest_id"] = str(uuid.uuid4())
    m["media"]["m1"]["media_id"] = str(mf.id)
    m["horizon"] = {"from_ms": now_ms(), "to_ms": now_ms() + 86_400_000, "repeat_period_ms": vectors.WEEK_MS}
    await store_manifest(db, screen.id, m)
    # outra tela/organização com o próprio dispositivo: NÃO pode baixar a mídia da tela 1
    org2, est2, screen2, s2, dev2 = await _paired(client, db)
    url = f"/api/v1/player/media/{mf.id}"
    r = await client.get(url, headers=dev.headers)  # autorizado pelo manifesto vigente (consulta o W-INV)
    assert r.status_code == 200 and r.content == data, r.text[:200]
    assert r.headers["etag"] == f'"{mf.sha256}"' and r.headers["accept-ranges"] == "bytes"
    assert "x-server-time-ms" in r.headers
    r = await client.get(url, headers={**dev.headers, "Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == data[10:20] and r.headers["content-range"] == f"bytes 10-19/{len(data)}"
    r = await client.get(url, headers={**dev.headers, "Range": "bytes=100-", "If-Range": f'"{mf.sha256}"'})
    assert r.status_code == 206 and r.content == data[100:]
    r = await client.get(url, headers={**dev.headers, "Range": "bytes=100-", "If-Range": '"outro"'})
    assert r.status_code == 200 and r.content == data  # If-Range divergente → arquivo inteiro
    r = await client.get(url, headers={**dev.headers, "Range": f"bytes={len(data) + 5}-"})
    assert r.status_code == 416
    r = await client.get(url, headers=dev2.headers)
    assert r.status_code == 404 and "x-server-time-ms" in r.headers
    assert (await client.get(f"/api/v1/player/media/{uuid.uuid4()}", headers=dev.headers)).status_code == 404
    assert (await client.get(url)).status_code == 401
    # referenciador para o GC do W-MEDIA: mídia usada por manifesto recente
    from app.modules.media.service import count_references

    refs = await count_references(db, [mf.id])
    assert sum(int(v) for v in (refs.get(mf.id) or refs.get(str(mf.id)) or {}).values()) >= 1, refs


async def test_anchor_write_once(client, db):
    org, est, screen, s, dev = await _paired(client, db)
    a = now_ms() // 1000 * 1000 - 5000
    r = await client.post("/api/v1/player/anchor", json={"anchor_ms": a}, headers=dev.headers)
    assert r.status_code == 200 and r.json()["anchor_ms"] == a
    r = await client.post("/api/v1/player/anchor", json={"anchor_ms": a + 60_000 - 60_000 + 1000}, headers=dev.headers)
    assert r.status_code == 200 and r.json()["anchor_ms"] == a  # write-once: devolve o existente
    for bad in (a + 1, now_ms() // 1000 * 1000 + 3_600_000):
        r = await client.post("/api/v1/player/anchor", json={"anchor_ms": bad}, headers=dev.headers)
        assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_ANCHOR"


@pytest.fixture
def release_key(monkeypatch):
    k = proto.generate_private_key()
    monkeypatch.setenv("PLAYER_RELEASE_PUBLIC_KEYS", '{"rel-test": "%s"}' % proto.public_spki_b64u(k))
    return k


async def test_app_update_waves_queue_and_release_signature(client, db, release_key):
    from app.modules.playback.updates import update_message

    org, est, screen, s, dev = await _paired(client, db)
    screen2 = await make_screen(db, est)
    dev2 = await pair_via_panel(client, s.headers, screen2.id)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    sha = hashlib.sha256(b"pacote").hexdigest()
    body = {"platform": "web", "version": "1.2.0", "url": "https://cdn.exemplo.test/p-1.2.0.zip", "sha256": sha,
            "size": 6, "key_id": "rel-test", "signature": proto.sign_es256(release_key, update_message("web", "1.2.0", sha, 6))}
    # assinatura de outra chave → recusada
    bad = dict(body, signature=proto.sign_es256(proto.generate_private_key(), update_message("web", "1.2.0", sha, 6)))
    r = await client.post("/api/v1/devices/releases", json=bad, headers=sa.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_RELEASE_SIGNATURE"
    r = await client.post("/api/v1/devices/releases", json=body, headers=sa.headers)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    # funcionário sem escopo global não publica versões
    assert (await client.post("/api/v1/devices/releases", json=body, headers=s.headers)).status_code in (403, 409)
    q = "/api/v1/player/app-update?platform=web&version=1.0.0"
    assert (await client.get(q, headers=dev.headers)).status_code == 204  # DRAFT
    r = await client.patch(f"/api/v1/devices/releases/{rid}", json={"status": "PUBLISHED", "rollout_percent": 0,
                                                                    "max_concurrent": 1}, headers=sa.headers)
    assert r.status_code == 200
    assert (await client.get(q, headers=dev.headers)).status_code == 204  # onda 0%
    await client.patch(f"/api/v1/devices/releases/{rid}", json={"rollout_percent": 100}, headers=sa.headers)
    r = await client.get(q, headers=dev.headers)
    assert r.status_code == 200, r.text
    offer = r.json()
    assert offer["version"] == "1.2.0" and offer["key_id"] == "rel-test" and "x-server-time-ms" in r.headers
    pub = proto.public_spki_b64u(release_key)
    assert proto.verify_es256(pub, update_message("web", offer["version"], offer["sha256"], offer["size"]), offer["signature"])
    # fila: limite de 1 download simultâneo
    r = await client.get(q, headers=dev2.headers)
    assert r.status_code == 204 and r.headers.get("retry-after") == "300"
    # o primeiro já atualizado libera a vaga
    assert (await client.get("/api/v1/player/app-update?platform=web&version=1.2.0", headers=dev.headers)).status_code == 204
    assert (await client.get(q, headers=dev2.headers)).status_code == 200
    # grupos: só o grupo "piloto"
    await client.patch(f"/api/v1/devices/releases/{rid}", json={"target_groups": ["piloto"], "max_concurrent": 50},
                       headers=sa.headers)
    assert (await client.get(q, headers=dev.headers)).status_code == 204
    await client.patch(f"/api/v1/devices/{dev.device_id}", json={"update_group": "piloto"}, headers=s.headers)
    assert (await client.get(q, headers=dev.headers)).status_code == 200
    # plataforma divergente da registrada
    r = await client.get("/api/v1/player/app-update?platform=android&version=1.0.0", headers=dev.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "PLATFORM_MISMATCH"


async def test_tech_pin_pbkdf2_never_in_clear(client, db):
    org, est, screen, s, dev = await _paired(client, db)
    screen2 = await make_screen(db, est)
    await pair_via_panel(client, s.headers, screen2.id)
    r = await client.post(f"/api/v1/screens/{screen.id}/tech-pin", json={"pin": "482913", "reason": "troca de técnico"},
                          headers=s.headers)
    assert r.status_code == 202, r.text
    pub = r.json()
    assert pub["command"] == "set_tech_pin" and pub["args"] == {"algo": "pbkdf2-sha256", "iterations": 210000}
    row = await db.get(DeviceCommand, uuid.UUID(pub["id"]))
    a = row.args
    assert set(a) == {"algo", "iterations", "salt_b64u", "hash_b64u"} and "482913" not in str(a)
    assert hashlib.pbkdf2_hmac("sha256", b"482913", proto.b64u_decode(a["salt_b64u"]), 210000, 32) == proto.b64u_decode(a["hash_b64u"])
    logs = (await db.execute(select(AuditLog).where(AuditLog.entity_id == pub["id"]))).scalars().all()
    assert logs and "482913" not in str([(x.after_data, x.before_data) for x in logs])
    r = await client.post(f"/api/v1/establishments/{est.id}/tech-pin", json={"pin": "111222", "reason": "todas"},
                          headers=s.headers)
    assert r.status_code == 202 and r.json()["total"] == 2
    salts = {(await db.get(DeviceCommand, uuid.UUID(x["id"]))).args["salt_b64u"] for x in r.json()["items"]}
    assert len(salts) == 2
    for bad in ("12a456", "12345"):
        assert (await client.post(f"/api/v1/screens/{screen.id}/tech-pin", json={"pin": bad, "reason": "x" * 4},
                                  headers=s.headers)).status_code == 422
    r = await client.post(f"/api/v1/screens/{screen.id}/commands", json={"command": "set_tech_pin", "reason": "x" * 4,
                                                                        "args": {"hash_b64u": "x"}}, headers=s.headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "USE_TECH_PIN_ENDPOINT"


async def test_metrics_do_not_query_database_per_scrape(client, db):
    await client.get("/metrics")  # aquece o cache (no máximo 1 consulta/min no cluster)
    statements: list[str] = []

    def _count(conn, cursor, statement, *a, **k):
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _count)
    try:
        for _ in range(5):
            r = await client.get("/metrics")
            assert r.status_code == 200
    finally:
        event.remove(Engine, "before_cursor_execute", _count)
    assert statements == [], statements
    txt = r.text
    assert "rd_ws_player_connections" in txt and txt.count("# HELP rd_screens_total") <= 1
    for line in txt.splitlines():
        if line.startswith("# HELP "):
            assert txt.count(line) == 1, line


@pytest.mark.parametrize("origin", ["http://localhost:8088", "https://player.rd.local"])
async def test_cors_allows_player_origins_and_exposes_protocol_headers(client, db, origin):
    r = await client.options("/api/v1/player/manifest", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization,if-none-match,range,if-range,x-rd-device-signature"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin
    org, est, screen, s, dev = await _paired(client, db)
    r = await client.get("/api/v1/player/manifest", headers={**dev.headers, "Origin": origin})
    exposed = r.headers.get("access-control-expose-headers", "").lower()
    assert "x-server-time-ms" in exposed and "etag" in exposed
    r = await client.options("/api/v1/player/manifest", headers={"Origin": "https://evil.example",
                                                                 "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"


async def test_commands_include_author_name_resolved_in_batch(client, db):
    """O painel mostra QUEM emitiu o comando (``issued_by_name``), resolvido numa única consulta."""
    org, est, screen, _s, dev = await _paired(client, db)
    s = await login_as(client, await _cmd_user(db, "Gerente Bruno Reis"))
    tech = await _cmd_user(db, "Técnica Ana Lima")
    st = await login_as(client, tech)
    for i in range(3):
        r = await client.post(f"/api/v1/screens/{screen.id}/commands",
                              json={"command": "resync", "reason": f"teste {i}"}, headers=(s.headers if i % 2 else st.headers))
        assert r.status_code == 202, r.text
    assert r.json()["issued_by_name"] == "Técnica Ana Lima"  # resposta do POST (último emitido pela técnica)
    statements: list[str] = []

    def _count(conn, cursor, statement, *a, **k):
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _count)
    try:
        r = await client.get(f"/api/v1/screens/{screen.id}/commands", headers=s.headers)
    finally:
        event.remove(Engine, "before_cursor_execute", _count)
    assert r.status_code == 200
    by_user = {(x["issued_by_user_id"], x["issued_by_name"]) for x in r.json()["items"]}
    assert by_user == {(str(tech.id), "Técnica Ana Lima"), (str(s.user.id), s.user.full_name)}
    name_queries = [q for q in statements if q.lstrip().startswith("SELECT users.id, users.full_name")]
    assert len(name_queries) == 1, name_queries  # em lote, sem N+1


async def _cmd_user(db, name: str):
    """Usuário com perfil próprio (GLOBAL) com ``screens.view`` + ``screens.command``, sem 2FA."""
    import secrets as _secrets

    from app.models.rbac import Permission, Role, RoleAssignment, RolePermission

    role = Role(id=uuid.uuid4(), slug=f"tcmd_{_secrets.token_hex(4)}", name="Teste comandos", is_system=False)
    db.add(role)
    await db.flush()
    for code in ("screens.view", "screens.command"):
        p = (await db.execute(select(Permission).where(Permission.code == code))).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=p.id))
    u = await make_user(db, name=name)
    db.add(RoleAssignment(id=uuid.uuid4(), user_id=u.id, role_id=role.id, scope_type="GLOBAL"))
    await db.commit()
    return u
