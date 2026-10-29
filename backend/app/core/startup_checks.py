"""
RD Mídia Indoor — Verificações de configuração no STARTUP dos processos de runtime.

Complementa as guardas de ``Settings`` (que valem para qualquer processo, inclusive o
``migrate``). Aqui ficam exigências dos processos que ATENDEM tráfego/tarefas — API,
worker e beat —, que o ``migrate`` não monta (ex.: chave de assinatura de manifestos).

Fora de ``development``/``test`` (production, staging):
- ``MANIFEST_SIGNING_KEY`` (via ``MANIFEST_SIGNING_KEY_FILE``) presente e chave privada
  EC P-256 em PEM (ES256 — RDPP/1);
- ``PLAYER_RELEASE_PUBLIC_KEYS`` (se definido) só com chaves públicas SPKI P-256 válidas;
- ``PLAYER_ALLOWED_ORIGINS``/``CORS_ORIGINS`` somente https:// exatas, sem curinga, loopback ou túnel
  público (ngrok etc.); ``RD_DEV_*`` (origens de desenvolvimento) proibidas.

Falha = exceção com a LISTA DE NOMES das configurações inválidas (nunca valores).
Chamada em: lifespan da API (``app.main``) e sinais ``worker_init``/``beat_init`` do Celery.
"""
from __future__ import annotations

import base64
from typing import Optional

from app.core.config import Settings, settings as _settings

DEV_ENVIRONMENTS = ("development", "test")


class StartupConfigError(RuntimeError):
    pass


def _b64u_decode(value: str) -> bytes:
    v = value.strip()
    return base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))


def manifest_key_problem(pem: str) -> Optional[str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if not (pem or "").strip():
        return "MANIFEST_SIGNING_KEY(_FILE) ausente"
    try:
        key = serialization.load_pem_private_key(pem.strip().encode("ascii"), password=None)
    except Exception:
        return "MANIFEST_SIGNING_KEY ilegível (esperado PEM PKCS#8 sem senha)"
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        return "MANIFEST_SIGNING_KEY não é chave privada EC P-256"
    return None


def release_keys_problems(keys: dict) -> list[str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    problems: list[str] = []
    for key_id, spki in (keys or {}).items():
        try:
            pub = serialization.load_der_public_key(_b64u_decode(str(spki)))
            ok = isinstance(pub, ec.EllipticCurvePublicKey) and isinstance(pub.curve, ec.SECP256R1)
        except Exception:
            ok = False
        if not ok:
            problems.append(f"PLAYER_RELEASE_PUBLIC_KEYS[{str(key_id)[:40]}] não é chave pública SPKI P-256")
    return problems


def runtime_problems(s: Optional[Settings] = None) -> list[str]:
    s = s or _settings
    if s.ENVIRONMENT in DEV_ENVIRONMENTS:
        return []
    problems: list[str] = []
    p = manifest_key_problem(s.MANIFEST_SIGNING_KEY)
    if p:
        problems.append(p)
    problems += release_keys_problems(s.PLAYER_RELEASE_PUBLIC_KEYS)
    if not s.PLAYER_ALLOWED_ORIGINS or any(not o.startswith("https://") for o in s.PLAYER_ALLOWED_ORIGINS):
        problems.append("PLAYER_ALLOWED_ORIGINS deve conter somente origens https://")
    # AÇÃO 003: origens oficiais exatas — sem curinga, loopback ou túnel público (ngrok etc.);
    # origens de DESENVOLVIMENTO nunca valem aqui (Settings já recusa RD_DEV_* fora de dev/test).
    from app.core.origins import official_origin_problems

    if not s.CORS_ORIGINS or any(not o.startswith("https://") for o in s.CORS_ORIGINS):
        problems.append("CORS_ORIGINS deve conter somente origens https://")
    problems += official_origin_problems("CORS_ORIGINS", s.CORS_ORIGINS)
    problems += official_origin_problems("PLAYER_ALLOWED_ORIGINS", s.PLAYER_ALLOWED_ORIGINS)
    if s.dev_panel_origins or s.dev_player_origins or s.RD_DEV_ALLOWED_ORIGINS or s.RD_DEV_PLAYER_ALLOWED_ORIGINS:
        problems.append("RD_DEV_ALLOWED_ORIGINS/RD_DEV_PLAYER_ALLOWED_ORIGINS presentes fora de development/test")
    return problems


def enforce_runtime_config(process: str, s: Optional[Settings] = None) -> None:
    """Lança ``StartupConfigError`` (sem valores) se a configuração de runtime for inválida."""
    problems = runtime_problems(s)
    if problems:
        env = (s or _settings).ENVIRONMENT
        raise StartupConfigError(
            f"Configuração inválida para {process} em ENVIRONMENT={env}: " + "; ".join(problems)
        )
