"""Helpers dos testes de segurança da instância (AÇÃO 003 — agente B).

- ``bootstrap_master``: roda o ``bootstrap_instance`` REAL (ENV + arquivo de senha em /tmp do container) e
  devolve o Master primário (senha temporária gerada por execução; nunca impressa).
- ``complete_first_access``: percorre o primeiro acesso pela API (senha → TOTP → senha financeira).
- ``active_master``: Master ativo pela fábrica + modo inicializado + assento selado (LEGACY_ADOPTED).
- ``capture_events``: captura ``events.publish`` (tempo real) sem Redis Pub/Sub.
"""
from __future__ import annotations

import re
import secrets
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from app.core import totp
from tests.factories import adopt_master_seats, auth_headers, init_approval_mode, make_user, totp_code

API = "/api/v1"


def strong_password(prefix: str = "Sec") -> str:
    return f"{prefix}{secrets.token_hex(8)}7b"


async def bootstrap_master(*, dual: bool = True, email: Optional[str] = None, name: str = "Sócia A") -> SimpleNamespace:
    from app.modules.security import bootstrap as boot

    pw = strong_password("Boot")
    email = email or f"master.{secrets.token_hex(3)}@rd-midiaindoor.localhost"
    d = Path(tempfile.mkdtemp(prefix="rd-sec-"))
    f = d / "bootstrap_master_password"
    f.write_text(pw, encoding="utf-8")
    res = await boot.bootstrap_instance({
        "RD_BOOTSTRAP_DUAL_MASTER_APPROVAL": "true" if dual else "false",
        "RD_BOOTSTRAP_MASTER_EMAIL": email,
        "RD_BOOTSTRAP_MASTER_NAME": name,
        "RD_BOOTSTRAP_MASTER_PASSWORD_FILE": str(f),
    })
    f.unlink()
    return SimpleNamespace(email=email, password=pw, name=name, result=res, totp_secret=None, _last_step=None,
                           fin_password=None)


def code_for(user: Any) -> str:
    return totp_code(user)


async def complete_first_access(client, m: SimpleNamespace) -> dict:
    """Login → senha → TOTP → senha financeira. Devolve o corpo final (com ``access_token``)."""
    r = await client.post(f"{API}/auth/login", json={"login": m.email, "password": m.password})
    assert r.status_code == 200 and r.json()["status"] == "mfa_setup_required", r.text
    tok = r.json()["mfa_setup_token"]
    new_pw = strong_password("Nova")
    r = await client.post(f"{API}/auth/setup/password", json={"current_password": m.password, "new_password": new_pw},
                          headers=auth_headers(tok))
    assert r.status_code == 200, r.text
    m.password = new_pw
    tok = r.json()["mfa_setup_token"]
    r = await client.post(f"{API}/auth/mfa/totp/setup", json={"password": m.password}, headers=auth_headers(tok))
    assert r.status_code == 200, r.text
    m.totp_secret = r.json()["secret"]
    m._last_step = None
    r = await client.post(f"{API}/auth/mfa/totp/enable", json={"code": code_for(m)}, headers=auth_headers(tok))
    assert r.status_code == 200, r.text
    access = r.json()["access_token"]
    m.fin_password = "Fin-" + secrets.token_hex(6)
    r = await client.post(f"{API}/auth/setup/financial-password",
                          json={"login_password": m.password, "totp_code": code_for(m),
                                "financial_password": m.fin_password}, headers=auth_headers(access))
    assert r.status_code == 200, r.text
    m.access_token = access
    m.headers = auth_headers(access)
    return r.json()


async def active_master(db, *, seat: int = 1, mode: str = "DUAL", name: str = "Sócio A", **kw) -> Any:
    u = await make_user(db, role="super_admin", mfa=True, admin_master_seat=seat, name=name, **kw)
    await init_approval_mode(db, mode)
    await adopt_master_seats(db, u)
    return u


TOKEN_RE = re.compile(r"/ativar-master#token=([A-Za-z0-9_\-]+)")


def token_from_mail(mail, email: str) -> str:
    msg = mail.last_to(email)
    assert msg is not None, "e-mail de convite não enviado"
    m = TOKEN_RE.search(msg.text)
    assert m, "link de ativação ausente"
    return m.group(1)


class EventCapture:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict, dict]] = []

    def of_type(self, t: str) -> list:
        return [x for x in self.items if x[0] == t]


def capture_events(monkeypatch) -> EventCapture:
    from app.core import events

    cap = EventCapture()
    orig = events.publish

    async def _pub(type: str, data: dict, **scope: Any) -> dict:
        cap.items.append((type, data, scope))
        return await orig(type, data, **scope)

    monkeypatch.setattr(events, "publish", _pub)
    return cap


def new_totp_user_secret() -> str:
    return totp.new_secret()
