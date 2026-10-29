"""
Pipeline real de mídia (worker): arquivos REAIS gerados com ffmpeg/Pillow, ffprobe/Pillow
reais, PostgreSQL real. Upload pela API → ``pipeline.process_media`` (o mesmo código da
task Celery) → verificação da SAÍDA com ffprobe/Pillow independentes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core import events
from app.core.timeutils import utcnow
from app.models.media import MediaFile
from app.modules.media import pipeline
from app.modules.media.policy import media_settings
from tests.factories import make_org
from tests.integration.test_media_gen import (  # noqa: F401  (fixture)
    ffprobe,
    make_image,
    make_video,
    media_env,
    media_user,
    object_keys,
    run_ffmpeg,
    staging_files,
    upload,
)


async def _up(client, s, path: Path, filename: str, ctype: str = "application/octet-stream") -> str:
    r = await upload(client, s, path, filename, ctype)
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def _process(mid: str):
    return await pipeline.process_media(uuid.UUID(mid))


async def _detail(client, s, mid: str) -> dict:
    r = await client.get(f"/api/v1/media/{mid}", headers=s.headers)
    assert r.status_code == 200, r.text
    return r.json()


def _obj_path(env, db_row: MediaFile, which: str = "main") -> Path:
    key = db_row.storage_key if which == "main" else db_row.preview_storage_key
    return env.storage._path(key)


async def _row(db, mid: str) -> MediaFile:
    db.expire_all()
    return (await db.execute(select(MediaFile).where(MediaFile.id == uuid.UUID(mid)))).scalar_one()


def _atoms(path: Path) -> list[str]:
    out = []
    with open(path, "rb") as f:
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            size = int.from_bytes(hdr[:4], "big")
            out.append(hdr[4:8].decode("latin-1"))
            if size == 1:
                size = int.from_bytes(f.read(8), "big")
                f.seek(size - 16, 1)
            elif size < 8:
                break
            else:
                f.seek(size - 8, 1)
    return out


@pytest.fixture
async def ctx_org(client, db, media_env):
    org = await make_org(db)
    u, s = await media_user(db, client, org)
    return SimpleNamespace(id=org.id), SimpleNamespace(id=u.id), s


# ─── Imagens ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "fmt,w,h,mime,orient",
    [
        ("jpeg", 1920, 1080, "image/jpeg", "HORIZONTAL"),
        ("jpeg", 1080, 1920, "image/jpeg", "VERTICAL"),
        ("png", 1920, 1080, "image/png", "HORIZONTAL"),
        ("png", 1080, 1920, "image/png", "VERTICAL"),
        ("gif", 1920, 1080, "image/png", "HORIZONTAL"),  # GIF estático → PNG
        ("gif", 1080, 1920, "image/png", "VERTICAL"),
    ],
)
async def test_images_full_hd_accepted(client, db, media_env, ctx_org, tmp_path, fmt, w, h, mime, orient):
    from PIL import Image

    org, _, s = ctx_org
    ext = {"jpeg": "jpg", "png": "png", "gif": "gif"}[fmt]
    p = make_image(tmp_path / f"in.{ext}", fmt, w, h)
    mid = await _up(client, s, p, f"arte.{ext}")
    out = await _process(mid)
    assert out.result == "ready", out
    d = await _detail(client, s, mid)
    assert d["status"] == "READY" and d["progress_pct"] == 100
    o = d["output"]
    assert (o["width"], o["height"], o["mime_type"], o["orientation"]) == (w, h, mime, orient)
    assert o["duration_ms"] is None and o["has_audio"] is False
    row = await _row(db, mid)
    main = _obj_path(media_env, row)
    data = main.read_bytes()
    assert hashlib.sha256(data).hexdigest() == o["sha256"] and len(data) == o["size_bytes"]
    with Image.open(main) as im:
        assert im.size == (w, h)
        im.load()
    with Image.open(_obj_path(media_env, row, "preview")) as pv:
        assert pv.format == "JPEG" and max(pv.size) <= 640
    assert d["has_preview"] is True
    # original apagado após READY; nenhum temporário sobrando
    assert row.source_storage_key is None
    assert sorted(object_keys(media_env)) == sorted([row.storage_key, row.preview_storage_key])
    assert staging_files(media_env) == []


async def test_jpeg_exif_rotation_and_gps_stripped(client, db, media_env, ctx_org, tmp_path):
    from PIL import Image

    _, _, s = ctx_org
    # pixels 1920×1080 com EXIF orientation=6 → exibida 1080×1920 (vertical) + GPS no EXIF
    p = make_image(tmp_path / "cel.jpg", "jpeg", 1920, 1080, exif_orientation=6)
    mid = await _up(client, s, p, "cel.jpg")
    assert (await _process(mid)).result == "ready"
    d = await _detail(client, s, mid)
    assert (d["output"]["width"], d["output"]["height"], d["output"]["orientation"]) == (1080, 1920, "VERTICAL")
    assert (d["source"]["width"], d["source"]["height"]) == (1080, 1920)
    row = await _row(db, mid)
    with Image.open(_obj_path(media_env, row)) as im:
        assert im.size == (1080, 1920)
        ex = im.getexif()
        assert 0x8825 not in ex and ex.get(0x0112, 1) == 1  # sem GPS e sem rotação pendente


async def test_jpeg_untouched_has_no_exif(client, db, media_env, ctx_org, tmp_path):
    from PIL import Image

    _, _, s = ctx_org
    p = make_image(tmp_path / "f.jpg", "jpeg", 1920, 1080, exif_orientation=1)
    mid = await _up(client, s, p, "f.jpg")
    assert (await _process(mid)).result == "ready"
    row = await _row(db, mid)
    with Image.open(_obj_path(media_env, row)) as im:
        assert 0x8825 not in im.getexif()


async def test_image_4k_converted_to_full_hd(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_image(tmp_path / "uhd.png", "png", 3840, 2160)
    mid = await _up(client, s, p, "uhd.png")
    assert (await _process(mid)).result == "ready"
    d = await _detail(client, s, mid)
    assert (d["output"]["width"], d["output"]["height"]) == (1920, 1080)
    assert (d["source"]["width"], d["source"]["height"]) == (3840, 2160)


@pytest.mark.parametrize("w,h,code", [(1280, 720, "RESOLUTION_BELOW_FULL_HD"), (720, 1280, "RESOLUTION_BELOW_FULL_HD"),
                                      (2560, 1440, "RESOLUTION_NOT_ALLOWED")])
async def test_image_bad_resolution_rejected(client, db, media_env, ctx_org, tmp_path, w, h, code):
    org, _, s = ctx_org
    p = make_image(tmp_path / "x.jpg", "jpeg", w, h)
    mid = await _up(client, s, p, "x.jpg")
    out = await _process(mid)
    assert (out.result, out.code) == ("rejected", code)
    d = await _detail(client, s, mid)
    assert d["status"] == "REJECTED" and d["status_code"] == code and d["output"] is None
    assert d["content_url"] is None
    assert object_keys(media_env) == []  # original rejeitado é apagado
    assert staging_files(media_env) == []
    r = await client.get(f"/api/v1/media/{mid}/content", headers=s.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "MEDIA_NOT_READY"


async def test_animated_gif_stays_image_gif(client, db, media_env, ctx_org, tmp_path):
    from PIL import Image

    _, _, s = ctx_org
    p = make_image(tmp_path / "anim.gif", "gif", 1920, 1080, frames=5)
    mid = await _up(client, s, p, "anim.gif")
    assert (await _process(mid)).result == "ready"
    d = await _detail(client, s, mid)
    o = d["output"]
    assert o["mime_type"] == "image/gif" and o["animated"] is True and o["frame_count"] == 5
    assert d["kind"] == "IMAGE" and o["duration_ms"] is None
    row = await _row(db, mid)
    with Image.open(_obj_path(media_env, row)) as im:
        assert im.n_frames == 5 and im.size == (1920, 1080)
    assert row.source_metadata["loop_duration_ms"] == 1000


async def test_animated_gif_4k_resized(client, db, media_env, ctx_org, tmp_path):
    from PIL import Image

    _, _, s = ctx_org
    p = make_image(tmp_path / "anim4k.gif", "gif", 2160, 3840, frames=3)
    mid = await _up(client, s, p, "anim4k.gif")
    assert (await _process(mid)).result == "ready"
    row = await _row(db, mid)
    with Image.open(_obj_path(media_env, row)) as im:
        assert im.size == (1080, 1920) and im.n_frames >= 2


async def test_truncated_jpeg_rejected(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_image(tmp_path / "ok.jpg", "jpeg", 1920, 1080)
    data = p.read_bytes()
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(data[: len(data) // 2])
    mid = await _up(client, s, cut, "cut.jpg")
    out = await _process(mid)
    assert (out.result, out.code) == ("rejected", "MEDIA_CORRUPTED")


async def test_png_garbage_body_rejected(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50000)
    mid = await _up(client, s, bad, "bad.png")
    out = await _process(mid)
    assert (out.result, out.code) == ("rejected", "MEDIA_CORRUPTED")


async def test_rejection_notifies_org(client, db, media_env, ctx_org, tmp_path):
    from app.models.notifications import Notification

    org, u, s = ctx_org
    p = make_image(tmp_path / "small.png", "png", 1280, 720)
    mid = await _up(client, s, p, "small.png")
    assert (await _process(mid)).result == "rejected"
    db.expire_all()
    rows = (await db.execute(select(Notification).where(Notification.event_type == "media.error"))).scalars().all()
    assert [n.user_id for n in rows] == [u.id]
    assert rows[0].dedupe_key == mid and "small.png" in (rows[0].body or "")


# ─── Vídeos ──────────────────────────────────────────────────────────────────

def _streams(path: Path) -> dict:
    data = ffprobe(path)
    return {
        "video": [s for s in data["streams"] if s["codec_type"] == "video"],
        "audio": [s for s in data["streams"] if s["codec_type"] == "audio"],
        "all": data["streams"],
        "format": data["format"],
    }


async def test_mp4_with_audio_output_has_no_audio(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_video(tmp_path / "com_audio.mp4", 1920, 1080, seconds=2, audio=True)
    assert len(_streams(p)["audio"]) == 1  # a entrada TEM áudio
    mid = await _up(client, s, p, "com_audio.mp4")
    out = await _process(mid)
    assert out.result == "ready", out
    d = await _detail(client, s, mid)
    assert d["source"]["has_audio"] is True and d["output"]["has_audio"] is False
    row = await _row(db, mid)
    main = _obj_path(media_env, row)
    st = _streams(main)
    assert st["audio"] == [] and len(st["all"]) == 1
    v = st["video"][0]
    assert v["codec_name"] == "h264" and v["profile"] == "High" and v["pix_fmt"] == "yuv420p"
    assert (v["width"], v["height"]) == (1920, 1080)
    atoms = _atoms(main)
    assert atoms.index("moov") < atoms.index("mdat")  # +faststart
    assert d["output"]["mime_type"] == "video/mp4" and d["output"]["codec"] == "h264"
    assert hashlib.sha256(main.read_bytes()).hexdigest() == d["output"]["sha256"]
    # preview: vídeo curto de baixa resolução, mudo
    pv = _streams(_obj_path(media_env, row, "preview"))
    assert pv["audio"] == [] and pv["video"][0]["width"] == 640 and pv["video"][0]["height"] == 360


async def test_webm_vp9_converted_to_mp4_h264(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_video(tmp_path / "in.webm", 1920, 1080, seconds=1, vcodec="libvpx-vp9", audio=True)
    assert _streams(p)["video"][0]["codec_name"] == "vp9"
    mid = await _up(client, s, p, "in.webm", "video/webm")
    assert (await _process(mid)).result == "ready"
    d = await _detail(client, s, mid)
    assert d["source"]["format"] == "webm" and d["source"]["codec"] == "vp9"
    row = await _row(db, mid)
    st = _streams(_obj_path(media_env, row))
    assert st["video"][0]["codec_name"] == "h264" and st["audio"] == []
    assert "mp4" in st["format"]["format_name"] or "mov" in st["format"]["format_name"]
    assert d["output"]["duration_ms"] == pytest.approx(1000, abs=34)


async def test_video_4k_scaled_to_full_hd(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_video(tmp_path / "uhd.mp4", 3840, 2160, seconds=1, audio=False)
    mid = await _up(client, s, p, "uhd.mp4")
    assert (await _process(mid)).result == "ready"
    d = await _detail(client, s, mid)
    assert (d["output"]["width"], d["output"]["height"]) == (1920, 1080)
    assert (d["source"]["width"], d["source"]["height"]) == (3840, 2160)
    row = await _row(db, mid)
    v = _streams(_obj_path(media_env, row))["video"][0]
    assert (v["width"], v["height"], v.get("sample_aspect_ratio", "1:1")) == (1920, 1080, "1:1")


async def test_video_vertical_and_rotation_metadata(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_video(tmp_path / "vert.mp4", 1080, 1920, seconds=1, audio=False)
    mid = await _up(client, s, p, "vert.mp4")
    assert (await _process(mid)).result == "ready"
    d = await _detail(client, s, mid)
    assert (d["output"]["width"], d["output"]["height"], d["output"]["orientation"]) == (1080, 1920, "VERTICAL")
    # celular: pixels 1920×1080 + matriz de rotação 90° → exibido 1080×1920
    land = make_video(tmp_path / "land.mp4", 1920, 1080, seconds=1, audio=False)
    rot = tmp_path / "rot.mp4"
    run_ffmpeg("-display_rotation", "90", "-i", str(land), "-c", "copy", str(rot))
    mid2 = await _up(client, s, rot, "rot.mp4")
    assert (await _process(mid2)).result == "ready"
    d2 = await _detail(client, s, mid2)
    assert (d2["output"]["width"], d2["output"]["height"]) == (1080, 1920)


@pytest.mark.parametrize("w,h,code", [(1280, 720, "RESOLUTION_BELOW_FULL_HD"), (640, 360, "RESOLUTION_BELOW_FULL_HD"),
                                      (1440, 1080, "RESOLUTION_BELOW_FULL_HD")])
async def test_video_below_full_hd_rejected(client, db, media_env, ctx_org, tmp_path, w, h, code):
    _, _, s = ctx_org
    p = make_video(tmp_path / "low.mp4", w, h, seconds=1, audio=True)
    mid = await _up(client, s, p, "low.mp4")
    out = await _process(mid)
    assert (out.result, out.code) == ("rejected", code)
    assert object_keys(media_env) == [] and staging_files(media_env) == []


async def test_truncated_mp4_rejected(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    full = make_video(tmp_path / "full.mp4", 1920, 1080, seconds=3, audio=True, faststart=True)
    data = full.read_bytes()
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(data[: int(len(data) * 0.5)])  # moov no início: ffprobe "funciona", decode não
    mid = await _up(client, s, cut, "cut.mp4")
    out = await _process(mid)
    assert (out.result, out.code) == ("rejected", "MEDIA_CORRUPTED"), out
    assert object_keys(media_env) == [] and staging_files(media_env) == []
    # sem faststart (moov no fim) e truncado → ffprobe falha → rejeitado
    nofs = make_video(tmp_path / "nofs.mp4", 1920, 1080, seconds=2, audio=False, faststart=False)
    d2 = nofs.read_bytes()
    cut2 = tmp_path / "cut2.mp4"
    cut2.write_bytes(d2[: int(len(d2) * 0.7)])
    mid2 = await _up(client, s, cut2, "cut2.mp4")
    out2 = await _process(mid2)
    assert (out2.result, out2.code) == ("rejected", "MEDIA_CORRUPTED")


async def test_duration_exact_30s_never_cut(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_video(tmp_path / "trinta.mp4", 1920, 1080, seconds=30, audio=True)
    src_dur = float(_streams(p)["video"][0]["duration"])
    assert src_dur == pytest.approx(30.0, abs=0.001)
    mid = await _up(client, s, p, "trinta.mp4")
    assert (await _process(mid)).result == "ready"
    d = await _detail(client, s, mid)
    frame_ms = 1000 / 30
    assert abs(d["output"]["duration_ms"] - 30000) <= frame_ms
    assert d["output"]["duration_ms"] >= 30000 - frame_ms  # nunca cortado
    row = await _row(db, mid)
    v = _streams(_obj_path(media_env, row))["video"][0]
    assert int(v["nb_frames"]) == 900
    assert float(v["duration"]) == pytest.approx(30.0, abs=1 / 30)
    assert d["source"]["duration_ms"] == 30000


async def test_60fps_reduced_to_30_duration_preserved(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_video(tmp_path / "f60.mp4", 1920, 1080, seconds=2, fps="60", audio=False)
    mid = await _up(client, s, p, "f60.mp4")
    assert (await _process(mid)).result == "ready"
    d = await _detail(client, s, mid)
    assert d["source"]["fps_milli"] == 60000 and d["output"]["fps_milli"] == 30000
    assert abs(d["output"]["duration_ms"] - 2000) <= 34


async def test_audio_only_file_rejected(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    a = tmp_path / "som.mp4"
    run_ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", "-f", "mp4", str(a))
    r = await upload(client, s, a, "som.mp4", "video/mp4")
    if r.status_code == 415:  # marca M4A no ftyp → recusado já no envio
        return
    assert r.status_code == 202
    out = await _process(r.json()["id"])
    assert (out.result, out.code) == ("rejected", "MEDIA_NO_VIDEO_STREAM")


# ─── Idempotência, concorrência, falhas e limpeza ────────────────────────────

async def test_task_twice_is_idempotent(client, db, media_env, ctx_org, tmp_path):
    from app.workers.tasks.media import process_media as task

    _, _, s = ctx_org
    p = make_video(tmp_path / "v.mp4", 1920, 1080, seconds=1, audio=True)
    mid = await _up(client, s, p, "v.mp4")
    r1 = await asyncio.to_thread(task.apply, args=[mid])
    assert r1.result["result"] == "ready", r1.result
    row1 = await _row(db, mid)
    keys1 = object_keys(media_env)
    mtime1 = _obj_path(media_env, row1).stat().st_mtime_ns
    r2 = await asyncio.to_thread(task.apply, args=[mid])
    assert r2.result["result"] == "noop" and r2.result["status"] == "READY"
    row2 = await _row(db, mid)
    assert (row2.sha256, row2.size_bytes, row2.attempts) == (row1.sha256, row1.size_bytes, row1.attempts)
    assert object_keys(media_env) == keys1
    assert _obj_path(media_env, row2).stat().st_mtime_ns == mtime1  # não reprocessou


async def test_concurrent_claim_respects_lease(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_image(tmp_path / "a.png", "png", 1920, 1080)
    mid = await _up(client, s, p, "a.png")
    row = await _row(db, mid)
    row.status = "PROCESSING"
    row.lease_until = utcnow() + timedelta(minutes=10)
    row.attempts = 1
    await db.commit()
    out = await _process(mid)
    assert out.result == "busy"
    # lease vencido (worker morreu) → outra execução assume
    row = await _row(db, mid)
    row.lease_until = utcnow() - timedelta(seconds=1)
    await db.commit()
    out = await _process(mid)
    assert out.result == "ready" and out.attempts == 2


async def test_two_parallel_runs_only_one_processes(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_video(tmp_path / "v.mp4", 1920, 1080, seconds=1, audio=False)
    mid = await _up(client, s, p, "v.mp4")
    a, b = await asyncio.gather(_process(mid), _process(mid))
    results = sorted([a.result, b.result])
    assert results in (["busy", "ready"], ["noop", "ready"]), results
    row = await _row(db, mid)
    assert row.status == "READY" and row.attempts == 1


async def test_failure_cleans_temporaries_and_keeps_source(client, db, media_env, ctx_org, tmp_path, monkeypatch):
    """Falha REAL (timeout do ffmpeg): processo morto, workspace removido, nenhuma saída,
    original preservado para retry; depois o retry pela API conclui."""
    _, _, s = ctx_org
    p = make_video(tmp_path / "v.mp4", 1920, 1080, seconds=6, audio=False)
    mid = await _up(client, s, p, "v.mp4")
    ms = media_settings()
    monkeypatch.setattr(ms, "X264_PRESET", "veryslow")
    monkeypatch.setattr(ms, "TRANSCODE_TIMEOUT_MIN_S", 1)
    monkeypatch.setattr(ms, "TRANSCODE_TIMEOUT_MAX_S", 1)
    out = await _process(mid)
    assert (out.result, out.code, out.retry) == ("failed", "MEDIA_PROCESSING_TIMEOUT", True)
    row = await _row(db, mid)
    assert row.status == "FAILED" and row.lease_until is None and row.storage_key is None
    assert object_keys(media_env) == [row.source_storage_key]  # só o original
    assert staging_files(media_env) == []  # workspace e temporários removidos
    # retry pela API (FAILED → UPLOADED → enfileirado após commit) e sucesso
    monkeypatch.setattr(ms, "X264_PRESET", "superfast")
    monkeypatch.setattr(ms, "TRANSCODE_TIMEOUT_MIN_S", 300)
    monkeypatch.setattr(ms, "TRANSCODE_TIMEOUT_MAX_S", 3000)
    media_env.enqueued.clear()
    r = await client.post(f"/api/v1/media/{mid}/retry", headers=s.headers)
    assert r.status_code == 202 and r.json()["status"] == "UPLOADED"
    assert media_env.enqueued == [mid]
    assert (await _process(mid)).result == "ready"
    r = await client.post(f"/api/v1/media/{mid}/retry", headers=s.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "MEDIA_RETRY_NOT_ALLOWED"


async def test_environment_failure_is_not_rejection(client, db, media_env, ctx_org, tmp_path, monkeypatch):
    """ffprobe sem memória (ambiente) → FAILED retentável, nunca REJECTED do arquivo."""
    _, _, s = ctx_org
    p = make_video(tmp_path / "v.mp4", 1920, 1080, seconds=1, audio=False)
    mid = await _up(client, s, p, "v.mp4")
    monkeypatch.setattr(media_settings(), "PROC_MEM_LIMIT_MB", 8)
    out = await _process(mid)
    assert out.result == "failed" and out.code in ("MEDIA_PROBE_UNAVAILABLE", "MEDIA_TRANSCODE_FAILED"), out
    assert staging_files(media_env) == []


async def test_max_attempts_stops_auto_retry(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    p = make_image(tmp_path / "a.png", "png", 1920, 1080)
    mid = await _up(client, s, p, "a.png")
    row = await _row(db, mid)
    row.status, row.attempts = "FAILED", media_settings().MAX_ATTEMPTS
    await db.commit()
    out = await _process(mid)
    assert out.result == "noop" and out.retry is False


async def test_processing_events_with_real_progress(client, db, media_env, ctx_org, tmp_path, redis):
    org, _, s = ctx_org
    p = make_video(tmp_path / "v.mp4", 1920, 1080, seconds=4, audio=False)
    mid = await _up(client, s, p, "v.mp4")
    ps = redis.pubsub()
    await ps.subscribe(events.channel_org(org.id))
    await asyncio.sleep(0.1)
    assert (await _process(mid)).result == "ready"
    got = []
    for _ in range(200):
        msg = await ps.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if msg is None:
            if got and got[-1]["data"]["status"] == "READY":
                break
            continue
        env = json.loads(msg["data"])
        if env["type"] == "media.processing" and env["data"]["id"] == mid:
            got.append(env)
    await ps.unsubscribe()
    await ps.aclose()
    statuses = [e["data"]["status"] for e in got]
    progress = [e["data"]["progress"] for e in got]
    assert statuses[0] == "VALIDATING" and statuses[-1] == "READY"
    assert "PROCESSING" in statuses
    assert progress == sorted(progress) and progress[-1] == 100
    assert any(0 < x < 88 for x in progress if x not in (1, 5))  # progresso intermediário real do ffmpeg
    assert all(e["scope"]["org_id"] == str(org.id) for e in got)
