"""
RD Mídia Indoor — Envelope de erro (contrato §1) e handlers globais.

Todas as respostas de erro têm o formato::

    {"error": {"code": "CODIGO", "message": "Mensagem pt-BR.", "details": {}}}

Uso nos domínios::

    from app.core.errors import DomainError, NotFound, Forbidden, Conflict
    raise DomainError("CAMPAIGN_INVALID_TRANSITION", "Transição inválida.", status=400,
                      details={"from": "DRAFT", "to": "ACTIVE"})
    raise NotFound()                 # 404 (inexistente OU fora do escopo)
    raise Forbidden()                # 403 (sem permissão)

Regras: 401 para credencial ausente/inválida (nunca 403); 404 para recurso fora do
escopo; 422 para schema inválido (sem ecoar valores enviados); 5xx sem stack trace.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger("rd.errors")

DEFAULT_CODES: dict[int, tuple[str, str]] = {
    400: ("BAD_REQUEST", "Requisição inválida."),
    401: ("UNAUTHENTICATED", "Autenticação necessária."),
    403: ("FORBIDDEN", "Você não tem permissão para esta ação."),
    404: ("NOT_FOUND", "Recurso não encontrado."),
    405: ("METHOD_NOT_ALLOWED", "Método não permitido."),
    409: ("CONFLICT", "Conflito com o estado atual do recurso."),
    413: ("PAYLOAD_TOO_LARGE", "Conteúdo muito grande."),
    415: ("UNSUPPORTED_MEDIA_TYPE", "Tipo de conteúdo não suportado."),
    422: ("VALIDATION_ERROR", "Dados inválidos."),
    423: ("LOCKED", "Recurso bloqueado temporariamente."),
    429: ("RATE_LIMITED", "Muitas requisições. Tente novamente em instantes."),
    500: ("INTERNAL_ERROR", "Erro interno. Tente novamente mais tarde."),
    503: ("SERVICE_UNAVAILABLE", "Serviço temporariamente indisponível, tente em instantes."),
}


def error_body(code: str, message: str, details: Optional[Mapping[str, Any]] = None) -> dict:
    return {"error": {"code": code, "message": message, "details": dict(details or {})}}


def error_response(
    status: int,
    code: str,
    message: str,
    details: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message, details), headers=dict(headers or {}))


class DomainError(Exception):
    """Erro de negócio com código estável (convertido no envelope do contrato)."""

    status: int = 400
    code: str = "BAD_REQUEST"
    message: str = "Requisição inválida."

    def __init__(
        self,
        code: Optional[str] = None,
        message: Optional[str] = None,
        status: Optional[int] = None,
        details: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.code = code or self.code
        self.message = message or self.message
        self.status = status or self.status
        self.details = dict(details or {})
        self.headers = dict(headers or {})
        super().__init__(f"{self.code}: {self.message}")


class BadRequest(DomainError):
    status, code, message = 400, "BAD_REQUEST", "Requisição inválida."


class Unauthorized(DomainError):
    status, code, message = 401, "UNAUTHENTICATED", "Autenticação necessária."

    def __init__(self, code: Optional[str] = None, message: Optional[str] = None, **kw):
        kw.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(code, message, **kw)


class Forbidden(DomainError):
    status, code, message = 403, "FORBIDDEN", "Você não tem permissão para esta ação."


class NotFound(DomainError):
    status, code, message = 404, "NOT_FOUND", "Recurso não encontrado."


class Conflict(DomainError):
    status, code, message = 409, "CONFLICT", "Conflito com o estado atual do recurso."


class Locked(DomainError):
    status, code, message = 423, "LOCKED", "Recurso bloqueado temporariamente."


class RateLimited(DomainError):
    status, code, message = 429, "RATE_LIMITED", "Muitas requisições. Tente novamente em instantes."


SERVICE_UNAVAILABLE_RETRY_AFTER_S = 5


class ServiceUnavailable(DomainError):
    """Dependência de infraestrutura indisponível (ex.: Redis). Nunca confundir com credencial."""

    status, code, message = 503, "SERVICE_UNAVAILABLE", "Serviço temporariamente indisponível, tente em instantes."

    def __init__(self, code: Optional[str] = None, message: Optional[str] = None, **kw):
        kw.setdefault("headers", {"Retry-After": str(SERVICE_UNAVAILABLE_RETRY_AFTER_S)})
        kw.setdefault("details", {"retry_after_s": SERVICE_UNAVAILABLE_RETRY_AFTER_S})
        super().__init__(code, message, **kw)


def _sanitize_validation_errors(exc: RequestValidationError) -> list[dict]:
    out = []
    for err in exc.errors():
        # Nunca ecoar o valor enviado ("input"/"ctx") — pode conter senha/token.
        out.append(
            {
                "loc": [str(p) for p in err.get("loc", ())],
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):  # noqa: ANN202
        return error_response(exc.status, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):  # noqa: ANN202
        code, message = DEFAULT_CODES.get(exc.status_code, ("HTTP_ERROR", "Erro na requisição."))
        details: dict = {}
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code", code))
            message = str(detail.get("message", message))
            details = dict(detail.get("details", {}) or {})
        elif isinstance(detail, str) and detail and exc.status_code < 500:
            # mensagens legadas dos handlers (pt-BR) são mantidas
            if detail not in ("Not Found", "Method Not Allowed", "Not authenticated", "Forbidden"):
                message = detail
        return error_response(exc.status_code, code, message, details, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):  # noqa: ANN202
        return error_response(422, "VALIDATION_ERROR", "Dados inválidos.", {"errors": _sanitize_validation_errors(exc)})

    import redis.exceptions as _rexc

    async def _redis_unavailable(request: Request, exc: Exception):  # noqa: ANN202
        from app.core.redis import mark_redis_down

        mark_redis_down()
        log.error("redis_unavailable", error_type=type(exc).__name__, path=request.url.path)
        e = ServiceUnavailable()
        return error_response(e.status, e.code, e.message, e.details, e.headers)

    app.add_exception_handler(_rexc.ConnectionError, _redis_unavailable)
    app.add_exception_handler(_rexc.TimeoutError, _redis_unavailable)

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError):  # noqa: ANN202
        orig = getattr(exc, "orig", None)
        constraint = getattr(getattr(orig, "__cause__", None), "constraint_name", None) or getattr(
            getattr(orig, "diag", None), "constraint_name", None
        )
        log.warning("integrity_error", constraint=constraint)
        return error_response(409, "CONFLICT", "Conflito de dados (registro duplicado ou referência inválida).")

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError):  # noqa: ANN202
        # Não devolver str(exc) (pode conter detalhes internos) — A7 B4.
        log.warning("value_error", error_type=type(exc).__name__)
        return error_response(400, "BAD_REQUEST", "Requisição inválida.")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # noqa: ANN202
        log.error("unhandled_exception", error_type=type(exc).__name__, exc_info=exc)
        return error_response(500, "INTERNAL_ERROR", "Erro interno. Tente novamente mais tarde.")
