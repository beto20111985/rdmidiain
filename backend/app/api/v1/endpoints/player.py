"""
RD Mídia Indoor — API do player (RDPP/1), prefixo ``/api/v1/player``.

Pareamento (sem autenticação; rate limit por IP), desafio/token (prova de posse da chave
ES256), e rotas com ``Authorization: Bearer <JWT typ=device>``: manifesto (ETag/304),
mídia (Range/If-Range; somente mídia de manifesto recente da própria tela), âncora,
ticket de WS, eventos PoP em lote e atualização do app.

TODA resposta (inclusive erros) traz ``X-Server-Time-Ms`` (rota customizada abaixo).
Nenhum segredo/token em URL (exceção: ``ticket`` de WS de uso único — rota WS).
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core import ratelimit
from app.core.authz import AuthContext, get_device_context
from app.core.database import get_db
from app.core.errors import DEFAULT_CODES, DomainError, NotFound, _sanitize_validation_errors, error_response
from app.core.net import client_ip
from app.core.redis import get_redis, rkey
from app.modules.playback import config as pcfg
from app.modules.playback import identity
from app.modules.playback import ingest
from app.modules.playback import integration as integ
from app.modules.playback import updates

log = structlog.get_logger("rd.player.api")


def _now_ms() -> int:
    return int(time.time() * 1000)


class PlayerRoute(APIRoute):
    """Garante ``X-Server-Time-Ms`` e o envelope de erro em TODAS as respostas do player."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except DomainError as exc:
                response = error_response(exc.status, exc.code, exc.message, exc.details, exc.headers)
            except RequestValidationError as exc:
                response = error_response(422, "VALIDATION_ERROR", "Dados inválidos.",
                                          {"errors": _sanitize_validation_errors(exc)})
            except StarletteHTTPException as exc:
                code, message = DEFAULT_CODES.get(exc.status_code, ("HTTP_ERROR", "Erro na requisição."))
                response = error_response(exc.status_code, code, message, None, getattr(exc, "headers", None))
            except Exception as exc:  # noqa: BLE001 — 500 no envelope, sem stack trace
                log.error("player_unhandled", error_type=type(exc).__name__, exc_info=exc)
                response = error_response(500, "INTERNAL_ERROR", "Erro interno. Tente novamente mais tarde.")
            response.headers["X-Server-Time-Ms"] = str(_now_ms())
            return response

        return handler


router = APIRouter(route_class=PlayerRoute)


# ─── Schemas ─────────────────────────────────────────────────────────────────

class PairingRequestIn(BaseModel):
    public_key: str = Field(..., max_length=400)
    key_alg: str = Field(..., max_length=10)
    platform: str = Field(..., max_length=20)
    app_version: Optional[str] = Field(None, max_length=40)
    device_info: Optional[dict] = None


class ClaimIn(BaseModel):
    login: str = Field(..., max_length=255)
    password: str = Field(..., max_length=256)
    totp: Optional[str] = Field(None, max_length=20)
    screen_code: str = Field(..., max_length=30)


class ChallengeIn(BaseModel):
    device_id: uuid.UUID


class TokenIn(BaseModel):
    device_id: uuid.UUID
    challenge: str = Field(..., max_length=64)
    signature: str = Field(..., max_length=100)


class AnchorIn(BaseModel):
    anchor_ms: int


# ─── Pareamento ──────────────────────────────────────────────────────────────

