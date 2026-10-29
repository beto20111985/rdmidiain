"""W-BIZ — Suporte: chamado por ID de criativo (RD-XXXXXX) com busca exata pela equipe,
estabelecimento sem identidade do anunciante, comentário interno invisível ao cliente,
escopo cross-tenant (404 — regressão do achado A7-A10), status/SLA/atribuição."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.support import Ticket
from tests.factories import login_as, make_user
from tests.inv_support import link, mk_campaign, mk_creative, mk_establishment, mk_org, mk_screen


@pytest.fixture(autouse=True)
async def _drain_after_test(clean_state):
    yield
    from app.modules.notifications.service import drain

    await drain()


async def _world(client, db):
    adv_org = await mk_org(db, "Bebidas Anunciante SA")
    creative = await mk_creative(db, adv_org)
    camp = await mk_campaign(db, adv_org, status="ATIVA", name="Verão Gelado")
    await link(db, camp, creative)
    est_org = await mk_org(db, "Mercado Parceiro")
    est = await mk_establishment(db, est_org)
    screen = await mk_screen(db, est)
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", est_org.id))
    staff = await make_user(db, role="employee")  # support.view|create|manage GLOBAL
    return adv_org, creative, camp, est_org, est, screen, owner, staff


async def test_removal_request_by_creative_code_and_exact_lookup(client, db, mail):
    adv_org, creative, camp, est_org, est, screen, owner, staff = await _world(client, db)
    so = await login_as(client, owner)
    ss = await login_as(client, staff)
    typed = creative.code.lower().replace("rd-", "rd- ")  # como o cliente digita
    r = await client.post("/api/v1/support/tickets", headers=so.headers, json={
        "establishment_id": str(est.id), "screen_id": str(screen.id), "creative_code": typed,
        "category": "RETIRADA_ANUNCIO", "title": "Pedido de retirada", "description": "Anúncio não combina com a loja"})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["creative_code"] == creative.code and t["org_id"] == str(est_org.id)
    # estabelecimento NUNCA recebe identidade comercial do anunciante
    for k in ("advertiser_org_id", "campaign_id", "creative_id", "assigned_to"):
        assert k not in t
    detail = (await client.get(f"/api/v1/support/tickets/{t['id']}", headers=so.headers)).json()
    assert "advertiser_org_id" not in detail and "Bebidas" not in str(detail)
    # equipe vê o vínculo resolvido
    sd = (await client.get(f"/api/v1/support/tickets/{t['id']}", headers=ss.headers)).json()
    assert sd["advertiser_org_id"] == str(adv_org.id) and sd["campaign_id"] == str(camp.id)
    assert sd["creative_id"] == str(creative.id) and sd["creative_found"] is True
    # busca EXATA pelo código (equipe)
    r = await client.get(f"/api/v1/support/creatives/{creative.code}", headers=ss.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["creative"]["code"] == creative.code and body["advertiser"]["name"] == "Bebidas Anunciante SA"
    assert body["campaigns"][0]["name"] == "Verão Gelado" and body["tickets"][0]["number"] == t["number"]
    # sem busca parcial/curinga
    assert (await client.get(f"/api/v1/support/creatives/{creative.code[:5]}", headers=ss.headers)).status_code == 400
    assert (await client.get("/api/v1/support/creatives/RD-%25", headers=ss.headers)).status_code == 400
    other_code = "RD-" + ("A" * 6 if creative.code != "RD-AAAAAA" else "B" * 6)
    assert (await client.get(f"/api/v1/support/creatives/{other_code}", headers=ss.headers)).status_code == 404
    # estabelecimento não usa a busca da equipe
    assert (await client.get(f"/api/v1/support/creatives/{creative.code}", headers=so.headers)).status_code == 403
    # retirada exige o código; formato inválido rejeitado
    r = await client.post("/api/v1/support/tickets", headers=so.headers, json={
        "establishment_id": str(est.id), "category": "RETIRADA_ANUNCIO", "title": "x retirada",
        "description": "sem código"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CREATIVE_CODE_REQUIRED"
    r = await client.post("/api/v1/support/tickets", headers=so.headers, json={
        "establishment_id": str(est.id), "creative_code": "RD-ILOK01", "title": "código inválido",
        "description": "letras proibidas"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CREATIVE_CODE_INVALID"


async def test_internal_comments_hidden_and_status_flow(client, db, mail):
    adv_org, creative, camp, est_org, est, screen, owner, staff = await _world(client, db)
    tech = await make_user(db, role="technician")
    so, ss = await login_as(client, owner), await login_as(client, staff)
    t = (await client.post("/api/v1/support/tickets", headers=so.headers, json={
        "establishment_id": str(est.id), "screen_id": str(screen.id), "category": "PROBLEMA_TELA",
        "priority": "ALTA", "title": "Tela apagada", "description": "Não liga desde ontem"})).json()
    row = await db.get(Ticket, uuid.UUID(t["id"]))
    assert row.sla_due_at - row.created_at <= timedelta(hours=8, seconds=5)
    assert row.sla_due_at - row.created_at >= timedelta(hours=7, minutes=59)
    # cliente não escreve comentário interno
    r = await client.post(f"/api/v1/support/tickets/{t['id']}/messages", headers=so.headers,
                          json={"body": "tentando nota interna", "is_internal": True})
    assert r.status_code == 403
    assert (await client.post(f"/api/v1/support/tickets/{t['id']}/messages", headers=ss.headers,
                              json={"body": "Cliente já trocou a fonte antes (interno)", "is_internal": True})).status_code == 201
    assert (await client.post(f"/api/v1/support/tickets/{t['id']}/messages", headers=ss.headers,
                              json={"body": "Vamos enviar um técnico.", "is_internal": False})).status_code == 201
    cm = (await client.get(f"/api/v1/support/tickets/{t['id']}/messages", headers=so.headers)).json()["items"]
    assert [m["body"] for m in cm] == ["Vamos enviar um técnico."]
    sm = (await client.get(f"/api/v1/support/tickets/{t['id']}/messages", headers=ss.headers)).json()["items"]
    assert len(sm) == 2
    # cliente não muda status operacional
    r = await client.post(f"/api/v1/support/tickets/{t['id']}/status", headers=so.headers, json={"status": "RESOLVIDO"})
    assert r.status_code == 403
    # atribuição ao técnico (atribuição é evento interno)
    r = await client.post(f"/api/v1/support/tickets/{t['id']}/assign", headers=ss.headers, json={"user_id": str(tech.id)})
    assert r.status_code == 200 and r.json()["status"] == "EM_ATENDIMENTO", r.text
    st = await login_as(client, tech)
    assert (await client.get(f"/api/v1/support/tickets/{t['id']}", headers=st.headers)).status_code == 200
    r = await client.post(f"/api/v1/support/tickets/{t['id']}/status", headers=st.headers, json={"status": "RESOLVIDO"})
    assert r.status_code == 200
    hist_client = (await client.get(f"/api/v1/support/tickets/{t['id']}", headers=so.headers)).json()["history"]
    assert all(h["type"] != "ATRIBUICAO" for h in hist_client)
    r = await client.post(f"/api/v1/support/tickets/{t['id']}/status", headers=so.headers, json={"status": "FECHADO"})
    assert r.status_code == 200 and r.json()["status"] == "FECHADO"
    r = await client.post(f"/api/v1/support/tickets/{t['id']}/messages", headers=so.headers, json={"body": "mais uma"})
    assert r.status_code == 409
    # histórico é append-only
    with pytest.raises(DBAPIError):
        await db.execute(text("UPDATE ticket_events SET to_value='X'"))
    await db.rollback()


async def test_cross_tenant_is_404_regression_a10(client, db, mail):
    adv_org, creative, camp, est_org, est, screen, owner, staff = await _world(client, db)
    so = await login_as(client, owner)
    t = (await client.post("/api/v1/support/tickets", headers=so.headers, json={
        "establishment_id": str(est.id), "title": "Dúvida", "description": "Como funciona?"})).json()
    intruder_org = await mk_org(db)
    intruder = await make_user(db, role="advertiser", scope=("ORGANIZATION", intruder_org.id))
    si = await login_as(client, intruder)
    for method, url, body in (
        ("get", f"/api/v1/support/tickets/{t['id']}", None),
        ("get", f"/api/v1/support/tickets/{t['id']}/messages", None),
        ("post", f"/api/v1/support/tickets/{t['id']}/messages", {"body": "invasão", "is_internal": True}),
        ("post", f"/api/v1/support/tickets/{t['id']}/messages", {"body": "invasão"}),
        ("post", f"/api/v1/support/tickets/{t['id']}/status", {"status": "CANCELADO"}),
    ):
        r = await getattr(client, method)(url, headers=si.headers, **({"json": body} if body else {}))
        assert r.status_code == 404, (url, r.status_code, r.text)
    lst = (await client.get("/api/v1/support/tickets", headers=si.headers)).json()
    assert lst["total"] == 0 and lst["items"] == []
    # abrir chamado em nome de outra organização → 404
    r = await client.post("/api/v1/support/tickets", headers=si.headers, json={
        "establishment_id": str(est.id), "title": "Falso", "description": "Em nome de outro"})
    assert r.status_code == 404
    # o dono vê só o seu
    assert (await client.get("/api/v1/support/tickets", headers=so.headers)).json()["total"] == 1
    assert (await client.get("/api/v1/support/tickets")).status_code == 401


async def test_message_authors_rule_and_scoped_assignees(client, db, mail):
    adv_org, creative, camp, est_org, est, screen, owner, _staff = await _world(client, db)
    colleague = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", est_org.id), name="Carlos Gerente")
    agent = await make_user(db, role="employee", name="Joana Suporte")
    tech_global = await make_user(db, role="technician", name="Técnico Nacional")
    tech_go = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Técnico Goiânia")
    tech_sp = await make_user(db, role="technician", scope=("CITY", "SP:sao-paulo"), name="Técnico São Paulo")
    await make_user(db, role="technician", name="Técnico Inativo", is_active=False)
    so, sc, sa = await login_as(client, owner), await login_as(client, colleague), await login_as(client, agent)
    t = (await client.post("/api/v1/support/tickets", headers=so.headers, json={
        "establishment_id": str(est.id), "title": "Tela travada", "description": "Congelou"})).json()
    await client.post(f"/api/v1/support/tickets/{t['id']}/messages", headers=so.headers, json={"body": "Segue foto"})
    await client.post(f"/api/v1/support/tickets/{t['id']}/messages", headers=sc.headers, json={"body": "Também vi"})
    r = await client.post(f"/api/v1/support/tickets/{t['id']}/messages", headers=sa.headers, json={"body": "Verificando"})
    assert r.status_code == 201 and r.json()["author_name"] == "Joana Suporte" and r.json()["author_is_staff"] is True
    await client.post(f"/api/v1/support/tickets/{t['id']}/messages", headers=sa.headers,
                      json={"body": "nota interna", "is_internal": True})
    # cliente: equipe aparece como "Equipe RD" (sem id), colegas da org pelo nome, interno oculto
    for url in (f"/api/v1/support/tickets/{t['id']}/messages", f"/api/v1/support/tickets/{t['id']}"):
        body = (await client.get(url, headers=so.headers)).json()
        msgs = body["items"] if "items" in body else body["messages"]
        assert [(m["author_name"], m["author_is_staff"]) for m in msgs] == [
            (owner.full_name, False), ("Carlos Gerente", False), ("Equipe RD", True)]
        assert msgs[2]["author_id"] is None and msgs[0]["author_id"] == str(owner.id)
        assert "Joana" not in str(body)
    # equipe: nomes reais e mensagem interna
    msgs = (await client.get(f"/api/v1/support/tickets/{t['id']}/messages", headers=sa.headers)).json()["items"]
    assert [m["author_name"] for m in msgs] == [owner.full_name, "Carlos Gerente", "Joana Suporte", "Joana Suporte"]
    assert msgs[2]["author_id"] == str(agent.id)

    # atribuíveis do chamado (Goiânia): equipe GLOBAL + regional GO; nunca SP, inativo ou cliente
    r = await client.get(f"/api/v1/support/assignees?ticket_id={t['id']}", headers=sa.headers)
    assert r.status_code == 200, r.text
    names = {c["name"] for c in r.json()["items"]}
    assert {"Joana Suporte", "Técnico Nacional", "Técnico Goiânia"} <= names
    assert "Técnico São Paulo" not in names and "Técnico Inativo" not in names
    assert str(owner.id) not in {c["user_id"] for c in r.json()["items"]}
    assert str(colleague.id) not in {c["user_id"] for c in r.json()["items"]}
    assert all(set(c) == {"user_id", "name", "scopes", "roles", "open_tickets"} for c in r.json()["items"])
    assert "@" not in r.text
    go = next(c for c in r.json()["items"] if c["name"] == "Técnico Goiânia")
    assert go["scopes"] == [{"type": "CITY", "value": "GO:goiania"}]
    # lista coerente com a atribuição: técnico de SP não pode receber chamado de Goiânia
    r = await client.post(f"/api/v1/support/tickets/{t['id']}/assign", headers=sa.headers,
                          json={"user_id": str(tech_sp.id)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "ASSIGNEE_INVALID"
    assert (await client.post(f"/api/v1/support/tickets/{t['id']}/assign", headers=sa.headers,
                              json={"user_id": str(tech_go.id)})).status_code == 200
    r = await client.get(f"/api/v1/support/assignees?ticket_id={t['id']}", headers=sa.headers)
    assert next(c for c in r.json()["items"] if c["name"] == "Técnico Goiânia")["open_tickets"] == 1
    # sem chamado: GLOBAL vê todas as regiões; regional vê GLOBAL + a sua
    names_all = {c["name"] for c in (await client.get("/api/v1/support/assignees", headers=sa.headers)).json()["items"]}
    assert "Técnico São Paulo" in names_all and "Técnico Nacional" in names_all
    sgo = await login_as(client, tech_go)
    names_go = {c["name"] for c in (await client.get("/api/v1/support/assignees", headers=sgo.headers)).json()["items"]}
    assert "Técnico Goiânia" in names_go and "Técnico Nacional" in names_go and "Técnico São Paulo" not in names_go
    # cliente não lista atribuíveis
    assert (await client.get("/api/v1/support/assignees", headers=so.headers)).status_code == 403
    assert tech_global.id
