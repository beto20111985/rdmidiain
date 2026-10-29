"""
RD Mídia Indoor — Segurança: JWT, TOTP, Password Hashing, Device Tokens
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

import pyotp
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

# ─── PASSWORD ────────────────────────────────────────────────────────────────

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,
)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ─── JWT ─────────────────────────────────────────────────────────────────────

def create_access_token(
    subject: str,
    extra_claims: Optional[dict] = None,
    expires_delta: Optional[timedelta] = None,
) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta
        or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid4()),
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid4()),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        return payload
    except JWTError as e:
        raise ValueError(f"Token inválido: {e}") from e


def create_email_verification_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    payload = {
        "sub": email,
        "exp": expire,
        "type": "email_verify",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_password_reset_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=2)
    payload = {
        "sub": email,
        "exp": expire,
        "type": "password_reset",
        "nonce": secrets.token_hex(16),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


# ─── TOTP (2FA) ──────────────────────────────────────────────────────────────

def generate_totp_secret() -> str:
    return pyotp.random_base32()


def get_totp_uri(secret: str, email: str, issuer: str = "RD Mídia Indoor") -> str:
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer)


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    """
    Verifica código TOTP.
    valid_window=1 aceita código do período anterior (tolerância de clock).
    """
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=valid_window)
def sign_playback_event(
    device_secret: str,
    event_data: str,
    nonce: str,
) -> str:
    """
    Assina evento de proof-of-play com HMAC-SHA256.
    Garante autenticidade do player.
    """
    message = f"{event_data}:{nonce}".encode("utf-8")
    return hmac.new(
        device_secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def verify_playback_signature(
    device_secret: str,
    event_data: str,
    nonce: str,
    signature: str,
) -> bool:
    expected = sign_playback_event(device_secret, event_data, nonce)
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verifica assinatura de webhook CloudPayPag."""
    expected = hmac.new(
        settings.CLOUDPAYPAG_WEBHOOK_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


# ─── MISC ────────────────────────────────────────────────────────────────────

def generate_invite_token() -> str:
    return secrets.token_urlsafe(32)


def mask_email(email: str) -> str:
    """Mascara email para logs públicos: jo***@ex***.com"""
    parts = email.split("@")
    if len(parts) != 2:
        return "***"
    local, domain = parts
    masked_local = local[:2] + "***" if len(local) > 2 else "***"
    dom_parts = domain.split(".")
    masked_domain = dom_parts[0][:2] + "***" if dom_parts else "***"
    return f"{masked_local}@{masked_domain}.{'.'.join(dom_parts[1:])}"
