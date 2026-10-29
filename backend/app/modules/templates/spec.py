"""Especificação JSON (segura) dos templates e renderizador no servidor (Pillow).

Spec::

    {
      "background": {"color": "#05070A"},
      "variables": [{"name": "preco", "label": "Preço", "max_len": 20}],
      "layers": [
        {"type": "rect",  "box": {"col": 0, "row": 18, "col_span": 24, "row_span": 6}, "color": "#0B84FF", "opacity": 90},
        {"type": "image", "box": {...}, "asset_id": "<uuid>", "fit": "contain"},
        {"type": "text",  "box": {...}, "text": "Só hoje {{preco}}", "font": "dejavu-sans-bold",
         "size_px": 120, "color": "#FFFFFF", "align": "center", "valign": "middle", "max_lines": 2}
      ]
    }

- GRADE 24 × 24 sobre 1920×1080 (célula 80×45 px) ou 1080×1920 (45×80 px).
- Fontes permitidas: ``ALLOWED_FONTS`` (arquivos locais do servidor; nunca do usuário).
- Texto é desenhado como pixels (sem HTML/JS/markup). ``{{var}}`` só com variáveis
  declaradas; valores limitados por ``max_len``. Texto que não cabe é reduzido até o
  mínimo legível e, por fim, cortado com reticências (nunca transborda a caixa).
"""
from __future__ import annotations

import io
import os
import re
import uuid
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GRID = 24
CANVAS = {"HORIZONTAL": (1920, 1080), "VERTICAL": (1080, 1920)}
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
ALLOWED_FONTS = {
    "dejavu-sans": "DejaVuSans.ttf",
    "dejavu-sans-bold": "DejaVuSans-Bold.ttf",
    "dejavu-serif": "DejaVuSerif.ttf",
    "dejavu-serif-bold": "DejaVuSerif-Bold.ttf",
    "dejavu-mono": "DejaVuSansMono.ttf",
}
HEX = r"^#[0-9A-Fa-f]{6}$"
VAR_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,29})\s*\}\}")
MIN_FONT_PX = 12
MAX_ASSET_PIXELS = 8000 * 8000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Box(_Strict):
    col: int = Field(ge=0, le=GRID - 1)
    row: int = Field(ge=0, le=GRID - 1)
    col_span: int = Field(ge=1, le=GRID)
    row_span: int = Field(ge=1, le=GRID)

    @model_validator(mode="after")
    def _inside(self):
        if self.col + self.col_span > GRID or self.row + self.row_span > GRID:
            raise ValueError("a caixa ultrapassa a grade 24×24")
        return self


class Background(_Strict):
    color: str = Field(pattern=HEX)


class Variable(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,29}$")
    label: str = Field(min_length=1, max_length=60)
    max_len: int = Field(80, ge=1, le=300)
    default: Optional[str] = Field(None, max_length=300)


class TextLayer(_Strict):
    type: Literal["text"]
    box: Box
    text: str = Field(min_length=1, max_length=500)
    font: Literal["dejavu-sans", "dejavu-sans-bold", "dejavu-serif", "dejavu-serif-bold", "dejavu-mono"] = "dejavu-sans"
    size_px: int = Field(64, ge=MIN_FONT_PX, le=400)
    color: str = Field("#FFFFFF", pattern=HEX)
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "middle", "bottom"] = "top"
    max_lines: int = Field(3, ge=1, le=12)
    uppercase: bool = False


class ImageLayer(_Strict):
    type: Literal["image"]
    box: Box
    asset_id: uuid.UUID
    fit: Literal["contain", "cover"] = "contain"


class RectLayer(_Strict):
    type: Literal["rect"]
    box: Box
    color: str = Field(pattern=HEX)
    opacity: int = Field(100, ge=0, le=100)
    radius_px: int = Field(0, ge=0, le=200)


Layer = Union[TextLayer, ImageLayer, RectLayer]


class TemplateSpec(_Strict):
    background: Background
    variables: list[Variable] = Field(default_factory=list, max_length=20)
    layers: list[Layer] = Field(default_factory=list, max_length=30)

    @field_validator("layers", mode="before")
    @classmethod
    def _discriminate(cls, v):
        if not isinstance(v, list):
            raise ValueError("layers deve ser lista")
        out = []
        for item in v:
            t = item.get("type") if isinstance(item, dict) else None
            model = {"text": TextLayer, "image": ImageLayer, "rect": RectLayer}.get(t)
            if model is None:
                raise ValueError("tipo de camada inválido (text, image ou rect)")
            out.append(model.model_validate(item))
        return out

    @model_validator(mode="after")
    def _vars(self):
        names = [v.name for v in self.variables]
        if len(set(names)) != len(names):
            raise ValueError("variáveis repetidas")
        declared = set(names)
        for layer in self.layers:
            if isinstance(layer, TextLayer):
                used = set(VAR_RE.findall(layer.text))
                missing = used - declared
                if missing:
                    raise ValueError(f"variáveis não declaradas: {sorted(missing)}")
                if re.search(r"\{\{|\}\}", VAR_RE.sub("", layer.text)):
                    raise ValueError("use variáveis apenas no formato {{nome}}")
        return self

    def asset_ids(self) -> list[uuid.UUID]:
        return [l.asset_id for l in self.layers if isinstance(l, ImageLayer)]


