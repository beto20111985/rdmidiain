"""W-ORG — disponibilidade agregada (sem nomes/endereços) e taxonomia hierárquica/política de conteúdo."""
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.screens import Screen
from app.models.taxonomy import ContentPolicy, Segment
from app.modules.taxonomy import service as tax
from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org, staff_session


async def _screens(db, est, n, **kw):
    out = []
    for i in range(n):
        s = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=f"TEL-{est.state}-{uuid.uuid4().hex[:4].upper()}",
                   name=f"S{i}", **kw)
        db.add(s)
        out.append(s)
    await db.commit()
    return out


async def test_availability_aggregates_without_names_or_addresses(client, db):
    org = await mk_org(db, roles=("PARTNER",))
    secret_name = "Supermercado Segredo Absoluto"
    secret_addr = "Rua Escondida 999"
    e1 = await mk_est(db, org, "Goiânia", "GO", name=secret_name, address=secret_addr, lat=-16.6801, lng=-49.2533)
    e2 = await mk_est(db, org, "Goiânia", "GO", name="Academia Oculta", lat=-16.7001, lng=-49.2601)
    e3 = await mk_est(db, org, "Rio Branco", "AC", name="Farmácia Discreta")
    e_off = await mk_est(db, org, "Goiânia", "GO", name="Inativo Ltda")
    await _screens(db, e1, 3)
    await _screens(db, e2, 1, screen_class="PREMIUM", orientation="VERTICAL")
    await _screens(db, e3, 2)
    await _screens(db, e_off, 5, sellable=False)  # estabelecimento sem contrato/inativo → não conta
    adv_org = await mk_org(db, roles=("ADVERTISER",))
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", adv_org.id))
    s = await login_as(client, adv)
    r = await client.get("/api/v1/catalog/availability", params={"group_by": "city"}, headers=s.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    raw = r.text
    for forbidden in (secret_name, secret_addr, "Academia Oculta", "Farmácia Discreta", str(e1.id), str(e2.id),
                      str(org.id)):
        assert forbidden not in raw
    by_city = {i["city_key"]: i for i in body["items"]}
    assert by_city["GO:goiania"]["screens"] == 4 and by_city["GO:goiania"]["establishments"] == 2
    assert by_city["GO:goiania"]["premium_screens"] == 1 and by_city["GO:goiania"]["vertical_screens"] == 1
    assert by_city["AC:rio-branco"]["screens"] == 2
    assert body["totals"]["screens"] == 6 and body["establishment_names_visible"] is False
    c = by_city["GO:goiania"]["approx_center"]
    assert c == {"lat": -16.7, "lng": -49.3}  # centro aproximado (1 casa decimal), não a coordenada real
    r = await client.get("/api/v1/catalog/availability", params={"group_by": "state", "state": "AC"}, headers=s.headers)
    assert r.json()["items"] == [{"screens": 2, "establishments": 1, "premium_screens": 0, "vertical_screens": 0,
                                  "state": "AC"}]
    r = await client.get("/api/v1/catalog/availability", params={"group_by": "region"}, headers=s.headers)
    assert {i["region"]: i["screens"] for i in r.json()["items"]} == {"CENTRO_OESTE": 4, "NORTE": 2}
    # anunciante não acessa a lista de estabelecimentos nem telas
    assert (await client.get("/api/v1/establishments", headers=s.headers)).status_code == 403
    assert (await client.get("/api/v1/screens", headers=s.headers)).status_code == 403
    # quem não tem campaigns.view nem checkout.create → 403
    tech = await staff_session(client, db, "technician", ("CITY", "GO:goiania"))
    assert (await client.get("/api/v1/catalog/availability", headers=tech.headers)).status_code == 403


async def test_availability_category_filter_includes_descendants(client, db):
    ids = await tax.ensure_reference_taxonomy(db)
    await db.commit()
    org = await mk_org(db)
    sup = await mk_est(db, org, "Goiânia", "GO", category_id=ids["supermercado"])
    acad = await mk_est(db, org, "Goiânia", "GO", category_id=ids["academia"])
    await _screens(db, sup, 2)
    await _screens(db, acad, 1)
    adv_org = await mk_org(db, roles=("ADVERTISER",))
    s = await login_as(client, await make_user(db, role="advertiser", scope=("ORGANIZATION", adv_org.id)))
    r = await client.get("/api/v1/catalog/availability", params={"group_by": "category",
                                                                "category_id": str(ids["alimentacao"])},
                         headers=s.headers)
    items = r.json()["items"]
    assert [(i["category_name"], i["screens"]) for i in items] == [("Supermercado", 2)]


async def test_taxonomy_hierarchy_rules_and_tree_queries(client, db, admin):
    h = admin.headers
    r = await client.post("/api/v1/taxonomy/nodes", json={"kind": "CATEGORY", "name": "Sem setor"}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "TAXONOMY_PARENT_INVALID"
    sec = (await client.post("/api/v1/taxonomy/nodes", json={"kind": "SECTOR", "name": "Alimentação"}, headers=h)).json()
    cat = (await client.post("/api/v1/taxonomy/nodes", json={"kind": "CATEGORY", "name": "Distribuidora",
                                                             "parent_id": sec["id"]}, headers=h)).json()
    r = await client.post("/api/v1/taxonomy/nodes", json={"kind": "SUBCATEGORY", "name": "Errado",
                                                          "parent_id": sec["id"]}, headers=h)
    assert r.status_code == 400
    sub = (await client.post("/api/v1/taxonomy/nodes", json={"kind": "SUBCATEGORY", "name": "Distribuidora de bebidas",
                                                             "parent_id": cat["id"]}, headers=h)).json()
    assert sub["ancestor_ids"] == [sec["id"], cat["id"]]
    anc = (await client.get(f"/api/v1/taxonomy/nodes/{sub['id']}/ancestors", headers=h)).json()["items"]
    assert [a["id"] for a in anc] == [sec["id"], cat["id"], sub["id"]]
    desc = (await client.get(f"/api/v1/taxonomy/nodes/{sec['id']}/descendants", headers=h)).json()["items"]
    assert {d["id"] for d in desc} == {sec["id"], cat["id"], sub["id"]}

    # segmentos: bebidas > alcoólicas > cerveja; mover subárvore atualiza descendentes
    beb = (await client.post("/api/v1/taxonomy/segments", json={"name": "Bebidas X"}, headers=h)).json()
    alc = (await client.post("/api/v1/taxonomy/segments", json={"name": "Alcoólicas X", "parent_id": beb["id"]},
                             headers=h)).json()
    cer = (await client.post("/api/v1/taxonomy/segments", json={"name": "Cerveja X", "parent_id": alc["id"]},
                             headers=h)).json()
    other = (await client.post("/api/v1/taxonomy/segments", json={"name": "Outra raiz"}, headers=h)).json()
    assert cer["depth"] == 2
    r = await client.patch(f"/api/v1/taxonomy/segments/{beb['id']}", json={"parent_id": cer["id"]}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "TAXONOMY_CYCLE"
    r = await client.patch(f"/api/v1/taxonomy/segments/{alc['id']}", json={"parent_id": other["id"]}, headers=h)
    assert r.status_code == 200, r.text
    ids = await tax.segment_ancestor_ids(db, [uuid.UUID(cer["id"])])
    assert ids == {uuid.UUID(other["id"]), uuid.UUID(alc["id"]), uuid.UUID(cer["id"])}
    d = await tax.segment_descendant_ids(db, [uuid.UUID(beb["id"])])
    assert d == {uuid.UUID(beb["id"])}
    cer_row = await db.get(Segment, uuid.UUID(cer["id"]), populate_existing=True)
    assert cer_row.depth == 2 and cer_row.path.startswith(f"/{uuid.UUID(other['id']).hex}/")
    assert "segments.updated" in await audit_actions(db, alc["id"])


async def test_taxonomy_write_is_rd_global_only(client, db):
    emp = await staff_session(client, db, "employee", ("CITY", "GO:goiania"))
    r = await client.post("/api/v1/taxonomy/segments", json={"name": "Tentativa"}, headers=emp.headers)
    assert r.status_code == 403
    assert (await client.get("/api/v1/taxonomy/segments", headers=emp.headers)).status_code == 200


async def test_default_content_policies_seed_and_inheritance(client, db, admin):
    res = await tax.ensure_default_content_policies(db)
    await db.commit()
    assert res == {"apostas": "BLOCK", "conteudo-adulto": "BLOCK", "armas": "BLOCK", "bebidas-alcoolicas": "ALLOW",
                   "tabaco": "ALLOW", "politica": "ALLOW", "farmaceuticos": "ALLOW"}
    # idempotente
    assert await tax.ensure_default_content_policies(db) == res
    await db.commit()
    assert (await db.execute(select(ContentPolicy))).scalars().all().__len__() == 7
    h = admin.headers
    apostas = (await db.execute(select(Segment).where(Segment.slug == "apostas"))).scalar_one()
    r = await client.post("/api/v1/taxonomy/segments", json={"name": "Cassino online", "parent_id": str(apostas.id)},
                          headers=h)
    child = uuid.UUID(r.json()["id"])
    blocked = await tax.blocked_segment_ids(db, [child])
    assert blocked == {child: apostas.id}
    pol = (await client.get(f"/api/v1/taxonomy/segments/{child}/policy", headers=h)).json()
    assert pol["effect"] == "BLOCK" and pol["source_segment_id"] == str(apostas.id)
    # filho com ALLOW explícito NÃO desbloqueia (bloqueio universal não é sobrescrevível)
    r = await client.put(f"/api/v1/taxonomy/content-policies/{child}",
                         json={"effect": "ALLOW", "reason": "Tentativa de liberar subcategoria"}, headers=h)
    assert r.status_code == 200
    assert await tax.blocked_segment_ids(db, [child]) == {child: apostas.id}
    # política administrável e auditada: libera bebidas alcoólicas → BLOCK
    alc = (await db.execute(select(Segment).where(Segment.slug == "bebidas-alcoolicas"))).scalar_one()
    r = await client.put(f"/api/v1/taxonomy/content-policies/{alc.id}", json={"effect": "BLOCK", "reason": "curto"},
                         headers=h)
    assert r.status_code == 422
    r = await client.put(f"/api/v1/taxonomy/content-policies/{alc.id}",
                         json={"effect": "BLOCK", "reason": "Decisão comercial da diretoria (teste)"}, headers=h)
    assert r.status_code == 200 and r.json()["is_default"] is False
    assert "content_policy.set" in await audit_actions(db, alc.id)
    lst = (await client.get("/api/v1/taxonomy/content-policies", headers=h)).json()["items"]
    assert {i["segment_slug"]: i["effect"] for i in lst}["bebidas-alcoolicas"] == "BLOCK"


async def test_competitor_links_canonical_pair(client, db, admin):
    a, b = await mk_org(db), await mk_org(db)
    r = await client.post("/api/v1/taxonomy/competitor-links",
                          json={"org_a_id": str(b.id), "org_b_id": str(a.id), "reason": "Mesma rede de distribuição"},
                          headers=admin.headers)
    assert r.status_code == 201, r.text
    lo, hi = sorted([a.id, b.id])
    assert r.json()["org_a_id"] == str(lo) and r.json()["org_b_id"] == str(hi)
    r = await client.post("/api/v1/taxonomy/competitor-links",
                          json={"org_a_id": str(a.id), "org_b_id": str(b.id), "reason": "Mesma rede de distribuição"},
                          headers=admin.headers)
    assert r.status_code == 409
    with pytest.raises(DBAPIError):
        await db.execute(text("INSERT INTO competitor_links (id, org_a_id, org_b_id, reason) VALUES "
                              "(gen_random_uuid(), :hi, :lo, 'x')"), {"hi": hi, "lo": lo})
        await db.commit()
    await db.rollback()
