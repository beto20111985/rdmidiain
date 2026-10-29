"""Magic bytes, extensões, política de resolução/fps e parser de Range (sem banco)."""
from __future__ import annotations

import io

import pytest

from app.modules.media import policy
from app.modules.media.service import _UNSAT, parse_range
from app.modules.media.sniff import (
    SniffRejected,
    check_extension,
    extension_matches,
    sanitize_filename,
    sniff,
)


def _png_bytes(w=4, h=4) -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (w, h)).save(b, "PNG")
    return b.getvalue()


def _ftyp(brand: bytes, compat=(b"isom",)) -> bytes:
    body = brand + b"\x00\x00\x02\x00" + b"".join(compat)
    return (8 + len(body)).to_bytes(4, "big") + b"ftyp" + body + b"\x00" * 64


@pytest.mark.parametrize(
    "head,fmt,kind",
    [
        (b"\xff\xd8\xff\xe0" + b"\x00" * 100, "jpeg", "IMAGE"),
        (b"GIF89a" + b"\x00" * 100, "gif", "IMAGE"),
        (b"GIF87a" + b"\x00" * 100, "gif", "IMAGE"),
        (_ftyp(b"isom"), "mp4", "VIDEO"),
        (_ftyp(b"mp42"), "mp4", "VIDEO"),
        (_ftyp(b"qt  ", (b"qt  ",)), "quicktime", "VIDEO"),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"webm" + b"\x00" * 40, "webm", "VIDEO"),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"matroska" + b"\x00" * 40, "matroska", "VIDEO"),
        (b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 40, "avi", "VIDEO"),
        ((b"\x47" + b"\x00" * 187) * 3, "mpegts", "VIDEO"),
    ],
)
def test_sniff_accepts_known_formats(head, fmt, kind):
    s = sniff(head)
    assert (s.format, s.kind) == (fmt, kind)


def test_sniff_png_real():
    s = sniff(_png_bytes())
    assert s.format == "png" and s.mime == "image/png"


@pytest.mark.parametrize(
    "head,detected",
    [
        (b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 40, "webp"),
        (_ftyp(b"heic", (b"mif1", b"heic")), "heif"),
        (_ftyp(b"avif", (b"avif", b"mif1")), "heif"),
        (_ftyp(b"M4A ", (b"M4A ",)), "m4a"),
        (b"PK\x03\x04" + b"\x00" * 50, "zip"),
        (b"%PDF-1.7\n" + b"\x00" * 50, "pdf"),
        (b"MZ\x90\x00" + b"\x00" * 50, "exe"),
        (b"\x7fELF" + b"\x00" * 50, "elf"),
        (b"ID3\x04" + b"\x00" * 50, "mp3"),
        (b"<html><body>oi</body></html>", "text"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "text"),
        (b"Este arquivo eh texto puro, nao video. " * 5, "text"),
        (b"BM" + b"\x00" * 60, "bmp"),
        (bytes(range(256)) * 2, "unknown"),
    ],
)
def test_sniff_rejects(head, detected):
    with pytest.raises(SniffRejected) as ei:
        sniff(head)
    assert ei.value.code == "MEDIA_TYPE_NOT_ALLOWED"
    assert ei.value.detected == detected


def test_sniff_empty():
    with pytest.raises(SniffRejected) as ei:
        sniff(b"")
    assert ei.value.code == "MEDIA_EMPTY"


@pytest.mark.parametrize("name", ["a.jpg", "A.JPEG", "x.png", "y.gif", "v.mp4", "v.webm", "v.MOV", "v.mkv", "v.avi", "v.ts"])
def test_extensions_allowed(name):
    assert check_extension(name)


@pytest.mark.parametrize("name", ["a.webp", "a.exe", "a.php", "a.svg", "a.html", "noext", "a.mp3", "a.pdf", "a."])
def test_extensions_rejected(name):
    with pytest.raises(SniffRejected) as ei:
        check_extension(name)
    assert ei.value.code == "MEDIA_EXTENSION_NOT_ALLOWED"


