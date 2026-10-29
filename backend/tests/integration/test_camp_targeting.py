"""Segmentação → telas (PostgreSQL real): combinação cidade + categoria − exclusão, região/UF/bairro/CEP,
vendáveis, duração, conflitos, nunca TV individual, nomes de estabelecimentos ocultos, cross-tenant."""
import uuid


from app.models.campaigns import Campaign
from app.modules.campaigns import service as S
from app.modules.campaigns import targeting as TG
from tests.camp_support import Tax, future_monday, make_est, make_screen, set_advertiser
from tests.factories import login_as, make_org, make_user


async def _probe(db, org):
    c = Campaign(id=uuid.uuid4(), org_id=org.id, name="Probe", status="RASCUNHO", duration_s=30)
    db.add(c)
    await db.commit()
    return c


async def _resolve(db, c, targets, duration=30):
    res = await TG.resolve(db, TG.specs_from(targets), duration_s=duration, campaign=c)
    return set(res.screen_ids), res


async def test_combinacao_cidade_categoria_menos_exclusao(db):
    t = await Tax().create(db)
    org = await make_org(db)
    await set_advertiser(db, org, category=t.supermercado)  # sem segmentos → só "mesmo ramo" pode bloquear
    gyn_gym1 = await make_est(db, category=t.academia, neighborhood="Setor Bueno", postal_code="74230010")
    gyn_gym2 = await make_est(db, category=t.academia, neighborhood="Setor Oeste", postal_code="74110020")
    gyn_dist = await make_est(db, category=t.distribuidora)
    ana_gym = await make_est(db, city="Anápolis", category=t.academia)
    sp_gym = await make_est(db, city="São Paulo", state="SP", category=t.academia)
    s = {e.id: await make_screen(db, e) for e in (gyn_gym1, gyn_gym2, gyn_dist, ana_gym, sp_gym)}
    c = await _probe(db, org)
    ids, _ = await _resolve(db, c, [{"type": "CITY", "value": "GO:Goiânia"},
                                    {"type": "CATEGORY", "value": str(t.academia.id)}])
    assert ids == {s[gyn_gym1.id].id, s[gyn_gym2.id].id}  # AND entre tipos
    ids, _ = await _resolve(db, c, [{"type": "CITY", "value": "GO:Goiânia"}, {"type": "CITY", "value": "GO:Anápolis"},
                                    {"type": "CATEGORY", "value": str(t.academia.id)},
                                    {"type": "NEIGHBORHOOD", "value": "GO:Goiânia:Setor Oeste", "exclude": True}])
    assert ids == {s[gyn_gym1.id].id, s[ana_gym.id].id}  # OR dentro do tipo − exclusão do bairro
    # categoria pai (setor) inclui descendentes
    ids, _ = await _resolve(db, c, [{"type": "CATEGORY", "value": str(t.alimentacao.id)}])
    assert ids == {s[gyn_dist.id].id}
    ids, _ = await _resolve(db, c, [{"type": "REGION", "value": "Centro-Oeste"}])
    assert ids == {s[e].id for e in (gyn_gym1.id, gyn_gym2.id, gyn_dist.id, ana_gym.id)}
    ids, _ = await _resolve(db, c, [{"type": "STATE", "value": "SP"}])
    assert ids == {s[sp_gym.id].id}
    ids, _ = await _resolve(db, c, [{"type": "POSTAL_CODE", "value": "742"}])
    assert ids == {s[gyn_gym1.id].id}
    ids, _ = await _resolve(db, c, [{"type": "COUNTRY", "value": "BR"},
                                    {"type": "ESTABLISHMENT", "value": str(sp_gym.id), "exclude": True},
                                    {"type": "STATE", "value": "GO", "exclude": True}])
    assert ids == set()
    # estabelecimento específico SOMA ao conjunto geográfico
    ids, _ = await _resolve(db, c, [{"type": "STATE", "value": "SP"}, {"type": "ESTABLISHMENT", "value": str(gyn_dist.id)}])
    assert ids == {s[sp_gym.id].id, s[gyn_dist.id].id}


async def test_vendaveis_manutencao_duracao_e_conflito(db, monkeypatch):
    t = await Tax().create(db)
    org = await make_org(db)
    await set_advertiser(db, org, category=t.distribuidora, segments=[t.bebidas])
    gym = await make_est(db, category=t.academia)
    dist = await make_est(db, category=t.distribuidora, segments=[t.bebidas])  # mesmo ramo → BLOCK
    inactive = await make_est(db, category=t.academia)
    inactive.is_active = False
    await db.commit()
    ok = await make_screen(db, gym)
    maint = await make_screen(db, gym, status="MAINTENANCE")  # manutenção NÃO sai do inventário
    await make_screen(db, gym, sellable=False)
    await make_screen(db, gym, status="DECOMMISSIONED")
    await make_screen(db, dist)
    await make_screen(db, inactive)
    c = await _probe(db, org)
    ids, res = await _resolve(db, c, [{"type": "CITY", "value": "GO:Goiânia"}])
    assert ids == {ok.id, maint.id}
    assert len(res.excluded_conflict_establishments) == 1
    assert next(iter(res.excluded_conflict_establishments.values())).reason_code == "MESMO_RAMO"
    # elegibilidade de duração: bloco externo de 60 s (ex.: 3+1) não recebe 120 s
    import sys
    import types

    fake = types.ModuleType("app.modules.inventory.service")

    async def external_seconds_by_screen(db_, screen_ids):
        return {sid: (60 if sid == maint.id else 120) for sid in screen_ids}

    fake.external_seconds_by_screen = external_seconds_by_screen
    monkeypatch.setitem(sys.modules, "app.modules.inventory.service", fake)
    ids, res = await _resolve(db, c, [{"type": "CITY", "value": "GO:Goiânia"}], duration=120)
    assert ids == {ok.id} and res.excluded_duration == 1
    # interface §4
    c.duration_s = 30
    await db.commit()
    import pytest

    from app.core.errors import DomainError

    with pytest.raises(DomainError) as e:  # sem alvos gravados → erro (nunca "todas as telas")
        await S.resolve_target_screens(db, c)
    assert e.value.code == "TARGET_REQUIRED"


