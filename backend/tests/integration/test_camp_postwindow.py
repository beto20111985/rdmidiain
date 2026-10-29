"""Correções pós-janela (W-CAMP): (b) revisão de conteúdo RD de criativos INTERNAL/INSTITUTIONAL/FALLBACK
→ add_internal/add_fallback do W-INV; (c) REPRODUZINDO em lote na listagem (sem N+1). PostgreSQL real."""
import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event, select, text

from app.core.authz import AuthContext
from app.core.settings_service import set_setting
from app.core.timeutils import utcnow
from app.models.campaigns import Creative
from app.modules.campaigns import creatives as CR
from tests.camp_support import FakeDeps, make_est, make_media
from tests.factories import login_as, make_org, make_user
from tests.integration.test_camp_lifecycle import checkout, new_campaign, pay, status_of, world

SYS = AuthContext.system("system")
CODE_RE = re.compile(r"^RD-[ABCDEFGHJMNPQRSTUVYZ23456789]{6}$")


async def _internal_creative(db, org, *, kind="INTERNAL", media_status="READY", duration_ms=30000):
    m = await make_media(db, org, status=media_status, duration_ms=duration_ms)
    c = Creative(id=uuid.uuid4(), org_id=org.id, name=f"Peça {kind}", kind=kind, media_file_id=m.id,
                 duration_seconds=30, orientation="HORIZONTAL", status="RASCUNHO", technical_status="PENDENTE",
                 version=1, code="RD-ZZZZZZ")
    await CR.insert_with_unique_code(db, c)
    await db.commit()
    return c, m


