"""
RD Mídia Indoor — Criptografia de dados em repouso e HMAC de códigos.

- Chaves derivadas por HKDF-SHA256 de ``DATA_ENCRYPTION_KEY`` (se definida) ou de
  ``SECRET_KEY``, com ``info`` distinto por finalidade (separação de chaves).
- ``encrypt_str``/``decrypt_str``: AES-256-GCM, formato ``v1:<base64url(nonce|ct)>``,
  com AAD opcional (ex.: id do usuário — impede trocar o segredo de linha).
- ``hmac_hex``: HMAC-SHA256 para guardar códigos curtos (reset 6 dígitos, códigos de
  recuperação) — um vazamento do banco não permite força bruta offline sem a chave.
- ``sha256_hex``: para tokens opacos de alta entropia (refresh/convite).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

_SALT = b"rd-midiaindoor/v1"


@lru_cache(maxsize=16)
def derive_key(purpose: str) -> bytes:
    master = (settings.DATA_ENCRYPTION_KEY or settings.SECRET_KEY).encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=purpose.encode()).derive(master)


def encrypt_str(plaintext: str, *, purpose: str = "totp", aad: str = "") -> str:
    key = derive_key(purpose)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8") or None)
    return "v1:" + base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_str(token: str, *, purpose: str = "totp", aad: str = "") -> str:
    if not token or not token.startswith("v1:"):
        raise ValueError("formato de segredo cifrado inválido")
    raw = base64.urlsafe_b64decode(token[3:].encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(derive_key(purpose)).decrypt(nonce, ct, aad.encode("utf-8") or None).decode("utf-8")


def hmac_hex(message: str, *, purpose: str) -> str:
    return hmac.new(derive_key("hmac:" + purpose), message.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