def test_validacoes_de_alvo():
    import pytest

    from app.core.errors import DomainError

    with pytest.raises(DomainError) as e:
        TG.validate_specs([TG.TargetSpec("SCREEN", str(uuid.uuid4()))])
    assert e.value.code == "TARGET_SCREEN_NOT_ALLOWED"
    with pytest.raises(DomainError) as e:
        TG.validate_specs([TG.TargetSpec("STATE", "GO", exclude=True)])
    assert e.value.code == "TARGET_REQUIRED"
    for t, v in (("STATE", "XX"), ("CITY", "Goiânia"), ("REGION", "LESTE"), ("POSTAL_CODE", "1"),
                 ("COUNTRY", "ARG"), ("CATEGORY", "nao-uuid")):
        with pytest.raises(DomainError):
            TG.normalize_target(t, v)
    assert TG.normalize_target("CITY", "go:Goiânia") == "GO:goiania"
    assert TG.normalize_target("REGION", "Centro-Oeste") == "CENTRO_OESTE"
    assert TG.normalize_target("NEIGHBORHOOD", "GO:Goiânia:Setor Bueno") == "GO:goiania:setor-bueno"


async def test_api_alvos_tv_individual_estabelecimento_e_cross_tenant(db, client):
    t = await Tax().create(db)
    org, org_b = await make_org(db), await make_org(db)
    await set_advertiser(db, org, category=t.academia)
    est = await make_est(db, category=t.supermercado, segments=[t.alimentos])
    await make_screen(db, est)
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    ub = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_b.id))
    s, sb = await login_as(client, u), await login_as(client, ub)
    start = future_monday()
    r = await client.post("/api/v1/campaigns", headers=s.headers, json={
        "name": "Campanha A", "start_date": start.isoformat(), "end_date": start.isoformat(),
        "targets": [{"type": "CITY", "value": "GO:Goiânia"}]})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # TV individual não existe como tipo de alvo
    r = await client.put(f"/api/v1/campaigns/{cid}/targets", headers=s.headers,
                         json={"targets": [{"type": "SCREEN", "value": str(uuid.uuid4())}]})
    assert r.status_code == 422
    # estabelecimento específico: anunciante não escolhe (nomes ocultos por padrão)
    r = await client.put(f"/api/v1/campaigns/{cid}/targets", headers=s.headers,
                         json={"targets": [{"type": "ESTABLISHMENT", "value": str(est.id)}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "TARGET_ESTABLISHMENT_NOT_ALLOWED"
    # outra organização: 404 em ler/alterar alvos e campanha
    assert (await client.get(f"/api/v1/campaigns/{cid}", headers=sb.headers)).status_code == 404
    r = await client.put(f"/api/v1/campaigns/{cid}/targets", headers=sb.headers,
                         json={"targets": [{"type": "STATE", "value": "SP"}]})
    assert r.status_code == 404
    assert (await client.get("/api/v1/campaigns", headers=sb.headers)).json()["total"] == 0
    # disponibilidade: só contagens, sem nomes/ids de estabelecimentos
    r = await client.post("/api/v1/campaigns/availability", headers=s.headers,
                          json={"targets": [{"type": "CITY", "value": "GO:Goiânia"}], "duration_s": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["screens_available"] == 1 and body["establishments_count"] == 1
    raw = r.text
    assert est.name not in raw and str(est.id) not in raw
    # telas da campanha: cliente recebe a visão PÚBLICA (friendly_id/UF/orientação), sem estabelecimento
    r = await client.get(f"/api/v1/campaigns/{cid}/screens", headers=s.headers)
    assert r.status_code == 200 and r.json()["source"] == "PREVISAO" and r.json()["total"] == 1
    item = r.json()["items"][0]
    assert set(item) == {"friendly_id", "state", "orientation", "letterbox"}
    assert item["friendly_id"].startswith("TEL-GO-") and item["state"] == "GO"
    assert est.name not in r.text and str(est.id) not in r.text
    assert (await client.get(f"/api/v1/campaigns/{cid}/screens", headers=sb.headers)).status_code == 404
    r = await client.get(f"/api/v1/campaigns/{cid}/conflicts", headers=s.headers)
    assert r.status_code == 200 and "blocked" not in r.json()
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    r = await client.get(f"/api/v1/campaigns/{cid}/screens", headers=sa.headers)
    assert r.status_code == 200 and len(r.json()["items"]) == 1
    assert r.json()["items"][0]["establishment_id"] == str(est.id) and r.json()["items"][0]["friendly_id"]
    # equipe RD pode segmentar por estabelecimento específico
    r = await client.put(f"/api/v1/campaigns/{cid}/targets", headers=sa.headers,
                         json={"targets": [{"type": "ESTABLISHMENT", "value": str(est.id)}]})
    assert r.status_code == 200
