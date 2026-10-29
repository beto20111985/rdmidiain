"""
RD Mídia Indoor — Senhas.

- Hash padrão: **argon2id** (argon2-cffi), parâmetros configuráveis.
- Hashes bcrypt legados (``$2a$/$2b$/$2y$``) continuam verificáveis e são
  migrados de forma transparente no próximo login bem-sucedido (``needs_rehash``).
- ``dummy_verify()``: gasta o mesmo tempo de uma verificação real quando o usuário
  não existe (evita enumeração por tempo).
- Política (contrato §2): mínimo 10 caracteres, letras e números, máximo 128,
  não pode conter o e-mail (nem a parte local, se ≥ 3 caracteres).
- ``random_unusable_hash()``: hash argon2 de um segredo aleatório descartado —
  ninguém conhece a senha (usado para invalidar senhas e contas sem senha).
"""
from __future__ import annotations

import re
import secrets
from functools import lru_cache
from typing import Optional

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings
from app.core.errors import DomainError

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128


@lru_cache(maxsize=1)
def _hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=settings.ARGON2_TIME_COST,
        memory_cost=settings.ARGON2_MEMORY_KIB,
        parallelism=settings.ARGON2_PARALLELISM,
    )


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def is_bcrypt_hash(h: str) -> bool:
    return bool(h) and h.startswith(("$2a$", "$2b$", "$2y$"))


def verify_password(password: str, hashed: Optional[str]) -> tuple[bool, bool]:
    """Retorna ``(ok, needs_rehash)``."""
    if not hashed:
        dummy_verify()
        return False, False
    if is_bcrypt_hash(hashed):
        try:
            ok = bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        except ValueError:
            return False, False
        return ok, ok  # bcrypt válido → migrar para argon2id
    try:
        _hasher().verify(hashed, password)
    except VerifyMismatchError:
        return False, False
    except (VerificationError, InvalidHashError):
        return False, False
    return True, _hasher().check_needs_rehash(hashed)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(24))


def dummy_verify() -> None:
    try:
        _hasher().verify(_dummy_hash(), "x" * 12)
    except VerificationError:
        pass


def random_unusable_hash() -> str:
    return hash_password(secrets.token_urlsafe(48))


_LETTER = re.compile(r"[A-Za-zÀ-ÿ]")
_DIGIT = re.compile(r"\d")


def password_policy_errors(password: str, email: Optional[str] = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(password, str):
        return ["Senha inválida."]
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"A senha deve ter no mínimo {PASSWORD_MIN_LENGTH} caracteres.")
    if len(password) > PASSWORD_MAX_LENGTH:
        errors.append(f"A senha deve ter no máximo {PASSWORD_MAX_LENGTH} caracteres.")
    if not _LETTER.search(password) or not _DIGIT.search(password):
        errors.append("A senha deve conter letras e números.")
    if email:
        low = password.lower()
        e = email.lower().strip()
        local = e.split("@", 1)[0]
        if e and e in low or (len(local) >= 3 and local in low):
            errors.append("A senha não pode conter o e-mail.")
    return errors


def validate_password_policy(password: str, email: Optional[str] = None) -> None:
    errors = password_policy_errors(password, email)
    if errors:
        raise DomainError("PASSWORD_POLICY", errors[0], status=400, details={"errors": errors})
