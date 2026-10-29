"""RD Mídia Indoor — Schemas (OpenAPI) da biblioteca de mídia."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MediaSourceOut(BaseModel):
    format: str = Field(description="Formato REAL detectado pelo conteúdo: jpeg|png|gif|mp4|quicktime|webm|matroska|avi|mpegts")
    mime: str
    size_bytes: int
    sha256: str
    width: Optional[int] = Field(None, description="Largura de exibição (após rotação/EXIF)")
    height: Optional[int] = None
    duration_ms: Optional[int] = None
    codec: Optional[str] = None
    fps_milli: Optional[int] = Field(None, description="fps × 1000 (29970 = 29,97 fps)")
    has_audio: Optional[bool] = Field(None, description="O ARQUIVO ENVIADO tinha áudio (a saída nunca tem)")


class MediaOutputOut(BaseModel):
    mime_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    duration_ms: Optional[int] = Field(None, description="Duração exata medida (ffprobe) — nula para imagem")
    codec: str
    fps_milli: Optional[int] = None
    orientation: str = Field(description="HORIZONTAL (1920×1080) | VERTICAL (1080×1920)")
    animated: bool = False
    frame_count: Optional[int] = None
    has_audio: bool = False


class MediaOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    original_filename: str
    origin: str = Field(description="UPLOAD | DESIGN_ART | TEMPLATE_RENDER | …")
    kind: str = Field(description="IMAGE | VIDEO")
    status: str = Field(description="UPLOADED | VALIDATING | PROCESSING | READY | REJECTED | FAILED")
    status_code: Optional[str] = Field(None, description="Motivo (REJECTED/FAILED), ex.: RESOLUTION_BELOW_FULL_HD")
    status_message: Optional[str] = None
    progress_pct: int
    attempts: int
    extension_mismatch: bool = Field(description="Extensão do nome ≠ tipo real (o tipo real prevalece)")
    source: MediaSourceOut
    output: Optional[MediaOutputOut] = Field(None, description="Presente somente quando READY")
    has_preview: bool
    content_url: Optional[str] = None
    preview_url: Optional[str] = None
    created_by: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime
    ready_at: Optional[datetime] = None


class MediaUploadOut(MediaOut):
    duplicate: bool = Field(False, description="True quando o mesmo arquivo já existia na organização (idempotência)")


class MediaPage(BaseModel):
    items: list[MediaOut]
    total: int
    page: int
    page_size: int


class MediaPatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class MediaUsageOut(BaseModel):
    media_id: uuid.UUID
    in_use: bool
    references: dict[str, int]


class MediaDeleteIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)
