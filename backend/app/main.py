"""
RD Mídia Indoor — FastAPI Application (ponto de entrada da API).
"""
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.error_tracking import init_error_tracking
from app.core.logging import configure_logging, request_context_middleware

configure_logging()
init_error_tracking("api")  # no-op sem SENTRY_DSN
log = structlog.get_logger("rd.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", app=settings.APP_NAME, version=settings.APP_VERSION, env=settings.ENVIRONMENT)
    # fail-fast fora de development/test: chave de manifesto, chaves de release, origens do player
    from app.core.startup_checks import enforce_runtime_config

    enforce_runtime_config("api")
    from app.core.redis import init_redis

    await init_redis()
    from app.modules.security.bootstrap import startup_security_check  # AÇÃO 003: instância inicializada?

    await startup_security_check()
    from app.modules.websocket import gateway as ws_gateway  # W-PLAY: gateway WS (Redis Pub/Sub)

    await ws_gateway.startup()
    yield
    await ws_gateway.shutdown()
    from app.core.database import dispose_engines
    from app.core.redis import close_redis

    await close_redis()
    await dispose_engines()
    log.info("shutdown", app=settings.APP_NAME)


_docs = bool(settings.OPENAPI_ENABLED)
app = FastAPI(
    title="RD Mídia Indoor API",
    description="Plataforma de gestão de publicidade em TVs — API REST",
    version=settings.APP_VERSION,
    docs_url="/api/v1/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/api/v1/openapi.json" if _docs else None,
    lifespan=lifespan,
)

install_error_handlers(app)

# ─── MIDDLEWARE ──────────────────────────────────────────────────────────────

app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def impersonation_audit_middleware(request: Request, call_next):
    """Toda ação mutável durante impersonation é auditada (usuário real + impersonado)."""
    response = await call_next(request)
    ctx = getattr(request.state, "auth_ctx", None)
    if ctx is not None and ctx.impersonation is not None and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        from app.core.audit import audit_independent

        try:
            await audit_independent(
                ctx,
                "impersonation.request",
                "http",
                f"{request.method} {request.url.path}"[:255],
                after={"status": response.status_code},
            )
        except Exception as exc:  # falha registrada, não silenciosa
            log.error("impersonation_audit_failed", error_type=type(exc).__name__)
    return response


app.middleware("http")(request_context_middleware)

# CORS restritivo (credenciais só para as origens configuradas)
app.add_middleware(
    CORSMiddleware,
    # Origens EXATAS (sem curinga): painel + Web Player + acréscimos RD_DEV_* só em development/test
    # (contrato em app.core.origins). Lista lida no import: mudar origens exige reiniciar a API.
    allow_origins=settings.all_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization", "Content-Type", "X-Request-ID", "X-RD-CSRF", "X-RD-Context", "Idempotency-Key",
        # Web Player (download parcial de mídia e assinatura de requisições do dispositivo)
        "Range", "If-Range", "If-None-Match", "X-RD-Device-Ts-Ms", "X-RD-Device-Signature",
    ],
    expose_headers=[
        "X-Request-ID", "Retry-After", "X-Server-Time-Ms", "ETag", "Content-Range", "Accept-Ranges", "Content-Length",
    ],
    max_age=600,
)


# ─── HEALTH ──────────────────────────────────────────────────────────────────

@app.get("/health", tags=["infra"])
async def health_check():
    """Healthcheck básico (processo vivo)."""
    return {"status": "healthy", "version": settings.APP_VERSION}


_ALEMBIC_HEADS: set[str] | None = None


def _alembic_heads() -> set[str]:
    """Revisões head do código (migrations/), calculadas uma vez por processo."""
    global _ALEMBIC_HEADS
    if _ALEMBIC_HEADS is None:
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        base = Path(__file__).resolve().parent.parent
        cfg = Config(str(base / "alembic.ini"))
        cfg.set_main_option("script_location", str(base / "migrations"))
        _ALEMBIC_HEADS = set(ScriptDirectory.from_config(cfg).get_heads())
    return _ALEMBIC_HEADS


@app.get("/health/ready", tags=["infra"])
async def readiness_check():
    """Prontidão: banco acessível, Redis acessível e schema na revisão head das migrations.

    200 quando tudo ok; 503 caso contrário (sem expor mensagens de exceção).
    """
    from fastapi.responses import JSONResponse
    from sqlalchemy import text

    from app.core.database import get_engine
    from app.core.redis import get_redis

    checks = {"db": "fail", "redis": "fail", "migrations": "fail"}
    db_revisions: list[str] = []
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
            checks["db"] = "ok"
            db_revisions = sorted(r[0] for r in (await conn.execute(text("SELECT version_num FROM alembic_version"))).all())
    except Exception as exc:
        log.error("ready_db_failed", error_type=type(exc).__name__)
    try:
        expected = sorted(_alembic_heads())
        if db_revisions and db_revisions == expected:
            checks["migrations"] = "ok"
    except Exception as exc:
        expected = []
        log.error("ready_alembic_failed", error_type=type(exc).__name__)
    try:
        r = await get_redis()
        await r.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        log.error("ready_redis_failed", error_type=type(exc).__name__)
    ok = all(v == "ok" for v in checks.values())
    body = {"status": "ready" if ok else "not_ready", "checks": checks, "db_revision": db_revisions,
            "expected_revision": expected}
    return JSONResponse(status_code=200 if ok else 503, content=body)


@app.get("/health/full", tags=["infra"])
async def full_health_check():
    """Healthcheck com DB e Redis (sem expor mensagens de exceção)."""
    from sqlalchemy import text

    from app.core.database import get_engine
    from app.core.redis import get_redis

    results = {"app": "healthy", "db": "unhealthy", "redis": "unhealthy"}
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        results["db"] = "healthy"
    except Exception as exc:
        log.error("health_db_failed", error_type=type(exc).__name__)
    try:
        r = await get_redis()
        await r.ping()
        results["redis"] = "healthy"
    except Exception as exc:
        log.error("health_redis_failed", error_type=type(exc).__name__)
    overall = "healthy" if all(v == "healthy" for v in results.values()) else "degraded"
    return {"status": overall, "services": results}


@app.get("/metrics", tags=["infra"])
async def prometheus_metrics():
    """Métricas Prometheus baratas (W-PLAY): contadores globais no Redis; nenhuma
    consulta pesada por scrape (contagens de domínio no máximo 1×/min no cluster)."""
    from fastapi.responses import Response

    from app.modules.websocket.metrics import render_metrics

    return Response(content=await render_metrics(), media_type="text/plain; version=0.0.4")


# ─── ROUTERS ─────────────────────────────────────────────────────────────────

from app.api.v1.router import api_router  # noqa: E402

app.include_router(api_router, prefix="/api/v1")

from app.modules.websocket.routes import ws_router  # noqa: E402

app.include_router(ws_router, prefix="/ws")
