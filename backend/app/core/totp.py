"""
RD Mídia Indoor — TOTP (RFC 6238) com anti-replay.

- Passo de 30 s, 6 dígitos, SHA-1 (compatível com Google Authenticator).
- Política de janela: aceita o passo atual e ±1 (tolerância de relógio).
- ANTI-REPLAY: ``match_step`` devolve o passo que casou; o chamador só aceita se
  ``step > user.totp_last_step`` e grava o novo ``totp_last_step`` (na mesma
  transação, com ``SELECT ... FOR UPDATE``). Assim o mesmo código — ou um código de
  passo anterior ao último aceito — nunca é aceito duas vezes.
- ``otpauth_uri``: URI para o app autenticador; o QR é gerado LOCALMENTE no
  frontend (nunca serviço de terceiros).
"""
from __future__ import annotations

import hmac
import time
from typing import Optional
from urllib.parse import quote

import pyotp

PERIOD = 30
DIGITS = 6
WINDOW = 1
ISSUER = "RD Mídia Indoor"


def new_secret() -> str:
    return pyotp.random_base32(length=32)


def current_step(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def code_at_step(secret: str, step: int) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD).at(step * PERIOD)


def match_step(secret: str, code: str, *, now: Optional[float] = None, window: int = WINDOW) -> Optional[int]:
    """Retorna o passo que corresponde ao código (ou None)."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    base = current_step(now)
    found: Optional[int] = None
    # compara todas as janelas (tempo constante em relação à posição)
    for delta in range(-window, window + 1):
        step = base + delta
        if hmac.compare_digest(code_at_step(secret, step), code):
            found = step if found is None else max(found, step)
    return found


def otpauth_uri(secret: str, account: str, issuer: str = ISSUER) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD).provisioning_uri(name=account, issuer_name=issuer)


def label(account: str) -> str:
    return quote(account)
