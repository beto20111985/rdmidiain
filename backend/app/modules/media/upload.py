"""
RD Mídia Indoor — Leitura em STREAMING de ``multipart/form-data`` (upload de mídia).

O ``UploadFile`` do Starlette grava o corpo inteiro em arquivo temporário ANTES do
handler — o limite só seria aplicado depois. Aqui o corpo é lido em blocos direto
de ``request.stream()`` e entregue ao ``StorageProvider`` à medida que chega:
limite aplicado durante o streaming, magic bytes nos primeiros bytes, SHA-256
incremental, sem o arquivo inteiro em RAM (PU §41; REQ-MID-003/010).

Formato aceito: exatamente UMA parte de arquivo (nome de campo ``file``) e campos de
texto curtos opcionais (``name``, ``org_id``) — antes ou depois do arquivo.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

from fastapi import Request

from app.core.errors import DomainError

try:  # python-multipart ≥ 0.0.13
    from python_multipart.multipart import MultipartParser, parse_options_header  # type: ignore
except ImportError:  # pragma: no cover
    from multipart.multipart import MultipartParser, parse_options_header  # type: ignore

MAX_FIELDS = 10
MAX_FIELD_BYTES = 4096
MAX_HEADER_BYTES = 8192


def _malformed(msg: str = "Envio mal formado (multipart/form-data esperado).") -> DomainError:
    return DomainError("MEDIA_MALFORMED_UPLOAD", msg, status=400)


class MultipartFileStream:
    def __init__(self, request: Request, *, idle_timeout_s: float = 60.0) -> None:
        ctype = request.headers.get("content-type", "")
        mtype, params = parse_options_header(ctype)
        boundary = params.get(b"boundary")
        if mtype != b"multipart/form-data" or not boundary or len(boundary) > 200:
            raise _malformed()
        self._body = request.stream().__aiter__()
        self._idle = idle_timeout_s
        self.fields: dict[str, str] = {}
        self.filename: Optional[str] = None
        self.file_content_type: Optional[str] = None
        self._queue: list[bytes] = []
        self._in_file = False
        self._file_started = False
        self._file_done = False
        self._ended = False
        self._error: Optional[DomainError] = None
        self._hdr_field = bytearray()
        self._hdr_value = bytearray()
        self._headers: dict[bytes, bytes] = {}
        self._field_name: Optional[str] = None
        self._field_buf = bytearray()
        self._header_bytes = 0
        callbacks = {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
            "on_end": self._on_end,
        }
        self._parser = MultipartParser(boundary, callbacks)

    # ── callbacks (síncronos, chamados por parser.write) ──
    def _fail(self, err: DomainError) -> None:
        if self._error is None:
            self._error = err

    def _on_part_begin(self) -> None:
        self._headers = {}
        self._hdr_field.clear()
        self._hdr_value.clear()
        self._header_bytes = 0

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_bytes += end - start
        if self._header_bytes > MAX_HEADER_BYTES:
            self._fail(_malformed())
            return
        self._hdr_field += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_bytes += end - start
        if self._header_bytes > MAX_HEADER_BYTES:
            self._fail(_malformed())
            return
        self._hdr_value += data[start:end]

    def _on_header_end(self) -> None:
        self._headers[bytes(self._hdr_field).lower()] = bytes(self._hdr_value)
        self._hdr_field.clear()
        self._hdr_value.clear()

    def _on_headers_finished(self) -> None:
        disp, opts = parse_options_header(self._headers.get(b"content-disposition", b""))
        if disp != b"form-data" or b"name" not in opts:
            self._fail(_malformed())
            return
        name = opts[b"name"].decode("utf-8", "replace")
        if b"filename" in opts:
            if self._file_started:
                self._fail(DomainError("MEDIA_SINGLE_FILE_ONLY", "Envie um arquivo por vez.", status=400))
                return
            if name != "file":
                self._fail(_malformed("O arquivo deve ser enviado no campo 'file'."))
                return
            self._file_started = True
            self._in_file = True
            self.filename = opts[b"filename"].decode("utf-8", "replace")
            ct = self._headers.get(b"content-type")
            self.file_content_type = ct.decode("latin-1", "replace")[:100] if ct else None
        else:
            if len(self.fields) >= MAX_FIELDS:
                self._fail(_malformed("Campos demais no envio."))
                return
            self._field_name = name[:64]
            self._field_buf = bytearray()

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._error is not None:
            return
        if self._in_file:
            self._queue.append(bytes(data[start:end]))
        elif self._field_name is not None:
            self._field_buf += data[start:end]
            if len(self._field_buf) > MAX_FIELD_BYTES:
                self._fail(_malformed("Campo de texto grande demais."))

    def _on_part_end(self) -> None:
        if self._in_file:
            self._in_file = False
            self._file_done = True
        elif self._field_name is not None:
            self.fields[self._field_name] = bytes(self._field_buf).decode("utf-8", "replace").strip()
            self._field_name = None

    def _on_end(self) -> None:
        self._ended = True

    # ── leitura ──
    async def _feed(self) -> bool:
        """Lê o próximo bloco do corpo e alimenta o parser. False = fim do corpo."""
        try:
            chunk = await asyncio.wait_for(self._body.__anext__(), timeout=self._idle)
        except StopAsyncIteration:
            try:
                self._parser.finalize()
            except Exception:
                raise _malformed() from None
            return False
        except asyncio.TimeoutError:
            raise DomainError("MEDIA_UPLOAD_TIMEOUT", "Envio interrompido (tempo sem dados excedido).", status=408) from None
        except Exception:  # cliente desconectou
            raise DomainError("MEDIA_UPLOAD_INCOMPLETE", "Envio interrompido antes de terminar.", status=400) from None
        if chunk:
            try:
                self._parser.write(chunk)
            except Exception:
                raise _malformed() from None
        if self._error is not None:
            raise self._error
        return True

    async def wait_file(self) -> None:
        """Lê até o início da parte de arquivo (campos anteriores ficam em ``fields``)."""
        while not self._file_started:
            if not await self._feed():
                raise DomainError("MEDIA_FILE_REQUIRED", "Nenhum arquivo enviado (campo 'file').", status=400)

    async def file_chunks(self) -> AsyncIterator[bytes]:
        """Blocos do arquivo, na ordem; depois lê o resto do corpo (campos posteriores)."""
        while True:
            if self._queue:
                data = b"".join(self._queue)
                self._queue.clear()
                yield data
            if self._file_done:
                break
            if not await self._feed():
                if not self._file_done:
                    raise DomainError("MEDIA_UPLOAD_INCOMPLETE", "Envio interrompido antes de terminar.", status=400)
        if self._queue:
            data = b"".join(self._queue)
            self._queue.clear()
            yield data

    async def finish(self) -> None:
        """Consome o restante do corpo (campos depois do arquivo; limitado)."""
        while await self._feed():
            pass
        if not self._ended:
            raise _malformed()
