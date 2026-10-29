"""Ciclo de vida da campanha com PostgreSQL real: checkout → pagamento → aprovação (MANUAL/AUTO/cliente
confiável) → AGENDADA/SINCRONIZANDO/ATIVA → pausa → alteração pós-aprovação → vencimento local.
Dependências de outros domínios (W-INV hold/convert e W-FIN cobrança) via ``FakeDeps``."""
import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.core.authz import AuthContext
from app.core.settings_service import set_setting
from app.models.campaigns import Campaign, CampaignRevision, CampaignStatusHistory, Creative
from app.modules.campaigns import checkout as CK
from app.modules.campaigns import service as S
from app.modules.campaigns.notify import pending_events
from tests.camp_support import FakeDeps, Tax, future_monday, make_est, make_media, make_price_table, make_screen, \
    set_advertiser
from tests.factories import login_as, make_org, make_user

SYS = AuthContext.system("system")


@pytest.fixture
def deps(monkeypatch):
    return FakeDeps().install(monkeypatch)


async def world(db, client, *, n=2, tz="America/Sao_Paulo", state="GO", city="Goiânia"):
    t = await Tax().create(db)
    org = await make_org(db)
    await set_advertiser(db, org, category=t.academia)
    ests = [await make_est(db, category=t.supermercado, segments=[t.alimentos], tz=tz, state=state, city=city)
            for _ in range(n)]
    screens = [await make_screen(db, e) for e in ests]
    await make_price_table(db)
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, adv)
    return t, org, ests, screens, s


async def new_campaign(client, s, *, with_creative=True, db=None, org=None, start=None, days=7, city="GO:Goiânia",
                       duration=30):
    start = start or future_monday()
    r = await client.post("/api/v1/campaigns", headers=s.headers, json={
        "name": "Campanha Teste", "period_type": "AVULSO", "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=days - 1)).isoformat(), "duration_s": duration, "freq_kind": "PER_HOUR",
        "freq_value": 2, "targets": [{"type": "CITY", "value": city}]})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    if with_creative:
        m = await make_media(db, org, duration_ms=duration * 1000)
        r = await client.post("/api/v1/creatives", headers=s.headers,
                              json={"name": "Vídeo", "media_file_id": str(m.id), "duration_seconds": duration})
        assert r.status_code == 201, r.text
        r = await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=s.headers,
                              json={"creative_id": r.json()["id"]})
        assert r.status_code == 201, r.text
    return cid


async def checkout(client, s, cid, key=None, coupon=None):
    body = {} if coupon is None else {"coupon_code": coupon}
    return await client.post(f"/api/v1/campaigns/{cid}/checkout", headers={**s.headers,
                             "Idempotency-Key": key or f"key-{uuid.uuid4().hex}"}, json=body)


async def pay(db, order_id, deps):
    from app.models.pricing import Order

    o = await db.get(Order, uuid.UUID(order_id))
    await CK.on_order_paid(db, o.id, o.payment_order_id)
    await db.commit()


async def status_of(db, cid) -> Campaign:
    return (await db.execute(select(Campaign).where(Campaign.id == uuid.UUID(str(cid)))
                             .execution_options(populate_existing=True))).scalar_one()