async def test_revisao_rd_aprovar_rejeitar_permissao_estado_e_caminho_ate_internal_fallback(db, client):
    org = await make_org(db)
    est = await make_est(db, org)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s_adv = await login_as(client, adv)
    emp = await make_user(db, role="employee")  # equipe RD sem campaigns.approve
    s_emp = await login_as(client, emp)
    internal, _ = await _internal_creative(db, org)
    fallback, _ = await _internal_creative(db, org, kind="FALLBACK")
    rejectme, _ = await _internal_creative(db, org, kind="INSTITUTIONAL")
    internal_id, internal_code, fallback_id, admin_id, est_id = internal.id, internal.code, fallback.id, admin.id, est.id

    url = f"/api/v1/creatives/{internal.id}/review"
    # sem permissão → 403 (anunciante e funcionário); inexistente → 404; corpo inválido → 422
    assert (await client.post(url, headers=s_adv.headers, json={"decision": "APPROVE"})).status_code == 403
    assert (await client.post(url, headers=s_emp.headers, json={"decision": "APPROVE"})).status_code == 403
    r = await client.post(f"/api/v1/creatives/{uuid.uuid4()}/review", headers=sa.headers, json={"decision": "APPROVE"})
    assert r.status_code == 404
    assert (await client.post(url, headers=sa.headers, json={"decision": "MAYBE"})).status_code == 422

    # W-INV recusa criativo não aprovado (o contorno antigo era marcar à mão)
    from app.core.errors import DomainError
    from app.modules.inventory import content as C

    with pytest.raises(DomainError) as e:
        await C.add_internal(db, SYS, establishment_id=est_id, creative_id=internal_id)
    assert e.value.code == "CREATIVE_NOT_APPROVED"
    await db.rollback()

    # aprovar
    r = await client.post(url, headers=sa.headers, json={"decision": "APPROVE"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APROVADO" and r.json()["code"] == internal_code and CODE_RE.match(internal_code)
    # estado não revisável → 409 (aprovado não volta a ser revisado)
    r = await client.post(url, headers=sa.headers, json={"decision": "REJECT", "reason": "Mudei de ideia agora"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CREATIVE_NOT_REVIEWABLE"
    assert (await client.post(f"/api/v1/creatives/{fallback.id}/review", headers=sa.headers,
                              json={"decision": "APPROVE"})).status_code == 200

    # rejeição exige motivo; com motivo → REJEITADO e não revisável depois
    rurl = f"/api/v1/creatives/{rejectme.id}/review"
    r = await client.post(rurl, headers=sa.headers, json={"decision": "REJECT"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "REJECTION_REASON_REQUIRED"
    r = await client.post(rurl, headers=sa.headers, json={"decision": "REJECT", "reason": "Logo fora do padrão da RD"})
    assert r.status_code == 200 and r.json()["status"] == "REJEITADO"
    assert r.json()["rejection_reason"] == "Logo fora do padrão da RD"
    assert (await client.post(rurl, headers=sa.headers, json={"decision": "APPROVE"})).status_code == 409

    # auditoria append-only com ator
    rows = (await db.execute(text("SELECT action, actor_user_id, reason FROM audit_logs WHERE entity_type = 'creative' "
                                  "AND action IN ('creative.approved','creative.rejected') ORDER BY seq"))).all()
    assert [r.action for r in rows] == ["creative.approved", "creative.approved", "creative.rejected"]
    assert all(r.actor_user_id == admin_id for r in rows)
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        await db.execute(text("DELETE FROM audit_logs WHERE action = 'creative.approved'"))
    await db.rollback()

    # caminho completo: aprovados entram no conteúdo interno e no fallback do W-INV
    row = await C.add_internal(db, SYS, establishment_id=est_id, creative_id=internal_id)
    fb = await C.add_fallback(db, SYS, scope="ESTABLISHMENT", establishment_id=est_id, creative_id=fallback_id,
                              purpose="RD_DEFAULT")
    assert row.creative_id == internal_id and fb.creative_id == fallback_id
    await db.commit()
    # o código RD continua imutável após a revisão
    with pytest.raises(DBAPIError):
        await db.execute(text("UPDATE creatives SET code = 'RD-AAAAAA' WHERE id = :i"), {"i": internal_id})
    await db.rollback()


async def test_revisao_exige_midia_pronta_e_externo_vai_pela_campanha(db, client):
    org = await make_org(db)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    pending, m = await _internal_creative(db, org, media_status="PROCESSING")
    r = await client.post(f"/api/v1/creatives/{pending.id}/review", headers=sa.headers, json={"decision": "APPROVE"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CREATIVE_NOT_READY"
    # rejeitar não depende da mídia
    r = await client.post(f"/api/v1/creatives/{pending.id}/review", headers=sa.headers,
                          json={"decision": "REJECT", "reason": "Mídia corrompida no envio"})
    assert r.status_code == 200 and r.json()["status"] == "REJEITADO"
    # vídeo com duração fora da tolerância não é aprovado
    bad, _ = await _internal_creative(db, org, duration_ms=31200)
    r = await client.post(f"/api/v1/creatives/{bad.id}/review", headers=sa.headers, json={"decision": "APPROVE"})
    assert r.status_code == 409 and r.json()["error"]["details"]["problem"] == "DURATION_MISMATCH"
    # EXTERNAL é revisado na campanha
    m2 = await make_media(db, org)
    ext = await CR.create_creative(db, SYS, org_id=org.id, name="ext", media_file_id=m2.id, duration_seconds=30)
    await db.commit()
    r = await client.post(f"/api/v1/creatives/{ext.id}/review", headers=sa.headers, json={"decision": "APPROVE"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CREATIVE_REVIEW_VIA_CAMPAIGN"


# ─── (c) REPRODUZINDO em lote ────────────────────────────────────────────────

@pytest.fixture
def deps(monkeypatch):
    return FakeDeps().install(monkeypatch)


async def _active_campaign(db, client, s, org, deps, screens):
    from app.modules.campaigns import service as S

    cid = await new_campaign(client, s, db=db, org=org)
    await pay(db, (await checkout(client, s, cid)).json()["order_id"], deps)
    c = await status_of(db, cid)
    await S.mark_syncing(db, c.id)
    c.start_date = c.start_date - timedelta(days=30)
    await db.commit()
    assert await S.mark_active_from_ack(db, [c.id], screen_id=screens[0].id, manifest_version=1) == [c.id]
    await db.commit()
    return c


def _count_sql(engine):
    """Conta os statements da requisição (ignora o despacho assíncrono de notificações de outros testes/commits)."""
    stmts = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if "notification" not in statement:
            stmts.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    return stmts, lambda: event.remove(engine.sync_engine, "before_cursor_execute", before)


async def test_listagem_mostra_reproduzindo_em_lote_coerente_com_detalhe(db, client, deps):
    from app.core.database import get_engine
    from tests.integration.test_biz_reports import _device, _ev

    t, org, ests, screens, s = await world(db, client)
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    playing = await _active_campaign(db, client, s, org, deps, screens)
    idle = await _active_campaign(db, client, s, org, deps, screens)
    draft = await new_campaign(client, s, db=db, org=org)
    dev = await _device(db, screens[0], online=True)
    cr = (await db.execute(select(Creative).where(Creative.org_id == org.id))).scalars().first()
    db.add(_ev(dev, screens[0], ests[0], creative=cr, campaign=playing, when=utcnow() - timedelta(minutes=2)))
    db.add(_ev(dev, screens[0], ests[0], creative=cr, campaign=idle, when=utcnow() - timedelta(hours=3)))  # antigo
    db.add(_ev(dev, screens[0], ests[0], creative=cr, campaign=idle, when=utcnow() - timedelta(minutes=1),
               valid=False, reason="INCOMPLETE_PLAYBACK", ms=20000))  # recente porém inválido
    await db.commit()

    stmts, stop = _count_sql(get_engine())
    try:
        r = await client.get("/api/v1/campaigns?page_size=100", headers=s.headers)
    finally:
        stop()
    assert r.status_code == 200
    items = {i["id"]: i for i in r.json()["items"]}
    assert items[str(playing.id)]["is_reproducing"] is True
    assert items[str(playing.id)]["display_status"] == "REPRODUZINDO" and items[str(playing.id)]["status"] == "ATIVA"
    assert items[str(idle.id)]["is_reproducing"] is False and items[str(idle.id)]["display_status"] == "ATIVA"
    assert items[draft]["is_reproducing"] is False and items[draft]["display_status"] == "RASCUNHO"
    pop_queries = [x for x in stmts if "playback_events" in x]
    assert len(pop_queries) == 1, pop_queries  # UMA consulta de PoP para a página inteira
    n_small = len(stmts)
    # coerência com o detalhe
    for cid in (playing.id, idle.id):
        d = (await client.get(f"/api/v1/campaigns/{cid}", headers=s.headers)).json()
        assert d["is_reproducing"] == items[str(cid)]["is_reproducing"]
        assert d["display_status"] == items[str(cid)]["display_status"]
        assert d["reproducing"]["playing"] == items[str(cid)]["is_reproducing"]
    # mais campanhas na página ⇒ mesmo número de consultas (sem N+1)
    for _ in range(3):
        await _active_campaign(db, client, s, org, deps, screens)
    stmts2, stop2 = _count_sql(get_engine())
    try:
        r = await client.get("/api/v1/campaigns?page_size=100", headers=s.headers)
    finally:
        stop2()
    assert r.json()["total"] == 6
    assert len([x for x in stmts2 if "playback_events" in x]) == 1
    assert len(stmts2) == n_small, (len(stmts2), n_small)
