"""
E2E PRINCIPAL REAL (P002 item 49) — contra o STACK VIVO (nginx → backend; worker Celery real).

Fluxo: organização parceira → estabelecimento (fuso) → tela → pareamento ES256 → upload real
(vídeo 30 s 1920×1080 + imagem) → processamento pelo WORKER → criativo/campanha → targeting
cidade+categoria → preço do servidor → checkout com reserva de 2 h → pagamento pelo webhook
ASSINADO do provedor FALSO (caminho dev) → aprovação RD (MANUAL) → manifesto assinado →
sincronização (manifesto + mídia com Range, sha256) → reprodução (resolve() + PoP 100 %) →
lote de PoP → validação → comissão apropriada → relatório + comprovante PDF verificável →
conciliação + ledger balanceado. Inclui idempotência (webhook repetido e PoP duplicado).

Pulado sem ``RD_E2E_BASE_URL`` (ver ``tests/e2e/conftest.py``).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.e2e import live_support as L

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

TZ_EXPECTED = "America/Cuiaba"
CITY, UF = "Cuiabá", "MT"
FREQ_PER_HOUR = 15  # 1 inserção por ciclo de 4 min (política 120/120) → espera curta pelo próximo slot


# ─── utilidades locais ───────────────────────────────────────────────────────

def _make_video(path: str, tag: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "30", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-profile:v", "high", "-c:a", "aac",
         "-shortest", "-metadata", f"comment={L.PREFIX}{tag}", path],
        check=True, timeout=240)


def _make_png(tag: str) -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (1920, 1080), (20, 90, 140))
    d = ImageDraw.Draw(im)
    d.rectangle([100, 100, 1820, 980], outline=(255, 255, 255), width=12)
    d.text((160, 160), f"{L.PREFIX} imagem de teste {tag}", fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class PlayerWS:
    """Cliente WS do player (envelope RDPP) com leitor em segundo plano e heartbeat."""

    def __init__(self, url: str, origin: str):
        self.url, self.origin = url, origin
        self.msgs: list[dict] = []
        self._ws = None
        self._tasks: list[asyncio.Task] = []
        self.closed_code = None
        self.finished = False

    async def connect(self):
        from websockets.asyncio.client import connect

        self._ws = await connect(self.url, additional_headers={"Origin": self.origin}, open_timeout=15)
        self._tasks.append(asyncio.create_task(self._reader()))
        return self

    async def _reader(self):
        try:
            async for raw in self._ws:
                try:
                    self.msgs.append(json.loads(raw))
                except ValueError:
                    pass
        except Exception:
            pass
        finally:
            self.closed_code = getattr(self._ws, "close_code", None)
            self.finished = True

    def start_heartbeat(self, state_fn):
        async def hb():
            while True:
                try:
                    await self.send("heartbeat", state_fn())
                except Exception:
                    return
                await asyncio.sleep(20)
        self._tasks.append(asyncio.create_task(hb()))

    async def send(self, typ: str, data: dict):
        await self._ws.send(json.dumps({"type": typ, "id": str(uuid.uuid4()), "ts_ms": L.now_ms(), "data": data}))

    def of_type(self, typ: str) -> list[dict]:
        return [m for m in self.msgs if m.get("type") == typ]

    async def wait_type(self, typ: str, timeout: float, after: int = 0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for m in self.msgs[after:]:
                if m.get("type") == typ:
                    return m
            await asyncio.sleep(0.25)
        raise L.E2EError(f"WS: '{typ}' não chegou em {timeout}s (recebidos: {[m.get('type') for m in self.msgs]})")

    async def close(self):
        for t in self._tasks:
            t.cancel()
        if self._ws is not None:
            await self._ws.close()


# ─── o teste ─────────────────────────────────────────────────────────────────

async def test_e2e_full_flow():  # noqa: C901 — fluxo narrado em 20 passos
    from app.modules.playback import protocol as proto
    from app.modules.organizations.documents import make_cnpj
    from app.modules.scheduler.resolve import resolve

    log = L.StepLog("E2E_full_flow")
    tag = L.run_tag()
    adm = await L.login("admin")
    fin = await L.login("financial")
    adv = None
    adv_org = None
    pclient = L.new_client()
    player = L.LivePlayer(pclient)
    pws = None
    ctx: dict = {}

    def step(n, title, fatal=True):
        """fatal=False: a falha é registrada (e reprova o teste no fim), mas os passos seguintes continuam."""
        class _S:
            def __enter__(s):
                s.t0 = time.monotonic()
                s.ev = {}
                return s

            def __exit__(s, et, e, tb):
                if et is None:
                    log.record(n, title, True, s.t0, s.ev)
                else:
                    s.ev["error"] = f"{et.__name__}: {str(e)[:600]}"
                    log.record(n, title, False, s.t0, s.ev)
                return (et is not None) and not fatal and issubclass(et, AssertionError)
        return _S()

    try:
        # 0 ─ higiene: telas QA de execuções anteriores na mesma cidade saem do inventário (a segmentação
        #     cidade+categoria desta execução deve resolver SOMENTE a tela criada agora)
        with step(0, "pré-limpeza: telas QA anteriores em MT:cuiaba descomissionadas") as s:
            old = L.q("select s.id::text from screens s join establishments e on e.id=s.establishment_id "
                      "where e.city_key='MT:cuiaba' and s.name like :p and s.status <> 'DECOMMISSIONED'", p=f"{L.PREFIX}%")
            done = []
            for (sid,) in old:
                r = await adm.post(f"/api/v1/screens/{sid}/decommission",
                                   json={"reason": "QA AÇÃO 002: limpeza de execução E2E anterior"})
                done.append((sid, r.status_code))
            s.ev.update(decommissioned=done)

        # 1 ─ organização parceira
        with step(1, "criar organização (parceiro)") as s:
            base = ("Q" + uuid.uuid4().hex.upper())[:12]
            doc = make_cnpj(base)
            r = await adm.post("/api/v1/organizations", json={
                "name": f"{L.PREFIX}parceiro_{tag}", "document": doc, "document_type": "CNPJ",
                "business_roles": ["PARTNER"], "trade_name": f"{L.PREFIX}parceiro", "responsible_name": f"{L.PREFIX}responsavel",
                "email": f"qa002.parceiro.{tag}@example.com", "phone": "65999990000", "city": CITY, "state": UF,
                "notes": "Criado pelo QA da AÇÃO 002 (E2E)."})
            assert r.status_code == 201, r.text
            org_id = r.json()["id"]
            ctx["org_id"] = org_id
            log.created["organization"] = org_id
            row = L.q1("select business_roles, document, kyc_status from organizations where id=:i", i=org_id)
            assert row and "PARTNER" in row[0] and row[1] == doc
            s.ev.update(org_id=org_id, alfanumeric_cnpj=not doc.isdigit(), business_roles=row[0], kyc=row[2])

        # 1b ─ anunciante (organização com cadastro de pagador completo + usuário por convite real/Mailpit)
        with step("1b", "anunciante: organização + usuário convidado (Mailpit)") as s:
            adv_org, adv_creds = await L.new_advertiser(adm, tag, city=CITY, uf=UF)
            adv = await L.login("advertiser", creds=adv_creds)
            log.created["advertiser_org"] = adv_org
            me = (await adv.get("/api/v1/me")).json()
            assert any(a["role"] == "advertiser" and a["scope_id"] == adv_org for a in me["assignments"]), me["assignments"]
            s.ev.update(advertiser_org=adv_org, advertiser_user=me["user"]["id"], invite_via_mailpit=True)

        # 2 ─ estabelecimento (fuso automático pela UF/cidade)
        with step(2, "estabelecimento com fuso correto") as s:
            r = await adm.get("/api/v1/taxonomy/nodes", params={"kind": "CATEGORY"})
            cats = {c["slug"]: c["id"] for c in r.json()["items"]}
            category_id = cats.get("academia") or next(iter(cats.values()))
            ctx["category_id"] = category_id
            r = await adm.get("/api/v1/partner-plans")
            plan = next((p for p in r.json()["items"] if p["tv_provision"] == "OWN_TV" and p["receives_commission"]), None)
            body = {"org_id": org_id, "name": f"{L.PREFIX}estab_{tag}", "city": CITY, "state": UF,
                    "postal_code": "78005000", "lat": -15.6010, "lng": -56.0974, "geo_source": "MANUAL",
                    "category_id": category_id, "receives_commission": True,
                    "responsible_name": f"{L.PREFIX}gerente", "phone": "65999990001",
                    "email": f"qa002.estab.{tag}@example.com", "notes": "QA AÇÃO 002 (E2E)."}
            if plan:
                body["partner_plan_id"] = plan["id"]
            r = await adm.post("/api/v1/establishments", json=body)
            assert r.status_code == 201, r.text
            est = r.json()
            est_id = est["id"]
            ctx["est_id"] = est_id
            log.created["establishment"] = est_id
            row = L.q1("select timezone, timezone_source, city_key, region, receives_commission from establishments where id=:i",
                       i=est_id)
            assert row[0] == TZ_EXPECTED, row
            assert row[2] == "MT:cuiaba", row
            s.ev.update(est_id=est_id, timezone=row[0], timezone_source=row[1], city_key=row[2], region=row[3])
            # contrato de parceria aceito (vendável) — registro manual pela equipe RD, rotulado QA
            r = await adm.get("/api/v1/contracts/templates/current", params={"kind": "PARTNERSHIP"})
            assert r.status_code == 200, r.text
            r = await adm.post("/api/v1/contracts/acceptances/manual", json={
                "template_id": r.json()["id"], "org_id": org_id, "establishment_id": est_id,
                "signer_name": f"{L.PREFIX}signatario (teste QA, sem assinatura real)",
                "notes": "Aceite registrado pelo QA da AÇÃO 002 em ambiente de desenvolvimento (E2E)."})
            assert r.status_code in (200, 201), r.text
            s.ev["contract_acceptance"] = r.status_code

        # 3 ─ tela
        with step(3, "tela") as s:
            r = await adm.post("/api/v1/screens", json={
                "establishment_id": est_id, "name": f"{L.PREFIX}tela_{tag}", "orientation": "HORIZONTAL", "inches": 43,
                "resolution_w": 1920, "resolution_h": 1080, "notes": "QA AÇÃO 002 (E2E)."})
            assert r.status_code == 201, r.text
            scr = r.json()
            screen_id = scr["id"]
            ctx["screen_id"] = screen_id
            log.created["screen"] = screen_id
            row = L.q1("select friendly_id, status, sellable from screens where id=:i", i=screen_id)
            assert row[0].startswith("TEL-MT-") and row[1] == "ACTIVE" and row[2] is True, row
            inv = await adm.get(f"/api/v1/inventory/screens/{screen_id}/state")
            inv_j = inv.json() if inv.status_code == 200 else {}
            s.ev.update(screen_id=screen_id, friendly_id=row[0], status=row[1], sellable=row[2],
                        inventory_state_http=inv.status_code, inventory_tz=inv_j.get("timezone"),
                        inventory_sellable=inv_j.get("sellable"))

        # 4 ─ pareamento (pedido com chave pública → claim no painel → resultado assinado → challenge/token)
        with step(4, "parear player (ES256: pedido → claim admin → resultado → token)") as s:
            pr = await player.request_pairing()
            assert pr["_headers"].get("x-server-time-ms"), "X-Server-Time-Ms ausente"
            from websockets.asyncio.client import connect as ws_connect

            pairing_msgs: list = []
            pws_pair = await ws_connect(L.ws_url(L.BASE_URL, f"/ws/player/pairing?request_id={player.request_id}"),
                                        additional_headers={"Origin": L.PLAYER_ORIGIN}, open_timeout=15)

            async def _read_pair():
                try:
                    async for raw in pws_pair:
                        pairing_msgs.append(json.loads(raw))
                except Exception:
                    pass
            rt = asyncio.create_task(_read_pair())
            await asyncio.sleep(0.5)
            r = await adm.post(f"/api/v1/screens/{screen_id}/pairing", json={"pairing_code": player.pairing_code})
            assert r.status_code == 201, r.text
            await L.wait_until(lambda: asyncio.sleep(0, result=any(m.get("type") == "pairing_claimed" for m in pairing_msgs)),
                               timeout=15, interval=0.3, what="pairing_claimed no WS de pareamento")
            rt.cancel()
            await pws_pair.close()
            r = await player.fetch_result()
            assert r.status_code == 200, r.text
            assert player.screen_id == screen_id and player.server_keys
            r = await player.authenticate()
            assert r.status_code == 200, r.text
            log.created["device"] = player.device_id
            row = L.q1("select status, platform, key_alg, screen_id::text, pairing_method from devices where id=:i",
                       i=player.device_id)
            assert row[0] == "ACTIVE" and row[2] == "ES256" and row[3] == screen_id, row
            aud = L.q1("select count(*) from audit_logs where entity_id=:i or entity_id=:s", i=player.device_id, s=screen_id)
            s.ev.update(device_id=player.device_id, pairing_code_format=len(player.pairing_code) == 9,
                        ws_pairing_claimed=True, device_status=row[0], pairing_method=row[4],
                        server_key_ids=[k["key_id"] for k in player.server_keys], audit_rows=aud[0])

        # player conectado ao WS (recebe sync_required/command/kill)
        ticket = await player.ws_ticket()
        pws = await PlayerWS(L.ws_url(L.BASE_URL, f"/ws/player?ticket={ticket}"), L.PLAYER_ORIGIN).connect()
        hb_state = {"state": "IDLE", "current": None, "manifest_version_applied": None, "manifest_version_pending": None,
                    "download": None, "last_sync_ms": None, "last_play_ms": None,
                    "storage": {"free_bytes": 10_000_000_000, "used_bytes": 0, "persistent": True},
                    "clock_offset_ms": 0, "alerts": []}
        pws.start_heartbeat(lambda: hb_state)
        await pws.wait_type("server_time", 10)

        # 5 ─ upload real (vídeo 30 s 1920×1080 + imagem)
        with step(5, "upload de mídia real (vídeo 30 s 1920×1080 + imagem PNG)") as s:
            with tempfile.TemporaryDirectory() as td:
                vp = os.path.join(td, "qa_video.mp4")
                t_ff = time.monotonic()
                _make_video(vp, tag)
                vbytes = open(vp, "rb").read()
            s.ev["ffmpeg_s"] = round(time.monotonic() - t_ff, 1)
            png = _make_png(tag)
            r = await adv.post("/api/v1/media/upload", data={"org_id": adv_org, "name": f"{L.PREFIX}video_{tag}"},
                               files={"file": ("qa_video.mp4", vbytes, "video/mp4")}, timeout=300)
            assert r.status_code in (200, 202), r.text
            video_id = r.json()["id"]
            r = await adv.post("/api/v1/media/upload", data={"org_id": adv_org, "name": f"{L.PREFIX}imagem_{tag}"},
                               files={"file": ("qa_imagem.png", png, "image/png")}, timeout=120)
            assert r.status_code in (200, 202), r.text
            image_id = r.json()["id"]
            ctx.update(video_id=video_id, image_id=image_id)
            log.created["media"] = [video_id, image_id]
            s.ev.update(video_id=video_id, video_bytes=len(vbytes), video_sha256_upload=hashlib.sha256(vbytes).hexdigest(),
                        image_id=image_id, image_bytes=len(png),
                        status_after_upload=L.q("select id::text, status from media_files where id in (:a,:b)",
                                                a=video_id, b=image_id))

        # 6 ─ processamento pelo WORKER real até READY
        with step(6, "processamento pelo worker real até READY") as s:
            t6 = time.monotonic()

            async def _ready():
                rows = L.q("select id::text, status, status_code from media_files where id in (:a,:b)", a=video_id, b=image_id)
                st = {r[0]: r[1] for r in rows}
                bad = [r for r in rows if r[1] in ("REJECTED", "FAILED")]
                if bad:
                    raise L.E2EError(f"mídia não processada: {bad}")
                return all(v == "READY" for v in st.values()) and len(st) == 2
            await L.wait_until(_ready, timeout=600, interval=3, what="mídias READY (worker)")
            v = L.q1("select status, width, height, duration_ms, codec, sha256, size_bytes, attempts, kind, mime_type "
                     "from media_files where id=:i", i=video_id)
            im = L.q1("select status, width, height, sha256, mime_type from media_files where id=:i", i=image_id)
            assert v[1] == 1920 and v[2] == 1080 and v[3] == 30000 and v[5] and len(v[5]) == 64, v
            assert im[0] == "READY" and im[1] == 1920 and im[2] == 1080, im
            r = await adv.get(f"/api/v1/media/{video_id}")
            assert r.status_code == 200 and r.json()["status"] == "READY", r.text
            aud = L.q("select action, origin from audit_logs where entity_id=:i order by created_at", i=video_id)
            ctx["video_sha"] = v[5]
            s.ev.update(processing_wait_s=round(time.monotonic() - t6, 1), video=dict(zip(
                ["status", "width", "height", "duration_ms", "codec", "sha256", "size", "attempts", "kind", "mime"], v)),
                image=dict(zip(["status", "width", "height", "sha256", "mime"], im)), audit=aud)

        # 7 ─ criativo + campanha (anunciante)
        tz = ZoneInfo(TZ_EXPECTED)
        today_local = datetime.now(tz).date()
        with step(7, "campanha (anunciante) com criativo") as s:
            r = await adv.post("/api/v1/creatives", json={"org_id": adv_org, "name": f"{L.PREFIX}criativo_{tag}",
                                                          "media_file_id": video_id, "duration_seconds": 30,
                                                          "kind": "EXTERNAL", "ad_text": "Teste QA AÇÃO 002"})
            assert r.status_code == 201, r.text
            creative = r.json()
            creative_id, creative_code = creative["id"], creative["code"]
            assert creative_code.startswith("RD-") and len(creative_code) == 9
            r = await adv.post("/api/v1/campaigns", json={
                "org_id": adv_org, "name": f"{L.PREFIX}campanha_{tag}", "description": "QA AÇÃO 002 — E2E",
                "period_type": "AVULSO", "start_date": today_local.isoformat(),
                "end_date": (today_local + timedelta(days=1)).isoformat(), "duration_s": 30,
                "freq_kind": "PER_HOUR", "freq_value": FREQ_PER_HOUR})
            assert r.status_code == 201, r.text
            camp = r.json()
            camp_id = camp["id"]
            ctx.update(creative_id=creative_id, camp_id=camp_id, creative_code=creative_code)
            log.created.update(creative=creative_id, campaign=camp_id)
            r = await adv.post(f"/api/v1/campaigns/{camp_id}/creatives", json={"creative_id": creative_id})
            assert r.status_code in (200, 201), r.text
            row = L.q1("select status from campaigns where id=:i", i=camp_id)
            assert row[0] == "RASCUNHO", row
            s.ev.update(creative_id=creative_id, creative_code=creative_code, campaign_id=camp_id, status=row[0],
                        start=today_local.isoformat())

        # 8 ─ targeting cidade + categoria
        with step(8, "targeting (cidade + categoria)") as s:
            targets = [{"type": "CITY", "value": f"{UF}:{CITY}", "exclude": False},
                       {"type": "CATEGORY", "value": category_id, "exclude": False}]
            r = await adv.put(f"/api/v1/campaigns/{camp_id}/targets", json={"targets": targets})
            assert r.status_code == 200, r.text
            r = await adv.post("/api/v1/campaigns/availability", json={"org_id": adv_org, "targets": targets, "duration_s": 30})
            assert r.status_code == 200, r.text
            av = r.json()
            by_city = {c["city_key"]: c["screens"] for c in av.get("by_city", [])}
            assert by_city.get("MT:cuiaba", 0) >= 1, av
            rows = L.q("select target_type, target_value, is_exclude from campaign_targets where campaign_id=:i order by 1",
                       i=camp_id)
            assert {(a, b) for a, b, _ in rows} >= {("CITY", "MT:cuiaba"), ("CATEGORY", category_id)}, rows
            # nomes de estabelecimentos não vazam ao anunciante
            assert f"{L.PREFIX}estab_{tag}" not in json.dumps(av), "nome do estabelecimento exposto ao anunciante"
            s.ev.update(targets=rows, availability_by_city=by_city, total_screens=av.get("screens") or av.get("total_screens"))

        # 9 ─ preço calculado no servidor
        with step(9, "preço do servidor (quote; preço do cliente rejeitado)") as s:
            r = await adv.post(f"/api/v1/campaigns/{camp_id}/quote", json={"method": "pix"})
            assert r.status_code == 200, r.text
            quote = r.json()["quote"]
            q_total = quote.get("total_cents")
            assert isinstance(q_total, int) and q_total > 0, quote
            assert "margin" not in quote and "per_screen_weights" not in quote, "visão do anunciante expõe custos/margem"
            r = await adv.post(f"/api/v1/campaigns/{camp_id}/checkout", json={"method": "pix", "amount_cents": 1},
                               headers={"Idempotency-Key": str(uuid.uuid4())})
            assert r.status_code == 422, r.text
            ra = await adm.post(f"/api/v1/campaigns/{camp_id}/quote", json={"method": "pix"})
            staff_q = ra.json().get("quote", {}) if ra.status_code == 200 else {}
            assert staff_q.get("total_cents") == q_total, (staff_q.get("total_cents"), q_total)
            s.ev.update(quote_total_cents=q_total, client_price_rejected=r.status_code,
                        quote={k: v for k, v in quote.items() if isinstance(v, (int, str)) and not isinstance(v, bool)},
                        staff_view_has_margin="margin" in staff_q)

        # 10 ─ checkout com reserva de 2 h
        with step(10, "checkout com hold de 2 h") as s:
            idem = str(uuid.uuid4())
            r = await adv.post(f"/api/v1/campaigns/{camp_id}/checkout", json={"method": "pix"},
                               headers={"Idempotency-Key": idem})
            assert r.status_code == 201, r.text
            order = r.json()
            order_id, po_id = order["order_id"], order["payment"]["id"]
            assert order["total_cents"] == q_total, (order["total_cents"], q_total)
            ctx.update(order_id=order_id, po_id=po_id)
            log.created.update(order=order_id, payment_order=po_id)
            r2 = await adv.post(f"/api/v1/campaigns/{camp_id}/checkout", json={"method": "pix"},
                                headers={"Idempotency-Key": idem})
            assert r2.status_code == 200 and r2.json()["order_id"] == order_id, r2.text
            h = L.q1("select id::text, status, extract(epoch from (hold_expires_at - created_at)), screens_held "
                     "from inventory_holds where campaign_id=:c order by created_at desc limit 1", c=camp_id)
            assert h and h[1] == "HELD" and abs(float(h[2]) - 7200) <= 60, h
            po = L.q1("select status, amount_cents, provider, method from payment_orders where id=:i", i=po_id)
            cst = L.q1("select status from campaigns where id=:i", i=camp_id)[0]
            assert cst == "AGUARDANDO_PAGAMENTO", cst
            ctx["hold_id"] = h[0]
            s.ev.update(order_id=order_id, total_cents=order["total_cents"], idempotent_replay=r2.status_code,
                        hold={"id": h[0], "status": h[1], "ttl_s": float(h[2]), "screens_held": h[3]},
                        payment_order={"status": po[0], "amount_cents": po[1], "provider": po[2], "method": po[3]},
                        campaign_status=cst)

        # 11 ─ pagamento: webhook ASSINADO (provedor FALSO, caminho dev) → worker processa
        with step(11, "pagamento via webhook ASSINADO (dev/fake) processado pelo worker") as s:
            led_before = L.q1("select count(*) from ledger_transactions")[0]
            r = await fin.post("/api/v1/financial/dev/simulate-webhook", json={"payment_order_id": po_id, "status": "paid",
                                                                               "fee_cents": 0})
            assert r.status_code in (200, 202), r.text
            sim = r.json()
            assert sim.get("simulated") is True and sim.get("provider_mode") == "FAKE", sim

            async def _paid():
                row = L.q1("select status from payment_orders where id=:i", i=po_id)
                return row[0] == "PAID"
            await L.wait_until(_paid, timeout=120, interval=1, what="payment_orders PAID (worker)")
            we = L.q("select status, signature_valid, provider_status from webhook_events where payment_order_id=:i", i=po_id)
            assert we and all(w[1] is True for w in we) and any(w[0] == "PROCESSED" for w in we), we

            async def _em_analise():
                return L.q1("select status from campaigns where id=:i", i=camp_id)[0] == "EM_ANALISE"
            await L.wait_until(_em_analise, timeout=60, interval=1, what="campanha EM_ANALISE")
            h = L.q1("select status from inventory_holds where id=:i", i=ctx["hold_id"])
            bs = L.q("select status, count(*) from booking_screens where campaign_id=:c group by 1", c=camp_id)
            led = L.q("select t.kind, sum(p.amount_cents) from ledger_transactions t join ledger_postings p "
                      "on p.transaction_id=t.id where t.ref_id=:r or t.external_ref=:r or t.ref_id=:o group by t.id, t.kind",
                      r=po_id, o=order_id)
            assert h[0] == "CONVERTED", h
            assert all(x[1] == 0 for x in led) and led, led
            s.ev.update(webhook_events=we, payment_status="PAID", campaign_status="EM_ANALISE", hold_status=h[0],
                        booking_screens=bs, ledger_txs=led,
                        ledger_new_tx=L.q1("select count(*) from ledger_transactions")[0] - led_before)

        # 12 ─ aprovação RD (modo MANUAL: pagamento não aprova)
        with step(12, "aprovação RD (modo MANUAL)") as s:
            r = await adm.get("/api/v1/campaigns/approval-policies")
            pol = r.json()
            org_pol = next((i for i in pol.get("items", []) if i.get("org_id") == adv_org), None)
            effective = (org_pol or {}).get("mode") or pol.get("global_mode")
            assert effective == "MANUAL", pol
            r = await adm.get("/api/v1/campaigns/review-queue")
            assert r.status_code == 200 and camp_id in r.text, r.text[:300]
            n_before = len(pws.msgs)
            r = await adm.post(f"/api/v1/campaigns/{camp_id}/approve", json={"note": "Aprovado pelo QA AÇÃO 002 (E2E)."})
            assert r.status_code == 200, r.text

            async def _approved():
                st = L.q1("select status from campaigns where id=:i", i=camp_id)[0]
                return st if st in ("APROVADA", "AGENDADA", "SINCRONIZANDO", "ATIVA") else None
            st = await L.wait_until(_approved, timeout=60, interval=1, what="campanha aprovada")
            hist = L.q("select from_status, to_status, trigger from campaign_status_history where campaign_id=:c "
                       "order by created_at", c=camp_id)
            s.ev.update(effective_policy=effective, status_after_approve=st, history=hist)
            ctx["ws_mark"] = n_before

        # 13 ─ manifesto gerado/assinado (assinatura verificada com a chave pública do servidor)
        with step(13, "manifesto gerado e assinado (verificação ES256)") as s:
            sync = await pws.wait_type("sync_required", 90, after=ctx["ws_mark"])

            async def _manifest_with_creative():
                rr = await pclient.get("/api/v1/player/manifest", headers=player.headers)
                if rr.status_code != 200:
                    return None
                env = rr.json()
                crs = env["manifest"]["creatives"]
                return (rr, env) if any(c["creative_id"] == creative_id for c in crs.values()) else None
            rr, env = await L.wait_until(_manifest_with_creative, timeout=90, interval=2, what="manifesto com o criativo")
            man = env["manifest"]
            keys = {k["key_id"]: k["public_key"] for k in player.server_keys}
            r = await adm.get("/api/v1/scheduler/public-keys")
            for k in r.json()["keys"]:
                keys.setdefault(k["key_id"], k["public_key"])
            assert env["alg"] == "ES256" and env["key_id"] in keys, (env["alg"], env["key_id"], list(keys))
            assert proto.verify_es256(keys[env["key_id"]], man, env["signature"]) is True
            tampered = json.loads(json.dumps(man))
            tampered["version"] = man["version"] + 1
            assert proto.verify_es256(keys[env["key_id"]], tampered, env["signature"]) is False
            assert rr.headers.get("etag") == f'"{man["version"]}"'
            r304 = await pclient.get("/api/v1/player/manifest", headers={**player.headers, "If-None-Match": f'"{man["version"]}"'})
            assert r304.status_code == 304
            ckey = next(k for k, c in man["creatives"].items() if c["creative_id"] == creative_id)
            cr = man["creatives"][ckey]
            mkey = cr["media"]
            assert man["media"][mkey]["sha256"] == ctx["video_sha"] and man["timezone"] == TZ_EXPECTED
            n_ext = sum(1 for es in man["external_plan"].values() for e in es if e["c"] == ckey)
            assert n_ext > 0
            ctx.update(manifest=man, ckey=ckey, mkey=mkey)
            s.ev.update(sync_required=sync.get("data"), version=man["version"], key_id=env["key_id"], signature_valid=True,
                        tampered_rejected=True, etag_304=True, timezone=man["timezone"], creative_key=ckey,
                        external_entries_for_creative=n_ext, rotation=man["rotation"], anchor_ms=man.get("anchor_ms"),
                        label=cr.get("label"), valid_until_ms=cr.get("valid_until_ms"))

        # 14 ─ sincronização: download com Range (2 partes, If-Range) + sha256; manifest_applied
        with step(14, "player sincroniza (mídia com Range; sha256 confere; manifest_applied)") as s:
            man = ctx["manifest"]
            media = man["media"][ctx["mkey"]]
            size = media["size"]
            half = size // 2
            r1 = await pclient.get(f"/api/v1/player/media/{media['media_id']}",
                                   headers={**player.headers, "Range": f"bytes=0-{half - 1}"})
            assert r1.status_code == 206, r1.status_code
            etag = r1.headers.get("etag")
            assert etag == f'"{media["sha256"]}"', etag
            r2 = await pclient.get(f"/api/v1/player/media/{media['media_id']}",
                                   headers={**player.headers, "Range": f"bytes={half}-", "If-Range": etag})
            assert r2.status_code == 206, r2.status_code
            data = r1.content + r2.content
            assert len(data) == size and hashlib.sha256(data).hexdigest() == media["sha256"]
            r_bad = await pclient.get(f"/api/v1/player/media/{uuid.uuid4()}", headers=player.headers)
            assert r_bad.status_code == 404
            hb_state.update(state="PLAYING", manifest_version_applied=man["version"], last_sync_ms=L.now_ms())
            await pws.send("manifest_applied", {"version": man["version"]})

            async def _active():
                st = L.q1("select status from campaigns where id=:i", i=camp_id)[0]
                return st if st == "ATIVA" else None
            await L.wait_until(_active, timeout=60, interval=1, what="campanha ATIVA após ack do player")
            dev = L.q1("select applied_manifest_version, presence from devices where id=:i", i=player.device_id)
            mv = L.q1("select applied_at is not null, applied_by_device::text from manifest_versions where screen_id=:s and version=:v",
                      s=screen_id, v=man["version"])
            assert mv[0] and mv[1] == player.device_id, mv
            s.ev.update(content_range_1=r1.headers.get("content-range"), content_range_2=r2.headers.get("content-range"),
                        bytes=len(data), sha256_ok=True, other_media_404=True, campaign_status="ATIVA",
                        device_applied_version=dev[0], presence=dev[1], manifest_applied_by_device=True)

        # 15 ─ "reproduz": âncora + resolve() + evento PoP com 100 % da duração
        with step(15, "reproduz (âncora + resolve() + PoP 100 %)") as s:
            man = ctx["manifest"]
            anchor = man.get("anchor_ms")
            if anchor is None:
                want = (L.now_ms() // 1000) * 1000
                r = await pclient.post("/api/v1/player/anchor", json={"anchor_ms": want}, headers=player.headers)
                assert r.status_code == 200, r.text
                anchor = r.json()["anchor_ms"]
            r = await pclient.post("/api/v1/player/anchor", json={"anchor_ms": anchor + 5000}, headers=player.headers)
            assert r.status_code == 200 and r.json()["anchor_ms"] == anchor, r.text  # write-once
            local_state = {"revoked": [], "media_ready": [ctx["mkey"]]}
            t = L.now_ms() + 2000
            seg = None
            limit = t + 3 * man["rotation"]["cycle_ms"]
            while t < limit:
                sg = resolve(man, anchor, local_state, t)
                if sg.block == "EXTERNAL" and sg.kind == "ITEM" and sg.creative_key == ctx["ckey"] and sg.start_ms >= L.now_ms():
                    seg = sg
                    break
                t = max(t + 1000, sg.end_ms)
            assert seg is not None, "resolve() não encontrou inserção do criativo em 3 ciclos"
            wait_s = (seg.end_ms - L.now_ms()) / 1000 + 1.5
            s.ev.update(anchor_ms=anchor, anchor_write_once=True, segment=dict(block=seg.block, kind=seg.kind,
                        creative_key=seg.creative_key, start_ms=seg.start_ms, end_ms=seg.end_ms), wait_s=round(wait_s, 1))
            hb_state.update(current={"creative_code": ctx["creative_code"], "started_at_ms": seg.start_ms})
            await asyncio.sleep(max(0.0, wait_s))
            # inserção exata do bloco (mesmo mapeamento bucket/horizonte do resolve)
            insertion_id = _insertion_for(man, anchor, seg, ctx["ckey"])
            assert insertion_id, "entrada do external_plan correspondente ao segmento não encontrada"
            expected = seg.end_ms - seg.start_ms
            assert expected == man["creatives"][ctx["ckey"]]["duration_ms"] == 30000
            ev = player.event(man, ctx["ckey"], start=seg.start_ms, expected=expected, actual=expected,
                              insertion_id=insertion_id)
            ctx["pop_event"] = ev
            s.ev.update(playback_uuid=ev["playback_uuid"], expected_ms=expected, actual_ms=ev["actual_ms"],
                        result=ev["result"], insertion_id=ev["insertion_id"])

        # 16 ─ envio do lote de PoP
        with step(16, "envia PoP em lote (events:batch)") as s:
            item = player.signed(ctx["pop_event"])
            ctx["pop_item"] = item
            r = await pclient.post("/api/v1/player/events:batch", json={"events": [item]}, headers=player.headers)
            assert r.status_code == 200, r.text
            res = r.json()
            s.ev.update(response=res)
            assert res["results"][0]["status"] == "ACCEPTED" and res["results"][0]["valid"] is True, res
            assert res["ack_upto_seq"] == ctx["pop_event"]["seq"], res

        # 17 ─ backend valida (estado no banco)
        with step(17, "backend valida PoP (ACCEPTED/valid no banco)") as s:
            pe = L.q1("select status, valid, reason, kind, actual_ms, expected_ms, financial_status, local_day::text, campaign_id::text "
                      "from playback_events where playback_uuid=:u", u=ctx["pop_event"]["playback_uuid"])
            assert pe and pe[0] == "ACCEPTED" and pe[1] is True and pe[2] is None and pe[8] == camp_id, pe
            s.ev.update(playback_event=dict(zip(["status", "valid", "reason", "kind", "actual_ms", "expected_ms",
                                                 "financial_status", "local_day", "campaign_id"], pe)))

        # 18 ─ comissão apropriada (job real deliver_financial a cada 60 s)
        with step(18, "comissão apropriada (accrual pelo worker; fechamento: close-due)") as s:
            pu = ctx["pop_event"]["playback_uuid"]

            async def _accrued():
                return L.q1("select counted from commission_accrual_events where playback_uuid=:u", u=pu)
            t18 = time.monotonic()
            ace = await L.wait_until(_accrued, timeout=240, interval=3, what="commission_accrual_events (deliver_financial)")
            assert ace[0] is True, ace
            acc = L.q("select establishment_id::text, local_day::text, competence::text, valid_count, expected_count, amount_ucents, "
                      "terms_id::text from commission_accruals where campaign_id=:c", c=camp_id)
            assert acc and acc[0][0] == est_id and acc[0][3] >= 1 and acc[0][5] > 0, acc
            cc = L.q1("select model, pool_bps, eligible_revenue_cents, pool_cents, status, kind, start_date::text, end_date::text, "
                      "expected_total from commission_campaigns where campaign_id=:c and payment_order_id=:p", c=camp_id, p=po_id)
            assert cc and cc[5] == "INITIAL" and cc[1] == 500 and cc[3] == cc[2] * 500 // 10000, cc
            dp = L.q("select local_day::text, e_day from commission_day_plans d join commission_campaigns t on t.id=d.terms_id "
                     "where t.campaign_id=:c", c=camp_id)
            pe = L.q1("select financial_status from playback_events where playback_uuid=:u", u=pu)
            r = await fin.get("/api/v1/financial/commissions", params={"campaign_id": camp_id})
            assert r.status_code == 200, r.text
            r_close = await fin.post("/api/v1/financial/commissions/close-due")
            assert r_close.status_code == 200, r_close.text
            ctx["accrual_snapshot"] = acc
            s.ev.update(wait_s=round(time.monotonic() - t18, 1), accrual_event_counted=ace[0], accruals=acc,
                        commission_terms=dict(zip(["model", "pool_bps", "eligible_revenue_cents", "pool_cents", "status", "kind",
                                                           "start_date", "end_date", "expected_total"], cc)),
                        day_plans=dp,
                        pop_financial_status=pe[0], api_commissions=r.json() if len(r.text) < 1500 else r.text[:1500],
                        close_due=r_close.json(),
                        note="competência corrente só fecha após 23:59:59 do último dia (SP); fechamento validado em clone descartável")

        # 19 ─ relatório de exibições + comprovante PDF verificável
        with step(19, "relatório de exibições + comprovante PDF verificável", fatal=False) as s:
            r = await adv.post("/api/v1/reports/proofs", json={"campaign_id": camp_id})
            assert r.status_code in (200, 201), r.text
            proof = r.json()
            proof_id = proof["id"]
            log.created["proof"] = proof_id
            r = await adv.get(f"/api/v1/reports/proofs/{proof_id}/pdf")
            assert r.status_code == 200 and r.content[:5] == b"%PDF-", r.status_code
            pdf = r.content
            rv = await adv.post("/api/v1/reports/proofs/verify", files={"file": ("comprovante.pdf", pdf, "application/pdf")})
            assert rv.status_code == 200, rv.text
            ver = rv.json()
            tampered = pdf.replace(b"%%EOF", b"%%EOF\n% x", 1)
            rt = await adv.post("/api/v1/reports/proofs/verify", files={"file": ("comprovante.pdf", tampered, "application/pdf")})
            code = proof.get("code") or proof.get("verification_code")
            rc = await adv.get(f"/api/v1/reports/proofs/{code}/verification") if code else None

            async def _report():
                rr = await adv.get("/api/v1/reports/data/exibicoes_por_campanha",
                                   params={"campaign_id": camp_id, "org_id": adv_org, "audience": "ANUNCIANTE",
                                           "start": today_local.isoformat(), "end": today_local.isoformat()})
                if rr.status_code != 200:
                    raise L.E2EError(f"relatório HTTP {rr.status_code} {L.err_code(rr)}")
                body = rr.json()
                return body if body.get("rows") else None
            t19 = time.monotonic()
            rep = await L.wait_until(_report, timeout=420, interval=10, what="relatório de exibições (agregação 5 min)")
            s.ev.update(proof={k: proof.get(k) for k in ("id", "code", "plays_valid", "valid_plays", "total_valid", "status",
                                                         "sha256", "sources") if k in proof},
                        pdf_bytes=len(pdf), verify=ver, verify_tampered=(rt.status_code, rt.text[:300]),
                        verification_page=(rc.status_code if rc is not None else None),
                        report_wait_s=round(time.monotonic() - t19, 1), report=json.dumps(rep, ensure_ascii=False)[:1200])
            assert ver.get("valid") is True or ver.get("status") in ("VALID", "VALIDO"), ver

        # 20 ─ conciliação + ledger balanceado
        with step(20, "financeiro concilia (conciliação do dia; ledger balanceado)", fatal=False) as s:
            r = await fin.post("/api/v1/financial/reconciliation/runs")
            assert r.status_code in (200, 201), r.text
            run = r.json()
            items = await fin.get("/api/v1/financial/reconciliation/items")
            unbalanced = L.q("select transaction_id::text, sum(amount_cents) from ledger_postings group by 1 having sum(amount_cents) <> 0")
            total = L.q1("select coalesce(sum(amount_cents),0), count(*) from ledger_postings")
            cash = L.q1("select coalesce(sum(p.amount_cents),0) from ledger_postings p join ledger_accounts a on a.id=p.account_id "
                        "where a.code in ('RD:Cash:CloudPayPag','RD:Cash:CloudPayPag:Blocked')")
            div = L.q("select kind, dedupe_key, internal_amount_cents, external_amount_cents, status from reconciliation_items "
                      "where status='DIVERGENT'")
            s.ev.update(run={k: run.get(k) for k in ("id", "status", "provider_balance_cents", "internal_cash_cents", "summary")},
                        ledger_unbalanced_transactions=unbalanced, ledger_sum_all=total[0], ledger_postings=total[1],
                        internal_cash_cents=cash[0], divergent_items=div,
                        items_http=items.status_code)
            mine = L.q("select kind, dedupe_key, status from reconciliation_items where run_id=:r", r=run.get("id"))
            s.ev["items_opened_by_this_run"] = mine
            assert not unbalanced and total[0] == 0
            assert run.get("status") == "COMPLETED", run
            # itens DIVERGENT de dias/execuções anteriores são históricos; o critério é ESTA conciliação fechar
            assert run.get("provider_balance_cents") == run.get("internal_cash_cents"), run
            assert not mine and (run.get("summary") or {}).get("items_opened", 0) == 0, mine

        # B ─ idempotência no stack vivo: PoP duplicado e webhook repetido (rota pública, mesmo corpo assinado)
        with step("B-idem", "idempotência: PoP duplicado + webhook repetido (comissão não duplica)", fatal=False) as s:
            acc_before = L.q("select valid_count, amount_ucents from commission_accruals where campaign_id=:c order by local_day",
                             c=camp_id)
            r = await pclient.post("/api/v1/player/events:batch", json={"events": [ctx["pop_item"]]}, headers=player.headers)
            assert r.status_code == 200 and r.json()["results"][0]["status"] == "DUPLICATE", r.text
            from app.modules.financial import cloudpaypag as cpp

            cfg = cpp.get_config()
            o = L.q1("select provider_external_ref, amount_cents, method from payment_orders where id=:i", i=po_id)
            raw, hdrs = cpp.FakeCloudPayPag(cfg).build_webhook(status="paid", external_ref=o[0], order_id=po_id,
                                                               amount_cents=o[1], fee_cents=0, method=o[2])
            w1 = await pclient.post(f"/api/v1/webhooks/cloudpaypag/{cfg.instance_slug}", content=raw, headers=hdrs)
            w2 = await pclient.post(f"/api/v1/webhooks/cloudpaypag/{cfg.instance_slug}", content=raw, headers=hdrs)
            bad = await pclient.post(f"/api/v1/webhooks/cloudpaypag/{cfg.instance_slug}", content=raw + b" ",
                                     headers={**hdrs, cpp.SIGNATURE_HEADER: "sha256=" + "0" * 64})
            await asyncio.sleep(75)  # ≥ 1 ciclo do deliver_financial (60 s) e processamento do webhook
            acc_after = L.q("select valid_count, amount_ucents from commission_accruals where campaign_id=:c order by local_day",
                            c=camp_id)
            pay_tx = L.q1("select count(*) from ledger_transactions where kind like 'PAYMENT%' and (ref_id=:p or external_ref=:p)",
                          p=po_id)
            we = L.q("select status, count(*) from webhook_events where payment_order_id=:i group by 1", i=po_id)
            s.ev.update(pop_duplicate="DUPLICATE", webhook_first=(w1.status_code, w1.text[:200]),
                        webhook_repeat=(w2.status_code, w2.text[:200]), webhook_bad_signature=bad.status_code,
                        accruals_before=acc_before, accruals_after=acc_after, payment_ledger_txs=pay_tx[0], webhook_events=we)
            assert acc_after == acc_before, (acc_before, acc_after)
            assert bad.status_code in (400, 401, 403), bad.status_code
            assert w2.status_code in (200, 202) and ("DUPLICATE" in w2.text.upper() or w2.json().get("duplicate") is True), w2.text
            assert L.q1("select status from payment_orders where id=:i", i=po_id)[0] == "PAID"

        # REPRODUZINDO (derivado de PoP recente)
        with step("extra", "campanha exibida como REPRODUZINDO após PoP válido") as s:
            r = await adv.get(f"/api/v1/campaigns/{camp_id}")
            c = r.json()
            s.ev.update(status=c.get("status"), playing=c.get("playing"), display_status=c.get("display_status"),
                        last_valid_play_at=c.get("last_valid_play_at"))
    finally:
        extra = {"ctx": {k: v for k, v in ctx.items() if k not in ("manifest", "pop_item", "pop_event", "accrual_snapshot")}}
        path = log.dump(extra)
        print(f"[E2E] relatório: {path}", flush=True)
        if pws is not None:
            await pws.close()
        for c in (adm, fin, adv):
            if c is not None:
                await c.aclose()
        await pclient.aclose()
    failed = [x for x in log.steps if x["result"] != "PASS"]
    assert not failed, failed


def _insertion_for(man: dict, anchor: int, seg, ckey: str):
    """Entrada de ``external_plan`` que corresponde ao segmento (mesma regra de bucket/horizonte do resolve)."""
    C = man["rotation"]["cycle_ms"]
    I = man["rotation"]["internal_ms"]
    U = man["rotation"]["unit_ms"]
    k = (seg.start_ms - anchor) // C
    s_ext = anchor + k * C + I
    b = (s_ext // C) * C
    h = man["horizon"]
    if not (h["from_ms"] <= b < h["to_ms"]):
        P = h["repeat_period_ms"]
        while b >= h["to_ms"]:
            b -= P
        while b < h["from_ms"]:
            b += P
    for e in man["external_plan"].get(str(b), []):
        if e["c"] == ckey and s_ext + e["u"] * U == seg.start_ms:
            return e["i"]
    return None
