"""
RD Mídia Indoor — Armazenamento de objetos (``StorageProvider``).

Requisitos: P001 §29 (abstração desde o início; nenhuma regra depende de caminho
local), PU §53 (local/object storage/CDN, autorização, integridade, exclusão),
PU §41 (streaming, limite, limpeza após falha). Dono: W-MEDIA (AÇÃO 002).

USO (qualquer domínio)
----------------------
    from app.core.storage import get_storage, build_key

    st = get_storage()
    key = build_key("installations", org_id, photo_id, "photo.jpg")   # chave gerada pelo SERVIDOR
    obj = await st.put_stream(key, chunks, "image/jpeg", max_bytes=10 * 1024 * 1024)
    obj.key, obj.size, obj.sha256

    info = await st.stat(key)                       # ObjectStat(size, modified_at, ...)
    body = await st.open_range(key, 0, info.size - 1)   # abre JÁ (ObjectNotFound imediato)
    async for chunk in body: ...                    # leitura em blocos, nunca o arquivo inteiro
    await st.delete(key); await st.exists(key)

    async with st.materialize(key) as path: ...     # caminho local SOMENTE-LEITURA p/ ffmpeg etc.
    async with st.workspace("transcode") as tmpdir: ...   # diretório temporário removido ao sair
    await st.list_keys("media/<org>/<id>")          # chaves sob um prefixo-diretório
    st.remove_workspaces("media-<id>-")             # limpa workspaces de execução interrompida

Regras
------
- **Chaves são geradas pelo servidor** (``build_key``) — nunca use nome de arquivo
  enviado pelo usuário. Formato: segmentos ``[a-z0-9][a-z0-9._-]*`` separados por
  ``/``; sem ``..``, sem ``/`` inicial, sem barra invertida, sem maiúsculas, máx.
  512 caracteres. Chave inválida → ``InvalidStorageKey`` (``ValueError``).
- ``put_stream`` é ATÔMICO: grava em arquivo temporário (staging, fora da árvore de
  objetos) e só então faz ``rename``. Falha, abort ou ``max_bytes`` excedido →
  temporário apagado e nada aparece na chave. Calcula SHA-256 e tamanho de forma
  incremental. ``expected_sha256`` opcional confere a integridade.
- ``open_range(key, start, end)``: ``end`` é INCLUSIVO (semântica HTTP Range);
  ``end=None`` = até o fim. Faixa inválida → ``InvalidRange``.
- ``url_for_internal(key)``: URI OPACA para consumo interno servidor→servidor
  (``local://<key>`` ou URL pré-assinada curta do S3/MinIO). Nunca entregue ao
  navegador/player como URL pública e nunca a interprete como caminho.
- Seleção por ``STORAGE_PROVIDER``: ``local`` (padrão, volume ``STORAGE_LOCAL_PATH``)
  ou ``minio``/``s3`` (``S3StorageProvider`` — ESQUELETO, não validado contra um
  MinIO real nesta ação). Testes trocam o provedor com ``set_storage(...)``.

Layout do ``LocalStorageProvider`` (detalhe de implementação; ninguém depende dele):
``<root>/objects/<key>`` (objetos) e ``<root>/.staging/`` (temporários, mesmo
sistema de arquivos para ``rename`` atômico; nunca servido).
"""
from __future__ import annotations

import abc
import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterable, AsyncIterator, Iterable, Optional, Union

import structlog

log = structlog.get_logger("rd.storage")

DEFAULT_CHUNK_SIZE = 256 * 1024
_WRITE_BUFFER = 1024 * 1024
MAX_KEY_LENGTH = 512
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

ChunkSource = Union[bytes, bytearray, memoryview, AsyncIterable[bytes], Iterable[bytes]]


# ─── Erros ───────────────────────────────────────────────────────────────────

class StorageError(Exception):
    """Erro genérico de armazenamento (mensagem sem caminhos/segredos)."""


class InvalidStorageKey(StorageError, ValueError):
    pass


class ObjectNotFound(StorageError):
    pass


