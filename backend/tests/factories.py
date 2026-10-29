"""
RD Mídia Indoor — Factories e helpers de teste (AÇÃO 002 / W1).

    org = await make_org(db)
    est = await make_establishment(db, org)
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, u)          # login REAL pela API (com TOTP se habilitado)
    r = await client.get("/api/v1/me", headers=s.headers)

Senhas de teste são geradas por execução (nunca fixas no código).
"""
from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.authz import city_key
from app.core.passwords import hash_password
from app.models.establishments import Establishment
from app.models.organizations import Organization

try:  # bitmask legado (o W-ORG pode substituí-lo por business_roles)
    from app.models.organizations import OrganizationTypeFlag
except ImportError:  # pragma: no cover
    OrganizationTypeFlag = None  # type: ignore[assignment]
from app.models.rbac import Role, RoleAssignment
from app.models.users import User
from app.modules.auth import service as svc

CSRF = {"X-RD-CSRF": "1", "Origin": "http://testserver"}

_HASH_CACHE: dict[str, str] = {}


def new_password() -> str:
    return f"Tst-{secrets.token_hex(6)}9z"


def _hash(pw: str) -> str:
    if pw not in _HASH_CACHE:
        _HASH_CACHE[pw] = hash_password(pw)
    return _HASH_CACHE[pw]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def make_org(
    db: AsyncSession,
    name: Optional[str] = None,
    typ: Optional[int] = None,
    business_roles: Optional[list[str]] = None,
    **extra: Any,
) -> Organization:
    """Organização de teste. ``business_roles`` (modelo novo do W-ORG) ou ``typ`` (bitmask legado)."""
    kw: dict = dict(
        id=uuid.uuid4(),
        name=name or f"Org {secrets.token_hex(3)}",
        document=f"T{secrets.token_hex(7)}",
        document_type="CNPJ",
        is_active=True,
    )
    cols = set(Organization.__table__.columns.keys())
    if "business_roles" in cols:
        kw["business_roles"] = business_roles or ["ADVERTISER"]
    if "type" in cols:
        kw["type"] = typ if typ is not None else 1  # IS_ADVERTISER
    kw.update(extra)
    o = Organization(**kw)
    db.add(o)
    await db.commit()
    return o


async def make_establishment(db: AsyncSession, org: Organization, city: str = "Goiânia", state: str = "GO",
                             tz: str = "America/Sao_Paulo") -> Establishment:
    e = Establishment(id=uuid.uuid4(), org_id=org.id, name=f"Est {secrets.token_hex(3)}", city=city, state=state,
                      country="BRA", timezone=tz, is_active=True)
    db.add(e)
    await db.commit()
    return e


async def assign(db: AsyncSession, user: User, role: str, scope: Optional[tuple] = None) -> RoleAssignment:
    r = (await db.execute(select(Role).where(Role.slug == role))).scalar_one()
    stype, sid, sval = "GLOBAL", None, None
    if scope:
        stype = scope[0]
        if stype == "CITY":
            sval = scope[1] if ":" in str(scope[1]) or str(scope[1]).isdigit() else city_key("GO", scope[1])
        else:
            sid = scope[1]
    ra = RoleAssignment(id=uuid.uuid4(), user_id=user.id, role_id=r.id, scope_type=stype, scope_id=sid,
                        scope_value=sval)
    db.add(ra)
    await db.commit()
    return ra


async def make_user(
    db: AsyncSession,
    *,
    role: Optional[str] = None,
    scope: Optional[tuple] = None,
    email: Optional[str] = None,
    password: Optional[str] = None,
    name: Optional[str] = None,
    mfa: bool = False,
    admin_master_seat: Optional[int] = None,
    must_change_password: bool = False,
    is_active: bool = True,
    phone: Optional[str] = None,
    hashed_password: Optional[str] = None,
    financial_password: Any = None,
) -> User:
    """Usuário de teste. Titular de assento (``admin_master_seat``) recebe senha financeira de teste
    (atributo ``.fin_password``) salvo ``financial_password=False``; ``financial_password="..."`` fixa o valor.

    O assento criado aqui NÃO tem evento selado: testes que passam pelo motor financeiro/integridade usam
    ``init_approval_mode`` + ``adopt_master_seats`` (AÇÃO 003).
    """
    pw = password or new_password()
    u = User(
        id=uuid.uuid4(),
        email=(email or f"u{secrets.token_hex(5)}@example.com").lower(),
        full_name=name or "Usuário Teste",
        hashed_password=hashed_password or _hash(pw),
        is_active=is_active,
        is_verified=True,
        must_change_password=must_change_password,
        admin_master_seat=admin_master_seat,
        phone=phone,
    )
    secret = None
    if mfa:
        secret = totp.new_secret()
        u.totp_enabled = True
        u.totp_secret_enc = svc.encrypt_totp_secret(u.id, secret)
    fin_pw = None
    if financial_password is None:
        financial_password = admin_master_seat is not None
    if financial_password:
        fin_pw = financial_password if isinstance(financial_password, str) else f"Fin-{secrets.token_hex(6)}"
        u.financial_password_hash = _hash(fin_pw)
        from app.core.timeutils import utcnow

        u.financial_password_changed_at = utcnow()
    db.add(u)
    await db.commit()
    if role:
        await assign(db, u, role, scope)
    u.password = pw  # type: ignore[attr-defined]
    u.fin_password = fin_pw  # type: ignore[attr-defined]
    u.totp_secret = secret  # type: ignore[attr-defined]
    u._last_step = None  # type: ignore[attr-defined]
    return u


