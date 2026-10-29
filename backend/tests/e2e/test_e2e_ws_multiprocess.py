"""
B (P002 itens 52–53) — WebSocket MULTI-PROCESSO real.

Instância TEMPORÁRIA do backend com a imagem de PRODUÇÃO e ``--workers 4`` (``RD_E2E_WS_BASE_URL``, rede
interna, sem porta publicada), apontando para o MESMO PostgreSQL/Redis do ambiente. Vários players simulados
(ticket) e um admin (ticket) conectados a ela, distribuídos entre os processos (prova pelo registro
``ws:proc:<host>:<pid>`` no Redis). Publicação de ``sync_required``, ``command`` e ``kill`` a partir:
(i) da API (da instância temporária e do rd-backend de dev — outro container), (ii) de uma task Celery
executada no rd-worker REAL (``publish_sync_required``). Verifica entrega a todos, reconexão com backlog de
comandos até o ack final, autenticação (ticket reutilizado/inválido/de outro tipo) e erro tratado.

Pulado sem ``RD_E2E_BASE_URL`` e ``RD_E2E_WS_BASE_URL``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest

from tests.e2e import live_support as L
from tests.e2e.test_e2e_full_flow import PlayerWS

pytestmark = [pytest.mark.e2e]

WS_BASE = os.environ.get("RD_E2E_WS_BASE_URL", "").rstrip("/")
WS_HOST = os.environ.get("RD_E2E_WS_HOST", "")  # hostname do container temporário (prefixo do PROC_ID)
N_PLAYERS = int(os.environ.get("RD_E2E_WS_PLAYERS", "8"))
# HTTP dos players direto no rd-backend de dev (rede interna): o rate limit de pareamento da borda nginx
# (20/min por IP) não é o objeto deste teste e o contêiner de teste é um único IP para N players.
PLAYER_API = os.environ.get("RD_E2E_PLAYER_API", "http://backend:8000")


def _live_redis():
    import redis

    return redis.Redis.from_url(L.live_redis_url(), socket_timeout=5, decode_responses=True)


def _proc_counts() -> dict:
    r = _live_redis()
    out = {}
    for k in r.scan_iter(match=f"{L.LIVE_REDIS_PREFIX}ws:proc:*", count=500):
        v = r.get(k)
        if v:
            out[k[len(L.LIVE_REDIS_PREFIX) + len("ws:proc:"):]] = json.loads(v)
    return out


@pytest.mark.skipif(not WS_BASE, reason="defina RD_E2E_WS_BASE_URL (instância temporária de produção)")
async def test_ws_multiprocess_delivery_backlog_auth():  # noqa: C901
    from websockets.asyncio.client import connect as ws_connect

    log = L.StepLog("B52_ws_multiprocess")
    tag = L.run_tag()
    adm_p = await L.login("admin", base_url=WS_BASE)   # API servida pela instância de PRODUÇÃO (4 workers)
    adm_d = await L.login("admin")                    # API do rd-backend de dev (outro container), via nginx
    players: list[L.LivePlayer] = []
    conns: list[PlayerWS] = []
    admin_ws = None
    kill_id = None
    hb = {"state": "PLAYING", "current": None, "manifest_version_applied": None, "manifest_version_pending": None,
          "download": None, "last_sync_ms": None, "last_play_ms": None,
          "storage": {"free_bytes": 1, "used_bytes": 0, "persistent": True}, "clock_offset_ms": 0, "alerts": []}

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
        with step("52.0", f"instância de produção: {N_PLAYERS} telas criadas pela API dela + pareamento") as s:
            h = await adm_p.client.get("/health")
            assert h.status_code == 200, h.text
            r = await adm_p.post("/api/v1/organizations", json={
                "name": f"{L.PREFIX}parceiroWS_{tag}", "document": L.valid_cnpj(True), "document_type": "CNPJ",
                "business_roles": ["PARTNER"], "city": "Boa Vista", "state": "RR", "email": f"qa002.ws.{tag}@example.com",
                "phone": "95999990000"})
            assert r.status_code == 201, r.text
            org = r.json()["id"]
            r = await adm_p.post("/api/v1/establishments", json={
                "org_id": org, "name": f"{L.PREFIX}estabWS_{tag}", "city": "Boa Vista", "state": "RR",
                "postal_code": "69301000", "lat": 2.8235, "lng": -60.6758, "geo_source": "MANUAL"})
            assert r.status_code == 201, r.text
            est = r.json()["id"]
            screens = []
            for i in range(N_PLAYERS):
                r = await adm_p.post("/api/v1/screens", json={"establishment_id": est, "name": f"{L.PREFIX}telaWS{i}_{tag}",
                                                               "orientation": "HORIZONTAL", "resolution_w": 1920,
                                                               "resolution_h": 1080})
                assert r.status_code == 201, r.text
                screens.append(r.json()["id"])
            for sid in screens:
                # HTTP do player pelo rd-backend de dev: o resultado do pareamento devolve a chave pública do
                # manifesto, cujo arquivo de dev (volume de mídia, 0600 root) não é legível pelo usuário rdapp da
                # imagem de produção — ver relatório. Os WebSockets vão para a instância de produção.
                p = L.LivePlayer(L.new_client(PLAYER_API))
                await p.request_pairing()
                r = await adm_p.post(f"/api/v1/screens/{sid}/pairing", json={"pairing_code": p.pairing_code})
                assert r.status_code == 201, r.text
                assert (await p.fetch_result()).status_code == 200
                assert (await p.authenticate()).status_code == 200
                players.append(p)
            log.created.update(org=org, establishment=est, screens=screens, devices=[p.device_id for p in players])
            s.ev.update(org=org, establishment=est, screens=len(screens), devices=len(players))

        with step("52.1", "players e admin conectados (ticket) e distribuídos entre os processos") as s:
            tickets = {}
            for p in players:
                t = await p.ws_ticket()
                tickets[p.device_id] = t
                c = await PlayerWS(L.ws_url(WS_BASE, f"/ws/player?ticket={t}"), L.PLAYER_ORIGIN).connect()
                c.start_heartbeat(lambda: hb)
                conns.append(c)
            for c in conns:
                await c.wait_type("server_time", 10)
            r = await adm_p.post("/api/v1/realtime/ticket")
            assert r.status_code == 200, r.text
            admin_ticket = r.json()["ticket"]
            admin_ws = await PlayerWS(L.ws_url(WS_BASE, f"/ws/admin?ticket={admin_ticket}"), L.PANEL_ORIGIN).connect()
            await asyncio.sleep(17)  # cada processo publica ws:proc a cada 15 s
            counts = _proc_counts()
            mine = {k: v for k, v in counts.items() if WS_HOST and k.startswith(WS_HOST + ":")}
            with_players = {k: v.get("player", 0) for k, v in mine.items() if v.get("player", 0) > 0}
            s.ev.update(proc_keys_temp_instance=len(mine), players_by_process=with_players,
                        admins_by_process={k: v.get("admin", 0) for k, v in mine.items() if v.get("admin", 0)},
                        other_processes=[k for k in counts if k not in mine])
            ctx_tickets = tickets
            assert len(mine) == 4, mine  # 4 workers uvicorn registrados
            assert sum(with_players.values()) >= N_PLAYERS and len(with_players) >= 2, with_players

        mark = [len(c.msgs) for c in conns]

        with step("52.2", "sync_required publicado pela API do rd-backend de DEV (outro container) → todos") as s:
            versions = {}
            for p in players:
                r = await adm_d.post(f"/api/v1/scheduler/screens/{p.screen_id}/manifests/regenerate", json={"force": True})
                versions[p.screen_id] = (r.status_code, (r.json() or {}).get("version") if r.status_code == 200 else r.text[:120])
                assert r.status_code == 200, r.text
            got = []
            for c, m in zip(conns, mark):
                msg = await c.wait_type("sync_required", 30, after=m)
                got.append(msg["data"].get("version"))
            s.ev.update(regenerate=list(versions.values())[:3], delivered=len(got), versions=got)
            assert len(got) == N_PLAYERS

        mark = [len(c.msgs) for c in conns]
        with step("52.3", "sync_required publicado por task Celery executada no rd-worker REAL → todos") as s:
            from celery import Celery

            from app.core.config import settings

            capp = Celery("qa-e2e", broker=settings.CELERY_BROKER_URL)
            ids = []
            for p in players:
                res = capp.send_task("app.workers.tasks.playback.publish_sync_required", args=[p.screen_id, 990001],
                                     queue="default")
                ids.append(res.id)
            got = []
            for c, m in zip(conns, mark):
                msg = await c.wait_type("sync_required", 60, after=m)
                got.append(msg["data"].get("version"))
            s.ev.update(task_ids=ids[:2], delivered=len(got), versions=sorted(set(got)))
            assert got == [990001] * N_PLAYERS

        mark = [len(c.msgs) for c in conns]
        with step("52.4", "command publicado pela API da instância de produção → todos; ack DONE") as s:
            cmd_ids = {}
            for p in players:
                r = await adm_p.post(f"/api/v1/screens/{p.screen_id}/commands",
                                     json={"command": "reload_manifest", "reason": f"{L.PREFIX} teste WS multi-processo"},
                                     headers={"Idempotency-Key": str(uuid.uuid4())})
                assert r.status_code in (200, 201, 202), r.text
                cmd_ids[p.screen_id] = r.json().get("id") or r.json().get("command_id")
            got = 0
            for p, c, m in zip(players, conns, mark):
                msg = await c.wait_type("command", 30, after=m)
                assert msg["data"]["command"] == "reload_manifest"
                await c.send("command_ack", {"command_id": msg["data"]["command_id"], "status": "DONE", "detail": "ok"})
                got += 1
            await asyncio.sleep(2)
            rows = L.q("select c.status, count(*) from device_commands c join screens s on s.id=c.screen_id "
                       "where s.establishment_id=:e group by 1", e=log.created["establishment"])
            s.ev.update(delivered=got, command_status=rows)
            assert got == N_PLAYERS and dict(rows).get("DONE", 0) >= N_PLAYERS

        mark = [len(c.msgs) for c in conns]
        with step("52.5", "kill publicado pela API do rd-backend de DEV → todos (na instância de produção)") as s:
            cr = L.q1("select id::text from creatives where name like :p order by created_at desc limit 1", p=f"{L.PREFIX}%")
            assert cr, "nenhum criativo QA para o kill"
            # kill regenera manifestos (assinatura): feito pelo dev — a chave de manifesto de dev não é legível pelo
            # usuário da imagem de produção (ver relatório); a entrega aos players continua sendo cross-processo.
            r = await adm_d.post("/api/v1/playback/kills", json={"creative_id": cr[0], "screen_ids": [p.screen_id for p in players],
                                                                "reason": f"{L.PREFIX} teste WS multi-processo (kill)"})
            assert r.status_code in (200, 201), r.text
            kill_id = r.json().get("id")
            got = []
            for c, m in zip(conns, mark):
                msg = await c.wait_type("kill", 30, after=m)
                got.append(cr[0] in (msg["data"].get("creative_ids") or []))
            s.ev.update(kill_id=kill_id, delivered=sum(got))
            assert all(got) and len(got) == N_PLAYERS

        with step("52.6", "admin (ticket) recebeu eventos de presença/status das telas") as s:
            types = {}
            for m in admin_ws.msgs:
                types[m.get("type")] = types.get(m.get("type"), 0) + 1
            s.ev.update(admin_event_types=types)
            assert any(t and t.startswith("screen.") for t in types), types

        with step("53.1", "reconexão com backlog: comando emitido offline é reentregue até o ack final") as s:
            p0, c0 = players[0], conns[0]
            await c0.close()
            await asyncio.sleep(1)
            r = await adm_p.post(f"/api/v1/screens/{p0.screen_id}/commands",
                                 json={"command": "resync", "reason": f"{L.PREFIX} backlog"},
                                 headers={"Idempotency-Key": str(uuid.uuid4())})
            assert r.status_code in (200, 201, 202), r.text
            c1 = await PlayerWS(L.ws_url(WS_BASE, f"/ws/player?ticket={await p0.ws_ticket()}"), L.PLAYER_ORIGIN).connect()
            m1 = await c1.wait_type("command", 20)
            cid = m1["data"]["command_id"]
            await c1.send("command_ack", {"command_id": cid, "status": "RECEIVED", "detail": "recebido"})
            await asyncio.sleep(1)
            await c1.close()
            c2 = await PlayerWS(L.ws_url(WS_BASE, f"/ws/player?ticket={await p0.ws_ticket()}"), L.PLAYER_ORIGIN).connect()
            m2 = await c2.wait_type("command", 20)
            assert m2["data"]["command_id"] == cid
            await c2.send("command_ack", {"command_id": cid, "status": "DONE", "detail": "ok"})
            await asyncio.sleep(1.5)
            await c2.close()
            c3 = await PlayerWS(L.ws_url(WS_BASE, f"/ws/player?ticket={await p0.ws_ticket()}"), L.PLAYER_ORIGIN).connect()
            await c3.wait_type("server_time", 10)
            await asyncio.sleep(3)
            redelivered = [m for m in c3.of_type("command") if m["data"].get("command_id") == cid]
            row = L.q1("select status, delivery_count from device_commands where id=:i", i=cid)
            conns[0] = c3
            s.ev.update(command_id=cid, after_received_redelivered=True, after_done_redelivered=len(redelivered),
                        db=row)
            assert not redelivered and row[0] == "DONE" and row[1] >= 2, row

        with step("53.2", "autenticação: ticket reutilizado / inválido / de outro tipo → recusado") as s:
            results = {}

            async def _try(url, origin):
                try:
                    ws = await ws_connect(url, additional_headers={"Origin": origin}, open_timeout=10)
                except Exception as exc:
                    return f"handshake:{type(exc).__name__}:{getattr(getattr(exc, 'response', None), 'status_code', '')}"
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                    await ws.close()
                    return "ACEITO"
                except Exception:
                    return f"fechado:{ws.close_code}"
            used = ctx_tickets[players[1].device_id]
            results["reused_player_ticket"] = await _try(L.ws_url(WS_BASE, f"/ws/player?ticket={used}"), L.PLAYER_ORIGIN)
            results["invalid_ticket"] = await _try(L.ws_url(WS_BASE, "/ws/player?ticket=" + "x" * 43), L.PLAYER_ORIGIN)
            pt = await players[2].ws_ticket()
            results["player_ticket_on_admin_ws"] = await _try(L.ws_url(WS_BASE, f"/ws/admin?ticket={pt}"), L.PANEL_ORIGIN)
            at = (await adm_p.post("/api/v1/realtime/ticket")).json()["ticket"]
            results["admin_ticket_on_player_ws"] = await _try(L.ws_url(WS_BASE, f"/ws/player?ticket={at}"), L.PLAYER_ORIGIN)
            pt2 = await players[3].ws_ticket()
            results["hostile_origin"] = await _try(L.ws_url(WS_BASE, f"/ws/player?ticket={pt2}"), "https://evil.example")
            s.ev.update(results=results)
            assert all(v != "ACEITO" for v in results.values()), results

        with step("53.3", "erro tratado: frame inválido/tipo desconhecido/ack malformado não derrubam o processo") as s:
            c = conns[4]
            await c._ws.send("isto não é json")
            await c.send("tipo_inexistente", {"x": 1})
            await c.send("command_ack", {"command_id": "nao-e-uuid", "status": "DONE"})
            await c.send("heartbeat", hb)
            await asyncio.sleep(2)
            alive = c.closed_code is None
            h = await adm_p.client.get("/health")
            pres = await adm_p.get(f"/api/v1/screens/{players[4].screen_id}/presence")
            s.ev.update(connection_alive=alive, health=h.status_code, presence_http=pres.status_code,
                        presence=(pres.json() if pres.status_code == 200 else None))
            assert h.status_code == 200
        with step("54.1", "erros do player (media_error / manifest_failed) chegam ao painel e ao relatório") as s:
            p5, c5 = players[5], conns[5]
            amark = len(admin_ws.msgs)
            fake_media = str(uuid.uuid4())
            await c5.send("manifest_failed", {"version": 1, "reason": "SIGNATURE_INVALID"})
            await c5.send("media_error", {"media_id": fake_media, "reason": "SHA256_MISMATCH", "attempts": 5})
            await asyncio.sleep(3)
            dev = L.q1("select last_error from devices where id=:i", i=p5.device_id)
            sync_ev = L.q("select type, reason from device_sync_events where device_id=:i and type in "
                          "('MANIFEST_FAILED','MEDIA_ERROR') order by occurred_at", i=p5.device_id)
            adm_types = [m.get("type") for m in admin_ws.msgs[amark:]]
            err_evt = [m for m in admin_ws.msgs[amark:] if "error" in json.dumps(m.get("data", {})).lower()
                       or "failed" in json.dumps(m.get("data", {})).lower()]
            pres = await adm_d.get(f"/api/v1/screens/{p5.screen_id}/presence")
            from datetime import date as _d
            rep = await adm_d.get("/api/v1/reports/data/sincronizacao", params={"start": _d.today().isoformat(),
                                                                                "end": _d.today().isoformat()})
            s.ev.update(device_last_error=dev[0] if dev else None, sync_events=sync_ev, admin_types=adm_types,
                        admin_error_events=len(err_evt), presence_error=(pres.json().get("live") or {}).get("error")
                        if pres.status_code == 200 else pres.status_code,
                        report_sincronizacao=(rep.status_code, rep.text[:300]))
            assert dev and dev[0] and "media_error" in dev[0]
            assert {t for t, _ in sync_ev} >= {"MANIFEST_FAILED", "MEDIA_ERROR"}, sync_ev
            assert err_evt, adm_types

    finally:
        if kill_id:
            try:
                await adm_d.post(f"/api/v1/playback/kills/{kill_id}/lift", json={"reason": f"{L.PREFIX} fim do teste"})
            except Exception:
                pass
        for c in conns:
            try:
                await c.close()
            except Exception:
                pass
        if admin_ws is not None:
            await admin_ws.close()
        for p in players:
            try:
                await adm_p.post(f"/api/v1/devices/{p.device_id}/revoke", json={"reason": f"{L.PREFIX} fim do teste WS"})
            except Exception:
                pass
            await p.client.aclose()
        print(f"[B52] relatório: {log.dump()}", flush=True)
        await adm_p.aclose()
        await adm_d.aclose()
    failed = [x for x in log.steps if x["result"] != "PASS"]
    assert not failed, failed
