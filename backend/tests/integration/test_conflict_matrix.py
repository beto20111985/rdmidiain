"""Matriz de conflitos no banco (A6 §10.7): todos os exemplos, override auditado, exceção por marca,
apostas não sobrescrevível, mesma rede, empate ⇒ BLOQUEIO, concorrência anúncio×anúncio, permissões."""
import uuid

from sqlalchemy import func, select, text

from app.core.authz import AuthContext
from app.models.campaigns import Campaign, CampaignSegment
from app.models.conflicts import ConflictDecision
from app.modules.campaigns import creatives as CR
from app.modules.conflicts import service as CS
from tests.camp_support import Tax, make_est, make_media, set_advertiser
from tests.factories import login_as, make_org, make_user

SYS = AuthContext.system("system")


async def _campaign(db, org, *segs, brand_id=None):
    c = Campaign(id=uuid.uuid4(), org_id=org.id, name="Campanha T", status="RASCUNHO", duration_s=30,
                 brand_id=brand_id)
    db.add(c)
    await db.flush()
    for s in segs:
        db.add(CampaignSegment(campaign_id=c.id, segment_id=s.id))
    await db.commit()
    return c


async def _creative(db, org, *segs, brand_id=None):
    m = await make_media(db, org)
    cr = await CR.create_creative(db, SYS, org_id=org.id, name="cr", media_file_id=m.id, duration_seconds=30,
                                  brand_id=brand_id, segment_ids=[s.id for s in segs])
    await db.commit()
    return cr


async def test_exemplos_obrigatorios(db):
    t = await Tax().create(db)
    dist_org, super_org = await make_org(db), await make_org(db)
    await set_advertiser(db, dist_org, category=t.distribuidora, segments=[t.bebidas])
    await set_advertiser(db, super_org, category=t.supermercado, segments=[t.alimentos, t.bebidas])
    distribuidora = await make_est(db, category=t.distribuidora, segments=[t.bebidas])
    supermercado = await make_est(db, category=t.supermercado, segments=[t.alimentos, t.bebidas])

    # distribuidora × distribuidora
    c1 = await _campaign(db, dist_org, t.bebidas)
    d = await CS.evaluate_conflict(db, c1, distribuidora.id)
    assert (d.result, d.reason_code) == ("BLOCK", "MESMO_RAMO")
    # supermercado: anúncio de bebida em distribuidora → bloqueado pelo segmento do anúncio
    beer = await _campaign(db, super_org, t.cerveja)
    d = await CS.evaluate_conflict(db, beer, distribuidora.id)
    assert (d.result, d.reason_code) == ("BLOCK", "SEGMENTO")
    # supermercado: anúncio de salgadinho em distribuidora → permitido
    snack = await _campaign(db, super_org, t.snacks)
    d = await CS.evaluate_conflict(db, snack, distribuidora.id, record=True, ctx=SYS)
    assert (d.result, d.reason_code) == ("ALLOW", "SEM_CONFLITO") and d.decision_id is not None
    # distribuidora anunciando em supermercado (vende bebidas) → bloqueado
    d = await CS.evaluate_conflict(db, c1, supermercado.id)
    assert (d.result, d.reason_code) == ("BLOCK", "SEGMENTO")
    # sem segmento declarado no anúncio: usa os segmentos do anunciante (supermercado vende bebidas)
    plain = await _campaign(db, super_org)
    assert (await CS.evaluate_conflict(db, plain, distribuidora.id)).reason_code == "SEGMENTO"
    # classificação do CRIATIVO prevalece sobre a da campanha/anunciante
    cr_snack = await _creative(db, super_org, t.snacks)
    d = await CS.evaluate_conflict(db, cr_snack, distribuidora.id)
    assert d.result == "ALLOW"
    await db.commit()
    n = (await db.execute(select(func.count()).select_from(ConflictDecision))).scalar_one()
    assert n == 1


