"""Biblioteca de mídia: escopo/cross-tenant, Range/checksum, exclusão segura + GC,
ingestão de arquivo gerado no servidor e interfaces para outros domínios."""
from __future__ import annotations

import hashlib
import io
import secrets
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.authz import AuthContext, load_grants
from app.core.errors import DomainError, NotFound
from app.models.audit import AuditLog
from app.models.media import MediaFile
from app.modules.media import pipeline
from app.modules.media import service as svc
from app.modules.media.policy import media_settings
from tests.factories import login_as, make_org, make_user
from tests.integration.test_media_gen import (  # noqa: F401  (fixture)
    make_image,
    media_env,
    media_user,
    object_keys,
    upload,
)


async def _ready_image(client, s, tmp_path, name="a.png", w=1920, h=1080, fmt="png") -> str:
    p = make_image(tmp_path / f"{uuid.uuid4().hex}.{'jpg' if fmt == 'jpeg' else fmt}", fmt, w, h,
                   seed=secrets.randbelow(10_000))
    r = await upload(client, s, p, name)
    assert r.status_code == 202, r.text
    mid = r.json()["id"]
    assert (await pipeline.process_media(uuid.UUID(mid))).result == "ready"
    return mid


async def _uploaded(client, s, tmp_path, name="u.png") -> str:
    p = make_image(tmp_path / f"{uuid.uuid4().hex}.png", "png", 1080, 1920, seed=secrets.randbelow(10_000))
    r = await upload(client, s, p, name)
    assert r.status_code == 202
    return r.json()["id"]


@pytest.fixture
async def two_orgs(client, db, media_env):
    a, b = await make_org(db), await make_org(db)
    ua, sa = await media_user(db, client, a)
    ub, sb = await media_user(db, client, b)
    return (SimpleNamespace(id=a.id, user_id=ua.id, s=sa), SimpleNamespace(id=b.id, user_id=ub.id, s=sb))


async def test_list_is_scoped_paginated_and_filtered(client, db, media_env, two_orgs, tmp_path):
    A, B = two_orgs
    ra = await _ready_image(client, A.s, tmp_path, "Promo 100% off.png")
    await _uploaded(client, A.s, tmp_path, "b.png")
    await _uploaded(client, A.s, tmp_path, "c.png")
    await _uploaded(client, B.s, tmp_path, "d.png")
    r = await client.get("/api/v1/media", headers=A.s.headers)
    body = r.json()
    assert r.status_code == 200 and body["total"] == 3 and {i["org_id"] for i in body["items"]} == {str(A.id)}
    r = await client.get("/api/v1/media", headers=B.s.headers)
    assert r.json()["total"] == 1 and r.json()["items"][0]["org_id"] == str(B.id)
    r = await client.get("/api/v1/media", params={"page_size": 2, "page": 2}, headers=A.s.headers)
    assert r.json()["total"] == 3 and len(r.json()["items"]) == 1 and r.json()["page"] == 2
    r = await client.get("/api/v1/media", params={"status": "READY"}, headers=A.s.headers)
    assert [i["id"] for i in r.json()["items"]] == [ra]
    r = await client.get("/api/v1/media", params={"q": "100%"}, headers=A.s.headers)
    assert [i["id"] for i in r.json()["items"]] == [ra]
    r = await client.get("/api/v1/media", params={"q": "%"}, headers=A.s.headers)
    assert [i["id"] for i in r.json()["items"]] == [ra]  # '%' é literal, não curinga
    r = await client.get("/api/v1/media", params={"org_id": str(A.id)}, headers=B.s.headers)
    assert r.json()["total"] == 0  # filtro não amplia escopo
    r = await client.get("/api/v1/media", params={"orientation": "HORIZONTAL", "kind": "IMAGE"}, headers=A.s.headers)
    assert [i["id"] for i in r.json()["items"]] == [ra]
    admin = await make_user(db, role="admin", mfa=True)
    sadm = await login_as(client, admin)
    r = await client.get("/api/v1/media", headers=sadm.headers)
    assert r.json()["total"] == 4