async def test_pagamento_manual_vai_para_analise_nunca_aprovada(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    r = await checkout(client, s, cid)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (await status_of(db, cid)).status == "AGUARDANDO_PAGAMENTO"
    assert deps.names()[:2] == ["hold", "create_payment_order"]
    assert body["payment"]["pix_copy_paste"] and body["total_cents"] == body["items"][0]["amount_cents"]
    # 2/h × 24 h × 7 dias × 2 telas × 10 c
    assert body["items"][0]["media_cents"] == 2 * 24 * 7 * 2 * 10
    await pay(db, body["order_id"], deps)
    c = await status_of(db, cid)
    assert c.status == "EM_ANALISE" and c.approved_at is None and c.live_revision_id is None
    assert c.review_due_at is not None and c.paid_at is not None
    assert "convert_hold" in deps.names()
    hist = [(h.from_status, h.to_status) for h in (await db.execute(
        select(CampaignStatusHistory).where(CampaignStatusHistory.campaign_id == c.id)
        .order_by(CampaignStatusHistory.created_at))).scalars()]
    assert hist == [("RASCUNHO", "AGUARDANDO_PAGAMENTO"), ("AGUARDANDO_PAGAMENTO", "EM_ANALISE")]
    # replay do webhook: idempotente
    await pay(db, body["order_id"], deps)
    assert (await status_of(db, cid)).status == "EM_ANALISE"
    assert deps.names().count("convert_hold") == 1


async def test_pagamento_auto_global_aprova(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    cid = await new_campaign(client, s, db=db, org=org)
    r = await checkout(client, s, cid)
    await pay(db, r.json()["order_id"], deps)
    c = await status_of(db, cid)
    assert c.status == "APROVADA" and c.approval_source == "AUTO_GLOBAL" and c.live_revision_id is not None
    cr = (await db.execute(select(Creative).where(Creative.org_id == org.id))).scalar_one()
    assert cr.status == "APROVADO"
    assert ("invalidate_screens", 2, f"campaign_approved:{c.id}") in deps.calls


async def test_excecao_cliente_confiavel(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    r = await client.put(f"/api/v1/campaigns/approval-policies/{org.id}", headers=sa.headers,
                         json={"mode": "AUTO", "reason": "Cliente premium de confiança pessoal"})
    assert r.status_code == 200, r.text
    # anunciante não altera a própria política
    r = await client.put(f"/api/v1/campaigns/approval-policies/{org.id}", headers=s.headers,
                         json={"mode": "AUTO", "reason": "quero aprovar sozinho"})
    assert r.status_code == 403
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    c = await status_of(db, cid)
    assert c.status == "APROVADA" and c.approval_source == "AUTO_TRUSTED_CLIENT"
    # outra organização continua MANUAL; política MANUAL explícita vence o AUTO global
    other = await make_org(db)
    await set_advertiser(db, other, category=t.academia)
    u2 = await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id))
    s2 = await login_as(client, u2)
    cid2 = await new_campaign(client, s2, db=db, org=other)
    await pay(db, (await checkout(client, s2, cid2)).json()["order_id"], deps)
    assert (await status_of(db, cid2)).status == "EM_ANALISE"
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    r = await client.put(f"/api/v1/campaigns/approval-policies/{other.id}", headers=sa.headers,
                         json={"mode": "MANUAL", "reason": "Cliente com histórico de conteúdo irregular"})
    assert r.status_code == 200
    cid3 = await new_campaign(client, s2, db=db, org=other)
    await pay(db, (await checkout(client, s2, cid3)).json()["order_id"], deps)
    assert (await status_of(db, cid3)).status == "EM_ANALISE"


async def test_sem_midia_aguarda_midia_e_depois_analise(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org, with_creative=False)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    assert (await status_of(db, cid)).status == "AGUARDANDO_MIDIA"
    r = await client.post(f"/api/v1/campaigns/{cid}/submit-media", headers=s.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CREATIVES_NOT_READY"
    m = await make_media(db, org)
    cr = (await client.post("/api/v1/creatives", headers=s.headers,
                            json={"name": "VV", "media_file_id": str(m.id), "duration_seconds": 30})).json()
    assert (await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=s.headers,
                              json={"creative_id": cr["id"]})).status_code == 201
    r = await client.post(f"/api/v1/campaigns/{cid}/submit-media", headers=s.headers)
    assert r.status_code == 200 and r.json()["status"] == "EM_ANALISE"


async def test_aprovar_rejeitar_guardas_reenvio_e_fila(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    cid = await new_campaign(client, s, db=db, org=org)
    # aprovar/rejeitar RASCUNHO → 409 (só de EM_ANALISE); anunciante não aprova (403)
    assert (await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={})).status_code == 409
    r = await client.post(f"/api/v1/campaigns/{cid}/reject", headers=sa.headers, json={"reason": "conteúdo impróprio"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CAMPAIGN_INVALID_TRANSITION"
    assert (await client.post(f"/api/v1/campaigns/{cid}/approve", headers=s.headers, json={})).status_code == 403
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    q = await client.get("/api/v1/campaigns/review-queue", headers=sa.headers)
    assert q.status_code == 200 and [i["id"] for i in q.json()["items"]] == [cid]
    assert (await client.get("/api/v1/campaigns/review-queue", headers=s.headers)).status_code == 403
    r = await client.post(f"/api/v1/campaigns/{cid}/reject", headers=sa.headers, json={"reason": "curto"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "REJECTION_REASON_REQUIRED"
    crid = (await client.get(f"/api/v1/campaigns/{cid}", headers=s.headers)).json()["creatives"][0]["creative_id"]
    r = await client.post(f"/api/v1/campaigns/{cid}/reject", headers=sa.headers,
                          json={"reason": "Vídeo com texto ilegível no rodapé", "creative_ids": [crid]})
    assert r.status_code == 200 and r.json()["status"] == "REJEITADA"
    assert r.json()["rejection_reason"] == "Vídeo com texto ilegível no rodapé"
    # reenviar com o criativo rejeitado ainda vinculado → 409; corrigir (nova versão) e reenviar
    r = await client.post(f"/api/v1/campaigns/{cid}/resubmit", headers=s.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CREATIVE_REJECTED_STILL_LINKED"
    m = await make_media(db, org)
    v2 = (await client.post(f"/api/v1/creatives/{crid}/versions", headers=s.headers,
                            json={"media_file_id": str(m.id)})).json()
    assert (await client.delete(f"/api/v1/campaigns/{cid}/creatives/{crid}", headers=s.headers)).status_code == 204
    assert (await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=s.headers,
                              json={"creative_id": v2["id"]})).status_code == 201
    r = await client.post(f"/api/v1/campaigns/{cid}/resubmit", headers=s.headers)
    assert r.status_code == 200 and r.json()["status"] == "EM_ANALISE", r.text
    r = await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={"note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "APROVADA"
    assert r.json()["approval_source"] == "MANUAL"
    # auditoria de quem aprovou/rejeitou
    rows = (await db.execute(text("SELECT action, actor_user_id FROM audit_logs WHERE entity_id = :e "
                                  "AND action IN ('campaign.approved','campaign.rejected') ORDER BY seq"),
                             {"e": cid})).all()
    assert [r.action for r in rows] == ["campaign.rejected", "campaign.approved"]
    assert all(r.actor_user_id == admin.id for r in rows)


async def test_aprovacao_concorrente_um_vence(db, client, deps, db_engine):
    from app.core.database import session_factory

    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    admin = await make_user(db, role="admin", mfa=True)
    from app.core.authz import load_grants

    ctx = AuthContext(kind="user", user=admin, grants=await load_grants(db, admin.id))
    results = []

    async def run(fn):
        async with session_factory(db_engine)() as s2:
            try:
                await fn(s2)
                await s2.commit()
                results.append("ok")
            except Exception as exc:  # noqa: BLE001
                await s2.rollback()
                results.append(getattr(exc, "code", type(exc).__name__))

    await asyncio.gather(run(lambda x: S.approve(x, ctx, uuid.UUID(cid))),
                         run(lambda x: S.reject(x, ctx, uuid.UUID(cid), reason="Rejeição concorrente de teste")))
    assert sorted(results) == ["CAMPAIGN_INVALID_TRANSITION", "ok"], results
    n = (await db.execute(select(func.count()).select_from(CampaignStatusHistory)
                          .where(CampaignStatusHistory.campaign_id == uuid.UUID(cid),
                                 CampaignStatusHistory.from_status == "EM_ANALISE"))).scalar_one()
    assert n == 1


async def test_agendamento_sincronizacao_ativacao_pausa_e_kill(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    c = await status_of(db, cid)
    assert await S.mark_scheduled(db, c.id) is True
    assert await S.mark_scheduled(db, c.id) is False  # idempotente
    assert await S.mark_syncing(db, c.id) is True
    # ack antes da data de início local → não ativa
    assert await S.mark_active_from_ack(db, [c.id], screen_id=screens[0].id, manifest_version=3) == []
    c.start_date = date.today() - timedelta(days=1)
    await db.commit()
    assert await S.mark_active_from_ack(db, [c.id], screen_id=screens[0].id, manifest_version=3) == [c.id]
    await db.commit()
    assert (await status_of(db, cid)).status == "ATIVA"
    # REPRODUZINDO é derivado (sem PoP recente → False; nunca status armazenado)
    d = (await client.get(f"/api/v1/campaigns/{cid}", headers=s.headers)).json()
    assert d["status"] == "ATIVA" and d["reproducing"]["playing"] in (False, None)
    r = await client.post(f"/api/v1/campaigns/{cid}/pause", headers=s.headers, json={"reason": "pausa do cliente"})
    assert r.status_code == 200 and r.json()["status"] == "PAUSADA" and r.json()["playable"] is False
    r = await client.post(f"/api/v1/campaigns/{cid}/resume", headers=s.headers)
    assert r.json()["status"] == "ATIVA"
    # kill: só RD (campaigns.kill global)
    assert (await client.post(f"/api/v1/campaigns/{cid}/kill", headers=s.headers,
                              json={"reason": "emergência"})).status_code == 403
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    r = await client.post(f"/api/v1/campaigns/{cid}/kill", headers=sa.headers,
                          json={"reason": "Conteúdo denunciado por ticket"})
    assert r.status_code == 200 and r.json()["status"] == "PAUSADA"


async def test_vencimento_2359_local_por_fuso_da_tela(db, client, deps):
    """São Paulo (UTC−3) e Rio Branco (UTC−5): FINALIZADA só após 23:59:59 da ÚLTIMA tela."""
    t, org, ests, screens, s = await world(db, client, n=1)
    rb = await make_est(db, category=t.supermercado, segments=[t.alimentos], tz="America/Rio_Branco", state="GO",
                        city="Goiânia")
    await make_screen(db, rb)
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    c = await status_of(db, cid)
    assert c.status == "APROVADA"
    end = c.end_date
    sp_end = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone(timedelta(hours=-3)))
    rb_end = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5)))
    assert await S.finalize_expired(db, now=sp_end + timedelta(seconds=1)) == []  # Rio Branco ainda veicula
    assert await S.finalize_expired(db, now=rb_end) == []  # 23:59:59 exato ainda vale
    assert await S.finalize_expired(db, now=rb_end + timedelta(seconds=1)) == [c.id]
    await db.commit()
    assert (await status_of(db, cid)).status == "FINALIZADA"


async def test_alteracao_pos_aprovacao_mantem_versao_anterior(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    assert (await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={})).status_code == 200
    c = await status_of(db, cid)
    await S.mark_syncing(db, c.id)
    c.start_date = date.today() - timedelta(days=1)
    await db.commit()
    await S.mark_active_from_ack(db, [c.id], screen_id=screens[0].id, manifest_version=1)
    await db.commit()
    live_before = c.live_revision_id
    old_cr = (await client.get(f"/api/v1/campaigns/{cid}", headers=s.headers)).json()["creatives"][0]
    # troca do criativo (texto/mídia) = nova versão (novo código) via pedido de alteração
    m = await make_media(db, org)
    v2 = (await client.post(f"/api/v1/creatives/{old_cr['creative_id']}/versions", headers=s.headers,
                            json={"media_file_id": str(m.id), "ad_text": "Novo texto"})).json()
    assert v2["code"] != old_cr["code"]
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers=s.headers, json={
        "creative_replacements": [{"old_creative_id": old_cr["creative_id"], "new_creative_id": v2["id"]}]})
    assert r.status_code == 201, r.text
    assert r.json()["diff_cents"] == 0 and r.json()["campaign_status"] == "EM_ANALISE"
    c = await status_of(db, cid)
    assert c.status == "EM_ANALISE" and c.live_revision_id == live_before and c.status_before_review == "ATIVA"
    # a versão anterior continua tocando (W-INV usa a revisão vigente)
    specs = await S.playable_campaign_specs(db)
    mine = [x for x in specs if x.campaign_id == c.id]
    assert len(mine) == 1 and mine[0].revision_id == live_before and mine[0].creative_ids == [old_cr["creative_id"]]
    assert mine[0].has_pending_change
    # nova alteração enquanto há uma pendente → 409
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers=s.headers, json={"days_mask": 31})
    assert r.status_code == 409
    r = await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={})
    assert r.status_code == 200, r.text
    c = await status_of(db, cid)
    assert c.status == "APROVADA" and c.live_revision_id != live_before
    specs = [x for x in await S.playable_campaign_specs(db) if x.campaign_id == c.id]
    assert specs[0].creative_ids == [v2["id"]]
    old = await db.get(Creative, uuid.UUID(old_cr["creative_id"]))
    assert old.status == "SUBSTITUIDO" and old.code == old_cr["code"]
    prev = await db.get(CampaignRevision, live_before)
    assert prev.status == "SUBSTITUIDA"


