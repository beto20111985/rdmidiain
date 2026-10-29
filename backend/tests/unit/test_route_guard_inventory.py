"""AÇÃO 003 — inventário de rotas do guard do primeiro acesso (ADR_011 D6; SPEC §3.2).

Toda rota ``/api/v1/*`` precisa ter ``get_auth_context`` (direto, via ``require()`` ou shims legados) ou
``get_device_context`` na árvore de dependências — o guard (``authz._check_unlocked``) nega tudo enquanto
houver passo bloqueante — OU estar numa lista EXPLÍCITA abaixo. Rota nova fora das listas = falha.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.core.authz import (
    get_auth_context,
    get_auth_context_lenient,
    get_device_context,
    get_mfa_setup_context,
    get_setup_status_context,
)

# Sem credencial de usuário por design (login, recuperação, cadastros públicos, player, webhooks, marca…).
PUBLIC_ROUTES = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/mfa/verify"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/password/forgot"),
    ("POST", "/api/v1/auth/password/reset"),
    ("POST", "/api/v1/auth/invites/accept"),
    ("POST", "/api/v1/auth/master-activation/inspect"),
    ("POST", "/api/v1/auth/master-activation/totp"),
    ("POST", "/api/v1/auth/master-activation/complete"),
    ("POST", "/api/v1/onboarding/public/signup"),
    ("GET", "/api/v1/onboarding/public/application"),
    ("PUT", "/api/v1/onboarding/public/application/steps/{step}"),
    ("POST", "/api/v1/onboarding/public/application/documents"),
    ("POST", "/api/v1/onboarding/public/application/submit"),
    ("GET", "/api/v1/advertiser-signup/terms"),
    ("GET", "/api/v1/advertiser-signup/options"),
    ("POST", "/api/v1/advertiser-signup/start"),
    ("GET", "/api/v1/advertiser-signup/application"),
    ("POST", "/api/v1/advertiser-signup/complete"),
    ("POST", "/api/v1/player/pairing/requests"),
    ("GET", "/api/v1/player/pairing/requests/{request_id}"),
    ("POST", "/api/v1/player/pairing/requests/{request_id}/claim"),
    ("GET", "/api/v1/player/pairing/requests/{request_id}/result"),
    ("POST", "/api/v1/player/auth/challenge"),
    ("POST", "/api/v1/player/auth/token"),
    ("POST", "/api/v1/webhooks/cloudpaypag"),
    ("POST", "/api/v1/webhooks/cloudpaypag/{instance_slug}"),
    ("GET", "/api/v1/webhooks/cloudpaypag/health"),
    ("GET", "/api/v1/reports/proofs/{code}/verification"),
    ("POST", "/api/v1/reports/proofs/verify"),
    ("POST", "/api/v1/control-plane/heartbeat"),
    ("GET", "/api/v1/instance/branding"),
    ("GET", "/api/v1/instance/logo"),
    ("GET", "/api/v1/instance/signing-keys"),
}

# Autenticadas SEM o guard (liberadas durante passos bloqueantes) — precisam de get_auth_context_lenient.
LENIENT_ROUTES = {
    ("POST", "/api/v1/auth/logout-all"),
    ("GET", "/api/v1/auth/sessions"),
    ("DELETE", "/api/v1/auth/sessions/{session_id}"),
    ("POST", "/api/v1/auth/password/change"),
    ("POST", "/api/v1/auth/mfa/totp/disable"),
    ("DELETE", "/api/v1/auth/impersonation"),
    ("GET", "/api/v1/me"),
    ("PATCH", "/api/v1/me"),
    ("GET", "/api/v1/me/consents"),
    ("POST", "/api/v1/me/consents"),
    ("GET", "/api/v1/me/data-export"),
    ("POST", "/api/v1/me/deletion-request"),
}

# Rotas dos passos do primeiro acesso (dependências próprias que impõem a ordem no servidor).
SETUP_ROUTES = {
    ("GET", "/api/v1/auth/setup/status"),
    ("POST", "/api/v1/auth/setup/password"),
    ("POST", "/api/v1/auth/mfa/totp/setup"),
    ("POST", "/api/v1/auth/mfa/totp/enable"),
    ("POST", "/api/v1/auth/setup/financial-password"),
}


def _calls(dep, acc: set) -> set:
    for d in dep.dependencies:
        acc.add(d.call)
        _calls(d, acc)
    return acc


def _routes():
    from app.main import app

    out = {}
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api/v1"):
            for m in r.methods - {"HEAD"}:
                out[(m, r.path)] = _calls(r.dependant, set())
    return out


def test_every_api_route_is_guarded_or_explicitly_listed():
    routes = _routes()
    unlisted = []
    for key, calls in sorted(routes.items()):
        if calls & {get_auth_context, get_device_context}:
            assert key not in PUBLIC_ROUTES | LENIENT_ROUTES, f"{key} tem guard mas está numa lista de exceção"
            continue
        if key not in PUBLIC_ROUTES | LENIENT_ROUTES | SETUP_ROUTES:
            unlisted.append(key)
    assert not unlisted, f"rotas fora do guard e fora das listas explícitas: {unlisted}"


def test_exception_lists_point_to_existing_routes_with_expected_dependency():
    routes = _routes()
    for key in PUBLIC_ROUTES | LENIENT_ROUTES | SETUP_ROUTES:
        assert key in routes, f"rota listada não existe mais: {key}"
    for key in LENIENT_ROUTES:
        assert get_auth_context_lenient in routes[key], key
    for key in SETUP_ROUTES:
        calls = routes[key]
        assert calls & {get_setup_status_context, get_mfa_setup_context} or any(
            getattr(c, "__rd_setup_step__", None) for c in calls), key
    for key in PUBLIC_ROUTES:
        assert get_auth_context_lenient not in routes[key], f"{key}: rota pública não deve usar sessão lenient"


@pytest.mark.parametrize("path", ["/api/v1/realtime/ticket", "/api/v1/security/masters", "/api/v1/security/integrity",
                                  "/api/v1/security/approval-mode", "/api/v1/financial/approvals"])
def test_sensitive_routes_are_behind_the_guard(path):
    routes = _routes()
    keys = [k for k in routes if k[1] == path]
    assert keys, path
    for k in keys:
        assert get_auth_context in routes[k], k
