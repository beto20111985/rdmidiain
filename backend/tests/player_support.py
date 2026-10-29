"""
Apoio aos testes do domínio de players (W-PLAY): telas, dispositivo simulado com chave
ES256 real, pareamento pela API, token de dispositivo e eventos PoP assinados.

Nada aqui substitui o núcleo testado: o "dispositivo" só gera chaves/assinaturas como um
player real faria (protocolo RDPP/1) e chama a API HTTP de verdade.
"""
from __future__ import annotations

import gzip
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ids
from app.models.screens import Screen
from app.modules.playback import protocol as proto


def now_ms() -> int:
    return int(time.time() * 1000)


async def make_screen(db: AsyncSession, est, *, screen_id: Optional[uuid.UUID] = None, uf: str = "GO") -> Screen:
    s = Screen(id=screen_id or uuid.uuid4(), establishment_id=est.id, friendly_id=ids.new_screen_code(uf),
               name=f"Tela {secrets.token_hex(2)}")
    db.add(s)
    await db.commit()
    return s


@dataclass
class SimDevice:
    """Player simulado: par ES256 próprio (como WebCrypto/Keystore)."""

    key: Any = field(default_factory=proto.generate_private_key)
    device_id: Optional[str] = None
    screen_id: Optional[str] = None
    request_id: Optional[str] = None
    pairing_code: Optional[str] = None
    token: Optional[str] = None
    seq: int = 0
    last_hash: str = proto.ZERO_HASH
    platform: str = "web"

    @property
    def spki(self) -> str:
        return proto.public_spki_b64u(self.key)

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def request_pairing(self, client: httpx.AsyncClient) -> dict:
        r = await client.post("/api/v1/player/pairing/requests", json={
            "public_key": self.spki, "key_alg": "ES256", "platform": self.platform, "app_version": "1.0.0",
            "device_info": {"model": "pytest", "os": "Linux", "resolution": "1920x1080"}})
        assert r.status_code == 201, r.text
        body = r.json()
        self.request_id = body["pairing_request_id"]
        self.pairing_code = body["pairing_code"]
        return body

    def result_headers(self, ts: Optional[int] = None) -> dict:
        ts = now_ms() if ts is None else ts
        sig = proto.sign_es256(self.key, {"pairing_request_id": self.request_id, "purpose": proto.PURPOSE_PAIRING_RESULT,
                                          "ts_ms": ts})
        return {"X-RD-Device-Ts-Ms": str(ts), "X-RD-Device-Signature": sig}

    async def fetch_result(self, client: httpx.AsyncClient) -> httpx.Response:
        r = await client.get(f"/api/v1/player/pairing/requests/{self.request_id}/result", headers=self.result_headers())
        if r.status_code == 200:
            self.device_id = r.json()["device_id"]
            self.screen_id = r.json()["screen_id"]
        return r

    async def login(self, client: httpx.AsyncClient) -> httpx.Response:
        r = await client.post("/api/v1/player/auth/challenge", json={"device_id": self.device_id})
        assert r.status_code == 200, r.text
        ch = r.json()["challenge"]
        sig = proto.sign_es256(self.key, {"challenge": ch, "device_id": self.device_id, "purpose": proto.PURPOSE_AUTH})
        r = await client.post("/api/v1/player/auth/token", json={"device_id": self.device_id, "challenge": ch, "signature": sig})
        if r.status_code == 200:
            self.token = r.json()["access_token"]
        return r

    async def ws_ticket(self, client: httpx.AsyncClient) -> str:
        r = await client.post("/api/v1/player/ws-ticket", headers=self.headers)
        assert r.status_code == 200, r.text
        return r.json()["ticket"]

    def event(self, manifest: dict, ckey: str, *, start: int, actual: Optional[int] = None, insertion_id: Optional[str] = None,
              result: str = "COMPLETED", **over: Any) -> dict:
        cr = manifest["creatives"][ckey]
        exp = cr["duration_ms"]
        act = exp if actual is None else actual
        if insertion_id is None and cr["kind"] == "EXTERNAL":
            for entries in manifest["external_plan"].values():
                for e in entries:
                    if e["c"] == ckey:
                        insertion_id = e["i"]
                        exp = e["d"]
                        break
                if insertion_id:
                    break
        self.seq += 1
        ev = {
            "schema": "rd.pop/1", "playback_uuid": str(uuid.uuid4()), "device_id": self.device_id,
            "screen_id": self.screen_id, "creative_id": cr["creative_id"], "creative_code": cr["code"],
            "campaign_id": cr["campaign_id"], "insertion_id": insertion_id, "kind": cr["kind"],
            "manifest_version": manifest["version"], "started_at_ms": start, "finished_at_ms": start + act,
            "expected_ms": exp, "actual_ms": act, "result": result, "seq": self.seq,
            "nonce": proto.b64u_encode(secrets.token_bytes(16)), "prev_hash": self.last_hash, "clock_offset_ms": 0,
            "offline": False,
        }
        ev.update(over)
        self.last_hash = proto.sha256_hex_of(ev)
        return ev

    def item(self, ev: dict, key: Any = None) -> dict:
        return {"event": ev, "signature": proto.sign_es256(key or self.key, ev)}


async def pair_via_panel(client: httpx.AsyncClient, admin_headers: dict, screen_id: Any, dev: Optional[SimDevice] = None
                         ) -> SimDevice:
    dev = dev or SimDevice()
    await dev.request_pairing(client)
    r = await client.post(f"/api/v1/screens/{screen_id}/pairing", json={"pairing_code": dev.pairing_code},
                          headers=admin_headers)
    assert r.status_code == 201, r.text
    r = await dev.fetch_result(client)
    assert r.status_code == 200, r.text
    r = await dev.login(client)
    assert r.status_code == 200, r.text
    return dev


async def store_manifest(db: AsyncSession, screen_id: Any, manifest: dict, *, version: Optional[int] = None) -> int:
    """Grava uma versão de manifesto (estado de banco do W-INV) para testes de validação de PoP."""
    from datetime import datetime, timezone

    from app.models.manifests import ManifestVersion

    m = dict(manifest)
    v = int(version if version is not None else m["version"])
    m["version"] = v
    m["screen_id"] = str(screen_id)
    data = proto.canonical_bytes(m)
    h = m.get("horizon") or {}
    row = ManifestVersion(
        screen_id=uuid.UUID(str(screen_id)), version=v, manifest_id=uuid.UUID(m["manifest_id"]),
        content_gz=gzip.compress(data, mtime=0), content_sha256=hashlib.sha256(data).hexdigest(),
        plan_sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), signature="test", key_id="test",
        reason="test", horizon_from=datetime.fromtimestamp(h.get("from_ms", 0) / 1000, tz=timezone.utc),
        horizon_to=datetime.fromtimestamp(h.get("to_ms", 0) / 1000, tz=timezone.utc),
        media_ids=[uuid.UUID(x["media_id"]) for x in m["media"].values()],
        creative_ids=list({uuid.UUID(x["creative_id"]) for x in m["creatives"].values()}),
        campaign_ids=list({uuid.UUID(x["campaign_id"]) for x in m["creatives"].values() if x.get("campaign_id")}),
    )
    db.add(row)
    await db.commit()
    return v
