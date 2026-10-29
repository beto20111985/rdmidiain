"""
Apoio aos E2E da AÇÃO 003 (QA — agente G): modo de aprovação ÚNICA × DUPLA, primeiro acesso, 2º Admin
Master, transições e bypass por ENV — contra INSTÂNCIAS DESCARTÁVEIS (nunca a instância de dev: o primeiro
acesso do Master real é do proprietário).

Instâncias (operador; ver ``stack/scripts/003/g/run_qa003_e2e.sh``): banco ``rdqa003_<modo>`` no mesmo
PostgreSQL (migrado como ``rd_owner``), backend ``rd-qa003-<modo>`` (mesma imagem, runtime ``rd_app``),
Redis em índice exclusivo, bootstrap ``python -m app.cli bootstrap-instance`` com
``RD_BOOTSTRAP_DUAL_MASTER_APPROVAL=false|true`` e e-mail ``master.<modo>@rd-qa.localhost``.

Habilitação (pulados por padrão — ``tests/e2e/conftest.py``):
    RD_E2E_QA003=1
    RD_E2E_QA003_DIR=/tmp/qa003          senha inicial de TESTE do Master (<modo>_master_pw) + estado entre fases
    RD_E2E_QA003_SINGLE_URL / _DUAL_URL  (padrão http://rd-qa003-single:8000 / http://rd-qa003-dual:8000)
    RD_E2E_QA003_PANEL_SINGLE / _DUAL    Origin permitido do painel descartável (padrão http://127.0.0.1:5191/5192)
    RD_E2E_MAILPIT_URL                   (padrão http://mailpit:8025)
    RD_E2E_REPORT_DIR                    (opcional) evidências JSON por cenário

Credenciais: TODAS de teste, geradas aqui ou pelo operador, guardadas só no arquivo de estado (0600) e
NUNCA impressas; TOTP calculado localmente (pyotp) respeitando o anti-replay (passo estritamente crescente).
Asserções de banco: SOMENTE leitura no banco descartável.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

QA_DIR = Path(os.environ.get("RD_E2E_QA003_DIR", "/tmp/qa003"))
MAILPIT_URL = os.environ.get("RD_E2E_MAILPIT_URL", "http://mailpit:8025").rstrip("/")
REPORT_DIR = os.environ.get("RD_E2E_REPORT_DIR")

INSTANCES = {
    "single": {"url": os.environ.get("RD_E2E_QA003_SINGLE_URL", "http://rd-qa003-single:8000"),
               "panel": os.environ.get("RD_E2E_QA003_PANEL_SINGLE", "http://127.0.0.1:5191"),
               "db": "rdqa003_single", "email": "master.single@rd-qa.localhost", "boot_dual": False},
    "dual": {"url": os.environ.get("RD_E2E_QA003_DUAL_URL", "http://rd-qa003-dual:8000"),
             "panel": os.environ.get("RD_E2E_QA003_PANEL_DUAL", "http://127.0.0.1:5192"),
             "db": "rdqa003_dual", "email": "master.dual@rd-qa.localhost", "boot_dual": True},
}

SECRET_KEY_HINTS = ("hash", "secret", "password", "token", "totp_seed", "otpauth", "recovery")


class QAError(AssertionError):
    pass


# ─── estado entre fases (credenciais de TESTE; arquivo 0600; nunca impresso) ──

def _state_path(mode: str) -> Path:
    return QA_DIR / f"{mode}_state.json"


def load_state(mode: str) -> dict:
    p = _state_path(mode)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_state(mode: str, data: dict) -> None:
    p = _state_path(mode)
    tmp = p.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def initial_password(mode: str) -> str:
    return (QA_DIR / f"{mode}_master_pw").read_text(encoding="utf-8").strip()


def new_login_password() -> str:
    alpha = string.ascii_letters + string.digits
    return "Qa3" + "".join(secrets.choice(alpha) for _ in range(20)) + "7k"


def new_financial_password() -> str:
    return "Fin" + secrets.token_urlsafe(12)


# ─── TOTP com anti-replay (passo estritamente crescente por usuário) ─────────

@dataclass
class Totp:
    secret: str
    last_step: int = 0

    async def code(self) -> str:
        import pyotp

        while True:
            step = int(time.time()) // 30
            if step > self.last_step and (time.time() % 30) < 27:  # evita a borda do passo
                self.last_step = step
                return pyotp.TOTP(self.secret).at(step * 30)
            await asyncio.sleep(max(0.5, 30.5 - (time.time() % 30)))

    def wrong(self) -> str:
        import pyotp

        real = pyotp.TOTP(self.secret).now()
        return f"{(int(real) + 111111) % 1000000:06d}"


# ─── HTTP ────────────────────────────────────────────────────────────────────

def err_code(r: httpx.Response) -> Optional[str]:
    try:
        return (r.json().get("error") or {}).get("code")
    except Exception:
        return None


def err_details(r: httpx.Response) -> dict:
    try:
        return (r.json().get("error") or {}).get("details") or {}
    except Exception:
        return {}


@dataclass
class Master:
    """Admin Master de TESTE (A = assento 1 pelo bootstrap; B = assento 2 por convite)."""
    mode: str
    label: str
    email: str
    password: str
    totp: Optional[Totp] = None
    fin_password: Optional[str] = None
    access_token: Optional[str] = None
    client: Optional[httpx.AsyncClient] = None
    user_id: Optional[str] = None

    @property
    def inst(self) -> dict:
        return INSTANCES[self.mode]

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}

    def ensure_client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(base_url=self.inst["url"], timeout=60)
        return self.client

    async def req(self, method: str, path: str, *, auth: bool = True, **kw) -> httpx.Response:
        c = self.ensure_client()
        hdrs = {**(self.headers if auth else {}), **(kw.pop("headers", None) or {})}
        return await c.request(method, f"/api/v1{path}", headers=hdrs, **kw)

    async def get(self, path: str, **kw) -> httpx.Response:
        return await self.req("GET", path, **kw)

    async def post(self, path: str, **kw) -> httpx.Response:
        return await self.req("POST", path, **kw)

    async def delete(self, path: str, **kw) -> httpx.Response:
        return await self.req("DELETE", path, **kw)

    async def refresh(self) -> httpx.Response:
        r = await self.post("/auth/refresh", auth=False, headers={"X-RD-CSRF": "1", "Origin": self.inst["panel"]})
        if r.status_code == 200:
            self.access_token = r.json()["access_token"]
        return r

    async def login(self) -> httpx.Response:
        """Login completo (senha + TOTP). Devolve a resposta final."""
        if self.client is not None:
            await self.client.aclose()
        self.client = None
        self.access_token = None
        r = await self.post("/auth/login", auth=False, json={"login": self.email, "password": self.password})
        if r.status_code != 200:
            return r
        body = r.json()
        if body.get("status") == "mfa_required":
            r = await self.post("/auth/mfa/verify", auth=False,
                                json={"mfa_token": body["mfa_token"], "code": await self.totp.code()})
            if r.status_code != 200:
                return r
            body = r.json()
        if body.get("status") == "ok":
            self.access_token = body["access_token"]
            self.user_id = (body.get("user") or {}).get("id") or self.user_id
        return r

    def to_state(self) -> dict:
        return {"label": self.label, "email": self.email, "password": self.password,
                "totp_secret": self.totp.secret if self.totp else None,
                "totp_last_step": self.totp.last_step if self.totp else 0,
                "fin_password": self.fin_password, "user_id": self.user_id}

    @classmethod
    def from_state(cls, mode: str, d: dict) -> "Master":
        t = Totp(d["totp_secret"], int(d.get("totp_last_step") or 0)) if d.get("totp_secret") else None
        return cls(mode=mode, label=d["label"], email=d["email"], password=d["password"], totp=t,
                   fin_password=d.get("fin_password"), user_id=d.get("user_id"))

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None


# ─── Mailpit ─────────────────────────────────────────────────────────────────

_CODE_RE = re.compile(r"é (\d{6})\.")
_INVITE_RE = re.compile(r"ativar-master#token=([A-Za-z0-9_\-]+)")


async def _mail_find(email: str, rx: re.Pattern, since: float, timeout: float = 60) -> tuple[str, dict]:
    from datetime import datetime

    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=MAILPIT_URL, timeout=20) as mp:
        while time.monotonic() < deadline:
            r = await mp.get("/api/v1/search", params={"query": f'to:"{email}"', "limit": 20})
            for m in r.json().get("messages") or []:
                created = datetime.fromisoformat(m["Created"].replace("Z", "+00:00")).timestamp()
                if created < since - 2:
                    continue
                full = (await mp.get(f"/api/v1/message/{m['ID']}")).json()
                found = rx.search(full.get("Text") or "") or rx.search(full.get("HTML") or "")
                if found:
                    return found.group(1), {"id": m["ID"], "subject": m.get("Subject"),
                                            "to": [t.get("Address") for t in (m.get("To") or [])],
                                            "created": m["Created"]}
            await asyncio.sleep(1)
    raise QAError(f"e-mail esperado não chegou ao Mailpit ({rx.pattern})")


async def mail_code(email: str, since: float) -> str:
    code, _ = await _mail_find(email, _CODE_RE, since)
    return code


async def mail_invite(email: str, since: float) -> tuple[str, dict]:
    return await _mail_find(email, _INVITE_RE, since)


async def mail_recipients_since(since: float, rx: re.Pattern) -> list[str]:
    """Destinatários de mensagens (após ``since``) cujo corpo casa ``rx`` — prova de "só para B"."""
    from datetime import datetime

    out = []
    async with httpx.AsyncClient(base_url=MAILPIT_URL, timeout=20) as mp:
        r = await mp.get("/api/v1/messages", params={"limit": 100})
        for m in r.json().get("messages") or []:
            created = datetime.fromisoformat(m["Created"].replace("Z", "+00:00")).timestamp()
            if created < since - 2:
                continue
            full = (await mp.get(f"/api/v1/message/{m['ID']}")).json()
            if rx.search(full.get("Text") or "") or rx.search(full.get("HTML") or ""):
                out += [t.get("Address") for t in (m.get("To") or [])]
    return out


# ─── Banco descartável (SOMENTE leitura) ─────────────────────────────────────

_engines: dict = {}


def q(mode: str, sql: str, **params) -> list:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from app.core.config import settings

    db = INSTANCES[mode]["db"]
    assert db.startswith("rdqa003_"), "asserções só em banco descartável"
    if db not in _engines:
        url = make_url(settings.DATABASE_URL).set(drivername="postgresql+psycopg2", database=db)
        _engines[db] = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=1)
    with _engines[db].connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        return [tuple(r) for r in c.execute(text(sql), params).all()]


def q1(mode: str, sql: str, **params) -> Any:
    rows = q(mode, sql, **params)
    return rows[0] if rows else None


def counts(mode: str) -> dict:
    r = q1(mode, """
        SELECT rd_security_current_approval_mode(),
               (SELECT count(*) FROM instance_approval_mode_history),
               (SELECT coalesce(max(mode_version), 0) FROM instance_approval_mode_history),
               (SELECT count(*) FROM admin_master_seat_events),
               (SELECT count(*) FROM users WHERE admin_master_seat IS NOT NULL),
               rd_active_master_count(),
               (SELECT count(*) FROM users),
               (SELECT count(*) FROM role_assignments ra JOIN roles r ON r.id = ra.role_id WHERE r.slug = 'super_admin'),
               (SELECT count(*) FROM admin_master_invites)""")
    keys = ("mode", "mode_rows", "mode_version", "seat_events", "seat_holders", "active_masters", "users",
            "super_admin", "invites")
    return dict(zip(keys, r))


def audit_count(mode: str, action: str) -> int:
    return int(q1(mode, "SELECT count(*) FROM audit_logs WHERE action = :a", a=action)[0])


# ─── Evidências ──────────────────────────────────────────────────────────────

@dataclass
class Evidence:
    name: str
    steps: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    failures: list = field(default_factory=list)

    def check(self, step_id: Any, title: str, ok: bool, /, **evidence: Any) -> bool:
        item = {"step": step_id, "title": title, "result": "PASS" if ok else "FAIL", "evidence": evidence}
        self.steps.append(item)
        if not ok:
            self.failures.append(f"{step_id} {title}")
        print(f"[QA003] {self.name} {step_id} — {title}: {item['result']} "
              f"{json.dumps(evidence, ensure_ascii=False, default=str)[:700]}", flush=True)
        return ok

    def dump(self) -> Optional[str]:
        out = {"name": self.name, "total_s": round(time.monotonic() - self.started, 1),
               "passed": sum(1 for s in self.steps if s["result"] == "PASS"),
               "failed": len(self.failures), "steps": self.steps}
        if REPORT_DIR:
            p = Path(REPORT_DIR) / f"{self.name}_{time.strftime('%Y%m%d_%H%M%S')}.json"
            p.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            return str(p)
        return None

    def assert_ok(self) -> None:
        self.dump()
        if self.failures:
            raise QAError(f"{self.name}: {len(self.failures)} falha(s): {self.failures}")


def leaked_secrets(payload: Any, secrets_: list[str]) -> list[str]:
    """Chaves suspeitas e valores secretos conhecidos presentes num JSON de status (deve ser [])."""
    found: list[str] = []
    allowed_keys = {"mfa_setup_token", "access_token", "token_type", "expires_in", "password_change",
                    "financial_password", "has_financial_password", "must_change_password"}

    def walk(v: Any, path: str) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                kl = str(k).lower()
                if kl not in allowed_keys and any(h in kl for h in SECRET_KEY_HINTS):
                    found.append(f"chave:{path}.{k}")
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
        elif isinstance(v, str):
            for s in secrets_:
                if s and len(s) >= 6 and s in v:
                    found.append(f"valor:{path}")

    walk(payload, "$")
    return found


# ─── Primeiro acesso completo (com as recusas do guard em cada etapa) ────────

async def first_access(m: Master, ev: Evidence, prefix: str, *, expect_mode: str) -> None:
    """Login inicial → troca de senha → TOTP → senha financeira, provando no BACKEND a ordem e o guard
    (SPEC §3): com o token de cada etapa, rotas fora do setup são recusadas."""
    initial = m.password
    r = await m.post("/auth/login", auth=False, json={"login": m.email, "password": initial})
    b = r.json() if r.status_code == 200 else {}
    ev.check(f"{prefix}.1", "login inicial ⇒ mfa_setup_required, next_step=PASSWORD_CHANGE (sem cookie)",
             r.status_code == 200 and b.get("status") == "mfa_setup_required"
             and (b.get("setup") or {}).get("next_step") == "PASSWORD_CHANGE" and "rd_refresh" not in r.cookies,
             http=r.status_code, status=b.get("status"), setup=b.get("setup"))
    setup_tok = b.get("mfa_setup_token")
    m.user_id = (b.get("user") or {}).get("id")
    sh = {"Authorization": f"Bearer {setup_tok}"}

    # token de setup fora das rotas de setup
    probes = [("GET", "/me"), ("GET", "/organizations"), ("GET", "/financial/approvals"), ("POST", "/realtime/ticket"),
              ("GET", "/security/approval-mode")]
    res = {}
    for meth, path in probes:
        rr = await m.req(meth, path, auth=False, headers=sh)
        res[f"{meth} {path}"] = (rr.status_code, err_code(rr))
    ev.check(f"{prefix}.2", "token mfa_setup fora do setup ⇒ 401 INVALID_TOKEN (não é sessão)",
             all(v[0] == 401 for v in res.values()), results=res)
    rr = await m.post("/auth/mfa/totp/setup", auth=False, headers=sh, json={"password": initial})
    ev.check(f"{prefix}.3", "TOTP antes da troca de senha ⇒ 403 PASSWORD_CHANGE_REQUIRED (ordem no servidor)",
             rr.status_code == 403 and err_code(rr) == "PASSWORD_CHANGE_REQUIRED",
             http=rr.status_code, code=err_code(rr), details=err_details(rr))
    rr = await m.post("/auth/setup/financial-password", auth=False, headers=sh,
                      json={"login_password": initial, "totp_code": "123456", "financial_password": "x" * 10})
    ev.check(f"{prefix}.4", "senha financeira com token de setup ⇒ recusada (exige sessão)",
             rr.status_code in (401, 403, 409), http=rr.status_code, code=err_code(rr))
    st = await m.get("/auth/setup/status", auth=False, headers=sh)
    ev.check(f"{prefix}.5", "GET /auth/setup/status com token de setup ⇒ 200 sem segredo",
             st.status_code == 200 and not leaked_secrets(st.json(), [initial]),
             http=st.status_code, setup=st.json() if st.status_code == 200 else None,
             leaks=leaked_secrets(st.json(), [initial]) if st.status_code == 200 else None)

    # passo 1 — troca de senha
    new_pw = new_login_password()
    rr = await m.post("/auth/setup/password", auth=False, headers=sh,
                      json={"current_password": initial, "new_password": initial})
    ev.check(f"{prefix}.6", "nova senha igual à atual ⇒ 400 PASSWORD_REUSED", rr.status_code == 400
             and err_code(rr) == "PASSWORD_REUSED", http=rr.status_code, code=err_code(rr))
    rr = await m.post("/auth/setup/password", auth=False, headers=sh,
                      json={"current_password": initial, "new_password": new_pw})
    b2 = rr.json() if rr.status_code == 200 else {}
    ev.check(f"{prefix}.7", "troca de senha ⇒ 200 + NOVO token de setup; next_step=TOTP",
             rr.status_code == 200 and b2.get("status") == "mfa_setup_required" and b2.get("mfa_setup_token")
             and b2.get("mfa_setup_token") != setup_tok and (b2.get("setup") or {}).get("next_step") == "TOTP",
             http=rr.status_code, setup=b2.get("setup"))
    m.password = new_pw
    old = await m.get("/auth/setup/status", auth=False, headers=sh)
    ev.check(f"{prefix}.8", "token de setup ANTERIOR à troca ⇒ 401", old.status_code == 401,
             http=old.status_code, code=err_code(old))
    sh = {"Authorization": f"Bearer {b2.get('mfa_setup_token')}"}

    # passo 2 — TOTP
    rr = await m.post("/auth/mfa/totp/setup", auth=False, headers=sh, json={"password": new_pw})
    ok = rr.status_code == 200 and rr.json().get("secret")
    ev.check(f"{prefix}.9", "TOTP setup (após a troca) ⇒ 200 (otpauth local)", bool(ok), http=rr.status_code)
    m.totp = Totp(rr.json()["secret"])
    rr = await m.post("/auth/mfa/totp/enable", auth=False, headers=sh, json={"code": m.totp.wrong()})
    ev.check(f"{prefix}.10", "TOTP enable com código errado ⇒ 400 INVALID_MFA_CODE",
             rr.status_code == 400 and err_code(rr) == "INVALID_MFA_CODE", http=rr.status_code, code=err_code(rr))
    rr = await m.post("/auth/mfa/totp/enable", auth=False, headers=sh, json={"code": await m.totp.code()})
    b3 = rr.json() if rr.status_code == 200 else {}
    ev.check(f"{prefix}.11", "TOTP enable ⇒ 200 + sessão (access + cookie rd_refresh) + códigos de recuperação",
             rr.status_code == 200 and b3.get("access_token") and len(b3.get("recovery_codes") or []) >= 8
             and "rd_refresh" in rr.cookies, http=rr.status_code, n_recovery=len(b3.get("recovery_codes") or []))
    m.access_token = b3.get("access_token")
    if m.client is not None and "rd_refresh" in rr.cookies:
        pass  # o cookie fica no jar do cliente (refresh posterior)

    # sessão com passo FINANCIAL_PASSWORD pendente
    res = {}
    for meth, path in [("GET", "/organizations"), ("GET", "/users"), ("GET", "/financial/approvals"),
                       ("POST", "/realtime/ticket"), ("GET", "/security/approval-mode"), ("GET", "/security/masters"),
                       ("GET", "/settings")]:
        rr = await m.req(meth, path)
        res[f"{meth} {path}"] = (rr.status_code, err_code(rr), err_details(rr).get("next_step"))
    ev.check(f"{prefix}.12", "sessão sem senha financeira ⇒ toda rota 403 FINANCIAL_PASSWORD_SETUP_REQUIRED (inclui ticket WS)",
             all(v[0] == 403 and v[1] == "FINANCIAL_PASSWORD_SETUP_REQUIRED" and v[2] == "FINANCIAL_PASSWORD"
                 for v in res.values()), results=res)
    me = await m.get("/me")
    meb = me.json() if me.status_code == 200 else {}
    ev.check(f"{prefix}.13", "GET /me (lenient) ⇒ 200 com security_setup.required=true, next_step=FINANCIAL_PASSWORD, sem segredo",
             me.status_code == 200 and (meb.get("security_setup") or {}).get("next_step") == "FINANCIAL_PASSWORD"
             and (meb.get("security_setup") or {}).get("required") is True
             and not leaked_secrets(meb, [new_pw, initial, m.totp.secret]),
             http=me.status_code, security_setup=meb.get("security_setup"),
             leaks=leaked_secrets(meb, [new_pw, initial, m.totp.secret]))
    rr = await m.post("/auth/setup/password", json={"current_password": new_pw, "new_password": new_login_password()})
    ev.check(f"{prefix}.14", "repetir troca de senha (já feita) ⇒ 409 PASSWORD_CHANGE_NOT_REQUIRED",
             rr.status_code == 409 and err_code(rr) == "PASSWORD_CHANGE_NOT_REQUIRED", http=rr.status_code,
             code=err_code(rr))

    # passo 3 — senha financeira
    fin = new_financial_password()
    rr = await m.post("/auth/setup/financial-password",
                      json={"login_password": new_pw, "totp_code": await m.totp.code(), "financial_password": new_pw})
    ev.check(f"{prefix}.15", "senha financeira igual à de login ⇒ 400 FINANCIAL_PASSWORD_POLICY",
             rr.status_code == 400 and err_code(rr) == "FINANCIAL_PASSWORD_POLICY", http=rr.status_code,
             code=err_code(rr))
    rr = await m.post("/auth/setup/financial-password",
                      json={"login_password": new_pw, "totp_code": await m.totp.code(), "financial_password": fin})
    b4 = rr.json() if rr.status_code == 200 else {}
    s4 = b4.get("setup") or {}
    expected_next = "SECOND_MASTER" if expect_mode == "DUAL" else None
    ev.check(f"{prefix}.16", f"senha financeira ⇒ 200; required=false; next_step={expected_next}",
             rr.status_code == 200 and s4.get("required") is False and s4.get("next_step") == expected_next
             and s4.get("approval_mode") == expect_mode, http=rr.status_code, code=err_code(rr), setup=s4)
    m.fin_password = fin
