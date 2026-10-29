"""Relatório de sincronizações/downloads (ETAPA2 #798–#799; P001 §103) e retenção do histórico."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models.devices import Device, DeviceSyncEvent
from app.modules.playback import protocol as proto
from app.modules.playback.syncevents import build, purge, sync_report
from tests.factories import make_establishment, make_org
from tests.player_support import make_screen


async def _dev(db, est):
    sc = await make_screen(db, est)
    spki, fp = proto.normalize_public_spki(proto.public_spki_b64u(proto.generate_private_key()))
    d = Device(id=uuid.uuid4(), screen_id=sc.id, public_key=spki, key_alg="ES256", key_fingerprint=fp, platform="web",
               status="ACTIVE", paired_at=datetime.now(timezone.utc), pairing_method="PANEL", presence="offline")
    db.add(d)
    await db.commit()
    return sc, d


async def test_sync_report_groups_latency_and_media_failures(db):
    org = await make_org(db)
    est = await make_establishment(db, org)
    other = await make_org(db)
    est2 = await make_establishment(db, other)
    s1, d1 = await _dev(db, est)
    s2, d2 = await _dev(db, est)
    s3, d3 = await _dev(db, est2)
    sc1 = {"org_id": str(org.id), "establishment_id": str(est.id)}
    t0 = datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc)  # 23:30 do dia 11 em São Paulo
    mid = uuid.uuid4()
    rows = [
        build(s1.id, d1.id, "MANIFEST_APPLIED", scope=sc1, manifest_version=1, duration_ms=1000, occurred_at=t0),
        build(s1.id, d1.id, "MANIFEST_APPLIED", scope=sc1, manifest_version=2, duration_ms=3000, occurred_at=t0 + timedelta(hours=2)),
        build(s2.id, d2.id, "MANIFEST_APPLIED", scope=sc1, manifest_version=1, duration_ms=9000, occurred_at=t0 + timedelta(hours=3)),
        build(s1.id, d1.id, "DOWNLOAD_COMPLETED", scope=sc1, manifest_version=2, bytes_=500, occurred_at=t0),
        build(s1.id, d1.id, "MEDIA_ERROR", scope=sc1, media_id=mid, reason="404", occurred_at=t0),
        build(s2.id, d2.id, "MEDIA_ERROR", scope=sc1, media_id=mid, reason="sha", occurred_at=t0 + timedelta(minutes=5)),
        build(s3.id, d3.id, "MANIFEST_APPLIED", scope={"org_id": str(other.id), "establishment_id": str(est2.id)},
              manifest_version=1, duration_ms=1, occurred_at=t0),
    ]
    db.add_all(rows)
    await db.commit()
    rng = {"start": t0 - timedelta(days=1), "end": t0 + timedelta(days=1)}
    rep = await sync_report(db, org_id=org.id, group_by="screen", **rng)
    by = {g["key"]: g for g in rep["groups"]}
    assert set(by) == {str(s1.id), str(s2.id)}  # outra organização fora
    assert by[str(s1.id)]["counts"]["MANIFEST_APPLIED"] == 2 and by[str(s1.id)]["bytes_downloaded"] == 500
    lat = by[str(s1.id)]["time_to_apply_ms"]
    assert (lat["n"], lat["avg"], lat["p50"], lat["max"]) == (2, 2000, 2000, 3000)
    assert rep["media_failures"] == [{"media_id": str(mid), "count": 2, "screens": 2,
                                      "last_at": rep["media_failures"][0]["last_at"], "last_reason": "sha"}]
    day = await sync_report(db, screen_ids=[s1.id, s2.id], group_by="day", **rng)
    assert {g["key"] for g in day["groups"]} == {"2026-10-11", "2026-10-12"}  # fuso America/Sao_Paulo
    org_rep = await sync_report(db, group_by="org", **rng)
    assert {g["key"] for g in org_rep["groups"]} == {str(org.id), str(other.id)}
    assert (await sync_report(db, screen_ids=[], **rng))["groups"] == []


async def test_sync_events_retention(db):
    org = await make_org(db)
    est = await make_establishment(db, org)
    s1, d1 = await _dev(db, est)
    now = datetime.now(timezone.utc)
    db.add_all([build(s1.id, d1.id, "MANIFEST_APPLIED", occurred_at=now - timedelta(days=200)),
                build(s1.id, d1.id, "MANIFEST_APPLIED", occurred_at=now - timedelta(days=10))])
    await db.commit()
    assert await purge(db) == 1
    await db.commit()
    assert (await db.execute(select(func.count()).select_from(DeviceSyncEvent))).scalar_one() == 1
