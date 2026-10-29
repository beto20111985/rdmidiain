"""StorageProvider (app/core/storage.py) — LocalStorageProvider em diretório temporário REAL."""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from app.core import storage as st
from app.core.storage import (
    InvalidRange,
    InvalidStorageKey,
    LocalStorageProvider,
    ObjectNotFound,
    ObjectTooLarge,
    IntegrityMismatch,
    build_key,
    validate_key,
)


@pytest.fixture
def prov(tmp_path):
    return LocalStorageProvider(tmp_path / "media")


async def _collect(it):
    out = bytearray()
    async for c in it:
        out += c
    return bytes(out)


async def _agen(parts):
    for p in parts:
        yield p


@pytest.mark.parametrize(
    "key",
    ["../etc/passwd", "/abs/key", "a/../b", "a//b", "a\\b", "A/b", ".hidden/x", "a/.x", "", "a/b/", "x" * 600,
     "media/\x00/x", "media/~root"],
)
def test_invalid_keys_rejected(key):
    with pytest.raises(InvalidStorageKey):
        validate_key(key)


def test_build_key_accepts_uuid_and_rejects_slash():
    oid = uuid.uuid4()
    assert build_key("media", oid, "source") == f"media/{oid}/source"
    with pytest.raises(InvalidStorageKey):
        build_key("media", "../x")
    with pytest.raises(InvalidStorageKey):
        build_key("media", "a/b")


async def test_put_stream_atomic_sha_and_range(prov, tmp_path):
    data = os.urandom(3 * 1024 * 1024 + 17)
    key = build_key("media", uuid.uuid4(), "source")
    obj = await prov.put_stream(key, _agen([data[i:i + 70000] for i in range(0, len(data), 70000)]), "video/mp4")
    assert obj.size == len(data)
    assert obj.sha256 == hashlib.sha256(data).hexdigest()
    assert await prov.exists(key)
    info = await prov.stat(key)
    assert info.size == len(data)
    # range inclusivo
    part = await _collect(await prov.open_range(key, 100, 199))
    assert part == data[100:200]
    tail = await _collect(await prov.open_range(key, len(data) - 5))
    assert tail == data[-5:]
    full = await _collect(await prov.open_range(key))
    assert full == data
    # staging vazio após sucesso (nada de temporário esquecido)
    assert os.listdir(prov.staging_dir) == []
    # objeto fica fora do staging
    assert not str(prov._path(key)).startswith(str(prov.staging_dir))


async def test_put_stream_limit_aborts_and_cleans(prov):
    key = build_key("media", uuid.uuid4(), "source")
    chunks = [b"x" * 1000] * 20
    with pytest.raises(ObjectTooLarge):
        await prov.put_stream(key, _agen(chunks), max_bytes=5000)
    assert not await prov.exists(key)
    assert os.listdir(prov.staging_dir) == []


async def test_put_stream_failure_mid_stream_cleans(prov):
    key = build_key("media", uuid.uuid4(), "source")

    async def boom():
        yield b"a" * 10
        raise RuntimeError("cliente abortou")

    with pytest.raises(RuntimeError):
        await prov.put_stream(key, boom())
    assert not await prov.exists(key)
    assert os.listdir(prov.staging_dir) == []


async def test_expected_sha_mismatch(prov):
    key = build_key("media", uuid.uuid4(), "x")
    with pytest.raises(IntegrityMismatch):
        await prov.put_stream(key, b"abc", expected_sha256="0" * 64)
    assert not await prov.exists(key)
    obj = await prov.put_stream(key, b"abc", expected_sha256=hashlib.sha256(b"abc").hexdigest())
    assert obj.size == 3


async def test_invalid_range_and_not_found(prov):
    key = build_key("media", uuid.uuid4(), "x")
    with pytest.raises(ObjectNotFound):
        await prov.open_range(key)
    with pytest.raises(ObjectNotFound):
        await prov.stat(key)
    await prov.put_stream(key, b"0123456789")
    with pytest.raises(InvalidRange):
        await prov.open_range(key, 10, 20)
    with pytest.raises(InvalidRange):
        await prov.open_range(key, 5, 3)


async def test_delete_idempotent_and_prunes(prov):
    key = build_key("media", uuid.uuid4(), "y")
    await prov.put_stream(key, b"z")
    assert await prov.delete(key) is True
    assert await prov.delete(key) is False
    assert not await prov.exists(key)
    # diretório da mídia podado
    assert not (prov.objects_dir / key).parent.exists()


async def test_put_file_move_and_materialize(prov, tmp_path):
    key = build_key("media", uuid.uuid4(), "r.mp4")
    async with prov.workspace("t") as ws:
        src = ws / "out.bin"
        src.write_bytes(b"hello" * 1000)
        obj = await prov.put_file(key, src, "video/mp4", move=True)
        assert not src.exists()
    assert not ws.exists()  # workspace removido
    assert obj.sha256 == hashlib.sha256(b"hello" * 1000).hexdigest()
    async with prov.materialize(key) as p:
        assert p.read_bytes() == b"hello" * 1000
    assert prov.url_for_internal(key) == f"local://{key}"


async def test_symlink_escape_blocked(prov, tmp_path):
    prov._ensure_dirs()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"s")
    os.symlink(outside, prov.objects_dir / "evil")
    with pytest.raises(InvalidStorageKey):
        await prov.stat("evil/secret")


def test_provider_selection(monkeypatch):
    from app.core.config import settings

    st.reset_storage()
    try:
        monkeypatch.setattr(settings, "STORAGE_PROVIDER", "local")
        assert isinstance(st.get_storage(), LocalStorageProvider)
        st.reset_storage()
        monkeypatch.setattr(settings, "STORAGE_PROVIDER", "minio")
        monkeypatch.setattr(settings, "MINIO_ENDPOINT", "")
        with pytest.raises(st.StorageUnavailable):
            st.get_storage()
        st.reset_storage()
        monkeypatch.setattr(settings, "STORAGE_PROVIDER", "ftp")
        with pytest.raises(st.StorageUnavailable):
            st.get_storage()
    finally:
        st.reset_storage()


async def test_rd_keys_dir_never_touched(prov):
    """`.rd-keys/` (chave de manifesto do W-INV no mesmo volume) é inalcançável."""
    keys_dir = prov.root / ".rd-keys"
    keys_dir.mkdir(parents=True)
    pem = keys_dir / "manifest_es256.pem"
    pem.write_bytes(b"-----BEGIN PRIVATE KEY-----\n")
    os.utime(pem, (1, 1))
    for bad in (".rd-keys/manifest_es256.pem", "../.rd-keys/manifest_es256.pem", "objects/../../.rd-keys/x"):
        with pytest.raises(InvalidStorageKey):
            await prov.stat(bad)
        with pytest.raises(InvalidStorageKey):
            await prov.delete(bad)
        with pytest.raises(InvalidStorageKey):
            await prov.put_stream(bad, b"x")
    k = build_key("media", uuid.uuid4(), "a")
    await prov.put_stream(k, b"1")
    await prov.delete(k)
    prov.sweep_staging(older_than_s=0)
    assert pem.read_bytes().startswith(b"-----BEGIN")


def test_sweep_staging_removes_old(prov):
    prov._ensure_dirs()
    old = prov.staging_dir / "up-old.part"
    old.write_bytes(b"x")
    os.utime(old, (1, 1))
    new = prov.staging_dir / "up-new.part"
    new.write_bytes(b"x")
    assert prov.sweep_staging(older_than_s=3600) == 1
    assert not old.exists() and new.exists()
