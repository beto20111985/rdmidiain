"""
RD Mídia Indoor — Pontes do domínio W-PLAY com os outros domínios (imports tardios).

Centraliza TODAS as chamadas a serviços de outros agentes (nomes do plano §4 e dos
avisos do coordenador), para que uma mudança de assinatura seja corrigida num ponto só:

- W-INV  (``app.modules.inventory.service``): ``current_manifest``, ``set_anchor``,
  ``manifest_public_keys``, ``mark_manifest_applied``, ``invalidate_screens``,
  ``get_manifest_version`` (manifesto de uma versão — validação de PoP).
- W-MEDIA (``app.modules.media.service``): ``open_media_range``, ``register_media_referencer``.
- W-ORG  (``app.modules.screens.service``): ``screen_establishment_at``.
- W-FIN  (``app.modules.financial.service``): ``accrue_commission_for_events``.

Nenhuma ponte devolve "sucesso falso": se o serviço do outro domínio não existir, a
função levanta ``DependencyUnavailable`` (o chamador decide: 503 na API; pendência no job).
"""
from __future__ import annotations

import importlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError

log = structlog.get_logger("rd.playback.integration")


class DependencyUnavailable(DomainError):
    status, code, message = 503, "DEPENDENCY_UNAVAILABLE", "Serviço temporariamente indisponível."


def _fn(module: str, name: str):
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return None
    return getattr(mod, name, None)


def _require(module: str, name: str):
    fn = _fn(module, name)
    if fn is None:
        log.error("dependency_missing", module=module, name=name)
        raise DependencyUnavailable(details={"dependency": f"{module}.{name}"})
    return fn


# ─── Telas (W-ORG) ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScreenInfo:
    screen_id: uuid.UUID
    code: str
    establishment_id: uuid.UUID
    org_id: Optional[uuid.UUID]
    city_key: Optional[str]
    timezone: str
    status: Optional[str]


def screen_code_of(screen: Any) -> str:
    return getattr(screen, "code", None) or getattr(screen, "friendly_id", None) or str(screen.id)


def screen_code_column():
    from app.models.screens import Screen

    return getattr(Screen, "code", None) or getattr(Screen, "friendly_id")


async def screen_info(db: AsyncSession, screen_id: uuid.UUID) -> Optional[ScreenInfo]:
    from app.core.authz import city_key
    from app.models.establishments import Establishment
    from app.models.screens import Screen

    row = (
        await db.execute(
            select(Screen, Establishment).join(Establishment, Establishment.id == Screen.establishment_id).where(
                Screen.id == screen_id
            )
        )
    ).first()
    if row is None:
        return None
    s, e = row
    ck = getattr(e, "city_key", None) or (city_key(e.state, e.city) if e.state and e.city else None)
    return ScreenInfo(
        screen_id=s.id,
        code=screen_code_of(s),
        establishment_id=e.id,
        org_id=e.org_id,
        city_key=ck,
        timezone=getattr(e, "timezone", None) or "America/Sao_Paulo",
        status=getattr(s, "status", None),
    )


async def screen_by_code(db: AsyncSession, code: str):
    from app.models.screens import Screen

    col = screen_code_column()
    return (await db.execute(select(Screen).where(col == code.strip().upper()))).scalar_one_or_none()


async def establishment_at(db: AsyncSession, screen_id: uuid.UUID, at: datetime) -> tuple[Optional[uuid.UUID], Optional[uuid.UUID]]:
    """(establishment_id, org_id) da tela na data ``at`` (W-ORG); fallback: vínculo atual."""
    fn = _fn("app.modules.screens.service", "screen_establishment_at")
    est_id = None
    if fn is not None:
        res = await fn(db, screen_id, at)
        est_id = getattr(res, "id", res)
    if est_id is None:
        info = await screen_info(db, screen_id)
        return (info.establishment_id, info.org_id) if info else (None, None)
    from app.models.establishments import Establishment

    est = await db.get(Establishment, est_id)
    return est_id, (est.org_id if est is not None else None)


# ─── Manifesto (W-INV) ───────────────────────────────────────────────────────

INV = "app.modules.inventory.service"


async def current_manifest(db: AsyncSession, screen_id: uuid.UUID):
    """``SignedManifest`` atual (``.version``, ``.manifest``, ``.envelope()``)."""
    return await _require(INV, "current_manifest")(db, screen_id)


def envelope_of(sm: Any) -> dict:
    if hasattr(sm, "envelope"):
        env = sm.envelope()
    elif isinstance(sm, dict):
        env = sm
    else:  # pragma: no cover
        raise DependencyUnavailable(details={"dependency": "SignedManifest.envelope"})
    return env


def manifest_of(sm: Any) -> dict:
    m = getattr(sm, "manifest", None)
    if m is None and isinstance(sm, dict):
        m = sm.get("manifest")
    return m or {}


def version_of(sm: Any) -> int:
    v = getattr(sm, "version", None)
    if v is None:
        v = manifest_of(sm).get("version")
    return int(v)


async def set_anchor(db: AsyncSession, screen_id: uuid.UUID, anchor_ms: int, device_id: uuid.UUID) -> int:
    return int(await _require(INV, "set_anchor")(db, screen_id, anchor_ms, device_id))


def manifest_public_keys() -> list[dict]:
    fn = _require(INV, "manifest_public_keys")
    return list(fn())


async def mark_manifest_applied(db: AsyncSession, screen_id: uuid.UUID, version: int, device_id: uuid.UUID) -> Any:
    return await _require(INV, "mark_manifest_applied")(db, screen_id, version, device_id)


async def invalidate_screens(db: AsyncSession, screen_ids: list[uuid.UUID], reason: str) -> Any:
    return await _require(INV, "invalidate_screens")(db, screen_ids, reason)


async def manifest_version(db: AsyncSession, screen_id: uuid.UUID, version: int) -> Optional[dict]:
    """Objeto ``manifest`` (RDPP §5) da versão informada, ou None se desconhecida.

    Se o conteúdo já foi podado pelo W-INV mas as referências existem, devolve
    ``{"_refs": {creative_ids, campaign_ids, ...}}`` (validação degradada sinalizada)."""
    fn = _require(INV, "get_manifest_version")
    res = await fn(db, screen_id, version)
    if res is None:
        refs_fn = _fn(INV, "get_manifest_refs")
        refs = await refs_fn(db, screen_id, version) if refs_fn is not None else None
        return {"_refs": refs} if refs else None
    if isinstance(res, dict):
        return res.get("manifest", res) if "schema" not in res else res
    return manifest_of(res)


async def screens_with_creative(db: AsyncSession, creative_id: uuid.UUID) -> Optional[list[uuid.UUID]]:
    fn = _fn(INV, "screens_with_creative")
    if fn is None:
        return None
    return list(await fn(db, creative_id))


# ─── Mídia (W-MEDIA) ─────────────────────────────────────────────────────────

MEDIA = "app.modules.media.service"


async def open_media_range(db: AsyncSession, media_id: uuid.UUID, range_header: Optional[str], if_range: Optional[str]):
    return await _require(MEDIA, "open_media_range")(db, media_id, range_header, if_range=if_range)


def register_media_referencer(name: str, fn) -> bool:
    reg = _fn(MEDIA, "register_media_referencer")
    if reg is None:
        return False
    reg(name, fn)
    return True


# ─── Financeiro (W-FIN) ──────────────────────────────────────────────────────

def accrue_commission_fn():
    """Função do W-FIN ou None (eventos ficam PENDING até existir)."""
    return _fn("app.modules.financial.service", "accrue_commission_for_events")
