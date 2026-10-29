"""Recebimento seguro de arquivos do módulo de design (anexos de briefing e versões da arte).

- Tipo decidido pelos MAGIC BYTES (nunca pela extensão/Content-Type do cliente).
- Gravação em streaming no ``StorageProvider`` (W-MEDIA) com limite aplicado durante a
  cópia; chave gerada pelo servidor; nome original apenas sanitizado para exibição.
"""
from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from fastapi import UploadFile

from app.core.errors import DomainError

MIB = 1024 * 1024
CHUNK = 256 * 1024

FORMATS = {
    "png": ("image/png", "IMAGEM"),
    "jpeg": ("image/jpeg", "IMAGEM"),
    "gif": ("image/gif", "IMAGEM"),
    "webp": ("image/webp", "IMAGEM"),
    "svg": ("image/svg+xml", "IMAGEM"),
    "pdf": ("application/pdf", "DOCUMENTO"),
    "mp4": ("video/mp4", "VIDEO"),
    "webm": ("video/webm", "VIDEO"),
    "txt": ("text/plain", "TEXTO"),
}
EXT = {"png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp", "svg": "svg", "pdf": "pdf", "mp4": "mp4",
       "webm": "webm", "txt": "txt"}
ATTACHMENT_LIMITS = {"IMAGEM": 10 * MIB, "DOCUMENTO": 20 * MIB, "VIDEO": 200 * MIB, "TEXTO": 1 * MIB}


def detect(head: bytes) -> Optional[str]:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    try:
        txt = head.decode("utf-8")
    except UnicodeDecodeError:
        return None
    t = txt.lstrip("﻿ \t\r\n").lower()
    if t.startswith("<?xml") or t.startswith("<svg"):
        return "svg" if "<svg" in t else None
    if not re.search(r"[\x00-\x08\x0e-\x1f]", txt):
        return "txt"
    return None


_CTRL = re.compile(r"[\x00-\x1f\x7f/\\]")


def display_name(raw: Optional[str], fmt: str) -> str:
    base = _CTRL.sub("_", (raw or "arquivo").strip())[:180] or "arquivo"
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return f"{stem}.{EXT[fmt]}"


@dataclass
class Stored:
    key: str
    sha256: str
    size: int
    fmt: str
    content_type: str
    category: str
    filename: str
    width: Optional[int] = None
    height: Optional[int] = None


async def _chunks(first: bytes, f: UploadFile) -> AsyncIterator[bytes]:
    if first:
        yield first
    while True:
        b = await f.read(CHUNK)
        if not b:
            break
        yield b


async def store_upload(f: UploadFile, key_parts: tuple, *, allowed: set[str], limits: dict[str, int]) -> Stored:
    from app.core.storage import ObjectTooLarge, build_key, get_storage

    first = await f.read(CHUNK)
    if not first:
        raise DomainError("FILE_EMPTY", "Arquivo vazio.", status=400)
    fmt = detect(first[:4096])
    if fmt is None or fmt not in allowed:
        raise DomainError("FILE_TYPE_NOT_ALLOWED", "Tipo de arquivo não permitido.", status=415,
                          details={"allowed": sorted(allowed)})
    content_type, category = FORMATS[fmt]
    limit = limits.get(category, 10 * MIB)
    key = build_key(*key_parts, f"{uuid.uuid4().hex}.{EXT[fmt]}")
    try:
        obj = await get_storage().put_stream(key, _chunks(first, f), content_type, max_bytes=limit)
    except ObjectTooLarge:
        raise DomainError("FILE_TOO_LARGE", f"Arquivo excede o limite de {limit // MIB} MB.", status=413,
                          details={"max_bytes": limit}) from None
    return Stored(key=obj.key, sha256=obj.sha256, size=obj.size, fmt=fmt, content_type=content_type,
                  category=category, filename=display_name(f.filename, fmt))


async def store_bytes(data: bytes, key_parts: tuple, fmt: str, filename: str) -> Stored:
    from app.core.storage import build_key, get_storage

    content_type, category = FORMATS[fmt]
    key = build_key(*key_parts, f"{uuid.uuid4().hex}.{EXT[fmt]}")
    obj = await get_storage().put_stream(key, data, content_type)
    return Stored(key=obj.key, sha256=obj.sha256, size=obj.size, fmt=fmt, content_type=content_type,
                  category=category, filename=display_name(filename, fmt))


def image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        im.verify()
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def delete_on_rollback(db, key: str) -> None:
    """Remove o objeto gravado se a transação da request for desfeita (sem órfãos no storage)."""
    from sqlalchemy import event

    from app.core.storage import get_storage

    sync = db.sync_session
    keys = sync.info.setdefault("rd_design_keys", [])
    keys.append(key)
    if not sync.info.get("rd_design_keys_hooked"):
        def _rb(session):  # noqa: ANN001
            for k in session.info.pop("rd_design_keys", []):
                try:
                    get_storage().delete_blocking(k)
                except Exception:
                    pass

        def _ok(session):  # noqa: ANN001
            session.info.pop("rd_design_keys", None)

        event.listen(sync, "after_rollback", _rb)
        event.listen(sync, "after_commit", _ok)
        sync.info["rd_design_keys_hooked"] = True
