"""
C (P002 item 50) — E2E de FALHA contra o stack vivo. SÓ roda com ``RD_E2E_FAILURE=1`` (além de
``RD_E2E_BASE_URL``) e com a orquestração de falhas feita FORA do contêiner de teste
(``stack/scripts/002/qa/c_failures.sh``: stop/start backend, stop/start redis, restart worker).

O player simulado (referência RDPP/1, chave ES256 de teste) roda de forma autônoma:
- reproduz pela função pura ``resolve()`` (tempo absoluto) e gera PoP assinado de cada inserção externa;
- fase "internet caiu" (sem HTTP/WS) — o outbox acumula e a reprodução continua pelo ``resolve()``;
- reenvia o outbox a cada 15 s; mantém WS com reconexão (backoff); refaz o token quando expira;
- coordena com o orquestrador por arquivos-marcador em ``RD_E2E_REPORT_DIR`` (READY / WEBHOOK_SENT / DONE).
No fim: todos os PoP gerados estão no banco (ACCEPTED/valid, sem duplicata), a comissão foi apropriada uma vez
por PoP externo (sem duplicar após quedas/restart do worker), o pagamento feito durante o restart do worker é
processado exatamente uma vez e o ledger continua balanceado.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.e2e import live_support as L
from tests.e2e.test_e2e_full_flow import PlayerWS, _make_png

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

ENABLED = os.environ.get("RD_E2E_FAILURE") == "1"
CITY, UF, TZ = "Macapá", "AP", "America/Belem"
OFFLINE_FROM_S, OFFLINE_TO_S = 60, 360        # fase "internet caiu" relativa ao READY
MAX_WAIT_DONE_S = int(os.environ.get("RD_E2E_FAILURE_MAX_S", "2400"))
FLUSH_EVERY_S = 15


def _marker(tag: str, name: str, payload: dict | None = None) -> None:
    d = Path(L.REPORT_DIR or "/tmp")
    (d / f"C_{tag}_{name}").write_text(json.dumps({"ts": time.time(), **(payload or {})}), encoding="utf-8")


def _has_marker(tag: str, name: str) -> bool:
    return (Path(L.REPORT_DIR or "/tmp") / f"C_{tag}_{name}").exists()


@pytest.mark.skipif(not ENABLED, reason="seção C: defina RD_E2E_FAILURE=1 (somente com SLOT DE FALHA autorizado)")
async def test_failure_resilience_player_outbox_and_no_double_commission():  # noqa: C901
    from app.modules.scheduler.resolve import resolve

    tag = os.environ.get("RD_E2E_FAILURE_TAG") or L.run_tag()
    log = L.StepLog("C_failures")
    timeline: list = []

    def tl(kind: str, **kw):
        timeline.append({"t": round(time.time(), 1), "kind": kind, **kw})

    adm = await L.login("admin")
    fin = await L.login("financial")
    adv = None
    pc = L.new_client(timeout=20)
    player = L.LivePlayer(pc)
    ws = None
    outbox: list = []          # itens assinados ainda não confirmados
    generated: list = []       # todos os PoP gerados (uuid, kind, offline)
    results: dict = {}
    try:
        # ── preparação (inventário dedicado, campanha paga/aprovada, player sincronizado) ─────────────
        t0 = time.monotonic()
        for (sid,) in L.q("select s.id::text from screens s join establishments e on e.id=s.establishment_id "
                          "where e.city_key='AP:macapa' and s.name like :p and s.status<>'DECOMMISSIONED'",
                          p=f"{L.PREFIX}%"):
            await adm.post(f"/api/v1/screens/{sid}/decommission", json={"reason": "QA AÇÃO 002: limpeza anterior (C)"})
        r = await adm.post("/api/v1/organizations", json={
            "name": f"{L.PREFIX}parceiroC_{tag}", "document": L.valid_cnpj(True), "document_type": "CNPJ",
            "business_roles": ["PARTNER"], "city": CITY, "state": UF, "email": f"qa002.pc.{tag}@example.com",
            "phone": "96999990000"})
        assert r.status_code == 201, r.text
        porg = r.json()["id"]
        r = await adm.post("/api/v1/establishments", json={
            "org_id": porg, "name": f"{L.PREFIX}estabC_{tag}", "city": CITY, "state": UF, "postal_code": "68900000",
            "lat": 0.0349, "lng": -51.0694, "geo_source": "MANUAL", "receives_commission": True,
            "email": f"qa002.ec.{tag}@example.com", "phone": "96999990001"})
        assert r.status_code == 201, r.text
        est = r.json()["id"]
        tmpl = (await adm.get("/api/v1/contracts/templates/current", params={"kind": "PARTNERSHIP"})).json()["id"]
        r = await adm.post("/api/v1/contracts/acceptances/manual", json={
            "template_id": tmpl, "org_id": porg, "establishment_id": est, "signer_name": f"{L.PREFIX}signatario",
            "notes": "Aceite registrado pelo QA da AÇÃO 002 (E2E de falha)."})
        assert r.status_code in (200, 201), r.text
        r = await adm.post("/api/v1/screens", json={"establishment_id": est, "name": f"{L.PREFIX}telaC_{tag}",
                                                     "orientation": "HORIZONTAL", "resolution_w": 1920, "resolution_h": 1080})
        assert r.status_code == 201, r.text
        screen = r.json()["id"]
        adv_org, creds = await L.new_advertiser(adm, tag, city=CITY, uf=UF)
        adv = await L.login("advertiser", creds=creds)
        await player.request_pairing()
        r = await adm.post(f"/api/v1/screens/{screen}/pairing", json={"pairing_code": player.pairing_code})
        assert r.status_code == 201, r.text
        assert (await player.fetch_result()).status_code == 200
        assert (await player.authenticate()).status_code == 200
        r = await adv.post("/api/v1/media/upload", data={"org_id": adv_org, "name": f"{L.PREFIX}imgC_{tag}"},
                           files={"file": ("qa_c.png", _make_png(tag), "image/png")})
        assert r.status_code in (200, 202), r.text
        mid = r.json()["id"]

        async def _ready():
            return L.q1("select status from media_files where id=:i", i=mid)[0] == "READY"
        await L.wait_until(_ready, timeout=600, interval=2, what="imagem READY")
        r = await adv.post("/api/v1/creatives", json={"org_id": adv_org, "name": f"{L.PREFIX}criativoC_{tag}",
                                                      "media_file_id": mid, "duration_seconds": 30, "kind": "EXTERNAL"})
        assert r.status_code == 201, r.text
        cr = r.json()["id"]
        today = datetime.now(ZoneInfo(TZ)).date()
        freq = int(os.environ.get("RD_E2E_FAILURE_FREQ", "15"))
        r = await adv.post("/api/v1/campaigns", json={
            "org_id": adv_org, "name": f"{L.PREFIX}campanhaC_{tag}", "period_type": "AVULSO",
            "start_date": today.isoformat(), "end_date": (today + timedelta(days=1)).isoformat(), "duration_s": 30,
            "freq_kind": "PER_HOUR", "freq_value": freq, "allow_same_advertiser_consecutive": True,
            "targets": [{"type": "CITY", "value": f"{UF}:{CITY}", "exclude": False}]})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        assert (await adv.post(f"/api/v1/campaigns/{cid}/creatives", json={"creative_id": cr})).status_code in (200, 201)
        r = await adv.post(f"/api/v1/campaigns/{cid}/checkout", json={"method": "pix"},
                           headers={"Idempotency-Key": str(uuid.uuid4())})
        assert r.status_code == 201, r.text
        po = r.json()["payment"]["id"]
        r = await fin.post("/api/v1/financial/dev/simulate-webhook", json={"payment_order_id": po, "status": "paid"})
        assert r.status_code in (200, 202), r.text

        async def _analise():
            return L.q1("select status from campaigns where id=:i", i=cid)[0] == "EM_ANALISE"
        await L.wait_until(_analise, timeout=120, interval=1, what="EM_ANALISE")
        r = await adm.post(f"/api/v1/campaigns/{cid}/approve", json={"note": "QA AÇÃO 002 — E2E de falha"})
        assert r.status_code == 200, r.text

        async def _manifest():
            rr = await pc.get("/api/v1/player/manifest", headers=player.headers)
            if rr.status_code != 200:
                return None
            env = rr.json()
            return env if any(c["creative_id"] == cr for c in env["manifest"]["creatives"].values()) else None
        env = await L.wait_until(_manifest, timeout=120, interval=2, what="manifesto com o criativo")
        man = env["manifest"]
        ckey = next(k for k, c in man["creatives"].items() if c["creative_id"] == cr)
        mkey = man["creatives"][ckey]["media"]
        media = man["media"][mkey]
        r = await pc.get(f"/api/v1/player/media/{media['media_id']}", headers=player.headers)
        assert r.status_code == 200 and len(r.content) == media["size"]
        ticket = await player.ws_ticket()
        ws = await PlayerWS(L.ws_url(L.BASE_URL, f"/ws/player?ticket={ticket}"), L.PLAYER_ORIGIN).connect()
        hb = {"state": "PLAYING", "current": None, "manifest_version_applied": man["version"],
              "manifest_version_pending": None, "download": None, "last_sync_ms": L.now_ms(), "last_play_ms": None,
              "storage": {"free_bytes": 1, "used_bytes": 0, "persistent": True}, "clock_offset_ms": 0, "alerts": []}
        ws.start_heartbeat(lambda: hb)
        await ws.send("manifest_applied", {"version": man["version"]})
        anchor = man.get("anchor_ms")
        if anchor is None:
            r = await pc.post("/api/v1/player/anchor", json={"anchor_ms": (L.now_ms() // 1000) * 1000},
                              headers=player.headers)
            anchor = r.json()["anchor_ms"]
        log.created.update(org=porg, establishment=est, screen=screen, advertiser_org=adv_org, campaign=cid,
                           payment_order=po, device=player.device_id)
        log.record("C.0", "preparação: tela + player pareado + campanha paga/aprovada + manifesto/mídia", True, t0,
                   {"campaign": cid, "freq_per_hour": freq, "manifest_version": man["version"], "anchor_ms": anchor})

        # ── execução autônoma ───────────────────────────────────────────────────────────────────────
        ready_at = time.time()
        _marker(tag, "READY", {"campaign": cid, "screen": screen})
        tl("ready")
        local_state = {"revoked": [], "media_ready": [mkey]}
        cursor = L.now_ms()
        sg = resolve(man, anchor, local_state, cursor)
        cursor = sg.end_ms  # começa na próxima fronteira (nunca no meio de item)
        last_flush = 0.0
        ws_backoff = 1.0
        next_ws_try = 0.0
        webhook2 = None

        def offline_now() -> bool:
            el = time.time() - ready_at
            return OFFLINE_FROM_S <= el < OFFLINE_TO_S

        async def flush():
            nonlocal outbox
            if not outbox:
                return
            batch = outbox[:200]
            try:
                rr = await pc.post("/api/v1/player/events:batch", json={"events": batch}, headers=player.headers)
            except (httpx.HTTPError, OSError) as exc:
                tl("flush_error", error=type(exc).__name__, pending=len(outbox))
                return
            if rr.status_code == 401:
                a = await player.authenticate()
                tl("reauth", http=a.status_code)
                return
            if rr.status_code != 200:
                tl("flush_http", http=rr.status_code, pending=len(outbox))
                return
            done = set()
            for res in rr.json()["results"]:
                results[res["playback_uuid"]] = res
                if res["status"] in ("ACCEPTED", "DUPLICATE", "REJECTED"):
                    done.add(res["playback_uuid"])
            outbox = [x for x in outbox if x["event"]["playback_uuid"] not in done]
            tl("flush_ok", sent=len(batch), confirmed=len(done), pending=len(outbox))

        deadline = ready_at + MAX_WAIT_DONE_S
        done_seen_at = None
        pre_worker_handled = False
        while time.time() < deadline:
            now = L.now_ms()
            # reprodução pelo resolve(): fecha os segmentos já terminados e gera PoP
            while True:
                sg = resolve(man, anchor, local_state, cursor)
                if sg.end_ms > now:
                    break
                if sg.kind == "ITEM" and sg.block == "EXTERNAL" and sg.creative_key == ckey:
                    c = man["creatives"][sg.creative_key]
                    from tests.e2e.test_e2e_full_flow import _insertion_for
                    ins = _insertion_for(man, anchor, sg, sg.creative_key)
                    d = sg.end_ms - sg.start_ms
                    ev = player.event(man, sg.creative_key, start=sg.start_ms, expected=d, actual=d, insertion_id=ins,
                                      offline=offline_now())
                    outbox.append(player.signed(ev))
                    generated.append({"uuid": ev["playback_uuid"], "kind": c["kind"], "offline": ev["offline"],
                                      "start": sg.start_ms})
                cursor = sg.end_ms
            off = offline_now()
            if off:
                if ws is not None:
                    await ws.close()
                    ws = None
                    tl("network_down_simulated")
            else:
                if ws is None or ws.finished:
                    if time.time() >= next_ws_try:
                        try:
                            if ws is not None:
                                await ws.close()
                            t = await player.ws_ticket()
                            ws = await PlayerWS(L.ws_url(L.BASE_URL, f"/ws/player?ticket={t}"), L.PLAYER_ORIGIN).connect()
                            ws.start_heartbeat(lambda: hb)
                            tl("ws_connected")
                            ws_backoff = 1.0
                        except Exception as exc:  # backend/redis fora: backoff exponencial com teto
                            ws = None
                            tl("ws_connect_failed", error=type(exc).__name__[:40])
                            next_ws_try = time.time() + ws_backoff
                            ws_backoff = min(ws_backoff * 2, 30.0)
                if time.time() - last_flush >= FLUSH_EVERY_S:
                    last_flush = time.time()
                    await flush()
            # restart do worker com tarefa em andamento: novo pagamento imediatamente antes
            if not pre_worker_handled and _has_marker(tag, "PRE_WORKER"):
                pre_worker_handled = True
                # segundo pedido/cobrança: nova campanha do mesmo anunciante (1 dia), paga durante o restart
                rr = await adv.post("/api/v1/campaigns", json={
                    "org_id": adv_org, "name": f"{L.PREFIX}campanhaC2_{tag}", "period_type": "AVULSO",
                    "start_date": (today + timedelta(days=2)).isoformat(), "end_date": (today + timedelta(days=2)).isoformat(),
                    "duration_s": 30, "freq_kind": "PER_HOUR", "freq_value": 1,
                    "targets": [{"type": "CITY", "value": f"{UF}:{CITY}", "exclude": False}]})
                c2 = rr.json().get("id") if rr.status_code == 201 else None
                if c2:
                    rr = await adv.post(f"/api/v1/campaigns/{c2}/checkout", json={"method": "pix"},
                                        headers={"Idempotency-Key": str(uuid.uuid4())})
                    po2 = rr.json().get("payment", {}).get("id") if rr.status_code == 201 else None
                    if po2:
                        w = await fin.post("/api/v1/financial/dev/simulate-webhook",
                                           json={"payment_order_id": po2, "status": "paid"})
                        webhook2 = {"campaign": c2, "payment_order": po2, "http": w.status_code}
                tl("webhook_before_worker_restart", **(webhook2 or {"error": rr.status_code}))
                _marker(tag, "WEBHOOK_SENT", webhook2 or {})
            if done_seen_at is None and _has_marker(tag, "DONE"):
                done_seen_at = time.time()
                tl("orchestrator_done")
            if done_seen_at and time.time() - done_seen_at > 90 and not outbox:
                break
            await asyncio.sleep(1)
        # fim: flush final (até 3 min)
        end = time.time() + 180
        while outbox and time.time() < end:
            await flush()
            await asyncio.sleep(5)

        # ── verificações ───────────────────────────────────────────────────────────────────────────
        t1 = time.monotonic()
        uuids = [g["uuid"] for g in generated]
        ext = [g["uuid"] for g in generated if g["kind"] == "EXTERNAL"]
        rows = L.q("select playback_uuid::text, status, valid, reason, offline from playback_events where playback_uuid::text = any(:u)",
                   u=uuids) if uuids else []
        by = {r[0]: r for r in rows}
        missing = [u for u in uuids if u not in by]
        invalid = [(u, by[u][1], by[u][3]) for u in uuids if u in by and not (by[u][1] == "ACCEPTED" and by[u][2])]
        dups = L.q1("select count(*) - count(distinct playback_uuid) from playback_events where device_id=:d",
                    d=player.device_id)[0]
        log.record("C.1", "PoP acumulados (offline + quedas) sincronizados após o retorno, sem perda/duplicata",
                   not missing and not invalid and dups == 0 and not outbox, t1,
                   {"generated": len(uuids), "external": len(ext), "offline_generated": sum(1 for g in generated if g["offline"]),
                    "in_db": len(rows), "missing": missing[:5], "invalid": invalid[:5], "outbox_left": len(outbox),
                    "dup_rows": dups})

        async def _all_accrued():
            n = L.q1("select count(*) from commission_accrual_events where playback_uuid::text = any(:u)", u=ext)[0] if ext else 0
            return n == len(ext)
        t2 = time.monotonic()
        try:
            await L.wait_until(_all_accrued, timeout=240, interval=5, what="apropriação de todos os PoP externos")
            accrued_ok = True
        except L.E2EError:
            accrued_ok = False
        acc_ev = L.q1("select count(*), count(*) filter (where counted) from commission_accrual_events where campaign_id=:c",
                      c=cid)
        acc = L.q("select local_day::text, valid_count, expected_count, amount_ucents from commission_accruals where campaign_id=:c",
                  c=cid)
        sum_valid = sum(r[1] for r in acc)
        log.record("C.2", "comissão NÃO duplica (accrual = 1 por PoP externo válido; ledger balanceado)",
                   accrued_ok and acc_ev[0] == len(ext) and sum_valid == acc_ev[1], t2,
                   {"external_pops": len(ext), "accrual_events": acc_ev, "accruals": acc,
                    "ledger_unbalanced": L.q("select transaction_id::text from ledger_postings group by 1 having sum(amount_cents)<>0")})
        t3 = time.monotonic()
        ok3, ev3 = True, {"webhook2": webhook2}
        if webhook2 and webhook2.get("payment_order"):
            async def _paid2():
                return L.q1("select status from payment_orders where id=:i", i=webhook2["payment_order"])[0] == "PAID"
            try:
                await L.wait_until(_paid2, timeout=240, interval=5, what="pagamento feito durante o restart do worker")
            except L.E2EError:
                ok3 = False
            ev3["payment_status"] = L.q1("select status from payment_orders where id=:i", i=webhook2["payment_order"])[0]
            ev3["webhook_events"] = L.q("select status, attempts from webhook_events where payment_order_id=:i",
                                        i=webhook2["payment_order"])
            ev3["payment_ledger_txs"] = L.q1("select count(*) from ledger_transactions where kind='PAYMENT_RECEIVED' "
                                             "and (ref_id=:p or external_ref=:p)", p=webhook2["payment_order"])[0]
            ev3["campaign2_status"] = L.q1("select status from campaigns where id=:i", i=webhook2["campaign"])[0]
            ok3 = ok3 and ev3["payment_ledger_txs"] == 1 and ev3["payment_status"] == "PAID"
        else:
            ok3 = False
        log.record("C.3", "pagamento enviado imediatamente antes do restart do worker: processado exatamente 1×", ok3, t3, ev3)
    finally:
        print(f"[C] relatório: {log.dump({'timeline': timeline[-400:], 'generated_count': len(generated)})}", flush=True)
        _marker(tag, "FINISHED", {})
        if ws is not None:
            await ws.close()
        for c in (adm, fin, adv):
            if c is not None:
                await c.aclose()
        await pc.aclose()
    failed = [x for x in log.steps if x["result"] != "PASS"]
    assert not failed, failed