async def test_cross_tenant_is_404_everywhere(client, db, media_env, two_orgs, tmp_path):
    A, B = two_orgs
    mid = await _ready_image(client, A.s, tmp_path)
    h = B.s.headers
    for method, path in (
        ("GET", f"/api/v1/media/{mid}"),
        ("GET", f"/api/v1/media/{mid}/content"),
        ("HEAD", f"/api/v1/media/{mid}/content"),
        ("GET", f"/api/v1/media/{mid}/preview"),
        ("GET", f"/api/v1/media/{mid}/usage"),
        ("POST", f"/api/v1/media/{mid}/retry"),
        ("DELETE", f"/api/v1/media/{mid}"),
    ):
        r = await client.request(method, path, headers=h)
        assert r.status_code == 404, (method, path, r.status_code)
    r = await client.patch(f"/api/v1/media/{mid}", json={"name": "x"}, headers=h)
    assert r.status_code == 404
    # inexistente também 404 (indistinguível)
    r = await client.get(f"/api/v1/media/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404
    # e A continua vendo
    assert (await client.get(f"/api/v1/media/{mid}", headers=A.s.headers)).status_code == 200


async def test_content_range_etag_and_checksum(client, db, media_env, two_orgs, tmp_path):
    A, _ = two_orgs
    mid = await _ready_image(client, A.s, tmp_path, fmt="jpeg", name="foto.jpg")
    d = (await client.get(f"/api/v1/media/{mid}", headers=A.s.headers)).json()
    sha, size = d["output"]["sha256"], d["output"]["size_bytes"]
    h = A.s.headers
    r = await client.get(f"/api/v1/media/{mid}/content", headers=h)
    assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == sha and len(r.content) == size
    assert r.headers["etag"] == f'"{sha}"' and r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "image/jpeg"
    full = r.content
    r = await client.get(f"/api/v1/media/{mid}/content", headers={**h, "Range": "bytes=0-99"})
    assert r.status_code == 206 and r.content == full[:100] and r.headers["content-range"] == f"bytes 0-99/{size}"
    r = await client.get(f"/api/v1/media/{mid}/content", headers={**h, "Range": "bytes=-10"})
    assert r.status_code == 206 and r.content == full[-10:]
    r = await client.get(f"/api/v1/media/{mid}/content", headers={**h, "Range": "bytes=100-", "If-Range": f'"{sha}"'})
    assert r.status_code == 206 and r.content == full[100:]
    r = await client.get(f"/api/v1/media/{mid}/content", headers={**h, "Range": "bytes=100-", "If-Range": '"outro"'})
    assert r.status_code == 200 and r.content == full
    r = await client.get(f"/api/v1/media/{mid}/content", headers={**h, "Range": f"bytes={size}-"})
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{size}"
    r = await client.head(f"/api/v1/media/{mid}/content", headers=h)
    assert r.status_code == 200 and r.content == b"" and r.headers["content-length"] == str(size)
    r = await client.get(f"/api/v1/media/{mid}/preview", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:3] == b"\xff\xd8\xff"
    # sem credencial → 401; nunca token em query
    r = await client.get(f"/api/v1/media/{mid}/content?token=abc")
    assert r.status_code == 401
    # rotas legadas inseguras removidas
    assert (await client.get(f"/api/v1/media/{mid}/serve", headers=h)).status_code in (404, 405)
    assert (await client.get(f"/api/v1/media/{mid}/status", headers=h)).status_code in (404, 405)


async def test_delete_blocked_when_referenced_and_audited(client, db, media_env, two_orgs, tmp_path):
    A, _ = two_orgs
    mid = await _ready_image(client, A.s, tmp_path)
    target = uuid.UUID(mid)

    async def fake_campaigns(db_, ids):
        return {target: 2} if target in ids else {}

    async def broken(db_, ids):
        raise RuntimeError("banco do referenciador fora")

    svc.register_media_referencer("fake_campaigns", fake_campaigns)
    try:
        r = await client.delete(f"/api/v1/media/{mid}", headers=A.s.headers)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "MEDIA_IN_USE"
        assert r.json()["error"]["details"]["references"] == {"fake_campaigns": 2}
        u = (await client.get(f"/api/v1/media/{mid}/usage", headers=A.s.headers)).json()
        assert u == {"media_id": mid, "in_use": True, "references": {"fake_campaigns": 2}}
        svc.unregister_media_referencer("fake_campaigns")
        svc.register_media_referencer("broken", broken)
        r = await client.delete(f"/api/v1/media/{mid}", headers=A.s.headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == "MEDIA_REFERENCE_CHECK_FAILED"  # falha FECHADO
    finally:
        svc.unregister_media_referencer("fake_campaigns")
        svc.unregister_media_referencer("broken")
    keys_before = object_keys(media_env)
    r = await client.delete(f"/api/v1/media/{mid}", params={"reason": "não uso mais"}, headers=A.s.headers)
    assert r.status_code == 204
    assert object_keys(media_env) == keys_before  # físico NÃO é apagado na hora (só pelo GC)
    assert (await client.get(f"/api/v1/media/{mid}", headers=A.s.headers)).status_code == 404
    assert (await client.get("/api/v1/media", headers=A.s.headers)).json()["total"] == 0
    assert (await client.delete(f"/api/v1/media/{mid}", headers=A.s.headers)).status_code == 404
    db.expire_all()
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "media.deleted"))).scalars().all()
    assert len(a) == 1 and a[0].reason == "não uso mais" and a[0].actor_user_id == A.user_id
    # mesmo arquivo pode ser enviado de novo depois de excluído (nova mídia)
    # (idempotência só entre mídias não excluídas)


