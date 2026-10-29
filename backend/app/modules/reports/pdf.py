"""
Geração de PDF (reportlab) compartilhada pelos módulos do W-BIZ: proposta comercial,
relatórios exportados e comprovante de veiculação.

- Fonte DejaVu Sans (acentos/símbolos) quando disponível no sistema; senão Helvetica.
- Documento descrito por blocos simples (``Heading``, ``Para``, ``KeyValues``,
  ``Table``, ``PageBreak``) — nada de HTML/JS do usuário: textos são escapados.
- ``render_pdf`` devolve ``bytes`` (``%PDF``…``%%EOF``).
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from typing import Optional, Sequence, Union
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, PageBreak as _PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
_FONTS: Optional[tuple[str, str]] = None

INK = colors.HexColor("#0F172A")
MUTED = colors.HexColor("#475569")
BRAND = colors.HexColor("#1D4ED8")
GRID = colors.HexColor("#CBD5E1")
ZEBRA = colors.HexColor("#F1F5F9")


def fonts() -> tuple[str, str]:
    """(regular, bold) — registra DejaVu uma única vez."""
    global _FONTS
    if _FONTS is None:
        reg, bold = os.path.join(FONT_DIR, "DejaVuSans.ttf"), os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
        if os.path.exists(reg) and os.path.exists(bold):
            pdfmetrics.registerFont(TTFont("RDSans", reg))
            pdfmetrics.registerFont(TTFont("RDSans-Bold", bold))
            _FONTS = ("RDSans", "RDSans-Bold")
        else:  # pragma: no cover - ambiente sem DejaVu
            _FONTS = ("Helvetica", "Helvetica-Bold")
    return _FONTS


@dataclass
class Heading:
    text: str
    level: int = 1


@dataclass
class Para:
    text: str
    small: bool = False
    muted: bool = False


@dataclass
class KeyValues:
    pairs: Sequence[tuple[str, str]]


@dataclass
class Table:
    headers: Sequence[str]
    rows: Sequence[Sequence[object]]
    col_widths: Optional[Sequence[float]] = None  # em mm
    align_right: Sequence[int] = ()  # índices de colunas numéricas


@dataclass
class PageBreak:
    pass


Block = Union[Heading, Para, KeyValues, Table, PageBreak]


@dataclass
class PdfDoc:
    title: str
    subtitle: Optional[str] = None
    blocks: list = field(default_factory=list)
    brand_name: str = "RD Mídia Indoor"  # fallback RD; chamadores passam ``Brand.pdf_kwargs()`` da instância
    brand_color: Optional[str] = None  # cor principal da instância (#RRGGBB; contraste ≥ 4,5:1 com branco)
    logo: Optional[bytes] = None  # PNG/JPG da logo da instância (opcional)
    footer: Optional[str] = None
    landscape: bool = False
    author: Optional[str] = None
    keywords: Optional[str] = None


def _styles():
    reg, bold = fonts()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName=bold, fontSize=17, leading=21, textColor=INK,
                                alignment=TA_LEFT, spaceAfter=2 * mm),
        "subtitle": ParagraphStyle("s", parent=base["Normal"], fontName=reg, fontSize=10, leading=13, textColor=MUTED,
                                   spaceAfter=4 * mm),
        "h1": ParagraphStyle("h1", parent=base["Heading2"], fontName=bold, fontSize=12.5, leading=16, textColor=INK,
                             spaceBefore=4 * mm, spaceAfter=2 * mm),
        "h2": ParagraphStyle("h2", parent=base["Heading3"], fontName=bold, fontSize=11, leading=14, textColor=INK,
                             spaceBefore=3 * mm, spaceAfter=1.5 * mm),
        "p": ParagraphStyle("p", parent=base["Normal"], fontName=reg, fontSize=9.5, leading=13, textColor=INK),
        "small": ParagraphStyle("sm", parent=base["Normal"], fontName=reg, fontSize=8, leading=10.5, textColor=INK),
        "muted": ParagraphStyle("mu", parent=base["Normal"], fontName=reg, fontSize=8, leading=10.5, textColor=MUTED),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontName=reg, fontSize=8, leading=10, textColor=INK),
        "cellr": ParagraphStyle("cr", parent=base["Normal"], fontName=reg, fontSize=8, leading=10, textColor=INK,
                                alignment=2),
        "head": ParagraphStyle("hd", parent=base["Normal"], fontName=bold, fontSize=8, leading=10, textColor=colors.white),
        "key": ParagraphStyle("k", parent=base["Normal"], fontName=bold, fontSize=9, leading=12, textColor=MUTED),
    }


def _color(hex_color: Optional[str]):
    try:
        return colors.HexColor(hex_color) if hex_color else BRAND
    except (ValueError, TypeError):
        return BRAND


def _logo(data: Optional[bytes]):
    if not data:
        return None
    try:
        from reportlab.lib.utils import ImageReader

        r = ImageReader(io.BytesIO(data))
        r.getSize()
        return r
    except Exception:  # logo inválida nunca impede o documento
        return None


def _txt(v: object) -> str:
    return escape("—" if v is None or v == "" else str(v)).replace("\n", "<br/>")


def render_pdf(doc: PdfDoc) -> bytes:
    st = _styles()
    reg, bold = fonts()
    accent = _color(doc.brand_color)
    logo_reader = _logo(doc.logo)
    buf = io.BytesIO()
    pagesize = landscape(A4) if doc.landscape else A4
    usable_w = pagesize[0] - 30 * mm

    def _decorate(canvas, d):  # noqa: ANN001
        canvas.saveState()
        x = 15 * mm
        if logo_reader is not None:
            iw, ih = logo_reader.getSize()
            h = 7 * mm
            w = min(40 * mm, h * iw / ih if ih else h)
            canvas.drawImage(logo_reader, x, pagesize[1] - 12.5 * mm, width=w, height=h, mask="auto",
                             preserveAspectRatio=True)
            x += w + 3 * mm
        canvas.setFont(bold, 9)
        canvas.setFillColor(accent)
        canvas.drawString(x, pagesize[1] - 10 * mm, doc.brand_name)
        canvas.setStrokeColor(accent)
        canvas.setLineWidth(0.8)
        canvas.line(15 * mm, pagesize[1] - 13.5 * mm, pagesize[0] - 15 * mm, pagesize[1] - 13.5 * mm)
        canvas.setFont(reg, 7.5)
        canvas.setFillColor(MUTED)
        if doc.footer:
            canvas.drawString(15 * mm, 8 * mm, doc.footer[:180])
        canvas.drawRightString(pagesize[0] - 15 * mm, 8 * mm, f"Página {d.page}")
        canvas.restoreState()

    tpl = SimpleDocTemplate(
        buf, pagesize=pagesize, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=16 * mm, bottomMargin=15 * mm,
        title=doc.title, author=doc.author or doc.brand_name, subject=doc.subtitle or "", creator=doc.brand_name,
        keywords=doc.keywords or "",
    )
    flow: list = [Paragraph(_txt(doc.title), st["title"])]
    if doc.subtitle:
        flow.append(Paragraph(_txt(doc.subtitle), st["subtitle"]))
    for b in doc.blocks:
        if isinstance(b, Heading):
            flow.append(Paragraph(_txt(b.text), st["h1" if b.level <= 1 else "h2"]))
        elif isinstance(b, Para):
            flow.append(Paragraph(_txt(b.text), st["muted" if b.muted else ("small" if b.small else "p")]))
            flow.append(Spacer(1, 1.5 * mm))
        elif isinstance(b, KeyValues):
            data = [[Paragraph(_txt(k), st["key"]), Paragraph(_txt(v), st["p"])] for k, v in b.pairs]
            if data:
                t = LongTable(data, colWidths=[usable_w * 0.32, usable_w * 0.68])
                t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                       ("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID),
                                       ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                                       ("TOPPADDING", (0, 0), (-1, -1), 3)]))
                flow.append(t)
                flow.append(Spacer(1, 2 * mm))
        elif isinstance(b, Table):
            head = [Paragraph(_txt(h), st["head"]) for h in b.headers]
            body = []
            for row in b.rows:
                body.append([
                    Paragraph(_txt(c), st["cellr"] if i in b.align_right else st["cell"]) for i, c in enumerate(row)
                ])
            widths = [w * mm for w in b.col_widths] if b.col_widths else [usable_w / max(1, len(b.headers))] * len(b.headers)
            t = LongTable([head] + (body or [[Paragraph("Sem dados no período.", st["cell"])] + [""] * (len(head) - 1)]),
                          colWidths=widths, repeatRows=1)
            style = [
                ("BACKGROUND", (0, 0), (-1, 0), accent),
                ("GRID", (0, 0), (-1, -1), 0.25, GRID),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ]
            for r in range(2, len(body) + 1, 2):
                style.append(("BACKGROUND", (0, r), (-1, r), ZEBRA))
            t.setStyle(TableStyle(style))
            flow.append(t)
            flow.append(Spacer(1, 3 * mm))
        elif isinstance(b, PageBreak):
            flow.append(_PageBreak())
    tpl.build(flow, onFirstPage=_decorate, onLaterPages=_decorate)
    return buf.getvalue()
