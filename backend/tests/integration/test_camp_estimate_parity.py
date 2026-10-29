"""Estimativa = valor real: com ``campaign_id``, ``/pricing/quote``, ``/campaigns/availability`` e
``/inventory/availability`` aplicam as MESMAS exclusões (segmentação, duração e CONFLITO com os criativos reais)
que o checkout e a alteração pós-aprovação — função única ``campaigns.service.eligible_resolution``.
W-INV e W-FIN REAIS."""
import uuid
from datetime import timedelta

import pytest

from tests.camp_real import approval, checkout_and_pay, make_active, set_payer, today_local
from tests.camp_support import future_monday, make_est, make_media, make_screen
from tests.fin_helpers import fin_env  # noqa: F401  (fixture do W-FIN: provedor falso + segredo de webhook)
from tests.integration.test_camp_lifecycle import world

pytestmark = pytest.mark.usefixtures("fin_env")


async def _beer_campaign(db, client, s, org, t, *, start, days=7):
    """Campanha cujo criativo é de BEBIDAS: bloqueada (SEGMENTO) em distribuidoras de bebidas."""
    r = await client.post("/api/v1/campaigns", headers=s.headers, json={
        "name": "Cerveja", "period_type": "AVULSO", "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=days - 1)).isoformat(), "duration_s": 30, "freq_kind": "PER_HOUR",
        "freq_value": 2, "targets": [{"type": "CITY", "value": "GO:Goiânia"}]})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    m = await make_media(db, org, duration_ms=30_000)
    cr = await client.post("/api/v1/creatives", headers=s.headers, json={
        "name": "Cerveja gelada", "media_file_id": str(m.id), "duration_seconds": 30,
        "segment_ids": [str(t.cerveja.id)]})
    assert cr.status_code == 201, cr.text
    r = await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=s.headers, json={"creative_id": cr.json()["id"]})
    assert r.status_code == 201, r.text
    return cid


def _body(start, days=7, targets=None, **kw):
    return {"targets": targets or [{"type": "CITY", "value": "GO:Goiânia"}], "duration_s": 30,
            "freq_kind": "PER_HOUR", "freq_value": 2, "start_date": start.isoformat(), "period_type": "AVULSO",
            "end_date": (start + timedelta(days=days - 1)).isoformat(), **kw}


async def test_cotacao_e_disponibilidade_com_campaign_id_batem_com_o_checkout(db, client):
    t, org, ests, screens, s = await world(db, client)  # 2 supermercados (alimentos) em Goiânia
    dist = await make_est(db, category=t.distribuidora, segments=[t.bebidas])  # vende bebidas: conflito
    await make_screen(db, dist)
    await set_payer(db, org.id)
    start = future_monday()
    cid = await _beer_campaign(db, client, s, org, t, start=start)

    # sem campaign_id: semântica antiga (só a classificação do anunciante) — conta a distribuidora
    r = await client.post("/api/v1/pricing/quote", headers=s.headers, json=_body(start))
    assert r.status_code == 200 and r.json()["availability"]["screens_available"] == 3, r.text
    # com campaign_id: conflito do criativo real exclui a distribuidora
    r = await client.post("/api/v1/pricing/quote", headers=s.headers, json=_body(start, campaign_id=cid))
    assert r.status_code == 200, r.text
    est = r.json()
    assert est["availability"]["screens_available"] == 2
    assert est["availability"]["excluded_by_conflict_establishments"] == 1
    r = await client.post("/api/v1/campaigns/availability", headers=s.headers,
                          json={"targets": [{"type": "CITY", "value": "GO:Goiânia"}], "campaign_id": cid})
    assert r.status_code == 200 and r.json()["screens_available"] == 2, r.text
    q = {"date_from": start.isoformat(), "date_to": (start + timedelta(days=6)).isoformat(), "city_key": "GO:goiania"}
    r = await client.get("/api/v1/inventory/availability", headers=s.headers, params=q)
    assert r.status_code == 200 and r.json()["screens"] == 3, r.text  # sem campaign_id: inalterado
    r = await client.get("/api/v1/inventory/availability", headers=s.headers, params={**q, "campaign_id": cid})
    assert r.status_code == 200 and r.json()["screens"] == 2, r.text
    r = await client.get("/api/v1/inventory/availability", headers=s.headers,
                         params={k: v for k, v in {**q, "campaign_id": cid}.items() if k != "city_key"})
    assert r.status_code == 200 and r.json()["screens"] == 2, r.text  # só a campanha: sua segmentação

    # checkout real cobra exatamente a estimativa
    r = await client.post(f"/api/v1/campaigns/{cid}/checkout",
                          headers={**s.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"}, json={})
    assert r.status_code == 201, r.text
    assert r.json()["total_cents"] == est["quote"]["total_cents"]
    assert r.json()["media_cents"] == est["quote"]["media_cents"]

    # campanha de outra organização: 404 (sem vazar existência)
    from tests.factories import login_as, make_org, make_user

    other = await make_org(db)
    so = await login_as(client, await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id)))
    r = await client.post("/api/v1/pricing/quote", headers=so.headers, json=_body(start, campaign_id=cid))
    assert r.status_code == 404, r.text
    r = await client.get("/api/v1/inventory/availability", headers=so.headers, params={**q, "campaign_id": cid})
    assert r.status_code == 404, r.text


async def test_estimativa_de_alteracao_com_campaign_id_bate_com_o_valor_real(db, client):
    t, org, ests, screens, s = await world(db, client)
    await set_payer(db, org.id)
    await approval(db, "AUTO")
    start = await today_local(db)
    cid = await _beer_campaign(db, client, s, org, t, start=start)
    await checkout_and_pay(db, client, s, cid)
    await make_active(db, cid, screens[0].id)
    # nova região: Anápolis com 1 supermercado (permitido) e 1 distribuidora (bloqueada pelo conflito)
    ana_ok = await make_est(db, category=t.supermercado, segments=[t.alimentos], city="Anápolis", state="GO")
    await make_screen(db, ana_ok)
    ana_dist = await make_est(db, category=t.distribuidora, segments=[t.bebidas], city="Anápolis", state="GO")
    await make_screen(db, ana_dist)
    targets = [{"type": "STATE", "value": "GO"}]
    r = await client.post("/api/v1/pricing/quote", headers=s.headers, json=_body(start, targets=targets))
    naive = r.json()["quote"]["total_cents"]
    r = await client.post("/api/v1/pricing/quote", headers=s.headers,
                          json=_body(start, targets=targets, campaign_id=cid))
    assert r.status_code == 200, r.text
    est = r.json()
    assert est["availability"]["screens_available"] == 3 and naive > est["quote"]["total_cents"]
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers={**s.headers, "Idempotency-Key": "par-0001"},
                          json={"targets": targets})
    assert r.status_code == 201, r.text
    assert r.json()["new_price_cents"] == est["quote"]["total_cents"]
