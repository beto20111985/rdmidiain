"""RDPP/1 §1–§3 — pareamento (painel e login de técnico), resultado assinado, desafio/token,
revogação. PostgreSQL + Redis reais, API HTTP real (ASGI)."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.timeutils import utcnow
from app.models.audit import AuditLog
from app.models.devices import Device, PairingRequest
from app.modules.playback import protocol as proto
from tests.factories import login_as, make_establishment, make_org, make_user, totp_code
from tests.player_support import SimDevice, make_screen, now_ms, pair_via_panel


async def _setup(db, client, role="employee", scope=None):
    org = await make_org(db)
    est = await make_establishment(db, org)
    screen = await make_screen(db, est)
    u = await make_user(db, role=role, scope=scope)
    s = await login_as(client, u)
    return org, est, screen, u, s


async def test_pairing_request_validates_key_and_returns_code(client, db):
    dev = SimDevice()
    body = await dev.request_pairing(client)
    assert body["ws_path"] == "/ws/player/pairing"
    code = body["pairing_code"]
    assert len(code) == 9 and code[4] == "-" and all(c in "ABCDEFGHJMNPQRSTUVYZ23456789" for c in code.replace("-", ""))
    assert body["panel_pair_url"].endswith(f"/telas/parear?code={code}")
    assert 14 * 60_000 < body["expires_at_ms"] - now_ms() <= 15 * 60_000
    r = await client.get(f"/api/v1/player/pairing/requests/{body['pairing_request_id']}")
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    assert "x-server-time-ms" in r.headers
    # chave inválida: RSA/lixo/curva errada → 422 (envelope) com X-Server-Time-Ms
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    rsa_spki = proto.b64u_encode(rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
    p384 = proto.b64u_encode(ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
    for bad in ("AAAA", p384, rsa_spki[:390]):
        r = await client.post("/api/v1/player/pairing/requests", json={"public_key": bad, "key_alg": "ES256", "platform": "web"})
        assert r.status_code == 422, (bad[:20], r.text)
        assert r.json()["error"]["code"] in ("INVALID_PUBLIC_KEY", "VALIDATION_ERROR")
        assert "x-server-time-ms" in r.headers
    r = await client.post("/api/v1/player/pairing/requests", json={"public_key": dev.spki, "key_alg": "RS256", "platform": "web"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNSUPPORTED_KEY_ALG"


async def test_full_pairing_via_panel_token_and_audit(client, db):
    org, est, screen, u, s = await _setup(db, client, scope=("ORGANIZATION", (await make_org(db)).id))
    # escopo em OUTRA org → 404 (não vaza existência)
    dev = SimDevice()
    await dev.request_pairing(client)
    r = await client.post(f"/api/v1/screens/{screen.id}/pairing", json={"pairing_code": dev.pairing_code}, headers=s.headers)
    assert r.status_code == 404
    # resultado antes do claim → 409 PAIRING_PENDING
    r = await dev.fetch_result(client)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PAIRING_PENDING"

    u2 = await make_user(db, role="employee", scope=("ORGANIZATION", org.id))
    s2 = await login_as(client, u2)
    r = await client.post(f"/api/v1/screens/{screen.id}/pairing", json={"pairing_code": dev.pairing_code.lower()},
                          headers=s2.headers)
    assert r.status_code == 201, r.text
    assert r.json()["device"]["screen_id"] == str(screen.id)
    # código usado não serve de novo
    r = await client.post(f"/api/v1/screens/{screen.id}/pairing", json={"pairing_code": dev.pairing_code}, headers=s2.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PAIRING_ALREADY_CLAIMED"
    st = await client.get(f"/api/v1/player/pairing/requests/{dev.request_id}")
    assert st.json()["status"] == "CLAIMED"

    # resultado: assinatura errada / relógio fora de ±120 s / correto
    bad = SimDevice()
    bad.request_id = dev.request_id
    r = await client.get(f"/api/v1/player/pairing/requests/{dev.request_id}/result", headers=bad.result_headers())
    assert r.status_code == 401 and r.json()["error"]["code"] == "BAD_SIGNATURE"
    r = await client.get(f"/api/v1/player/pairing/requests/{dev.request_id}/result",
                         headers=dev.result_headers(now_ms() - 121_000))
    assert r.status_code == 401 and r.json()["error"]["code"] == "CLOCK_SKEW"
    r = await client.get(f"/api/v1/player/pairing/requests/{dev.request_id}/result")
    assert r.status_code == 401
    r = await dev.fetch_result(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["screen_id"] == str(screen.id) and body["screen_code"] == screen.friendly_id
    assert body["server_keys"] and body["server_keys"][0]["alg"] == "ES256"
    proto.load_public_spki(body["server_keys"][0]["public_key"])

    # servidor guarda SOMENTE a chave pública
    row = (await db.execute(select(Device).where(Device.id == uuid.UUID(dev.device_id)))).scalar_one()
    assert row.public_key == dev.spki and row.status == "ACTIVE" and row.pairing_method == "PANEL"
    assert not hasattr(row, "device_secret") and not hasattr(row, "device_token")

    # token: JWT typ=device de 15 min com sub/scr/org/sid
    r = await dev.login(client)
    assert r.status_code == 200, r.text
    assert r.json()["expires_in"] == 900 and abs(r.json()["server_time_ms"] - now_ms()) < 5000
    from app.core import tokens

    claims = tokens.decode(dev.token)
    assert claims["typ"] == "device" and claims["sub"] == dev.device_id and claims["scr"] == str(screen.id)
    assert claims["org"] == str(org.id) and claims["sid"] and claims["exp"] - claims["iat"] == 900
    # token de dispositivo não abre rotas de usuário; token de usuário não abre rotas do player
    assert (await client.get("/api/v1/me", headers=dev.headers)).status_code == 401
    assert (await client.post("/api/v1/player/ws-ticket", headers=s2.headers)).status_code == 401
    assert (await client.post("/api/v1/player/ws-ticket")).status_code == 401

    actions = set((await db.execute(select(AuditLog.action).where(AuditLog.entity_id == dev.device_id))).scalars())
    assert "device.paired" in actions


async def test_challenge_single_use_bad_signature_and_revoked(client, db):
    org, est, screen, u, s = await _setup(db, client)
    dev = await pair_via_panel(client, s.headers, screen.id)
    # assinatura errada (outra chave) → 401 e o desafio é consumido
    r = await client.post("/api/v1/player/auth/challenge", json={"device_id": dev.device_id})
    ch = r.json()["challenge"]
    other = proto.generate_private_key()
    sig = proto.sign_es256(other, {"challenge": ch, "device_id": dev.device_id, "purpose": proto.PURPOSE_AUTH})
    r = await client.post("/api/v1/player/auth/token", json={"device_id": dev.device_id, "challenge": ch, "signature": sig})
    assert r.status_code == 401 and r.json()["error"]["code"] == "BAD_SIGNATURE"
    good = proto.sign_es256(dev.key, {"challenge": ch, "device_id": dev.device_id, "purpose": proto.PURPOSE_AUTH})
    r = await client.post("/api/v1/player/auth/token", json={"device_id": dev.device_id, "challenge": ch, "signature": good})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CHALLENGE"  # reutilizado
    # propósito errado na mensagem assinada
    r = await client.post("/api/v1/player/auth/challenge", json={"device_id": dev.device_id})
    ch = r.json()["challenge"]
    sig = proto.sign_es256(dev.key, {"challenge": ch, "device_id": dev.device_id, "purpose": proto.PURPOSE_PAIRING_RESULT})
    r = await client.post("/api/v1/player/auth/token", json={"device_id": dev.device_id, "challenge": ch, "signature": sig})
    assert r.status_code == 401
    # desafio de outro dispositivo não serve
    r = await client.post("/api/v1/player/auth/challenge", json={"device_id": str(uuid.uuid4())})
    ch = r.json()["challenge"]
    sig = proto.sign_es256(dev.key, {"challenge": ch, "device_id": dev.device_id, "purpose": proto.PURPOSE_AUTH})
    r = await client.post("/api/v1/player/auth/token", json={"device_id": dev.device_id, "challenge": ch, "signature": sig})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CHALLENGE"

    # revogação imediata: token já emitido para de funcionar; novo token → DEVICE_REVOKED
    assert (await client.post("/api/v1/player/ws-ticket", headers=dev.headers)).status_code == 200
    r = await client.post(f"/api/v1/devices/{dev.device_id}/revoke", json={"reason": "perdido"}, headers=s.headers)
    assert r.status_code == 200 and r.json()["status"] == "REVOKED"
    r = await client.post("/api/v1/player/ws-ticket", headers=dev.headers)
    assert r.status_code == 401 and r.json()["error"]["code"] == "DEVICE_REVOKED"
    assert "x-server-time-ms" in r.headers
    r = await dev.login(client)
    assert r.status_code == 401 and r.json()["error"]["code"] == "DEVICE_REVOKED"
    # a chave do dispositivo revogado não pode ser pareada de novo (novo device_id = nova chave)
    r = await client.post("/api/v1/player/pairing/requests", json={"public_key": dev.spki, "key_alg": "ES256", "platform": "web"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "KEY_ALREADY_USED"


async def test_repairing_revokes_previous_device(client, db):
    org, est, screen, u, s = await _setup(db, client)
    d1 = await pair_via_panel(client, s.headers, screen.id)
    d2 = await pair_via_panel(client, s.headers, screen.id)
    assert d1.device_id != d2.device_id
    rows = {str(d.id): d for d in (await db.execute(select(Device).where(Device.screen_id == screen.id))).scalars()}
    assert rows[d1.device_id].status == "REVOKED" and rows[d1.device_id].revoked_reason
    assert rows[d2.device_id].status == "ACTIVE"
    r = await client.post("/api/v1/player/ws-ticket", headers=d1.headers)
    assert r.status_code == 401 and r.json()["error"]["code"] == "DEVICE_REVOKED"
    assert (await client.post("/api/v1/player/ws-ticket", headers=d2.headers)).status_code == 200
    from app.models.screens import Screen

    sc = (await db.execute(select(Screen).where(Screen.id == screen.id).execution_options(populate_existing=True))).scalar_one()
    assert str(sc.device_id) == d2.device_id


async def test_expired_code_rejected(client, db):
    org, est, screen, u, s = await _setup(db, client)
    dev = SimDevice()
    await dev.request_pairing(client)
    await db.execute(update(PairingRequest).where(PairingRequest.id == uuid.UUID(dev.request_id))
                     .values(expires_at=utcnow() - timedelta(seconds=1)))
    await db.commit()
    r = await client.post(f"/api/v1/screens/{screen.id}/pairing", json={"pairing_code": dev.pairing_code}, headers=s.headers)
    assert r.status_code == 410 and r.json()["error"]["code"] == "PAIRING_CODE_EXPIRED"
    st = await client.get(f"/api/v1/player/pairing/requests/{dev.request_id}")
    assert st.json()["status"] == "EXPIRED"
    r = await client.post(f"/api/v1/screens/{screen.id}/pairing", json={"pairing_code": "ABCD-2345"}, headers=s.headers)
    assert r.status_code == 404
    r = await client.post(f"/api/v1/screens/{screen.id}/pairing", json={"pairing_code": "LIXO"}, headers=s.headers)
    assert r.status_code == 422


async def test_pairing_by_technician_login_on_device(client, db, mail):
    org = await make_org(db)
    est = await make_establishment(db, org)
    screen = await make_screen(db, est)
    other_est = await make_establishment(db, org)
    other_screen = await make_screen(db, other_est)
    tech = await make_user(db, role="technician", scope=("ESTABLISHMENT", est.id), mfa=True)
    dev = SimDevice(platform="android")
    await dev.request_pairing(client)
    url = f"/api/v1/player/pairing/requests/{dev.request_id}/claim"
    # senha errada → 401 (conta para o lockout)
    r = await client.post(url, json={"login": tech.email, "password": "errada-123", "screen_code": screen.friendly_id})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    # 2FA obrigatório quando habilitado
    r = await client.post(url, json={"login": tech.email, "password": tech.password, "screen_code": screen.friendly_id})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_MFA_CODE"
    # tela fora do escopo do técnico → 404
    r = await client.post(url, json={"login": tech.email, "password": tech.password, "totp": totp_code(tech),
                                     "screen_code": other_screen.friendly_id})
    assert r.status_code == 404
    r = await client.post(url, json={"login": tech.email, "password": tech.password, "totp": totp_code(tech),
                                     "screen_code": screen.friendly_id})
    assert r.status_code == 200, r.text
    r = await dev.fetch_result(client)
    assert r.status_code == 200 and r.json()["screen_id"] == str(screen.id)
    row = (await db.execute(select(Device).where(Device.id == uuid.UUID(dev.device_id)))).scalar_one()
    assert row.pairing_method == "DEVICE_LOGIN" and row.paired_by_user_id == tech.id and row.platform == "android"


async def test_lockout_applies_to_device_login(client, db):
    org = await make_org(db)
    est = await make_establishment(db, org)
    screen = await make_screen(db, est)
    tech = await make_user(db, role="technician", scope=("ESTABLISHMENT", est.id))
    dev = SimDevice()
    await dev.request_pairing(client)
    url = f"/api/v1/player/pairing/requests/{dev.request_id}/claim"
    codes = []
    for _ in range(6):
        r = await client.post(url, json={"login": tech.email, "password": "senha-errada-1", "screen_code": screen.friendly_id})
        codes.append(r.status_code)
    assert codes[-1] == 423, codes
    r = await client.post(url, json={"login": tech.email, "password": tech.password, "screen_code": screen.friendly_id})
    assert r.status_code == 423  # bloqueada mesmo com a senha certa


@pytest.mark.parametrize("path", ["/api/v1/player/manifest", "/api/v1/player/app-update?platform=web&version=1.0.0"])
async def test_player_routes_require_device_credential(client, db, path):
    """Substitui test_player_sync.py (rotas legadas removidas): sem credencial → 401;
    credencial inválida → 401; X-Server-Time-Ms sempre presente."""
    r = await client.get(path)
    assert r.status_code == 401 and r.json()["error"]["code"] in ("UNAUTHENTICATED",)
    assert "x-server-time-ms" in r.headers
    r = await client.get(path, headers={"Authorization": "Bearer invalido"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_TOKEN"
    r = await client.post("/api/v1/player/events:batch", json={"events": []})
    assert r.status_code == 401


async def test_legacy_player_routes_removed(client, db):
    for method, path in (("post", f"/api/v1/screens/{uuid.uuid4()}/sync"), ("post", "/api/v1/playback/event"),
                         ("post", "/api/v1/playback/sync"), ("post", "/api/v1/devices/pairing/init"),
                         ("post", "/api/v1/devices/pairing/confirm")):
        r = await getattr(client, method)(path, json={})
        assert r.status_code in (404, 405), (path, r.status_code)


async def test_screen_removed_listener_publishes_only_after_commit(client, db, redis):
    import asyncio
    import json

    from app.core.database import create_engine, session_factory
    from app.modules.playback import identity
    from app.modules.websocket import bus

    org, est, screen, u, s = await _setup(db, client)
    dev = await pair_via_panel(client, s.headers, screen.id)
    ps = redis.pubsub()
    await ps.subscribe(bus.ch_device(dev.device_id))
    await asyncio.sleep(0.1)

    async def drain_msgs(wait=0.8):
        out, end = [], asyncio.get_running_loop().time() + wait
        while asyncio.get_running_loop().time() < end:
            m = await ps.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if m:
                out.append(json.loads(m["data"]))
        return out

    eng = create_engine(nullpool=True)
    try:
        async with session_factory(eng)() as s1:  # rollback: nada revogado, nada publicado
            await identity.on_screens_removed(s1, [screen.id], "teste")
            await s1.rollback()
        assert [m for m in await drain_msgs() if m.get("type") == "device_revoked"] == []
        assert not await identity.is_revoked_cached(dev.device_id)
        async with session_factory(eng)() as s2:
            await identity.on_screens_removed(s2, [screen.id], "teste")
            await s2.commit()
        msgs = await drain_msgs(2.0)
        assert any(m.get("type") == "device_revoked" for m in msgs), msgs
        assert await identity.is_revoked_cached(dev.device_id)
    finally:
        await ps.aclose()
        await eng.dispose()
    r = await client.post("/api/v1/player/ws-ticket", headers=dev.headers)
    assert r.status_code == 401 and r.json()["error"]["code"] == "DEVICE_REVOKED"
