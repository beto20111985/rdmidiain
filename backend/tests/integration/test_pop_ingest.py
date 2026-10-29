"""RDPP/1 §7 — ingestão de PoP pela API real (HTTP), concorrência de UUID, entrega ao
financeiro, agregação diária e retenção (90 d só após agregar E apropriar)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from app.core.database import create_engine, session_factory
from app.core.timeutils import utcnow
from app.models.playback import PlaybackDailyStat, PlaybackEvent
from app.modules.playback import ingest, maintenance, vectors
from tests.factories import login_as, make_establishment, make_org, make_user
from tests.player_support import make_screen, now_ms, pair_via_panel, store_manifest

C, I = 240000, 120000


def _manifest_for(screen_id, t_ref: int) -> dict:
    """Manifesto sintético (formato RDPP) com inserções externas válidas por 2 dias."""
    m = vectors.base_manifest(version=7)
    m["screen_id"] = str(screen_id)
    m["manifest_id"] = str(uuid.uuid4())
    b = (t_ref // C) * C
    m["anchor_ms"] = b
    m["horizon"] = {"from_ms": b - 86_400_000, "to_ms": b + 86_400_000, "repeat_period_ms": vectors.WEEK_MS}
    for k in ("c1", "c2", "c3", "c4", "c6"):
        m["creatives"][k]["valid_from_ms"] = b - 86_400_000
        m["creatives"][k]["valid_until_ms"] = b + 86_400_000
    vectors._plan(m, b, [("c3", 0, 30000), ("c2", 1, 60000), ("c6", 3, 30000)], screen=str(screen_id))
    return m


async def _paired(client, db):
    org = await make_org(db)
    est = await make_establishment(db, org)
    screen = await make_screen(db, est)
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    dev = await pair_via_panel(client, s.headers, screen.id)
    await _backdate(db, dev)
    m = _manifest_for(screen.id, now_ms())
    await store_manifest(db, screen.id, m)
    return org, est, screen, s, dev, m


async def _backdate(db, dev):
    """Pareado há 1 dia (eventos dos testes começam minutos atrás)."""
    from app.models.devices import Device

    await db.execute(update(Device).where(Device.id == uuid.UUID(dev.device_id)).values(paired_at=utcnow() - timedelta(days=1)))
    await db.commit()


async def test_batch_over_http_100_percent_rule_and_ack(client, db):
    org, est, screen, s, dev, m = await _paired(client, db)
    t = now_ms() - 600_000
    e_ok = dev.event(m, "c3", start=t)  # 30 000 / 30 000
    e_short = dev.event(m, "c3", start=t + 30_000, actual=29_999)
    e_25 = dev.event(m, "c3", start=t + 60_000, actual=25_000, result="INTERRUPTED")
    e_ok2 = dev.event(m, "c2", start=t + 90_000)  # 60 s
    e_skip = dev.event(m, "c6", start=t + 150_000, actual=0, result="SKIPPED")
    items = [dev.item(e) for e in (e_ok, e_short, e_25, e_ok2, e_skip)]
    r = await client.post("/api/v1/player/events:batch", json={"events": items}, headers=dev.headers)
    assert r.status_code == 200, r.text
    assert "x-server-time-ms" in r.headers
    body = r.json()
    got = [(x["status"], x["valid"], x["reason"]) for x in body["results"]]
    assert got == [("ACCEPTED", True, None), ("ACCEPTED", False, "INCOMPLETE_DURATION"),
                   ("ACCEPTED", False, "NOT_COMPLETED"), ("ACCEPTED", True, None),
                   ("ACCEPTED", False, "NOT_COMPLETED")], got
    assert body["ack_upto_seq"] == e_skip["seq"]
    rows = {str(x.playback_uuid): x for x in (await db.execute(select(PlaybackEvent))).scalars()}
    assert rows[e_ok["playback_uuid"]].financial_status == "PENDING"
    assert rows[e_short["playback_uuid"]].financial_status == "NOT_APPLICABLE"
    assert rows[e_short["playback_uuid"]].reasons == ["INCOMPLETE_DURATION"]
    assert rows[e_ok["playback_uuid"]].org_id == org.id and rows[e_ok["playback_uuid"]].establishment_id == est.id
    # retry após falha de rede: idempotente, mesma validade, nada novo gravado
    r2 = await client.post("/api/v1/player/events:batch", json={"events": items}, headers=dev.headers)
    assert [(x["status"], x["valid"]) for x in r2.json()["results"]] == [("DUPLICATE", v) for _, v, _ in got]
    assert (await db.execute(select(func.count()).select_from(PlaybackEvent))).scalar_one() == 5
    # limites e forma
    r = await client.post("/api/v1/player/events:batch", json={"events": [items[0]] * 201}, headers=dev.headers)
    assert r.status_code == 413 and r.json()["error"]["code"] == "BATCH_TOO_LARGE"
    r = await client.post("/api/v1/player/events:batch", content=b"{nao json", headers={**dev.headers, "Content-Type": "application/json"})
    assert r.status_code == 422
    # evento assinado por OUTRO dispositivo autenticado → REJECTED DEVICE_MISMATCH
    other = await pair_via_panel(client, s.headers, (await make_screen(db, est)).id)
    await _backdate(db, other)
    e_x = dev.event(m, "c3", start=t + 300_000)
    r = await client.post("/api/v1/player/events:batch", json={"events": [dev.item(e_x)]}, headers=other.headers)
    assert r.json()["results"][0]["reason"] == "DEVICE_MISMATCH"


async def test_concurrent_same_uuid_from_two_devices(client, db):
    org, est, screen, s, d1, m = await _paired(client, db)
    screen2 = await make_screen(db, est)
    d2 = await pair_via_panel(client, s.headers, screen2.id)
    await _backdate(db, d2)
    m2 = _manifest_for(screen2.id, now_ms())
    await store_manifest(db, screen2.id, m2)
    t = now_ms() - 300_000
    pu = str(uuid.uuid4())
    e1 = d1.event(m, "c3", start=t, playback_uuid=pu)
    e2 = d2.event(m2, "c3", start=t, playback_uuid=pu)

    async def run(dev_id, item):
        eng = create_engine(nullpool=True)
        try:
            async with session_factory(eng)() as s_:
                out = await ingest.ingest_batch(s_, uuid.UUID(dev_id), [item])
                await asyncio.sleep(0.2)  # segura a transação para forçar a disputa no índice único
                await s_.commit()
                return out.results[0]
        finally:
            await eng.dispose()

    a, b = await asyncio.gather(run(d1.device_id, d1.item(e1)), run(d2.device_id, d2.item(e2)))
    statuses = sorted([a.status, b.status])
    assert statuses == ["ACCEPTED", "DUPLICATE"], (a, b)
    dup = a if a.status == "DUPLICATE" else b
    assert dup.reason == "DUPLICATE_UUID_MISMATCH" and dup.valid is False
    assert (await db.execute(select(func.count()).select_from(PlaybackEvent))).scalar_one() == 1


async def test_concurrent_batches_same_device_are_serialized(client, db):
    """Dois lotes simultâneos do MESMO dispositivo com eventos sobrepostos: só um vale."""
    org, est, screen, s, dev, m = await _paired(client, db)
    t = now_ms() - 300_000
    ea = dev.event(m, "c3", start=t)
    eb = dev.event(m, "c3", start=t + 10_000)  # sobrepõe ea

    async def run(item):
        eng = create_engine(nullpool=True)
        try:
            async with session_factory(eng)() as s_:
                out = await ingest.ingest_batch(s_, uuid.UUID(dev.device_id), [item])
                await asyncio.sleep(0.2)
                await s_.commit()
                return out.results[0]
        finally:
            await eng.dispose()

    a, b = await asyncio.gather(run(dev.item(ea)), run(dev.item(eb)))
    valid = sorted([a.valid, b.valid])
    assert valid == [False, True], (a, b)
    bad = a if not a.valid else b
    assert bad.reason in ("OVERLAP", "SEQ_ROLLBACK")


async def test_financial_delivery_aggregation_and_retention(client, db):
    org, est, screen, s, dev, m = await _paired(client, db)
    t = now_ms() - 900_000
    evs = [dev.event(m, "c3", start=t), dev.event(m, "c3", start=t + 30_000, actual=1000),
           dev.event(m, "c7", start=t + 60_000)]
    r = await client.post("/api/v1/player/events:batch", json={"events": [dev.item(e) for e in evs]}, headers=dev.headers)
    assert [x["valid"] for x in r.json()["results"]] == [True, False, True]

    # entrega ao financeiro (W-FIN real): só o válido EXTERNAL; idempotente
    res = await ingest.deliver_pending_financial(db)
    await db.commit()
    assert res["delivered"] == 1, res
    res2 = await ingest.deliver_pending_financial(db)
    await db.commit()
    assert res2["delivered"] == 0
    from app.models.financial import CommissionAccrualEvent  # noqa: PLC0415

    n = (await db.execute(select(func.count()).select_from(CommissionAccrualEvent))).scalar_one()
    assert n == 1

    # agregação (sem contagem dupla em reexecução)
    a1 = await maintenance.aggregate_pending(db)
    await db.commit()
    a2 = await maintenance.aggregate_pending(db)
    await db.commit()
    assert a1["events"] == 3 and a2["events"] == 0
    st = (await db.execute(select(PlaybackDailyStat).where(PlaybackDailyStat.creative_id == uuid.UUID(evs[0]["creative_id"])))).scalar_one()
    assert (st.plays_accepted, st.plays_valid, st.plays_invalid, st.valid_ms) == (2, 1, 1, 30000)
    assert st.last_valid_started_at is not None
    from app.modules.playback.service import last_valid_play_at

    assert await last_valid_play_at(db, uuid.UUID(evs[0]["campaign_id"])) is not None

    # retenção: um evento EXTERNAL válido ainda NÃO apropriado não é apagado
    e_pending = dev.event(m, "c3", start=t + 120_000 + 60_000)
    await client.post("/api/v1/player/events:batch", json={"events": [dev.item(e_pending)]}, headers=dev.headers)
    await maintenance.aggregate_pending(db)
    old = utcnow() - timedelta(days=200)
    await db.execute(update(PlaybackEvent).values(received_at=old, started_at=old))
    await db.execute(update(PlaybackEvent).where(PlaybackEvent.financial_status == "APPROPRIATED")
                     .values(financial_appropriated_at=old))
    await db.commit()
    deleted = await maintenance.purge_raw_events(db)
    await db.commit()
    left = {str(x.playback_uuid): x.financial_status for x in (await db.execute(select(PlaybackEvent))).scalars()}
    assert deleted == 3 and left == {e_pending["playback_uuid"]: "PENDING"}, (deleted, left)
    # agregados preservados após a retenção
    assert (await db.execute(select(func.count()).select_from(PlaybackDailyStat))).scalar_one() >= 2
    # evento agregado mas não apropriado também fica (mesmo velho)
    await db.execute(update(PlaybackEvent).values(aggregated_at=None))
    await db.commit()
    assert await maintenance.purge_raw_events(db) == 0


async def test_kill_flows_into_manifest_and_pop(client, db):
    from app.models.devices import CreativeKill
    from app.modules.playback.kills import revoked_creative_ids

    org, est, screen, s, dev, m = await _paired(client, db)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    cid = m["creatives"]["c3"]["creative_id"]
    r = await client.post("/api/v1/playback/kills", json={"creative_id": cid, "screen_ids": [str(screen.id)],
                                                        "reason": "conteúdo irregular"}, headers=sa.headers)
    assert r.status_code == 201, r.text
    assert [str(x) for x in await revoked_creative_ids(db, screen.id)] == [cid]
    # evento iniciado DEPOIS do kill → inválido CREATIVE_REVOKED
    kill_at = (await db.execute(select(CreativeKill.created_at))).scalar_one()
    e = dev.event(m, "c3", start=int(kill_at.timestamp() * 1000) + 1000)
    r = await client.post("/api/v1/player/events:batch", json={"events": [dev.item(e)]}, headers=dev.headers)
    assert r.json()["results"][0]["reason"] == "CREATIVE_REVOKED"
    # sem permissão de kill → 403; kill global exige escopo global
    emp = await make_user(db, role="employee", scope=("ORGANIZATION", org.id))
    se = await login_as(client, emp)
    r = await client.post("/api/v1/playback/kills", json={"creative_id": cid, "reason": "x" * 5}, headers=se.headers)
    assert r.status_code == 403
