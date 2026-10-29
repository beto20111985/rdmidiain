"""
RD Mídia Indoor — Detecção do tipo REAL pelo conteúdo (magic bytes).

Nunca confiar em extensão/Content-Type (PU §40, §58; P001 §26). Usado nos primeiros
bytes do upload (streaming) — antes de aceitar o resto do corpo — e de novo no
worker sobre o arquivo armazenado. Implementação própria (sem libmagic): só
reconhecemos o que aceitamos e alguns formatos conhecidos para dar mensagem clara.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from app.modules.media.policy import (
    ALLOWED_EXTENSIONS,
    EXTENSION_FORMAT,
    kind_of_format,
    mime_of_format,
)

SNIFF_BYTES = 4096

_ISO_IMAGE_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1",
                     b"avif", b"avis", b"jxl ", b"crx "}
_ISO_AUDIO_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B "}
_QT_ATOMS = {b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"}


@dataclass(frozen=True)
class Sniffed:
    format: str  # jpeg|png|gif|mp4|quicktime|webm|matroska|avi|mpegts
    kind: str  # IMAGE|VIDEO
    mime: str


class SniffRejected(Exception):
    def __init__(self, code: str, message: str, detected: Optional[str] = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.detected = detected


def _reject(detected: str, label: str) -> SniffRejected:
    return SniffRejected(
        "MEDIA_TYPE_NOT_ALLOWED",
        f"Tipo de arquivo não aceito ({label}). Envie imagem JPG, PNG ou GIF, ou vídeo (MP4, WebM e outros).",
        detected,
    )


def sniff(head: bytes) -> Sniffed:
    """Identifica o formato pelos primeiros bytes (≥ 512 recomendado). Lança ``SniffRejected``."""
    if len(head) == 0:
        raise SniffRejected("MEDIA_EMPTY", "Arquivo vazio.")
    fmt = _detect(head)
    return Sniffed(fmt, kind_of_format(fmt), mime_of_format(fmt))


def _detect(h: bytes) -> str:
    if h[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(h) >= 12 and h[4:8] == b"ftyp":
        brand = h[8:12]
        size = int.from_bytes(h[0:4], "big")
        compat = {h[i:i + 4] for i in range(16, min(max(size, 16), len(h), 256), 4)} if size >= 16 else set()
        brands = {brand} | compat
        if brand in _ISO_IMAGE_BRANDS or (brands & _ISO_IMAGE_BRANDS and not brands & {b"isom", b"mp41", b"mp42"}):
            raise _reject("heif", "HEIC/AVIF")
        if brand in _ISO_AUDIO_BRANDS:
            raise _reject("m4a", "áudio")
        if brand == b"qt  ":
            return "quicktime"
        return "mp4"
    if len(h) >= 8 and h[4:8] in _QT_ATOMS:
        return "quicktime"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "webm" if b"webm" in h[:SNIFF_BYTES] else "matroska"
    if h[:4] == b"RIFF" and len(h) >= 12:
        sub = h[8:12]
        if sub == b"AVI ":
            return "avi"
        if sub == b"WEBP":
            raise _reject("webp", "WebP")
        if sub == b"WAVE":
            raise _reject("wav", "áudio")
        raise _reject("riff", "RIFF desconhecido")
    if _is_mpegts(h):
        return "mpegts"
    # formatos conhecidos (mensagem clara) — todos recusados
    known = (
        (b"%PDF", "pdf", "PDF"),
        (b"PK\x03\x04", "zip", "ZIP/Office"),
        (b"PK\x05\x06", "zip", "ZIP"),
        (b"MZ", "exe", "executável"),
        (b"\x7fELF", "elf", "executável"),
        (b"BM", "bmp", "BMP"),
        (b"II*\x00", "tiff", "TIFF"),
        (b"MM\x00*", "tiff", "TIFF"),
        (b"ID3", "mp3", "áudio"),
        (b"OggS", "ogg", "Ogg"),
        (b"fLaC", "flac", "áudio"),
        (b"FLV", "flv", "FLV"),
        (b"\x1f\x8b", "gzip", "compactado"),
        (b"Rar!", "rar", "compactado"),
        (b"7z\xbc\xaf", "7z", "compactado"),
        (b"\x00\x00\x01\xba", "mpeg-ps", "MPEG-PS"),
    )
    for sig, det, label in known:
        if h.startswith(sig):
            raise _reject(det, label)
    if h[:2] == b"\xff\xfb" or h[:2] == b"\xff\xf3":
        raise _reject("mp3", "áudio")
    stripped = h.lstrip()[:16].lower()
    if stripped.startswith((b"<", b"{", b"[")) or _looks_text(h):
        raise _reject("text", "texto/HTML/SVG")
    raise _reject("unknown", "desconhecido")


def _is_mpegts(h: bytes) -> bool:
    if len(h) >= 377 and h[0] == 0x47 and h[188] == 0x47 and h[376] == 0x47:
        return True
    if len(h) >= 389 and h[4] == 0x47 and h[196] == 0x47 and h[388] == 0x47:  # M2TS (192 bytes/pacote)
        return True
    return False


def _looks_text(h: bytes) -> bool:
    sample = h[:512]
    if not sample:
        return False
    printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127)
    return printable / len(sample) > 0.95


# ─── Nome de arquivo ─────────────────────────────────────────────────────────

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_filename(raw: Optional[str]) -> str:
    """Nome para EXIBIÇÃO apenas (nunca caminho): basename, sem controle, NFC, ≤ 255."""
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CTRL.sub("", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if len(name) > 255:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 255 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 16 else name[:255]
    return name or "arquivo"


def extension_of(filename: str) -> Optional[str]:
    base = filename.rsplit("/", 1)[-1]
    if "." not in base:
        return None
    ext = base.rsplit(".", 1)[-1].lower()
    return ext[:16] or None


def check_extension(filename: str) -> str:
    ext = extension_of(filename)
    if not ext or ext not in ALLOWED_EXTENSIONS:
        raise SniffRejected(
            "MEDIA_EXTENSION_NOT_ALLOWED",
            "Extensão de arquivo não aceita. Use .jpg, .png, .gif, .mp4, .webm (ou outro vídeo comum).",
            ext,
        )
    return ext


def extension_matches(ext: Optional[str], fmt: str) -> bool:
    natural = EXTENSION_FORMAT.get(ext or "")
    if natural is None:
        return False
    if natural == fmt:
        return True
    # mp4/mov compartilham o contêiner ISO-BMFF; webm/mkv o Matroska
    return {natural, fmt} in ({"mp4", "quicktime"}, {"webm", "matroska"})


def head_of_file(path: "os.PathLike[str] | str", n: int = SNIFF_BYTES) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)
