"""
RD Mídia Indoor — Configuração do domínio de players (W-PLAY).

Lê os campos de ``app.core.config.settings`` quando o W1 os publica e, enquanto não
existirem, as variáveis de ambiente equivalentes (com suporte a ``<NOME>_FILE``):

- ``PLAYER_ALLOWED_ORIGINS``: origens aceitas no ``Origin`` do WS do player (e no CORS);
  padrão: ``http://localhost:8088`` (player web local) e ``https://player.rd.local``
  (origem virtual da casca Windows). JSON ou CSV.
- ``PANEL_BASE_URL``: URL base do painel para o ``panel_pair_url`` (fallback ``FRONTEND_URL``).
- ``PLAYER_RELEASE_PUBLIC_KEYS``: JSON ``{"key_id": "<spki b64u>"}`` das chaves de RELEASE
  (verificação das versões publicadas — a assinatura é feita fora do servidor).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from app.core.config import settings

DEFAULT_PLAYER_ORIGINS = ["http://localhost:8088", "https://player.rd.local"]

# Parâmetros do protocolo (RDPP §2–§4, §7)
PAIRING_TTL_S = 15 * 60
CHALLENGE_TTL_S = 60
DEVICE_TOKEN_TTL_S = 15 * 60
WS_TICKET_TTL_S = 30
SIGNED_TS_SKEW_MS = 120_000
HEARTBEAT_INTERVAL_MS = 30_000
PRESENCE_TTL_S = 90
TELEMETRY_MIN_INTERVAL_S = 240
POP_BATCH_MAX = 200
POP_CLOCK_FUTURE_MS = 120_000
POP_MAX_OVERRUN_MS = 60_000
COMMAND_DEFAULT_TTL_S = 24 * 3600
COMMAND_MAX_TTL_S = 7 * 24 * 3600
UPDATE_SLOT_TTL_S = 30 * 60
MEDIA_AUTH_TTL_S = 8 * 24 * 3600


def _env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    if v:
        return v
    f = os.environ.get(f"{name}_FILE")
    if f:
        try:
            return Path(f).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    if s.startswith("["):
        return [str(x).strip() for x in json.loads(s)]
    return [x.strip() for x in s.split(",") if x.strip()]


def player_allowed_origins() -> list[str]:
    """Origens do Web Player: ``PLAYER_ALLOWED_ORIGINS`` (ou padrão) + ``RD_DEV_PLAYER_ALLOWED_ORIGINS``
    (somente development/test — ``settings.dev_player_origins``)."""
    from app.core.origins import merge_origins

    v = getattr(settings, "PLAYER_ALLOWED_ORIGINS", None)
    if not v:
        v = _env("PLAYER_ALLOWED_ORIGINS")
    origins = _as_list(v) or list(DEFAULT_PLAYER_ORIGINS)
    return merge_origins(origins, getattr(settings, "dev_player_origins", []))


def origin_allowed(origin: Optional[str]) -> bool:
    """Origin ausente (player nativo Android) é aceito; presente deve estar na lista
    de origens do player ou do painel (``settings.panel_origins``)."""
    from app.core.origins import origin_in

    if not origin:
        return True
    return origin_in(origin, player_allowed_origins()) or origin_in(origin, settings.panel_origins)


def panel_base_url() -> str:
    v = getattr(settings, "PANEL_BASE_URL", None) or _env("PANEL_BASE_URL") or settings.FRONTEND_URL
    return str(v).rstrip("/")


def panel_pair_url(code: str) -> str:
    return f"{panel_base_url()}/telas/parear?code={code}"


def release_public_keys() -> dict[str, str]:
    v = getattr(settings, "PLAYER_RELEASE_PUBLIC_KEYS", None) or _env("PLAYER_RELEASE_PUBLIC_KEYS")
    if not v:
        return {}
    if isinstance(v, dict):
        return {str(k): str(x) for k, x in v.items()}
    try:
        data = json.loads(v)
    except ValueError:
        return {}
    return {str(k): str(x) for k, x in data.items()} if isinstance(data, dict) else {}


# ─── Limites de presença CONFIGURÁVEIS (REQ-HB-002; conflito C-40) ───────────
# Mecanismo da fundação (system_settings: histórico, auditoria, PUT /settings/{key} com
# settings.manage). Lidos com cache CURTO por processo (≤ SETTINGS_CACHE_S): uma alteração
# vale em todos os processos (API e worker) sem reinício e sem polling no frontend.
from app.core.settings_service import SettingDef, get_setting, register  # noqa: E402

SETTING_PRESENCE_TTL_S = "player.presence_ttl_s"
SETTING_OFFLINE_ALERT_AFTER_MIN = "player.offline_alert_after_min"
DEFAULT_OFFLINE_ALERT_AFTER_MIN = 10
SETTINGS_CACHE_S = 30.0

register(
    SettingDef(
        SETTING_PRESENCE_TTL_S,
        PRESENCE_TTL_S,
        "int",
        "Segundos sem sinal (heartbeat/mensagem) até a tela ser considerada offline. Mínimo 40 s "
        "(heartbeat de 30 s + folga de rede), máximo 600 s.",
        min=40,
        max=600,
        category="players",
    )
)
register(
    SettingDef(
        SETTING_OFFLINE_ALERT_AFTER_MIN,
        DEFAULT_OFFLINE_ALERT_AFTER_MIN,
        "int",
        "Minutos offline até notificar o estabelecimento e a equipe (alerta screen.offline).",
        min=2,
        max=1440,
        category="players",
    )
)

_settings_cache: dict = {}


def _clock() -> float:
    import time

    return time.monotonic()


def clear_settings_cache() -> None:
    _settings_cache.clear()


async def _cached_setting(key: str, db=None):
    """Valor da configuração com cache por processo; falha de leitura → último valor ou default."""
    import structlog

    now = _clock()
    hit = _settings_cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        if db is not None:
            value = await get_setting(db, key)
        else:
            from app.core.database import session_factory

            async with session_factory()() as s:
                value = await get_setting(s, key)
    except Exception as exc:  # banco indisponível não derruba a presença
        structlog.get_logger("rd.playback.config").error("setting_read_failed", key=key,
                                                         error_type=type(exc).__name__)
        if hit is not None:
            return hit[1]
        from app.core.settings_service import get_def

        return get_def(key).default
    _settings_cache[key] = (now + SETTINGS_CACHE_S, value)
    return value


async def presence_ttl_s(db=None) -> int:
    return int(await _cached_setting(SETTING_PRESENCE_TTL_S, db))


async def offline_alert_after_min(db=None) -> int:
    return int(await _cached_setting(SETTING_OFFLINE_ALERT_AFTER_MIN, db))
