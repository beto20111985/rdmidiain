"""Checkout (PostgreSQL real): preço só no servidor, Idempotency-Key, carrinho, escopo, cupom, margem,
expiração da reserva e pagamento tardio, falha ao criar cobrança, dependências ausentes."""
import sys
import types
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.models.campaigns import CampaignRevision
from app.models.pricing import Coupon, Order, OrderItem
from app.modules.campaigns import checkout as CK
from tests.camp_support import FakeDeps, make_price_table
from tests.factories import login_as, make_org, make_user
from tests.integration.test_camp_lifecycle import new_campaign, status_of, world

UTC = timezone.utc


@pytest.fixture
def deps(monkeypatch):
    return FakeDeps().install(monkeypatch)


def H(s, key=None):
    return {**s.headers, "Idempotency-Key": key or f"key-{uuid.uuid4().hex}"}


async def test_preco_somente_no_servidor_e_valor_do_cliente_rejeitado(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    for url, body in ((f"/api/v1/campaigns/{cid}/checkout", {"amount_cents": 1}),
                      ("/api/v1/checkout", {"campaign_ids": [cid], "amount_cents": 1}),
                      ("/api/v1/checkout", {"campaign_ids": [cid], "total_cents": 1, "price": 1})):
        r = await client.post(url, headers=H(s), json=body)
        assert r.status_code == 422, r.text
    assert (await status_of(db, cid)).status == "RASCUNHO" and deps.calls == []
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
    assert r.status_code == 201, r.text
    body = r.json()
    expected = 2 * 24 * 7 * 2 * 10  # tabela: 10 c por inserção de 30 s; 2/h; 7 dias; 2 telas
    assert body["total_cents"] == expected and body["items"][0]["amount_cents"] == expected
    assert "margin" not in body["items"][0]["quote"] and "per_screen_weights" not in body["items"][0]["quote"]
    item = (await db.execute(select(OrderItem).where(OrderItem.order_id == uuid.UUID(body["order_id"])))).scalar_one()
    rev = await db.get(CampaignRevision, item.revision_id)
    assert item.quote_hash == rev.quote_hash == body["items"][0]["quote_hash"] and rev.price_cents == expected
    pay = next(iter(deps.payments.values()))
    assert pay["amount_cents"] == expected and pay["reference"] == {"type": "campaign_checkout",
                                                                    "id": uuid.UUID(body["order_id"])}
    # sem Idempotency-Key → 400
    cid2 = await new_campaign(client, s, db=db, org=org)
    r = await client.post("/api/v1/checkout", headers=s.headers, json={"campaign_ids": [cid2]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_idempotencia_e_estado(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    key = f"idem-{uuid.uuid4().hex}"
    r1 = await client.post("/api/v1/checkout", headers=H(s, key), json={"campaign_ids": [cid]})
    r2 = await client.post("/api/v1/checkout", headers=H(s, key), json={"campaign_ids": [cid]})
    assert r1.status_code == 201 and r2.status_code == 200 and r1.json() == r2.json()
    assert deps.names().count("hold") == 1 and deps.names().count("create_payment_order") == 1
    r3 = await client.post("/api/v1/checkout", headers=H(s, key), json={"campaign_ids": [cid], "coupon_code": "X1"})
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    # nova chave para campanha já aguardando pagamento → 409
    r4 = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
    assert r4.status_code == 409 and r4.json()["error"]["code"] == "CAMPAIGN_NOT_CHECKOUTABLE"
    assert (await db.execute(select(func.count()).select_from(Order))).scalar_one() == 1


async def test_escopo_carrinho_e_organizacoes(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid_a = await new_campaign(client, s, db=db, org=org)
    cid_a2 = await new_campaign(client, s, db=db, org=org)
    other = await make_org(db)
    u2 = await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id))
    s2 = await login_as(client, u2)
    cid_b = await new_campaign(client, s2, db=db, org=other)
    # campanha de outra organização → 404 (não vaza existência)
    r = await client.post(f"/api/v1/campaigns/{cid_b}/checkout", headers=H(s), json={})
    assert r.status_code == 404
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid_a, cid_b]})
    assert r.status_code == 404
    # equipe RD com escopo global: organizações misturadas na mesma cobrança → 400
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    r = await client.post("/api/v1/checkout", headers=H(sa), json={"campaign_ids": [cid_a, cid_b]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CART_MIXED_ORGS"
    # carrinho com duas campanhas = um pedido, uma cobrança
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid_a, cid_a2]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["items"]) == 2 and body["total_cents"] == sum(i["amount_cents"] for i in body["items"])
    assert deps.names().count("create_payment_order") == 1 and deps.names().count("hold") == 2
    # anunciante vê só os próprios pedidos
    assert (await client.get("/api/v1/checkout/orders", headers=s2.headers)).json()["total"] == 0
    r = await client.get(f"/api/v1/checkout/orders/{body['order_id']}", headers=s2.headers)
    assert r.status_code == 404
    r = await client.get(f"/api/v1/checkout/orders/{body['order_id']}", headers=s.headers)
    assert r.status_code == 200 and "margin" not in r.json()["items"][0]["quote"]


