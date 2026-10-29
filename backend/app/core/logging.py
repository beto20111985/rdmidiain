"""
RD Mídia Indoor — Logging estruturado (structlog) com request_id.

- ``configure_logging()``: JSON em produção, console legível em dev/test.
- Processador ``redact`` remove valores de chaves sensíveis (senha, token,
  segredo, cookie, authorization, código TOTP/reset...).
- ``request_context_middleware``: lê/gera ``X-Request-ID`` (validado), associa ao
  contexto (``structlog.contextvars``) e devolve no header da resposta. Loga apenas
  o *path* — nunca a query string.
- ``QueryRedactionFilter`` nos loggers ``uvicorn``, ``uvicorn.error`` (handshake WebSocket:
  ``"WebSocket /ws/player?ticket=…" [accepted]``) e ``uvicorn.access``: valores de parâmetros
  sensíveis da query (ticket, token, access_token, refresh_token, code, signature, key...) viram
  ``[MASCARADO]`` — método, rota, demais parâmetros e status são preservados (REQ-SEC-002/OBS-004).
- ``current_request_meta()``: request_id/ip/user_agent da request corrente (usado
  pela auditoria).
"""
from __future__ import annotations

import contextvars
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import structlog

from app.core.config import settings

SENSITIVE_KEY_RE = re.compile(
    r"(pass(word)?|senha|secret|token|authorization|cookie|api[_-]?key|otp|totp|mfa_code|"
    r"recovery|signature|private|credential|code_hash|hashed)",
    re.IGNORECASE,
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


@dataclass
class RequestMeta:
    request_id: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    path: Optional[str] = None
    method: Optional[str] = None


_request_meta: contextvars.ContextVar[RequestMeta] = contextvars.ContextVar("rd_request_meta", default=RequestMeta())


def current_request_meta() -> RequestMeta:
    return _request_meta.get()


def set_request_meta(meta: RequestMeta) -> contextvars.Token:
    return _request_meta.set(meta)


def redact(_, __, event_dict: dict) -> dict:
    for key in list(event_dict.keys()):
        if key in ("event", "level", "timestamp", "logger"):
            continue
        if SENSITIVE_KEY_RE.search(key):
            event_dict[key] = "[REDACTED]"
    return event_dict


MASK = "[MASCARADO]"
SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "ticket", "token", "access_token", "refresh_token", "id_token", "mfa_token", "code", "signature", "sig",
        "key", "api_key", "apikey", "password", "secret", "client_secret", "jwt", "auth", "otp",
    }
)
# parâmetro de query: separador (?, & ou ;) + nome + '=' + valor (até &, #, espaço, aspas)
_QUERY_PARAM_RE = re.compile(r"(?P<sep>[?&;])(?P<name>[^=&#\s\"'?;]+)=(?P<value>[^&#\s\"';]*)")


def is_sensitive_query_param(name: str) -> bool:
    n = name.strip().lower()
    return n in SENSITIVE_QUERY_PARAMS or n.endswith("token") or n.endswith("_secret") or n.endswith("_signature")


def redact_query_values(text: str) -> str:
    """``/ws/player?ticket=abc&x=1`` → ``/ws/player?ticket=[MASCARADO]&x=1``."""
    if not text or "=" not in text:
        return text

    def _sub(m: re.Match) -> str:
        if m.group("value") and is_sensitive_query_param(m.group("name")):
            return f"{m.group('sep')}{m.group('name')}={MASK}"
        return m.group(0)

    return _QUERY_PARAM_RE.sub(_sub, text)


class QueryRedactionFilter(logging.Filter):
    """Redige valores sensíveis de query string em mensagens/args (uvicorn access/error/WS)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_query_values(record.msg)
            if isinstance(record.args, tuple) and record.args:
                record.args = tuple(redact_query_values(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict) and record.args:
                record.args = {k: redact_query_values(v) if isinstance(v, str) else v for k, v in record.args.items()}
        except Exception:  # um filtro de log nunca pode derrubar a requisição
            pass
        return True


REDACTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def install_query_redaction() -> None:
    """Idempotente: instala o filtro nos loggers do uvicorn."""
    for name in REDACTED_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, QueryRedactionFilter) for f in lg.filters):
            lg.addFilter(QueryRedactionFilter())


_configured = False


def configure_logging() -> None:
    global _configured
    install_query_redaction()  # sempre (o uvicorn pode reconfigurar seus loggers no reload)
    if _configured:
        return
    _configured = True
    level = logging.DEBUG if settings.DEBUG else logging.INFO
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")
    # SQLAlchemy nunca em nível que registre parâmetros
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    renderer = structlog.processors.JSONRenderer() if settings.LOG_JSON else structlog.dev.ConsoleRenderer(colors=False)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


async def request_context_middleware(request, call_next):
    """Middleware HTTP: request_id + log de início/fim sem query string."""
    from app.core.net import client_ip

    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if _REQUEST_ID_RE.match(incoming or "") else uuid.uuid4().hex
    meta = RequestMeta(
        request_id=request_id,
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:500] or None,
        path=request.url.path,
        method=request.method,
    )
    token = set_request_meta(meta)
    request.state.request_id = request_id
    log = structlog.get_logger("rd.http")
    start = time.perf_counter()
    try:
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            response = await call_next(request)
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
            response.headers["X-Request-ID"] = request_id
            return response
    finally:
        _request_meta.reset(token)


def bind_task_context(**kw: Any) -> None:
    """Para workers: associa task_id/origem ao contexto de log."""
    structlog.contextvars.bind_contextvars(**kw)
