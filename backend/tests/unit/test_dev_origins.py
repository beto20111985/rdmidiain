"""AÇÃO 003 (agente F) — contrato de origens: RD_DEV_* só em development/test, produção recusa
origens de túnel/curinga/loopback, listas efetivas usadas por CORS/CSRF/WS.

Domínios de túnel aqui são FICTÍCIOS (nenhum domínio real de conta/instância em código)."""
import pytest

from app.core import origins as og
from app.core import startup_checks as sc
from app.core.config import ConfigurationError
from tests.unit.test_config import _settings

TUNNEL = "https://rd-teste-tunel.ngrok-free.dev"
TUNNEL_PLAYER = "https://rd-teste-player.ngrok-free.dev"
EVIL = "https://malicioso.example.com"


# ─── parse/normalização ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://localhost", "http://localhost"),
        ("http://LOCALHOST/", "http://localhost"),
        ("  http://127.0.0.1:80 ", "http://127.0.0.1"),
        ("https://Rd-Teste-Tunel.ngrok-free.dev:443/", TUNNEL),
        ("http://localhost:8088", "http://localhost:8088"),
        ("http://[::1]:5173", "http://[::1]:5173"),
    ],
)
def test_normalize_origin_ok(raw, expected):
    assert og.normalize_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "*",
        "https://*.ngrok-free.dev",
        "*.ngrok-free.dev",
        "rd-teste-tunel.ngrok-free.dev",  # sem esquema
        "ftp://exemplo.com",
        "https://exemplo.com/painel",
        "https://exemplo.com?x=1",
        "https://exemplo.com#frag",
        "https://user:pw@exemplo.com",
        "https://exa mple.com",
        "https://exemplo..com",
        "https://-exemplo.com",
        "https://exemplo.com:99999",
        "http:localhost",
        "null",
    ],
)
def test_normalize_origin_rejects(raw):
    with pytest.raises(og.OriginError):
        og.normalize_origin(raw)


def test_parse_list_ignores_empty_dedups_and_reports_position_without_value():
    assert og.parse_origin_list(["http://localhost", "", "http://localhost/", TUNNEL]) == ["http://localhost", TUNNEL]
    with pytest.raises(og.OriginError) as ei:
        og.parse_origin_list(["http://localhost", "https://*.segredo-xyz.example"])
    assert "item 2" in ei.value.reason and "segredo-xyz" not in str(ei.value)


def test_merge_drops_wildcards():
    assert og.merge_origins(["*", "http://localhost/"], ["http://localhost", "https://*.x.com"]) == ["http://localhost"]


def test_origin_in_is_exact():
    allowed = ["http://localhost", TUNNEL]
    assert og.origin_in("http://localhost", allowed) and og.origin_in(TUNNEL + "/", allowed)
    for bad in (None, "", "null", EVIL, "http://localhost:5173", TUNNEL.replace("https", "http"),
                "https://x" + TUNNEL[8:], TUNNEL + ".evil.com"):
        assert not og.origin_in(bad, allowed), bad


# ─── Settings: desenvolvimento ──────────────────────────────────────────────

def test_dev_origins_feed_panel_player_and_cors(monkeypatch):
    s = _settings(
        monkeypatch, ENVIRONMENT="development",
        CORS_ORIGINS="http://localhost", PLAYER_ALLOWED_ORIGINS="http://localhost:8088",
        RD_DEV_ALLOWED_ORIGINS=f"http://localhost, http://127.0.0.1 ,{TUNNEL}/",
        RD_DEV_PLAYER_ALLOWED_ORIGINS=f"http://localhost:8088,{TUNNEL_PLAYER}",
    )
    assert s.RD_DEV_ALLOWED_ORIGINS == ["http://localhost", "http://127.0.0.1", TUNNEL]
    assert s.panel_origins == ["http://localhost", "http://127.0.0.1", TUNNEL]
    assert s.player_origins == ["http://localhost:8088", TUNNEL_PLAYER]
    assert s.all_cors_origins == ["http://localhost", "http://127.0.0.1", TUNNEL, "http://localhost:8088", TUNNEL_PLAYER]
    assert EVIL not in s.all_cors_origins and "*" not in s.all_cors_origins
    # cookies continuam como antes (dev http): HttpOnly/SameSite=Strict são fixos no código
    assert s.COOKIE_SECURE is False and s.REFRESH_COOKIE_PATH == "/api/v1/auth"


def test_dev_origins_json_list_also_accepted(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="test", RD_DEV_ALLOWED_ORIGINS=f'["{TUNNEL}"]')
    assert s.dev_panel_origins == [TUNNEL]


def test_without_dev_origins_lists_are_unchanged(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="development", CORS_ORIGINS="http://localhost")
    assert s.RD_DEV_ALLOWED_ORIGINS == [] and s.panel_origins == ["http://localhost"]


@pytest.mark.parametrize("bad", ["*", "https://*.ngrok-free.dev", "rd-teste-tunel.ngrok-free.dev", "https://x.com/p"])
def test_invalid_dev_origin_refuses_to_start_even_in_dev(monkeypatch, bad):
    with pytest.raises(ConfigurationError) as ei:
        _settings(monkeypatch, ENVIRONMENT="development", RD_DEV_ALLOWED_ORIGINS=f"http://localhost,{bad}")
    assert "RD_DEV_ALLOWED_ORIGINS" in str(ei.value) and "item 2" in str(ei.value)


