"""AÇÃO 003 (agente F) — host/origem externo de DESENVOLVIMENTO: autorizado × não autorizado.

Processo uvicorn REAL configurado só por variáveis de ambiente — exatamente como o compose faz com
``ops/dev/dev-origins.env`` — cobrindo CORS (preflight e simples), CSRF (refresh/logout), sessão
completa login → refresh → /me → logout com ``Host``/``Origin`` do túnel, cookie de refresh
(atributos inalterados) e ``Origin`` dos WebSockets do painel e do player.

Domínios de túnel FICTÍCIOS (nenhum domínio real de conta em código)."""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from tests.factories import login_as, make_user
from tests.integration.test_ws_gateway import UA, _env, spawn  # noqa: F401  (spawn = fixture)

pytestmark = pytest.mark.slow

TUNNEL_HOST = "rd-teste-tunel.ngrok-free.dev"
TUNNEL = f"https://{TUNNEL_HOST}"
TUNNEL_PLAYER = "https://rd-teste-player.ngrok-free.dev"
EVIL = "https://malicioso.example.com"
DEV_ENV = {
    "RD_DEV_ALLOWED_ORIGINS": f"http://localhost,http://127.0.0.1,{TUNNEL}",
    "RD_DEV_PLAYER_ALLOWED_ORIGINS": f"http://localhost:8088,{TUNNEL_PLAYER}",
}
NO_DEV_ENV = {"RD_DEV_ALLOWED_ORIGINS": "", "RD_DEV_PLAYER_ALLOWED_ORIGINS": ""}
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"


def _preflight(origin: str) -> dict:
    return {"Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-rd-csrf"}


async def _rejected(url: str, origin: str) -> None:
    with pytest.raises((InvalidStatus, ConnectionClosed)):
        w = await ws_connect(url, additional_headers={"Origin": origin}, open_timeout=10)
        await asyncio.wait_for(w.recv(), 5)


async def test_cors_allows_only_configured_origins(spawn):
    a = await spawn(DEV_ENV)
    async with a.client() as c:
        cases = [("http://localhost", True), ("http://127.0.0.1", True), (TUNNEL, True), (TUNNEL_PLAYER, True),
                 (EVIL, False), ("null", False), (TUNNEL.replace("https://", "http://"), False),
                 ("https://outro.ngrok-free.dev", False)]
        for origin, ok in cases:
            r = await c.options("/api/v1/auth/login", headers=_preflight(origin))
            acao = r.headers.get("access-control-allow-origin")
            if ok:
                assert r.status_code == 200 and acao == origin, (origin, r.status_code, acao)
                assert r.headers.get("access-control-allow-credentials") == "true"
            else:
                assert r.status_code == 400 and acao is None, (origin, r.status_code, acao)
            r = await c.get("/health", headers={"Origin": origin})
            assert r.status_code == 200
            assert r.headers.get("access-control-allow-origin") == (origin if ok else None), origin
            assert r.headers.get("access-control-allow-origin") != "*"


async def test_without_dev_config_tunnel_is_refused(spawn, db):
    """Sem RD_DEV_* (estado anterior à AÇÃO 003): o túnel NÃO é aceito — nada é implícito."""
    a = await spawn(NO_DEV_ENV)
    async with a.client() as c:
        r = await c.options("/api/v1/auth/login", headers=_preflight(TUNNEL))
        assert r.headers.get("access-control-allow-origin") is None
        r = await c.post(REFRESH, headers={"X-RD-CSRF": "1", "Origin": TUNNEL})
        assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"


async def test_csrf_refresh_logout_by_origin_without_cookie(spawn):
    a = await spawn(DEV_ENV)
    async with a.client() as c:
        for origin in ("http://localhost", TUNNEL):
            r = await c.post(REFRESH, headers={"X-RD-CSRF": "1", "Origin": origin})
            assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_REFRESH", (origin, r.text)
            assert (await c.post(LOGOUT, headers={"X-RD-CSRF": "1", "Origin": origin})).status_code == 204
        for origin in (EVIL, TUNNEL_PLAYER, "null"):
            for path in (REFRESH, LOGOUT):
                r = await c.post(path, headers={"X-RD-CSRF": "1", "Origin": origin})
                assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED", (origin, path)
        # Origin permitido, mas sem o cabeçalho anti-CSRF → bloqueado
        r = await c.post(REFRESH, headers={"Origin": TUNNEL})
        assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"


