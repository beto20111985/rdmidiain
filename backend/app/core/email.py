"""
RD Mídia Indoor — Envio de e-mail.

Interface ``EmailSender`` (``async send(message)``) com implementações:
- ``SmtpEmailSender``  — aiosmtplib (``SMTP_HOST``/``SMTP_PORT``; em dev: Mailpit
  ``mailpit:1025`` sem TLS). STARTTLS por padrão fora da porta 25/1025.
- ``LogEmailSender``   — não envia; registra apenas destinatário mascarado e
  assunto (NUNCA o corpo, que pode conter códigos).
- ``MemoryEmailSender``— caixa de saída em memória (testes).

``get_email_sender()`` devolve o sender configurado (``EMAIL_BACKEND``) e pode ser
substituído em testes via ``set_email_sender``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Optional, Protocol

import structlog

from app.core.config import settings

log = structlog.get_logger("rd.email")


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    text: str
    html: Optional[str] = None
    tags: dict = field(default_factory=dict)
    # anexos: (nome_do_arquivo, mime "tipo/subtipo", conteúdo)
    attachments: list[tuple[str, str, bytes]] = field(default_factory=list)


MAX_ATTACHMENTS_BYTES = 20 * 1024 * 1024


def build_mime(message: OutgoingEmail, sender: Optional[str] = None) -> EmailMessage:
    """Monta a mensagem MIME (texto, HTML opcional e anexos)."""
    msg = EmailMessage()
    name, addr = parseaddr(sender or settings.SMTP_FROM)
    msg["From"] = formataddr((name, addr))
    msg["To"] = message.to
    msg["Subject"] = message.subject
    msg.set_content(message.text)
    if message.html:
        msg.add_alternative(message.html, subtype="html")
    total = 0
    for filename, mime, content in message.attachments:
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("conteúdo de anexo deve ser bytes")
        total += len(content)
        if total > MAX_ATTACHMENTS_BYTES:
            raise ValueError("anexos excedem o limite de 20 MB")
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        safe_name = re.sub(r"[\r\n/\\\x00]", "_", filename or "anexo")[:150]
        msg.add_attachment(bytes(content), maintype=maintype, subtype=subtype or "octet-stream", filename=safe_name)
    return msg


class EmailSender(Protocol):
    async def send(self, message: OutgoingEmail) -> None: ...


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    return (local[:2] + "***") + "@" + domain


class LogEmailSender:
    async def send(self, message: OutgoingEmail) -> None:
        log.warning("email_not_sent_no_smtp", to=mask_email(message.to), subject=message.subject)


class MemoryEmailSender:
    def __init__(self) -> None:
        self.outbox: list[OutgoingEmail] = []

    async def send(self, message: OutgoingEmail) -> None:
        self.outbox.append(message)

    def last_to(self, email: str) -> Optional[OutgoingEmail]:
        for m in reversed(self.outbox):
            if m.to.lower() == email.lower():
                return m
        return None


class SmtpEmailSender:
    async def send(self, message: OutgoingEmail) -> None:
        import aiosmtplib

        msg = build_mime(message)
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER or None,
            password=settings.SMTP_PASSWORD or None,
            start_tls=bool(settings.SMTP_STARTTLS),
            timeout=settings.SMTP_TIMEOUT_S,
        )
        log.info("email_sent", to=mask_email(message.to), subject=message.subject,
                 attachments=len(message.attachments))


_override: Optional[EmailSender] = None


def set_email_sender(sender: Optional[EmailSender]) -> None:
    global _override
    _override = sender


def get_email_sender() -> EmailSender:
    if _override is not None:
        return _override
    backend = settings.EMAIL_BACKEND
    if backend == "smtp":
        return SmtpEmailSender()
    if backend == "memory":
        return MemoryEmailSender()
    return LogEmailSender()


async def send_safely(message: OutgoingEmail) -> bool:
    """Envia e registra falha sem propagar (usado após a resposta, sem enumeração)."""
    try:
        await get_email_sender().send(message)
        return True
    except Exception as exc:  # falha registrada, nunca silenciosa
        log.error("email_send_failed", to=mask_email(message.to), subject=message.subject, error_type=type(exc).__name__)
        return False
