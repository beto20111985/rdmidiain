"""
Apoio aos testes E2E contra o STACK VIVO (AÇÃO 002 / QA).

Diferente do resto da suíte (banco descartável + ASGI em processo), aqui tudo passa por
HTTP real (nginx → backend), o worker Celery REAL processa as tarefas e as asserções de
estado leem o banco VIVO (somente leitura). Só roda com ``RD_E2E_BASE_URL`` definido.

- Credenciais: contas de TESTE do CLI (``backend/.devtest/test_accounts.json``), usadas e
  nunca impressas; TOTP calculado localmente (pyotp) com tratamento de anti-replay.
- Player: implementação de referência RDPP/1 do backend (``app.modules.playback.protocol``),
  chave ES256 gerada no teste (nunca chaves de produção).
- Dados criados: prefixo ``TEST_MIDIAINDOOR_002_QA_``.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

PREFIX = "TEST_MIDIAINDOOR_002_QA_"
BASE_URL = os.environ.get("RD_E2E_BASE_URL", "").rstrip("/")
PANEL_ORIGIN = os.environ.get("RD_E2E_PANEL_ORIGIN", "http://localhost")
PLAYER_ORIGIN = os.environ.get("RD_E2E_PLAYER_ORIGIN", "http://localhost:8088")
ACCOUNTS_FILE = Path(os.environ.get("RD_E2E_ACCOUNTS", "/app/.devtest/test_accounts.json"))
REPORT_DIR = os.environ.get("RD_E2E_REPORT_DIR")


def now_ms() -> int:
    return int(time.time() * 1000)


def run_tag() -> str:
    return time.strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)


def load_accounts() -> dict:
    return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))


def ws_url(base: str, path: str) -> str:
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + path
    return "ws://" + base[len("http://"):] + path


# ─── Banco VIVO (somente leitura) ────────────────────────────────────────────

_live_engine = None


def live_engine():
    """Engine síncrona para o banco VIVO (o conftest raiz reaponta ``settings.DATABASE_URL`` para
    um ``rdtest_*``; aqui voltamos ao banco do ambiente). Usada só para SELECT."""
    global _live_engine
    if _live_engine is None:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url

        from app.core.config import settings

        dbname = os.environ.get("RD_E2E_DB_NAME") or os.environ.get("POSTGRES_DB") or "rdmidiaindoor"
        url = make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2", database=dbname)
        _live_engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)
    return _live_engine


def q(sql: str, **params) -> list:
    from sqlalchemy import text

    with live_engine().connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        return [tuple(r) for r in c.execute(text(sql), params).all()]


def q1(sql: str, **params) -> Any:
    rows = q(sql, **params)
    return rows[0] if rows else None


# ─── Redis VIVO (somente leitura, para provas de WS multi-processo) ──────────

def live_redis_url() -> str:
    from urllib.parse import quote

    host = os.environ.get("REDIS_HOST", "redis")
    port = os.environ.get("REDIS_PORT", "6379")
    pw_file = os.environ.get("REDIS_PASSWORD_FILE")
    pw = Path(pw_file).read_text().strip() if pw_file and Path(pw_file).exists() else ""
    auth = f":{quote(pw, safe='')}@" if pw else ""
    return f"redis://{auth}{host}:{port}/0"


LIVE_REDIS_PREFIX = os.environ.get("RD_E2E_REDIS_PREFIX", "rd:")


# ─── Sessão HTTP de um usuário do painel ─────────────────────────────────────

class E2EError(AssertionError):
    pass


@dataclass
class Session:
    client: httpx.AsyncClient
    role: str
    access_token: Optional[str] = None
    user: dict = field(default_factory=dict)

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}

    async def refresh(self) -> bool:
        r = await self.client.post("/api/v1/auth/refresh", headers={"X-RD-CSRF": "1", "Origin": PANEL_ORIGIN})
        if r.status_code == 200:
            self.access_token = r.json()["access_token"]
            return True
        return False

    async def req(self, method: str, path: str, *, retry_auth: bool = True, retries_5xx: int = 3, **kw) -> httpx.Response:
        hdrs = dict(kw.pop("headers", {}) or {})
        for attempt in range(retries_5xx + 1):
            try:
                r = await self.client.request(method, path, headers={**self.headers, **hdrs}, **kw)
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
                if attempt >= retries_5xx:
                    raise
                await asyncio.sleep(2)  # backend vivo recarrega ao salvar (--reload): queda momentânea
                continue
            if r.status_code == 401 and retry_auth and await self.refresh():
                r = await self.client.request(method, path, headers={**self.headers, **hdrs}, **kw)
            if r.status_code in (502, 503, 504) and attempt < retries_5xx and method == "GET":
                await asyncio.sleep(2)
                continue
            return r
        return r

    async def get(self, path: str, **kw) -> httpx.Response:
        return await self.req("GET", path, **kw)

    async def post(self, path: str, **kw) -> httpx.Response:
        return await self.req("POST", path, **kw)

    async def put(self, path: str, **kw) -> httpx.Response:
        return await self.req("PUT", path, **kw)

    async def patch(self, path: str, **kw) -> httpx.Response:
        return await self.req("PATCH", path, **kw)

    async def delete(self, path: str, **kw) -> httpx.Response:
        return await self.req("DELETE", path, **kw)

    async def aclose(self) -> None:
        await self.client.aclose()


def new_client(base_url: Optional[str] = None, timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url or BASE_URL, timeout=timeout, follow_redirects=False)


async def _totp_verify(client: httpx.AsyncClient, mfa_token: str, secret: str) -> httpx.Response:
    import pyotp

    return await client.post("/api/v1/auth/mfa/verify", json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()})


async def login(role: str, *, base_url: Optional[str] = None, creds: Optional[dict] = None) -> Session:
    """Login real (com MFA quando exigido). Anti-replay de TOTP: se o passo atual já foi usado
    (por este ou outro agente), espera o próximo passo e repete o login."""
    acc = creds or load_accounts()["accounts"][role]
    client = new_client(base_url)
    last = None
    for attempt in range(4):
        r = await client.post("/api/v1/auth/login", json={"login": acc["email"], "password": acc["password"]})
        last = r
        if r.status_code == 429:
            await asyncio.sleep(int(r.headers.get("Retry-After", "7") or 7))
            continue
        if r.status_code != 200:
            raise E2EError(f"login {role}: HTTP {r.status_code} {r.json().get('error', {}).get('code')}")
        body = r.json()
        if body.get("status") == "ok":
            return Session(client, role, body["access_token"], body.get("user") or {})
        if body.get("status") == "mfa_required":
            secret = acc.get("totp_secret")
            if not secret:
                raise E2EError(f"login {role}: MFA exigido sem segredo de teste")
            v = await _totp_verify(client, body["mfa_token"], secret)
            if v.status_code == 200 and v.json().get("status") == "ok":
                return Session(client, role, v.json()["access_token"], v.json().get("user") or {})
            last = v
            # passo TOTP já usado (anti-replay) → aguarda o próximo passo de 30 s
            await asyncio.sleep(31 - (time.time() % 30))
            continue
        raise E2EError(f"login {role}: status inesperado {body.get('status')}")
    raise E2EError(f"login {role}: falhou após tentativas (último HTTP {getattr(last, 'status_code', None)})")


async def wait_until(fn: Callable[[], Awaitable[Any]], *, timeout: float, interval: float = 1.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await fn()
        if last:
            return last
        await asyncio.sleep(interval)
    raise E2EError(f"timeout ({timeout:.0f}s) aguardando {what}; último valor: {last!r}")


def err_code(r: httpx.Response) -> Optional[str]:
    try:
        return (r.json().get("error") or {}).get("code")
    except Exception:
        return None


MAILPIT_URL = os.environ.get("RD_E2E_MAILPIT_URL", "http://mailpit:8025")


async def invite_user(adm: "Session", *, org_id: Optional[str], role: str, tag: str, scope_type: str = "ORGANIZATION") -> dict:
    """Convite REAL: POST /auth/invites → e-mail entregue ao Mailpit → token lido do e-mail → aceite.
    Devolve credenciais em memória (nunca impressas)."""
    import re

    email = f"qa002.{role}.{tag}@example.com"
    body = {"email": email, "role": role, "scope_type": scope_type, "name": f"{PREFIX}{role}_{tag}"}
    if org_id:
        body["scope_id"] = org_id
    r = await adm.post("/api/v1/auth/invites", json=body)
    if r.status_code not in (200, 201):
        raise E2EError(f"convite {role}: HTTP {r.status_code} {r.text[:300]}")
    async with httpx.AsyncClient(base_url=MAILPIT_URL, timeout=20) as mp:
        async def _msg():
            rr = await mp.get("/api/v1/search", params={"query": f'to:"{email}"'})
            msgs = rr.json().get("messages") or []
            return msgs[0]["ID"] if msgs else None
        mid = await wait_until(_msg, timeout=90, interval=1, what="e-mail de convite no Mailpit")
        msg = (await mp.get(f"/api/v1/message/{mid}")).json()
    m = re.search(r"token=([A-Za-z0-9_\-\.~%]+)", msg.get("Text") or msg.get("HTML") or "")
    if not m:
        raise E2EError("token de convite não encontrado no e-mail")
    password = "Qa002" + secrets.token_urlsafe(18)
    async with new_client() as c:
        r = await c.post("/api/v1/auth/invites/accept", json={"token": m.group(1), "name": f"{PREFIX}{role}_{tag}",
                                                              "password": password})
    if r.status_code != 200:
        raise E2EError(f"aceite do convite: HTTP {r.status_code} {r.text[:300]}")
    return {"email": email, "password": password, "totp_secret": None}


def valid_cnpj(alnum: bool = False) -> str:
    from app.modules.organizations.documents import make_cnpj

    if alnum:
        return make_cnpj(("Q" + uuid.uuid4().hex.upper())[:12])
    return make_cnpj(str(int.from_bytes(secrets.token_bytes(6), "big") % 10**12).zfill(12))


async def new_advertiser(adm: "Session", tag: str, *, city: str, uf: str, alnum_cnpj: bool = False) -> tuple[str, dict]:
    """Organização ANUNCIANTE com cadastro de pagador completo + usuário anunciante convidado."""
    r = await adm.post("/api/v1/organizations", json={
        "name": f"{PREFIX}anunciante_{tag}", "document": valid_cnpj(alnum_cnpj), "document_type": "CNPJ",
        "business_roles": ["ADVERTISER"], "trade_name": f"{PREFIX}anunciante", "responsible_name": f"{PREFIX}responsavel",
        "email": f"qa002.anunciante.{tag}@example.com", "phone": "65999991234", "address": "Rua de Teste QA",
        "number": "100", "neighborhood": "Centro", "city": city, "state": uf, "postal_code": "78005000",
        "notes": "Organização anunciante criada pelo QA da AÇÃO 002 (E2E)."})
    if r.status_code != 201:
        raise E2EError(f"org anunciante: HTTP {r.status_code} {r.text[:300]}")
    org_id = r.json()["id"]
    creds = await invite_user(adm, org_id=org_id, role="advertiser", tag=tag)
    return org_id, creds


# ─── Player simulado (referência RDPP/1 do backend) ──────────────────────────

@dataclass
class LivePlayer:
    client: httpx.AsyncClient
    key: Any = None
    device_id: Optional[str] = None
    screen_id: Optional[str] = None
    request_id: Optional[str] = None
    pairing_code: Optional[str] = None
    token: Optional[str] = None
    server_keys: list = field(default_factory=list)
    seq: int = 0
    last_hash: str = "0" * 64

    def __post_init__(self) -> None:
        from app.modules.playback import protocol as proto

        if self.key is None:
            self.key = proto.generate_private_key()

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def request_pairing(self) -> dict:
        from app.modules.playback import protocol as proto

        r = await self.client.post("/api/v1/player/pairing/requests", json={
            "public_key": proto.public_spki_b64u(self.key), "key_alg": "ES256", "platform": "web",
            "app_version": "1.0.0", "device_info": {"model": f"{PREFIX}player", "os": "Linux", "resolution": "1920x1080"}},
            headers={"Origin": PLAYER_ORIGIN})
        if r.status_code != 201:
            raise E2EError(f"pairing request HTTP {r.status_code} {r.text[:200]}")
        b = r.json()
        self.request_id, self.pairing_code = b["pairing_request_id"], b["pairing_code"]
        return {**b, "_headers": dict(r.headers)}

    async def fetch_result(self) -> httpx.Response:
        from app.modules.playback import protocol as proto

        ts = now_ms()
        sig = proto.sign_es256(self.key, {"pairing_request_id": self.request_id, "purpose": proto.PURPOSE_PAIRING_RESULT,
                                          "ts_ms": ts})
        r = await self.client.get(f"/api/v1/player/pairing/requests/{self.request_id}/result",
                                  headers={"X-RD-Device-Ts-Ms": str(ts), "X-RD-Device-Signature": sig})
        if r.status_code == 200:
            b = r.json()
            self.device_id, self.screen_id, self.server_keys = b["device_id"], b["screen_id"], b.get("server_keys") or []
        return r

    async def authenticate(self) -> httpx.Response:
        from app.modules.playback import protocol as proto

        r = await self.client.post("/api/v1/player/auth/challenge", json={"device_id": self.device_id})
        if r.status_code != 200:
            return r
        ch = r.json()["challenge"]
        sig = proto.sign_es256(self.key, {"challenge": ch, "device_id": self.device_id, "purpose": proto.PURPOSE_AUTH})
        r = await self.client.post("/api/v1/player/auth/token", json={"device_id": self.device_id, "challenge": ch,
                                                                     "signature": sig})
        if r.status_code == 200:
            self.token = r.json()["access_token"]
        return r

    async def ws_ticket(self) -> str:
        r = await self.client.post("/api/v1/player/ws-ticket", headers=self.headers)
        if r.status_code != 200:
            raise E2EError(f"ws-ticket HTTP {r.status_code}")
        return r.json()["ticket"]

    def event(self, manifest: dict, ckey: str, *, start: int, expected: int, actual: int, insertion_id: Optional[str],
              result: str = "COMPLETED", **over: Any) -> dict:
        from app.modules.playback import protocol as proto

        cr = manifest["creatives"][ckey]
        self.seq += 1
        ev = {
            "schema": "rd.pop/1", "playback_uuid": str(uuid.uuid4()), "device_id": self.device_id,
            "screen_id": self.screen_id, "creative_id": cr["creative_id"], "creative_code": cr["code"],
            "campaign_id": cr.get("campaign_id"), "insertion_id": insertion_id, "kind": cr["kind"],
            "manifest_version": manifest["version"], "started_at_ms": start, "finished_at_ms": start + actual,
            "expected_ms": expected, "actual_ms": actual, "result": result, "seq": self.seq,
            "nonce": proto.b64u_encode(secrets.token_bytes(16)), "prev_hash": self.last_hash, "clock_offset_ms": 0,
            "offline": False,
        }
        ev.update(over)
        self.last_hash = proto.sha256_hex_of(ev)
        return ev

    def signed(self, ev: dict) -> dict:
        from app.modules.playback import protocol as proto

        return {"event": ev, "signature": proto.sign_es256(self.key, ev)}


# ─── Registro de passos (matriz passo × resultado) ───────────────────────────

@dataclass
class StepLog:
    name: str
    steps: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    created: dict = field(default_factory=dict)

    def record(self, n: Any, title: str, ok: bool, t0: float, evidence: dict) -> None:
        item = {"step": n, "title": title, "result": "PASS" if ok else "FAIL",
                "duration_s": round(time.monotonic() - t0, 2), "evidence": evidence}
        self.steps.append(item)
        print(f"[E2E] passo {n} — {title}: {item['result']} ({item['duration_s']} s) {json.dumps(evidence, ensure_ascii=False, default=str)[:900]}",
              flush=True)

    def dump(self, extra: Optional[dict] = None) -> Optional[str]:
        out = {"name": self.name, "total_s": round(time.monotonic() - self.started, 2), "steps": self.steps,
               "created": self.created, **(extra or {})}
        txt = json.dumps(out, ensure_ascii=False, indent=1, default=str)
        if REPORT_DIR:
            p = Path(REPORT_DIR) / f"{self.name}_{time.strftime('%Y%m%d_%H%M%S')}.json"
            p.write_text(txt, encoding="utf-8")
            return str(p)
        return None