async def test_dependencia_ausente_nao_gera_sucesso_falso(db, client, monkeypatch):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    empty = types.ModuleType("app.modules.inventory.service")
    monkeypatch.setitem(sys.modules, "app.modules.inventory.service", empty)
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
    assert r.status_code == 503 and r.json()["error"]["code"] == "CHECKOUT_DEPENDENCY_UNAVAILABLE"
    assert (await status_of(db, cid)).status == "RASCUNHO"
    assert (await db.execute(select(func.count()).select_from(Order))).scalar_one() == 0
    assert (await db.execute(select(func.count()).select_from(CampaignRevision))).scalar_one() == 0


async def test_falha_ao_criar_cobranca_cancela_e_preserva_registro(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    deps.payment_status = "CREATION_FAILED"
    key = f"fail-{uuid.uuid4().hex}"
    r = await client.post("/api/v1/checkout", headers=H(s, key), json={"campaign_ids": [cid]})
    assert r.status_code == 502 and r.json()["error"]["code"] == "PAYMENT_CREATION_FAILED"
    assert (await status_of(db, cid)).status == "RASCUNHO"
    o = (await db.execute(select(Order))).scalar_one()
    assert o.status == "CANCELADO" and o.payment_order_id is not None
    assert any(c[0] == "release_hold" and c[2] == "PAYMENT_CREATION_FAILED" for c in deps.calls)
    # replay da mesma chave devolve o mesmo erro (não cobra de novo)
    r = await client.post("/api/v1/checkout", headers=H(s, key), json={"campaign_ids": [cid]})
    assert r.status_code == 502 and deps.names().count("create_payment_order_failed") == 1


async def test_margem_negativa_bloqueia_e_abaixo_do_minimo_alerta(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    # nova tabela ativa com custo direto alto → margem negativa

    await db.execute(text("UPDATE price_tables SET status = 'ARQUIVADA'"))
    await db.commit()
    await make_price_table(db, extra=[("COST_DIRECT", "*", "FIXED_CENTS", None, None, 10_000)])
    cid = await new_campaign(client, s, db=db, org=org)
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MARGIN_NEGATIVE"
    assert (await status_of(db, cid)).status == "RASCUNHO" and deps.calls == []
    # custo que deixa a margem positiva porém < 40% → passa com alerta auditado
    await db.execute(text("UPDATE price_tables SET status = 'ARQUIVADA'"))
    await db.commit()
    await make_price_table(db, extra=[("COST_DIRECT", "*", "PERCENT_BPS", None, None, 5000)])
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
    assert r.status_code == 201, r.text
    assert any("MARGIN_BELOW_MIN" in w for w in r.json()["warnings"])
    n = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'pricing.margin_below_min'"))).scalar_one()
    assert n == 1


async def _coupon(db, **kw):
    now = datetime.now(UTC)
    c = Coupon(id=uuid.uuid4(), code=kw.pop("code", f"C{uuid.uuid4().hex[:6].upper()}"), discount_type="PERCENT",
               discount_bps=kw.pop("bps", 1000), discount_cents=0, valid_from=kw.pop("valid_from", now - timedelta(days=1)),
               valid_until=kw.pop("valid_until", now + timedelta(days=1)), uses_count=0, min_order_cents=0,
               active=True, **kw)
    db.add(c)
    await db.commit()
    return c


async def test_cupom_aplicado_expirado_esgotado_e_devolvido_na_expiracao(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    c10 = await _coupon(db, code="DEZ10", bps=1000, max_uses=1)
    await _coupon(db, code="VELHO", valid_from=datetime.now(UTC) - timedelta(days=10),
                            valid_until=datetime.now(UTC) - timedelta(days=1))
    cid = await new_campaign(client, s, db=db, org=org)
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid], "coupon_code": "velho"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "COUPON_EXPIRED"
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid], "coupon_code": "dez10"})
    assert r.status_code == 201, r.text
    order_id = uuid.UUID(r.json()["order_id"])
    full = 2 * 24 * 7 * 2 * 10
    assert r.json()["total_cents"] == full - full // 10 and r.json()["discount_cents"] == full // 10
    pay = next(iter(deps.payments.values()))
    assert [i["kind"] for i in pay["items"]] == ["MEDIA", "DISCOUNT"]
    await db.refresh(c10)
    assert c10.uses_count == 1
    cid2 = await new_campaign(client, s, db=db, org=org)
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid2], "coupon_code": "DEZ10"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "COUPON_EXHAUSTED"
    # reserva expira sem pagamento → cupom devolvido, reservas liberadas, campanha volta a RASCUNHO
    await CK.on_order_expired(db, order_id)
    await db.commit()
    await db.refresh(c10)
    assert c10.uses_count == 0 and (await status_of(db, cid)).status == "RASCUNHO"
    assert any(x[0] == "release_hold" and x[2] == "PAYMENT_EXPIRED" for x in deps.calls)
    # pagamento tardio continua aceito: campanha segue para análise com a especificação paga
    o = await db.get(Order, order_id)
    await CK.on_order_paid(db, o.id, o.payment_order_id)
    await db.commit()
    c = await status_of(db, cid)
    assert c.status == "EM_ANALISE" and c.contracted_price_cents == full - full // 10
    assert (await db.get(Order, order_id)).status == "PAGO"


