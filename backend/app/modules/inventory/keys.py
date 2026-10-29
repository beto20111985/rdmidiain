"""
RD Mídia Indoor — Chave ES256 de assinatura de MANIFESTO (W-INV é o dono).

Origem da chave (em ordem):
1. ``settings.MANIFEST_SIGNING_KEY`` — PEM PKCS8 EC P-256, normalmente carregado de
   ``MANIFEST_SIGNING_KEY_FILE`` (Docker secret) pela configuração do W1;
2. desenvolvimento: gerada UMA vez e persistida em
   ``<STORAGE_LOCAL_PATH>/.rd-keys/manifest_es256.pem`` (diretório 0700, arquivo
   0600, volume ``media-storage`` presente na API e no worker; fora do GC/listagens
   do W-MEDIA). Caminho alternativo: ``MANIFEST_SIGNING_KEY_DEV_PATH``;
3. testes: chave efêmera em memória (nunca grava em disco);
4. produção sem chave configurada ⇒ erro (nunca assinar com chave improvisada).

A chave privada nunca é logada, devolvida por API ou gravada no banco. ``key_id`` =
``srv-`` + 12 hex do SHA-256 da SPKI (estável para a mesma chave).
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Optional

import structlog
from cryptography.hazmat.primitives import serialization

from app.core.config import settings
from app.modules.playback import protocol as P

log = structlog.get_logger("rd.inventory.keys")

_lock = threading.Lock()
_cached: Optional[tuple] = None  # (private_key, key_id, spki_b64u)


def _env_name() -> str:
    return str(getattr(settings.ENVIRONMENT, "value", settings.ENVIRONMENT)).lower()


def dev_key_path() -> Path:
    p = os.environ.get("MANIFEST_SIGNING_KEY_DEV_PATH")
    if p:
        return Path(p)
    return Path(settings.STORAGE_LOCAL_PATH) / ".rd-keys" / "manifest_es256.pem"


def _load_or_create_dev_key(path: Path):
    if path.exists():
        return P.load_private_key_pem(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:  # pragma: no cover - FS sem chmod
        pass
    key = P.generate_private_key()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # outro processo criou primeiro — usa a dele
        return P.load_private_key_pem(path.read_bytes())
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    log.info("manifest_dev_key_created", path=str(path))
    return key


def _build(key) -> tuple:
    spki = P.public_spki_b64u(key)
    kid = "srv-" + hashlib.sha256(P.public_spki_der(key)).hexdigest()[:12]
    return key, kid, spki


def signing_key() -> tuple:
    """(private_key, key_id, public_spki_b64u)."""
    global _cached
    if _cached is not None:
        return _cached
    with _lock:
        if _cached is not None:
            return _cached
        pem = (getattr(settings, "MANIFEST_SIGNING_KEY", "") or "").strip()
        env = _env_name()
        if pem:
            key = P.load_private_key_pem(pem.encode("ascii"))
        elif env == "test":
            key = P.generate_private_key()
        elif env in ("development", "dev", "local"):
            key = _load_or_create_dev_key(dev_key_path())
        else:
            raise RuntimeError("MANIFEST_SIGNING_KEY_FILE é obrigatório fora de desenvolvimento")
        _cached = _build(key)
        return _cached


def reset_cache() -> None:  # testes
    global _cached
    with _lock:
        _cached = None


def server_public_keys() -> list[dict]:
    _, kid, spki = signing_key()
    return [{"key_id": kid, "public_key": spki, "alg": P.ALG}]
