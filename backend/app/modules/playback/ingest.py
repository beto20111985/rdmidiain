"""
RD Mídia Indoor — Ingestão e validação de prova de exibição (PoP, RDPP/1 §7).

``ingest_batch(db, device, items)``: lote ≤ 200, idempotente (PK ``playback_uuid``,
``INSERT … ON CONFLICT DO NOTHING``), serializado por dispositivo (``SELECT … FOR
UPDATE`` na linha do dispositivo), com resposta por evento + ``ack_upto_seq``.

Status por evento:
- ``DUPLICATE`` — ``playback_uuid`` já recebido (sem efeito; devolve a validade original);
- ``REJECTED``  — evento malformado (não gravado), de OUTRO dispositivo ou com assinatura
  inválida (gravados como evidência, nunca válidos);
- ``ACCEPTED``  — autêntico (assinatura ES256 do dispositivo autenticado sobre RDJ1);
  ``valid`` = TODAS as regras abaixo passaram (100%, sem tolerância).

Regras (``reason`` = primeira violada; ``reasons`` = todas):
DEVICE_NOT_BOUND · BEFORE_PAIRING · CLOCK_FUTURE (> 120 s no futuro do servidor) ·
TIME_INCONSISTENT (finished ≠ started + actual) · DURATION_IMPLAUSIBLE ·
SEQ_ROLLBACK (seq ≤ último aceito) · NONCE_REUSED · NOT_COMPLETED (result ≠ COMPLETED) ·
INCOMPLETE_DURATION (actual < expected — 29 999/30 000 é inválido) · MANIFEST_UNKNOWN ·
CREATIVE_NOT_IN_MANIFEST · CREATIVE_MISMATCH · CAMPAIGN_MISMATCH ·
INSERTION_NOT_IN_MANIFEST · EXPECTED_MISMATCH · CAMPAIGN_NOT_ACTIVE (fora da vigência
no ``started_at_ms``) · CREATIVE_REVOKED (manifesto ``revoked`` ou kill ativo no início)
· OVERLAP (sobreposição com outro evento do dispositivo) · IMPOSSIBLE_VOLUME (tela)
(soma de ``actual_ms`` recortada na hora > 3 600 000).

Anomalias sinalizadas (``flags``, não invalidam sozinhas): SEQ_GAP, CHAIN_BREAK,
LARGE_CLOCK_OFFSET. Eventos offline NÃO são invalidados por chegarem tarde (não há
regra "recebido muito depois de started_at").
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutils import local_date, utcnow
from app.models.devices import CreativeKill, Device
from app.models.playback import FinancialStatus, PlaybackEvent, PopStatus
from app.modules.playback import config as pcfg
from app.modules.playback import integration as integ
from app.modules.playback import protocol as proto

log = structlog.get_logger("rd.playback.ingest")

SCHEMA = "rd.pop/1"
EVENT_KEYS = frozenset(
    {
        "schema",
        "playback_uuid",
        "device_id",
        "screen_id",
        "creative_id",
        "creative_code",
        "campaign_id",
        "insertion_id",
        "kind",
        "manifest_version",
        "started_at_ms",
        "finished_at_ms",
        "expected_ms",
        "actual_ms",
        "result",
        "seq",
        "nonce",
        "prev_hash",
        "clock_offset_ms",
        "offline",
    }
)
KINDS = frozenset({"EXTERNAL", "INSTITUTIONAL", "INTERNAL", "FALLBACK"})
RESULTS = frozenset({"COMPLETED", "INTERRUPTED", "ERROR", "SKIPPED"})
HOUR_MS = 3_600_000
MAX_SPAN_MS = 6 * HOUR_MS  # eventos maiores são DURATION_IMPLAUSIBLE e ignorados no cálculo de sobreposição
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[A-Z0-9-]{1,16}$")
LARGE_OFFSET_MS = 120_000


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _uuid_str(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    try:
        return str(uuid.UUID(v)) == v
    except ValueError:
        return False


def schema_errors(ev: Any) -> list[str]:
    """Erros de forma do evento (vazio = ok)."""
    if not isinstance(ev, dict):
        return ["event"]
    errs: list[str] = []
    keys = set(ev)
    missing = EVENT_KEYS - keys
    extra = keys - EVENT_KEYS
    errs += [f"missing:{k}" for k in sorted(missing)]
    errs += [f"extra:{k}" for k in sorted(extra)]
    if missing:
        return errs
    if ev["schema"] != SCHEMA:
        errs.append("schema")
    for k in ("playback_uuid", "device_id", "screen_id", "creative_id"):
        if not _uuid_str(ev[k]):
            errs.append(k)
    for k in ("campaign_id", "insertion_id"):
        if ev[k] is not None and not _uuid_str(ev[k]):
            errs.append(k)
    if not isinstance(ev["creative_code"], str) or not _CODE.match(ev["creative_code"]):
        errs.append("creative_code")
    if ev["kind"] not in KINDS:
        errs.append("kind")
    if ev["result"] not in RESULTS:
        errs.append("result")
    for k in ("manifest_version", "started_at_ms", "finished_at_ms", "expected_ms", "actual_ms", "seq"):
        if not _is_int(ev[k]) or ev[k] < 0:
            errs.append(k)
    if _is_int(ev.get("expected_ms")) and ev["expected_ms"] == 0:
        errs.append("expected_ms")
    if not _is_int(ev["clock_offset_ms"]):
        errs.append("clock_offset_ms")
    if not proto.is_b64u_bytes(ev["nonce"], 16):
        errs.append("nonce")
    if not isinstance(ev["prev_hash"], str) or not _HEX64.match(ev["prev_hash"]):
        errs.append("prev_hash")
    if not isinstance(ev["offline"], bool):
        errs.append("offline")
    for k in ("manifest_version", "started_at_ms", "finished_at_ms", "expected_ms", "actual_ms", "seq", "clock_offset_ms"):
        if _is_int(ev.get(k)) and abs(ev[k]) > 2**62:
            errs.append(f"range:{k}")
    return errs


@dataclass
class EventResult:
    playback_uuid: Optional[str]
    status: str
    valid: bool
    reason: Optional[str]
    seq: Optional[int] = None
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {"playback_uuid": self.playback_uuid, "status": self.status, "valid": self.valid, "reason": self.reason}


@dataclass
class _ManifestIndex:
    manifest: dict
    by_creative: dict  # creative_id(str) → [(key, entry), ...] (um criativo pode ter várias chaves)
    insertions: dict  # insertion_id(str) → (key, d)
    durations: dict  # key → set(d)
    revoked: set
    timezone: Optional[str]
    refs: Optional[dict] = None  # conteúdo podado pelo W-INV: só listas de referências


def index_manifest(m: dict) -> _ManifestIndex:
    if "_refs" in m:  # conteúdo podado: validação degradada (sinalizada em flags)
        r = m["_refs"] or {}
        return _ManifestIndex({}, {}, {}, {}, set(), None,
                              refs={"creative_ids": {str(x) for x in r.get("creative_ids") or []},
                                    "campaign_ids": {str(x) for x in r.get("campaign_ids") or []}})
    creatives = m.get("creatives") or {}
    by_creative: dict = {}
    durations: dict = {}
    for key, c in creatives.items():
        if isinstance(c, dict) and c.get("creative_id"):
            by_creative.setdefault(str(c["creative_id"]), []).append((key, c))
            if _is_int(c.get("duration_ms")):
                durations.setdefault(key, set()).add(c["duration_ms"])
    insertions: dict = {}
    for entries in (m.get("external_plan") or {}).values():
        for e in entries or []:
            if isinstance(e, dict) and e.get("i"):
                insertions[str(e["i"])] = (e.get("c"), e.get("d"))
                if _is_int(e.get("d")):
                    durations.setdefault(e.get("c"), set()).add(e["d"])
    for seq in (m.get("internal_pattern") or []):
        for it in seq or []:
            if isinstance(it, dict) and _is_int(it.get("d")):
                durations.setdefault(it.get("c"), set()).add(it["d"])
    for it in m.get("fallback") or []:
        if isinstance(it, dict) and _is_int(it.get("d")):
            durations.setdefault(it.get("c"), set()).add(it["d"])
    return _ManifestIndex(m, by_creative, insertions, durations, {str(x) for x in (m.get("revoked") or [])}, m.get("timezone"))


def manifest_reasons(ev: dict, mi: Optional[_ManifestIndex]) -> list[str]:
    """Regras puras contra o manifesto da versão (também usadas pelos vetores)."""
    if mi is None:
        return ["MANIFEST_UNKNOWN"]
    if mi.refs is not None:
        out = []
        if ev["creative_id"] not in mi.refs["creative_ids"]:
            return ["CREATIVE_NOT_IN_MANIFEST"]
        if ev["campaign_id"] and ev["campaign_id"] not in mi.refs["campaign_ids"]:
            out.append("CAMPAIGN_MISMATCH")
        return out
    out: list[str] = []
    hits = mi.by_creative.get(ev["creative_id"]) or []
    if not hits:
        return ["CREATIVE_NOT_IN_MANIFEST"]
    cands = [(k, c) for k, c in hits if c.get("code") == ev["creative_code"] and c.get("kind") == ev["kind"]]
    if not cands:
        out.append("CREATIVE_MISMATCH")
        cands = hits
    camp = [(k, c) for k, c in cands if (c.get("campaign_id") or None) == (ev["campaign_id"] or None)]
    if not camp:
        out.append("CAMPAIGN_MISMATCH")
        camp = cands
    keys = {k for k, _ in camp}
    if ev["kind"] == "EXTERNAL":
        ins = mi.insertions.get(ev["insertion_id"]) if ev["insertion_id"] else None
        if ins is None or ins[0] not in keys:
            out.append("INSERTION_NOT_IN_MANIFEST")
            sel = camp
        else:
            sel = [(k, c) for k, c in camp if k == ins[0]]
            if ins[1] != ev["expected_ms"]:
                out.append("EXPECTED_MISMATCH")
    else:
        sel = [(k, c) for k, c in camp if ev["expected_ms"] in mi.durations.get(k, set())]
        if not sel:
            out.append("EXPECTED_MISMATCH")
            sel = camp
    if ev["kind"] != "FALLBACK":
        def _in_window(c: dict) -> bool:
            vf, vu = c.get("valid_from_ms"), c.get("valid_until_ms")
            return not ((_is_int(vf) and ev["started_at_ms"] < vf) or (_is_int(vu) and ev["started_at_ms"] >= vu))

        if not any(_in_window(c) for _, c in sel):
            out.append("CAMPAIGN_NOT_ACTIVE")
    if ev["creative_id"] in mi.revoked:
        out.append("CREATIVE_REVOKED")
    return out


def intrinsic_reasons(ev: dict, *, now_ms: int, paired_at_ms: Optional[int], bound_screen_id: Optional[str]) -> list[str]:
    """Regras que só dependem do evento e do dispositivo."""
    out: list[str] = []
    if bound_screen_id is not None and ev["screen_id"] != bound_screen_id:
        out.append("DEVICE_NOT_BOUND")
    if paired_at_ms is not None and ev["started_at_ms"] < paired_at_ms:
        out.append("BEFORE_PAIRING")
    if ev["started_at_ms"] > now_ms + pcfg.POP_CLOCK_FUTURE_MS or ev["finished_at_ms"] > now_ms + pcfg.POP_CLOCK_FUTURE_MS:
        out.append("CLOCK_FUTURE")
    if ev["finished_at_ms"] != ev["started_at_ms"] + ev["actual_ms"]:
        out.append("TIME_INCONSISTENT")
    if ev["actual_ms"] > MAX_SPAN_MS or ev["actual_ms"] > ev["expected_ms"] + pcfg.POP_MAX_OVERRUN_MS:
        out.append("DURATION_IMPLAUSIBLE")
    return out


def completion_reasons(ev: dict) -> list[str]:
    out: list[str] = []
    if ev["result"] != "COMPLETED":
        out.append("NOT_COMPLETED")
    if ev["actual_ms"] < ev["expected_ms"]:
        out.append("INCOMPLETE_DURATION")
    return out


REASON_ORDER = [
    "DEVICE_NOT_BOUND",
    "BEFORE_PAIRING",
    "CLOCK_FUTURE",
    "TIME_INCONSISTENT",
    "DURATION_IMPLAUSIBLE",
    "SEQ_ROLLBACK",
    "NONCE_REUSED",
    "NOT_COMPLETED",
    "INCOMPLETE_DURATION",
    "MANIFEST_UNKNOWN",
    "CREATIVE_NOT_IN_MANIFEST",
    "CREATIVE_MISMATCH",
    "CAMPAIGN_MISMATCH",
    "INSERTION_NOT_IN_MANIFEST",
    "EXPECTED_MISMATCH",
    "CAMPAIGN_NOT_ACTIVE",
    "CREATIVE_REVOKED",
    "OVERLAP",
    "IMPOSSIBLE_VOLUME",
]


def order_reasons(reasons: list[str]) -> list[str]:
    uniq = list(dict.fromkeys(reasons))
    return sorted(uniq, key=lambda r: REASON_ORDER.index(r) if r in REASON_ORDER else len(REASON_ORDER))


def _dt(ms_: int) -> datetime:
    return datetime.fromtimestamp(ms_ / 1000, tz=timezone.utc)


# ─── Consultas por dispositivo ───────────────────────────────────────────────

async def _overlaps(db: AsyncSession, device_id: uuid.UUID, st: int, fin: int) -> bool:
    if fin <= st:
        return False
    q = (
        select(PlaybackEvent.playback_uuid)
        .where(
            PlaybackEvent.device_id == device_id,
            PlaybackEvent.status == PopStatus.ACCEPTED.value,
            PlaybackEvent.actual_ms > 0,
            PlaybackEvent.actual_ms <= MAX_SPAN_MS,
            PlaybackEvent.started_at_ms > st - MAX_SPAN_MS,
            PlaybackEvent.started_at_ms < fin,
            PlaybackEvent.finished_at_ms > st,
        )
        .limit(1)
    )
    return (await db.execute(q)).first() is not None


async def _hour_volume(db: AsyncSession, screen_id: uuid.UUID, st: int, fin: int) -> int:
    """Soma de ``actual_ms`` (recortada) dos eventos aceitos da TELA (qualquer dispositivo) na
    hora de ``st``. Uma tela exibe um item por vez: soma > 1 h na hora = volume impossível."""
    h0 = (st // HOUR_MS) * HOUR_MS
    h1 = h0 + HOUR_MS
    clipped = func.greatest(
        0, func.least(PlaybackEvent.finished_at_ms, h1) - func.greatest(PlaybackEvent.started_at_ms, h0)
    )
    q = select(func.coalesce(func.sum(clipped), 0)).where(
        PlaybackEvent.screen_id == screen_id,
        PlaybackEvent.status == PopStatus.ACCEPTED.value,
        PlaybackEvent.actual_ms > 0,
        PlaybackEvent.actual_ms <= MAX_SPAN_MS,
        PlaybackEvent.started_at_ms > h0 - MAX_SPAN_MS,
        PlaybackEvent.started_at_ms < h1,
        PlaybackEvent.finished_at_ms > h0,
    )
    existing = int((await db.execute(q)).scalar_one() or 0)
    mine = max(0, min(fin, h1) - max(st, h0))
    return existing + mine


async def _nonce_used(db: AsyncSession, device_id: uuid.UUID, nonce: str) -> bool:
    q = select(PlaybackEvent.playback_uuid).where(PlaybackEvent.device_id == device_id, PlaybackEvent.nonce == nonce).limit(1)
    return (await db.execute(q)).first() is not None


async def _killed(db: AsyncSession, creative_id: uuid.UUID, screen_id: uuid.UUID, started: datetime) -> bool:
    q = (
        select(CreativeKill.id)
        .where(
            CreativeKill.creative_id == creative_id,
            or_(CreativeKill.screen_id.is_(None), CreativeKill.screen_id == screen_id),
            CreativeKill.created_at <= started,
            or_(CreativeKill.lifted_at.is_(None), CreativeKill.lifted_at > started),
        )
        .limit(1)
    )
    return (await db.execute(q)).first() is not None


# ─── Decisão PURA (usada pela ingestão e pelos vetores de conformidade) ──────

@dataclass(frozen=True)
class DeviceFacts:
    device_id: str
    screen_id: str  # tela à qual o dispositivo está vinculado
    paired_at_ms: int
    public_key: str


@dataclass
class RuleFacts:
    now_ms: int
    last_seq: Optional[int]
    last_hash: Optional[str]
    nonce_used: bool
    killed: bool
    overlap: bool
    hour_volume_ms: int  # soma recortada na hora de started_at, INCLUINDO este evento
    manifest: Optional[_ManifestIndex]


def precheck(ev: dict, sig: str, device: DeviceFacts) -> tuple[str, list[str]]:
    """Autenticidade: ACCEPTED (assinatura do dispositivo autenticado) ou REJECTED."""
    if ev["device_id"] != device.device_id:
        return PopStatus.REJECTED.value, ["DEVICE_MISMATCH"]
    if not proto.verify_es256(device.public_key, ev, sig):
        return PopStatus.REJECTED.value, ["BAD_SIGNATURE"]
    return PopStatus.ACCEPTED.value, []


def rule_reasons(ev: dict, device: DeviceFacts, f: RuleFacts) -> tuple[list[str], list[str]]:
    """Todas as regras de validade (§7) e anomalias, a partir de fatos já levantados."""
    reasons = intrinsic_reasons(ev, now_ms=f.now_ms, paired_at_ms=device.paired_at_ms, bound_screen_id=device.screen_id)
    if f.last_seq is not None and ev["seq"] <= f.last_seq:
        reasons.append("SEQ_ROLLBACK")
    if f.nonce_used:
        reasons.append("NONCE_REUSED")
    reasons += completion_reasons(ev)
    reasons += manifest_reasons(ev, f.manifest)
    if f.killed and "CREATIVE_REVOKED" not in reasons:
        reasons.append("CREATIVE_REVOKED")
    if ev["actual_ms"] <= MAX_SPAN_MS:
        if f.overlap:
            reasons.append("OVERLAP")
        elif f.hour_volume_ms > HOUR_MS:
            reasons.append("IMPOSSIBLE_VOLUME")
    flags: list[str] = []
    if f.last_seq is not None and ev["seq"] > f.last_seq + 1:
        flags.append("SEQ_GAP")
    if f.last_seq is not None and ev["seq"] == f.last_seq + 1 and f.last_hash and ev["prev_hash"] != f.last_hash:
        flags.append("CHAIN_BREAK")
    if abs(ev["clock_offset_ms"]) > LARGE_OFFSET_MS:
        flags.append("LARGE_CLOCK_OFFSET")
    if f.manifest is not None and f.manifest.refs is not None:
        flags.append("MANIFEST_CONTENT_PRUNED")
    return order_reasons(reasons), flags


def duplicate_result(pu: str, seq: int, ehash: str, stored_valid: Optional[bool], stored_reason: Optional[str],
                     stored_hash: Optional[str]) -> "EventResult":
    same = stored_hash == ehash
    return EventResult(pu, PopStatus.DUPLICATE.value, bool(stored_valid and same),
                       stored_reason if same else "DUPLICATE_UUID_MISMATCH", seq)


# ─── Lote ────────────────────────────────────────────────────────────────────

@dataclass
class BatchOutcome:
    results: list[EventResult]
    ack_upto_seq: Optional[int]
    accepted: int = 0
    valid: int = 0

    def public(self) -> dict:
        return {"results": [r.public() for r in self.results], "ack_upto_seq": self.ack_upto_seq}


def parse_items(items: list[Any]) -> tuple[list[Optional[EventResult]], list[tuple[int, dict, str]]]:
    """Separa itens malformados (REJECTED SCHEMA_INVALID, não gravados) dos bem formados,
    estes ordenados por ``seq`` (ordem de chegada como desempate)."""
    results: list[Optional[EventResult]] = [None] * len(items)
    parsed: list[tuple[int, dict, str]] = []
    for i, it in enumerate(items):
        ev = it.get("event") if isinstance(it, dict) else None
        sig = it.get("signature") if isinstance(it, dict) else None
        pu = ev.get("playback_uuid") if isinstance(ev, dict) and isinstance(ev.get("playback_uuid"), str) else None
        if schema_errors(ev) or not isinstance(sig, str) or len(sig) > 100:
            results[i] = EventResult(pu if _uuid_str(pu) else None, PopStatus.REJECTED.value, False, "SCHEMA_INVALID")
            continue
        parsed.append((i, ev, sig))
    parsed.sort(key=lambda t: (t[1]["seq"], t[0]))
    return results, parsed


def ack_upto(last_seq: Optional[int], results: list[EventResult]) -> Optional[int]:
    seqs = [r.seq for r in results if r.seq is not None]
    return max([x for x in [last_seq, *seqs] if x is not None], default=None)


async def ingest_batch(
    db: AsyncSession, device_id: uuid.UUID, items: list[Any], *, now_ms: Optional[int] = None
) -> BatchOutcome:
    if now_ms is None:
        now_ms = int(utcnow().timestamp() * 1000)
    # serializa a ingestão por dispositivo (seq/nonce/sobreposição consistentes)
    dev = (await db.execute(select(Device).where(Device.id == device_id).with_for_update())).scalar_one()
    facts_dev = DeviceFacts(str(dev.id), str(dev.screen_id), int(dev.paired_at.timestamp() * 1000), dev.public_key)

    results, parsed = parse_items(items)
    uuids = [uuid.UUID(ev["playback_uuid"]) for _, ev, _ in parsed]
    existing: dict[str, PlaybackEvent] = {}
    if uuids:
        rows = (await db.execute(select(PlaybackEvent).where(PlaybackEvent.playback_uuid.in_(uuids)))).scalars().all()
        existing = {str(r.playback_uuid): r for r in rows}

    manifests: dict[int, Optional[_ManifestIndex]] = {}
    est_cache: dict[Any, tuple] = {}
    tz_default: Optional[str] = None
    seen: dict[str, tuple] = {}
    last_seq = dev.pop_last_seq
    last_hash = dev.pop_last_hash
    n_acc = n_valid = 0

    for i, ev, sig in parsed:
        pu = ev["playback_uuid"]
        ehash = proto.sha256_hex_of(ev)
        prev_row = existing.get(pu)
        if prev_row is not None:
            results[i] = duplicate_result(pu, ev["seq"], ehash, prev_row.valid, prev_row.reason, prev_row.event_hash)
            continue
        if pu in seen:  # repetido no MESMO lote: mesma resposta de um reenvio
            sv, sr, sh = seen[pu]
            results[i] = duplicate_result(pu, ev["seq"], ehash, sv, sr, sh)
            continue

        status, reasons = precheck(ev, sig, facts_dev)
        flags: list[str] = []
        claimed = None
        if status == PopStatus.REJECTED.value and reasons == ["DEVICE_MISMATCH"]:
            claimed = {"device_id": ev["device_id"], "screen_id": ev["screen_id"]}
        elif ev["screen_id"] != facts_dev.screen_id:
            claimed = {"screen_id": ev["screen_id"]}

        started = _dt(ev["started_at_ms"])
        mi: Optional[_ManifestIndex] = None
        if status == PopStatus.ACCEPTED.value:
            v = ev["manifest_version"]
            if v not in manifests:
                m = await integ.manifest_version(db, dev.screen_id, v)
                manifests[v] = index_manifest(m) if m else None
            mi = manifests[v]
            small = ev["actual_ms"] <= MAX_SPAN_MS
            overlap = small and await _overlaps(db, dev.id, ev["started_at_ms"], ev["finished_at_ms"])
            volume = (
                await _hour_volume(db, dev.screen_id, ev["started_at_ms"], ev["finished_at_ms"]) if small and not overlap else 0
            )
            facts = RuleFacts(
                now_ms=now_ms,
                last_seq=last_seq,
                last_hash=last_hash,
                nonce_used=await _nonce_used(db, dev.id, ev["nonce"]),
                killed=await _killed(db, uuid.UUID(ev["creative_id"]), dev.screen_id, started),
                overlap=bool(overlap),
                hour_volume_ms=volume,
                manifest=mi,
            )
            reasons, flags = rule_reasons(ev, facts_dev, facts)
            if last_seq is None or ev["seq"] > last_seq:
                last_seq, last_hash = ev["seq"], ehash

        tz = mi.timezone if mi else None
        if tz is None:
            if tz_default is None:
                info = await integ.screen_info(db, dev.screen_id)
                tz_default = info.timezone if info else "America/Sao_Paulo"
            tz = tz_default
        try:
            local_day = local_date(started, tz)
        except Exception:
            local_day = local_date(started, "America/Sao_Paulo")
        if local_day not in est_cache:
            est_cache[local_day] = await integ.establishment_at(db, dev.screen_id, started)
        est_id, org_id = est_cache[local_day]

        valid = status == PopStatus.ACCEPTED.value and not reasons
        fin = FinancialStatus.PENDING.value if (valid and ev["kind"] == "EXTERNAL") else FinancialStatus.NOT_APPLICABLE.value
        row = {
            "playback_uuid": uuid.UUID(pu),
            "device_id": dev.id,
            "screen_id": dev.screen_id,
            "establishment_id": est_id,
            "org_id": org_id,
            "creative_id": uuid.UUID(ev["creative_id"]),
            "creative_code": ev["creative_code"],
            "campaign_id": uuid.UUID(ev["campaign_id"]) if ev["campaign_id"] else None,
            "insertion_id": uuid.UUID(ev["insertion_id"]) if ev["insertion_id"] else None,
            "kind": ev["kind"],
            "manifest_version": ev["manifest_version"],
            "started_at_ms": ev["started_at_ms"],
            "finished_at_ms": ev["finished_at_ms"],
            "started_at": started,
            "finished_at": _dt(ev["finished_at_ms"]),
            "expected_ms": ev["expected_ms"],
            "actual_ms": ev["actual_ms"],
            "result": ev["result"],
            "seq": ev["seq"],
            "nonce": ev["nonce"],
            "prev_hash": ev["prev_hash"],
            "event_hash": ehash,
            "clock_offset_ms": ev["clock_offset_ms"],
            "offline": ev["offline"],
            "signature": sig,
            "claimed": claimed,
            "status": status,
            "valid": valid,
            "reason": reasons[0] if reasons else None,
            "reasons": reasons,
            "flags": flags,
            "local_day": local_day,
            "financial_status": fin,
        }
        ins = await db.execute(
            pg_insert(PlaybackEvent).values(**row).on_conflict_do_nothing(index_elements=["playback_uuid"]).returning(
                PlaybackEvent.playback_uuid
            )
        )
        if ins.first() is None:  # corrida com outra transação: o UUID já foi gravado
            other = (
                await db.execute(
                    select(PlaybackEvent.valid, PlaybackEvent.reason, PlaybackEvent.event_hash).where(
                        PlaybackEvent.playback_uuid == uuid.UUID(pu)
                    )
                )
            ).first()
            results[i] = duplicate_result(pu, ev["seq"], ehash, other.valid if other else None,
                                          other.reason if other else None, other.event_hash if other else None)
            continue
        if status == PopStatus.ACCEPTED.value:
            n_acc += 1
            n_valid += int(valid)
        results[i] = EventResult(pu, status, valid, reasons[0] if reasons else None, ev["seq"], reasons, flags)
        seen[pu] = (valid, reasons[0] if reasons else None, ehash)

    if last_seq is not None and last_seq != dev.pop_last_seq:
        dev.pop_last_seq = last_seq
        dev.pop_last_hash = last_hash
    if n_acc:
        dev.last_play_at = utcnow()
    final = [r for r in results if r is not None]
    return BatchOutcome(final, ack_upto(dev.pop_last_seq, final), n_acc, n_valid)


# ─── Entrega ao financeiro (W-FIN) ───────────────────────────────────────────

async def deliver_pending_financial(db: AsyncSession, limit: int = 500) -> dict:
    """Entrega eventos válidos EXTERNAL pendentes a ``accrue_commission_for_events``.

    Idempotente: lote travado com ``FOR UPDATE SKIP LOCKED``; marca APPROPRIATED na
    mesma transação da chamada ao financeiro (que é idempotente por ``playback_uuid``).
    Sem a função do W-FIN, nada muda (eventos seguem PENDING — sem sucesso falso).
    """
    fn = integ.accrue_commission_fn()
    if fn is None:
        pending = (await db.execute(
            select(func.count()).select_from(PlaybackEvent).where(PlaybackEvent.financial_status == FinancialStatus.PENDING.value)
        )).scalar_one()
        return {"delivered": 0, "pending": int(pending), "reason": "FINANCIAL_SERVICE_UNAVAILABLE"}
    rows = (
        await db.execute(
            select(PlaybackEvent)
            .where(PlaybackEvent.financial_status == FinancialStatus.PENDING.value)
            .order_by(PlaybackEvent.received_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    if not rows:
        return {"delivered": 0, "pending": 0}
    for r in rows:
        r.financial_attempts = (r.financial_attempts or 0) + 1
    by_campaign: dict = {}
    for r in rows:
        by_campaign.setdefault(r.campaign_id, []).append(r)
    delivered, failed, errors = 0, 0, {}
    now = utcnow()
    # um lote (savepoint) por campanha: campanha sem plano não bloqueia as demais
    for cid in sorted(by_campaign, key=str):
        group = by_campaign[cid]
        try:
            async with db.begin_nested():
                await fn(db, list(group))
        except Exception as exc:  # segue PENDING; o job reentrega
            failed += len(group)
            errors[str(cid)] = type(exc).__name__
            log.error("financial_accrual_failed", campaign_id=str(cid), error_type=type(exc).__name__, events=len(group))
            continue
        for r in group:
            r.financial_status = FinancialStatus.APPROPRIATED.value
            r.financial_appropriated_at = now
        delivered += len(group)
    out = {"delivered": delivered, "pending": failed}
    if errors:
        out["errors"] = errors
    return out


__all__ = [
    "SCHEMA",
    "EVENT_KEYS",
    "schema_errors",
    "index_manifest",
    "manifest_reasons",
    "intrinsic_reasons",
    "completion_reasons",
    "order_reasons",
    "ingest_batch",
    "deliver_pending_financial",
    "BatchOutcome",
    "EventResult",
]