async def test_override_justificativa_auditoria_e_apostas_nunca(db, client):
    t = await Tax().create(db)
    super_org = await make_org(db)
    await set_advertiser(db, super_org, category=t.supermercado, segments=[t.alimentos])
    distribuidora = await make_est(db, category=t.distribuidora, segments=[t.bebidas])
    beer = await _campaign(db, super_org, t.cerveja)
    bet = await _campaign(db, super_org, t.apostas)
    admin = await make_user(db, role="admin", mfa=True)
    s = await login_as(client, admin)
    # justificativa curta → 400 (nada gravado)
    r = await client.post("/api/v1/conflicts/overrides", headers=s.headers,
                          json={"campaign_id": str(beer.id), "establishment_id": str(distribuidora.id),
                                "justification": "curta demais"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "OVERRIDE_JUSTIFICATION_REQUIRED"
    just = "Parceria cruzada aprovada pela diretoria em 07/10/2026"
    r = await client.post("/api/v1/conflicts/overrides", headers=s.headers,
                          json={"campaign_id": str(beer.id), "establishment_id": str(distribuidora.id),
                                "justification": just})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["decision_before"]["result"] == "BLOCK" and body["decision_after"]["reason_code"] == "OVERRIDE_ADMIN"
    d = await CS.evaluate_conflict(db, beer.id, distribuidora.id)
    assert (d.result, d.reason_code) == ("ALLOW", "OVERRIDE_ADMIN")
    row = (await db.execute(text("SELECT actor_user_id, reason, after_data, ip_address FROM audit_logs "
                                 "WHERE action = 'conflicts.override_created'"))).one()
    assert row.actor_user_id == admin.id and row.reason == just and row.after_data["decision"] == "ALLOW"
    assert row.ip_address
    # apostas: override não libera (política universal)
    r = await client.post("/api/v1/conflicts/overrides", headers=s.headers,
                          json={"campaign_id": str(bet.id), "establishment_id": str(distribuidora.id),
                                "justification": just})
    assert r.status_code == 201
    assert r.json()["decision_after"]["reason_code"] == "PROIBIDO_UNIVERSAL" and "warning" in r.json()
    academia = await make_est(db, category=t.academia)
    for est in (distribuidora, academia):
        assert (await CS.evaluate_conflict(db, bet.id, est.id)).result == "BLOCK"
    # anunciante não cria override
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", super_org.id))
    sa = await login_as(client, adv)
    r = await client.post("/api/v1/conflicts/overrides", headers=sa.headers,
                          json={"campaign_id": str(beer.id), "establishment_id": str(academia.id),
                                "justification": just})
    assert r.status_code == 403
    # duplicado ativo → 409; revogar → volta a bloquear
    r = await client.post("/api/v1/conflicts/overrides", headers=s.headers,
                          json={"campaign_id": str(beer.id), "establishment_id": str(distribuidora.id),
                                "justification": just})
    assert r.status_code == 409
    r = await client.get(f"/api/v1/conflicts/overrides?campaign_id={beer.id}", headers=s.headers)
    oid = r.json()["items"][0]["id"]
    r = await client.post(f"/api/v1/conflicts/overrides/{oid}/revoke", headers=s.headers, json={"reason": "fim parceria"})
    assert r.status_code == 200
    assert (await CS.evaluate_conflict(db, beer.id, distribuidora.id)).result == "BLOCK"
    # audit_logs é append-only
    from sqlalchemy.exc import DBAPIError
    import pytest

    with pytest.raises(DBAPIError):
        await db.execute(text("DELETE FROM audit_logs WHERE action = 'conflicts.override_created'"))
    await db.rollback()


async def test_excecao_do_estabelecimento_so_para_a_marca(db, client):
    from app.models.taxonomy import Brand

    t = await Tax().create(db)
    super_org = await make_org(db)
    await set_advertiser(db, super_org, category=t.supermercado)
    z = Brand(id=uuid.uuid4(), name="Cervejaria Z", slug=f"z-{uuid.uuid4().hex[:6]}")
    y = Brand(id=uuid.uuid4(), name="Cervejaria Y", slug=f"y-{uuid.uuid4().hex[:6]}")
    db.add_all([z, y])
    await db.commit()
    distribuidora = await make_est(db, category=t.distribuidora, segments=[t.bebidas])
    cz = await _creative(db, super_org, t.cerveja, brand_id=z.id)
    cy = await _creative(db, super_org, t.cerveja, brand_id=y.id)
    admin = await make_user(db, role="admin", mfa=True)
    s = await login_as(client, admin)
    r = await client.post("/api/v1/conflicts/exceptions", headers=s.headers,
                          json={"establishment_id": str(distribuidora.id), "object_type": "BRAND",
                                "object_id": str(z.id), "reason": "Contrato comercial com a Cervejaria Z"})
    assert r.status_code == 201, r.text
    assert (await CS.evaluate_conflict(db, cz, distribuidora.id)).reason_code == "EXCECAO_ESTABELECIMENTO"
    assert (await CS.evaluate_conflict(db, cy, distribuidora.id)).reason_code == "SEGMENTO"
    # estabelecimento (dono) não cria exceção — só a RD
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", distribuidora.org_id))
    so = await login_as(client, owner)
    r = await client.post("/api/v1/conflicts/exceptions", headers=so.headers,
                          json={"establishment_id": str(distribuidora.id), "object_type": "BRAND",
                                "object_id": str(y.id), "reason": "Quero liberar a marca Y aqui"})
    assert r.status_code == 403


async def test_mesma_rede_permitida(db):
    t = await Tax().create(db)
    org = await make_org(db)
    await set_advertiser(db, org, category=t.distribuidora, segments=[t.bebidas])
    own = await make_est(db, org, category=t.distribuidora, segments=[t.bebidas])
    other = await make_est(db, category=t.distribuidora, segments=[t.bebidas])
    c = await _campaign(db, org, t.cerveja)
    assert (await CS.evaluate_conflict(db, c, own.id)).reason_code == "MESMA_REDE"
    assert (await CS.evaluate_conflict(db, c, other.id)).result == "BLOCK"


async def test_regras_explicitas_api_empate_bloqueia_e_especificidade(db, client):
    t = await Tax().create(db)
    org = await make_org(db)
    await set_advertiser(db, org, category=t.academia)
    sup = await make_est(db, category=t.supermercado)  # não declara o que vende
    c = await _campaign(db, org, t.snacks)
    assert (await CS.evaluate_conflict(db, c, sup.id)).reason_code == "SEM_CONFLITO"
    admin = await make_user(db, role="admin", mfa=True)
    s = await login_as(client, admin)

    async def rule(effect, obj, subj=t.supermercado.id, otype="AD_SEGMENT", stype="EST_CATEGORY"):
        r = await client.post("/api/v1/conflicts/rules", headers=s.headers,
                              json={"subject_type": stype, "subject_id": str(subj), "object_type": otype,
                                    "object_id": str(obj), "effect": effect, "reason": "regra de teste"})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    await rule("BLOCK", t.alimentos.id)  # supermercado × alimentos (inclui snacks) → bloqueia
    assert (await CS.evaluate_conflict(db, c, sup.id)).reason_code == "REGRA_EXPLICITA"
    allow_snacks = await rule("ALLOW", t.snacks.id)  # mais específica (segmento mais profundo) libera
    d = await CS.evaluate_conflict(db, c, sup.id)
    assert (d.result, d.reason_code) == ("ALLOW", "REGRA_EXPLICITA")
    await rule("BLOCK", t.snacks.id)  # mesma especificidade com efeito oposto → empate ⇒ BLOQUEIO
    d = await CS.evaluate_conflict(db, c, sup.id)
    assert d.result == "BLOCK" and d.rule_path[-1]["detail"]["tie"] is True
    # desativar a ALLOW mantém o bloqueio; regra com referência inexistente é recusada
    r = await client.patch(f"/api/v1/conflicts/rules/{allow_snacks}", headers=s.headers,
                           json={"active": False, "reason": "revisão comercial"})
    assert r.status_code == 200 and r.json()["version"] == 2
    # o motivo do PATCH vai só para a auditoria; a JUSTIFICATIVA original da regra é mantida
    assert r.json()["reason"] == "regra de teste" and r.json()["active"] is False
    row = (await db.execute(text("SELECT reason, before_data->>'reason', after_data->>'reason' FROM audit_logs "
                                 "WHERE action = 'conflicts.rule_updated' AND entity_id = :e"),
                            {"e": allow_snacks})).one()
    assert tuple(row) == ("revisão comercial", "regra de teste", "regra de teste")
    r = await client.post("/api/v1/conflicts/rules", headers=s.headers,
                          json={"subject_type": "EST_CATEGORY", "subject_id": str(uuid.uuid4()),
                                "object_type": "AD_SEGMENT", "object_id": str(t.snacks.id), "effect": "BLOCK",
                                "reason": "x" * 10})
    assert r.status_code == 400
    # anunciante não administra a matriz
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    sa = await login_as(client, adv)
    assert (await client.get("/api/v1/conflicts/rules", headers=sa.headers)).status_code == 403
    # avaliação explicável pela API (equipe RD)
    r = await client.post("/api/v1/conflicts/evaluate", headers=s.headers,
                          json={"campaign_id": str(c.id), "establishment_id": str(sup.id), "record": True})
    assert r.status_code == 200 and r.json()["result"] == "BLOCK" and r.json()["decision_id"]


async def test_concorrentes_anuncio_x_anuncio_e_lote(db):
    from app.models.taxonomy import CompetitorLink

    t = await Tax().create(db)
    a_org, b_org, c_org, d_org = [await make_org(db) for _ in range(4)]
    await set_advertiser(db, a_org, category=t.supermercado)
    await set_advertiser(db, b_org, category=t.distribuidora)
    await set_advertiser(db, c_org, category=t.academia)
    await set_advertiser(db, d_org, category=t.academia)
    beer_a = await _creative(db, a_org, t.cerveja)
    bev_b = await _creative(db, b_org, t.bebidas)
    snack_c = await _creative(db, c_org, t.snacks)
    gym_d = await _creative(db, d_org)
    assert (await CS.are_competitors(db, beer_a, bev_b)).result == "BLOCK"
    assert (await CS.are_competitors(db, beer_a, snack_c)).result == "ALLOW"
    assert (await CS.are_competitors(db, snack_c, gym_d)).reason_code == "MESMO_RAMO"  # academia × academia
    lo, hi = sorted([a_org.id, c_org.id])
    db.add(CompetitorLink(id=uuid.uuid4(), org_a_id=lo, org_b_id=hi, reason="concorrentes declarados"))
    await db.commit()
    pairs = await CS.competitor_pairs(db, [beer_a.id, bev_b.id, snack_c.id, gym_d.id])
    assert pairs == {frozenset((beer_a.id, bev_b.id)), frozenset((beer_a.id, snack_c.id)),
                     frozenset((snack_c.id, gym_d.id))}


async def test_decisoes_sao_append_only(db):
    import pytest
    from sqlalchemy.exc import DBAPIError

    t = await Tax().create(db)
    org = await make_org(db)
    est = await make_est(db, category=t.academia)
    c = await _campaign(db, org, t.snacks)
    await CS.evaluate_conflict(db, c, est.id, record=True, ctx=SYS)
    await db.commit()
    with pytest.raises(DBAPIError):
        await db.execute(text("UPDATE conflict_decisions SET result = 'ALLOW'"))
    await db.rollback()