# ─── Settings/startup: produção e staging ───────────────────────────────────

@pytest.mark.parametrize("env", ["production", "staging"])
@pytest.mark.parametrize("var", ["RD_DEV_ALLOWED_ORIGINS", "RD_DEV_PLAYER_ALLOWED_ORIGINS"])
def test_non_dev_refuses_dev_origins(monkeypatch, env, var):
    with pytest.raises(ConfigurationError) as ei:
        _settings(monkeypatch, ENVIRONMENT=env, **{var: TUNNEL})
    msg = str(ei.value)
    assert var in msg and "development|test" in msg and "ngrok" not in msg  # sem valores


def test_production_empty_dev_var_is_absent(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="production", RD_DEV_ALLOWED_ORIGINS="")
    assert s.dev_panel_origins == [] and s.production_problems() == []


@pytest.mark.parametrize(
    "env,needle",
    [
        ({"CORS_ORIGINS": f"https://painel.cliente-exemplo.com.br,{TUNNEL}"}, "túnel"),
        ({"CORS_ORIGINS": "https://rd-x.ngrok.app"}, "túnel"),
        ({"CORS_ORIGINS": "https://abc.trycloudflare.com"}, "túnel"),
        ({"PLAYER_ALLOWED_ORIGINS": f"https://player.cliente-exemplo.com.br,{TUNNEL_PLAYER}"}, "túnel"),
        ({"CORS_ORIGINS": "https://*.cliente-exemplo.com.br"}, "curinga"),
        ({"CORS_ORIGINS": "*"}, "https://"),
        ({"CORS_ORIGINS": "https://localhost"}, "loopback"),
        ({"PLAYER_ALLOWED_ORIGINS": "https://127.0.0.1"}, "loopback"),
        ({"CORS_ORIGINS": "https://painel.cliente-exemplo.com.br/app"}, "malformada"),
    ],
)
def test_production_refuses_tunnel_wildcard_loopback(monkeypatch, env, needle):
    with pytest.raises(ConfigurationError) as ei:
        _settings(monkeypatch, ENVIRONMENT="production", **env)
    assert needle in str(ei.value)
    assert "rd-teste" not in str(ei.value)  # nunca ecoa valores


def test_production_official_origins_still_pass(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="production",
                  CORS_ORIGINS="https://painel.cliente-exemplo.com.br,https://player.cliente-exemplo.com.br",
                  PLAYER_ALLOWED_ORIGINS="https://player.cliente-exemplo.com.br,https://player.rd.local")
    assert s.production_problems() == [] and s.dev_panel_origins == [] and s.dev_player_origins == []


def test_staging_runtime_check_refuses_tunnel_origins(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="staging", CORS_ORIGINS=TUNNEL,
                  PLAYER_ALLOWED_ORIGINS="https://player.cliente-exemplo.com.br")
    probs = sc.runtime_problems(s)
    assert any("CORS_ORIGINS" in p and "túnel" in p for p in probs)


def test_dev_runtime_check_unchanged(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="development", RD_DEV_ALLOWED_ORIGINS=TUNNEL)
    assert sc.runtime_problems(s) == []


# ─── consumidores leem as listas efetivas ───────────────────────────────────

def test_consumers_use_effective_lists(monkeypatch):
    from starlette.requests import Request

    from app.core.config import settings
    from app.core.errors import DomainError
    from app.modules.auth.cookies import check_csrf
    from app.modules.playback import config as pcfg
    from app.modules.websocket.routes import _panel_origin_ok

    monkeypatch.setattr(settings, "RD_DEV_ALLOWED_ORIGINS", [TUNNEL])
    monkeypatch.setattr(settings, "RD_DEV_PLAYER_ALLOWED_ORIGINS", [TUNNEL_PLAYER])

    def req(origin):
        headers = [(b"x-rd-csrf", b"1")] + ([(b"origin", origin.encode())] if origin else [])
        return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})

    check_csrf(req(TUNNEL))  # permitido
    for bad in (EVIL, TUNNEL_PLAYER, None):  # origem do player NÃO autoriza o cookie do painel
        with pytest.raises(DomainError) as ei:
            check_csrf(req(bad))
        assert ei.value.code == "CSRF_FAILED"
    assert _panel_origin_ok(TUNNEL) and not _panel_origin_ok(EVIL) and not _panel_origin_ok(TUNNEL_PLAYER)
    assert pcfg.origin_allowed(TUNNEL_PLAYER) and pcfg.origin_allowed(TUNNEL) and not pcfg.origin_allowed(EVIL)

    # fora de development/test as listas de dev não valem, mesmo que preenchidas
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert TUNNEL not in settings.panel_origins and not _panel_origin_ok(TUNNEL)
    assert not pcfg.origin_allowed(TUNNEL_PLAYER)
    with pytest.raises(DomainError):
        check_csrf(req(TUNNEL))
