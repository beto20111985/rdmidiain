"""
RD Mídia Indoor — Montagem PURA do manifesto RDPP/1 (§5) a partir do plano empacotado.

``assemble_manifest(inputs) -> dict`` produz o objeto ``manifest`` exato do protocolo
(todas as chaves obrigatórias; somente inteiros; mapas possivelmente vazios). O
serviço ``app.modules.inventory.service`` resolve os dados no banco e chama esta
função; o W-PLAY a usa para gerar os vetores de conformidade com dados sintéticos.

Regras (§5): ``external_plan`` indexado pelo início do bucket UTC (texto decimal),
entradas ``{"i","c","u","d"}`` ordenadas por ``u``; unidades não vendidas já vêm
preenchidas com conteúdo INTERNO; ``i`` = uuid5(tela|bucket|u) (concilia PoP
planejado × exibido); chaves ``cN``/``mN`` atribuídas em ordem canônica
(determinismo); cada (criativo, campanha, janela de validade) tem chave própria —
uma peça contratada em duas janelas não "vaza" para os dias entre elas na
repetição semanal além do horizonte; ``label`` por kind.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence

from app.modules.scheduler.internal import InternalStream, build_internal_pattern
from app.modules.scheduler.packer import DayPlan
from app.modules.scheduler.policy import UNIT_MS, RotationSpec

SCHEMA = "rd.manifest/1"
WEEK_MS = 7 * 24 * 3600 * 1000
MAX_VALID_UNTIL_MS = 253402300799999  # 9999-12-31T23:59:59.999Z
INSERTION_NS = uuid.UUID("6d1c3f0e-5a3b-4d7e-9f2a-7b1e0c4d2a11")

KIND_EXTERNAL = "EXTERNAL"
KIND_INTERNAL = "INTERNAL"
KIND_INSTITUTIONAL = "INSTITUTIONAL"
KIND_FALLBACK = "FALLBACK"
LABELS = {
    KIND_EXTERNAL: "Publicidade",
    KIND_INSTITUTIONAL: "Publicidade",
    KIND_INTERNAL: "Conteúdo do estabelecimento",
    KIND_FALLBACK: "RD Mídia Indoor",
}


@dataclass(frozen=True)
class MediaRef:
    media_id: str
    sha256: str
    size: int
    mime: str
    width: Optional[int] = None
    height: Optional[int] = None
    duration_ms: Optional[int] = None

    def to_manifest(self) -> dict:
        return {
            "media_id": self.media_id,
            "sha256": self.sha256,
            "size": int(self.size),
            "mime": self.mime,
            "width": None if self.width is None else int(self.width),
            "height": None if self.height is None else int(self.height),
            "duration_ms": None if self.duration_ms is None else int(self.duration_ms),
        }


@dataclass(frozen=True)
class CreativeRef:
    creative_id: str
    code: str
    kind: str
    media: MediaRef
    duration_ms: int
    campaign_id: Optional[str] = None
    advertiser_id: Optional[str] = None
    valid_from_ms: int = 0
    valid_until_ms: int = MAX_VALID_UNTIL_MS

    def identity(self) -> tuple:
        return (self.kind, self.creative_id, self.campaign_id or "", self.valid_from_ms, self.valid_until_ms)


@dataclass(frozen=True)
class EmergencyRef:
    emergency_id: str
    creative: CreativeRef
    from_ms: int
    until_ms: int


@dataclass
class ManifestInputs:
    screen_id: str
    screen_code: str
    timezone: str
    rotation: RotationSpec
    anchor_ms: Optional[int]
    horizon_from_ms: int
    horizon_to_ms: int
    settings: dict
    day_plans: Sequence[DayPlan] = ()
    # criativos tocáveis por linha (booking_pattern), na ordem de rotação
    line_creatives: dict = field(default_factory=dict)
    internal_items: Sequence[CreativeRef] = ()
    fallback_items: Sequence[CreativeRef] = ()
    emergencies: Sequence[EmergencyRef] = ()
    revoked: Sequence[str] = ()
    # campos voláteis (fora do checksum de conteúdo)
    version: int = 0
    manifest_id: str = "00000000-0000-0000-0000-000000000000"
    generated_at_ms: int = 0


VOLATILE_KEYS = ("manifest_id", "version", "generated_at_ms")


def insertion_id(screen_id: str, bucket_ms: int, u: int) -> str:
    return str(uuid.uuid5(INSERTION_NS, f"{screen_id}|{bucket_ms}|{u}"))


def assemble_manifest(inp: ManifestInputs) -> dict:
    rot = inp.rotation
    # 1. catálogo de criativos/mídias usados (chaves canônicas)
    used: dict[tuple, CreativeRef] = {}

    def reg(cr: CreativeRef) -> None:
        used.setdefault(cr.identity(), cr)

    internal = [c for c in inp.internal_items if 0 < c.duration_ms <= rot.internal_s * 1000]
    for c in internal:
        reg(c)
    for c in inp.fallback_items:
        reg(c)
    for em in inp.emergencies:
        reg(em.creative)

    # 2. plano externo (somente buckets no horizonte)
    gap_items = [c for c in inp.internal_items if 0 < c.duration_ms <= rot.external_s * 1000]
    by_ident = {c.identity(): c for c in gap_items}
    stream = InternalStream([(c.identity(), c.duration_ms) for c in gap_items])
    counters: dict[str, int] = {}
    raw_plan: list[tuple[int, list[tuple[int, tuple, int]]]] = []  # (bucket, [(u, identity, d_ms)])
    e_units = rot.units_per_block
    for dp in sorted(inp.day_plans, key=lambda p: p.frame.local_date):
        for b, blk in zip(dp.frame.buckets, dp.blocks):
            if not (inp.horizon_from_ms <= b < inp.horizon_to_ms):
                continue
            occ: list[tuple[int, tuple, int]] = []
            for pl in blk:
                crs = inp.line_creatives.get(pl.line_id) or []
                if not crs:
                    continue  # inventário vendido sem peça tocável ⇒ unidades vão ao interno
                n = counters.get(pl.line_id, 0)
                counters[pl.line_id] = n + 1
                cr = crs[n % len(crs)]
                if cr.duration_ms != pl.du * UNIT_MS:
                    continue
                reg(cr)
                occ.append((pl.u, cr.identity(), cr.duration_ms))
            # lacunas → conteúdo interno (fluxo contínuo)
            entries: list[tuple[int, tuple, int]] = []
            busy = sorted(occ)
            cursor_u = 0
            for u, ident, dms in busy + [(e_units, None, 0)]:
                if u > cursor_u:
                    pos = cursor_u
                    for ident_i, d_i in stream.fill((u - cursor_u) * UNIT_MS):
                        reg(by_ident[ident_i])
                        entries.append((pos, ident_i, d_i))
                        pos += d_i // UNIT_MS
                if ident is not None:
                    entries.append((u, ident, dms))
                    cursor_u = u + dms // UNIT_MS
            if entries:
                raw_plan.append((b, entries))
    # 3. chaves canônicas
    idents = sorted(used)
    ckey = {ident: f"c{i + 1}" for i, ident in enumerate(idents)}
    media_ids = sorted({used[i].media.media_id for i in idents})
    mkey = {mid: f"m{i + 1}" for i, mid in enumerate(media_ids)}
    media_map: dict[str, dict] = {}
    for ident in idents:
        m = used[ident].media
        media_map[mkey[m.media_id]] = m.to_manifest()
    creatives_map: dict[str, dict] = {}
    for ident in idents:
        cr = used[ident]
        creatives_map[ckey[ident]] = {
            "creative_id": cr.creative_id,
            "code": cr.code,
            "kind": cr.kind,
            "campaign_id": cr.campaign_id,
            "advertiser_id": cr.advertiser_id,
            "media": mkey[cr.media.media_id],
            "duration_ms": int(cr.duration_ms),
            "valid_from_ms": int(cr.valid_from_ms),
            "valid_until_ms": int(cr.valid_until_ms),
            "label": LABELS.get(cr.kind, "Publicidade"),
        }

    external_plan: dict[str, list] = {}
    for b, entries in raw_plan:
        external_plan[str(b)] = [
            {"i": insertion_id(inp.screen_id, b, u), "c": ckey[ident], "u": int(u), "d": int(d)}
            for u, ident, d in sorted(entries, key=lambda x: x[0])
        ]

    internal_pattern = build_internal_pattern([(ckey[c.identity()], c.duration_ms) for c in internal],
                                              rot.internal_s * 1000)
    fallback = [{"c": ckey[c.identity()], "d": int(c.duration_ms)} for c in inp.fallback_items]
    emergency = [
        {"id": em.emergency_id, "c": ckey[em.creative.identity()], "from_ms": int(em.from_ms),
         "until_ms": int(em.until_ms)}
        for em in sorted(inp.emergencies, key=lambda x: (x.from_ms, x.emergency_id))
    ]
    return {
        "schema": SCHEMA,
        "manifest_id": inp.manifest_id,
        "version": int(inp.version),
        "screen_id": inp.screen_id,
        "screen_code": inp.screen_code,
        "generated_at_ms": int(inp.generated_at_ms),
        "timezone": inp.timezone,
        "rotation": rot.to_manifest(),
        "anchor_ms": None if inp.anchor_ms is None else int(inp.anchor_ms),
        "horizon": {"from_ms": int(inp.horizon_from_ms), "to_ms": int(inp.horizon_to_ms),
                    "repeat_period_ms": WEEK_MS},
        "settings": inp.settings,
        "media": media_map,
        "creatives": creatives_map,
        "internal_pattern": internal_pattern,
        "fallback": fallback,
        "external_plan": external_plan,
        "emergency": emergency,
        "revoked": sorted(set(inp.revoked)),
    }


def content_view(manifest: dict) -> dict:
    """Manifesto sem os campos voláteis (base do checksum de conteúdo/determinismo)."""
    return {k: v for k, v in manifest.items() if k not in VOLATILE_KEYS}


def referenced_media_ids(manifest: dict) -> list[str]:
    return sorted({m["media_id"] for m in (manifest.get("media") or {}).values()})


def referenced_creatives(manifest: dict) -> tuple[list[str], list[str]]:
    crs = (manifest.get("creatives") or {}).values()
    return (sorted({c["creative_id"] for c in crs}),
            sorted({c["campaign_id"] for c in crs if c.get("campaign_id")}))


def default_settings(*, orientation: str = "LANDSCAPE", offline_alert_days: Sequence[int] = (7, 28),
                     panel_url: str = "") -> dict:
    return {
        "image_default_ms": 30000,
        "fade_ms": 400,
        "orientation": orientation,
        "footer": {"enabled": True, "height_permille": 60},
        "offline_alert_days": [int(x) for x in offline_alert_days],
        "heartbeat_interval_ms": 30000,
        "telemetry_interval_ms": 300000,
        "panel_url": panel_url,
    }
