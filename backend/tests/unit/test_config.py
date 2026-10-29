"""Configuração: defaults seguros, guardas de produção e segredos via <NOME>_FILE."""
import pytest
from pydantic import ValidationError

from app.core.config import ConfigurationError, Settings

# Configuração de PRODUÇÃO válida (valores fictícios; domínio fictício de cliente)
BASE = dict(
    SECRET_KEY="s" * 40,
    JWT_SECRET_KEY="j" * 40,
    DATA_ENCRYPTION_KEY="d" * 40,
    POSTGRES_PASSWORD="pg",
    REDIS_PASSWORD="rd",
    CORS_ORIGINS="https://painel.cliente-exemplo.com.br",
    PLAYER_ALLOWED_ORIGINS="https://player.cliente-exemplo.com.br",
    FRONTEND_URL="https://painel.cliente-exemplo.com.br",
    EMAIL_BACKEND="smtp",
    SMTP_HOST="smtp.cliente-exemplo.com.br",
    SMTP_FROM="Mídia Indoor <no-reply@cliente-exemplo.com.br>",
    CLOUDPAYPAG_API_KEY="k" * 24,
    CLOUDPAYPAG_WEBHOOK_SECRET="w" * 32,
)


def _settings(monkeypatch, **env):
    # isola do ambiente do container: remove TODA variável de configuração (e seu _FILE)
    for name in Settings.model_fields:
        for k in (name, f"{name}_FILE", name.lower(), f"{name}_FILE".lower()):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    for k, v in {**BASE, **env}.items():
        monkeypatch.setenv(k, str(v))
    return Settings(_env_file=None)


def test_environment_is_required(monkeypatch):
    with pytest.raises(ValidationError):
        _settings(monkeypatch)


def test_valid_production_config_starts(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="production")
    assert s.production_problems() == []


def test_safe_defaults(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="production")
    assert s.DEBUG is False and s.MOCK_PAYMENT is False
    assert s.COOKIE_SECURE is True and s.OPENAPI_ENABLED is False
    assert s.ACCESS_TOKEN_EXPIRE_MINUTES == 15
    assert "***" in repr(s) and "s" * 40 not in repr(s)


@pytest.mark.parametrize(
    "env",
    [
        {"MOCK_PAYMENT": "true"},
        {"DEBUG": "true"},
        {"COOKIE_SECURE": "false"},
        {"OPENAPI_ENABLED": "true"},
        {"SECRET_KEY": "curta"},
        {"JWT_SECRET_KEY": "j" * 10},
        {"JWT_SECRET_KEY": "s" * 40},  # igual à SECRET_KEY
        {"ACCESS_TOKEN_EXPIRE_MINUTES": "60"},
        {"CORS_ORIGINS": "http://inseguro.example.com"},
        {"PLAYER_ALLOWED_ORIGINS": "http://player.example.com"},
        {"PLAYER_ALLOWED_ORIGINS": ""},
        {"DATA_ENCRYPTION_KEY": ""},
        {"FRONTEND_URL": "http://painel.cliente-exemplo.com.br"},
        {"PANEL_BASE_URL": "http://painel.cliente-exemplo.com.br"},
        {"EMAIL_BACKEND": "log"},
        {"SMTP_HOST": ""},
        {"SMTP_FROM": ""},
        {"SMTP_FROM": "RD Mídia Indoor <no-reply@localhost>"},  # default de dev
        {"SMTP_FROM": "x <a@example.com>"},
        {"SMTP_FROM": "x <a@painel.local>"},
        {"CLOUDPAYPAG_API_KEY": ""},
        {"CLOUDPAYPAG_WEBHOOK_SECRET": ""},
    ],
)
def test_production_refuses_insecure_config(monkeypatch, env):
    with pytest.raises(ConfigurationError) as ei:
        _settings(monkeypatch, ENVIRONMENT="production", **env)
    msg = str(ei.value)
    assert msg.startswith("Configuração insegura para produção") or "ACCESS_TOKEN" in msg
    # nunca expõe valores (nem trechos de segredos)
    for secret in ("s" * 10, "j" * 10, "d" * 10, "k" * 10, "w" * 10):
        assert secret not in msg


def test_smtp_from_default_is_neutral(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="development", SMTP_FROM="")
    monkeypatch.delenv("SMTP_FROM", raising=False)
    s = Settings(_env_file=None)
    assert "rdmidiaindoor" not in s.SMTP_FROM and s.SMTP_FROM.endswith("@localhost>")


def test_development_allows_but_clamps_access_ttl(monkeypatch):
    s = _settings(monkeypatch, ENVIRONMENT="development", ACCESS_TOKEN_EXPIRE_MINUTES="60", MOCK_PAYMENT="true")
    assert s.ACCESS_TOKEN_EXPIRE_MINUTES == 15
    assert s.COOKIE_SECURE is False and s.OPENAPI_ENABLED is True


def test_file_secret(monkeypatch, tmp_path):
    f = tmp_path / "jwt"
    f.write_text("k" * 48 + "\n", encoding="utf-8")
    s = _settings(monkeypatch, ENVIRONMENT="production", JWT_SECRET_KEY="", JWT_SECRET_KEY_FILE=str(f))
    assert s.JWT_SECRET_KEY == "k" * 48


def test_explicit_value_wins_over_file(monkeypatch, tmp_path):
    f = tmp_path / "jwt"
    f.write_text("k" * 48, encoding="utf-8")
    s = _settings(monkeypatch, ENVIRONMENT="production", JWT_SECRET_KEY_FILE=str(f))
    assert s.JWT_SECRET_KEY == "j" * 40


def test_file_secret_missing_file_fails(monkeypatch, tmp_path):
    with pytest.raises(ConfigurationError):
        _settings(monkeypatch, ENVIRONMENT="development", JWT_SECRET_KEY="",
                  JWT_SECRET_KEY_FILE=str(tmp_path / "nao-existe"))


def test_player_settings_and_panel_url(monkeypatch, tmp_path):
    keys = tmp_path / "keys.json"
    keys.write_text('{"k1": "MCowBQYDK2VwAyEA"}', encoding="utf-8")
    pem = tmp_path / "manifest.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n", encoding="utf-8")
    s = _settings(monkeypatch, ENVIRONMENT="development", PLAYER_RELEASE_PUBLIC_KEYS_FILE=str(keys),
                  MANIFEST_SIGNING_KEY_FILE=str(pem), CORS_ORIGINS="http://localhost", FRONTEND_URL="http://localhost",
                  PLAYER_ALLOWED_ORIGINS="http://localhost:8088,http://localhost")
    assert s.PLAYER_RELEASE_PUBLIC_KEYS == {"k1": "MCowBQYDK2VwAyEA"}
    assert s.MANIFEST_SIGNING_KEY.startswith("-----BEGIN PRIVATE KEY-----") and "abc" not in repr(s)
    assert s.all_cors_origins == ["http://localhost", "http://localhost:8088"]
    assert s.panel_base_url == "http://localhost"
    s2 = _settings(monkeypatch, ENVIRONMENT="development", PANEL_BASE_URL="https://painel.example.com/",
                   PLAYER_RELEASE_PUBLIC_KEYS='{"a": "b"}')
    assert s2.panel_base_url == "https://painel.example.com" and s2.PLAYER_RELEASE_PUBLIC_KEYS == {"a": "b"}
