"""
A2 (REQ-CRM-005, CRÍTICO) — CRM ponta a ponta SEM DUBLÊS contra o stack vivo:
lead → plano PERSONALIZADO (criado pelo comercial; aprovação do admin, E2#1023) → campanha do cliente com o
plano → proposta (desconto acima do limite → aprovação; envio por e-mail com PDF; aceite do cliente) →
conversão em pedido (checkout real do W-CAMP: preço do servidor + desconto comercial + hold REAL de 2 h +
cobrança) → pagamento pelo webhook ASSINADO do provedor FALSO (worker real) → campanha EM_ANALISE na fila de
aprovação RD → atribuição/comissão do vendedor registrada.
Continua com a RENOVAÇÃO (campanha MENSAL aprovada): proposta de renovação → checkout da renovação (hold) →
pagamento → extensão sem reaprovação.

Pulado sem ``RD_E2E_BASE_URL``.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.e2e import live_support as L

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

CITY, UF, TZ = "Palmas", "TO", "America/Araguaina"


async def test_crm_lead_to_paid_campaign_and_renewal():  # noqa: C901
    from tests.e2e.test_e2e_full_flow import _make_png

    log = L.StepLog("A2_crm_flow")
    tag = L.run_tag()
    adm = await L.login("admin")
    sales = await L.login("sales")
    fin = await L.login("financial")
    adv = None
    ctx: dict = {}

    def step(n, title):
        class _S:
            def __enter__(s):
                s.t0, s.ev = time.monotonic(), {}
                return s

            def __exit__(s, et, e, tb):
                if et is not None:
                    s.ev["error"] = f"{et.__name__}: {str(e)[:700]}"
                log.record(n, title, et is None, s.t0, s.ev)
                return False
        return _S()

    try:
        with step("A2.0", "inventário dedicado (Palmas-TO) + cliente anunciante (convite Mailpit)") as s:
            r = await adm.post("/api/v1/organizations", json={
                "name": f"{L.PREFIX}parceiroCRM_{tag}", "document": L.valid_cnpj(True), "document_type": "CNPJ",
                "business_roles": ["PARTNER"], "city": CITY, "state": UF, "email": f"qa002.pcrm.{tag}@example.com",
                "phone": "63999990000"})
            assert r.status_code == 201, r.text
            porg = r.json()["id"]
            r = await adm.post("/api/v1/establishments", json={
                "org_id": porg, "name": f"{L.PREFIX}estabCRM_{tag}", "city": CITY, "state": UF, "postal_code": "77001000",
                "lat": -10.1840, "lng": -48.3336, "geo_source": "MANUAL", "receives_commission": True,
                "email": f"qa002.ecrm.{tag}@example.com", "phone": "63999990001"})
            assert r.status_code == 201, r.text
            est = r.json()["id"]
            tz = L.q1("select timezone from establishments where id=:i", i=est)[0]
            tmpl = (await adm.get("/api/v1/contracts/templates/current", params={"kind": "PARTNERSHIP"})).json()["id"]
            r = await adm.post("/api/v1/contracts/acceptances/manual", json={
                "template_id": tmpl, "org_id": porg, "establishment_id": est, "signer_name": f"{L.PREFIX}signatario",
                "notes": "Aceite registrado pelo QA da AÇÃO 002 (E2E CRM)."})
            assert r.status_code in (200, 201), r.text
            r = await adm.post("/api/v1/screens", json={"establishment_id": est, "name": f"{L.PREFIX}telaCRM_{tag}",
                                                         "orientation": "HORIZONTAL", "inches": 43,
                                                         "resolution_w": 1920, "resolution_h": 1080})
            assert r.status_code == 201, r.text
            screen = r.json()["id"]
            client_org, creds = await L.new_advertiser(adm, tag, city=CITY, uf=UF)
            adv = await L.login("advertiser", creds=creds)
            ctx.update(porg=porg, est=est, screen=screen, client_org=client_org)
            log.created.update(partner_org=porg, establishment=est, screen=screen, client_org=client_org)
            s.ev.update(establishment_tz=tz, screen=screen, client_org=client_org)
            assert tz == TZ

        with step("A2.1", "vendedor na estrutura comercial (% próprio exige commissions.adjust → 403 ao admin)") as s:
            sales_id = sales.user["id"]
            r = await adm.put("/api/v1/crm/sales-reps", json={"user_id": sales_id, "kind": "VENDEDOR", "uf": UF,
                                                              "city": CITY, "compensation": "COMISSAO"})
            s.ev["rep_upsert_admin"] = r.status_code
            assert r.status_code == 200, r.text
            r = await adm.put("/api/v1/crm/sales-reps", json={"user_id": sales_id, "kind": "VENDEDOR", "uf": UF,
                                                              "city": CITY, "compensation": "COMISSAO", "commission_bps": 300})
            s.ev["rep_bps_by_admin"] = (r.status_code, L.err_code(r))
            assert r.status_code == 403  # % do vendedor exige commissions.adjust (contas de teste não têm crm.manage+commissions.adjust)
            rep = L.q1("select commission_bps, kind, is_active, id::text from sales_reps where user_id=:u", u=sales_id)
            aud = L.q("select action, origin, actor_user_id is null from audit_logs where entity_id=:i order by created_at",
                      i=rep[3])
            assert rep[2] is True, rep
            ctx["sales_id"] = sales_id
            ctx["rep_bps"] = rep[0]
            s.ev.update(rep_commission_bps=rep[0], audit=aud,
                        note="% próprio do vendedor NÃO configurado: exige crm.manage+commissions.adjust (Admin Master); "
                             "setup in-process recusado pela política de permissões do ambiente do QA")

        with step("A2.2", "lead criado e qualificado pelo comercial") as s:
            r = await sales.post("/api/v1/crm/leads", json={
                "name": f"{L.PREFIX}lead_{tag}", "company": f"{L.PREFIX}empresa", "email": f"qa002.lead.{tag}@example.com",
                "phone": "63999991111", "city": CITY, "uf": UF, "org_id": client_org, "interest": "ANUNCIANTE",
                "estimated_value_cents": 500000, "notes": "QA AÇÃO 002"})
            assert r.status_code == 201, r.text
            lead = r.json()
            r = await sales.post(f"/api/v1/crm/leads/{lead['id']}/stage", json={"stage": "QUALIFICADO"})
            assert r.status_code == 200, r.text
            ctx["lead"] = lead["id"]
            log.created["lead"] = lead["id"]
            s.ev.update(lead=lead["id"], stage=r.json().get("stage"), owner=lead.get("owner_name"))

        with step("A2.3", "plano PERSONALIZADO: criado pelo comercial, aprovado pelo admin (#1023)") as s:
            r = await sales.post("/api/v1/pricing/plans", json={
                "name": f"{L.PREFIX}plano_custom_{tag}", "description": "Plano personalizado QA AÇÃO 002",
                "kind": "CUSTOM", "org_id": client_org, "period_type": "MENSAL", "duration_s": 30, "freq_kind": "PER_HOUR",
                "freq_value": 4, "screens_min": 1, "screens_max": 5, "discount_bps": 500})
            assert r.status_code == 201, r.text
            plan = r.json()
            assert plan["status"] == "PENDENTE_APROVACAO", plan
            r_self = await sales.post(f"/api/v1/pricing/plans/{plan['id']}/approve")
            assert r_self.status_code == 403, r_self.text
            r = await adm.post(f"/api/v1/pricing/plans/{plan['id']}/approve")
            assert r.status_code == 200, r.text
            row = L.q1("select status, approved_by::text, created_by::text, discount_bps, margin_status from plans where id=:i",
                       i=plan["id"])
            assert row[0] == "ATIVO" and row[1] == adm.user["id"] and row[2] == ctx["sales_id"], row
            ctx["plan"] = plan["id"]
            log.created["plan"] = plan["id"]
            s.ev.update(plan=plan["id"], status_created=plan["status"], seller_self_approve=r_self.status_code,
                        status=row[0], approved_by_admin=True, discount_bps=row[3], margin_status=row[4])

        with step("A2.4", "campanha do cliente com o plano personalizado (criativo de imagem READY pelo worker)") as s:
            r = await adv.post("/api/v1/media/upload", data={"org_id": client_org, "name": f"{L.PREFIX}img_crm_{tag}"},
                               files={"file": ("qa_crm.png", _make_png(tag), "image/png")})
            assert r.status_code in (200, 202), r.text
            mid = r.json()["id"]

            async def _ready():
                st = L.q1("select status from media_files where id=:i", i=mid)[0]
                if st in ("REJECTED", "FAILED"):
                    raise L.E2EError(st)
                return st == "READY"
            await L.wait_until(_ready, timeout=300, interval=2, what="imagem READY")
            r = await adv.post("/api/v1/creatives", json={"org_id": client_org, "name": f"{L.PREFIX}criativo_crm_{tag}",
                                                          "media_file_id": mid, "duration_seconds": 30, "kind": "EXTERNAL"})
            assert r.status_code == 201, r.text
            cr = r.json()["id"]
            today = datetime.now(ZoneInfo(TZ)).date()
            r = await adv.post("/api/v1/campaigns", json={
                "org_id": client_org, "name": f"{L.PREFIX}campanha_crm_{tag}", "plan_id": ctx["plan"],
                "period_type": "MENSAL", "start_date": today.isoformat(), "duration_s": 30, "freq_kind": "PER_HOUR",
                "freq_value": 4, "targets": [{"type": "CITY", "value": f"{UF}:{CITY}", "exclude": False}]})
            assert r.status_code == 201, r.text
            camp = r.json()
            cid = camp["id"]
            r = await adv.post(f"/api/v1/campaigns/{cid}/creatives", json={"creative_id": cr})
            assert r.status_code in (200, 201), r.text
            q = await adv.post(f"/api/v1/campaigns/{cid}/quote", json={"method": "pix"})
            assert q.status_code == 200, q.text
            ctx.update(cid=cid, quote_total=q.json()["quote"]["total_cents"], media=mid, creative=cr)
            log.created.update(campaign=cid, creative=cr, media=mid)
            row = L.q1("select status, plan_id::text, start_date::text, end_date::text, period_type, auto_renew from campaigns "
                       "where id=:i", i=cid)
            assert row[1] == ctx["plan"], row
            s.ev.update(campaign=cid, status=row[0], plan_id_ok=True, start=row[2], end=row[3], period=row[4],
                        auto_renew=row[5], quote_total_cents=ctx["quote_total"],
                        quote_discount_cents=q.json()["quote"].get("discount_cents"))

        with step("A2.5", "proposta: desconto acima do limite → aprovação (≠ vendedor) → envio com PDF → aceite") as s:
            r = await sales.post("/api/v1/crm/proposals", json={
                "title": f"{L.PREFIX}proposta_{tag}", "lead_id": ctx["lead"], "discount_bps": 1200,
                "discount_justification": "Volume anual (teste QA)",
                "items": [{"package_code": "PLANO-CUSTOM", "description": "Plano personalizado mensal (QA)", "quantity": 1,
                           "unit_price_cents": ctx["quote_total"], "details": {"plan_id": ctx["plan"]}}]})
            assert r.status_code == 201, r.text
            prop = r.json()
            pid = prop["id"]
            ctx["proposal"] = pid
            log.created["proposal"] = pid
            r = await sales.post(f"/api/v1/crm/proposals/{pid}/submit")
            assert r.status_code == 200, r.text
            st_sub = r.json()["status"]
            r_self = await sales.post(f"/api/v1/crm/proposals/{pid}/discount-decision", json={"approve": True})
            r_send_early = await sales.post(f"/api/v1/crm/proposals/{pid}/send", json={})
            r = await adm.post(f"/api/v1/crm/proposals/{pid}/discount-decision", json={"approve": True, "reason": "QA"})
            assert r.status_code == 200, r.text
            r = await sales.post(f"/api/v1/crm/proposals/{pid}/send", json={})
            assert r.status_code == 200, r.text
            sent = r.json()
            import httpx

            async with httpx.AsyncClient(base_url=L.MAILPIT_URL, timeout=20) as mp:
                async def _mail():
                    rr = await mp.get("/api/v1/search", params={"query": f'to:"qa002.lead.{tag}@example.com"'})
                    m = rr.json().get("messages") or []
                    return m[0] if m else None
                mail = await L.wait_until(_mail, timeout=60, interval=1, what="e-mail da proposta")
                full = (await mp.get(f"/api/v1/message/{mail['ID']}")).json()
            atts = [a.get("ContentType") for a in (full.get("Attachments") or [])]
            r_pdf = await sales.get(f"/api/v1/crm/proposals/{pid}/pdf")
            r_conv_early = await sales.post(f"/api/v1/crm/proposals/{pid}/convert", json={"campaign_ids": [ctx["cid"]]},
                                            headers={"Idempotency-Key": f"qa-early-{tag}"})
            r = await sales.post(f"/api/v1/crm/proposals/{pid}/client-decision",
                                 json={"accepted": True, "note": "Aceite do cliente (teste QA)"})
            assert r.status_code == 200, r.text
            s.ev.update(proposal=pid, status_after_submit=st_sub, seller_self_approve=(r_self.status_code, L.err_code(r_self)),
                        send_before_approval=(r_send_early.status_code, L.err_code(r_send_early)),
                        sent_status=sent.get("status"), expires_at=sent.get("expires_at"),
                        email_attachments=atts, pdf=(r_pdf.status_code, r_pdf.content[:5] == b"%PDF-"),
                        convert_before_accept=(r_conv_early.status_code, L.err_code(r_conv_early)),
                        client_decision=r.json().get("status"))
            assert st_sub == "AGUARDANDO_APROVACAO" and r_self.status_code == 403 and r_send_early.status_code in (403, 409)
            assert "application/pdf" in atts and r_conv_early.status_code == 409

        with step("A2.6", "conversão → pedido (checkout real: preço do servidor + desconto + hold 2 h + cobrança)") as s:
            idem = f"qa-conv-{tag}"
            r = await sales.post(f"/api/v1/crm/proposals/{pid}/convert", json={"campaign_ids": [ctx["cid"]]},
                                 headers={"Idempotency-Key": idem})
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["status"] == "CONVERTIDA" and out["order_id"], out
            oid = out["order_id"]
            o = L.q1("select status, total_cents, media_cents, discount_cents, commercial_discount_bps, "
                     "commercial_discount_approved_by::text, proposal_id::text, payment_order_id::text, org_id::text "
                     "from orders where id=:i", i=oid)
            assert o[4] == 1200 and o[5] == adm.user["id"] and o[6] == pid and o[8] == ctx["client_org"], o
            assert 0 < o[1] < ctx["quote_total"], (o[1], ctx["quote_total"])
            h = L.q1("select status, extract(epoch from (hold_expires_at-created_at)), order_id::text from inventory_holds "
                     "where campaign_id=:c order by created_at desc limit 1", c=ctx["cid"])
            assert h[0] == "HELD" and abs(float(h[1]) - 7200) <= 60, h
            po = L.q1("select status, amount_cents, provider from payment_orders where id=:i", i=o[7])
            assert po[1] == o[1], (po, o)
            att = L.q1("select seller_id::text, amount_cents, commission_bps, commission_cents, status, proposal_id::text "
                       "from sales_attributions where order_id=:i", i=oid)
            assert att and att[0] == ctx["sales_id"] and att[1] == o[1] and att[5] == pid, att
            assert att[3] == o[1] * att[2] // 10000, att
            lead_stage = L.q1("select stage from leads where id=:i", i=ctx["lead"])[0]
            r2 = await sales.post(f"/api/v1/crm/proposals/{pid}/convert", json={"campaign_ids": [ctx["cid"]]},
                                  headers={"Idempotency-Key": f"qa-conv2-{tag}"})
            ctx.update(order=oid, po=o[7])
            log.created.update(order=oid, payment_order=o[7])
            s.ev.update(order=dict(zip(["status", "total_cents", "media_cents", "discount_cents", "commercial_discount_bps",
                                        "discount_approved_by_admin", "proposal_id", "payment_order_id", "org_id"], o)),
                        quote_without_commercial_discount=ctx["quote_total"], hold={"status": h[0], "ttl_s": float(h[1])},
                        payment_order=po, attribution=att, lead_stage=lead_stage,
                        second_conversion=(r2.status_code, L.err_code(r2)))
            assert lead_stage == "GANHO" and r2.status_code == 409

        with step("A2.7", "pagamento (webhook assinado dev, worker) → campanha EM_ANALISE na fila RD") as s:
            r = await fin.post("/api/v1/financial/dev/simulate-webhook", json={"payment_order_id": ctx["po"], "status": "paid"})
            assert r.status_code in (200, 202), r.text

            async def _paid():
                return L.q1("select status from payment_orders where id=:i", i=ctx["po"])[0] == "PAID"
            await L.wait_until(_paid, timeout=120, interval=1, what="pagamento PAID")

            async def _analise():
                st = L.q1("select status from campaigns where id=:i", i=ctx["cid"])[0]
                return st if st in ("EM_ANALISE", "AGUARDANDO_MIDIA", "APROVADA") else None
            st = await L.wait_until(_analise, timeout=60, interval=1, what="campanha após pagamento")
            q = await adm.get("/api/v1/campaigns/review-queue")
            h = L.q1("select status from inventory_holds where campaign_id=:c order by created_at desc limit 1", c=ctx["cid"])
            att = L.q1("select status, commission_cents from sales_attributions where order_id=:i", i=ctx["order"])
            seller_tx = L.q("select t.kind, sum(case when p.amount_cents>0 then p.amount_cents else 0 end) "
                            "from ledger_transactions t join ledger_postings p on p.transaction_id=t.id "
                            "where t.kind='SELLER_COMMISSION' and t.ref_id in (select id::text from sales_attributions "
                            "where order_id=:i) group by 1", i=ctx["order"])
            ra = await sales.get("/api/v1/crm/attributions")
            mine = [a for a in ra.json().get("items", []) if a.get("order_id") == ctx["order"]]
            s.ev.update(campaign_status=st, in_review_queue=ctx["cid"] in q.text, hold=h[0], attribution_after_paid=att,
                        seller_ledger=seller_tx, api_attribution=mine[:1])
            assert st == "EM_ANALISE" and ctx["cid"] in q.text and h[0] == "CONVERTED"
            assert mine, "atribuição não aparece em /crm/attributions"
            if att[1]:
                assert seller_tx and seller_tx[0][1] == att[1], (seller_tx, att)
            else:
                assert not seller_tx, seller_tx  # bps 0 ⇒ nenhum lançamento (SELLER_COMMISSION não exercitado)
            unb = L.q("select t.id::text, sum(p.amount_cents) from ledger_transactions t join ledger_postings p on p.transaction_id=t.id "
                      "where t.kind='SELLER_COMMISSION' group by 1 having sum(p.amount_cents)<>0")
            assert not unb, unb

        with step("A2.8", "aprovação RD da campanha convertida") as s:
            r = await adm.post(f"/api/v1/campaigns/{ctx['cid']}/approve", json={"note": "Aprovado pelo QA (E2E CRM)."})
            assert r.status_code == 200, r.text
            st = L.q1("select status, end_date::text from campaigns where id=:i", i=ctx["cid"])
            ctx["end_before"] = st[1]
            s.ev.update(status=st[0], end_date=st[1])
            assert st[0] in ("APROVADA", "AGENDADA", "SINCRONIZANDO")

        # Renovação (manual, pelo cliente) — a D-5 automática usa o MESMO serviço (generate_auto_renewals → renew)
        with step("R.1", "renovação: proposta do período adicional (sem reserva)") as s:
            r = await adv.post(f"/api/v1/campaigns/{ctx['cid']}/renew", json={"periods": 1})
            s.ev.update(http=r.status_code, body=r.text[:700])
            assert r.status_code in (200, 201), r.text
            holds_before = L.q("select status from inventory_holds where campaign_id=:c and status='HELD'", c=ctx["cid"])
            rev = L.q1("select number, kind, status from campaign_revisions where campaign_id=:c and kind='RENEWAL' "
                       "order by number desc limit 1", c=ctx["cid"])
            g = await adv.get(f"/api/v1/campaigns/{ctx['cid']}/renewal")
            s.ev.update(revision=list(rev) if rev else None, held_after_proposal=len(holds_before), get_renewal=g.status_code)
            assert rev and rev[1] == "RENEWAL" and rev[2] == "AGUARDANDO_PAGAMENTO" and not holds_before

        with step("R.2", "renovação: checkout (hold 2 h + cobrança)") as s:
            r = await adv.post(f"/api/v1/campaigns/{ctx['cid']}/renew/checkout", json={"method": "pix"},
                               headers={"Idempotency-Key": f"qa-renew-{tag}"})
            s.ev.update(http=r.status_code, body=r.text[:700])
            assert r.status_code in (200, 201), r.text
            out = r.json()
            po = (out.get("payment") or {}).get("id") or out.get("payment_order_id")
            h = L.q1("select status, extract(epoch from (hold_expires_at-created_at)) from inventory_holds where campaign_id=:c "
                     "and status='HELD' order by created_at desc limit 1", c=ctx["cid"])
            s.ev.update(order_id=out.get("order_id"), payment_order=po, hold=(h[0], float(h[1])) if h else None,
                        total_cents=out.get("total_cents"))
            assert po and h and abs(float(h[1]) - 7200) <= 60
            ctx["ren_po"] = po
            log.created["renewal_payment_order"] = po

        with step("R.3", "renovação: pagamento → extensão sem reaprovação + termo de comissão RENEWAL") as s:
            r = await fin.post("/api/v1/financial/dev/simulate-webhook", json={"payment_order_id": ctx["ren_po"], "status": "paid"})
            assert r.status_code in (200, 202), r.text

            async def _ext():
                row = L.q1("select status, end_date::text from campaigns where id=:i", i=ctx["cid"])
                return row if row[1] != ctx["end_before"] else None
            row = await L.wait_until(_ext, timeout=120, interval=2, what="extensão do período após pagamento")
            terms = L.q("select kind, pool_cents, start_date::text, end_date::text, status from commission_campaigns "
                        "where campaign_id=:c order by registered_at", c=ctx["cid"])
            hist = L.q("select from_status, to_status, trigger from campaign_status_history where campaign_id=:c "
                       "order by created_at", c=ctx["cid"])
            s.ev.update(status=row[0], end_before=ctx["end_before"], end_after=row[1], commission_terms=terms, history=hist)
            assert row[0] in ("APROVADA", "AGENDADA", "SINCRONIZANDO", "ATIVA")
            assert not any(h[1] == "EM_ANALISE" for h in hist[-2:]), "renovação não deve exigir reaprovação"
            assert [t[0] for t in terms] == ["INITIAL", "RENEWAL"], terms

    finally:
        print(f"[A2] relatório: {log.dump({'ctx': {k: v for k, v in ctx.items() if k != 'renewal'}})}", flush=True)
        for c in (adm, sales, fin, adv):
            if c is not None:
                await c.aclose()
    failed = [x for x in log.steps if x["result"] != "PASS"]
    assert not failed, failed
