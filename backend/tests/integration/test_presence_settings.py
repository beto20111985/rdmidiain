"""REQ-HB-002 / C-40 — limites de presença CONFIGURÁVEIS pelo mecanismo da fundação
(``PUT /api/v1/settings/{key}``, histórico + auditoria): TTL de presença (default 90 s,
40–600) e alerta de offline (default 10 min, 2–1440). Valores lidos sem reiniciar o processo
(cache curto por processo) e com efeito real na presença (TTL no Redis, varredura) e no alerta."""
from __future__ import annotations

import secrets
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.timeutils import utcnow
from app.models.audit import AuditLog
from app.models.devices import Device
from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
from app.models.settings import SystemSettingHistory
from app.modules.playback import config as pcfg
from app.modules.playback import maintenance
from app.modules.playback import protocol as proto
from app.modules.presence import service as presence
from tests.factories import login_as, make_establishment, make_org, make_user
from tests.player_support import make_screen

TTL = pcfg.SETTING_PRESENCE_TTL_S
ALERT = pcfg.SETTING_OFFLINE_ALERT_AFTER_MIN


@pytest.fixture(autouse=True)
def _fresh_cache():
    pcfg.clear_settings_cache()
    yield
    pcfg.clear_settings_cache()


async def _role_user(db, perms, *, scope=("GLOBAL", None)):
    role = Role(id=uuid.uuid4(), slug=f"tpres_{secrets.token_hex(4)}", name="Teste presença", is_system=False)
    db.add(role)
    await db.flush()
    for code in perms:
        p = (await db.execute(select(Permission).where(Permission.code == code))).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=p.id))
    u = await make_user(db)
    db.add(RoleAssignment(id=uuid.uuid4(), user_id=u.id, role_id=role.id, scope_type=scope[0], scope_id=scope[1]))
    await db.commit()
    return u


async def _device(db, est):
    sc = await make_screen(db, est)
    spki, fp = proto.normalize_public_spki(proto.public_spki_b64u(proto.generate_private_key()))
    d = Device(id=uuid.uuid4(), screen_id=sc.id, public_key=spki, key_alg="ES256", key_fingerprint=fp, platform="web",
               status="ACTIVE", paired_at=utcnow() - timedelta(days=1), pairing_method="PANEL", presence="online")
    db.add(d)
    await db.commit()
    return sc, d


def _advance_cache_clock(monkeypatch, seconds: float) -> None:
    base = time.monotonic()
    monkeypatch.setattr(pcfg, "_clock", lambda: base + seconds)


async def test_defaults_listed_and_used(client, db):
    admin = await _role_user(db, ("settings.view", "settings.manage"))
    s = await login_as(client, admin)
    items = {i["key"]: i for i in (await client.get("/api/v1/settings", headers=s.headers)).json()["items"]}
    assert (items[TTL]["value"], items[TTL]["min"], items[TTL]["max"]) == (90, 40, 600)
    assert (items[ALERT]["value"], items[ALERT]["min"], items[ALERT]["max"]) == (10, 2, 1440)
    assert await pcfg.presence_ttl_s() == 90 and await pcfg.offline_alert_after_min() == 10


@pytest.mark.parametrize("key,bad", [(TTL, 39), (TTL, 601), (TTL, "90"), (ALERT, 1), (ALERT, 1441), (ALERT, 2.5)])
async def test_out_of_range_rejected(client, db, key, bad):
    admin = await _role_user(db, ("settings.view", "settings.manage"))
    s = await login_as(client, admin)
    r = await client.put(f"/api/v1/settings/{key}", json={"value": bad, "reason": "teste de limite"}, headers=s.headers)
    assert r.status_code in (400, 422) and r.json()["error"]["code"] in ("SETTING_INVALID", "VALIDATION_ERROR"), r.text
    assert await pcfg.presence_ttl_s() == 90 and await pcfg.offline_alert_after_min() == 10


async def test_only_settings_manage_can_change(client, db):
    viewer = await _role_user(db, ("settings.view", "screens.view"))
    s = await login_as(client, viewer)
    r = await client.put(f"/api/v1/settings/{TTL}", json={"value": 120}, headers=s.headers)
    assert r.status_code == 403


async def test_ttl_change_applies_without_restart_to_redis_ttl_and_sweep(client, db, redis, monkeypatch):
    org = await make_org(db)
    est = await make_establishment(db, org)
    _, d1 = await _device(db, est)
    _, d2 = await _device(db, est)
    admin = await _role_user(db, ("settings.view", "settings.manage"))
    s = await login_as(client, admin)

    # default 90 s: TTL do hash de presença e varredura (100 s sem sinal ⇒ offline)
    await presence.connected(d1.id, d1.screen_id, "c1")
    assert 80 < await redis.ttl(presence.k_dev(d1.id)) <= 90
    now = presence.now_ms()
    await redis.zadd(presence.k_last_seen(), {str(d2.id): now - 100_000})
    assert str(d2.id) in await presence.sweep()
    assert await pcfg.presence_ttl_s() == 90  # valor em cache neste processo

    r = await client.put(f"/api/v1/settings/{TTL}", json={"value": 180, "reason": "rede instável nas lojas"},
                         headers=s.headers)
    assert r.status_code == 200 and r.json()["value"] == 180, r.text
    hist = (await db.execute(select(func.count()).select_from(SystemSettingHistory)
                             .where(SystemSettingHistory.key == TTL))).scalar_one()
    aud = (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == TTL))).scalar_one()
    assert hist == 1 and aud == 1

    # cache curto: ainda o valor antigo até expirar; depois o novo, SEM reiniciar o processo
    assert await pcfg.presence_ttl_s() == 90
    _advance_cache_clock(monkeypatch, pcfg.SETTINGS_CACHE_S + 1)
    assert await pcfg.presence_ttl_s() == 180
    await presence.heartbeat(d1.id, d1.screen_id, "c1", {"state": "PLAYING"})
    assert 170 < await redis.ttl(presence.k_dev(d1.id)) <= 180
    await redis.zadd(presence.k_last_seen(), {str(d2.id): presence.now_ms() - 100_000})
    assert await presence.sweep() == []  # 100 s < 180 s: continua online
    await redis.zadd(presence.k_last_seen(), {str(d2.id): presence.now_ms() - 181_000})
    assert str(d2.id) in await presence.sweep()


async def test_offline_alert_threshold_configurable(client, db, monkeypatch):
    org = await make_org(db)
    est = await make_establishment(db, org)
    _, dev = await _device(db, est)
    await _role_user(db, ("screens.view",), scope=("ESTABLISHMENT", est.id))
    dev = await db.get(Device, dev.id)
    dev.presence, dev.last_seen_at = "offline", utcnow() - timedelta(minutes=5)
    await db.commit()
    assert await maintenance.offline_alerts(db) == 0  # 5 min < 10 min (default)
    await db.commit()

    admin = await _role_user(db, ("settings.view", "settings.manage"))
    s = await login_as(client, admin)
    r = await client.put(f"/api/v1/settings/{ALERT}", json={"value": 3, "reason": "alerta mais cedo"}, headers=s.headers)
    assert r.status_code == 200, r.text
    _advance_cache_clock(monkeypatch, pcfg.SETTINGS_CACHE_S + 1)
    sent = await maintenance.offline_alerts(db)
    await db.commit()
    assert sent >= 1  # 5 min ≥ 3 min


async def test_unreadable_setting_falls_back_without_breaking_presence(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(pcfg, "get_setting", boom)
    assert await pcfg.presence_ttl_s() == 90  # default do registro; presença segue funcionando
