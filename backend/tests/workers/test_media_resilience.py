"""
BUG-QA-01 (P002 §50 "worker reinicia"): queda/reinício do worker no meio do processamento.

Lease curto renovado por heartbeat, job de manutenção que reenfileira lease vencido UMA vez
(com backoff e limite de tentativas), limpeza dos restos da execução interrompida, abandono
de execução que perdeu o lease e ffmpeg que morre junto com o processo do worker.

Tudo REAL: PostgreSQL, Redis, ffmpeg/ffprobe, arquivos gerados aqui; as "quedas" são
processos Python separados mortos com SIGKILL (mesmo código da task Celery).
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.core.timeutils import utcnow
from app.models.audit import AuditLog
from app.models.media import MediaFile
from app.modules.media import pipeline
from app.modules.media import service as svc
from app.modules.media.policy import media_settings
from tests.factories import make_org
from tests.integration.test_media_gen import (  # noqa: F401  (fixture)
    make_image,
    make_video,
    media_env,
    media_user,
    object_keys,
    staging_files,
    upload,
)

APP_DIR = str(Path(pipeline.__file__).resolve().parents[3])
CHILD = (
    "import asyncio, json, sys, uuid\n"
    "from app.modules.media import pipeline\n"
    "out = asyncio.run(pipeline.process_media(uuid.UUID(sys.argv[1])))\n"
    "print('RESULT ' + json.dumps(out.as_dict()), flush=True)\n"
)


# ─── utilidades ──────────────────────────────────────────────────────────────

@pytest.fixture
async def ctx_org(client, db, media_env):
    org = await make_org(db)
    u, s = await media_user(db, client, org)
    return SimpleNamespace(id=org.id), SimpleNamespace(id=u.id), s


async def _up(client, s, path: Path, name: str) -> str:
    r = await upload(client, s, path, name)
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def _snap(db, mid) -> SimpleNamespace:
    await db.rollback()
    row = (
        await db.execute(
            select(MediaFile.status, MediaFile.lease_until, MediaFile.progress_pct, MediaFile.attempts,
                   MediaFile.status_code, MediaFile.storage_key, MediaFile.preview_storage_key)
            .where(MediaFile.id == uuid.UUID(str(mid)))
        )
    ).one()
    await db.rollback()
    return SimpleNamespace(status=row[0], lease_until=row[1], progress=row[2], attempts=row[3], code=row[4],
                           storage_key=row[5], preview_key=row[6])


async def _wait(db, mid, pred, timeout: float, what: str) -> SimpleNamespace:
    deadline = time.monotonic() + timeout
    while True:
        snap = await _snap(db, mid)
        if pred(snap):
            return snap
        if time.monotonic() > deadline:
            raise AssertionError(f"timeout esperando {what}: {snap}")
        await asyncio.sleep(0.2)


def _ffmpeg_pids(marker: str) -> list[int]:
    """ffmpeg VIVOS (não zumbis) cujo cmdline contém ``marker``."""
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
            with open(f"/proc/{d}/stat", "rb") as f:
                state = f.read().rsplit(b")", 1)[1].split()[0]
        except OSError:
            continue
        if argv and argv[0].endswith("ffmpeg") and state != b"Z" and any(marker in a for a in argv):
            pids.append(int(d))
    return pids


def _child_env(env, **media) -> dict:
    e = dict(os.environ)
    e.update(
        ENVIRONMENT="test",
        DATABASE_URL=settings.DATABASE_URL,
        REDIS_URL=settings.REDIS_URL,
        REDIS_KEY_PREFIX=settings.REDIS_KEY_PREFIX,
        STORAGE_PROVIDER="local",
        STORAGE_LOCAL_PATH=str(env.root),
        EMAIL_BACKEND="memory",
        PYTHONUNBUFFERED="1",
    )
    e.update({f"MEDIA_{k}": str(v) for k, v in media.items()})
    return e


def _spawn(mid: str, env: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", CHILD, mid], cwd=APP_DIR, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)


def _result_of(proc: subprocess.Popen, timeout: float = 300) -> dict:
    out, _ = proc.communicate(timeout=timeout)
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.startswith("RESULT ")]
    assert lines, out.decode("utf-8", "replace")[-3000:]
    return json.loads(lines[-1][len("RESULT "):])


def _slow(monkeypatch, *, lease: float, heartbeat: float) -> None:
    ms = media_settings()
    monkeypatch.setattr(ms, "LEASE_S", lease)
    monkeypatch.setattr(ms, "HEARTBEAT_S", heartbeat)
    monkeypatch.setattr(ms, "X264_PRESET", "slow")
    monkeypatch.setattr(ms, "FFMPEG_THREADS", 1)


def _fast(monkeypatch) -> None:
    ms = media_settings()
    monkeypatch.setattr(ms, "LEASE_S", 90)
    monkeypatch.setattr(ms, "HEARTBEAT_S", 15.0)
    monkeypatch.setattr(ms, "X264_PRESET", "superfast")
    monkeypatch.setattr(ms, "FFMPEG_THREADS", 4)


async def _cancel(task: asyncio.Task) -> None:
    """Teste falhou no meio: não deixar a execução pendurada após o fim do event loop."""
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class _Enq:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def __call__(self, media_id, countdown=None):
        self.calls.append((media_id, countdown))


# ─── 1. lease vencido ⇒ reenfileira UMA vez (com backoff) ────────────────────

async def test_expired_lease_requeued_exactly_once_and_leftovers_cleaned(client, db, media_env, ctx_org, tmp_path):
    org, _, s = ctx_org
    enq = _Enq()
    svc.set_enqueuer(enq)
    mid = await _up(client, s, make_image(tmp_path / "a.png", "png", 1920, 1080), "a.png")
    alive = await _up(client, s, make_image(tmp_path / "b.png", "png", 1080, 1920), "b.png")
    # mídia 1: worker morreu no meio (lease vencido) deixando workspace e uma saída órfã
    await db.execute(update(MediaFile).where(MediaFile.id == uuid.UUID(mid)).values(
        status="PROCESSING", attempts=1, progress_pct=40, lease_until=utcnow() - timedelta(seconds=1)))
    # mídia 2: worker VIVO (lease no futuro) — não pode ser tocada
    await db.execute(update(MediaFile).where(MediaFile.id == uuid.UUID(alive)).values(
        status="PROCESSING", attempts=1, progress_pct=40, lease_until=utcnow() + timedelta(seconds=60)))
    await db.commit()
    ws_dead = media_env.storage.staging_dir / f"media-{mid}-a1-dead0001"
    ws_dead.mkdir(parents=True)
    (ws_dead / "main.mp4").write_bytes(b"parcial")
    ws_alive = media_env.storage.staging_dir / f"media-{alive}-a1-live0001"
    ws_alive.mkdir()
    orphan = f"media/{org.id}/{mid}/main-1-deadbeef.png"
    await media_env.storage.put_stream(orphan, b"saida-orfa")
    enq.calls.clear()  # enfileiramentos do próprio upload

    r1 = await svc.requeue_stale()
    assert r1 == {"requeued": 1, "worker_lost": 1, "exhausted": 0}
    assert enq.calls == [(mid, 30)]  # backoff 30 s na 1ª queda
    snap = await _snap(db, mid)
    assert (snap.status, snap.code, snap.lease_until, snap.progress, snap.attempts) == (
        "UPLOADED", "MEDIA_WORKER_LOST", None, 0, 1)
    assert not ws_dead.exists() and ws_alive.exists()  # só a execução morta é limpa
    assert (await _snap(db, alive)).status == "PROCESSING"
    # segunda varredura (próximo tick do beat): NADA de novo
    r2 = await svc.requeue_stale()
    assert r2 == {"requeued": 0, "worker_lost": 0, "exhausted": 0} and enq.calls == [(mid, 30)]
    await db.rollback()
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "media.processing_interrupted"))).scalars().all()
    assert [x.entity_id for x in a] == [mid]

    # a mensagem reenfileirada processa até READY; a saída órfã da execução morta é removida
    out = await pipeline.process_media(uuid.UUID(mid))
    assert out.result == "ready" and out.attempts == 2
    snap = await _snap(db, mid)
    assert snap.status == "READY" and snap.code is None
    assert "/main-2-" in snap.storage_key and "/preview-2-" in snap.preview_key
    keys = [k for k in object_keys(media_env) if f"/{mid}/" in k]
    assert sorted(keys) == sorted([snap.storage_key, snap.preview_key])
    assert not any(n.startswith(f"media-{mid}-") for n in staging_files(media_env))
    # mensagem original reentregue depois (visibility_timeout do broker) → no-op
    assert (await pipeline.process_media(uuid.UUID(mid))).result == "noop"


async def test_worker_lost_on_last_attempt_fails_instead_of_looping(client, db, media_env, ctx_org, tmp_path):
    from app.models.notifications import Notification

    _, u, s = ctx_org
    enq = _Enq()
    svc.set_enqueuer(enq)
    mx = media_settings().MAX_ATTEMPTS
    m1 = await _up(client, s, make_image(tmp_path / "a.png", "png", 1920, 1080), "derruba.png")
    m2 = await _up(client, s, make_image(tmp_path / "b.png", "png", 1080, 1920), "derruba2.png")
    for mid in (m1, m2):
        await db.execute(update(MediaFile).where(MediaFile.id == uuid.UUID(mid)).values(
            status="PROCESSING", attempts=mx, lease_until=utcnow() - timedelta(seconds=1)))
    await db.commit()
    enq.calls.clear()
    # via manutenção
    r = await svc.requeue_stale()
    assert r["exhausted"] == 2 and r["requeued"] == 0 and enq.calls == []
    for mid in (m1, m2):
        snap = await _snap(db, mid)
        assert (snap.status, snap.code) == ("FAILED", "MEDIA_WORKER_LOST")
        assert (await pipeline.process_media(uuid.UUID(mid))).result == "noop"  # não entra em loop
    assert (await svc.requeue_stale())["requeued"] == 0
    await db.rollback()
    notes = (await db.execute(select(Notification).where(Notification.event_type == "media.error"))).scalars().all()
    assert sorted(n.dedupe_key for n in notes) == sorted([m1, m2]) and {n.user_id for n in notes} == {u.id}
    # via claim direto (mensagem reentregue antes da manutenção)
    m3 = await _up(client, s, make_image(tmp_path / "c.png", "png", 1920, 1080, seed=7), "c.png")
    await db.execute(update(MediaFile).where(MediaFile.id == uuid.UUID(m3)).values(
        status="VALIDATING", attempts=mx, lease_until=utcnow() - timedelta(seconds=1)))
    await db.commit()
    out = await pipeline.process_media(uuid.UUID(m3))
    assert (out.result, out.code, out.retry) == ("failed", "MEDIA_WORKER_LOST", False)
    # o usuário pode reprocessar manualmente (zera tentativas)
    r = await client.post(f"/api/v1/media/{m3}/retry", headers=s.headers)
    assert r.status_code == 202


# ─── 2. heartbeat mantém o lease; segundo worker fica de fora ────────────────

async def test_heartbeat_keeps_short_lease_alive_and_blocks_second_worker(client, db, media_env, ctx_org, tmp_path,
                                                                          monkeypatch):
    _, _, s = ctx_org
    enq = _Enq()
    svc.set_enqueuer(enq)
    mid = await _up(client, s, make_video(tmp_path / "v.mp4", 1920, 1080, seconds=8, audio=False), "v.mp4")
    _slow(monkeypatch, lease=2, heartbeat=0.4)
    enq.calls.clear()
    run = asyncio.create_task(pipeline.process_media(uuid.UUID(mid)))
    try:
        await _heartbeat_scenario(db, mid, run, enq)
    finally:
        await _cancel(run)


async def _heartbeat_scenario(db, mid, run, enq) -> None:
    first = await _wait(db, mid, lambda x: x.status == "PROCESSING", 60, "PROCESSING")
    t0 = time.monotonic()
    leases, progress = {first.lease_until}, [first.progress]
    checked = False
    while not run.done():
        snap = await _snap(db, mid)
        if snap.status != "PROCESSING":
            break
        leases.add(snap.lease_until)
        progress.append(snap.progress)
        if time.monotonic() - t0 > 2 * 2 + 1 and not checked:
            # passou mais de 2 leases inteiros: o lease continua no futuro (renovado)
            assert snap.lease_until > utcnow(), snap
            busy = await pipeline.process_media(uuid.UUID(mid))
            assert busy.result == "busy"
            r = await svc.requeue_stale()
            assert r["requeued"] == 0 and enq.calls == []
            checked = True
        await asyncio.sleep(0.3)
    out = await run
    assert checked, "o processamento terminou cedo demais para provar a renovação (aumente a carga)"
    assert out.result == "ready" and out.attempts == 1
    assert len(leases) >= 5  # renovado várias vezes
    assert max(progress) > 5 and progress == sorted(progress)  # progresso real persistido pelo heartbeat


# ─── 3. lease perdido ⇒ execução aborta (ffmpeg morto) sem estragar o novo dono ─

async def test_run_that_lost_its_lease_aborts_and_does_not_touch_new_owner(client, db, media_env, ctx_org, tmp_path,
                                                                           monkeypatch):
    _, _, s = ctx_org
    enq = _Enq()
    svc.set_enqueuer(enq)
    mid = await _up(client, s, make_video(tmp_path / "v.mp4", 1920, 1080, seconds=8, audio=False), "v.mp4")
    _slow(monkeypatch, lease=3, heartbeat=0.4)
    enq.calls.clear()
    run_a = asyncio.create_task(pipeline.process_media(uuid.UUID(mid)))
    marker = f"media-{mid}-a1"
    try:
        await _wait(db, mid, lambda x: x.status == "PROCESSING" and x.progress >= 6, 90,
                    "transcodificação em andamento")
        assert _ffmpeg_pids(marker), "ffmpeg da execução A deveria estar rodando"
        # A "sumiu" para o resto do sistema (ex.: rede/banco): lease vence e a manutenção devolve à fila
        await db.execute(update(MediaFile).where(MediaFile.id == uuid.UUID(mid)).values(
            lease_until=utcnow() - timedelta(seconds=1)))
        await db.commit()
        assert (await svc.requeue_stale())["requeued"] == 1
        t0 = time.monotonic()
        out_a = await asyncio.wait_for(asyncio.shield(run_a), timeout=30)
    finally:
        await _cancel(run_a)
    assert out_a.result == "abandoned" and time.monotonic() - t0 < 5
    assert _ffmpeg_pids(marker) == []  # ffmpeg de A foi morto
    assert [k for k in object_keys(media_env) if "/main-" in k or "/preview-" in k] == []  # A não gravou saída
    # B (novo dono) termina normalmente
    _fast(monkeypatch)
    out_b = await pipeline.process_media(uuid.UUID(mid))
    assert out_b.result == "ready" and out_b.attempts == 2
    snap = await _snap(db, mid)
    assert snap.status == "READY" and "/main-2-" in snap.storage_key
    assert staging_files(media_env) == []


# ─── 4. dois processos worker reais na mesma mídia ───────────────────────────

async def test_two_worker_processes_never_process_the_same_media(client, db, media_env, ctx_org, tmp_path):
    _, _, s = ctx_org
    mid = await _up(client, s, make_video(tmp_path / "v.mp4", 1920, 1080, seconds=2, audio=True), "v.mp4")
    env = _child_env(media_env, X264_PRESET="superfast", FFMPEG_THREADS=2)
    p1, p2 = _spawn(mid, env), _spawn(mid, env)
    r1, r2 = await asyncio.gather(asyncio.to_thread(_result_of, p1), asyncio.to_thread(_result_of, p2))
    assert sorted([r1["result"], r2["result"]]) in (["busy", "ready"], ["noop", "ready"]), (r1, r2)
    snap = await _snap(db, mid)
    assert snap.status == "READY" and snap.attempts == 1
    await db.rollback()
    ready = (await db.execute(select(AuditLog).where(AuditLog.action == "media.ready",
                                                     AuditLog.entity_id == mid))).scalars().all()
    assert len(ready) == 1
    assert sorted(k for k in object_keys(media_env) if f"/{mid}/" in k) == sorted([snap.storage_key, snap.preview_key])


# ─── 5. worker morto (SIGKILL) no meio da transcodificação ⇒ volta e termina READY ─

async def test_worker_killed_mid_transcode_is_recovered_to_ready(client, db, media_env, ctx_org, tmp_path,
                                                                 monkeypatch):
    _, _, s = ctx_org
    enq = _Enq()
    svc.set_enqueuer(enq)
    src = make_video(tmp_path / "v.mp4", 1920, 1080, seconds=8, audio=True)
    mid = await _up(client, s, src, "v.mp4")
    lease_s = 3
    enq.calls.clear()
    proc = _spawn(mid, _child_env(media_env, LEASE_S=lease_s, HEARTBEAT_S=0.5, X264_PRESET="slow", FFMPEG_THREADS=1))
    marker = f"media-{mid}-a1"
    try:
        await _wait(db, mid, lambda x: x.status == "PROCESSING" and x.progress >= 10, 120,
                    "transcodificação em andamento no processo worker")
        assert _ffmpeg_pids(marker), "ffmpeg do worker deveria estar rodando"
    finally:
        os.kill(proc.pid, signal.SIGKILL)  # queda do processo do worker (OOM/kill/reinício)
        proc.wait(timeout=30)
    # ffmpeg morre junto com o worker (PR_SET_PDEATHSIG) — nada órfão consumindo CPU
    deadline = time.monotonic() + 5
    while _ffmpeg_pids(marker) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert _ffmpeg_pids(marker) == []
    snap = await _snap(db, mid)
    assert snap.status == "PROCESSING" and snap.attempts == 1
    # antes de o lease vencer ninguém assume (sem processamento duplicado)
    if snap.lease_until > utcnow() + timedelta(milliseconds=500):
        assert (await svc.requeue_stale())["requeued"] == 0 and enq.calls == []
        assert (await pipeline.process_media(uuid.UUID(mid))).result == "busy"
    # lease curto vence → manutenção reenfileira UMA vez e limpa o workspace da execução morta
    await _wait(db, mid, lambda x: x.lease_until is None or x.lease_until < utcnow(), lease_s + 5, "lease vencer")
    assert any(n.startswith(marker) for n in staging_files(media_env))  # resto da execução morta
    r = await svc.requeue_stale()
    assert r["requeued"] == 1 and enq.calls == [(mid, 30)]
    assert (await svc.requeue_stale())["requeued"] == 0 and len(enq.calls) == 1
    assert not any(n.startswith(marker) for n in staging_files(media_env))
    # novo worker processa e termina READY, duração preservada
    _fast(monkeypatch)
    out = await pipeline.process_media(uuid.UUID(mid))
    assert out.result == "ready" and out.attempts == 2
    snap = await _snap(db, mid)
    assert snap.status == "READY"
    d = (await client.get(f"/api/v1/media/{mid}", headers=s.headers)).json()
    assert abs(d["output"]["duration_ms"] - 8000) <= 34 and d["output"]["has_audio"] is False
    assert staging_files(media_env) == []
    assert sorted(k for k in object_keys(media_env) if f"/{mid}/" in k) == sorted([snap.storage_key, snap.preview_key])


def test_defaults_recover_in_few_minutes():
    """Contrato: lease curto, heartbeat ≤ lease/3, beat de manutenção a cada 60 s fora da fila media."""
    from app.modules.media.policy import MediaSettings, heartbeat_interval_s, requeue_backoff_s, stale_uploaded_s
    from app.workers.celery_app import celery_app

    d = MediaSettings()
    assert d.LEASE_S <= 120 and d.HEARTBEAT_S <= d.LEASE_S / 3
    entry = celery_app.conf.beat_schedule["media-maintenance"]
    assert entry["schedule"] <= 60 and entry["options"]["queue"] != "media"
    worst = d.LEASE_S + entry["schedule"] + min(d.REQUEUE_BACKOFF_MAX_S, d.RETRY_BASE_S)
    assert worst <= 240  # ≤ 4 min do SIGKILL até a mídia voltar a processar
    assert heartbeat_interval_s() <= media_settings().LEASE_S / 3
    assert [requeue_backoff_s(n) for n in (1, 2, 3, 9)] == [30, 60, 120, 120]
    assert stale_uploaded_s() >= d.REQUEUE_BACKOFF_MAX_S + 60