async def test_checkout_inicio_no_passado_e_rejeitada_sem_novo_pagamento(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org, start=date.today() - timedelta(days=3))
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CAMPAIGN_START_IN_PAST"
    await db.execute(text("UPDATE campaigns SET status = 'REJEITADA', requires_new_payment = false WHERE id = :i"),
                     {"i": uuid.UUID(cid)})
    await db.commit()
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CAMPAIGN_NOT_CHECKOUTABLE"


async def test_cancelar_pedido_aguardando_pagamento(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
    oid = r.json()["order_id"]
    r = await client.post(f"/api/v1/campaigns/{cid}/cancel", headers=s.headers, json={"reason": "desisti da compra"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELADA", r.text
    assert (await db.get(Order, uuid.UUID(oid))).status == "CANCELADO"
    assert any(x[0] == "release_hold" and x[2] == "ORDER_CANCELLED" for x in deps.calls)


async def test_cartao_de_credito_repassado_ao_financeiro_sem_vazar_token(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid], "method": "credit_card"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "PAYMENT_SOURCE_TOKEN_REQUIRED"
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid], "installments": 3})
    assert r.status_code == 400 and r.json()["error"]["code"] == "PAYMENT_METHOD_INVALID"
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid], "method": "boleto"})
    assert r.status_code == 422
    token = "tok_" + uuid.uuid4().hex
    key = f"card-{uuid.uuid4().hex}"
    body = {"campaign_ids": [cid], "method": "credit_card", "payment_source_token": token, "installments": 3}
    r = await client.post("/api/v1/checkout", headers=H(s, key), json=body)
    assert r.status_code == 201, r.text
    assert deps.last_payment_kwargs["method"] == "credit_card" and deps.last_payment_kwargs["installments"] == 3
    assert deps.last_payment_kwargs["token"] == token
    assert token not in r.text
    leaked = (await db.execute(text("SELECT count(*) FROM orders WHERE response::text LIKE :t OR request_hash = :t"),
                               {"t": f"%{token}%"})).scalar_one()
    leaked += (await db.execute(text("SELECT count(*) FROM audit_logs WHERE after_data::text LIKE :t"),
                                {"t": f"%{token}%"})).scalar_one()
    assert leaked == 0
    # replay com outro token (uso único) e mesma chave/forma → mesmo pedido, sem nova cobrança
    r2 = await client.post("/api/v1/checkout", headers=H(s, key), json={**body, "payment_source_token": "tok_outro123"})
    assert r2.status_code == 200 and r2.json()["order_id"] == r.json()["order_id"]
    assert deps.names().count("create_payment_order") == 1
    # mesma chave com outra forma/parcelas → 409
    r3 = await client.post("/api/v1/checkout", headers=H(s, key), json={**body, "installments": 2})
    assert r3.status_code == 409


async def test_cancel_quote_e_ids_de_cobrancas_pagas(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    r = await client.get(f"/api/v1/campaigns/{cid}/cancel-quote", headers=s.headers)
    assert r.status_code == 200 and r.json()["paid"] is False and r.json()["refund_total_cents"] == 0
    r = await client.post("/api/v1/checkout", headers=H(s), json={"campaign_ids": [cid]})
    total = r.json()["total_cents"]
    o = await db.get(Order, uuid.UUID(r.json()["order_id"]))
    await CK.on_order_paid(db, o.id, o.payment_order_id)
    await db.commit()
    d = (await client.get(f"/api/v1/campaigns/{cid}", headers=s.headers)).json()
    assert d["paid_payment_order_id"] == str(o.payment_order_id)
    assert d["paid_payment_order_ids"] == [str(o.payment_order_id)]
    r = await client.get(f"/api/v1/campaigns/{cid}/cancel-quote", headers=s.headers)
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["can_cancel"] is True and q["refund_total_cents"] == total
    assert q["payments"][0]["payment_order_id"] == str(o.payment_order_id)
    other = await make_org(db)
    uo = await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id))
    so = await login_as(client, uo)
    assert (await client.get(f"/api/v1/campaigns/{cid}/cancel-quote", headers=so.headers)).status_code == 404
    # reembolso fora da janela não impede o cancelamento (registrado como não reembolsado)
    deps.refund_errors[o.payment_order_id] = "REFUND_WINDOW_EXPIRED"
    r = await client.post(f"/api/v1/campaigns/{cid}/cancel", headers=s.headers, json={"reason": "fim do contrato"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELADA", r.text
    row = (await db.execute(text("SELECT after_data FROM audit_logs WHERE action = 'campaign.cancel_refunds'"))).scalar_one()
    assert row["refunds"][0]["skipped"] == "REFUND_WINDOW_EXPIRED"
