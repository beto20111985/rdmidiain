"""Jobs controlados do W-PLAY executados como tasks Celery (no processo, repetidas vezes):
varredura de presença (meio-aberto/processo morto), flush coalescido + alerta de offline,
agregação, entrega ao financeiro por campanha (isolamento de falha), housekeeping."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta

import redis as sync_redis
from sqlalchemy import func, select, update

from app.core import events
from app.core.config import settings
from app.core.database import create_engine, session_factory
from app.core.timeutils import utcnow
from app.models.devices import Device, DeviceCommand, PairingRequest
from app.models.playback import PlaybackEvent
from app.modules.playback import protocol as proto
from app.modules.presence import service as presence
from app.workers.tasks import playback as T


async def _with_db(fn):
    eng = create_engine(nullpool=True)
    try:
        async with session_factory(eng)() as db:
            return await fn(db)
    finally:
        await eng.dispose()


async def _seed_devices(n: int, *, last_seen_days: int = 0):
    from tests.factories import make_establishment, make_org
    from tests.player_support import make_screen

    async def go(db):
        org = await make_org(db)
        est = await make_establishment(db, org)
        out = []
        for _ in range(n):
            sc = await make_screen(db, est)
            k = proto.generate_private_key()
            spki, fp = proto.normalize_public_spki(proto.public_spki_b64u(k))
            d = Device(id=uuid.uuid4(), screen_id=sc.id, public_key=spki, key_alg="ES256", key_fingerprint=fp,
                       platform="web", status="ACTIVE", paired_at=utcnow() - timedelta(days=30), pairing_method="PANEL",
                       presence="online", last_seen_at=utcnow() - timedelta(days=last_seen_days))
            db.add(d)
            out.append((d.id, sc.id))
        await db.commit()
        return org, est, out

    return await _with_db(go)


def test_presence_sweep_marks_half_open_connections_offline(clean_state):
    org, est, devs = asyncio.run(_seed_devices(3))
    r = sync_redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    now = int(time.time() * 1000)
    stale_dev, fresh_dev = devs[0], devs[1]
    for (dev, scr), ts in ((stale_dev, now - 200_000), (fresh_dev, now - 5_000)):
        r.hset(presence.k_dev(dev), mapping={"screen_id": str(scr), "conn": "x", "connected": "1", "status": "reproduzindo",
                                             "last_seen_ms": str(ts)})
        r.zadd(presence.k_last_seen(), {str(dev): ts})
    ps = r.pubsub()
    ps.psubscribe(events.subscribe_pattern())
    time.sleep(0.2)
    res1 = T.presence_sweep.apply().get(timeout=60)
    res2 = T.presence_sweep.apply().get(timeout=60)  # idempotente
    assert res1 == {"offline": 1} and res2 == {"offline": 0}

    async def check(db):
        rows = {d.id: d.presence for d in (await db.execute(select(Device))).scalars()}
        return rows

    rows = asyncio.run(_with_db(check))
    assert rows[stale_dev[0]] == "offline" and rows[fresh_dev[0]] == "online"
    assert r.zscore(presence.k_last_seen(), str(stale_dev[0])) is None
    got = None
    deadline = time.time() + 5
    while time.time() < deadline and got is None:
        m = ps.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if m and json.loads(m["data"]).get("type") == "screen.status":
            got = json.loads(m["data"])
    ps.close()
    assert got and got["data"]["status"] == "offline" and got["data"]["screen_id"] == str(stale_dev[1])
    assert got["scope"]["org_id"] == str(org.id)  # filtrável por escopo no gateway


def test_presence_flush_and_offline_alert_dedup(clean_state):
    org, est, devs = asyncio.run(_seed_devices(2, last_seen_days=8))
    r = sync_redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    now = int(time.time() * 1000)
    live = devs[0]
    r.zadd(presence.k_last_seen(), {str(live[0]): now})

    async def mark_offline(db):
        await db.execute(update(Device).where(Device.id == devs[1][0]).values(presence="offline"))

        await _viewer(db, est)
        await db.commit()

    asyncio.run(_with_db(mark_offline))
    res = T.presence_flush.apply().get(timeout=60)
    assert res["flushed"] == 1
    res2 = T.presence_flush.apply().get(timeout=60)

    async def check(db):
        from app.models.notifications import Notification

        d = await db.get(Device, live[0])
        n = (await db.execute(select(func.count()).select_from(Notification)
                              .where(Notification.event_type == "screen.offline"))).scalar_one()
        return d.last_seen_at, n

    seen, n = asyncio.run(_with_db(check))
    assert abs(seen.timestamp() * 1000 - now) < 1000
    assert res["alerts"] >= 1 and res2["alerts"] == 0 and n >= 1  # dedupe por episódio/marco


async def _seed_events(kinds_campaigns: list[tuple[str, uuid.UUID]]):
    org, est, devs = await _seed_devices(1)
    dev, scr = devs[0]

    async def go(db):
        base = int(time.time() * 1000) - 3_600_000
        ids = []
        for i, (kind, camp) in enumerate(kinds_campaigns):
            st = base + i * 30_000
            pu = uuid.uuid4()
            db.add(PlaybackEvent(
                playback_uuid=pu, device_id=dev, screen_id=scr, org_id=org.id, establishment_id=est.id,
                creative_id=uuid.uuid4(), creative_code="RD-A7M2P9", campaign_id=camp, kind=kind, manifest_version=1,
                started_at_ms=st, finished_at_ms=st + 30_000, started_at=utcnow() - timedelta(hours=1),
                finished_at=utcnow() - timedelta(hours=1), expected_ms=30_000, actual_ms=30_000, result="COMPLETED",
                seq=i + 1, nonce=proto.b64u_encode(uuid.uuid4().bytes), prev_hash="0" * 64, event_hash="1" * 64,
                clock_offset_ms=0, offline=False, signature="s", status="ACCEPTED", valid=True, reason=None, reasons=[],
                flags=[], local_day=utcnow().date(), financial_status="PENDING" if kind == "EXTERNAL" else "NOT_APPLICABLE"))
            ids.append(pu)
        await db.commit()
        return ids

    return await _with_db(go)


def test_financial_delivery_isolates_failing_campaign(clean_state, monkeypatch):
    good, bad = uuid.uuid4(), uuid.uuid4()
    asyncio.run(_seed_events([("EXTERNAL", good), ("EXTERNAL", good), ("EXTERNAL", bad), ("INTERNAL", None)]))
    from app.modules.playback import integration as integ
    from app.modules.financial.service import accrue_commission_for_events as real

    calls = []

    async def wrapped(db, evs):
        evs = list(evs)
        calls.append({e.campaign_id for e in evs})
        if any(e.campaign_id == bad for e in evs):
            from app.core.errors import DomainError

            raise DomainError("COMMISSION_PLAN_UNAVAILABLE", "plano indisponível", status=503)
        return await real(db, evs)

    monkeypatch.setattr(integ, "accrue_commission_fn", lambda: wrapped)
    res = T.deliver_financial.apply().get(timeout=60)
    assert res["delivered"] == 2 and res["pending"] == 1 and str(bad) in res["errors"]
    assert all(len(c) == 1 for c in calls)  # um lote por campanha

    async def check(db):
        rows = (await db.execute(select(PlaybackEvent.campaign_id, PlaybackEvent.financial_status,
                                        PlaybackEvent.financial_attempts))).all()
        return rows

    rows = asyncio.run(_with_db(check))
    st = {(c, s) for c, s, _ in rows}
    assert (good, "APPROPRIATED") in st and (bad, "PENDING") in st and (None, "NOT_APPLICABLE") in st
    # reentrega: a campanha boa não é reenviada; a ruim tenta de novo
    calls.clear()
    res = T.deliver_financial.apply().get(timeout=60)
    assert res["delivered"] == 0 and calls == [{bad}]


def test_aggregate_task_repeated_and_housekeeping(clean_state):
    asyncio.run(_seed_events([("EXTERNAL", uuid.uuid4())] * 3))
    a = [T.aggregate_playback.apply().get(timeout=60) for _ in range(3)]
    assert [x["events"] for x in a] == [3, 0, 0]

    async def seed(db):
        d = (await db.execute(select(Device))).scalars().first()
        db.add(DeviceCommand(device_id=d.id, screen_id=d.screen_id, command="resync", args={}, reason="t",
                             status="DELIVERED", expires_at=utcnow() - timedelta(seconds=1)))
        db.add(PairingRequest(pairing_code="ABCD-2345", public_key="x", key_fingerprint="f" * 64, platform="web",
                              status="PENDING", expires_at=utcnow() - timedelta(minutes=1)))
        await db.commit()

    asyncio.run(_with_db(seed))
    h = T.housekeeping.apply().get(timeout=60)
    assert h["commands_expired"] == 1 and h["expired"] == 1


def test_outdated_alert_for_online_screen_not_applying_current_version(clean_state):
    org, est, devs = asyncio.run(_seed_devices(1))
    dev, scr = devs[0]

    async def seed(db):
        from app.modules.playback import vectors
        from tests.player_support import store_manifest
        from app.models.manifests import ManifestVersion

        m = vectors.base_manifest(version=5)
        m["manifest_id"] = str(uuid.uuid4())
        await store_manifest(db, scr, m)
        await db.execute(update(ManifestVersion).values(generated_at=utcnow() - timedelta(hours=3)))
        await db.execute(update(Device).where(Device.id == dev).values(applied_manifest_version=4, presence="online"))
        await _viewer(db, est)
        await db.commit()

    asyncio.run(_with_db(seed))
    res = T.presence_flush.apply().get(timeout=60)
    res2 = T.presence_flush.apply().get(timeout=60)
    assert res["outdated_alerts"] >= 1 and res2["outdated_alerts"] == 0  # dedupe por tela+versão

    async def applied(db):
        await db.execute(update(Device).where(Device.id == dev).values(applied_manifest_version=5))
        await db.commit()

    asyncio.run(_with_db(applied))
    assert T.presence_flush.apply().get(timeout=60)["outdated_alerts"] == 0


async def _viewer(db, est):
    """Destinatário DETERMINÍSTICO: perfil próprio só com ``screens.view`` no estabelecimento (não depende
    das permissões dos perfis de sistema, que outros testes podem editar — catálogo preservado entre testes)."""
    import secrets

    from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
    from tests.factories import make_user

    role = Role(id=uuid.uuid4(), slug=f"tplay_{secrets.token_hex(4)}", name="Teste player", is_system=False)
    db.add(role)
    await db.flush()
    p = (await db.execute(select(Permission).where(Permission.code == "screens.view"))).scalar_one()
    db.add(RolePermission(role_id=role.id, permission_id=p.id))
    u = await make_user(db)
    db.add(RoleAssignment(id=uuid.uuid4(), user_id=u.id, role_id=role.id, scope_type="ESTABLISHMENT", scope_id=est.id))
    await db.commit()
    return u