async def test_alteracao_regiao_recalcula_preco_cobra_diferenca_e_rejeicao_volta(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    await make_est(db, category=t.supermercado, segments=[t.alimentos], state="GO", city="Anápolis")
    an = (await db.execute(select(func.count()).select_from(text("establishments")))).scalar_one()
    assert an == 3
    from app.models.establishments import Establishment

    e3 = (await db.execute(select(Establishment).where(Establishment.city == "Anápolis"))).scalar_one()
    await make_screen(db, e3)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={})
    c = await status_of(db, cid)
    price0 = c.contracted_price_cents
    # região maior (+Anápolis) → preço maior → diferença cobrada antes da análise
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers={**s.headers, "Idempotency-Key": "chg-key-0001"},
                          json={"targets": [{"type": "STATE", "value": "GO"}]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["new_price_cents"] == price0 * 3 // 2 and out["diff_cents"] == price0 // 2
    assert out["order"]["total_cents"] == price0 // 2 and out["campaign_status"] == "APROVADA"
    hold_new = [h for h in deps.holds.values() if h.get("replaces")]
    assert len(hold_new) == 1 and len(hold_new[0]["screens"]) == 3
    await pay(db, out["order"]["order_id"], deps)
    c = await status_of(db, cid)
    assert c.status == "EM_ANALISE" and c.contracted_price_cents == price0  # só muda ao aprovar
    # rejeição da alteração: volta ao status anterior com a versão vigente
    r = await client.post(f"/api/v1/campaigns/{cid}/reject", headers=sa.headers,
                          json={"reason": "Região fora da política comercial"})
    assert r.status_code == 200 and r.json()["status"] == "APROVADA", r.text
    c = await status_of(db, cid)
    assert c.pending_revision_id is None and c.contracted_price_cents == price0
    rev = (await db.execute(select(CampaignRevision).where(CampaignRevision.campaign_id == c.id,
                                                           CampaignRevision.kind == "CHANGE"))).scalar_one()
    assert rev.status == "REJEITADA" and rev.change_summary.get("requires_refund_review") is True


async def test_eventos_publicados_somente_apos_commit(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    from app.modules.campaigns.service import create_campaign
    from app.core.authz import load_grants
    from tests.factories import make_user as mk

    u = await mk(db, role="advertiser", scope=("ORGANIZATION", org.id))
    ctx = AuthContext(kind="user", user=u, grants=await load_grants(db, u.id))
    await create_campaign(db, ctx, org_id=org.id, data={"name": "Evento"})
    assert [e[0] for e in pending_events(db)] == ["campaign.updated"]
    await db.rollback()
    assert pending_events(db) == []  # rollback descarta: nada publicado


async def test_aprovacao_em_massa_parcial_e_sla(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    ids = []
    for _ in range(2):
        cid = await new_campaign(client, s, db=db, org=org)
        await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
        ids.append(cid)
    draft = await new_campaign(client, s, db=db, org=org)  # não está EM_ANALISE
    c0 = await status_of(db, ids[0])
    assert c0.review_due_at - c0.submitted_at == timedelta(hours=24)  # SLA padrão (campaign.review_sla_hours)
    r = await client.post("/api/v1/campaigns/approvals/bulk", headers=sa.headers,
                          json={"campaign_ids": ids + [draft], "action": "approve"})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["done"]) == sorted(ids)
    assert [f["campaign_id"] for f in r.json()["failed"]] == [draft]
    assert r.json()["failed"][0]["code"] == "CAMPAIGN_INVALID_TRANSITION"
    for cid in ids:
        assert (await status_of(db, cid)).status == "APROVADA"
    assert (await status_of(db, draft)).status == "RASCUNHO"
    r = await client.post("/api/v1/campaigns/approvals/bulk", headers=sa.headers,
                          json={"campaign_ids": ids, "action": "reject"})
    assert r.status_code == 400  # rejeição em massa exige motivo


async def test_cancelamento_de_campanha_paga_aciona_reembolso_e_libera_reserva(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    r = await client.post(f"/api/v1/campaigns/{cid}/cancel", headers=s.headers,
                          json={"reason": "Mudança de estratégia"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELADA", r.text
    assert "request_refund" in deps.names()
    assert any(c[0] == "release_hold" and c[2] == "CAMPAIGN_CANCELLED" for c in deps.calls)
    # terminal: nada mais muda
    r = await client.post(f"/api/v1/campaigns/{cid}/cancel", headers=s.headers, json={"reason": "de novo"})
    assert r.status_code == 409


async def test_duplicar_campanha_copia_especificacao_sem_preco_nem_status(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    r = await client.post(f"/api/v1/campaigns/{cid}/duplicate", headers=s.headers)
    assert r.status_code == 201, r.text
    d = r.json()
    src = (await client.get(f"/api/v1/campaigns/{cid}", headers=s.headers)).json()
    assert d["id"] != cid and d["status"] == "RASCUNHO" and d["contracted_price_cents"] == 0
    assert d["targets"] == src["targets"] and d["duration_s"] == src["duration_s"]
    assert [c["creative_id"] for c in d["creatives"]] == [c["creative_id"] for c in src["creatives"]]
    other = await make_org(db)
    uo = await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id))
    so = await login_as(client, uo)
    assert (await client.post(f"/api/v1/campaigns/{cid}/duplicate", headers=so.headers)).status_code == 404


async def test_eventos_campaign_updated_e_approval_pending_chegam_ao_redis(db, client, deps, redis):
    import json

    from app.core import events

    t, org, ests, screens, s = await world(db, client)
    ps = redis.pubsub()
    await ps.psubscribe(events.subscribe_pattern())
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    got = []
    for _ in range(100):
        m = await ps.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if m and m.get("type") == "pmessage":
            got.append((m["channel"], json.loads(m["data"])))
        if any(e["type"] == "approval.pending" for _, e in got):
            break
    await ps.aclose()
    upd = [e for ch, e in got if e["type"] == "campaign.updated" and e["data"]["id"] == cid]
    assert {e["data"]["status"] for e in upd} >= {"RASCUNHO", "AGUARDANDO_PAGAMENTO", "EM_ANALISE"}
    assert all(ch.endswith(f"events:org:{org.id}") for ch, e in got if e["type"] == "campaign.updated")
    pend = [(ch, e) for ch, e in got if e["type"] == "approval.pending"]
    assert pend and pend[0][0].endswith("events:global")  # só equipe RD com campaigns.approve global recebe


async def test_kill_de_campanha_gera_kills_dos_criativos_publica_e_so_rd_retoma(db, client, deps, redis):
    import json

    from app.models.devices import CreativeKill
    from app.modules.playback.kills import revoked_creative_ids
    from app.modules.websocket import bus

    t, org, ests, screens, s = await world(db, client)
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    c = await status_of(db, cid)
    crid = (await client.get(f"/api/v1/campaigns/{cid}", headers=s.headers)).json()["creatives"][0]["creative_id"]
    ps = redis.pubsub()
    await ps.psubscribe(bus.pattern())
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    r = await client.post(f"/api/v1/campaigns/{cid}/kill", headers=sa.headers,
                          json={"reason": "Conteúdo denunciado por ticket"})
    assert r.status_code == 200 and r.json()["status"] == "PAUSADA", r.text
    kills = (await db.execute(select(CreativeKill).where(CreativeKill.creative_id == uuid.UUID(crid)))).scalars().all()
    assert {k.screen_id for k in kills} == {sc.id for sc in screens} and all(k.lifted_at is None for k in kills)
    for sc in screens:
        assert uuid.UUID(crid) in await revoked_creative_ids(db, sc.id)  # "revoked" no próximo manifesto
    got = []
    for _ in range(60):
        m = await ps.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if m and m.get("type") == "pmessage":
            got.append((m["channel"], json.loads(m["data"])))
        if len([1 for _, e in got if e["type"] == "kill"]) >= len(screens):
            break
    await ps.aclose()
    kill_msgs = [(ch, e) for ch, e in got if e["type"] == "kill"]
    assert {ch.rsplit(":", 1)[-1] for ch, _ in kill_msgs} == {str(sc.id) for sc in screens}
    assert all(e["data"]["creative_ids"] == [crid] for _, e in kill_msgs)
    # anunciante não retoma campanha interrompida pela RD
    r = await client.post(f"/api/v1/campaigns/{cid}/resume", headers=s.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CAMPAIGN_KILLED"
    r = await client.post(f"/api/v1/campaigns/{cid}/resume", headers=sa.headers)
    assert r.status_code == 200 and r.json()["status"] == c.status
    for sc in screens:
        assert await revoked_creative_ids(db, sc.id) == []
    # pausa simples do cliente não gera kill
    assert await S.mark_syncing(db, c.id) is True
    await db.commit()
    r = await client.post(f"/api/v1/campaigns/{cid}/pause", headers=s.headers, json={"reason": "pausa"})
    assert r.status_code == 200
    n = (await db.execute(select(func.count()).select_from(CreativeKill).where(CreativeKill.lifted_at.is_(None)))
         ).scalar_one()
    assert n == 0
    assert (await client.post(f"/api/v1/campaigns/{cid}/resume", headers=s.headers)).status_code == 200
