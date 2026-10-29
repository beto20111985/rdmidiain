"""
Geração de arquivos de mídia REAIS para os testes (ffmpeg/Pillow em diretório temporário)
e fixtures comuns dos testes de mídia. Nenhum binário é commitado: tudo é gerado aqui.

Importado por ``test_media_*.py`` (este módulo não contém testes).
"""
from __future__ import annotations

import json
import secrets
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

import pytest
from sqlalchemy import select

from app.core import storage as core_storage
from app.core.storage import LocalStorageProvider
from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
from app.modules.media import policy
from app.modules.media import service as media_service
from tests.factories import login_as, make_user

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def run_ffmpeg(*args: str) -> None:
    cp = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", *args], capture_output=True, timeout=300)
    assert cp.returncode == 0, cp.stderr.decode()[-2000:]


def ffprobe(path: Path) -> dict:
    cp = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                        capture_output=True, timeout=60)
    assert cp.returncode == 0, cp.stderr.decode()[-2000:]
    return json.loads(cp.stdout)


# ─── Imagens ─────────────────────────────────────────────────────────────────

def make_image(path: Path, fmt: str, w: int, h: int, *, exif_orientation: Optional[int] = None,
               frames: int = 1, alpha: bool = False, seed: int = 0) -> Path:
    from PIL import Image, ImageDraw

    def frame(i: int):
        mode = "RGBA" if alpha else "RGB"
        im = Image.new(mode, (w, h), ((20 + i * 30) % 200, (60 + seed * 7) % 256, (120 + seed * 13) % 256,
                                      255 if not alpha else 200))
        d = ImageDraw.Draw(im)
        for k in range(0, w, max(1, w // 16)):
            d.line([(k, 0), (w - k, h)], fill=(255, (k + i * 40) % 255, 0, 255), width=3)
        d.rectangle([w // 4, h // 4, w // 2, h // 2], fill=(0, 200, (i * 70) % 255, 255))
        return im

    if fmt == "gif":
        frames_list = [frame(i).convert("P", palette=Image.ADAPTIVE) for i in range(frames)]
        if frames > 1:
            frames_list[0].save(path, "GIF", save_all=True, append_images=frames_list[1:], duration=200, loop=0)
        else:
            frames_list[0].save(path, "GIF")
        return path
    im = frame(0)
    if fmt == "jpeg":
        im = im.convert("RGB")
        kw = {"quality": 90}
        if exif_orientation:
            ex = Image.Exif()
            ex[0x0112] = exif_orientation
            ex[0x8825] = {2: (16.0, 40.0, 0.0)}  # GPS (deve ser removido na saída)
            kw["exif"] = ex.tobytes()
        im.save(path, "JPEG", **kw)
    elif fmt == "png":
        im.save(path, "PNG")
    else:
        raise ValueError(fmt)
    return path


# ─── Vídeos ──────────────────────────────────────────────────────────────────

def make_video(path: Path, w: int, h: int, *, seconds: float = 1.0, fps: str = "30", audio: bool = True,
               vcodec: str = "libx264", extra_out: Iterable[str] = (), faststart: bool = True) -> Path:
    args = ["-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    args += ["-map", "0:v"] + (["-map", "1:a"] if audio else [])
    if vcodec == "libx264":
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    elif vcodec == "libvpx-vp9":
        args += ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "800k", "-row-mt", "1"]
    else:
        args += ["-c:v", vcodec]
    if audio:
        args += ["-c:a", "libopus" if str(path).endswith(".webm") else "aac", "-b:a", "64k"]
    if faststart and str(path).endswith((".mp4", ".mov")):
        args += ["-movflags", "+faststart"]
    args += list(extra_out) + [str(path)]
    run_ffmpeg(*args)
    return path


# ─── Upload em streaming (multipart montado à mão) ───────────────────────────

def multipart_stream(
    file_iter: Iterable[bytes] | AsyncIterator[bytes],
    filename: str,
    content_type: str = "application/octet-stream",
    fields: Optional[dict] = None,
    fields_after: Optional[dict] = None,
    counter: Optional[dict] = None,
) -> tuple[AsyncIterator[bytes], str]:
    boundary = "rdtest" + secrets.token_hex(8)

    def field_part(name: str, value: str) -> bytes:
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()

    async def gen():
        for k, v in (fields or {}).items():
            yield field_part(k, v)
        yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
               f"Content-Type: {content_type}\r\n\r\n").encode()
        if hasattr(file_iter, "__aiter__"):
            async for c in file_iter:  # type: ignore[union-attr]
                if counter is not None:
                    counter["sent"] = counter.get("sent", 0) + len(c)
                yield c
        else:
            for c in file_iter:  # type: ignore[union-attr]
                if counter is not None:
                    counter["sent"] = counter.get("sent", 0) + len(c)
                yield c
        yield b"\r\n"
        for k, v in (fields_after or {}).items():
            yield field_part(k, v)
        yield f"--{boundary}--\r\n".encode()

    return gen(), f"multipart/form-data; boundary={boundary}"


def file_chunks(path: Path, chunk: int = 256 * 1024) -> Iterable[bytes]:
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                return
            yield b


async def upload(client, session, path_or_bytes, filename: str, content_type: str = "application/octet-stream",
                 *, org_id=None, headers: Optional[dict] = None, fields_after: Optional[dict] = None,
                 counter: Optional[dict] = None):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
        it: Iterable[bytes] = [data[i:i + 65536] for i in range(0, len(data), 65536)] or [b""]
    elif isinstance(path_or_bytes, Path):
        it = file_chunks(path_or_bytes)
    else:
        it = path_or_bytes
    fields = {"org_id": str(org_id)} if org_id else None
    body, ctype = multipart_stream(it, filename, content_type, fields=fields, fields_after=fields_after, counter=counter)
    h = {**session.headers, "Content-Type": ctype, **(headers or {})}
    return await client.post("/api/v1/media/upload", content=body, headers=h)


# ─── Ambiente ────────────────────────────────────────────────────────────────

@dataclass
class MediaEnv:
    root: Path
    storage: LocalStorageProvider
    enqueued: list = field(default_factory=list)


@pytest.fixture
def media_env(tmp_path, monkeypatch):
    st = LocalStorageProvider(tmp_path / "media-root")
    core_storage.set_storage(st)
    env = MediaEnv(root=tmp_path / "media-root", storage=st)
    media_service.set_enqueuer(env.enqueued.append)
    ms = policy.media_settings()
    monkeypatch.setattr(ms, "X264_PRESET", "superfast")
    monkeypatch.setattr(ms, "FFMPEG_THREADS", 4)
    yield env
    media_service.set_enqueuer(None)
    core_storage.set_storage(None)


async def media_role(db, perms=("media.view", "media.upload", "media.delete")) -> Role:
    role = Role(id=uuid.uuid4(), slug=f"tmedia_{secrets.token_hex(4)}", name="Teste mídia", is_system=False)
    db.add(role)
    await db.flush()
    for code in perms:
        p = (await db.execute(select(Permission).where(Permission.code == code))).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=p.id))
    await db.commit()
    return role


async def media_user(db, client, org, perms=("media.view", "media.upload", "media.delete")):
    """Usuário com permissões de mídia em escopo ORGANIZATION de ``org`` → (user, session)."""
    role = await media_role(db, perms)
    u = await make_user(db)
    db.add(RoleAssignment(id=uuid.uuid4(), user_id=u.id, role_id=role.id, scope_type="ORGANIZATION", scope_id=org.id))
    await db.commit()
    s = await login_as(client, u)
    return u, s


def staging_files(env: MediaEnv) -> list:
    d = env.storage.staging_dir
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def object_keys(env: MediaEnv) -> list:
    base = env.storage.objects_dir
    if not base.exists():
        return []
    return sorted(str(p.relative_to(base)).replace("\\", "/") for p in base.rglob("*") if p.is_file())
