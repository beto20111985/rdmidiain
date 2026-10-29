"""Preços/planos/cupons administráveis sem deploy (PostgreSQL real): versões de tabela imutáveis,
margem na ativação de plano (negativa bloqueia; < 40% alerta), plano personalizado com aprovação de
outra pessoa, catálogo do cliente, cotação em tempo real calculada no servidor."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.camp_support import Tax, future_monday, make_est, make_screen, set_advertiser
from tests.factories import login_as, make_org, make_user

UTC = timezone.utc


async def _admin(db, client):
    a = await make_user(db, role="admin", mfa=True)
    return await login_as(client, a)


async def _table(client, sa, items):
    r = await client.post("/api/v1/pricing/tables", headers=sa.headers, json={"name": "Tabela base"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    for it in items:
        r = await client.post(f"/api/v1/pricing/tables/{tid}/items", headers=sa.headers, json=it)
        assert r.status_code == 201, (it, r.text)
    return tid


BASE = [{"kind": "DURATION_BASE", "key": "30", "mode": "BASE_CENTS", "value": 10},
        {"kind": "DURATION_BASE", "key": "60", "mode": "BASE_CENTS", "value": 18},
        {"kind": "COST_GATEWAY", "key": "*", "mode": "PERCENT_BPS", "value": 199},
        {"kind": "TAX", "key": "ISS", "mode": "PERCENT_BPS", "value": 500},
        {"kind": "PERIOD", "key": "ANUAL", "mode": "MULTIPLIER_BPS", "value": 8000}]


async def test_tabela_versionada_ativacao_e_imutabilidade(db, client):
    sa = await _admin(db, client)
    tid = await _table(client, sa, BASE)
    bad = [{"kind": "DURATION_BASE", "key": "45", "mode": "BASE_CENTS", "value": 10},
           {"kind": "PERIOD", "key": "TRIMESTRAL", "mode": "MULTIPLIER_BPS", "value": 9000},
           {"kind": "TAX", "key": "ISS", "mode": "FIXED_CENTS", "value": 10},
           {"kind": "TIME_BAND", "key": "*", "mode": "MULTIPLIER_BPS", "value": 15000}]
    for it in bad:
        r = await client.post(f"/api/v1/pricing/tables/{tid}/items", headers=sa.headers, json=it)
        assert r.status_code == 400, (it, r.text)
    r = await client.post(f"/api/v1/pricing/tables/{tid}/items", headers=sa.headers,
                          json={"kind": "DURATION_BASE", "key": "30", "mode": "BASE_CENTS", "value": 1.5})
    assert r.status_code == 422  # dinheiro nunca float
    r = await client.post(f"/api/v1/pricing/tables/{tid}/activate", headers=sa.headers)
    assert r.status_code == 200 and r.json()["status"] == "ATIVA", r.text
    item_id = (await client.get(f"/api/v1/pricing/tables/{tid}", headers=sa.headers)).json()["items"][0]["id"]
    r = await client.patch(f"/api/v1/pricing/tables/{tid}/items/{item_id}", headers=sa.headers, json={"value": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PRICE_TABLE_IMMUTABLE"
    # nova versão por duplicação; ativar arquiva a anterior (preço muda sem deploy)
    r = await client.post(f"/api/v1/pricing/tables/{tid}/duplicate", headers=sa.headers)
    t2 = r.json()
    assert r.status_code == 201 and t2["version"] == 2 and len(t2["items"]) == len(BASE)
    base30 = next(i for i in t2["items"] if i["kind"] == "DURATION_BASE" and i["key"] == "30")
    r = await client.patch(f"/api/v1/pricing/tables/{t2['id']}/items/{base30['id']}", headers=sa.headers,
                           json={"value": 12})
    assert r.status_code == 200
    assert (await client.post(f"/api/v1/pricing/tables/{t2['id']}/activate", headers=sa.headers)).status_code == 200
    rows = dict((await db.execute(text("SELECT version, status FROM price_tables"))).all())
    assert rows == {1: "ARQUIVADA", 2: "ATIVA"}
    # anunciante não vê/edita tabelas
    org = await make_org(db)
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, adv)
    assert (await client.get("/api/v1/pricing/tables", headers=s.headers)).status_code == 403
    n = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action LIKE 'pricing.%'"))).scalar_one()
    assert n >= len(BASE) + 4


async def test_plano_margem_negativa_bloqueia_abaixo_do_minimo_alerta(db, client):
    sa = await _admin(db, client)
    tid = await _table(client, sa, BASE + [{"kind": "COST_SERVER", "key": "*", "mode": "PER_SCREEN_MONTH_CENTS",
                                            "value": 300}])
    await client.post(f"/api/v1/pricing/tables/{tid}/activate", headers=sa.headers)
    plan = {"name": "Bairro Mensal", "period_type": "MENSAL", "duration_s": 30, "freq_kind": "PER_HOUR",
            "freq_value": 2, "screens_min": 1, "screens_max": 10}
    r = await client.post("/api/v1/pricing/plans", headers=sa.headers, json=plan)
    assert r.status_code == 201 and r.json()["status"] == "RASCUNHO", r.text
    pid = r.json()["id"]
    r = await client.post(f"/api/v1/pricing/plans/{pid}/activate", headers=sa.headers)
    assert r.status_code == 200 and r.json()["status"] == "ATIVO" and r.json()["margin_status"] == "OK", r.text
    assert r.json()["warnings"] == []
    # desconto de 99% → margem negativa → bloqueado (continua RASCUNHO)
    r = await client.post("/api/v1/pricing/plans", headers=sa.headers, json={**plan, "name": "Promo", "discount_bps": 9900})
    pid2 = r.json()["id"]
    r = await client.post(f"/api/v1/pricing/plans/{pid2}/activate", headers=sa.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_MARGIN_NEGATIVE"
    assert (await client.get(f"/api/v1/pricing/plans/{pid2}", headers=sa.headers)).json()["status"] == "RASCUNHO"
    # desconto de 97% (custo de servidor fixo pesa) → margem positiva mas < 40% → ativa com alerta
    r = await client.post("/api/v1/pricing/plans", headers=sa.headers, json={**plan, "name": "Meia", "discount_bps": 9700})
    pid3 = r.json()["id"]
    r = await client.post(f"/api/v1/pricing/plans/{pid3}/activate", headers=sa.headers)
    assert r.status_code == 200 and r.json()["margin_status"] == "BELOW_MIN"
    assert r.json()["warnings"][0]["code"] == "MARGIN_BELOW_MIN" and r.json()["margin_bps"] < 4000
    # nova tabela que deixaria plano ativo negativo não pode ser ativada
    r = await client.post(f"/api/v1/pricing/tables/{tid}/duplicate", headers=sa.headers)
    t2 = r.json()["id"]
    await client.post(f"/api/v1/pricing/tables/{t2}/items", headers=sa.headers,
                      json={"kind": "COST_DIRECT", "key": "*", "mode": "PERCENT_BPS", "value": 9000})
    r = await client.post(f"/api/v1/pricing/tables/{t2}/activate", headers=sa.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "TABLE_MAKES_PLAN_NEGATIVE"
    # trimestral não existe
    r = await client.post("/api/v1/pricing/plans", headers=sa.headers, json={**plan, "period_type": "TRIMESTRAL"})
    assert r.status_code == 422


async def test_plano_personalizado_exige_aprovacao_de_outra_pessoa_e_catalogo(db, client):
    sa = await _admin(db, client)
    tid = await _table(client, sa, BASE)
    await client.post(f"/api/v1/pricing/tables/{tid}/activate", headers=sa.headers)
    org, other = await make_org(db), await make_org(db)
    seller = await make_user(db, role="sales")
    ss = await login_as(client, seller)
    custom = {"name": "Especial Cliente", "kind": "CUSTOM", "org_id": str(org.id), "period_type": "ANUAL",
              "duration_s": 30, "freq_kind": "PER_HOUR", "freq_value": 1, "screens_min": 1, "screens_max": 5,
              "discount_bps": 500}
    r = await client.post("/api/v1/pricing/plans", headers=ss.headers, json=custom)
    assert r.status_code == 201 and r.json()["status"] == "PENDENTE_APROVACAO", r.text
    pid = r.json()["id"]
    # vendedor não cria plano pré-definido nem aprova
    r = await client.post("/api/v1/pricing/plans", headers=ss.headers, json={**custom, "kind": "PREDEFINED",
                                                                          "org_id": None})
    assert r.status_code == 403
    assert (await client.post(f"/api/v1/pricing/plans/{pid}/approve", headers=ss.headers)).status_code == 403
    r = await client.post(f"/api/v1/pricing/plans/{pid}/approve", headers=sa.headers)
    assert r.status_code == 200 and r.json()["status"] == "ATIVO", r.text
    # quem cria não aprova
    r = await client.post("/api/v1/pricing/plans", headers=sa.headers, json={**custom, "name": "Outro"})
    r = await client.post(f"/api/v1/pricing/plans/{r.json()['id']}/approve", headers=sa.headers)
    assert r.status_code == 403
    # catálogo: cliente vê o próprio personalizado; outro cliente não
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    uo = await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id))
    s, so = await login_as(client, u), await login_as(client, uo)
    r = await client.get(f"/api/v1/pricing/plans/catalog?org_id={org.id}", headers=s.headers)
    assert [p["id"] for p in r.json()["items"]] == [pid] and "margin_bps" not in r.json()["items"][0]
    r = await client.get(f"/api/v1/pricing/plans/catalog?org_id={org.id}", headers=so.headers)
    assert r.status_code == 404
    r = await client.get(f"/api/v1/pricing/plans/catalog?org_id={other.id}", headers=so.headers)
    assert r.json()["items"] == []


async def test_cotacao_em_tempo_real_no_servidor(db, client):
    sa = await _admin(db, client)
    tid = await _table(client, sa, BASE)
    await client.post(f"/api/v1/pricing/tables/{tid}/activate", headers=sa.headers)
    t = await Tax().create(db)
    org = await make_org(db)
    await set_advertiser(db, org, category=t.academia)
    for _ in range(3):
        await make_screen(db, await make_est(db, category=t.supermercado))
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, u)
    body = {"targets": [{"type": "CITY", "value": "GO:Goiânia"}], "duration_s": 30, "freq_kind": "PER_DAY",
            "freq_value": 10, "start_date": future_monday().isoformat(), "period_type": "AVULSO", "period_days": 7}
    r = await client.post("/api/v1/pricing/quote", headers=s.headers, json=body)
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    assert q["total_cents"] == 10 * 7 * 3 * 10 and "margin" not in q and "per_screen_weights" not in q
    assert r.json()["availability"]["screens_available"] == 3
    r = await client.post("/api/v1/pricing/quote", headers=s.headers, json={**body, "amount_cents": 1})
    assert r.status_code == 422
    r = await client.post("/api/v1/pricing/quote", headers=sa.headers, json={**body, "org_id": str(org.id)})
    assert r.status_code == 200 and "margin" in r.json()["quote"]
    # anual com multiplicador 0,8
    r = await client.post("/api/v1/pricing/quote", headers=s.headers, json={**body, "period_type": "ANUAL"})
    assert r.status_code == 200 and r.json()["quote"]["inputs"]["calendar_days"] in (365, 366)


async def test_cupom_crud_e_validacao(db, client):
    sa = await _admin(db, client)
    now = datetime.now(UTC)
    r = await client.post("/api/v1/pricing/coupons", headers=sa.headers, json={
        "code": "promo10", "discount_type": "PERCENT", "discount_bps": 1000, "valid_from": now.isoformat(),
        "valid_until": (now + timedelta(days=5)).isoformat(), "max_uses": 10})
    assert r.status_code == 201 and r.json()["code"] == "PROMO10", r.text
    r = await client.post("/api/v1/pricing/coupons", headers=sa.headers, json={
        "code": "PROMO10", "discount_type": "PERCENT", "discount_bps": 1000, "valid_from": now.isoformat(),
        "valid_until": (now + timedelta(days=5)).isoformat()})
    assert r.status_code == 409
    r = await client.post("/api/v1/pricing/coupons", headers=sa.headers, json={
        "code": "FIX", "discount_type": "FIXED", "discount_bps": 500, "valid_from": now.isoformat(),
        "valid_until": (now + timedelta(days=5)).isoformat()})
    assert r.status_code == 400
    org = await make_org(db)
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, u)
    r = await client.post("/api/v1/pricing/coupons/validate", headers=s.headers, json={"code": "promo10"})
    assert r.status_code == 200 and r.json()["valid"] is True
    r = await client.post("/api/v1/pricing/coupons/validate", headers=s.headers, json={"code": "NAOEXISTE"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "COUPON_INVALID"
    cid = (await client.get("/api/v1/pricing/coupons", headers=sa.headers)).json()["items"][0]["id"]
    assert (await client.patch(f"/api/v1/pricing/coupons/{cid}", headers=sa.headers,
                               json={"active": False})).status_code == 200
    r = await client.post("/api/v1/pricing/coupons/validate", headers=s.headers, json={"code": "promo10"})
    assert r.status_code == 400


async def test_promocao_aplicada_automaticamente_na_cotacao(db, client):
    sa = await _admin(db, client)
    tid = await _table(client, sa, BASE)
    await client.post(f"/api/v1/pricing/tables/{tid}/activate", headers=sa.headers)
    t = await Tax().create(db)
    org = await make_org(db)
    await set_advertiser(db, org, category=t.academia)
    await make_screen(db, await make_est(db, category=t.supermercado))
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, u)
    body = {"targets": [{"type": "CITY", "value": "GO:Goiânia"}], "duration_s": 30, "freq_kind": "PER_DAY",
            "freq_value": 10, "start_date": future_monday().isoformat(), "period_type": "AVULSO", "period_days": 7}
    base = (await client.post("/api/v1/pricing/quote", headers=s.headers, json=body)).json()["quote"]["total_cents"]
    now = datetime.now(UTC)
    r = await client.post("/api/v1/pricing/promotions", headers=sa.headers, json={
        "name": "Outubro", "discount_bps": 1500, "valid_from": (now - timedelta(hours=1)).isoformat(),
        "valid_until": (now + timedelta(days=1)).isoformat()})
    assert r.status_code == 201, r.text
    q = (await client.post("/api/v1/pricing/quote", headers=s.headers, json=body)).json()["quote"]
    assert q["total_cents"] == base - base * 1500 // 10000
    assert any(line["code"] == "PROMOTION" for line in q["lines"])
    # promoção desativada deixa de valer
    await client.patch(f"/api/v1/pricing/promotions/{r.json()['id']}", headers=sa.headers, json={"active": False})
    assert (await client.post("/api/v1/pricing/quote", headers=s.headers, json=body)).json()["quote"]["total_cents"] == base


async def test_limites_do_plano_campanhas_e_criativos(db, client):
    from app.models.pricing import Plan
    from tests.camp_support import make_media

    org = await make_org(db)
    p = Plan(id=uuid.uuid4(), name="Limitado", slug=f"lim-{uuid.uuid4().hex[:6]}", kind="CUSTOM", org_id=org.id,
             status="ATIVO", period_type="MENSAL", duration_s=30, freq_kind="PER_HOUR", freq_value=1, days_mask=127,
             time_bands=[[0, 1440]], screens_min=1, screens_max=5, priority="STANDARD", exclusivity="NONE",
             discount_bps=0, max_campaigns=1, max_creatives=1, min_interval_s=0)
    db.add(p)
    await db.commit()
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, u)
    start = future_monday().isoformat()
    r = await client.post("/api/v1/campaigns", headers=s.headers,
                          json={"name": "Plano 1", "plan_id": str(p.id), "start_date": start})
    assert r.status_code == 201 and r.json()["period_type"] == "MENSAL", r.text
    cid = r.json()["id"]
    r2 = await client.post("/api/v1/campaigns", headers=s.headers,
                           json={"name": "Plano 2", "plan_id": str(p.id), "start_date": start})
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "PLAN_LIMIT_CAMPAIGNS"
    ids = []
    for _ in range(2):
        m = await make_media(db, org)
        ids.append((await client.post("/api/v1/creatives", headers=s.headers,
                                      json={"name": "cr", "media_file_id": str(m.id), "duration_seconds": 30}
                                      )).json()["id"])
    assert (await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=s.headers,
                              json={"creative_id": ids[0]})).status_code == 201
    r = await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=s.headers, json={"creative_id": ids[1]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_LIMIT_CREATIVES"
    # plano personalizado de outra organização não pode ser usado
    other = await make_org(db)
    uo = await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id))
    so = await login_as(client, uo)
    r = await client.post("/api/v1/campaigns", headers=so.headers, json={"name": "Intruso", "plan_id": str(p.id)})
    assert r.status_code == 404