class InvalidRange(StorageError):
    def __init__(self, size: int) -> None:
        super().__init__("Faixa de bytes inválida.")
        self.size = size


class ObjectTooLarge(StorageError):
    def __init__(self, limit: int) -> None:
        super().__init__("Objeto excede o limite de tamanho.")
        self.limit = limit


class IntegrityMismatch(StorageError):
    pass


class StorageUnavailable(StorageError):
    pass


# ─── Estruturas ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    sha256: str
    content_type: Optional[str] = None


@dataclass(frozen=True)
class ObjectStat:
    key: str
    size: int
    modified_at: datetime
    content_type: Optional[str] = None
    sha256: Optional[str] = None  # quando o provedor guarda (S3: metadado)


# ─── Chaves ──────────────────────────────────────────────────────────────────

def validate_key(key: str) -> str:
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_LENGTH:
        raise InvalidStorageKey("Chave de armazenamento inválida.")
    if key.startswith("/") or "\\" in key or "\x00" in key:
        raise InvalidStorageKey("Chave de armazenamento inválida.")
    for seg in key.split("/"):
        if not _SEGMENT_RE.match(seg) or seg in (".", ".."):
            raise InvalidStorageKey("Chave de armazenamento inválida.")
    return key


def build_key(*parts: Any) -> str:
    """Monta uma chave a partir de segmentos gerados pelo servidor (UUIDs, códigos fixos).

    Cada parte vira texto minúsculo e precisa casar ``[a-z0-9][a-z0-9._-]*``; nunca
    passe nome de arquivo do usuário aqui.
    """
    segs = []
    for p in parts:
        s = str(p).lower() if isinstance(p, uuid.UUID) else str(p)
        if "/" in s:
            raise InvalidStorageKey("Segmento de chave inválido.")
        segs.append(s)
    return validate_key("/".join(segs))


# ─── Utilidades de streaming ─────────────────────────────────────────────────

