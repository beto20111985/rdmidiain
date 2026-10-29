"""
RD Mídia Indoor — Atualização do app do player (RDPP/1 §8; ETAPA2 #257–#258; A4 Q23/Q24).

- Versões cadastradas por admin com assinatura ES256 feita FORA do servidor com a chave
  de RELEASE (distinta da chave de manifesto) sobre RDJ1
  ``{"platform","purpose":"rd-app-update-v1","sha256","size","version"}``; o servidor
  VERIFICA contra ``PLAYER_RELEASE_PUBLIC_KEYS`` antes de aceitar (nunca assina).
- Rollout controlado no servidor: ondas (``rollout_percent`` com bucket determinístico
  por dispositivo), grupos (``target_groups`` × ``Device.update_group``) e fila com
  limite de downloads simultâneos (``max_concurrent``; vagas no Redis com TTL).
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.errors import Conflict, DomainError, NotFound
from app.core.redis import get_redis, rkey
from app.core.timeutils import utcnow
from app.models.devices import AppRelease, AppReleaseStatus, Device
from app.modules.playback import config as pcfg
from app.modules.playback import protocol as proto

PLATFORMS = ("web", "windows", "android")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?$")
_SHA = re.compile(r"^[0-9a-f]{64}$")

_SLOT_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZSCORE', KEYS[1], ARGV[2]) then return 1 end
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], ARGV[4], ARGV[2])
  redis.call('EXPIRE', KEYS[1], ARGV[5])
  return 1
end
return 0
"""


def version_key(v: str) -> tuple:
    m = _SEMVER.match(v or "")
    if not m:
        raise DomainError("INVALID_VERSION", "Versão inválida (use x.y.z).", status=422)
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def update_message(platform: str, version: str, sha256: str, size: int) -> dict:
    return {"platform": platform, "purpose": proto.PURPOSE_APP_UPDATE, "sha256": sha256, "size": size, "version": version}


def release_public(rel: AppRelease) -> dict:
    def iso(d):
        return d.isoformat().replace("+00:00", "Z") if d else None

    return {
        "id": str(rel.id),
        "platform": rel.platform,
        "version": rel.version,
        "url": rel.url,
        "sha256": rel.sha256,
        "size": rel.size,
        "signature": rel.signature,
        "key_id": rel.key_id,
        "mandatory": rel.mandatory,
        "status": rel.status,
        "rollout_percent": rel.rollout_percent,
        "target_groups": rel.target_groups,
        "max_concurrent": rel.max_concurrent,
        "notes": rel.notes,
        "created_at": iso(rel.created_at),
        "published_at": iso(rel.published_at),
    }


def _validate_url(url: str) -> str:
    p = urlsplit(url or "")
    if p.scheme not in ("https", "http") or not p.netloc or p.username or p.password or len(url) > 2000:
        raise DomainError("INVALID_URL", "URL do pacote inválida.", status=422)
    return url


async def create_release(db: AsyncSession, ctx: AuthContext, body: dict) -> AppRelease:
    platform = body.get("platform")
    if platform not in PLATFORMS:
        raise DomainError("INVALID_PLATFORM", "Plataforma inválida.", status=422)
    version = str(body.get("version") or "")
    version_key(version)
    sha = str(body.get("sha256") or "").lower()
    if not _SHA.match(sha):
        raise DomainError("INVALID_SHA256", "sha256 inválido.", status=422)
    size = body.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise DomainError("INVALID_SIZE", "Tamanho inválido.", status=422)
    key_id = str(body.get("key_id") or "")
    keys = pcfg.release_public_keys()
    if key_id not in keys:
        raise DomainError("UNKNOWN_RELEASE_KEY", "Chave de release desconhecida (configure PLAYER_RELEASE_PUBLIC_KEYS).", status=422)
    sig = str(body.get("signature") or "")
    if not proto.verify_es256(keys[key_id], update_message(platform, version, sha, size), sig):
        raise DomainError("INVALID_RELEASE_SIGNATURE", "Assinatura da versão não confere com a chave de release.", status=422)
    dup = (await db.execute(select(AppRelease.id).where(AppRelease.platform == platform, AppRelease.version == version))).first()
    if dup:
        raise Conflict("RELEASE_EXISTS", "Versão já cadastrada para esta plataforma.")
    rel = AppRelease(
        id=uuid.uuid4(),
        platform=platform,
        version=version,
        url=_validate_url(str(body.get("url") or "")),
        sha256=sha,
        size=size,
        signature=sig,
        key_id=key_id,
        mandatory=bool(body.get("mandatory", False)),
        status=AppReleaseStatus.DRAFT.value,
        rollout_percent=0,
        target_groups=None,
        max_concurrent=20,
        notes=(str(body["notes"])[:2000] if body.get("notes") else None),
        created_by_user_id=ctx.user_id,
    )
    db.add(rel)
    await db.flush()
    await audit(db, ctx, "app_release.created", "app_release", rel.id, after=release_public(rel))
    return rel