async def test_full_session_via_tunnel_host_and_origin(spawn, db):
    """login → refresh → /me → logout com Host/Origin do túnel; origem maliciosa barrada
    em cada passo sem derrubar a sessão legítima. Cookie de refresh com atributos inalterados."""
    a = await spawn(DEV_ENV)
    u = await make_user(db, role="employee")
    hdr = {**UA, "Host": TUNNEL_HOST, "Origin": TUNNEL}
    async with httpx.AsyncClient(base_url=a.base, headers=hdr, timeout=20) as c:
        s = await login_as(c, u)
        assert c.cookies.get("rd_refresh")

        # origem maliciosa com o cookie presente → CSRF_FAILED e a sessão continua válida
        r = await c.post(REFRESH, headers={"X-RD-CSRF": "1", "Origin": EVIL})
        assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"

        r = await c.post(REFRESH, headers={"X-RD-CSRF": "1"})
        assert r.status_code == 200, r.text
        access = r.json()["access_token"]
        # atributos do cookie NÃO mudam por causa do túnel (dev http: Secure segue COOKIE_SECURE=false)
        sc = [h for h in r.headers.get_list("set-cookie") if h.startswith("rd_refresh=")]
        assert len(sc) == 1
        attrs = {p.strip().split("=", 1)[0].lower(): (p.strip().split("=", 1) + [""])[1].lower()
                 for p in sc[0].split(";")[1:]}
        assert "httponly" in attrs and attrs.get("samesite") == "strict" and attrs.get("path") == "/api/v1/auth"
        assert "domain" not in attrs and "secure" not in attrs
        r = await c.get("/api/v1/me", headers={"Authorization": f"Bearer {access}"})
        assert r.status_code == 200 and r.json()["user"]["email"] == u.email
        assert r.headers.get("access-control-allow-origin") == TUNNEL

        assert (await c.post(LOGOUT, headers={"X-RD-CSRF": "1", "Origin": EVIL})).status_code == 403
        assert (await c.post(LOGOUT, headers={"X-RD-CSRF": "1", "Authorization": f"Bearer {access}"})).status_code == 204
        r = await c.post(REFRESH, headers={"X-RD-CSRF": "1"})
        assert r.status_code == 401
        assert (await c.get("/api/v1/me", headers=s.headers)).status_code == 401


async def test_panel_and_player_websocket_origin(spawn, db):
    a = await spawn(DEV_ENV)
    async with a.client() as c:
        org, est, screen, adm, dev = await _env(db, c)

        async def admin_ticket() -> str:
            r = await c.post("/api/v1/realtime/ticket", headers=adm.headers)
            assert r.status_code in (200, 201), r.text
            return r.json()["ticket"]

        # painel: origem do túnel autorizada → conecta e responde ping
        for origin in ("http://localhost", TUNNEL):
            w = await ws_connect(f"{a.ws}/ws/admin?ticket={await admin_ticket()}",
                                 additional_headers={"Origin": origin}, open_timeout=10)
            await w.send(json.dumps({"type": "ping"}))
            msg = json.loads(await asyncio.wait_for(w.recv(), 8))
            assert msg["type"] == "pong"
            await w.close()
        # painel: maliciosa, origem do PLAYER e túnel não configurado → recusados (ticket válido)
        for origin in (EVIL, TUNNEL_PLAYER, "https://outro.ngrok-free.dev"):
            await _rejected(f"{a.ws}/ws/admin?ticket={await admin_ticket()}", origin)

        # player: túnel do player autorizado → conecta (server_time); maliciosa → recusada
        w = await ws_connect(f"{a.ws}/ws/player?ticket={await dev.ws_ticket(c)}",
                             additional_headers={"Origin": TUNNEL_PLAYER}, open_timeout=10)
        assert json.loads(await asyncio.wait_for(w.recv(), 8))["type"] == "server_time"
        await w.close()
        await _rejected(f"{a.ws}/ws/player?ticket={await dev.ws_ticket(c)}", EVIL)
        await _rejected(f"{a.ws}/ws/player/pairing?request_id={uuid.uuid4()}", EVIL)
