"""
RD Mídia Indoor — Política de mídia (tipos, limites, resoluções) e parâmetros técnicos.

Fontes: RESPOSTAS RODRIGO #324–#357; ETAPA2 #324–#357; P001 §25–§29; P002 item 23.

- Tipos: SOMENTE imagem (JPG, PNG, GIF) e vídeo (MP4; WebM e outros contêineres de
  vídeo — MOV, MKV, AVI, MPEG-TS — são CONVERTIDOS para MP4). WebP, HEIC/AVIF, áudio,
  PDF, HTML etc. são recusados.
- Resoluções aceitas (já considerando rotação/EXIF): 1920×1080 (horizontal),
  1080×1920 (vertical) e 4K — UHD 3840×2160/2160×3840 (escala exata ÷2) e DCI
  4096×2160/2160×4096 (ajuste sem esticar + tarja preta). Qualquer outra → REJEITADA
  (abaixo de Full HD: ``RESOLUTION_BELOW_FULL_HD``; demais: ``RESOLUTION_NOT_ALLOWED``).
- Tamanho: imagem ≤ ``MAX_IMAGE_SIZE_MB`` MiB (10), vídeo ≤ ``MAX_VIDEO_SIZE_MB`` MiB
  (200) — fonte única: ``app.core.config`` (aplicado DURANTE o streaming).
- Duração do vídeo: sem limite de negócio aqui (a regra 30/60/120 s × slot é do
  W-CAMP/W-INV com ``duration_ms`` exato); há um teto TÉCNICO anti-abuso
  (``MEDIA_MAX_VIDEO_DURATION_S``, padrão 3600 s).
- GIF: estático → normalizado para PNG; ANIMADO → continua IMAGEM (``image/gif``,
  decisão documentada: o proprietário classifica GIF como imagem; a duração de exibição
  é a do criativo, a animação repete em loop — nada é cortado; os players web/Windows/
  Android já renderizam ``image/gif`` animado).

Parâmetros técnicos: variáveis de ambiente com prefixo ``MEDIA_`` (``MediaSettings``).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import settings as core_settings

MIB = 1024 * 1024


class MediaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEDIA_", case_sensitive=False, extra="ignore")

    FFMPEG_BIN: str = "ffmpeg"
    FFPROBE_BIN: str = "ffprobe"
    X264_PRESET: str = "medium"
    X264_CRF: int = 21
    MAXRATE: str = "8M"
    BUFSIZE: str = "16M"
    FFMPEG_THREADS: int = 2
    PROBE_TIMEOUT_S: int = 30
    TRANSCODE_TIMEOUT_MIN_S: int = 300
    TRANSCODE_TIMEOUT_FACTOR: float = 8.0  # × duração do vídeo
    TRANSCODE_TIMEOUT_MAX_S: int = 3000  # < soft_time_limit do Celery (3300 s)
    MAX_VIDEO_DURATION_S: int = 3600
    MAX_FPS: int = 30
    PREVIEW_MAX_S: int = 120
    PREVIEW_MAX_SIDE: int = 640
    PREVIEW_FPS: int = 15
    MAX_GIF_FRAMES: int = 3000
    PROC_MEM_LIMIT_MB: int = 4096  # RLIMIT_AS do ffmpeg/ffprobe
    PROC_FSIZE_LIMIT_MB: int = 2048  # RLIMIT_FSIZE do ffmpeg (maior arquivo que ele pode gravar)
    # Lease CURTO renovado por heartbeat enquanto o pipeline progride (BUG-QA-01): após queda
    # /reinício do worker a mídia volta a processar em ≈ LEASE_S + intervalo do beat
    # media-maintenance (60 s) + backoff — poucos minutos, sem processamento duplicado.
    LEASE_S: int = 90
    HEARTBEAT_S: float = 15.0  # renovação do lease (efetivo: ≤ LEASE_S/3)
    MAX_ATTEMPTS: int = 3
    RETRY_BASE_S: int = 30
    REQUEUE_BACKOFF_MAX_S: int = 120  # teto do backoff do reenfileiramento após queda do worker
    KEEP_SOURCE: bool = False  # manter o original após READY
    GC_GRACE_HOURS: int = 24
    STALE_UPLOADED_S: int = 180  # UPLOADED parado há mais que isso é reenfileirado (≥ backoff + 60 s)
    UPLOAD_IDLE_TIMEOUT_S: int = 60
    MIN_FREE_BYTES: int = 512 * MIB
    MULTIPART_OVERHEAD_BYTES: int = 1 * MIB


@lru_cache
def media_settings() -> MediaSettings:
    return MediaSettings()


def heartbeat_interval_s() -> float:
    ms = media_settings()
    return max(0.1, min(float(ms.HEARTBEAT_S), ms.LEASE_S / 3.0))


def requeue_backoff_s(attempts: int) -> int:
    """Espera antes de reprocessar após queda do worker (30 s, 60 s, … até o teto)."""
    ms = media_settings()
    return int(min(ms.REQUEUE_BACKOFF_MAX_S, ms.RETRY_BASE_S * (2 ** max(0, int(attempts) - 1))))


def stale_uploaded_s() -> int:
    ms = media_settings()
    return int(max(ms.STALE_UPLOADED_S, ms.REQUEUE_BACKOFF_MAX_S + 60))


# ─── Tipos ───────────────────────────────────────────────────────────────────

IMAGE_FORMATS = {"jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif"}
VIDEO_FORMATS = {
    "mp4": "video/mp4",
    "quicktime": "video/quicktime",
    "webm": "video/webm",
    "matroska": "video/x-matroska",
    "avi": "video/x-msvideo",
    "mpegts": "video/mp2t",
}
# demuxer FORÇADO no ffprobe/ffmpeg (nunca autodetecção → evita demuxers perigosos)
FFMPEG_DEMUXER = {
    "mp4": "mov",
    "quicktime": "mov",
    "webm": "matroska",
    "matroska": "matroska",
    "avi": "avi",
    "mpegts": "mpegts",
    "gif": "gif",
}

IMAGE_EXTENSIONS = {"jpg", "jpeg", "jpe", "jfif", "png", "gif"}
VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "qt", "webm", "mkv", "avi", "ts", "mts", "m2ts"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# extensão → formato "natural" (para registrar extension_mismatch)
EXTENSION_FORMAT = {
    "jpg": "jpeg", "jpeg": "jpeg", "jpe": "jpeg", "jfif": "jpeg", "png": "png", "gif": "gif",
    "mp4": "mp4", "m4v": "mp4", "mov": "quicktime", "qt": "quicktime", "webm": "webm", "mkv": "matroska",
    "avi": "avi", "ts": "mpegts", "mts": "mpegts", "m2ts": "mpegts",
}


def kind_of_format(fmt: str) -> str:
    return "IMAGE" if fmt in IMAGE_FORMATS else "VIDEO"


def mime_of_format(fmt: str) -> str:
    return IMAGE_FORMATS.get(fmt) or VIDEO_FORMATS[fmt]


# ─── Limites ─────────────────────────────────────────────────────────────────

def max_image_bytes() -> int:
    return int(core_settings.MAX_IMAGE_SIZE_MB) * MIB


def max_video_bytes() -> int:
    return int(core_settings.MAX_VIDEO_SIZE_MB) * MIB


def max_bytes_for_kind(kind: str) -> int:
    return max_image_bytes() if kind == "IMAGE" else max_video_bytes()


def max_upload_bytes() -> int:
    return max(max_image_bytes(), max_video_bytes())


# ─── Resoluções ──────────────────────────────────────────────────────────────

FHD_H = (1920, 1080)
FHD_V = (1080, 1920)


@dataclass(frozen=True)
class ResolutionDecision:
    ok: bool
    target_w: int = 0
    target_h: int = 0
    orientation: Optional[str] = None
    scaled: bool = False  # precisou reduzir (4K → FHD)
    padded: bool = False  # proporção diferente de 16:9 → tarja preta (DCI 4K)
    code: Optional[str] = None
    message: Optional[str] = None


ACCEPTED_RESOLUTIONS = {
    (1920, 1080): (FHD_H, False, False),
    (1080, 1920): (FHD_V, False, False),
    (3840, 2160): (FHD_H, True, False),
    (2160, 3840): (FHD_V, True, False),
    (4096, 2160): (FHD_H, True, True),
    (2160, 4096): (FHD_V, True, True),
}


def decide_resolution(w: int, h: int) -> ResolutionDecision:
    """Decide a saída para a resolução de EXIBIÇÃO (após rotação/EXIF/SAR)."""
    hit = ACCEPTED_RESOLUTIONS.get((int(w), int(h)))
    if hit:
        (tw, th), scaled, padded = hit
        return ResolutionDecision(True, tw, th, "HORIZONTAL" if tw > th else "VERTICAL", scaled, padded)
    long_side, short_side = max(w, h), min(w, h)
    if long_side < 1920 or short_side < 1080:
        return ResolutionDecision(
            False,
            code="RESOLUTION_BELOW_FULL_HD",
            message=f"Resolução {w}×{h} abaixo de Full HD. Envie 1920×1080 (horizontal), 1080×1920 (vertical) ou 4K.",
        )
    return ResolutionDecision(
        False,
        code="RESOLUTION_NOT_ALLOWED",
        message=f"Resolução {w}×{h} não aceita. Envie 1920×1080 (horizontal), 1080×1920 (vertical) ou 4K "
                f"(3840×2160 / 2160×3840).",
    )


_STANDARD_RATES = ((24000, 1001), (24, 1), (25, 1), (30000, 1001), (30, 1), (50, 1), (60000, 1001), (60, 1))


def _snap(num: int, den: int) -> tuple[int, int]:
    """Taxa média ligeiramente irregular (VFR de celular: 29,87) → taxa padrão mais próxima (±2%)."""
    v = num / den
    best = min(_STANDARD_RATES, key=lambda r: abs(v - r[0] / r[1]))
    c = best[0] / best[1]
    if abs(v - c) / c <= 0.02:
        return best
    return num, den


def target_fps(src_num: int, src_den: int) -> tuple[int, int]:
    """fps de saída como fração: preserva até MAX_FPS; acima disso divide por 2 quando cabe
    (60→30, 59,94→29,97, 50→25), senão MAX_FPS. A duração é sempre preservada (filtro fps)."""
    mx = media_settings().MAX_FPS
    if src_num <= 0 or src_den <= 0:
        return mx, 1
    src_num, src_den = _snap(src_num, src_den)
    if src_num <= mx * src_den:
        return src_num, src_den
    half_num, half_den = src_num, src_den * 2
    if half_num <= mx * half_den:
        from fractions import Fraction

        f = Fraction(half_num, half_den)
        return f.numerator, f.denominator
    return mx, 1


def policy_summary() -> dict:
    """Requisitos exibidos ao usuário (UI de upload — REQ-MID-011)."""
    return {
        "image": {
            "formats": ["JPG", "PNG", "GIF"],
            "extensions": sorted(IMAGE_EXTENSIONS),
            "max_bytes": max_image_bytes(),
        },
        "video": {
            "formats": ["MP4", "WebM (convertido)", "MOV/MKV/AVI/TS (convertidos)"],
            "extensions": sorted(VIDEO_EXTENSIONS),
            "max_bytes": max_video_bytes(),
            "audio": "removido automaticamente",
            "output": "MP4 H.264 sem áudio, até 30 fps",
        },
        "resolutions": [
            {"width": 1920, "height": 1080, "orientation": "HORIZONTAL"},
            {"width": 1080, "height": 1920, "orientation": "VERTICAL"},
            {"width": 3840, "height": 2160, "orientation": "HORIZONTAL", "converted_to": "1920x1080"},
            {"width": 2160, "height": 3840, "orientation": "VERTICAL", "converted_to": "1080x1920"},
            {"width": 4096, "height": 2160, "orientation": "HORIZONTAL", "converted_to": "1920x1080"},
            {"width": 2160, "height": 4096, "orientation": "VERTICAL", "converted_to": "1080x1920"},
        ],
        "not_allowed": ["WebP", "HEIC/AVIF", "áudio", "PDF", "HTML", "resolução abaixo de Full HD"],
    }
