"""Upload de mídia em STREAMING (API real, PostgreSQL real, arquivos reais gerados aqui)."""
from __future__ import annotations

import hashlib
import io
import os
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditLog
from app.models.media import MediaFile
from tests.factories import login_as, make_org, make_user
from tests.integration.test_media_gen import (  # noqa: F401  (fixture)
    make_image,
    make_video,
    media_env,
    media_user,
    object_keys,
    staging_files,
    upload,
)

MiB = 1024 * 1024
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _filler(total: int, head: bytes = b"", chunk: int = 256 * 1024):
    yield head
    left = total - len(head)
    block = b"\x00" * chunk
    while left > 0:
        n = min(chunk, left)
        yield block[:n]
        left -= n


async def test_upload_png_streaming_creates_uploaded_media(client, db, media_env, tmp_path):
    org = await make_org(db)
    u, s = await media_user(db, client, org)
    p = make_image(tmp_path / "a.png", "png", 1920, 1080)
    raw = p.read_bytes()
    r = await upload(client, s, p, "Banner Loja.png", "image/png")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "UPLOADED" and body["kind"] == "IMAGE" and body["duplicate"] is False
    assert body["org_id"] == str(org.id)
    assert body["source"]["format"] == "png" and body["source"]["mime"] == "image/png"
    assert body["source"]["size_bytes"] == len(raw)
    assert body["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert body["output"] is None and body["content_url"] is None
    # objeto com chave gerada pelo servidor; staging limpo; processamento enfileirado APÓS o commit
    assert object_keys(media_env) == [f"media/{org.id}/{body['id']}/source"]
    assert staging_files(media_env) == []
    assert media_env.enqueued == [body["id"]]
    m = (await db.execute(select(MediaFile).where(MediaFile.id == uuid.UUID(body["id"])))).scalar_one()
    assert m.created_by == u.id and m.origin == "UPLOAD"
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "media.uploaded"))).scalars().all()
    assert len(a) == 1 and a[0].entity_id == body["id"] and a[0].org_id == org.id


async def test_idempotent_same_file_same_media(client, db, media_env, tmp_path):
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    p = make_image(tmp_path / "a.jpg", "jpeg", 1920, 1080)
    r1 = await upload(client, s, p, "a.jpg", "image/jpeg")
    r2 = await upload(client, s, p, "outro-nome.jpg", "image/jpeg")
    assert r1.status_code == 202 and r2.status_code == 200, r2.text
    assert r2.json()["duplicate"] is True and r2.json()["id"] == r1.json()["id"]
    assert len(object_keys(media_env)) == 1  # o segundo upload foi descartado
    assert media_env.enqueued == [r1.json()["id"]]
    n = (await db.execute(select(MediaFile).where(MediaFile.org_id == org.id))).scalars().all()
    assert len(n) == 1
    # mesmo arquivo em OUTRA organização → mídia própria (idempotência é por organização)
    org_b = await make_org(db)
    _, sb = await media_user(db, client, org_b)
    r3 = await upload(client, sb, p, "a.jpg", "image/jpeg")
    assert r3.status_code == 202 and r3.json()["id"] != r1.json()["id"]


async def test_path_traversal_in_filename_is_ignored(client, db, media_env, tmp_path):
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    p = make_image(tmp_path / "a.png", "png", 1920, 1080)
    r = await upload(client, s, p, "../../../../etc/cron.d/evil.png", "image/png")
    assert r.status_code == 202, r.text
    b = r.json()
    assert b["original_filename"] == "evil.png" and b["name"] == "evil.png"
    keys = object_keys(media_env)
    assert keys == [f"media/{org.id}/{b['id']}/source"]
    # nada foi criado fora da raiz de objetos
    created = {str(x.relative_to(media_env.root)).replace("\\", "/") for x in media_env.root.rglob("*")}
    assert not any("etc" in c or "evil" in c for c in created)