async def test_delete_blocked_by_real_creative_fk(client, db, media_env, two_orgs, tmp_path):
    """Qualquer FK para media_files.id conta como referência (cobre ``creatives`` do W-CAMP)."""
    from app.models.campaigns import Creative

    A, _ = two_orgs
    mid = await _ready_image(client, A.s, tmp_path)
    assert any(t == "creatives" for t, _ in svc._fk_columns())
    db.add(Creative(id=uuid.uuid4(), org_id=A.id, code="RD-ABCDEF", name="Anúncio", media_file_id=uuid.UUID(mid),
                    duration_seconds=30))
    await db.commit()
    r = await client.delete(f"/api/v1/media/{mid}", headers=A.s.headers)
    assert r.status_code == 409 and r.json()["error"]["details"]["references"] == {"creatives": 1}


async def test_advertiser_delete_contract(client, db, media_env, tmp_path):
    """Decisão do coordenador (AÇÃO 002): advertiser tem media.delete em escopo ORGANIZATION —
    exclui mídia PRÓPRIA não referenciada; outra org → 404; referenciada → 409; establishment_owner → 403."""
    org, other = await make_org(db), await make_org(db)
    org_id, other_id = org.id, other.id
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_id))
    adv_other = await make_user(db, role="advertiser", scope=("ORGANIZATION", other_id))
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org_id))
    s, s_other, s_owner = await login_as(client, adv), await login_as(client, adv_other), await login_as(client, owner)
    m1 = (await upload(client, s, make_image(tmp_path / "a.png", "png", 1920, 1080, seed=1), "a.png")).json()["id"]
    m2 = (await upload(client, s, make_image(tmp_path / "b.png", "png", 1920, 1080, seed=2), "b.png")).json()["id"]
    assert (await client.delete(f"/api/v1/media/{m1}", headers=s_owner.headers)).status_code == 403
    assert (await client.delete(f"/api/v1/media/{m1}", headers=s_other.headers)).status_code == 404
    target = uuid.UUID(m2)

    async def ref(db_, ids):
        return {target: 1} if target in ids else {}

    svc.register_media_referencer("test_ref", ref)
    try:
        r = await client.delete(f"/api/v1/media/{m2}", headers=s.headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == "MEDIA_IN_USE"
    finally:
        svc.unregister_media_referencer("test_ref")
    assert (await client.delete(f"/api/v1/media/{m1}", headers=s.headers)).status_code == 204
    assert (await client.get(f"/api/v1/media/{m1}", headers=s.headers)).status_code == 404


async def test_gc_purges_only_unreferenced_after_grace(client, db, media_env, two_orgs, tmp_path, monkeypatch):
    A, _ = two_orgs
    m1 = await _ready_image(client, A.s, tmp_path, "um.png")
    m2 = await _ready_image(client, A.s, tmp_path, "dois.png", w=1080, h=1920)
    for mid in (m1, m2):
        assert (await client.delete(f"/api/v1/media/{mid}", headers=A.s.headers)).status_code == 204
    keys = object_keys(media_env)
    assert len(keys) == 4  # main + preview de cada
    res = await svc.gc_deleted_media()
    assert res == {"purged": 0, "skipped": 0}  # dentro da carência (24 h)
    monkeypatch.setattr(media_settings(), "GC_GRACE_HOURS", 0)
    keep = uuid.UUID(m2)

    async def pending_download(db_, ids):  # ex.: player ainda baixando (registrado depois da exclusão)
        return {keep: 1} if keep in ids else {}

    svc.register_media_referencer("pending_download", pending_download)
    try:
        res = await svc.gc_deleted_media()
    finally:
        svc.unregister_media_referencer("pending_download")
    assert res == {"purged": 1, "skipped": 1}
    db.expire_all()
    r1 = await db.get(MediaFile, uuid.UUID(m1))
    r2 = await db.get(MediaFile, keep)
    assert r1.purged_at is not None and r2.purged_at is None
    remaining = object_keys(media_env)
    assert remaining == sorted([r2.storage_key, r2.preview_storage_key])
    res = await svc.gc_deleted_media()
    assert res == {"purged": 1, "skipped": 0}
    assert object_keys(media_env) == []
    # registro preservado (histórico) e purgado não é servido nem ao player
    with pytest.raises(NotFound):
        await svc.open_media_range(db, m1)


async def test_rename(client, db, media_env, two_orgs, tmp_path):
    A, _ = two_orgs
    mid = await _uploaded(client, A.s, tmp_path)
    r = await client.patch(f"/api/v1/media/{mid}", json={"name": "../Nova\x00 Arte.png"}, headers=A.s.headers)
    assert r.status_code == 200 and r.json()["name"] == "Nova Arte.png"
    r = await client.patch(f"/api/v1/media/{mid}", json={"name": ""}, headers=A.s.headers)
    assert r.status_code == 422


async def test_ingest_generated_file_after_commit_and_rollback(db, media_env):
    from PIL import Image

    org = await make_org(db)
    org_id = org.id
    b = io.BytesIO()
    Image.new("RGB", (1920, 1080), (200, 10, 10)).save(b, "PNG")
    data = b.getvalue()
    sys_ctx = AuthContext.system(origin="designer")  # origem fora do CHECK → normalizada p/ "system"
    m = await svc.ingest_generated_file(db, sys_ctx, org_id, "arte-final.png", "image/png", data, "DESIGN_ART")
    mid = m.id
    assert m.status == "UPLOADED" and m.origin == "DESIGN_ART" and m.created_by is None
    assert m.source_sha256 == hashlib.sha256(data).hexdigest()
    assert media_env.enqueued == []  # ainda não comitado → não enfileira
    await db.commit()
    assert media_env.enqueued == [str(mid)]
    # mesmo conteúdo → mesma mídia
    again = await svc.ingest_generated_file(db, sys_ctx, org_id, "x.png", "image/png", data, "TEMPLATE_RENDER")
    assert again.id == mid
    await db.commit()
    # rollback → objeto recém-gravado removido e nada enfileirado
    b2 = io.BytesIO()
    Image.new("RGB", (1080, 1920), (1, 2, 3)).save(b2, "PNG")
    m2 = await svc.ingest_generated_file(db, sys_ctx, org_id, "t.png", "image/png", b2.getvalue(), "TEMPLATE_RENDER")
    k2 = m2.source_storage_key
    assert await media_env.storage.exists(k2)
    await db.rollback()
    assert not await media_env.storage.exists(k2)
    assert media_env.enqueued == [str(mid)]
    # mesma validação do upload
    with pytest.raises(DomainError) as ei:
        await svc.ingest_generated_file(db, sys_ctx, org_id, "x.png", "image/png", b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\0" * 64,
                                        "DESIGN_ART")
    assert ei.value.code == "MEDIA_TYPE_NOT_ALLOWED"
    with pytest.raises(ValueError):
        await svc.ingest_generated_file(db, sys_ctx, org_id, "x.png", "image/png", data, "upload")
    await db.rollback()
    # o pipeline processa a mídia gerada como qualquer outra
    assert (await pipeline.process_media(mid)).result == "ready"
    desc = await svc.get_media_descriptors(db, [mid, uuid.uuid4()])
    assert list(desc) == [mid]
    d = desc[mid]
    assert (d.mime, d.width, d.height, d.kind, d.duration_ms) == ("image/png", 1920, 1080, "IMAGE", None)
    db.expire_all()
    row = await db.get(MediaFile, mid)
    assert d.sha256 == row.sha256 and d.size == row.size_bytes


async def test_ensure_media_usable_and_open_range_service(client, db, media_env, two_orgs, tmp_path):
    from app.models.users import User

    A, B = two_orgs
    ready = await _ready_image(client, A.s, tmp_path)
    pending = await _uploaded(client, A.s, tmp_path)
    ua = await db.get(User, A.user_id)
    ub = await db.get(User, B.user_id)
    ctx_a = AuthContext(kind="user", user=ua, grants=await load_grants(db, A.user_id))
    ctx_b = AuthContext(kind="user", user=ub, grants=await load_grants(db, B.user_id))
    m = await svc.ensure_media_usable(db, ctx_a, ready, for_update=True)
    assert m.status == "READY"
    await db.rollback()
    with pytest.raises(NotFound):
        await svc.ensure_media_usable(db, ctx_b, ready)
    with pytest.raises(DomainError) as ei:
        await svc.ensure_media_usable(db, ctx_a, pending)
    assert ei.value.code == "MEDIA_NOT_READY" and ei.value.status == 409
    # interface do player (W-PLAY): sem autorização própria, só READY
    res = await svc.open_media_range(db, ready, "bytes=0-9")
    chunks = [c async for c in res.body]
    assert res.status == 206 and len(b"".join(chunks)) == 10 and res.headers["Content-Range"].startswith("bytes 0-9/")
    with pytest.raises(NotFound):
        await svc.open_media_range(db, pending)


async def test_openapi_documents_upload(client, db, media_env):
    r = await client.get("/api/v1/openapi.json")
    if r.status_code == 404:
        pytest.skip("OpenAPI desabilitado neste ambiente")
    paths = r.json()["paths"]
    up = paths["/api/v1/media/upload"]["post"]
    assert "multipart/form-data" in up["requestBody"]["content"]
    assert "/api/v1/media/{media_id}/serve" not in paths and "/api/v1/media/{media_id}/status" not in paths
    assert {"/api/v1/media", "/api/v1/media/{media_id}/content", "/api/v1/media/{media_id}/usage"} <= set(paths)


def test_x264_preset_never_baseline(monkeypatch):
    monkeypatch.setattr(media_settings(), "X264_PRESET", "ultrafast")
    assert pipeline.x264_preset() == "superfast"
    monkeypatch.setattr(media_settings(), "X264_PRESET", "medium")
    assert pipeline.x264_preset() == "medium"
    monkeypatch.setattr(media_settings(), "X264_PRESET", "; rm -rf /")
    assert pipeline.x264_preset() == "superfast"
