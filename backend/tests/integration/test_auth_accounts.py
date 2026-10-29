"""Interfaces de conta para outros módulos (W-ORG / REQ-ONB-004): create_user_account,
grant_client_role, issue_login (+ set_refresh_cookie importável) e record_consent."""
import uuid

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.models.audit import AuditLog
from app.models.rbac import RoleAssignment
from app.modules.auth import accounts
from app.modules.auth.cookies import set_refresh_cookie
from tests.factories import CSRF, auth_headers, get_user, make_establishment, make_org, make_user

PW = "SenhaForte2026x"


async def _audits(db, action):
    return (await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()


# ─── create_user_account ─────────────────────────────────────────────────────

async def test_create_user_account_ok(db):
    u = await accounts.create_user_account(db, email="  Novo.Anunciante@Example.com ", full_name=" Ana Lima ",
                                           password=PW, origin="advertiser_signup")
    await db.commit()
    fresh = await get_user(db, u.id)
    assert fresh.email == "novo.anunciante@example.com" and fresh.full_name == "Ana Lima"
    assert fresh.is_verified and fresh.is_active and fresh.password_changed_at is not None
    assert fresh.hashed_password.startswith("$argon2id$") and fresh.phone is None
    a = (await _audits(db, "user.created"))[0]
    assert a.entity_id == str(u.id) and a.after_data["origin"] == "advertiser_signup" and a.actor_user_id == u.id


async def test_create_user_account_refusals(db, monkeypatch):
    await make_user(db, email="existe@example.com")
    with pytest.raises(Conflict) as ei:
        await accounts.create_user_account(db, email="EXISTE@example.com", full_name="X Y", password=PW, origin="t")
    assert ei.value.code == "EMAIL_UNAVAILABLE"
    with pytest.raises(DomainError) as ei:
        await accounts.create_user_account(db, email="novo@example.com", full_name="X Y", password="fraca", origin="t")
    assert ei.value.code == "PASSWORD_POLICY"
    with pytest.raises(DomainError) as ei:
        await accounts.create_user_account(db, email="sem-arroba", full_name="X Y", password=PW, origin="t")
    assert ei.value.code == "EMAIL_INVALID"
    with pytest.raises(DomainError) as ei:
        await accounts.create_user_account(db, email="a@example.com", full_name=" ", password=PW, origin="t")
    assert ei.value.code == "NAME_INVALID"
    await db.rollback()


async def test_create_user_account_hashes_before_existence_check(db, monkeypatch):
    """Sem atalho de tempo: o hash argon2 é calculado também quando o e-mail já existe."""
    await make_user(db, email="existe2@example.com")
    calls = []
    real = accounts.hash_password
    monkeypatch.setattr(accounts, "hash_password", lambda p: calls.append(1) or real(p))
    with pytest.raises(Conflict):
        await accounts.create_user_account(db, email="existe2@example.com", full_name="X Y", password=PW, origin="t")
    await accounts.create_user_account(db, email="inedito@example.com", full_name="X Y", password=PW, origin="t")
    assert len(calls) == 2
    await db.rollback()


# ─── grant_client_role ───────────────────────────────────────────────────────

async def test_grant_client_role_ok_idempotent_and_audited(db):
    org = await make_org(db)
    est = await make_establishment(db, org)
    u = await make_user(db)
    ra = await accounts.grant_client_role(db, user_id=u.id, role_slug="advertiser", scope_type="ORGANIZATION",
                                          scope_id=org.id, reason="auto-cadastro do anunciante")
    again = await accounts.grant_client_role(db, user_id=u.id, role_slug="advertiser", scope_type="ORGANIZATION",
                                             scope_id=org.id, reason="repetido")
    assert again.id == ra.id
    await accounts.grant_client_role(db, user_id=u.id, role_slug="establishment_owner", scope_type="ESTABLISHMENT",
                                     scope_id=est.id, reason="dono da unidade")
    await db.commit()
    n = (await db.execute(select(func.count()).select_from(RoleAssignment).where(RoleAssignment.user_id == u.id)))
    assert n.scalar_one() == 2
    aud = await _audits(db, "user.assignment_added")
    assert len(aud) == 2 and {a.org_id for a in aud} == {org.id}
    assert aud[0].reason == "auto-cadastro do anunciante" and aud[0].actor_user_id == u.id


async def test_grant_client_role_refusals(db):
    org = await make_org(db)
    u = await make_user(db)
    master = await make_user(db, admin_master_seat=1)
    cases = [
        dict(role_slug="admin", scope_type="ORGANIZATION", scope_id=org.id),          # perfil de staff
        dict(role_slug="super_admin", scope_type="ORGANIZATION", scope_id=org.id),
        dict(role_slug="advertiser", scope_type="GLOBAL", scope_id=None),             # escopo global
        dict(role_slug="advertiser", scope_type="CITY", scope_id=None),
    ]
    for c in cases:
        with pytest.raises(DomainError) as ei:
            await accounts.grant_client_role(db, user_id=u.id, reason="teste", **c)
        assert ei.value.code in ("ROLE_NOT_ALLOWED", "SCOPE_NOT_ALLOWED"), c
    with pytest.raises(NotFound):
        await accounts.grant_client_role(db, user_id=u.id, role_slug="advertiser", scope_type="ORGANIZATION",
                                         scope_id=uuid.uuid4(), reason="teste")
    with pytest.raises(NotFound):
        await accounts.grant_client_role(db, user_id=uuid.uuid4(), role_slug="advertiser", scope_type="ORGANIZATION",
                                         scope_id=org.id, reason="teste")
    with pytest.raises(Forbidden):
        await accounts.grant_client_role(db, user_id=master.id, role_slug="advertiser", scope_type="ORGANIZATION",
                                         scope_id=org.id, reason="teste")
    with pytest.raises(DomainError) as ei:
        await accounts.grant_client_role(db, user_id=u.id, role_slug="advertiser", scope_type="ORGANIZATION",
                                         scope_id=org.id, reason="")
    assert ei.value.code == "REASON_REQUIRED"
    await db.rollback()


async def test_grant_client_role_checks_granter_with_foundation_rule(db):
    org = await make_org(db)
    u = await make_user(db)
    admin = await make_user(db, role="admin", mfa=True)  # users.manage GLOBAL → pode conceder perfil de cliente
    emp = await make_user(db, role="employee")           # sem users.manage → não pode
    ra = await accounts.grant_client_role(db, user_id=u.id, role_slug="establishment_owner", scope_type="ORGANIZATION",
                                          scope_id=org.id, granted_by=admin.id, reason="cadastro pelo admin")
    assert ra.granted_by == admin.id
    with pytest.raises(Forbidden) as ei:
        await accounts.grant_client_role(db, user_id=u.id, role_slug="advertiser", scope_type="ORGANIZATION",
                                         scope_id=org.id, granted_by=emp.id, reason="tentativa")
    assert ei.value.code == "ROLE_ESCALATION"
    await db.commit()
    a = (await _audits(db, "user.assignment_added"))[0]
    assert a.actor_user_id == admin.id


# ─── issue_login + set_refresh_cookie ────────────────────────────────────────

async def test_issue_login_payload_session_refresh_and_audit(client, db):
    org = await make_org(db)
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    payload, refresh_raw = await accounts.issue_login(db, u, ip="198.51.100.9", user_agent="Mozilla/5.0 Chrome/120",
                                                      via="advertiser_signup")
    await db.commit()
    assert set(payload) == {"status", "access_token", "token_type", "expires_in", "user"}
    assert payload["status"] == "ok" and payload["expires_in"] == 15 * 60 and payload["user"]["id"] == str(u.id)
    assert len(refresh_raw) > 40
    me = await client.get("/api/v1/me", headers=auth_headers(payload["access_token"]))
    assert me.status_code == 200 and me.json()["user"]["id"] == str(u.id)
    # set_refresh_cookie importável produz o mesmo cookie do /auth/login
    resp = JSONResponse(payload)
    set_refresh_cookie(resp, refresh_raw)
    cookie = resp.headers["set-cookie"]
    assert "rd_refresh=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/v1/auth" in cookie
    # o refresh emitido participa da rotação normal
    r = await client.post("/api/v1/auth/refresh", headers=CSRF, cookies={"rd_refresh": refresh_raw})
    assert r.status_code == 200
    r = await client.post("/api/v1/auth/refresh", headers=CSRF, cookies={"rd_refresh": refresh_raw})
    assert r.status_code == 401 and r.json()["error"]["code"] == "REFRESH_REUSED"
    a = (await _audits(db, "auth.login"))[0]
    assert a.after_data["via"] == "advertiser_signup" and a.actor_user_id == u.id
    row = (await db.execute(text("select ip_address from user_sessions where user_id = :u"), {"u": u.id})).first()
    assert row.ip_address == "198.51.100.9"


async def test_issue_login_refuses_mfa_and_inactive(db):
    with_totp = await make_user(db, role="employee", mfa=True)
    mandatory = await make_user(db, role="financial")
    inactive = await make_user(db, role="employee", is_active=False)
    for u in (with_totp, mandatory):
        with pytest.raises(DomainError) as ei:
            await accounts.issue_login(db, u, ip=None, user_agent=None, via="t")
        assert ei.value.code == "MFA_REQUIRED_USE_LOGIN" and ei.value.status == 409
    with pytest.raises(Forbidden):
        await accounts.issue_login(db, inactive, ip=None, user_agent=None, via="t")
    await db.rollback()


def test_auth_endpoint_uses_the_shared_cookie_code():
    from app.api.v1.endpoints import auth as auth_ep
    from app.modules.auth import cookies

    assert auth_ep.set_refresh_cookie is cookies.set_refresh_cookie
    assert auth_ep.clear_refresh_cookie is cookies.clear_refresh_cookie
    assert auth_ep.check_csrf is cookies.check_csrf


# ─── record_consent ──────────────────────────────────────────────────────────

async def test_record_consent_ok_and_refusals(db):
    u = await make_user(db)
    rec = await accounts.record_consent(db, user_id=u.id, document="terms", version="2026-10-01",
                                        ip="203.0.113.5", user_agent="UA", ctx=None)
    await db.commit()
    assert rec.action == "accepted" and rec.ip_address == "203.0.113.5"
    a = (await _audits(db, "lgpd.consent_accepted"))[0]
    assert a.actor_user_id == u.id and a.after_data == {"version": "2026-10-01"} and a.origin == "api"
    for kw in (dict(document="contrato", version="1"), dict(document="terms", version=""),
               dict(document="terms", version="1", action="talvez")):
        with pytest.raises(DomainError):
            await accounts.record_consent(db, user_id=u.id, ip=None, user_agent=None, **kw)
    await db.rollback()


async def test_me_consents_endpoint_uses_record_consent(client, db, monkeypatch):
    from tests.factories import login_as

    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    called = []
    real = accounts.record_consent

    async def spy(*a, **kw):
        called.append(kw["document"])
        return await real(*a, **kw)

    monkeypatch.setattr(accounts, "record_consent", spy)
    r = await client.post("/api/v1/me/consents", json={"document": "privacy"}, headers=s.headers)
    assert r.status_code == 201 and called == ["privacy"]
