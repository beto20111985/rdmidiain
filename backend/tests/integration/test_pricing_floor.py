"""Piso de preço da RD imposto no SERVIDOR (REQ-NEG-006) em todos os pontos de decisão de preço:
ativação de tabela, plano (criação/edição/ativação), promoção, cupom, cotação e checkout — usando a interface
do W-BIZ (``assert_price_not_below_floor``/``floors_for``). Instância white-label: abaixo do piso ⇒ 400
PRICE_BELOW_RD_MINIMUM; igual/acima ⇒ aceito. Instância RD (não white-label) continua livre."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.authz import AuthContext
from app.modules.instance import service as inst
from app.modules.pricing import floors as FL
from tests.camp_real import create_campaign
from tests.camp_support import FakeDeps, future_monday
from tests.factories import login_as, make_user
from tests.integration.test_camp_lifecycle import new_campaign, world

SYS = AuthContext.system("system")
UTC = timezone.utc
# 2/h × 24 h × 10 c por inserção = 480 c/dia por tela ⇒ 14.400 c por tela a cada 30 dias (qualquer mês)
MONTH_PER_SCREEN = 2 * 24 * 10 * 30


@pytest.fixture
def deps(monkeypatch):
    return FakeDeps().install(monkeypatch)


async def white_label(db, on=True):
    await inst.get_or_create_profile(db)
    await db.execute(text("UPDATE instance_profile SET is_white_label = :v WHERE id = 1"), {"v": on})
    await db.commit()


async def floor(db, key, cents, unit="tela/mês"):
    await inst.set_floor(db, SYS, key, min_price_cents=cents, label=key, unit=unit, reason="teste")
    await db.commit()


def below(r, key=None):
    """Contrato ÚNICO do W-BIZ, propagado sem reembrulho: 400 + details exatamente com 3 chaves."""
    if r.status_code != 400 or r.json()["error"]["code"] != "PRICE_BELOW_RD_MINIMUM":
        return False
    d = r.json()["error"]["details"]
    assert set(d) == {"floor_key", "min_price_cents", "price_cents"}, d
    assert d["price_cents"] < d["min_price_cents"]
    return key is None or d["floor_key"] == key


async def _admin(db, client):
    return await login_as(client, await make_user(db, role="admin", mfa=True))


BASE = [{"kind": "DURATION_BASE", "key": "30", "mode": "BASE_CENTS", "value": 10},
        {"kind": "COST_GATEWAY", "key": "*", "mode": "PERCENT_BPS", "value": 199}]


async def _table(client, sa, items):
    r = await client.post("/api/v1/pricing/tables", headers=sa.headers, json={"name": "Tabela piso"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    for it in items:
        r = await client.post(f"/api/v1/pricing/tables/{tid}/items", headers=sa.headers, json=it)
        assert r.status_code == 201, r.text
    return tid


def test_convencao_de_chaves_deterministica():
    assert FL.monthly_key(30) == "slot_30s_mensal_tela"
    assert FL.monthly_key(120, "ANUAL") == "slot_120s_anual_tela"
    assert FL.monthly_key(60, "SEMESTRAL") == "slot_60s_semestral_tela"
    assert FL.monthly_key(30, "AVULSO") == "slot_30s_avulso_tela"
    assert FL.insertion_key(120) == "slot_120s_insercao"
    assert FL.CAMPAIGN_MIN_KEY == "campanha_minimo" and FL.ERROR_CODE == "PRICE_BELOW_RD_MINIMUM"
    for k in (FL.monthly_key(30, "ANUAL"), FL.insertion_key(60), FL.CAMPAIGN_MIN_KEY):
        assert inst.FLOOR_KEY_RE.match(k)  # cadastráveis no W-BIZ (PUT /instance/price-floors/{key})


async def test_ativacao_de_tabela_respeita_piso_por_insercao_e_minimo(db, client):
    await white_label(db)
    sa = await _admin(db, client)
    await floor(db, "slot_30s_insercao", 11, unit="inserção/tela")
    await floor(db, "campanha_minimo", 5000, unit="campanha")
    t1 = await _table(client, sa, BASE)
    r = await client.post(f"/api/v1/pricing/tables/{t1}/activate", headers=sa.headers)
    assert below(r, "slot_30s_insercao"), r.text
    assert r.json()["error"]["details"] == {"floor_key": "slot_30s_insercao", "min_price_cents": 11, "price_cents": 10}
    base11 = [{**BASE[0], "value": 11}, BASE[1]]
    t2 = await _table(client, sa, base11 + [{"kind": "MIN_PRICE", "key": "*", "mode": "FIXED_CENTS", "value": 4999}])
    assert below(await client.post(f"/api/v1/pricing/tables/{t2}/activate", headers=sa.headers), "campanha_minimo")
    t3 = await _table(client, sa, base11 + [{"kind": "MIN_PRICE", "key": "*", "mode": "FIXED_CENTS", "value": 5000}])
    r = await client.post(f"/api/v1/pricing/tables/{t3}/activate", headers=sa.headers)
    assert r.status_code == 200 and r.json()["status"] == "ATIVA", r.text  # igual ao piso: aceito


async def test_plano_promocao_e_cupom_nao_deixam_o_preco_final_abaixo_do_piso(db, client):
    await white_label(db)
    sa = await _admin(db, client)
    tid = await _table(client, sa, BASE)
    assert (await client.post(f"/api/v1/pricing/tables/{tid}/activate", headers=sa.headers)).status_code == 200
    await floor(db, "slot_30s_mensal_tela", 14000)
    plan = {"name": "Mensal Bairro", "period_type": "MENSAL", "duration_s": 30, "freq_kind": "PER_HOUR",
            "freq_value": 2, "screens_min": 1, "screens_max": 5}
    # criação: 10% de desconto ⇒ 12.960 < 14.000 por tela/30 dias
    r = await client.post("/api/v1/pricing/plans", headers=sa.headers, json={**plan, "discount_bps": 1000})
    assert below(r, "slot_30s_mensal_tela"), r.text
    assert r.json()["error"]["details"] == {"floor_key": "slot_30s_mensal_tela", "min_price_cents": 14000,
                                            "price_cents": MONTH_PER_SCREEN * 9 // 10}
    r = await client.post("/api/v1/pricing/plans", headers=sa.headers, json=plan)
    assert r.status_code == 201, r.text  # 14.400 ≥ 14.000
    pid = r.json()["id"]
    # edição: abaixo recusa; 2% ⇒ 14.112 aceita
    assert below(await client.patch(f"/api/v1/pricing/plans/{pid}", headers=sa.headers, json={"discount_bps": 2000}))
    r = await client.patch(f"/api/v1/pricing/plans/{pid}", headers=sa.headers, json={"discount_bps": 200})
    assert r.status_code == 200, r.text
    # ativação: piso subiu depois do cadastro ⇒ recusa; piso igual ao preço ⇒ aceita
    await floor(db, "slot_30s_mensal_tela", 14113)
    assert below(await client.post(f"/api/v1/pricing/plans/{pid}/activate", headers=sa.headers))
    await floor(db, "slot_30s_mensal_tela", 14112)
    r = await client.post(f"/api/v1/pricing/plans/{pid}/activate", headers=sa.headers)
    assert r.status_code == 200 and r.json()["status"] == "ATIVO", r.text
    await floor(db, "slot_30s_mensal_tela", 14000)

    now = datetime.now(UTC)
    promo = {"name": "Outubro", "valid_from": (now - timedelta(hours=1)).isoformat(),
             "valid_until": (now + timedelta(days=1)).isoformat()}
    # promoção sobre o plano ativo (já com 2%): 20% ⇒ abaixo; 0,5% ⇒ 14.041 ≥ 14.000
    r = await client.post("/api/v1/pricing/promotions", headers=sa.headers, json={**promo, "discount_bps": 2000})
    assert below(r, "slot_30s_mensal_tela"), r.text
    r = await client.post("/api/v1/pricing/promotions", headers=sa.headers, json={**promo, "discount_bps": 50})
    assert r.status_code == 201, r.text
    # promoção cadastrada INATIVA não é avaliada; ativá-la depois (PATCH) é
    r = await client.post("/api/v1/pricing/promotions", headers=sa.headers,
                          json={**promo, "name": "Relâmpago", "discount_bps": 1500, "active": False})
    assert r.status_code == 201, r.text
    assert below(await client.patch(f"/api/v1/pricing/promotions/{r.json()['id']}", headers=sa.headers,
                                    json={"active": True}))

    cup = {"valid_from": now.isoformat(), "valid_until": (now + timedelta(days=3)).isoformat()}
    # cupom cumulativo com plano (2%) e promoção vigente (0,5%): 10% ⇒ abaixo; R$ 0,01 fixo ⇒ aceito
    r = await client.post("/api/v1/pricing/coupons", headers=sa.headers,
                          json={**cup, "code": "DEZ", "discount_type": "PERCENT", "discount_bps": 1000})
    assert below(r, "slot_30s_mensal_tela"), r.text
    r = await client.post("/api/v1/pricing/coupons", headers=sa.headers,
                          json={**cup, "code": "FIX1", "discount_type": "FIXED", "discount_cents": 1})
    assert r.status_code == 201, r.text
    # cupom cujo pedido mínimo o plano nunca atinge não muda o preço do plano ⇒ não é barrado
    r = await client.post("/api/v1/pricing/coupons", headers=sa.headers,
                          json={**cup, "code": "GRANDE", "discount_type": "PERCENT", "discount_bps": 5000,
                                "min_order_cents": 10**9})
    assert r.status_code == 201, r.text
    # cupom inativo reativado por PATCH é avaliado
    r = await client.post("/api/v1/pricing/coupons", headers=sa.headers,
                          json={**cup, "code": "VINTE", "discount_type": "PERCENT", "discount_bps": 2000,
                                "active": False})
    assert r.status_code == 201, r.text
    assert below(await client.patch(f"/api/v1/pricing/coupons/{r.json()['id']}", headers=sa.headers,
                                    json={"active": True}))

    # nova tabela mais barata deixaria o plano ATIVO abaixo do piso ⇒ ativação da tabela recusada
    t2 = await _table(client, sa, [{**BASE[0], "value": 9}, BASE[1]])
    r = await client.post(f"/api/v1/pricing/tables/{t2}/activate", headers=sa.headers)
    assert below(r, "slot_30s_mensal_tela"), r.text


async def test_cotacao_e_checkout_defesa_em_profundidade(db, client, deps):
    t, org, ests, screens, s = await world(db, client)  # 2 telas em Goiânia; 10 c por inserção (30 s)
    await white_label(db)
    await floor(db, "slot_30s_mensal_tela", MONTH_PER_SCREEN + 1)
    start = future_monday()
    body = {"targets": [{"type": "CITY", "value": "GO:Goiânia"}], "duration_s": 30, "freq_kind": "PER_HOUR",
            "freq_value": 2, "start_date": start.isoformat(), "period_type": "MENSAL"}
    r = await client.post("/api/v1/pricing/quote", headers=s.headers, json=body)
    assert below(r, "slot_30s_mensal_tela"), r.text
    assert r.json()["error"]["details"]["price_cents"] == MONTH_PER_SCREEN
    cid = await create_campaign(client, s, db, org, start=start, period="MENSAL", name="Mensal")
    key = f"k-{uuid.uuid4().hex}"
    assert below(await client.post("/api/v1/checkout", headers={**s.headers, "Idempotency-Key": key},
                                   json={"campaign_ids": [cid]}))
    assert below(await client.post(f"/api/v1/campaigns/{cid}/checkout",
                                   headers={**s.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"}, json={}))
    assert not [c for c in deps.calls if c[0] in ("hold", "create_payment_order")]  # nada reservado nem cobrado
    await floor(db, "slot_30s_mensal_tela", MONTH_PER_SCREEN)  # igual ao piso: aceito
    assert (await client.post("/api/v1/pricing/quote", headers=s.headers, json=body)).status_code == 200
    r = await client.post("/api/v1/checkout", headers={**s.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"},
                          json={"campaign_ids": [cid]})
    assert r.status_code == 201, r.text


async def test_instancia_rd_nao_white_label_sem_piso_para_si(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    await white_label(db, on=False)
    await floor(db, "slot_30s_mensal_tela", 10**9)
    await floor(db, "slot_30s_avulso_tela", 10**9)
    await floor(db, "slot_30s_insercao", 10**6)
    await floor(db, "campanha_minimo", 10**9, unit="campanha")
    body = {"targets": [{"type": "CITY", "value": "GO:Goiânia"}], "duration_s": 30, "freq_kind": "PER_HOUR",
            "freq_value": 2, "start_date": future_monday().isoformat(), "period_type": "MENSAL"}
    assert (await client.post("/api/v1/pricing/quote", headers=s.headers, json=body)).status_code == 200
    cid = await new_campaign(client, s, db=db, org=org)
    r = await client.post(f"/api/v1/campaigns/{cid}/checkout",
                          headers={**s.headers, "Idempotency-Key": f"k-{uuid.uuid4().hex}"}, json={})
    assert r.status_code == 201, r.text
    sa = await _admin(db, client)
    t2 = await _table(client, sa, BASE)
    assert (await client.post(f"/api/v1/pricing/tables/{t2}/activate", headers=sa.headers)).status_code == 200
