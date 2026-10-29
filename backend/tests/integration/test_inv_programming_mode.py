"""W-INV — REQ-ORG-003 (MAT-1): ``screens.programming_mode`` honrado pelo GERADOR REAL.

ESTABLISHMENT_DEFAULT ⇒ programação interna comum do estabelecimento; CUSTOM ⇒ programação
interna própria da tela (``internal_content.screen_id``). Caminho de produto: criativos INTERNAL
criados pela RD e aprovados pela rota de revisão; listas cadastradas por ``/internal-content``;
troca de modo pela rota do W-ORG ``PATCH /screens/{id}`` (flush + invalidação) ⇒ nova versão
assinada. PostgreSQL real, sem dublês."""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import func, select

from app.models.manifests import ManifestVersion
from app.modules.inventory import service as inv
from app.modules.playback import protocol as P
from tests.camp_support import make_media
from tests.factories import login_as, make_user
from tests.inv_support import mk_establishment, mk_org, mk_screen


async def _rd_internal(client, sa, org_id, name: str) -> dict:
    m = await make_media(client._db, SimpleNamespace(id=org_id), duration_ms=60000)
    r = await client.post("/api/v1/creatives", headers=sa.headers, json={
        "org_id": str(org_id), "name": name, "media_file_id": str(m.id), "duration_seconds": 60, "kind": "INTERNAL"})
    assert r.status_code == 201, r.text
    c = r.json()
    r = await client.post(f"/api/v1/creatives/{c['id']}/review", headers=sa.headers, json={"decision": "APPROVE"})
    assert r.status_code == 200, r.text
    return c


async def _latest(db, screen_id):
    sm = await inv.current_manifest(db, screen_id)
    await db.commit()
    assert P.verify_es256(inv.server_public_keys()[0]["public_key"], sm.manifest, sm.signature)
    return sm


def _pattern_ids(m: dict) -> list:
    return [[m["creatives"][x["c"]]["creative_id"] for x in blk] for blk in m["internal_pattern"]]


async def _max_version(db, screen_id) -> int:
    return (await db.execute(select(func.max(ManifestVersion.version)).where(
        ManifestVersion.screen_id == screen_id))).scalar_one() or 0


async def test_programming_mode_default_vs_custom_and_mode_switch_regenerates(db, client):
    owner = await mk_org(db, "Rede Dona")
    est = await mk_establishment(db, owner)
    a = await mk_screen(db, est)
    b = await mk_screen(db, est)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    client._db = db
    common = await _rd_internal(client, sa, owner.id, "Grade comum")
    own = await _rd_internal(client, sa, owner.id, "Grade própria da tela B")
    r = await client.post("/api/v1/internal-content", headers=sa.headers,
                          json={"establishment_id": str(est.id), "creative_id": common["id"]})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/internal-content", headers=sa.headers,
                          json={"establishment_id": str(est.id), "screen_id": str(b.id), "creative_id": own["id"]})
    assert r.status_code == 201, r.text

    # ambas DEFAULT: a lista própria de B fica preparada, mas B toca a grade COMUM
    ma, mb = await _latest(db, a.id), await _latest(db, b.id)
    assert _pattern_ids(ma.manifest) == [[common["id"], common["id"]]]
    assert _pattern_ids(mb.manifest) == [[common["id"], common["id"]]]
    assert own["id"] not in {v["creative_id"] for v in mb.manifest["creatives"].values()}
    v_b0, v_a0 = await _max_version(db, b.id), await _max_version(db, a.id)

    # B → CUSTOM pela rota do W-ORG: nova versão assinada com a programação PRÓPRIA da tela
    r = await client.patch(f"/api/v1/screens/{b.id}", headers=sa.headers, json={"programming_mode": "CUSTOM"})
    assert r.status_code == 200 and r.json()["programming_mode"] == "CUSTOM", r.text
    assert await _max_version(db, b.id) == v_b0 + 1
    mb2 = await _latest(db, b.id)
    assert mb2.version == v_b0 + 1
    assert _pattern_ids(mb2.manifest) == [[own["id"], own["id"]]]
    # unidades externas não vendidas também usam a programação própria
    ext_internal = {mb2.manifest["creatives"][e["c"]]["creative_id"]
                    for es in mb2.manifest["external_plan"].values() for e in es}
    assert ext_internal == {own["id"]}
    # A (DEFAULT) não muda: mesma versão, grade comum; manifestos de A e B agora diferem
    assert await _max_version(db, a.id) == v_a0
    assert mb2.plan_sha256 != (await _latest(db, a.id)).plan_sha256

    # B → DEFAULT: nova versão de volta à grade comum
    r = await client.patch(f"/api/v1/screens/{b.id}", headers=sa.headers,
                           json={"programming_mode": "ESTABLISHMENT_DEFAULT"})
    assert r.status_code == 200, r.text
    mb3 = await _latest(db, b.id)
    assert mb3.version == v_b0 + 2 and _pattern_ids(mb3.manifest) == [[common["id"], common["id"]]]

    # A → CUSTOM sem lista própria: bloco interno vai para o fallback (padrão interno vazio)
    r = await client.patch(f"/api/v1/screens/{a.id}", headers=sa.headers, json={"programming_mode": "CUSTOM"})
    assert r.status_code == 200, r.text
    ma2 = await _latest(db, a.id)
    assert ma2.version == v_a0 + 1 and ma2.manifest["internal_pattern"] == []
