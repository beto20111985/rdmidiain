"""Reexecuta CADA caso de ``protocol/vectors/pop_validation.json`` (gerado em memória pelo
mesmo gerador) pelo caminho REAL de ingestão com PostgreSQL: o estado do ``context`` vira
linhas no banco (dispositivo, manifesto da versão, eventos já gravados, kills) e o
resultado de ``ingest_batch`` tem de ser idêntico ao ``expected`` do vetor."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.models.devices import CreativeKill, Device
from app.models.playback import PlaybackEvent
from app.modules.playback import ingest, vectors
from app.modules.playback import protocol as proto
from tests.factories import make_establishment, make_org
from tests.player_support import make_screen, store_manifest

POP = vectors.build_pop()
SPKI = POP["device_key"]["public_spki_b64u"]


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


async def _materialize(db, ctx: dict) -> None:
    org = await make_org(db)
    est = await make_establishment(db, org, tz="America/Sao_Paulo")
    sid = uuid.UUID(ctx["device"]["screen_id"])
    await make_screen(db, est, screen_id=sid)
    dev = Device(id=uuid.UUID(ctx["authenticated_device_id"]), screen_id=sid, public_key=SPKI, key_alg="ES256",
                 key_fingerprint=proto.normalize_public_spki(SPKI)[1], platform="web", status="ACTIVE",
                 paired_at=_dt(ctx["device"]["paired_at_ms"]), pairing_method="PANEL", presence="offline",
                 pop_last_seq=ctx["last_seq"], pop_last_hash=ctx["last_hash"])
    db.add(dev)
    others = {k["device_id"] for k in ctx["known_events"]} - {str(dev.id)}
    for i, oid in enumerate(sorted(others)):
        db.add(Device(id=uuid.UUID(oid), screen_id=sid, public_key=SPKI, key_alg="ES256", key_fingerprint=f"{i:064d}",
                      platform="web", status="REVOKED", paired_at=_dt(ctx["device"]["paired_at_ms"]),
                      pairing_method="PANEL", presence="offline"))
    await db.commit()
    for v, m in ctx["manifests"].items():
        await store_manifest(db, sid, m, version=int(v))
    for k in ctx["known_events"]:
        db.add(PlaybackEvent(
            playback_uuid=uuid.UUID(k["playback_uuid"]), device_id=uuid.UUID(k["device_id"]), screen_id=sid,
            creative_id=uuid.uuid4(), creative_code="RD-A7M2P9", kind="EXTERNAL", manifest_version=42,
            started_at_ms=k["started_at_ms"], finished_at_ms=k["finished_at_ms"], started_at=_dt(k["started_at_ms"]),
            finished_at=_dt(k["finished_at_ms"]), expected_ms=30000, actual_ms=k["actual_ms"], result="COMPLETED",
            seq=k["seq"], nonce=k["nonce"], prev_hash="0" * 64, event_hash=k["event_hash"], clock_offset_ms=0,
            offline=False, signature="x", status=k["status"], valid=k["valid"], reason=k["reason"], reasons=[],
            flags=[], local_day=_dt(k["started_at_ms"]).date()))
    for kl in ctx["kills"]:
        db.add(CreativeKill(creative_id=uuid.UUID(kl["creative_id"]),
                            screen_id=uuid.UUID(kl["screen_id"]) if kl["screen_id"] else None, reason="teste",
                            created_at=_dt(kl["created_at_ms"]),
                            lifted_at=_dt(kl["lifted_at_ms"]) if kl.get("lifted_at_ms") else None))
    await db.commit()


@pytest.mark.parametrize("case", POP["cases"], ids=[c["name"] for c in POP["cases"]])
async def test_pop_vector_case_through_database(db, case):
    ctx = case["context"]
    await _materialize(db, ctx)
    out = await ingest.ingest_batch(db, uuid.UUID(ctx["authenticated_device_id"]),
                                    [{"event": case["event"], "signature": case["signature_b64u"]}], now_ms=ctx["now_ms"])
    await db.commit()
    r = out.results[0]
    exp = case["expected"]
    assert (r.status, r.valid, r.reason) == (exp["status"], exp["valid"], exp["reason"]), case["name"]
    assert sorted(r.flags) == sorted(exp.get("flags", [])), case["name"]
    if exp["status"] != "DUPLICATE" and exp["reason"] != "SCHEMA_INVALID":
        row = await db.get(PlaybackEvent, uuid.UUID(case["event"]["playback_uuid"]))
        assert row is not None and row.valid == exp["valid"] and row.reason == exp["reason"]
        assert row.financial_status == ("PENDING" if exp["valid"] and case["event"]["kind"] == "EXTERNAL" else "NOT_APPLICABLE")


@pytest.mark.parametrize("bcase", POP["batch_cases"], ids=[c["name"] for c in POP["batch_cases"]])
async def test_pop_vector_batch_and_retry_through_database(db, bcase):
    ctx = bcase["context"]
    await _materialize(db, ctx)
    dev = uuid.UUID(ctx["authenticated_device_id"])
    first = await ingest.ingest_batch(db, dev, bcase["items"], now_ms=ctx["now_ms"])
    await db.commit()
    assert first.public() == bcase["expected"]
    retry = await ingest.ingest_batch(db, dev, bcase["items"], now_ms=ctx["now_ms"])
    await db.commit()
    assert retry.public() == bcase["expected_retry"]