async def aiter_chunks(source: ChunkSource, chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
    """Normaliza bytes / iterável síncrono / iterável assíncrono em blocos ``bytes``."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        mv = memoryview(source)
        for i in range(0, len(mv), chunk_size):
            yield bytes(mv[i:i + chunk_size])
        return
    if hasattr(source, "__aiter__"):
        async for chunk in source:  # type: ignore[union-attr]
            if chunk:
                yield bytes(chunk)
        return
    for chunk in source:  # type: ignore[union-attr]
        if chunk:
            yield bytes(chunk)


async def _spool(
    source: ChunkSource,
    fd: int,
    *,
    max_bytes: Optional[int],
) -> tuple[int, str]:
    """Copia ``source`` para o descritor ``fd`` (fsync no fim). Retorna (tamanho, sha256)."""
    h = hashlib.sha256()
    size = 0
    buf = bytearray()
    async for chunk in aiter_chunks(source):
        size += len(chunk)
        if max_bytes is not None and size > max_bytes:
            raise ObjectTooLarge(max_bytes)
        h.update(chunk)
        buf += chunk
        if len(buf) >= _WRITE_BUFFER:
            data, buf = bytes(buf), bytearray()
            await asyncio.to_thread(_write_all, fd, data)
    if buf:
        await asyncio.to_thread(_write_all, fd, bytes(buf))
    await asyncio.to_thread(os.fsync, fd)
    return size, h.hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def sha256_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> tuple[int, str]:
    """(tamanho, sha256) de um arquivo local, lido em blocos."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            size += len(b)
            h.update(b)
    return size, h.hexdigest()


def _check_range(start: int, end: Optional[int], size: int) -> tuple[int, int]:
    if end is None:
        end = size - 1
    if start < 0 or end < start or end >= size:
        if size == 0 and start == 0 and (end is None or end == -1):
            return 0, -1
        raise InvalidRange(size)
    return start, end


# ─── Interface ───────────────────────────────────────────────────────────────

class StorageProvider(abc.ABC):
    """Contrato de armazenamento. Nenhuma regra de negócio depende do provedor."""

    name: str = "abstract"

    @abc.abstractmethod
    async def put_stream(
        self,
        key: str,
        chunks: ChunkSource,
        content_type: Optional[str] = None,
        *,
        max_bytes: Optional[int] = None,
        expected_sha256: Optional[str] = None,
    ) -> StoredObject:
        """Grava atomicamente; ``max_bytes`` aplicado DURANTE o streaming."""

    @abc.abstractmethod
    async def put_file(
        self, key: str, path: Union[str, Path], content_type: Optional[str] = None, *, move: bool = False
    ) -> StoredObject:
        """Grava um arquivo local (ex.: saída do ffmpeg). ``move=True`` pode consumir o arquivo."""

    @abc.abstractmethod
    async def open_range(
        self, key: str, start: int = 0, end: Optional[int] = None, *, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        """Abre imediatamente (``ObjectNotFound``/``InvalidRange`` já aqui) e devolve um iterador assíncrono."""

    @abc.abstractmethod
    async def stat(self, key: str) -> ObjectStat:
        ...

    @abc.abstractmethod
    async def delete(self, key: str) -> bool:
        """Remove (idempotente). Retorna True se existia."""

    @abc.abstractmethod
    async def exists(self, key: str) -> bool:
        ...

    @abc.abstractmethod
    def url_for_internal(self, key: str, *, expires_s: int = 300) -> str:
        """URI opaca para consumo interno servidor→servidor (nunca pública)."""

    @abc.abstractmethod
    def delete_blocking(self, key: str) -> bool:
        """Versão síncrona de ``delete`` (hooks síncronos, ex.: after_rollback)."""

    @abc.abstractmethod
    def materialize(self, key: str) -> Any:
        """``async with st.materialize(key) as path`` → caminho local somente-leitura."""

    @abc.abstractmethod
    def workspace(self, prefix: str = "job") -> Any:
        """``async with st.workspace() as dir`` → diretório temporário privado, removido ao sair."""

    async def list_keys(self, prefix: str) -> list[str]:
        """Chaves de objetos SOB o prefixo-diretório ``prefix`` (ex.: ``media/<org>/<id>``)."""
        raise NotImplementedError

    def remove_workspaces(self, prefix: str) -> int:
        """Remove workspaces temporários cujo nome começa com ``prefix`` (processamento
        interrompido por queda do worker). Retorna quantos removeu."""
        return 0

    async def read_bytes(self, key: str, *, max_bytes: int = 16 * 1024 * 1024) -> bytes:
        """Conveniência para objetos PEQUENOS (limite obrigatório)."""
        info = await self.stat(key)
        if info.size > max_bytes:
            raise ObjectTooLarge(max_bytes)
        out = bytearray()
        if info.size == 0:
            return b""
        async for chunk in await self.open_range(key, 0, info.size - 1):
            out += chunk
        return bytes(out)


# ─── Provedor local (volume persistente) ─────────────────────────────────────

class LocalStorageProvider(StorageProvider):
    name = "local"

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.staging_dir = self.root / ".staging"

    # -- caminhos --
    def _ensure_dirs(self) -> None:
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.staging_dir, 0o700)
        except OSError:  # pragma: no cover - FS sem chmod
            pass

    def _path(self, key: str) -> Path:
        validate_key(key)
        base = os.path.realpath(self.objects_dir)
        p = os.path.realpath(os.path.join(base, *key.split("/")))
        if os.path.commonpath([base, p]) != base or p == base:
            raise InvalidStorageKey("Chave de armazenamento inválida.")
        return Path(p)

    def _new_temp(self, prefix: str = "up-") -> tuple[int, str]:
        self._ensure_dirs()
        return tempfile.mkstemp(dir=self.staging_dir, prefix=prefix, suffix=".part")

    # -- escrita --
    async def put_stream(self, key, chunks, content_type=None, *, max_bytes=None, expected_sha256=None):
        dest = self._path(key)
        fd, tmp = await asyncio.to_thread(self._new_temp)
        closed = False
        try:
            size, digest = await _spool(chunks, fd, max_bytes=max_bytes)
            os.close(fd)
            closed = True
            if expected_sha256 and expected_sha256.lower() != digest:
                raise IntegrityMismatch("SHA-256 divergente.")
            await asyncio.to_thread(self._commit, tmp, dest)
            tmp = None  # type: ignore[assignment]
            return StoredObject(key=key, size=size, sha256=digest, content_type=content_type)
        finally:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp:
                _unlink_quiet(tmp)

    def _commit(self, tmp: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
        try:
            os.chmod(dest, 0o640)
        except OSError:  # pragma: no cover
            pass

    async def put_file(self, key, path, content_type=None, *, move=False):
        dest = self._path(key)
        src = Path(path)
        if move:
            same_dev = await asyncio.to_thread(_same_device, src, self.root)
            if same_dev:
                size, digest = await asyncio.to_thread(sha256_file, src)

                def _mv() -> None:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(src, "rb") as f:
                        os.fsync(f.fileno())
                    os.replace(src, dest)

                await asyncio.to_thread(_mv)
                return StoredObject(key=key, size=size, sha256=digest, content_type=content_type)
        obj = await self.put_stream(key, _file_chunks(src), content_type)
        if move:
            _unlink_quiet(str(src))
        return obj

    # -- leitura --
    async def open_range(self, key, start=0, end=None, *, chunk_size=DEFAULT_CHUNK_SIZE):
        p = self._path(key)
        try:
            f = await asyncio.to_thread(open, p, "rb")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise ObjectNotFound("Objeto não encontrado.") from None
        try:
            size = os.fstat(f.fileno()).st_size
            s, e = _check_range(start, end, size)
            await asyncio.to_thread(f.seek, s)
        except BaseException:
            f.close()
            raise
        return _read_file(f, e - s + 1, chunk_size)

    async def stat(self, key):
        p = self._path(key)
        try:
            st = await asyncio.to_thread(os.stat, p)
        except (FileNotFoundError, NotADirectoryError):
            raise ObjectNotFound("Objeto não encontrado.") from None
        if not os.path.isfile(p):
            raise ObjectNotFound("Objeto não encontrado.")
        return ObjectStat(key=key, size=st.st_size, modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))

    async def exists(self, key):
        return await asyncio.to_thread(os.path.isfile, self._path(key))

    async def delete(self, key):
        return await asyncio.to_thread(self.delete_blocking, key)

    def delete_blocking(self, key):
        p = self._path(key)
        try:
            os.unlink(p)
        except FileNotFoundError:
            return False
        # poda diretórios vazios até objects/ (best effort)
        base = Path(os.path.realpath(self.objects_dir))
        parent = p.parent
        while parent != base and base in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return True

    def url_for_internal(self, key, *, expires_s=300):
        return f"local://{validate_key(key)}"

    @asynccontextmanager
    async def materialize(self, key):
        p = self._path(key)
        if not await asyncio.to_thread(os.path.isfile, p):
            raise ObjectNotFound("Objeto não encontrado.")
        yield p

    @asynccontextmanager
    async def workspace(self, prefix="job"):
        await asyncio.to_thread(self._ensure_dirs)
        d = await asyncio.to_thread(tempfile.mkdtemp, prefix=f"{_safe_prefix(prefix)}-", dir=self.staging_dir)
        try:
            yield Path(d)
        finally:
            await asyncio.to_thread(shutil.rmtree, d, True)

    async def list_keys(self, prefix):
        base_dir = self._path(prefix)
        objects = os.path.realpath(self.objects_dir)

        def _walk() -> list[str]:
            if not os.path.isdir(base_dir):
                return []
            out = []
            for root, dirs, files in os.walk(base_dir, followlinks=False):
                dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                for f in files:
                    full = os.path.join(root, f)
                    if os.path.islink(full):
                        continue
                    out.append(os.path.relpath(full, objects).replace(os.sep, "/"))
            return sorted(out)

        return await asyncio.to_thread(_walk)

    def remove_workspaces(self, prefix):
        return _remove_dirs_with_prefix(self.staging_dir, prefix)

    def sweep_staging(self, older_than_s: int = 6 * 3600) -> int:
        """Remove temporários abandonados (queda do processo). Retorna quantos removeu."""
        if not self.staging_dir.exists():
            return 0
        limit = time.time() - older_than_s
        n = 0
        for entry in os.scandir(self.staging_dir):
            try:
                if entry.stat(follow_symlinks=False).st_mtime >= limit:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.unlink(entry.path)
                n += 1
            except OSError:
                continue
        return n


def _remove_dirs_with_prefix(parent: Path, prefix: str) -> int:
    sp = _safe_prefix(prefix)
    if not sp or len(sp) < 8 or not parent.exists():  # nunca um prefixo genérico demais
        return 0
    n = 0
    for entry in os.scandir(parent):
        try:
            if entry.name.startswith(sp) and entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n


def _safe_prefix(prefix: str) -> str:
    p = re.sub(r"[^a-z0-9_-]", "", (prefix or "job").lower())[:80]
    return p or "job"


def _same_device(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def _file_chunks(path: Path, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    f = await asyncio.to_thread(open, path, "rb")
    try:
        while True:
            b = await asyncio.to_thread(f.read, chunk_size)
            if not b:
                break
            yield b
    finally:
        f.close()


async def _read_file(f, remaining: int, chunk_size: int) -> AsyncIterator[bytes]:
    try:
        while remaining > 0:
            b = await asyncio.to_thread(f.read, min(chunk_size, remaining))
            if not b:
                break
            remaining -= len(b)
            yield b
    finally:
        f.close()


# ─── Provedor S3/MinIO (ESQUELETO — MinIO futuro; não validado contra servidor real) ───

class S3StorageProvider(StorageProvider):
    """Implementação sobre boto3 (S3/MinIO). ESQUELETO: lógica completa, porém sem
    validação contra um MinIO real nesta ação (dependência externa registrada).
    Uploads passam por spool local (limite + SHA-256 incremental) e depois
    ``upload_file`` (multipart automático do boto3); o SHA-256 vai no metadado ``sha256``.
    """

    name = "s3"

    def __init__(self, *, endpoint: str, access_key: str, secret_key: str, bucket: str,
                 region: str = "us-east-1", tmp_dir: Optional[str] = None) -> None:
        if not (endpoint and access_key and secret_key and bucket):
            raise StorageUnavailable("Configuração de object storage incompleta.")
        self.endpoint = endpoint if "://" in endpoint else f"http://{endpoint}"
        self.bucket = bucket
        self.region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self.tmp_dir = tmp_dir
        self._client = None

    def _c(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self.region,
                config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                              s3={"addressing_style": "path"}),
            )
        return self._client

    @staticmethod
    def _is_404(exc: Exception) -> bool:
        resp = getattr(exc, "response", None) or {}
        code = str((resp.get("Error") or {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound")

    async def put_stream(self, key, chunks, content_type=None, *, max_bytes=None, expected_sha256=None):
        validate_key(key)
        fd, tmp = tempfile.mkstemp(dir=self.tmp_dir, prefix="s3up-", suffix=".part")
        closed = False
        try:
            size, digest = await _spool(chunks, fd, max_bytes=max_bytes)
            os.close(fd)
            closed = True
            if expected_sha256 and expected_sha256.lower() != digest:
                raise IntegrityMismatch("SHA-256 divergente.")
            extra = {"Metadata": {"sha256": digest}}
            if content_type:
                extra["ContentType"] = content_type
            await asyncio.to_thread(self._c().upload_file, tmp, self.bucket, key, ExtraArgs=extra)
            return StoredObject(key=key, size=size, sha256=digest, content_type=content_type)
        finally:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            _unlink_quiet(tmp)

    async def put_file(self, key, path, content_type=None, *, move=False):
        obj = await self.put_stream(key, _file_chunks(Path(path)), content_type)
        if move:
            _unlink_quiet(str(path))
        return obj

    async def stat(self, key):
        validate_key(key)
        try:
            h = await asyncio.to_thread(self._c().head_object, Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._is_404(exc):
                raise ObjectNotFound("Objeto não encontrado.") from None
            raise StorageUnavailable("Object storage indisponível.") from None
        return ObjectStat(key=key, size=int(h["ContentLength"]), modified_at=h["LastModified"],
                          content_type=h.get("ContentType"), sha256=(h.get("Metadata") or {}).get("sha256"))

    async def open_range(self, key, start=0, end=None, *, chunk_size=DEFAULT_CHUNK_SIZE):
        info = await self.stat(key)
        s, e = _check_range(start, end, info.size)
        if e < s:
            async def _empty():
                if False:  # pragma: no cover
                    yield b""
            return _empty()
        resp = await asyncio.to_thread(self._c().get_object, Bucket=self.bucket, Key=key, Range=f"bytes={s}-{e}")
        body = resp["Body"]

        async def _gen():
            try:
                while True:
                    b = await asyncio.to_thread(body.read, chunk_size)
                    if not b:
                        break
                    yield b
            finally:
                body.close()

        return _gen()

    async def exists(self, key):
        try:
            await self.stat(key)
            return True
        except ObjectNotFound:
            return False

    async def delete(self, key):
        return await asyncio.to_thread(self.delete_blocking, key)

    def delete_blocking(self, key):
        validate_key(key)
        try:
            self._c().head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._is_404(exc):
                return False
            raise StorageUnavailable("Object storage indisponível.") from None
        self._c().delete_object(Bucket=self.bucket, Key=key)
        return True

    async def list_keys(self, prefix):
        validate_key(prefix)

        def _ls() -> list[str]:
            out = []
            pag = self._c().get_paginator("list_objects_v2")
            for page in pag.paginate(Bucket=self.bucket, Prefix=prefix + "/"):
                out.extend(o["Key"] for o in page.get("Contents") or [])
            return sorted(out)

        return await asyncio.to_thread(_ls)

    def remove_workspaces(self, prefix):
        return _remove_dirs_with_prefix(Path(self.tmp_dir or tempfile.gettempdir()), prefix)

    def url_for_internal(self, key, *, expires_s=300):
        validate_key(key)
        return self._c().generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=int(expires_s)
        )

    @asynccontextmanager
    async def materialize(self, key):
        validate_key(key)
        fd, tmp = tempfile.mkstemp(dir=self.tmp_dir, prefix="s3get-")
        os.close(fd)
        try:
            try:
                await asyncio.to_thread(self._c().download_file, self.bucket, key, tmp)
            except Exception as exc:
                if self._is_404(exc):
                    raise ObjectNotFound("Objeto não encontrado.") from None
                raise StorageUnavailable("Object storage indisponível.") from None
            yield Path(tmp)
        finally:
            _unlink_quiet(tmp)

    @asynccontextmanager
    async def workspace(self, prefix="job"):
        d = await asyncio.to_thread(tempfile.mkdtemp, prefix=f"{_safe_prefix(prefix)}-", dir=self.tmp_dir)
        try:
            yield Path(d)
        finally:
            await asyncio.to_thread(shutil.rmtree, d, True)


# ─── Seleção do provedor ─────────────────────────────────────────────────────

_override: Optional[StorageProvider] = None
_default: Optional[StorageProvider] = None


def _build_default() -> StorageProvider:
    from app.core.config import settings

    kind = (settings.STORAGE_PROVIDER or "local").strip().lower()
    if kind == "local":
        return LocalStorageProvider(settings.STORAGE_LOCAL_PATH)
    if kind in ("minio", "s3"):
        return S3StorageProvider(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            bucket=settings.MINIO_BUCKET,
        )
    raise StorageUnavailable(f"STORAGE_PROVIDER desconhecido: {kind!r}")


def get_storage() -> StorageProvider:
    """Provedor ativo (``set_storage`` tem precedência — usado nos testes)."""
    global _default
    if _override is not None:
        return _override
    if _default is None:
        _default = _build_default()
    return _default


def set_storage(provider: Optional[StorageProvider]) -> None:
    """Troca o provedor ativo (``None`` volta ao padrão da configuração)."""
    global _override
    _override = provider


def reset_storage() -> None:
    global _default, _override
    _default = None
    _override = None