def test_extension_matches():
    assert extension_matches("jpg", "jpeg")
    assert not extension_matches("jpg", "png")
    assert extension_matches("mov", "mp4") and extension_matches("mkv", "webm")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd.png", "passwd.png"),
        ("..\\..\\windows\\system32\\a.jpg", "a.jpg"),
        ("/abs/path/v.mp4", "v.mp4"),
        ("a\x00b\x1f.png", "ab.png"),
        ("", "arquivo"),
        ("...", "arquivo"),
        ("x" * 400 + ".png", "x" * 251 + ".png"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize(
    "w,h,ok,target,orient,scaled",
    [
        (1920, 1080, True, (1920, 1080), "HORIZONTAL", False),
        (1080, 1920, True, (1080, 1920), "VERTICAL", False),
        (3840, 2160, True, (1920, 1080), "HORIZONTAL", True),
        (2160, 3840, True, (1080, 1920), "VERTICAL", True),
        (4096, 2160, True, (1920, 1080), "HORIZONTAL", True),
        (1280, 720, False, None, None, None),
        (1919, 1080, False, None, None, None),
        (2560, 1440, False, None, None, None),
        (1920, 1200, False, None, None, None),
        (7680, 4320, False, None, None, None),
    ],
)
def test_resolution_policy(w, h, ok, target, orient, scaled):
    d = policy.decide_resolution(w, h)
    assert d.ok is ok
    if ok:
        assert (d.target_w, d.target_h) == target and d.orientation == orient and d.scaled is scaled
    else:
        assert d.code in ("RESOLUTION_BELOW_FULL_HD", "RESOLUTION_NOT_ALLOWED")


def test_below_full_hd_code():
    assert policy.decide_resolution(1280, 720).code == "RESOLUTION_BELOW_FULL_HD"
    assert policy.decide_resolution(720, 1280).code == "RESOLUTION_BELOW_FULL_HD"
    assert policy.decide_resolution(2560, 1440).code == "RESOLUTION_NOT_ALLOWED"


@pytest.mark.parametrize(
    "src,expected",
    [
        ((30, 1), (30, 1)),
        ((25, 1), (25, 1)),
        ((30000, 1001), (30000, 1001)),
        ((60, 1), (30, 1)),
        ((60000, 1001), (30000, 1001)),
        ((50, 1), (25, 1)),
        ((120, 1), (30, 1)),
        ((2987, 100), (30000, 1001)),  # VFR de celular → taxa padrão mais próxima
        ((2499, 100), (25, 1)),
        ((0, 0), (30, 1)),
        ((12, 1), (12, 1)),
    ],
)
def test_target_fps(src, expected):
    assert policy.target_fps(*src) == expected


def test_configured_limits_are_10_and_200_mb():
    from app.core.config import Settings, settings

    assert Settings.model_fields["MAX_IMAGE_SIZE_MB"].default == 10
    assert Settings.model_fields["MAX_VIDEO_SIZE_MB"].default == 200
    assert settings.MAX_IMAGE_SIZE_MB == 10 and settings.MAX_VIDEO_SIZE_MB == 200
    assert policy.max_image_bytes() == 10 * 1024 * 1024
    assert policy.max_video_bytes() == 200 * 1024 * 1024
    assert policy.max_bytes_for_kind("VIDEO") == 200 * 1024 * 1024


@pytest.mark.parametrize(
    "header,size,expected",
    [
        (None, 100, None),
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=10-", 100, (10, 99)),
        ("bytes=-10", 100, (90, 99)),
        ("bytes=-1000", 100, (0, 99)),
        ("bytes=90-1000", 100, (90, 99)),
        ("bytes=100-", 100, _UNSAT),
        ("bytes=-0", 100, _UNSAT),
        ("bytes=5-2", 100, None),
        ("bytes=0-1,5-6", 100, None),
        ("items=0-1", 100, None),
        ("bytes=abc", 100, None),
    ],
)
def test_parse_range(header, size, expected):
    assert parse_range(header, size) == expected
