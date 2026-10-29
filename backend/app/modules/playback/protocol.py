"""
RD Mídia Indoor — Primitivas CANÔNICAS do RD Player Protocol v1 (RDPP/1 §0).

Implementação de referência (backend) usada por TODOS os domínios que assinam ou
verificam objetos do protocolo (manifesto — W-INV; pareamento/autenticação/PoP/
app-update — W-PLAY) e pelo gerador dos vetores de conformidade
(``app/modules/playback/vectors.py`` → ``protocol/vectors/*.json``).

RDJ1 (JSON canônico):
- objeto: chaves ordenadas por ponto de código; sem espaços (``,`` e ``:``);
- strings: escapes mínimos (``\\"``, ``\\\\``, ``\\b``, ``\\f``, ``\\n``, ``\\r``, ``\\t``;
  demais controles U+0000–U+001F como ``\\u00xx`` minúsculo); não-ASCII sem escape;
- SOMENTE inteiros (float → ``CanonicalError("FLOAT_NOT_ALLOWED")``), booleanos, ``null``;
- bytes assinados = UTF-8 do texto canônico.

ES256: ECDSA P-256 + SHA-256; assinatura no formato BRUTO r‖s (64 bytes) em
base64url sem padding; chave pública = SPKI DER em base64url sem padding.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any, Optional, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

ALG = "ES256"
SIGNATURE_LEN = 64
ZERO_HASH = "0" * 64

# Propósitos (domain separation) — RDPP §2.5, §3.2, §8
PURPOSE_AUTH = "rd-player-auth-v1"
PURPOSE_PAIRING_RESULT = "rd-pairing-result-v1"
PURPOSE_APP_UPDATE = "rd-app-update-v1"


class CanonicalError(ValueError):
    """Valor fora do subconjunto RDJ1. ``code`` é estável (usado nos vetores)."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class ProtocolCryptoError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


# ─── base64url ───────────────────────────────────────────────────────────────

_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*={0,2}$")


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """Decodifica base64url (sem padding; padding tolerado). Alfabeto estrito."""
    if not isinstance(text, str) or not _B64U_RE.match(text):
        raise ProtocolCryptoError("BAD_BASE64URL", "base64url inválido")
    s = text.rstrip("=")
    if len(s) % 4 == 1:
        raise ProtocolCryptoError("BAD_BASE64URL", "base64url inválido")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError):
        raise ProtocolCryptoError("BAD_BASE64URL", "base64url inválido") from None


