"""
B (P002 item 51) — Dois checkouts SIMULTÂNEOS de anunciantes DIFERENTES (duas sessões HTTP reais) sobre o
MESMO inventário esgotável, no stack vivo: exatamente um vence; o outro recebe 409 com motivo; nenhuma
sobre-reserva (ocupação conferida pela API e pelo banco).

Inventário: 1 tela dedicada (estabelecimento QA em Porto Velho-RO). Cada campanha usa criativo de 120 s
(ocupa o bloco externo inteiro da política 120/120 → 15 blocos/h) a 8 inserções/h: uma cabe (8 ≤ 15),
as duas não (16 > 15).

O 2º anunciante é criado de verdade: organização ADVERTISER + convite por e-mail (Mailpit) + aceite.
Pulado sem ``RD_E2E_BASE_URL``.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.e2e import live_support as L

pytestmark = [pytest.mark.e2e]

async def test_two_simultaneous_checkouts_same_inventory():
    from app.modules.organizations.documents import make_cnpj

    log = L.StepLog("B51_checkout_concurrency")
    tag = L.run_tag()
    adm = await L.login("admin")
    adv1 = adv2 = None
    try:
        t0 = time.monotonic()
        # higiene: telas QA de execuções anteriores em Porto Velho saem do inventário (a cidade deve ter 1 tela)
        for (sid,) in L.q("select s.id::text from screens s join establishments e on e.id=s.establishment_id "
                          "where e.city_key='RO:porto-velho' and s.name like :p and s.status <> 'DECOMMISSIONED'",
                          p=f"{L.PREFIX}%"):
            await adm.post(f"/api/v1/screens/{sid}/decommission", json={"reason": "QA AÇÃO 002: limpeza de execução anterior"})
        # inventário dedicado: org parceira + estabelecimento (contrato aceito) + 1 tela
        r = await adm.post("/api/v1/organizations", json={
            "name": f"{L.PREFIX}parceiroB_{tag}", "document": make_cnpj(("Q" + uuid.uuid4().hex.upper())[:12]),
            "document_type": "CNPJ", "business_roles": ["PARTNER"], "city": "Porto Velho", "state": "RO",
            "email": f"qa002.pb.{tag}@example.com", "phone": "69999990000"})
        assert r.status_code == 201, r.text
        porg = r.json()["id"]
        r = await adm.post("/api/v1/establishments", json={
            "org_id": porg, "name": f"{L.PREFIX}estabB_{tag}", "city": "Porto Velho", "state": "RO",
            "postal_code": "76801000", "lat": -8.7612, "lng": -63.9004, "geo_source": "MANUAL", "receives_commission": True,
            "email": f"qa002.eb.{tag}@example.com", "phone": "69999990001"})
        assert r.status_code == 201, r.text
        est = r.json()["id"]
        tmpl = (await adm.get("/api/v1/contracts/templates/current", params={"kind": "PARTNERSHIP"})).json()["id"]
        r = await adm.post("/api/v1/contracts/acceptances/manual", json={
            "template_id": tmpl, "org_id": porg, "establishment_id": est, "signer_name": f"{L.PREFIX}signatario",
            "notes": "Aceite registrado pelo QA da AÇÃO 002 (teste de concorrência)."})
        assert r.status_code in (200, 201), r.text
        r = await adm.post("/api/v1/screens", json={"establishment_id": est, "name": f"{L.PREFIX}telaB_{tag}",
                                                     "orientation": "HORIZONTAL", "inches": 43,
                                                     "resolution_w": 1920, "resolution_h": 1080})
        assert r.status_code == 201, r.text
        screen = r.json()["id"]
        log.created.update(partner_org=porg, establishment=est, screen=screen)
        # dois anunciantes de organizações DIFERENTES (cadastro de pagador completo; usuários por convite real)
        adv1_org, c1 = await L.new_advertiser(adm, tag + "a", city="Porto Velho", uf="RO")
        adv2_org, c2 = await L.new_advertiser(adm, tag + "b", city="Porto Velho", uf="RO")
        adv1 = await L.login("advertiser", creds=c1)
        adv2 = await L.login("advertiser", creds=c2)
        log.created.update(advertiser1_org=adv1_org, advertiser2_org=adv2_org)
        log.record("setup", "inventário dedicado + 2 anunciantes (convite Mailpit)", True, t0,
                   {"screen": screen, "establishment": est, "adv1_org": adv1_org, "adv2_org": adv2_org})

        tz = ZoneInfo("America/Porto_Velho")
        start = datetime.now(tz).date() + timedelta(days=1)
        camps = {}
        for name, sess, org in (("A", adv1, adv1_org), ("B", adv2, adv2_org)):
            r = await sess.post("/api/v1/campaigns", json={
                "org_id": org, "name": f"{L.PREFIX}concorrencia{name}_{tag}", "period_type": "AVULSO",
                "start_date": start.isoformat(), "end_date": start.isoformat(), "duration_s": 120,
                "freq_kind": "PER_HOUR", "freq_value": 8,
                "targets": [{"type": "CITY", "value": "RO:Porto Velho", "exclude": False}]})
            assert r.status_code == 201, r.text
            camps[name] = r.json()["id"]
            q = await sess.post(f"/api/v1/campaigns/{camps[name]}/quote", json={"method": "pix"})
            assert q.status_code == 200, q.text
            assert q.json()["availability"]["by_city"][0]["screens"] == 1, q.text
        log.created["campaigns"] = camps

        # checkouts SIMULTÂNEOS (duas sessões, duas organizações)
        t1 = time.monotonic()
        ra, rb = await asyncio.gather(
            adv1.post(f"/api/v1/campaigns/{camps['A']}/checkout", json={"method": "pix"},
                      headers={"Idempotency-Key": str(uuid.uuid4())}),
            adv2.post(f"/api/v1/campaigns/{camps['B']}/checkout", json={"method": "pix"},
                      headers={"Idempotency-Key": str(uuid.uuid4())}))
        codes = sorted([ra.status_code, rb.status_code])
        loser = ra if ra.status_code != 201 else rb
        winner_name = "A" if ra.status_code == 201 else "B"
        ev = {"status_A": ra.status_code, "status_B": rb.status_code, "winner": winner_name,
              "loser_error": (loser.json().get("error") if loser.status_code != 201 else None)}
        holds = L.q("select h.campaign_id::text, h.status, h.screens_held from inventory_holds h "
                    "where h.campaign_id in (:a,:b) order by created_at", a=camps["A"], b=camps["B"])
        ev["holds"] = holds
        occ = await adm.get("/api/v1/inventory/availability",
                            params={"date_from": start.isoformat(), "date_to": start.isoformat(), "establishment_id": est})
        ev["availability_http"] = occ.status_code
        ev["availability"] = occ.json() if occ.status_code == 200 else occ.text[:500]
        bs = L.q("select campaign_id::text, status from booking_screens where screen_id=:s", s=screen)
        ev["booking_screens"] = bs
        ok = (codes == [201, 409] and len([h for h in holds if h[1] == "HELD"]) == 1
              and len([b for b in bs if b[1] in ("HELD", "BOOKED")]) == 1)
        log.record("B51", "dois checkouts simultâneos: 1×201 + 1×409, sem sobre-reserva", ok, t1, ev)
        assert codes == [201, 409], ev
        assert ev["loser_error"] and ev["loser_error"].get("code"), ev

        # limpeza: cancela o pedido vencedor (libera a reserva)
        win = ra if ra.status_code == 201 else rb
        sess = adv1 if winner_name == "A" else adv2
        rc = await sess.post(f"/api/v1/checkout/orders/{win.json()['order_id']}/cancel")
        hs = L.q("select status, release_reason from inventory_holds where campaign_id=:c", c=camps[winner_name])
        log.record("cleanup", "cancelar pedido vencedor (libera reserva)", rc.status_code in (200, 204), time.monotonic(),
                   {"cancel_http": rc.status_code, "holds": hs})
        assert ok, ev
    finally:
        print(f"[B51] relatório: {log.dump()}", flush=True)
        for c in (adm, adv1, adv2):
            if c is not None:
                await c.aclose()