async def update_release(db: AsyncSession, ctx: AuthContext, release_id: uuid.UUID, body: dict) -> AppRelease:
    rel = await db.get(AppRelease, release_id)
    if rel is None:
        raise NotFound()
    before = release_public(rel)
    if "status" in body:
        st = body["status"]
        if st not in (AppReleaseStatus.PUBLISHED.value, AppReleaseStatus.PAUSED.value, AppReleaseStatus.RETIRED.value):
            raise DomainError("INVALID_STATUS", "Status inválido.", status=422)
        if rel.status == AppReleaseStatus.RETIRED.value:
            raise Conflict("RELEASE_RETIRED", "Versão aposentada não pode ser reativada.")
        rel.status = st
        if st == AppReleaseStatus.PUBLISHED.value and rel.published_at is None:
            rel.published_at = utcnow()
    if "rollout_percent" in body:
        p = body["rollout_percent"]
        if not isinstance(p, int) or isinstance(p, bool) or not (0 <= p <= 100):
            raise DomainError("INVALID_ROLLOUT", "rollout_percent deve estar entre 0 e 100.", status=422)
        rel.rollout_percent = p
    if "target_groups" in body:
        g = body["target_groups"]
        if g is not None and (not isinstance(g, list) or not all(isinstance(x, str) and 0 < len(x) <= 40 for x in g)):
            raise DomainError("INVALID_GROUPS", "target_groups inválido.", status=422)
        rel.target_groups = g or None
    if "max_concurrent" in body:
        c = body["max_concurrent"]
        if not isinstance(c, int) or isinstance(c, bool) or not (1 <= c <= 10000):
            raise DomainError("INVALID_CONCURRENCY", "max_concurrent deve estar entre 1 e 10000.", status=422)
        rel.max_concurrent = c
    if "mandatory" in body:
        rel.mandatory = bool(body["mandatory"])
    await audit(db, ctx, "app_release.updated", "app_release", rel.id, before=before, after=release_public(rel),
                reason=body.get("reason"))
    return rel


def in_wave(rel: AppRelease, device_id: uuid.UUID) -> bool:
    bucket = int(hashlib.sha256(f"{rel.id}:{device_id}".encode()).hexdigest()[:8], 16) % 100
    return bucket < rel.rollout_percent


def in_groups(rel: AppRelease, device: Device) -> bool:
    if not rel.target_groups:
        return True
    return device.update_group is not None and device.update_group in rel.target_groups


async def _take_slot(rel: AppRelease, device_id: uuid.UUID) -> bool:
    r = await get_redis()
    now = int(time.time())
    return bool(
        int(
            await r.eval(
                _SLOT_LUA,
                1,
                rkey("rdpp", "upd", rel.id),
                now,
                str(device_id),
                rel.max_concurrent,
                now + pcfg.UPDATE_SLOT_TTL_S,
                pcfg.UPDATE_SLOT_TTL_S * 2,
            )
        )
    )


async def release_slot(release_id: Any, device_id: Any) -> None:
    r = await get_redis()
    await r.zrem(rkey("rdpp", "upd", release_id), str(device_id))


async def offer_for(db: AsyncSession, device: Device, platform: str, current_version: str) -> tuple[Optional[dict], bool]:
    """Retorna ``(oferta|None, fila_cheia)``."""
    if platform != device.platform:
        raise DomainError("PLATFORM_MISMATCH", "Plataforma diferente da registrada no pareamento.", status=422)
    cur = version_key(current_version)
    rels = (
        await db.execute(
            select(AppRelease).where(AppRelease.platform == platform, AppRelease.status == AppReleaseStatus.PUBLISHED.value)
        )
    ).scalars().all()
    for rel in rels:
        if version_key(rel.version) == cur:
            await release_slot(rel.id, device.id)
    candidates = sorted((r for r in rels if version_key(r.version) > cur), key=lambda r: version_key(r.version), reverse=True)
    queued = False
    for rel in candidates:
        if not in_groups(rel, device) or not in_wave(rel, device.id):
            continue
        if not await _take_slot(rel, device.id):
            queued = True
            continue
        return (
            {
                "version": rel.version,
                "url": rel.url,
                "sha256": rel.sha256,
                "size": rel.size,
                "signature": rel.signature,
                "key_id": rel.key_id,
                "mandatory": rel.mandatory,
            },
            False,
        )
    return None, queued
