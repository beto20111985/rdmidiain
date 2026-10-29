"""Gateway WebSocket com Redis Pub/Sub — PROCESSOS REAIS (uvicorn) + PostgreSQL/Redis reais.

Cobre P002 itens 52–53 e A2 E32–E34/A4 C4/C7: mensagem publicada no processo B chega ao
player conectado no A; publicação a partir de uma task Celery em processo separado;
backlog de comandos na reconexão; kill; revogação/re-pareamento fecham o WS; presença e
filtro de escopo no WS do painel; 60 players com pool pequeno sem ``idle in transaction``;
admin com conexão morta não trava o gateway; contadores globais em /metrics.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from typing import Optional

import httpx
import pytest
from sqlalchemy import select, text, update
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.core.config import settings
from app.models.devices import Device, DeviceCommand
from tests.factories import assign, login_as, make_establishment, make_org, make_user
from tests.player_support import SimDevice, make_screen, pair_via_panel

pytestmark = pytest.mark.slow

UA = {"User-Agent": "pytest-rd/1.0 (Windows NT 10.0) Chrome/120.0"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _child_env(extra: Optional[dict] = None) -> dict:
    env = dict(os.environ)
    env.update({
        "ENVIRONMENT": "test",
        "DATABASE_URL": settings.DATABASE_URL,
        "REDIS_URL": settings.REDIS_URL,
        "REDIS_KEY_PREFIX": settings.REDIS_KEY_PREFIX,
        "CORS_ORIGINS": json.dumps(settings.CORS_ORIGINS),
        "FRONTEND_URL": settings.FRONTEND_URL,
        "EMAIL_BACKEND": "log",
        "LOG_JSON": "false",
    })
    env.update(extra or {})
    return env


class Server:
    def __init__(self, extra_env: Optional[dict] = None) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.ws = f"ws://127.0.0.1:{self.port}"
        self.log = f"/tmp/rd_ws_srv_{self.port}.log"
        self._fh = open(self.log, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"],
            cwd="/app", env=_child_env(extra_env), stdout=self._fh, stderr=subprocess.STDOUT,
        )

    async def wait_ready(self) -> None:
        deadline = time.monotonic() + 40
        async with httpx.AsyncClient() as c:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(open(self.log, encoding="utf-8", errors="replace").read()[-3000:])
                try:
                    if (await c.get(self.base + "/health", timeout=1)).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.3)
        raise RuntimeError("servidor não subiu: " + open(self.log, encoding="utf-8", errors="replace").read()[-3000:])

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base, headers=UA, timeout=20)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._fh.close()


@pytest.fixture
async def spawn(db_engine):
    servers: list[Server] = []

    async def _spawn(extra_env: Optional[dict] = None) -> Server:
        s = Server(extra_env)
        servers.append(s)
        await s.wait_ready()
        return s

    yield _spawn
    for s in servers:
        s.stop()


async def _admin(db, client, *, scope=None):
    u = await make_user(db, role="employee", scope=scope)
    # REQ-RBAC-006: comandos remotos só admin/técnico — o operador de teste acumula técnico
    await assign(db, u, "technician", scope)
    return await login_as(client, u)


async def _player_ws(server: Server, client: httpx.AsyncClient, dev: SimDevice, origin: Optional[str] = None):
    ticket = await dev.ws_ticket(client)
    headers = {"Origin": origin} if origin else None
    ws = await ws_connect(f"{server.ws}/ws/player?ticket={ticket}", additional_headers=headers, open_timeout=10)
    first = json.loads(await asyncio.wait_for(ws.recv(), 10))
    assert first["type"] == "server_time" and isinstance(first["data"]["server_time_ms"], int)
    assert set(first) == {"type", "id", "ts_ms", "data"}
    return ws


async def _recv_type(ws, typ: str, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"mensagem {typ} não chegou")
        msg = json.loads(await asyncio.wait_for(ws.recv(), left))
        if msg.get("type") == typ:
            return msg


async def _no_message(ws, pred, wait: float = 1.5) -> None:
    deadline = time.monotonic() + wait
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), left))
        except asyncio.TimeoutError:
            return
        assert not pred(msg), msg


async def _env(db, client):
    org = await make_org(db)
    est = await make_establishment(db, org)
    screen = await make_screen(db, est)
    adm = await _admin(db, client)
    dev = await pair_via_panel(client, adm.headers, screen.id)
    return org, est, screen, adm, dev


async def test_ticket_single_use_origin_and_wrong_ticket_kind(db, spawn):
    a = await spawn()
    async with a.client() as c:
        org, est, screen, adm, dev = await _env(db, c)
        ticket = await dev.ws_ticket(c)
        ws = await ws_connect(f"{a.ws}/ws/player?ticket={ticket}")
        assert json.loads(await ws.recv())["type"] == "server_time"
        await ws.close()
        for url, hdr in (
            (f"{a.ws}/ws/player?ticket={ticket}", None),  # reutilizado
            (f"{a.ws}/ws/player", None),
            (f"{a.ws}/ws/player?ticket={await dev.ws_ticket(c)}", {"Origin": "https://evil.example"}),
            (f"{a.ws}/ws/player?token={dev.token}", None),  # parâmetro antigo
        ):
            with pytest.raises((InvalidStatus, ConnectionClosed)):
                w = await ws_connect(url, additional_headers=hdr)
                await asyncio.wait_for(w.recv(), 5)
        # ticket do PAINEL não abre WS do player (e vice-versa)
        uticket = (await c.post("/api/v1/realtime/ticket", headers=adm.headers)).json()["ticket"]
        with pytest.raises((InvalidStatus, ConnectionClosed)):
            w = await ws_connect(f"{a.ws}/ws/player?ticket={uticket}")
            await asyncio.wait_for(w.recv(), 5)
        with pytest.raises((InvalidStatus, ConnectionClosed)):
            w = await ws_connect(f"{a.ws}/ws/admin?ticket={await dev.ws_ticket(c)}")
            await asyncio.wait_for(w.recv(), 5)
        # origens permitidas do player
        for origin in ("http://localhost:8088", "https://player.rd.local"):
            w = await _player_ws(a, c, dev, origin=origin)
            await w.close()


async def test_publish_on_process_b_reaches_player_on_process_a(db, spawn):
    a = await spawn()
    b = await spawn()
    async with a.client() as ca, b.client() as cb:
        org, est, screen, adm, dev = await _env(db, ca)
        ws = await _player_ws(a, ca, dev)
        r = await cb.post(f"/api/v1/screens/{screen.id}/commands",
                          json={"command": "reload_manifest", "reason": "teste multi-processo"}, headers=adm.headers)
        assert r.status_code == 202, r.text
        msg = await _recv_type(ws, "command")
        assert msg["data"]["command"] == "reload_manifest" and msg["data"]["command_id"] == r.json()["id"]
        assert isinstance(msg["data"]["expires_at_ms"], int)
        await ws.send(json.dumps({"type": "command_ack", "id": str(uuid.uuid4()), "ts_ms": 0,
                                  "data": {"command_id": r.json()["id"], "status": "DONE", "detail": "ok"}}))
        for _ in range(40):
            got = (await cb.get(f"/api/v1/screens/{screen.id}/commands", headers=adm.headers)).json()["items"][0]
            if got["status"] == "DONE":
                break
            await asyncio.sleep(0.25)
        assert got["status"] == "DONE" and got["ack_status"] == "DONE" and got["delivery_count"] >= 1
        # confirmação obrigatória para comandos destrutivos
        r = await cb.post(f"/api/v1/screens/{screen.id}/commands", json={"command": "reboot", "reason": "travou"},
                          headers=adm.headers)
        assert r.status_code == 400 and r.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
        await ws.close()


async def test_celery_task_in_separate_process_reaches_player(db, spawn):
    a = await spawn()
    async with a.client() as ca:
        org, est, screen, adm, dev = await _env(db, ca)
        ws = await _player_ws(a, ca, dev)
        code = ("from app.workers.tasks.playback import publish_sync_required as t; "
                f"r = t.apply(args=['{screen.id}', 777]); print('RECEIVERS', r.get())")
        p = await asyncio.create_subprocess_exec(sys.executable, "-c", code, cwd="/app", env=_child_env(),
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), 60)
        assert p.returncode == 0, out.decode()[-2000:]
        msg = await _recv_type(ws, "sync_required")
        assert msg["data"] == {"version": 777}
        # evento do painel screen.sync (W-INV invalidate_screens) publicado por OUTRO processo → sync_required
        code2 = ("from app.core import events; "
                 f"events.publish_sync('screen.sync', {{'screen_id': '{screen.id}', 'version': 778, 'reason': 't'}}, "
                 f"screen_id='{screen.id}', org_id='{org.id}')")
        p = await asyncio.create_subprocess_exec(sys.executable, "-c", code2, cwd="/app", env=_child_env())
        assert await asyncio.wait_for(p.wait(), 60) == 0
        msg = await _recv_type(ws, "sync_required")
        assert msg["data"] == {"version": 778}
        await ws.close()


async def test_command_backlog_redelivered_until_final_ack(db, spawn):
    a = await spawn()
    async with a.client() as c:
        org, est, screen, adm, dev = await _env(db, c)
        # emitido com o player OFFLINE: persistido (não é "queued" falso)
        r = await c.post(f"/api/v1/screens/{screen.id}/commands",
                         json={"command": "clear_cache", "reason": "limpeza", "confirm": True}, headers=adm.headers)
        assert r.status_code == 202 and r.json()["status"] == "PENDING"
        cid = r.json()["id"]
        ws = await _player_ws(a, c, dev)
        assert (await _recv_type(ws, "command"))["data"]["command_id"] == cid
        await ws.send(json.dumps({"type": "command_ack", "data": {"command_id": cid, "status": "RECEIVED"}}))
        await asyncio.sleep(0.5)
        await ws.close()
        ws = await _player_ws(a, c, dev)  # RECEIVED não é final → reentrega
        assert (await _recv_type(ws, "command"))["data"]["command_id"] == cid
        await ws.send(json.dumps({"type": "command_ack", "data": {"command_id": cid, "status": "UNSUPPORTED",
                                                                  "detail": "sem suporte"}}))
        await asyncio.sleep(0.5)
        await ws.close()
        ws = await _player_ws(a, c, dev)
        await _no_message(ws, lambda m: m.get("type") == "command")
        await ws.close()
        row = (await db.execute(select(DeviceCommand).where(DeviceCommand.id == uuid.UUID(cid))
                                .execution_options(populate_existing=True))).scalar_one()
        assert row.status == "UNSUPPORTED" and row.delivery_count >= 2 and row.result_detail == "sem suporte"


async def test_kill_revocation_and_repairing_close_ws(db, spawn):
    a = await spawn()
    b = await spawn()
    async with a.client() as ca, b.client() as cb:
        org, est, screen, adm, dev = await _env(db, ca)
        admin = await make_user(db, role="admin", mfa=True)
        sa = await login_as(cb, admin)
        ws = await _player_ws(a, ca, dev)
        cid = str(uuid.uuid4())
        r = await cb.post("/api/v1/playback/kills", json={"creative_id": cid, "screen_ids": [str(screen.id)],
                                                           "reason": "ordem judicial"}, headers=sa.headers)
        assert r.status_code == 201, r.text
        kill = await _recv_type(ws, "kill")
        assert kill["data"]["creative_ids"] == [cid] and kill["data"]["reason"] == "ordem judicial"
        # re-pareamento revoga o anterior e FECHA o WS dele
        dev2 = await pair_via_panel(cb, adm.headers, screen.id)
        await _recv_type(ws, "device_revoked")
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5)
        assert ws.close_code == 4003
        # revogação pelo painel fecha o WS do novo dispositivo
        ws2 = await _player_ws(a, ca, dev2)
        r = await cb.post(f"/api/v1/devices/{dev2.device_id}/revoke", json={"reason": "furto"}, headers=adm.headers)
        assert r.status_code == 200
        await _recv_type(ws2, "device_revoked")
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws2.recv(), 5)


async def test_presence_and_admin_scope_filter(db, spawn):
    a = await spawn()
    b = await spawn()
    async with a.client() as ca, b.client() as cb:
        org, est, screen, adm, dev = await _env(db, ca)
        org_b = await make_org(db)
        est_b = await make_establishment(db, org_b)
        screen_b = await make_screen(db, est_b)
        dev_b = await pair_via_panel(ca, adm.headers, screen_b.id)
        viewer = await _admin(db, cb, scope=("ORGANIZATION", org.id))
        t = (await cb.post("/api/v1/realtime/ticket", headers=viewer.headers)).json()["ticket"]
        aws = await ws_connect(f"{b.ws}/ws/admin?ticket={t}")
        await aws.send(json.dumps({"type": "ping"}))
        assert (await _recv_type(aws, "pong"))["type"] == "pong"
        p1 = await _player_ws(a, ca, dev)
        st = await _recv_type(aws, "screen.status")
        assert st["data"]["screen_id"] == str(screen.id) and st["data"]["status"] == "conectado"
        assert set(st) == {"type", "id", "ts", "data"}  # escopo interno removido
        await p1.send(json.dumps({"type": "heartbeat", "id": str(uuid.uuid4()), "ts_ms": 0, "data": {
            "state": "PLAYING", "current": {"creative_code": "RD-A7M2P9", "started_at_ms": 1},
            "manifest_version_applied": 3, "manifest_version_pending": None, "clock_offset_ms": 12}}))
        st = await _recv_type(aws, "screen.status")
        assert st["data"]["status"] == "reproduzindo" and st["data"]["manifest_version_applied"] == 3
        assert st["data"]["sync"] == "sincronizado"
        # heartbeat repetido sem transição: nenhum evento novo e nenhuma escrita
        await p1.send(json.dumps({"type": "heartbeat", "data": {"state": "PLAYING", "manifest_version_applied": 3}}))
        await _no_message(aws, lambda m: m.get("type") == "screen.status", wait=1.0)
        # tela de OUTRA organização: nunca chega ao admin com escopo na org A
        p2 = await _player_ws(a, ca, dev_b)
        await p2.send(json.dumps({"type": "heartbeat", "data": {"state": "ERROR"}}))
        await _no_message(aws, lambda m: m.get("data", {}).get("screen_id") == str(screen_b.id), wait=1.5)
        row = (await db.execute(select(Device).where(Device.id == uuid.UUID(dev.device_id))
                                .execution_options(populate_existing=True))).scalar_one()
        assert row.presence == "online" and row.player_state == "PLAYING" and row.applied_manifest_version == 3
        # telemetria (sem IP) persistida
        await p1.send(json.dumps({"type": "telemetry", "data": {"cpu_pct": 12.5, "ram_used_mb": 900, "ip": "10.0.0.9",
                                                                "app_version": "1.0.1", "wifi": {"ssid": "loja", "rssi_dbm": -60}}}))
        await asyncio.sleep(0.8)
        row = (await db.execute(select(Device).where(Device.id == uuid.UUID(dev.device_id))
                                .execution_options(populate_existing=True))).scalar_one()
        assert row.telemetry and "ip" not in row.telemetry and row.app_version == "1.0.1"
        # desconexão → offline imediato
        await p1.close()
        st = await _recv_type(aws, "screen.status")
        assert st["data"]["status"] == "offline" and st["data"]["presence"] == "offline"
        await p2.close()
        await aws.close()


async def test_sixty_players_small_pool_no_idle_in_transaction(db, spawn):
    small = await spawn({"DB_NULLPOOL": "false", "DB_POOL_SIZE": "2", "DB_MAX_OVERFLOW": "1", "RATE_LIMIT_ENABLED": "false"})
    async with small.client() as c:
        org = await make_org(db)
        est = await make_establishment(db, org)
        adm = await _admin(db, c)
        devs = []
        for _ in range(60):
            sc = await make_screen(db, est)
            devs.append(await pair_via_panel(c, adm.headers, sc.id))
        conns = await asyncio.gather(*[_player_ws(small, c, d) for d in devs])
        for w in conns:
            await w.send(json.dumps({"type": "heartbeat", "data": {"state": "PLAYING", "manifest_version_applied": 1}}))
        await asyncio.sleep(2.0)
        db_name = settings.DATABASE_URL.rsplit("/", 1)[-1]
        rows = (await db.execute(text("SELECT state, count(*) FROM pg_stat_activity WHERE datname = :d GROUP BY state"),
                                 {"d": db_name})).all()
        states = {s: n for s, n in rows}
        assert states.get("idle in transaction", 0) == 0, states
        assert sum(states.values()) <= 3 + 3, states  # pool 2+1 do servidor + conexões do próprio teste
        # HTTP do mesmo processo segue respondendo com o pool pequeno
        t0 = time.monotonic()
        r = await c.get(f"/api/v1/screens/{devs[0].screen_id}/presence", headers=adm.headers)
        assert r.status_code == 200 and r.json()["live"]["status"] == "reproduzindo"
        assert time.monotonic() - t0 < 5
        online = (await db.execute(select(Device).where(Device.presence == "online"))).scalars().all()
        assert len(online) == 60
        m = (await c.get("/metrics")).text
        assert "rd_ws_player_connections 60" in m, m
        await asyncio.gather(*[w.close() for w in conns])


async def test_dead_admin_connection_does_not_block_gateway(db, spawn):
    a = await spawn()
    async with a.client() as c:
        org, est, screen, adm, dev = await _env(db, c)
        t1 = (await c.post("/api/v1/realtime/ticket", headers=adm.headers)).json()["ticket"]
        t2 = (await c.post("/api/v1/realtime/ticket", headers=adm.headers)).json()["ticket"]
        dead = await ws_connect(f"{a.ws}/ws/admin?ticket={t1}")
        live = await ws_connect(f"{a.ws}/ws/admin?ticket={t2}")
        dead.transport.abort()  # conexão morta sem close frame (A2 E33)
        from app.core import events

        for i in range(30):
            await events.publish("campaign.updated", {"id": f"c{i}"}, org_id=org.id)
        got = 0
        deadline = time.monotonic() + 8
        while got < 30 and time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(live.recv(), 8))
            got += msg.get("type") == "campaign.updated"
        assert got == 30
        # o gateway continua aceitando e encerrando players normalmente
        p = await _player_ws(a, c, dev)
        await p.close()
        for _ in range(20):
            row = (await db.execute(select(Device.presence).where(Device.id == uuid.UUID(dev.device_id))
                                    .execution_options(populate_existing=True))).scalar_one()
            if row == "offline":
                break
            await asyncio.sleep(0.25)
        assert row == "offline"
        await live.close()


async def test_metrics_sum_connections_across_processes(db, spawn):
    a = await spawn()
    b = await spawn()
    async with a.client() as ca, b.client() as cb:
        org, est, screen, adm, dev = await _env(db, ca)
        s2 = await make_screen(db, est)
        dev2 = await pair_via_panel(ca, adm.headers, s2.id)
        w1 = await _player_ws(a, ca, dev)
        w2 = await _player_ws(b, cb, dev2)
        await asyncio.sleep(0.8)
        for cli in (ca, cb):
            txt = (await cli.get("/metrics")).text
            assert "rd_ws_player_connections 2" in txt, txt
            assert txt.count("# HELP rd_ws_player_connections") == 1
            assert "rd_players_present 2" in txt
        await w1.close()
        await w2.close()


async def test_pairing_ws_receives_claim_from_other_process_and_expiry(db, spawn):
    a = await spawn()
    b = await spawn()
    async with a.client() as ca, b.client() as cb:
        org = await make_org(db)
        est = await make_establishment(db, org)
        screen = await make_screen(db, est)
        adm = await _admin(db, cb)
        dev = SimDevice()
        await dev.request_pairing(ca)
        pws = await ws_connect(f"{a.ws}/ws/player/pairing?request_id={dev.request_id}", open_timeout=10)
        r = await cb.post(f"/api/v1/screens/{screen.id}/pairing", json={"pairing_code": dev.pairing_code},
                          headers=adm.headers)
        assert r.status_code == 201
        msg = json.loads(await asyncio.wait_for(pws.recv(), 8))
        assert msg["type"] == "pairing_claimed" and set(msg) == {"type", "id", "ts_ms", "data"}
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(pws.recv(), 5)
        # já reclamado: ao conectar recebe pairing_claimed imediatamente
        pws = await ws_connect(f"{a.ws}/ws/player/pairing?request_id={dev.request_id}")
        assert json.loads(await asyncio.wait_for(pws.recv(), 5))["type"] == "pairing_claimed"
        # expiração enquanto aguarda
        dev2 = SimDevice()
        await dev2.request_pairing(ca)
        from app.models.devices import PairingRequest
        from app.core.timeutils import utcnow
        from datetime import timedelta

        await db.execute(update(PairingRequest).where(PairingRequest.id == uuid.UUID(dev2.request_id))
                         .values(expires_at=utcnow() + timedelta(seconds=2)))
        await db.commit()
        pws2 = await ws_connect(f"{a.ws}/ws/player/pairing?request_id={dev2.request_id}")
        msg = json.loads(await asyncio.wait_for(pws2.recv(), 8))
        assert msg["type"] == "pairing_expired"
        # request inexistente → recusado
        with pytest.raises((InvalidStatus, ConnectionClosed)):
            w = await ws_connect(f"{a.ws}/ws/player/pairing?request_id={uuid.uuid4()}")
            await asyncio.wait_for(w.recv(), 5)


async def test_sync_events_recorded_only_on_transitions_and_report(db, spawn):
    from app.models.devices import DeviceSyncEvent
    from app.modules.playback.syncevents import sync_report

    a = await spawn()
    async with a.client() as c:
        org, est, screen, adm, dev = await _env(db, c)
        r = await c.get("/api/v1/player/manifest", headers=dev.headers)
        assert r.status_code == 200
        v = r.json()["manifest"]["version"]
        assert (await c.get("/api/v1/player/manifest", headers={**dev.headers, "If-None-Match": f'"{v}"'})).status_code == 304
        ws = await _player_ws(a, c, dev)

        async def hb(**d):
            await ws.send(json.dumps({"type": "heartbeat", "data": {"state": "SYNCING", **d}}))
            await asyncio.sleep(0.3)

        await hb(manifest_version_applied=None, manifest_version_pending=v, download={"done_bytes": 0, "total_bytes": 1000})
        for _ in range(3):  # heartbeats repetidos: nenhuma linha nova
            await hb(manifest_version_applied=None, manifest_version_pending=v, download={"done_bytes": 500, "total_bytes": 1000})
        await hb(manifest_version_applied=None, manifest_version_pending=v, download={"done_bytes": 1000, "total_bytes": 1000})
        await hb(manifest_version_applied=None, manifest_version_pending=v, download={"done_bytes": 1000, "total_bytes": 1000})
        await ws.send(json.dumps({"type": "manifest_applied", "data": {"version": v}}))
        await ws.send(json.dumps({"type": "media_error", "data": {"media_id": str(uuid.uuid4()), "reason": "sha256 divergente",
                                                                   "attempts": 5}}))
        await ws.send(json.dumps({"type": "manifest_failed", "data": {"version": v + 1, "reason": "assinatura inválida"}}))
        await asyncio.sleep(1.0)
        await ws.close()
        rows = (await db.execute(select(DeviceSyncEvent).where(DeviceSyncEvent.device_id == uuid.UUID(dev.device_id))
                                 .order_by(DeviceSyncEvent.id))).scalars().all()
        types = [x.type for x in rows]
        assert types == ["MANIFEST_RECEIVED", "DOWNLOAD_STARTED", "DOWNLOAD_COMPLETED", "MANIFEST_APPLIED", "MEDIA_ERROR",
                         "MANIFEST_FAILED"], types
        assert rows[0].org_id == org.id and rows[0].establishment_id == est.id and rows[0].manifest_version == v
        assert rows[2].bytes == 1000 and rows[2].duration_ms is not None and rows[2].duration_ms >= 0
        assert rows[3].duration_ms is not None and rows[3].duration_ms >= 0  # tempo recebimento → aplicação
        from app.core.timeutils import utcnow
        from datetime import timedelta

        rep = await sync_report(db, org_id=org.id, start=utcnow() - timedelta(hours=1), end=utcnow() + timedelta(minutes=1),
                                group_by="screen")
        g = rep["groups"][0]
        assert g["key"] == str(screen.id) and g["counts"]["MANIFEST_APPLIED"] == 1 and g["counts"]["DOWNLOAD_COMPLETED"] == 1
        assert g["bytes_downloaded"] == 1000 and g["time_to_apply_ms"]["n"] == 1
        assert rep["media_failures"][0]["count"] == 1 and "sha256" in rep["media_failures"][0]["last_reason"]


async def test_decommission_revokes_device_and_closes_ws(db, spawn):
    """Retirada definitiva (W-ORG) → listener do W-PLAY revoga o dispositivo na mesma transação
    e, após o commit, o WS do player recebe ``device_revoked`` e é fechado."""
    a = await spawn()
    b = await spawn()
    async with a.client() as ca, b.client() as cb:
        org, est, screen, adm, dev = await _env(db, ca)
        ws = await _player_ws(a, ca, dev)
        r = await cb.post(f"/api/v1/screens/{screen.id}/decommission", json={"reason": "TV quebrada, retirada definitiva"},
                          headers=adm.headers)
        assert r.status_code == 200, r.text
        await _recv_type(ws, "device_revoked")
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5)
        row = (await db.execute(select(Device).where(Device.id == uuid.UUID(dev.device_id))
                                .execution_options(populate_existing=True))).scalar_one()
        assert row.status == "REVOKED" and "tela removida" in row.revoked_reason
        r = await ca.post("/api/v1/player/ws-ticket", headers=dev.headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "DEVICE_REVOKED"


async def test_failed_decommission_does_not_revoke(db, spawn):
    """Rollback da operação do W-ORG (justificativa curta) → nada é revogado nem publicado."""
    a = await spawn()
    async with a.client() as ca:
        org, est, screen, adm, dev = await _env(db, ca)
        ws = await _player_ws(a, ca, dev)
        r = await ca.post(f"/api/v1/screens/{screen.id}/decommission", json={"reason": "curta"}, headers=adm.headers)
        assert r.status_code in (400, 422)
        await _no_message(ws, lambda m: m.get("type") == "device_revoked", wait=1.5)
        assert (await ca.post("/api/v1/player/ws-ticket", headers=dev.headers)).status_code == 200
        await ws.close()


async def test_batch_presence_with_download_and_scope(db, spawn):
    a = await spawn()
    async with a.client() as c:
        org, est, screen, adm, dev = await _env(db, c)
        unpaired = await make_screen(db, est)
        org_b = await make_org(db)
        screen_b = await make_screen(db, await make_establishment(db, org_b))
        viewer = await _admin(db, c, scope=("ORGANIZATION", org.id))
        t = (await c.post("/api/v1/realtime/ticket", headers=viewer.headers)).json()["ticket"]
        aws = await ws_connect(f"{a.ws}/ws/admin?ticket={t}")
        ws = await _player_ws(a, c, dev)
        await ws.send(json.dumps({"type": "heartbeat", "data": {"state": "SYNCING", "manifest_version_applied": 2,
                                                                "manifest_version_pending": 3,
                                                                "download": {"done_bytes": 250, "total_bytes": 1000}}}))
        st = await _recv_type(aws, "screen.status")
        while st["data"]["status"] != "sincronizando":
            st = await _recv_type(aws, "screen.status")
        assert st["data"]["download"] == {"done_bytes": 250, "total_bytes": 1000}
        q = "&".join(f"screen_id={x}" for x in (screen.id, unpaired.id, screen_b.id, uuid.uuid4()))
        r = await c.get(f"/api/v1/screens/presence?{q}", headers=viewer.headers)
        assert r.status_code == 200, r.text
        items = {x["screen_id"]: x for x in r.json()["items"]}
        assert set(items) == {str(screen.id), str(unpaired.id)}  # outra org e inexistente omitidas
        it = items[str(screen.id)]
        assert it["paired"] and it["device_id"] == dev.device_id and it["status"] == "sincronizando"
        assert it["manifest_version_applied"] == 2 and it["manifest_version_pending"] == 3
        assert it["download"] == {"done_bytes": 250, "total_bytes": 1000} and it["last_seen_at"].endswith("Z")
        assert items[str(unpaired.id)] == {"screen_id": str(unpaired.id), "paired": False, "device_id": None,
                                            "status": "offline", "player_state": None, "last_seen_at": None,
                                            "app_version": None, "manifest_version_applied": None,
                                            "manifest_version_pending": None, "download": None}
        live = (await c.get(f"/api/v1/screens/{screen.id}/presence", headers=viewer.headers)).json()["live"]
        assert live["download"] == {"done_bytes": 250, "total_bytes": 1000}
        too_many = "&".join(f"screen_id={uuid.uuid4()}" for _ in range(101))
        assert (await c.get(f"/api/v1/screens/presence?{too_many}", headers=viewer.headers)).status_code == 422
        assert (await c.get(f"/api/v1/screens/presence?{q}")).status_code == 401
        await ws.close()
        await aws.close()
