"""
RD Mídia Indoor — CAPTCHA (contrato §2: acionado após 3 falhas de login).

Interface ``CaptchaVerifier``. Nenhum provedor está configurado localmente
(``CAPTCHA_PROVIDER`` vazio) → ``DisabledCaptchaVerifier`` (``enabled=False``) e o
login não exige CAPTCHA. DEPENDÊNCIA EXTERNA: escolher provedor (ex.: hCaptcha,
Cloudflare Turnstile), configurar ``CAPTCHA_PROVIDER``/``CAPTCHA_SECRET`` e
implementar o verificador HTTP correspondente.
"""
from __future__ import annotations

from typing import Optional, Protocol

from app.core.config import settings


class CaptchaVerifier(Protocol):
    enabled: bool

    async def verify(self, token: Optional[str], remote_ip: Optional[str]) -> bool: ...


class DisabledCaptchaVerifier:
    enabled = False

    async def verify(self, token: Optional[str], remote_ip: Optional[str]) -> bool:
        return True


_override: Optional[CaptchaVerifier] = None


def set_captcha_verifier(v: Optional[CaptchaVerifier]) -> None:
    global _override
    _override = v


def get_captcha_verifier() -> CaptchaVerifier:
    if _override is not None:
        return _override
    if not settings.CAPTCHA_PROVIDER:
        return DisabledCaptchaVerifier()
    raise RuntimeError(
        f"CAPTCHA_PROVIDER={settings.CAPTCHA_PROVIDER!r} não implementado (dependência externa pendente)"
    )
