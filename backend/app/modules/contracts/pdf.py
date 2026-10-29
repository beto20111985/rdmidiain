"""
RD Mídia Indoor — PDF do contrato aceito (reportlab, gerado no servidor).

O PDF reproduz o texto exato da versão aceita e o REGISTRO DO ACEITE (versão, hash
SHA-256 do texto, usuário, IP, user-agent, data/hora UTC e local). Não é assinatura
digital certificada (P001 §113): o aceite vale pelo login do usuário no sistema
(ETAPA2 #1157).
"""
from __future__ import annotations

import html
import io
from datetime import datetime
from typing import Optional

from app.core.timeutils import isoformat_z, to_local


def _p(text: str) -> str:
    return html.escape(text or "").replace("\n", "<br/>")


def render_acceptance_pdf(
    *,
    acceptance_id: str,
    title: str,
    kind_label: str,
    version: int,
    body: str,
    body_sha256: str,
    org_name: str,
    org_document: str,
    establishment_name: Optional[str],
    signer_name: str,
    signer_email: Optional[str],
    method_label: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
    accepted_at: datetime,
    timezone_name: str,
    extra_lines: Optional[list[str]] = None,
) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=title, author="RD Mídia Indoor", subject=f"{kind_label} v{version}", creator="RD Mídia Indoor",
    )
    ss = getSampleStyleSheet()
    story = [
        Paragraph(_p(title), ss["Title"]),
        Paragraph(_p(f"{kind_label} — versão {version}"), ss["Heading3"]),
        Spacer(1, 4 * mm),
    ]
    for para in (body or "").split("\n\n"):
        story.append(Paragraph(_p(para), ss["BodyText"]))
        story.append(Spacer(1, 2 * mm))
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("Registro do aceite", ss["Heading2"]))
    local = to_local(accepted_at, timezone_name)
    lines = [
        f"Identificador do aceite: {acceptance_id}",
        f"Forma: {method_label}",
        f"Contratante: {org_name} — {org_document}",
    ]
    if establishment_name:
        lines.append(f"Estabelecimento: {establishment_name}")
    lines += [
        f"Aceito por: {signer_name}" + (f" ({signer_email})" if signer_email else ""),
        f"Data/hora (UTC): {isoformat_z(accepted_at)}",
        f"Data/hora local ({timezone_name}): {local.strftime('%d/%m/%Y %H:%M:%S')}",
        f"Endereço IP: {ip_address or '—'}",
        f"Navegador (user-agent): {(user_agent or '—')[:200]}",
        f"Versão do texto: {version} — SHA-256 do texto: {body_sha256}",
        "O aceite eletrônico foi realizado mediante login pessoal no sistema RD Mídia Indoor; "
        "não constitui assinatura digital certificada (ICP-Brasil).",
    ]
    lines += list(extra_lines or [])
    for ln in lines:
        story.append(Paragraph(_p(ln), ss["BodyText"]))
    doc.build(story)
    return buf.getvalue()