@pytest.mark.parametrize(
    "content,filename,detected",
    [
        (b"isto nao e um video, e um texto qualquer\n" * 50, "video.mp4", "text"),
        (b"PK\x03\x04" + os.urandom(4000), "video.mp4", "zip"),
        (b"MZ\x90\x00" + os.urandom(4000), "foto.png", "exe"),
        (b"%PDF-1.4\n" + os.urandom(4000), "arte.jpg", "pdf"),
        (b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'></svg>" * 10, "arte.png", "text"),
    ],
)
async def test_magic_bytes_reject_fake_content(client, db, media_env, content, filename, detected):
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    r = await upload(client, s, content, filename, "video/mp4")
    assert r.status_code == 415, r.text
    err = r.json()["error"]
    assert err["code"] == "MEDIA_TYPE_NOT_ALLOWED" and err["details"]["detected"] == detected
    assert object_keys(media_env) == [] and staging_files(media_env) == []
    assert media_env.enqueued == []
    assert (await db.execute(select(MediaFile))).scalars().all() == []
    rej = (await db.execute(select(AuditLog).where(AuditLog.action == "media.upload_rejected"))).scalars().all()
    assert len(rej) == 1 and rej[0].after_data["code"] == "MEDIA_TYPE_NOT_ALLOWED"


async def test_webp_rejected_by_content_and_extension(client, db, media_env, tmp_path):
    from PIL import Image

    org = await make_org(db)
    _, s = await media_user(db, client, org)
    b = io.BytesIO()
    Image.new("RGB", (1920, 1080), (10, 20, 30)).save(b, "WEBP")
    r = await upload(client, s, b.getvalue(), "foto.png", "image/png")  # WebP disfarçado de PNG
    assert r.status_code == 415 and r.json()["error"]["details"]["detected"] == "webp"
    r = await upload(client, s, b.getvalue(), "foto.webp", "image/webp")
    assert r.status_code == 415 and r.json()["error"]["code"] == "MEDIA_EXTENSION_NOT_ALLOWED"
    assert object_keys(media_env) == []


async def test_executable_extension_rejected(client, db, media_env):
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    r = await upload(client, s, b"MZ\x90\x00" + b"\x00" * 100, "hack.exe", "application/x-msdownload")
    assert r.status_code == 415 and r.json()["error"]["code"] == "MEDIA_EXTENSION_NOT_ALLOWED"


async def test_png_renamed_jpg_accepted_by_real_content(client, db, media_env, tmp_path):
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    p = make_image(tmp_path / "x.png", "png", 1080, 1920)
    r = await upload(client, s, p, "foto.jpg", "image/jpeg")
    assert r.status_code == 202, r.text
    b = r.json()
    assert b["source"]["format"] == "png" and b["source"]["mime"] == "image/png"
    assert b["extension_mismatch"] is True
    m = await db.get(MediaFile, uuid.UUID(b["id"]))
    assert m.declared_mime == "image/jpeg" and m.original_extension == "jpg"


async def test_image_limit_exact_accepted_plus_one_rejected(client, db, media_env):
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    limit = 10 * MiB
    r = await upload(client, s, _filler(limit, PNG_SIG + b"\x00" * 100), "exato.png", "image/png")
    assert r.status_code == 202, r.text  # no limite: aceito no envio (o decode real é no worker)
    assert r.json()["source"]["size_bytes"] == limit
    counter: dict = {}
    r = await upload(client, s, _filler(limit + 1, PNG_SIG + b"\x01" * 100), "grande.png", "image/png",
                     counter=counter)
    assert r.status_code == 413, r.text
    err = r.json()["error"]
    assert err["code"] == "MEDIA_TOO_LARGE" and err["details"] == {"limit_bytes": limit, "kind": "IMAGE"}
    assert len(object_keys(media_env)) == 1  # só o primeiro
    assert staging_files(media_env) == []


async def test_upload_aborted_during_streaming(client, db, media_env):
    """O servidor para de ler assim que passa do limite (não recebe os 64 MB)."""
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    counter: dict = {}
    r = await upload(client, s, _filler(64 * MiB, PNG_SIG + b"\x00" * 100), "bomba.png", "image/png", counter=counter)
    assert r.status_code == 413
    assert counter["sent"] < 12 * MiB, counter
    assert object_keys(media_env) == [] and staging_files(media_env) == []


async def test_video_limit_applied_while_streaming(client, db, media_env, tmp_path, monkeypatch):
    """Limite de vídeo REDUZIDO por setting de teste (1 MiB) para não gerar 200 MB."""
    monkeypatch.setattr(settings, "MAX_VIDEO_SIZE_MB", 1)
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    v = make_video(tmp_path / "v.mp4", 1920, 1080, seconds=1)
    head = v.read_bytes()
    counter: dict = {}
    r = await upload(client, s, _filler(20 * MiB, head), "v.mp4", "video/mp4", counter=counter)
    assert r.status_code == 413, r.text
    assert r.json()["error"]["details"] == {"limit_bytes": 1 * MiB, "kind": "VIDEO"}
    assert counter["sent"] < 3 * MiB
    assert object_keys(media_env) == [] and staging_files(media_env) == []
    # dentro do limite reduzido ainda passa
    small = make_video(tmp_path / "s.mp4", 1920, 1080, seconds=0.2, audio=False)
    assert small.stat().st_size < MiB
    r = await upload(client, s, small, "s.mp4", "video/mp4")
    assert r.status_code == 202 and r.json()["kind"] == "VIDEO"


async def test_content_length_precheck(client, db, media_env):
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    r = await client.post("/api/v1/media/upload", content=b"x",
                          headers={**s.headers, "Content-Type": "multipart/form-data; boundary=abc",
                                   "Content-Length": str(300 * MiB)})
    assert r.status_code == 413 and r.json()["error"]["code"] == "MEDIA_TOO_LARGE"


async def test_empty_and_missing_file(client, db, media_env):
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    r = await upload(client, s, b"", "vazio.png", "image/png")
    assert r.status_code == 400 and r.json()["error"]["code"] == "MEDIA_EMPTY"
    r = await client.post("/api/v1/media/upload", content=b"--b\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\n1\r\n--b--\r\n",
                          headers={**s.headers, "Content-Type": "multipart/form-data; boundary=b"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "MEDIA_FILE_REQUIRED"
    r = await client.post("/api/v1/media/upload", content=b"abc", headers={**s.headers, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "MEDIA_MALFORMED_UPLOAD"
    assert object_keys(media_env) == []


async def test_org_comes_from_scope_never_from_query(client, db, media_env, tmp_path):
    org_a, org_b = await make_org(db), await make_org(db)
    _, sa = await media_user(db, client, org_a)
    p = make_image(tmp_path / "a.png", "png", 1920, 1080)
    # org na query → recusada
    body_ok = await upload(client, sa, p, "a.png")
    assert body_ok.status_code == 202 and body_ok.json()["org_id"] == str(org_a.id)
    r = await client.post(f"/api/v1/media/upload?org_id={org_b.id}", content=b"", headers=sa.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "MEDIA_ORG_IN_QUERY_NOT_ALLOWED"
    # org de OUTRO tenant no campo → 404 (não vaza existência); contexto de outro tenant → 404
    p2 = make_image(tmp_path / "b.png", "png", 1080, 1920)
    r = await upload(client, sa, p2, "b.png", org_id=org_b.id)
    assert r.status_code == 404
    r = await upload(client, sa, p2, "b.png", headers={"X-RD-Context": f"adv:{org_b.id}"})
    assert r.status_code == 404
    # org inexistente → 404
    r = await upload(client, sa, p2, "b.png", org_id=uuid.uuid4())
    assert r.status_code == 404
    # org_id posterior ao arquivo divergente → 400 e nada fica gravado
    r = await upload(client, sa, p2, "b.png", fields_after={"org_id": str(org_b.id)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "MEDIA_ORG_MISMATCH"
    assert (await db.execute(select(MediaFile).where(MediaFile.org_id == org_b.id))).scalars().all() == []
    assert len(object_keys(media_env)) == 1  # rollback apagou o objeto do upload divergente
    assert len(media_env.enqueued) == 1


async def test_global_user_must_choose_org(client, db, media_env, tmp_path):
    org = await make_org(db)
    admin = await make_user(db, role="admin", mfa=True)
    s = await login_as(client, admin)
    p = make_image(tmp_path / "a.png", "png", 1920, 1080)
    r = await upload(client, s, p, "a.png")
    assert r.status_code == 400 and r.json()["error"]["code"] == "MEDIA_ORG_REQUIRED"
    r = await upload(client, s, p, "a.png", org_id=org.id)
    assert r.status_code == 202 and r.json()["org_id"] == str(org.id)
    r = await upload(client, s, make_image(tmp_path / "b.png", "png", 1080, 1920), "b.png",
                     headers={"X-RD-Context": f"est:{org.id}"})
    assert r.status_code == 202 and r.json()["org_id"] == str(org.id)


async def test_permissions_upload(client, db, media_env, tmp_path):
    org = await make_org(db)
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org.id))
    s = await login_as(client, owner)
    p = make_image(tmp_path / "a.png", "png", 1920, 1080)
    r = await upload(client, s, p, "a.png")
    assert r.status_code == 403  # estabelecimento NÃO envia mídia (ETAPA2 #737)
    r = await client.post("/api/v1/media/upload", content=b"")
    assert r.status_code == 401
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    sa = await login_as(client, adv)
    r = await upload(client, sa, p, "a.png")
    assert r.status_code == 202 and r.json()["org_id"] == str(org.id)


async def test_limits_endpoint(client, db, media_env):
    org = await make_org(db)
    _, s = await media_user(db, client, org)
    r = await client.get("/api/v1/media/limits", headers=s.headers)
    assert r.status_code == 200
    b = r.json()
    assert b["image"]["max_bytes"] == 10 * MiB and b["video"]["max_bytes"] == 200 * MiB
    assert "webp" not in b["image"]["extensions"]
