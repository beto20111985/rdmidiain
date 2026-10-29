"""
RD Mídia Indoor — Configuração central.

Regras (AÇÃO 002 / W1):
- Toda configuração vem de variáveis de ambiente (Pydantic Settings).
- Qualquer campo pode ser lido de arquivo via ``<NOME>_FILE`` (Docker secrets):
  ex.: ``JWT_SECRET_KEY_FILE=/run/secrets/jwt`` — o conteúdo do arquivo (sem
  quebra de linha final) vira o valor de ``JWT_SECRET_KEY``. Se ambos existirem,
  o valor explícito ``<NOME>`` prevalece.
- Defaults SEGUROS: ``DEBUG=False``, ``MOCK_PAYMENT=False``, ``COOKIE_SECURE``
  verdadeiro fora de development/test, ``ENVIRONMENT`` obrigatório.
- Validação de startup: em ``ENVIRONMENT=production`` a aplicação recusa subir com
  mock de pagamento, debug, cookie inseguro ou segredos curtos/iguais.
- Nunca imprimir valores de segredos (``Settings.__repr__`` mascara campos sensíveis).
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger("rd.config")

Environment = Literal["development", "test", "staging", "production"]

# Campos considerados segredos (nunca exibidos; validados em produção).
SECRET_FIELDS = (
    "SECRET_KEY",
    "JWT_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "DATA_ENCRYPTION_KEY",
    "SMTP_PASSWORD",
    "CLOUDPAYPAG_API_KEY",
    "CLOUDPAYPAG_WEBHOOK_SECRET",
    "MINIO_SECRET_KEY",
    "CAPTCHA_SECRET",
    "DATABASE_URL",
    "REDIS_URL",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "SENTRY_DSN",
    "MANIFEST_SIGNING_KEY",
)

MIN_SECRET_LENGTH = 32


class ConfigurationError(Exception):
    """Configuração inválida/insegura. NÃO é ValueError de propósito: o Pydantic propaga a
    exceção sem embrulhar em ValidationError — que anexaria ``input_value`` com trechos dos
    valores (segredos) à mensagem. A mensagem contém apenas NOMES de variáveis."""


_PLACEHOLDER_MAIL_DOMAINS = ("localhost", "example.com", "example.org", "example.net")
_PLACEHOLDER_MAIL_SUFFIXES = (".local", ".localhost", ".test", ".invalid", ".example")


def smtp_from_is_placeholder(value: str) -> bool:
    """True se o remetente estiver vazio ou usar domínio de desenvolvimento/reservado."""
    from email.utils import parseaddr

    _, addr = parseaddr(value or "")
    if "@" not in addr:
        return True
    domain = addr.rsplit("@", 1)[1].strip().lower()
    return (not domain or domain in _PLACEHOLDER_MAIL_DOMAINS or domain.endswith(_PLACEHOLDER_MAIL_SUFFIXES)
            or "." not in domain)


def _apply_file_secrets(values: dict[str, Any], field_names: set[str]) -> dict[str, Any]:
    """Suporte genérico a ``<NOME>_FILE`` para qualquer campo da configuração."""
    explicit = {k.lower(): v for k, v in values.items()}
    for name in field_names:
        file_var = f"{name}_FILE"
        path = os.environ.get(file_var) or os.environ.get(file_var.lower())
        if not path:
            continue
        # valor explícito prevalece
        if explicit.get(name.lower()) not in (None, ""):
            continue
        try:
            content = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:  # arquivo de segredo ausente é erro de configuração
            raise ConfigurationError(f"Não foi possível ler {file_var}: {exc.__class__.__name__}") from None
        values[name] = content
    return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── APP ────────────────────────────────────────────────────────────────
    ENVIRONMENT: Environment  # obrigatório — sem default inseguro
    DEBUG: bool = False
    APP_NAME: str = "RD Mídia Indoor"
    APP_VERSION: str = "1.0.0"
    SECRET_KEY: str
    # ALLOWED_HOSTS: informativo/reservado — NÃO é aplicado pela API (decisão AÇÃO 003, agente F).
    # Host é validado na BORDA: produção = nginx server_name (host desconhecido → 444 /
    # ssl_reject_handshake); desenvolvimento = Vite server.allowedHosts (painel/player). A API
    # autoriza por Origin (CORS/CSRF/WS — ver app.core.origins) + Bearer/cookie, e URLs absolutas
    # vêm de FRONTEND_URL/PANEL_BASE_URL — nunca do Host da requisição.
    ALLOWED_HOSTS: List[str] = ["localhost", "127.0.0.1"]
    # OpenAPI/docs: por padrão só em development/test (contrato §1)
    OPENAPI_ENABLED: Optional[bool] = None
    FRONTEND_URL: str = "http://localhost"
    # URL base do painel usada em links de e-mail/QR (fallback: FRONTEND_URL)
    PANEL_BASE_URL: str = ""

    # ─── DATABASE ────────────────────────────────────────────────────────────
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "rdmidiaindoor"
    POSTGRES_USER: str = "rduser"
    POSTGRES_PASSWORD: str
    DATABASE_URL: str = ""
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    # NullPool: obrigatório em test (um event loop por teste) e útil em scripts
    DB_NULLPOOL: Optional[bool] = None

    # ─── REDIS ───────────────────────────────────────────────────────────────
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str
    REDIS_URL: str = ""
    REDIS_KEY_PREFIX: str = "rd:"
    # Pool BLOQUEANTE: em rajada, espera conexão livre até REDIS_POOL_TIMEOUT_S em vez de
    # falhar com "Too many connections" (players/WS em massa por processo).
    REDIS_MAX_CONNECTIONS: int = 200
    REDIS_POOL_TIMEOUT_S: float = 5.0
    # Redis fora do ar → erro claro e RÁPIDO (P002 §50): conexão/operação curtas e disjuntor
    # em processo (após uma falha, chamadas críticas respondem 503 sem tentar por N s).
    REDIS_CONNECT_TIMEOUT_S: float = 1.0
    REDIS_SOCKET_TIMEOUT_S: float = 1.0
    REDIS_BREAKER_S: float = 5.0
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    # ─── JWT / SESSÕES ───────────────────────────────────────────────────────
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: Literal["HS256", "HS384", "HS512"] = "HS256"
    JWT_ISSUER: str = "rd-midiaindoor"
    JWT_AUDIENCE: str = "rd-api"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    SESSION_MAX_DAYS: int = 30
    MFA_TOKEN_EXPIRE_SECONDS: int = 300
    MFA_SETUP_TOKEN_EXPIRE_SECONDS: int = 600
    IMPERSONATION_EXPIRE_MINUTES: int = 15
    COOKIE_SECURE: Optional[bool] = None
    REFRESH_COOKIE_NAME: str = "rd_refresh"
    REFRESH_COOKIE_PATH: str = "/api/v1/auth"

    # Chave de criptografia de dados em repouso (TOTP). Se ausente, é derivada
    # de SECRET_KEY via HKDF (rotacionar SECRET_KEY exige re-cifrar segredos).
    DATA_ENCRYPTION_KEY: str = ""

    # ─── PROTEÇÃO DE LOGIN ───────────────────────────────────────────────────
    LOGIN_MAX_FAILED_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15
    CAPTCHA_AFTER_FAILURES: int = 3
    CAPTCHA_PROVIDER: str = ""  # "" = desabilitado (dependência externa)
    CAPTCHA_SECRET: str = ""
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 60
    # Proxies confiáveis para resolver o IP real (CIDRs). Somente quando o par TCP
    # é um destes, X-Forwarded-For / X-Real-IP são considerados.
    TRUSTED_PROXIES: List[str] = [
        "127.0.0.1/32",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]

    # ─── ARGON2 ──────────────────────────────────────────────────────────────
    ARGON2_TIME_COST: int = 3
    ARGON2_MEMORY_KIB: int = 65536
    ARGON2_PARALLELISM: int = 4

    # ─── CORS / ORIGENS (contrato em app.core.origins) ───────────────────────
    # Base do PAINEL: CORS + CSRF (refresh/logout) + Origin do WS /ws/admin. Produção: https oficiais.
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost"]
    # Base do WEB PLAYER: CORS + Origin dos WS /ws/player e /ws/player/pairing.
    PLAYER_ALLOWED_ORIGINS: List[str] = ["http://localhost:8088", "https://player.rd.local"]
    # Acréscimos de DESENVOLVIMENTO (ex.: túnel ngrok) — SOMENTE development/test; em staging/
    # production a presença de qualquer um deles RECUSA a subida. Origem exata http(s)://host[:porta]
    # (sem curinga). Valor local em ops/dev/dev-origins.env (nunca em código).
    RD_DEV_ALLOWED_ORIGINS: List[str] = []
    RD_DEV_PLAYER_ALLOWED_ORIGINS: List[str] = []

    # ─── PLAYER / MANIFESTO (agente de playback) ─────────────────────────────
    # Chaves públicas de releases do player: JSON {key_id: spki_base64url}; aceita _FILE
    PLAYER_RELEASE_PUBLIC_KEYS: Dict[str, str] = {}
    # Chave privada PEM para assinar manifestos (opcional em dev); use MANIFEST_SIGNING_KEY_FILE
    MANIFEST_SIGNING_KEY: str = ""

    @field_validator("PLAYER_RELEASE_PUBLIC_KEYS", mode="before")
    @classmethod
    def parse_json_dict(cls, v):
        if isinstance(v, str):
            import json

            v = v.strip()
            return json.loads(v) if v else {}
        return v

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", "TRUSTED_PROXIES", "PLAYER_ALLOWED_ORIGINS",
                     "RD_DEV_ALLOWED_ORIGINS", "RD_DEV_PLAYER_ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_csv(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json

                return json.loads(v)
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    # ─── STORAGE ─────────────────────────────────────────────────────────────
    STORAGE_PROVIDER: str = "local"  # "local" | "minio" | "s3"
    STORAGE_LOCAL_PATH: str = "/app/media"
    MINIO_ENDPOINT: str = ""
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "rdmidiaindoor"

    # ─── UPLOAD ──────────────────────────────────────────────────────────────
    MAX_IMAGE_SIZE_MB: int = 10
    MAX_VIDEO_SIZE_MB: int = 200
    ALLOWED_IMAGE_TYPES: List[str] = ["image/jpeg", "image/png", "image/gif"]
    ALLOWED_VIDEO_TYPES: List[str] = ["video/mp4", "video/webm"]
    MIN_VIDEO_DURATION_S: float = 29.5
    MAX_VIDEO_DURATION_S: float = 120.5

    @field_validator("ALLOWED_IMAGE_TYPES", "ALLOWED_VIDEO_TYPES", mode="before")
    @classmethod
    def parse_list(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    # ─── FINANCEIRO ──────────────────────────────────────────────────────────
    # Regras de negócio configuráveis vivem em app.core.settings_service
    # (system_settings). Os campos abaixo são legados mantidos por compatibilidade.
    MIN_MARGIN_PERCENT: float = 40.0
    DEFAULT_COMMISSION_POOL_PERCENT: float = 5.0
    GATEWAY_FEE_PERCENT: float = 2.5
    MOCK_PAYMENT: bool = False  # default seguro; produção recusa True

    # ─── CLOUDPAYPAG ─────────────────────────────────────────────────────────
    CLOUDPAYPAG_BASE_URL: str = "https://cloudpaypag.top"
    CLOUDPAYPAG_API_KEY: str = ""
    CLOUDPAYPAG_WEBHOOK_SECRET: str = ""

    # ─── SCHEDULER (legado — ver settings_service) ───────────────────────────
    INVENTORY_RESERVATION_HOURS: int = 2
    PIX_KEY_CHANGE_BLOCK_HOURS: int = 12
    PLAYER_OFFLINE_ALERT_DAYS: int = 7
    PLAYER_OFFLINE_CRITICAL_DAYS: int = 28

    # ─── COMISSÃO (legado) ───────────────────────────────────────────────────
    COMMISSION_CLOSE_DAY: int = 28
    COMMISSION_PAY_DAY: int = 1

    # ─── EMAIL ───────────────────────────────────────────────────────────────
    # EMAIL_BACKEND: "smtp" (aiosmtplib) | "log" (descarta, registra só o assunto)
    # | "memory" (testes). Default: smtp se SMTP_HOST definido, senão log.
    EMAIL_BACKEND: Optional[Literal["smtp", "log", "memory"]] = None
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_STARTTLS: Optional[bool] = None  # default: True exceto porta 1025 (Mailpit)
    SMTP_TIMEOUT_S: int = 10
    # Remetente: domínio NUNCA fixo no código (REQ-NEG-008). Default neutro p/ dev (Mailpit);
    # produção exige valor configurado (guarda abaixo).
    SMTP_FROM: str = "RD Mídia Indoor <no-reply@localhost>"

    # ─── LGPD ────────────────────────────────────────────────────────────────
    TERMS_VERSION: str = "2026-10-01"
    PRIVACY_VERSION: str = "2026-10-01"

    # ─── OBSERVABILIDADE ─────────────────────────────────────────────────────
    SENTRY_DSN: str = ""
    PROMETHEUS_ENABLED: bool = False
    LOG_JSON: Optional[bool] = None

    # ------------------------------------------------------------------------
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        """Listas aceitam JSON (``["a","b"]``) ou CSV (``a,b``) nas variáveis de ambiente."""
        for src in (env_settings, dotenv_settings):
            orig = src.decode_complex_value

            def lenient(field_name, field, value, _orig=orig):
                try:
                    return _orig(field_name, field, value)
                except ValueError:
                    return value

            src.decode_complex_value = lenient
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @model_validator(mode="before")
    @classmethod
    def _file_secrets(cls, values: Any) -> Any:
        if isinstance(values, dict):
            return _apply_file_secrets(dict(values), set(cls.model_fields.keys()))
        return values

    @model_validator(mode="after")
    def _derive_and_validate(self) -> "Settings":
        if not self.DATABASE_URL:
            self.DATABASE_URL = (
                f"postgresql+asyncpg://{self.POSTGRES_USER}:"
                f"{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:"
                f"{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            )
        if not self.REDIS_URL:
            self.REDIS_URL = f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        if not self.CELERY_BROKER_URL:
            self.CELERY_BROKER_URL = f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/1"
        if not self.CELERY_RESULT_BACKEND:
            self.CELERY_RESULT_BACKEND = f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/2"

        dev_like = self.ENVIRONMENT in ("development", "test")
        if self.COOKIE_SECURE is None:
            self.COOKIE_SECURE = not dev_like
        if self.OPENAPI_ENABLED is None:
            self.OPENAPI_ENABLED = dev_like
        if self.DB_NULLPOOL is None:
            self.DB_NULLPOOL = self.ENVIRONMENT == "test"
        if self.LOG_JSON is None:
            self.LOG_JSON = not dev_like
        if self.EMAIL_BACKEND is None:
            self.EMAIL_BACKEND = "smtp" if self.SMTP_HOST else "log"
        if self.SMTP_STARTTLS is None:
            self.SMTP_STARTTLS = self.SMTP_PORT not in (25, 1025)

        # Access token curto (contrato §2: 15 min). Fora de produção, valor maior
        # (ex.: .env antigo com 60) é reduzido a 15 com aviso; em produção é erro.
        if not (1 <= self.ACCESS_TOKEN_EXPIRE_MINUTES <= 15):
            if self.ENVIRONMENT == "production":
                raise ConfigurationError("ACCESS_TOKEN_EXPIRE_MINUTES deve estar entre 1 e 15 em produção")
            _log.warning("ACCESS_TOKEN_EXPIRE_MINUTES fora de 1..15; usando 15 (contrato §2)")
            self.ACCESS_TOKEN_EXPIRE_MINUTES = 15

        # Origens de DESENVOLVIMENTO (AÇÃO 003): fora de development/test → recusa subir;
        # em development/test → validadas e normalizadas (entrada inválida → recusa subir).
        from app.core.origins import OriginError, parse_origin_list

        for name in ("RD_DEV_ALLOWED_ORIGINS", "RD_DEV_PLAYER_ALLOWED_ORIGINS"):
            raw = [x for x in (getattr(self, name) or []) if str(x or "").strip()]
            if not dev_like:
                if raw:
                    raise ConfigurationError(
                        f"{name} só é aceito com ENVIRONMENT=development|test (atual: {self.ENVIRONMENT}). "
                        "Remova-o; origens oficiais vão em CORS_ORIGINS/PLAYER_ALLOWED_ORIGINS (https://)."
                    )
                setattr(self, name, [])
                continue
            try:
                setattr(self, name, parse_origin_list(raw))
            except OriginError as exc:
                raise ConfigurationError(f"{name} inválido ({exc.reason}); formato: http(s)://host[:porta]") from None

        problems = self.production_problems()
        if self.ENVIRONMENT == "production" and problems:
            raise ConfigurationError("Configuração insegura para produção: " + "; ".join(problems))
        return self

    def production_problems(self) -> list[str]:
        """Lista de violações das guardas de produção (sem expor valores)."""
        problems: list[str] = []
        if self.MOCK_PAYMENT:
            problems.append("MOCK_PAYMENT=true")
        if self.DEBUG:
            problems.append("DEBUG=true")
        if not self.COOKIE_SECURE:
            problems.append("COOKIE_SECURE=false")
        if self.OPENAPI_ENABLED:
            problems.append("OPENAPI_ENABLED=true")
        for name in ("SECRET_KEY", "JWT_SECRET_KEY"):
            if len(getattr(self, name) or "") < MIN_SECRET_LENGTH:
                problems.append(f"{name} com menos de {MIN_SECRET_LENGTH} caracteres")
        if self.SECRET_KEY and self.SECRET_KEY == self.JWT_SECRET_KEY:
            problems.append("SECRET_KEY igual a JWT_SECRET_KEY")
        if self.DATA_ENCRYPTION_KEY and len(self.DATA_ENCRYPTION_KEY) < MIN_SECRET_LENGTH:
            problems.append(f"DATA_ENCRYPTION_KEY com menos de {MIN_SECRET_LENGTH} caracteres")
        if not self.DATA_ENCRYPTION_KEY:
            problems.append("DATA_ENCRYPTION_KEY(_FILE) ausente")
        if not self.CORS_ORIGINS or any(not o.startswith("https://") for o in self.CORS_ORIGINS):
            problems.append("CORS_ORIGINS deve conter somente origens https://")
        if not self.PLAYER_ALLOWED_ORIGINS or any(not o.startswith("https://") for o in self.PLAYER_ALLOWED_ORIGINS):
            problems.append("PLAYER_ALLOWED_ORIGINS deve conter somente origens https://")
        from app.core.origins import official_origin_problems

        problems += official_origin_problems("CORS_ORIGINS", self.CORS_ORIGINS)
        problems += official_origin_problems("PLAYER_ALLOWED_ORIGINS", self.PLAYER_ALLOWED_ORIGINS)
        for name in ("FRONTEND_URL", "PANEL_BASE_URL"):
            v = getattr(self, name) or ""
            if (v or name == "FRONTEND_URL") and not v.startswith("https://"):
                problems.append(f"{name} deve ser https://")
        if self.EMAIL_BACKEND != "smtp" or not self.SMTP_HOST:
            problems.append("EMAIL_BACKEND=smtp com SMTP_HOST é obrigatório")
        if smtp_from_is_placeholder(self.SMTP_FROM):
            problems.append("SMTP_FROM não configurado (domínio de desenvolvimento/placeholder)")
        if not self.MOCK_PAYMENT:
            for name in ("CLOUDPAYPAG_API_KEY", "CLOUDPAYPAG_WEBHOOK_SECRET"):
                if not getattr(self, name):
                    problems.append(f"{name}(_FILE) ausente")
        return problems

    def __repr_args__(self):  # nunca expor segredos em repr/log
        for k, v in super().__repr_args__():
            if k in SECRET_FIELDS:
                yield k, "***"
            else:
                yield k, v

    @property
    def panel_base_url(self) -> str:
        return (self.PANEL_BASE_URL or self.FRONTEND_URL).rstrip("/")

    # ─── Listas EFETIVAS de origens (únicos pontos lidos pelos consumidores) ──
    @property
    def dev_panel_origins(self) -> list[str]:
        """``RD_DEV_ALLOWED_ORIGINS`` normalizada — vazia fora de development/test."""
        return list(self.RD_DEV_ALLOWED_ORIGINS) if self.ENVIRONMENT in ("development", "test") else []

    @property
    def dev_player_origins(self) -> list[str]:
        """``RD_DEV_PLAYER_ALLOWED_ORIGINS`` normalizada — vazia fora de development/test."""
        return list(self.RD_DEV_PLAYER_ALLOWED_ORIGINS) if self.ENVIRONMENT in ("development", "test") else []

    @property
    def panel_origins(self) -> list[str]:
        """Painel: CORS + CSRF (cookie de refresh) + Origin do WS /ws/admin."""
        from app.core.origins import merge_origins

        return merge_origins(self.CORS_ORIGINS, self.dev_panel_origins)

    @property
    def player_origins(self) -> list[str]:
        """Web Player: CORS + Origin dos WS do player."""
        from app.core.origins import merge_origins

        return merge_origins(self.PLAYER_ALLOWED_ORIGINS, self.dev_player_origins)

    @property
    def all_cors_origins(self) -> list[str]:
        """Origens aceitas pelo CORS: painel + Web Player (sem duplicatas, sem curinga)."""
        from app.core.origins import merge_origins

        return merge_origins(self.panel_origins, self.player_origins)

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"

    @property
    def is_test(self) -> bool:
        return self.ENVIRONMENT == "test"

    @property
    def database_url_sync(self) -> str:
        """URL síncrona (psycopg2) para Alembic."""
        return self.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
