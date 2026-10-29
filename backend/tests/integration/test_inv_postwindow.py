"""W-INV — pós-janela: CAMINHO DE PRODUTO (sem dublês do inventário), PostgreSQL real.

(a) REQ-CNT-005: campanha da organização RD com criativo INSTITUTIONAL criado pela equipe RD e
    aprovado pela rota de revisão de conteúdo do W-CAMP (``POST /creatives/{id}/review``);
    checkout REAL (W-CAMP → ``inventory.service.hold``), pagamento REAL (webhook assinado → W-FIN →
    W-CAMP ``on_order_paid`` → ``convert_hold``), aprovação automática → manifesto ASSINADO da tela
    com o institucional no bloco EXTERNO, na quota contratada, sem invadir a quota interna, sem
    prioridade especial (STANDARD; consome a capacidade externa como qualquer booking).
(b) INTERNAL/FALLBACK: criativos criados pela RD, aprovados pela mesma rota, cadastrados pelas
    rotas ``/internal-content`` e ``/internal-content/fallback`` → manifesto assinado.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, update

from app.core.authz import AuthContext
from app.core.settings_service import set_setting
from app.models.inventory import BookingPattern, InventoryHold
from app.models.organizations import Organization
from app.models.pricing import Order
from app.modules.inventory import service as inv
from app.modules.playback import protocol as P
from app.modules.scheduler.resolve import resolve
from tests.camp_support import make_media
from tests.factories import login_as, make_user
from tests.fin_helpers import fin_env, webhook_for  # noqa: F401  (fixture)
from tests.integration.test_camp_lifecycle import checkout, new_campaign, status_of, world

SYS = AuthContext.system("system")
TZ = "America/Sao_Paulo"


async def _paid(db, client, po, event_id: int) -> str:
    from app.models.financial import WebhookEvent
    from app.modules.financial import payments

    raw, headers = webhook_for(po, event_id=event_id, fee_cents=150)
    r = await client.post("/api/v1/webhooks/cloudpaypag", content=raw, headers=headers)
    assert r.status_code == 200, r.text
    ev = (await db.execute(select(WebhookEvent).where(WebhookEvent.webhook_event_id == event_id))).scalar_one()
    st = await payments.process_webhook_event(db, ev.id)
    await db.commit()
    return st


async def _rd_creative(client, sa, org_id, media_id, kind: str, duration: int) -> dict:
    r = await client.post("/api/v1/creatives", headers=sa.headers, json={
        "org_id": str(org_id), "name": f"Peça {kind}", "media_file_id": str(media_id),
        "duration_seconds": duration, "kind": kind})
    assert r.status_code == 201, r.text
    return r.json()


async def _review_ok(client, sa, creative_id) -> None:
    r = await client.post(f"/api/v1/creatives/{creative_id}/review", headers=sa.headers, json={"decision": "APPROVE"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APROVADO"


def _verify(m: dict, sig: str) -> None:
    key = inv.server_public_keys()[0]["public_key"]
    assert P.verify_es256(key, m, sig), "assinatura ES256 do manifesto deve verificar"


def _local_day(bucket_ms: int):
    return datetime.fromtimestamp(bucket_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(TZ)).date()


async def test_rd_institutional_campaign_product_path_lands_in_external_block(db, client, fin_env):
    t, org, ests, screens, s_rd = await world(db, client, n=1)
    scr = screens[0]
    scr_id, org_id = scr.id, org.id
    await db.execute(update(Organization).where(Organization.id == org_id).values(
        name="RD Mídia Indoor (institucional)", email="marketing@rdmidia.com.br", phone="62999990002",
        document=f"{uuid.uuid4().int % 10**14:014d}", document_type="CNPJ"))
    await db.commit()
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)

    # 1) criativo INSTITUTIONAL: só a equipe RD cria; aprovado pela rota de revisão de conteúdo
    media = await make_media(db, org, duration_ms=30000)
    r = await client.post("/api/v1/creatives", headers=s_rd.headers, json={
        "name": "Institucional", "media_file_id": str(media.id), "duration_seconds": 30, "kind": "INSTITUTIONAL"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CREATIVE_KIND_FORBIDDEN"
    inst = await _rd_creative(client, sa, org_id, media.id, "INSTITUTIONAL", 30)
    await _review_ok(client, sa, inst["id"])

    # 2) campanha da organização RD com o institucional (booking NORMAL — ADR_006 §7 item 6)
    today = (await db.execute(text("SELECT (now() AT TIME ZONE :tz)::date"), {"tz": TZ})).scalar_one()
    d1 = today + timedelta(days=1)
    cid = await new_campaign(client, s_rd, with_creative=False, db=db, org=org, start=d1, days=2)
    r = await client.post(f"/api/v1/campaigns/{cid}/creatives", headers=s_rd.headers, json={"creative_id": inst["id"]})
    assert r.status_code == 201, r.text

    # 3) checkout REAL (hold do W-INV) + pagamento REAL (webhook → W-FIN → W-CAMP → convert_hold)
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="institucional RD (teste pós-janela)")
    await db.commit()
    r = await checkout(client, s_rd, cid)
    assert r.status_code == 201, r.text
    order = await db.get(Order, uuid.UUID(r.json()["order_id"]))
    from app.models.financial import PaymentOrder

    po = await db.get(PaymentOrder, order.payment_order_id)
    held = (await db.execute(select(InventoryHold).where(InventoryHold.campaign_id == uuid.UUID(cid)))).scalars().all()
    assert [h.status for h in held] == ["HELD"]
    assert await _paid(db, client, po, 7_700_001) == "PROCESSED"
    db.expire_all()
    holds = (await db.execute(select(InventoryHold.status).where(InventoryHold.campaign_id == uuid.UUID(cid)))).scalars().all()
    assert holds == ["CONVERTED"]
    c = await status_of(db, cid)
    assert c.status in ("APROVADA", "AGENDADA", "SINCRONIZANDO", "ATIVA"), c.status
    # sem prioridade especial: o institucional é STANDARD e respeita gap (2/h, nunca 2 no mesmo bloco)
    bp = (await db.execute(select(BookingPattern).where(BookingPattern.campaign_id == uuid.UUID(cid)))).scalar_one()
    assert (bp.priority, bp.duration_units, bp.freq_kind, bp.freq_value) == ("STANDARD", 1, "PER_HOUR", 2)

    # 4) manifesto ASSINADO da tela (gerado pela invalidação disparada na aprovação)
    sm = await inv.current_manifest(db, scr_id)
    await db.commit()
    m = sm.manifest
    _verify(m, sm.signature)
    keys = [k for k, v in m["creatives"].items() if v["creative_id"] == inst["id"]]
    assert len(keys) == 1  # uma janela de validade (d1–d2) ⇒ uma chave
    for k in keys:
        cr = m["creatives"][k]
        assert (cr["kind"], cr["label"], cr["code"], cr["campaign_id"]) == ("INSTITUTIONAL", "Publicidade",
                                                                             inst["code"], cid)
    rot = m["rotation"]
    per_day: dict = {}
    buckets: dict = {}
    for b, entries in m["external_plan"].items():
        for e in entries:
            # quota interna NUNCA invadida: toda entrada cabe no bloco EXTERNO
            assert e["u"] * rot["unit_ms"] + e["d"] <= rot["external_ms"]
            if e["c"] in keys:
                day = _local_day(int(b))
                per_day[day] = per_day.get(day, 0) + 1
                buckets.setdefault(b, 0)
                buckets[b] += 1
    assert per_day == {d1: 48, d1 + timedelta(days=1): 48}, per_day  # 2/h × 24 h × 2 dias contratados
    assert max(buckets.values()) == 1, "mesma linha nunca duas vezes no mesmo bloco"
    for blk in m["internal_pattern"]:
        assert sum(x["d"] for x in blk) <= rot["internal_ms"]

    # 5) execução de referência: o institucional toca SÓ em bloco EXTERNO; a fase interna segue interna
    anchor = int(datetime.combine(d1, datetime.min.time()).replace(tzinfo=ZoneInfo(TZ)).timestamp() * 1000) + 7000
    ready = {"revoked": [], "media_ready": list(m["media"])}
    seen_ext = 0
    for k in range(15 * 6):  # 6 horas de ciclos
        start = anchor + k * rot["cycle_ms"]
        seg_int = resolve(m, anchor, ready, start + 1000)
        assert seg_int.block == "INTERNAL" and seg_int.creative_key not in keys
        s0 = start + rot["internal_ms"]
        for u in range(rot["external_ms"] // rot["unit_ms"]):
            seg = resolve(m, anchor, ready, s0 + u * rot["unit_ms"])
            if seg.creative_key in keys:
                assert seg.block == "EXTERNAL" and seg.kind == "ITEM"
                seen_ext += 1
    assert seen_ext == 12  # 2/h × 6 h

    # 6) consome a capacidade externa como qualquer booking (sem reserva extra nem prioridade máxima):
    #    2 blocos/h têm 1 unidade ocupada ⇒ cabem 13 anúncios de 120 s/h, não 14
    q14 = await inv.quote_capacity(db, SYS, [scr_id], [{"date_from": d1.isoformat(), "date_to": d1.isoformat(),
                                                         "duration_units": 4, "freq_kind": "PER_HOUR",
                                                         "freq_value": 14}])
    q13 = await inv.quote_capacity(db, SYS, [scr_id], [{"date_from": d1.isoformat(), "date_to": d1.isoformat(),
                                                         "duration_units": 4, "freq_kind": "PER_HOUR",
                                                         "freq_value": 13}])
    assert not q14.feasible and q13.feasible


async def test_internal_and_fallback_product_path(db, client, fin_env):
    t, org, ests, screens, s = await world(db, client, n=1)
    est, scr = ests[0], screens[0]
    est_id, est_org, scr_id = est.id, est.org_id, scr.id
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    owner = SimpleNamespace(id=est_org)
    m_int = await make_media(db, owner, duration_ms=60000)
    m_fb = await make_media(db, owner, duration_ms=30000)
    internal = await _rd_creative(client, sa, est_org, m_int.id, "INTERNAL", 60)
    fallback = await _rd_creative(client, sa, est_org, m_fb.id, "FALLBACK", 30)

    # não aprovado → W-INV recusa pela rota de produto
    r = await client.post("/api/v1/internal-content", headers=sa.headers,
                          json={"establishment_id": str(est_id), "creative_id": internal["id"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CREATIVE_NOT_APPROVED"
    await _review_ok(client, sa, internal["id"])
    await _review_ok(client, sa, fallback["id"])
    r = await client.post("/api/v1/internal-content", headers=sa.headers,
                          json={"establishment_id": str(est_id), "creative_id": internal["id"]})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/internal-content/fallback", headers=sa.headers, json={
        "scope": "ESTABLISHMENT", "establishment_id": str(est_id), "creative_id": fallback["id"],
        "purpose": "ESTABLISHMENT_LOGO"})
    assert r.status_code == 201, r.text
    # tipos trocados são recusados (INTERNAL não vira fallback e vice-versa)
    r = await client.post("/api/v1/internal-content", headers=sa.headers,
                          json={"establishment_id": str(est_id), "creative_id": fallback["id"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "CREATIVE_KIND_INVALID"

    sm = await inv.current_manifest(db, scr_id)
    await db.commit()
    m = sm.manifest
    _verify(m, sm.signature)
    by_id = {v["creative_id"]: (k, v) for k, v in m["creatives"].items()}
    ki, ci = by_id[internal["id"]]
    kf, cf = by_id[fallback["id"]]
    assert (ci["kind"], ci["label"], ci["code"]) == ("INTERNAL", "Conteúdo do estabelecimento", internal["code"])
    assert (cf["kind"], cf["label"], cf["code"]) == ("FALLBACK", "RD Mídia Indoor", fallback["code"])
    # peça de 1 min repete dentro do bloco interno de 2 min; fallback do estabelecimento listado
    assert m["internal_pattern"] == [[{"c": ki, "d": 60000}, {"c": ki, "d": 60000}]]
    assert m["fallback"] == [{"c": kf, "d": 30000}]
    # sem anúncios vendidos: as unidades externas voltam ao estabelecimento (interno), sem cortar
    rot = m["rotation"]
    assert m["external_plan"]
    for entries in m["external_plan"].values():
        assert [e["c"] for e in entries] == [ki, ki]
        assert all(e["u"] * rot["unit_ms"] + e["d"] <= rot["external_ms"] for e in entries)
    # execução de referência: interno no bloco interno; mídia ausente ⇒ fallback do estabelecimento
    anchor = int(sm.generated_at.timestamp()) * 1000
    full = {"revoked": [], "media_ready": list(m["media"])}
    assert resolve(m, anchor, full, anchor + 1000).creative_key == ki
    no_int = {"revoked": [], "media_ready": [cf["media"]]}
    seg = resolve(m, anchor, no_int, anchor + 1000)
    assert (seg.block, seg.kind, seg.creative_key) == ("INTERNAL", "ITEM", kf)
