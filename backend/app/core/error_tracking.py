"""
RD Mídia Indoor — Rastreamento de erros (Sentry), OPCIONAL.

- Só liga quando ``SENTRY_DSN`` está configurado; sem DSN é no-op (estado padrão local).
- API (FastAPI/Starlette) e Celery são instrumentados pelas integrações automáticas do SDK.
- Privacidade (REQ-SEC-002/OBS-004): ``send_default_pii=False``; ``before_send`` remove
  cookies, cabeçalhos de credencial e corpo da requisição, e mascara valores sensíveis de
  query string (mesma regra dos logs: ``redact_query_values``) também nos breadcrumbs.
- Sem tracing por padrão (``traces_sample_rate=0``): erros apenas.
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.config import settings
from app.core.logging import MASK, redact_query_values

_SENSITIVE_HEADERS = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-rd-csrf",
    "x-rd-signup-token",
    "x-rd-onboarding-token",
    "x-cloudpaypag-signature",
    "proxy-authorization",
})

_initialized: Optional[str] = None


def _scrub_request(request: dict[str, Any]) -> None:
    request.pop("cookies", None)
    if "data" in request:
        request["data"] = MASK  # corpo pode conter senha, TOTP, código por e-mail, dados pessoais
    headers = request.get("headers")
    if isinstance(headers, dict):
        for name in list(headers):
            if name.lower() in _SENSITIVE_HEADERS:
                headers[name] = MASK
    qs = request.get("query_string")
    if isinstance(qs, str) and qs:
        request["query_string"] = redact_query_values("?" + qs)[1:]
    url = request.get("url")
    if isinstance(url, str):
        request["url"] = redact_query_values(url)


def scrub_event(event: dict[str, Any], _hint: Any = None) -> dict[str, Any]:
    """``before_send``/``before_send_transaction``: remove credenciais e PII do evento."""
    request = event.get("request")
    if isinstance(request, dict):
        _scrub_request(request)
    user = event.get("user")
    if isinstance(user, dict):
        # só o identificador interno; nada de e-mail/IP/nome
        event["user"] = {k: v for k, v in user.items() if k == "id"}
    crumbs = event.get("breadcrumbs")
    values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    if isinstance(values, list):
        for crumb in values:
            if not isinstance(crumb, dict):
                continue
            if isinstance(crumb.get("message"), str):
                crumb["message"] = redact_query_values(crumb["message"])
            data = crumb.get("data")
            if isinstance(data, dict) and isinstance(data.get("url"), str):
                data["url"] = redact_query_values(data["url"])
    return event


def init_error_tracking(component: str) -> bool:
    """Inicializa o Sentry uma vez por processo. Devolve ``True`` se ligado."""
    global _initialized
    dsn = (settings.SENTRY_DSN or "").strip()
    if not dsn:
        return False
    if _initialized is not None:
        return True
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=str(getattr(settings.ENVIRONMENT, "value", settings.ENVIRONMENT)),
        release=f"rdmidiaindoor@{settings.APP_VERSION}",
        send_default_pii=False,
        traces_sample_rate=0.0,
        max_request_body_size="never",
        before_send=scrub_event,
        before_send_transaction=scrub_event,
    )
    sentry_sdk.set_tag("component", component)
    _initialized = component
    return True