def validate_spec(raw: dict) -> TemplateSpec:
    from pydantic import ValidationError

    from app.core.errors import DomainError

    try:
        return TemplateSpec.model_validate(raw)
    except ValidationError as exc:
        errs = [{"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", ""))} for e in exc.errors()]
        raise DomainError("TEMPLATE_SPEC_INVALID", "Template inválido.", status=400, details={"errors": errs[:20]}) from None


def resolve_variables(spec: TemplateSpec, values: dict) -> dict:
    from app.core.errors import DomainError

    unknown = set(values) - {v.name for v in spec.variables}
    if unknown:
        raise DomainError("TEMPLATE_VARIABLE_UNKNOWN", "Variável não existe neste template.", status=400,
                          details={"unknown": sorted(unknown)})
    out = {}
    for v in spec.variables:
        val = values.get(v.name, v.default or "")
        if not isinstance(val, str):
            raise DomainError("TEMPLATE_VARIABLE_INVALID", "Valores de variáveis devem ser texto.", status=400,
                              details={"variable": v.name})
        val = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", val)
        if len(val) > v.max_len:
            raise DomainError("TEMPLATE_VARIABLE_TOO_LONG", f"'{v.label}' aceita até {v.max_len} caracteres.",
                              status=400, details={"variable": v.name, "max_len": v.max_len})
        out[v.name] = val
    return out


# ─── Renderização (Pillow) ───────────────────────────────────────────────────

def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _px(box: Box, w: int, h: int) -> tuple[int, int, int, int]:
    cw, ch = w / GRID, h / GRID
    x0, y0 = round(box.col * cw), round(box.row * ch)
    x1, y1 = round((box.col + box.col_span) * cw), round((box.row + box.row_span) * ch)
    return x0, y0, x1, y1


def _font(name: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(os.path.join(FONT_DIR, ALLOWED_FONTS[name]), size=size)


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        cur = ""
        for word in words:
            cand = word if not cur else cur + " " + word
            if draw.textlength(cand, font=font) <= max_w:
                cur = cand
                continue
            if cur:
                lines.append(cur)
            # palavra maior que a linha: quebra por caractere
            while draw.textlength(word, font=font) > max_w and len(word) > 1:
                cut = len(word)
                while cut > 1 and draw.textlength(word[:cut], font=font) > max_w:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            cur = word
        lines.append(cur)
    return lines


def _draw_text(img, layer: TextLayer, text: str) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = _px(layer.box, *img.size)
    pad = 4
    max_w, max_h = max(1, x1 - x0 - 2 * pad), max(1, y1 - y0 - 2 * pad)
    if layer.uppercase:
        text = text.upper()
    size = layer.size_px
    while True:
        font = _font(layer.font, size)
        lines = _wrap(draw, text, font, max_w)
        ascent, descent = font.getmetrics()
        line_h = int((ascent + descent) * 1.15)
        if (len(lines) <= layer.max_lines and len(lines) * line_h <= max_h) or size <= MIN_FONT_PX:
            break
        size = max(MIN_FONT_PX, int(size * 0.9))
    fit_lines = max(1, min(layer.max_lines, max_h // max(1, line_h)))
    if len(lines) > fit_lines:
        lines = lines[:fit_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last + "…"
    total_h = len(lines) * line_h
    if layer.valign == "middle":
        y = y0 + pad + (max_h - total_h) // 2
    elif layer.valign == "bottom":
        y = y1 - pad - total_h
    else:
        y = y0 + pad
    color = _rgb(layer.color)
    for line in lines:
        lw = draw.textlength(line, font=font)
        if layer.align == "center":
            x = x0 + pad + (max_w - lw) / 2
        elif layer.align == "right":
            x = x1 - pad - lw
        else:
            x = x0 + pad
        draw.text((x, y), line, font=font, fill=color)
        y += line_h


def _draw_rect(img, layer: RectLayer) -> None:
    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = _px(layer.box, *img.size)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=layer.radius_px,
                                              fill=(*_rgb(layer.color), round(255 * layer.opacity / 100)))
    img.alpha_composite(overlay)


def _draw_image(img, layer: ImageLayer, data: bytes) -> None:
    from PIL import Image, ImageOps

    x0, y0, x1, y1 = _px(layer.box, *img.size)
    bw, bh = x1 - x0, y1 - y0
    with Image.open(io.BytesIO(data)) as src:
        src = src.convert("RGBA")
        if layer.fit == "cover":
            fitted = ImageOps.fit(src, (bw, bh))
        else:
            fitted = ImageOps.contain(src, (bw, bh))
    ox = x0 + (bw - fitted.width) // 2
    oy = y0 + (bh - fitted.height) // 2
    img.alpha_composite(fitted, (ox, oy))


def render(spec: TemplateSpec, orientation: str, variables: dict, assets: dict, fmt: str = "PNG") -> bytes:
    """Renderiza no servidor. ``assets``: {asset_id: bytes}. → bytes PNG/JPEG."""
    from PIL import Image

    w, h = CANVAS[orientation]
    img = Image.new("RGBA", (w, h), (*_rgb(spec.background.color), 255))
    for layer in spec.layers:
        if isinstance(layer, RectLayer):
            _draw_rect(img, layer)
        elif isinstance(layer, ImageLayer):
            _draw_image(img, layer, assets[layer.asset_id])
        else:
            text = VAR_RE.sub(lambda m: variables.get(m.group(1), ""), layer.text)
            if text.strip():
                _draw_text(img, layer, text)
    out = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
    else:
        img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()