def totp_code(user: User, *, fresh: bool = True) -> str:
    """Código TOTP válido ainda não usado (anti-replay) para o usuário de teste."""
    secret = user.totp_secret  # type: ignore[attr-defined]
    step = totp.current_step()
    last = getattr(user, "_last_step", None)
    if fresh and last is not None and step <= last:
        step = last + 1
        if step > totp.current_step() + totp.WINDOW:
            time.sleep((step - totp.WINDOW) * totp.PERIOD - time.time() + 0.05)
    user._last_step = step  # type: ignore[attr-defined]
    return totp.code_at_step(secret, step)


@dataclass
class Session:
    user: User
    access_token: str
    body: dict

    @property
    def headers(self) -> dict:
        return auth_headers(self.access_token)


async def login_as(client: httpx.AsyncClient, user: User, *, password: Optional[str] = None, expect_ok: bool = True) -> Session:
    r = await client.post("/api/v1/auth/login", json={"login": user.email, "password": password or user.password})
    assert r.status_code == 200, r.text
    body = r.json()
    if body["status"] == "mfa_required":
        r = await client.post("/api/v1/auth/mfa/verify", json={"mfa_token": body["mfa_token"], "code": totp_code(user)})
        assert r.status_code == 200, r.text
        body = r.json()
    if expect_ok:
        assert body["status"] == "ok", body
    return Session(user=user, access_token=body.get("access_token", ""), body=body)


async def setup_mfa_via_api(client: httpx.AsyncClient, user: User, token: str) -> dict:
    """Configura TOTP pela API (setup + enable) e devolve a resposta do enable."""
    r = await client.post("/api/v1/auth/mfa/totp/setup", json={"password": user.password}, headers=auth_headers(token))
    assert r.status_code == 200, r.text
    user.totp_secret = r.json()["secret"]  # type: ignore[attr-defined]
    user._last_step = None  # type: ignore[attr-defined]
    r = await client.post("/api/v1/auth/mfa/totp/enable", json={"code": totp_code(user)}, headers=auth_headers(token))
    assert r.status_code == 200, r.text
    return r.json()


async def get_user(db: AsyncSession, user_id: Any) -> User:
    """Recarrega o usuário do banco (sem expirar outros objetos da sessão)."""
    q = select(User).where(User.id == user_id).execution_options(populate_existing=True)
    return (await db.execute(q)).scalar_one()


# ─── AÇÃO 003: modo de aprovação e assentos selados (contrato SPEC §13) ──────

async def init_approval_mode(db: AsyncSession, mode: str) -> None:
    """Inicializa o modo de aprovação financeira da instância de teste (``BOOTSTRAP_ENV``) e faz commit.

    Idempotente (já inicializado ⇒ nada muda). ``mode``: ``"SINGLE"`` | ``"DUAL"``.
    """
    from app.modules.security import approval_mode

    await approval_mode.initialize(db, mode=mode, source="BOOTSTRAP_ENV",
                                   env_value="true" if mode == "DUAL" else "false")
    await db.commit()


async def adopt_master_seats(db: AsyncSession, *users: Any) -> None:
    """Sela (evento ``LEGACY_ADOPTED``) os assentos de usuários criados pela fábrica com ``admin_master_seat``.

    Exige o modo já inicializado (``init_approval_mode``). Ordem pelo assento; commit no fim.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.modules.security import masters

    ids = [u if isinstance(u, uuid.UUID) else sa_inspect(u).identity[0] for u in users]
    rows = (await db.execute(select(User.id, User.admin_master_seat).where(User.id.in_(ids)))).all()
    for uid, seat in sorted(rows, key=lambda r: r[1] or 0):
        if seat is None:
            raise ValueError("adopt_master_seats: usuário sem admin_master_seat")
        await masters.assign_seat(db, user_id=uid, seat=int(seat), source="LEGACY_ADOPTED")
    await db.commit()
