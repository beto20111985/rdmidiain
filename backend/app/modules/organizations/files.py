"""
RD Mídia Indoor — Adaptador fino de arquivos do domínio W-ORG sobre o StorageProvider
(``app.core.storage`` — W-MEDIA).

- Fotos de instalação e assinatura: somente JPEG/PNG, tipo real por MAGIC BYTES e
  decodificação completa com Pillow (arquivo corrompido/disfarçado é rejeitado);
  limite de tamanho aplicado durante a leitura.
- Documentos de onboarding: PDF (``%PDF-``) ou JPEG/PNG.
- Chaves SEMPRE geradas pelo servidor (``build_key``); nome do arquivo do usuário nunca
  é usado.
- Objeto gravado numa transação que falhar é removido (hook ``after_rollback``), para
  não deixar órfãos no storage.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import UploadFile
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.storage import StoredObject, build_key, get_storage

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_SIGNATURE_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_READ_CHUNK = 256 * 1024


@dataclass
class ImageInfo:
    content_type: str
    width: int
    height: int


async def read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await upload.read(_READ_CHUNK)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_bytes:
            raise DomainError("FILE_TOO_LARGE", "Arquivo maior que o permitido.", status=413,
                              details={"max_bytes": max_bytes})
    if not buf:
        raise DomainError("FILE_EMPTY", "Arquivo vazio.", status=400)
    return bytes(buf)


def sniff_image(data: bytes) -> ImageInfo:
    if data[:3] == b"\xff\xd8\xff":
        ctype, fmt = "image/jpeg", "JPEG"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ctype, fmt = "image/png", "PNG"
    else:
        raise DomainError("FILE_TYPE_INVALID", "Envie uma imagem JPEG ou PNG.", status=415)
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            if im.format != fmt:
                raise ValueError("formato divergente")
            w, h = im.size
            im.load()  # decodifica por completo (detecta truncamento)
        with Image.open(io.BytesIO(data)) as im2:
            im2.verify()
    except DomainError:
        raise
    except Exception:
        raise DomainError("FILE_CORRUPTED", "Imagem corrompida ou inválida.", status=415) from None
    if w < 2 or h < 2 or w > 12000 or h > 12000:
        raise DomainError("IMAGE_DIMENSIONS_INVALID", "Dimensões de imagem inválidas.", status=415)
    return ImageInfo(ctype, w, h)


def sniff_document(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        if b"%%EOF" not in data[-2048:]:
            raise DomainError("FILE_CORRUPTED", "PDF incompleto ou corrompido.", status=415)
        return "application/pdf"
    return sniff_image(data).content_type


def _ensure_cleanup_hook(db: AsyncSession) -> list:
    info = db.info
    keys = info.get("rd_org_pending_keys")
    if keys is None:
        keys = []
        info["rd_org_pending_keys"] = keys
        sync = db.sync_session

        def _rollback(session) -> None:  # noqa: ANN001
            pending = session.info.get("rd_org_pending_keys") or []
            st = get_storage()
            for k in list(pending):
                try:
                    st.delete_blocking(k)
                except Exception:  # noqa: BLE001
                    pass
            pending.clear()

        def _commit(session) -> None:  # noqa: ANN001
            (session.info.get("rd_org_pending_keys") or []).clear()

        event.listen(sync, "after_rollback", _rollback)
        event.listen(sync, "after_commit", _commit)
    return keys


async def store(db: Optional[AsyncSession], key: str, data: bytes, content_type: str,
                max_bytes: Optional[int] = None) -> StoredObject:
    obj = await get_storage().put_stream(key, data, content_type, max_bytes=max_bytes)
    if db is not None:
        _ensure_cleanup_hook(db).append(obj.key)
    return obj


def key_for(*parts: Any) -> str:
    return build_key(*parts)