@router.post("/pairing/requests", status_code=201)
async def create_pairing_request(body: PairingRequestIn, request: Request, db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce("pair_req_ip", client_ip(request), limit=10, window_s=60)
    req = await identity.create_pairing_request(
        db,
        public_key=body.public_key,
        key_alg=body.key_alg,
        platform=body.platform,
        app_version=body.app_version,
        device_info=body.device_info,
    )
    return {
        "pairing_request_id": str(req.id),
        "pairing_code": req.pairing_code,
        "expires_at_ms": identity.ms(req.expires_at),
        "panel_pair_url": pcfg.panel_pair_url(req.pairing_code),
        "ws_path": "/ws/player/pairing",
    }


@router.get("/pairing/requests/{request_id}")
async def pairing_request_status(request_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce("pair_status_ip", client_ip(request), limit=60, window_s=60)
    req = await identity.get_request(db, request_id)
    return {"status": identity.effective_status(req), "expires_at_ms": identity.ms(req.expires_at)}


@router.post("/pairing/requests/{request_id}/claim")
async def claim_on_device(request_id: str, body: ClaimIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Login controlado do técnico no próprio dispositivo (``screens.pair`` no escopo da tela)."""
    outcome = await identity.claim_by_device_login(
        db,
        request_id,
        login=body.login,
        password=body.password,
        totp_code=body.totp,
        screen_code=body.screen_code,
        ip=client_ip(request),
    )
    await db.commit()
    await identity.after_claim(outcome)
    return {"status": "CLAIMED", "screen_id": str(outcome.screen_id), "device_id": str(outcome.device.id)}


@router.get("/pairing/requests/{request_id}/result")
async def pairing_result(request_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce("pair_result_ip", client_ip(request), limit=60, window_s=60)
    return await identity.pairing_result(
        db, request_id, request.headers.get("x-rd-device-ts-ms"), request.headers.get("x-rd-device-signature")
    )


# ─── Autenticação ────────────────────────────────────────────────────────────

@router.post("/auth/challenge")
async def auth_challenge(body: ChallengeIn, request: Request):
    return await identity.create_challenge(body.device_id, client_ip(request))


@router.post("/auth/token")
async def auth_token(body: TokenIn, request: Request, db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce("dev_token_ip", client_ip(request), limit=120, window_s=60)
    return await identity.issue_token(db, body.device_id, body.challenge, body.signature)


# ─── Manifesto e mídia ───────────────────────────────────────────────────────

def _k_media(screen_id: Any) -> str:
    return rkey("rdpp", "media", screen_id)


def _k_mref(media_id: Any) -> str:
    return rkey("rdpp", "mref", media_id)


async def remember_manifest_media(screen_id: Any, manifest: dict) -> list[str]:
    """Autoriza (por 8 dias) as mídias do manifesto servido à tela e registra a referência."""
    media_ids = [str(m.get("media_id")) for m in (manifest.get("media") or {}).values() if isinstance(m, dict) and m.get("media_id")]
    if not media_ids:
        return []
    r = await get_redis()
    pipe = r.pipeline(transaction=False)
    pipe.sadd(_k_media(screen_id), *media_ids)
    pipe.expire(_k_media(screen_id), pcfg.MEDIA_AUTH_TTL_S)
    for mid in media_ids:
        pipe.sadd(_k_mref(mid), str(screen_id))
        pipe.expire(_k_mref(mid), pcfg.MEDIA_AUTH_TTL_S)
    await pipe.execute()
    return media_ids


async def media_referencer(db: AsyncSession, media_ids) -> dict:
    """Referenciador para o W-MEDIA: nº de telas cujo manifesto recente (≤ 8 dias) usa a mídia."""
    r = await get_redis()
    ids = list(media_ids)
    pipe = r.pipeline(transaction=False)
    for mid in ids:
        pipe.scard(_k_mref(mid))
    counts = await pipe.execute()
    return {mid: int(c) for mid, c in zip(ids, counts)}


integ.register_media_referencer("player_manifests", media_referencer)


@router.get("/manifest")
async def get_manifest(request: Request, ctx: AuthContext = Depends(get_device_context), db: AsyncSession = Depends(get_db)):
    sm = await integ.current_manifest(db, ctx.screen_id)
    version = integ.version_of(sm)
    etag = f'"{version}"'
    await remember_manifest_media(ctx.screen_id, integ.manifest_of(sm))
    inm = request.headers.get("if-none-match")
    if inm and etag in [x.strip() for x in inm.split(",")]:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    from app.modules.playback import syncevents

    await syncevents.note_received(ctx.device_id, version, _now_ms())
    await syncevents.record(ctx.screen_id, ctx.device_id, "MANIFEST_RECEIVED", db=db, manifest_version=version)
    return JSONResponse(integ.envelope_of(sm), headers={"ETag": etag, "Cache-Control": "no-cache"})


@router.get("/media/{media_id}")
async def get_media(
    media_id: str, request: Request, ctx: AuthContext = Depends(get_device_context), db: AsyncSession = Depends(get_db)
):
    try:
        mid = uuid.UUID(media_id)
    except ValueError:
        raise NotFound() from None
    r = await get_redis()
    allowed = bool(await r.sismember(_k_media(ctx.screen_id), str(mid)))
    if not allowed:
        sm = await integ.current_manifest(db, ctx.screen_id)
        allowed = str(mid) in await remember_manifest_media(ctx.screen_id, integ.manifest_of(sm))
    if not allowed:
        raise NotFound()  # mídia de outra tela/organização: não vazar existência
    res = await integ.open_media_range(db, mid, request.headers.get("range"), request.headers.get("if-range"))
    return res.to_response()


@router.post("/anchor")
async def post_anchor(body: AnchorIn, ctx: AuthContext = Depends(get_device_context), db: AsyncSession = Depends(get_db)):
    a = body.anchor_ms
    now = _now_ms()
    if a % 1000 != 0 or a > now + pcfg.SIGNED_TS_SKEW_MS or a < now - 400 * 86_400_000:
        raise DomainError("INVALID_ANCHOR", "Âncora inválida (ms inteiros, múltiplo de 1000, não futura).", status=422)
    stored = await integ.set_anchor(db, ctx.screen_id, a, ctx.device_id)
    return {"anchor_ms": stored}


# ─── WS / eventos / atualização ──────────────────────────────────────────────

@router.post("/ws-ticket")
async def ws_ticket(ctx: AuthContext = Depends(get_device_context)):
    return await identity.create_ws_ticket(ctx)


@router.post("/events:batch")
async def events_batch(request: Request, ctx: AuthContext = Depends(get_device_context), db: AsyncSession = Depends(get_db)):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > 1_000_000:
        raise DomainError("PAYLOAD_TOO_LARGE", "Lote muito grande.", status=413)
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise DomainError("PAYLOAD_TOO_LARGE", "Lote muito grande.", status=413)
    try:
        body = json.loads(raw)
    except ValueError:
        raise DomainError("VALIDATION_ERROR", "JSON inválido.", status=422) from None
    items = body.get("events") if isinstance(body, dict) else None
    if not isinstance(items, list) or not items:
        raise DomainError("VALIDATION_ERROR", "Informe 'events' (lista não vazia).", status=422)
    if len(items) > pcfg.POP_BATCH_MAX:
        raise DomainError("BATCH_TOO_LARGE", f"Máximo de {pcfg.POP_BATCH_MAX} eventos por lote.", status=413)
    outcome = await ingest.ingest_batch(db, ctx.device_id, items)
    return outcome.public()


@router.get("/app-update")
async def app_update(
    request: Request,
    platform: str = Query(..., max_length=20),
    version: str = Query(..., max_length=40),
    ctx: AuthContext = Depends(get_device_context),
    db: AsyncSession = Depends(get_db),
):
    dev = getattr(request.state, "device", None)
    if dev is None:
        from app.models.devices import Device

        dev = await db.get(Device, ctx.device_id)
    offer, queued = await updates.offer_for(db, dev, platform, version)
    if offer is None:
        headers = {"Retry-After": "300"} if queued else {}
        return Response(status_code=204, headers=headers)
    return offer