# ─── RDJ1 ────────────────────────────────────────────────────────────────────

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _escape_str(s: str) -> str:
    out = ['"']
    for ch in s:
        esc = _ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        elif 0xD800 <= ord(ch) <= 0xDFFF:
            raise CanonicalError("INVALID_STRING", "string com surrogate isolado")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _canon(value: Any, out: list[str], depth: int) -> None:
    if depth > 64:
        raise CanonicalError("TOO_DEEP", "estrutura aninhada demais")
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(int(value)))
    elif isinstance(value, float):
        raise CanonicalError("FLOAT_NOT_ALLOWED", "RDJ1 aceita somente inteiros")
    elif isinstance(value, str):
        out.append(_escape_str(value))
    elif isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise CanonicalError("NON_STRING_KEY", "chaves de objeto devem ser strings")
        out.append("{")
        first = True
        for k in sorted(value):
            if not first:
                out.append(",")
            first = False
            out.append(_escape_str(k))
            out.append(":")
            _canon(value[k], out, depth + 1)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _canon(item, out, depth + 1)
        out.append("]")
    else:
        raise CanonicalError("UNSUPPORTED_TYPE", f"tipo não suportado: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Texto RDJ1 do valor."""
    out: list[str] = []
    _canon(value, out, 0)
    return "".join(out)


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex_of(value: Any) -> str:
    """SHA-256 (hex minúsculo) do RDJ1 do valor — usado em ``prev_hash`` do PoP."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# ─── Chaves ──────────────────────────────────────────────────────────────────

PrivateKey = ec.EllipticCurvePrivateKey
PublicKey = ec.EllipticCurvePublicKey


def generate_private_key() -> PrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def load_private_key_pem(data: Union[bytes, str], password: Optional[bytes] = None) -> PrivateKey:
    if isinstance(data, str):
        data = data.encode("ascii")
    key = serialization.load_pem_private_key(data, password=password)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ProtocolCryptoError("BAD_KEY", "chave privada deve ser EC P-256")
    return key


def load_private_key_pkcs8_b64u(text: str) -> PrivateKey:
    key = serialization.load_der_private_key(b64u_decode(text), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ProtocolCryptoError("BAD_KEY", "chave privada deve ser EC P-256")
    return key


def private_pkcs8_b64u(key: PrivateKey) -> str:
    return b64u_encode(
        key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )


def public_spki_der(key: Union[PrivateKey, PublicKey]) -> bytes:
    pub = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def public_spki_b64u(key: Union[PrivateKey, PublicKey]) -> str:
    return b64u_encode(public_spki_der(key))


def load_public_spki(spki_b64u: str) -> PublicKey:
    """Carrega e VALIDA uma chave pública SPKI (DER base64url) EC P-256 (ES256)."""
    if not isinstance(spki_b64u, str) or not (40 <= len(spki_b64u) <= 200):
        raise ProtocolCryptoError("BAD_PUBLIC_KEY", "chave pública inválida")
    der = b64u_decode(spki_b64u)
    try:
        key = serialization.load_der_public_key(der)
    except (ValueError, TypeError):
        raise ProtocolCryptoError("BAD_PUBLIC_KEY", "chave pública inválida") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ProtocolCryptoError("BAD_PUBLIC_KEY", "a chave deve ser EC P-256 (ES256)")
    return key


def normalize_public_spki(spki_b64u: str) -> tuple[str, str]:
    """Valida e normaliza a SPKI → ``(spki_b64u_canônica, fingerprint_sha256_hex)``."""
    key = load_public_spki(spki_b64u)
    der = public_spki_der(key)
    return b64u_encode(der), hashlib.sha256(der).hexdigest()


# ─── ES256 ───────────────────────────────────────────────────────────────────

def sign_bytes(key: PrivateKey, data: bytes, *, deterministic: bool = False) -> str:
    """ES256 r‖s. ``deterministic=True`` = RFC 6979 (usado SOMENTE nos vetores de conformidade)."""
    der = key.sign(data, ec.ECDSA(hashes.SHA256(), deterministic_signing=deterministic))
    r, s = decode_dss_signature(der)
    return b64u_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def sign_es256(key: PrivateKey, obj: Any, *, deterministic: bool = False) -> str:
    """Assinatura ES256 (r‖s, base64url) sobre RDJ1(obj)."""
    return sign_bytes(key, canonical_bytes(obj), deterministic=deterministic)


def verify_bytes(public_key: Union[PublicKey, str], data: bytes, signature_b64u: str) -> bool:
    if not isinstance(signature_b64u, str) or "=" in signature_b64u:
        return False  # base64url SEM padding (RDPP §0)
    try:
        pub = load_public_spki(public_key) if isinstance(public_key, str) else public_key
        raw = b64u_decode(signature_b64u)
    except ProtocolCryptoError:
        return False
    if len(raw) != SIGNATURE_LEN:
        return False
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    if r == 0 or s == 0:
        return False
    try:
        pub.verify(encode_dss_signature(r, s), data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_es256(public_key: Union[PublicKey, str], obj: Any, signature_b64u: str) -> bool:
    """Verifica ES256 sobre RDJ1(obj). Objeto fora do RDJ1 → False."""
    try:
        data = canonical_bytes(obj)
    except CanonicalError:
        return False
    return verify_bytes(public_key, data, signature_b64u)


def is_b64u_bytes(text: Any, n: int) -> bool:
    """``text`` é base64url (sem padding) de exatamente ``n`` bytes?"""
    if not isinstance(text, str) or "=" in text:
        return False
    try:
        return len(b64u_decode(text)) == n
    except ProtocolCryptoError:
        return False


__all__ = [
    "ALG",
    "PURPOSE_AUTH",
    "PURPOSE_PAIRING_RESULT",
    "PURPOSE_APP_UPDATE",
    "ZERO_HASH",
    "CanonicalError",
    "ProtocolCryptoError",
    "b64u_encode",
    "b64u_decode",
    "canonical_json",
    "canonical_bytes",
    "sha256_hex_of",
    "generate_private_key",
    "load_private_key_pem",
    "load_private_key_pkcs8_b64u",
    "private_pkcs8_b64u",
    "public_spki_der",
    "public_spki_b64u",
    "load_public_spki",
    "normalize_public_spki",
    "sign_bytes",
    "sign_es256",
    "verify_bytes",
    "verify_es256",
    "is_b64u_bytes",
]
