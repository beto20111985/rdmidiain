"""Convites (users.manage + escopo + anti-escalonamento) e impersonation (contrato §2.1)."""
import re

from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.rbac import RoleAssignment
from tests.factories import auth_headers, login_as, make_org, make_user


def _token(mail, email):
    m = mail.last_to(email)
    assert m is not None
    return re.search(r"token=([A-Za-z0-9_\-]+)", m.text).group(1)


async def test_org_admin_invites_into_own_org_and_accept_creates_user(client, db, mail):
    org = await make_org(db)
    # gestor da organização: perfil personalizado não existe por padrão → usar admin global p/ convidar
    admin = await make_user(db, role="admin", mfa=True)
    s = await login_as(client, admin)
    r = await client.post("/api/v1/auth/invites", json={"email": "Novo@Example.com", "role": "advertiser",
                                                         "scope_type": "ORGANIZATION", "scope_id": str(org.id)},
                          headers=s.headers)
    assert r.status_code == 201, r.text
    tok = _token(mail, "novo@example.com")
    r = await client.post("/api/v1/auth/invites/accept", json={"token": tok, "name": "Novo Usuário", "password": "fraca"})
    assert r.status_code == 400
    r = await client.post("/api/v1/auth/invites/accept",
                          json={"token": tok, "name": "Novo Usuário", "password": "SenhaForte2026x"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    me = (await client.get("/api/v1/me", headers=auth_headers(r.json()["access_token"]))).json()
    assert me["assignments"][0]["role"] == "advertiser" and me["assignments"][0]["scope_id"] == str(org.id)
    assert me["contexts"] == [{"key": f"adv:{org.id}", "label": f"Minhas campanhas — {org.name}",
                               "kind": "ADVERTISER", "org_id": str(org.id)}]
    r = await client.post("/api/v1/auth/invites/accept",
                          json={"token": tok, "name": "Outro", "password": "SenhaForte2026x"})
    assert r.status_code == 400  # convite usado


async def test_invite_privilege_escalation_blocked(client, db, mail):
    org_a, org_b = await make_org(db), await make_org(db)
    emp = await make_user(db, role="employee")
    admin = await make_user(db, role="admin", mfa=True)
    s_emp = await login_as(client, emp)
    r = await client.post("/api/v1/auth/invites", json={"email": "x@example.com", "role": "advertiser",
                                                         "scope_type": "ORGANIZATION", "scope_id": str(org_a.id)},
                          headers=s_emp.headers)
    assert r.status_code == 403  # employee não tem users.manage
    s_adm = await login_as(client, admin)
    r = await client.post("/api/v1/auth/invites", json={"email": "x@example.com", "role": "super_admin"},
                          headers=s_adm.headers)
    # AÇÃO 003 (ADR_011 D9): super_admin é reservado aos assentos de Admin Master — nunca pela API
    assert r.status_code == 403 and r.json()["error"]["code"] == "ROLE_RESERVED_FOR_ADMIN_MASTER"
    r = await client.post("/api/v1/auth/invites", json={"email": "x@example.com", "role": "financial"},
                          headers=s_adm.headers)
    assert r.status_code == 403  # admin não tem finance.manage → não concede financeiro
    r = await client.post("/api/v1/auth/invites", json={"email": "x@example.com", "role": "advertiser",
                                                         "scope_type": "ORGANIZATION",
                                                         "scope_id": "00000000-0000-0000-0000-000000000000"},
                          headers=s_adm.headers)
    assert r.status_code == 404  # organização inexistente
    assert org_b.id


async def test_invite_existing_user_requires_their_password(client, db, mail):
    org = await make_org(db)
    existing = await make_user(db, role="employee")
    admin = await make_user(db, role="admin", mfa=True)
    s = await login_as(client, admin)
    r = await client.post("/api/v1/auth/invites", json={"email": existing.email, "role": "advertiser",
                                                         "scope_type": "ORGANIZATION", "scope_id": str(org.id)},
                          headers=s.headers)
    assert r.status_code == 201
    tok = _token(mail, existing.email)
    r = await client.post("/api/v1/auth/invites/accept", json={"token": tok, "name": "Xx Yy", "password": "OutraSenha2026"})
    assert r.status_code == 400
    r = await client.post("/api/v1/auth/invites/accept", json={"token": tok, "name": "Xx Yy", "password": existing.password})
    assert r.status_code == 200
    n = (await db.execute(select(RoleAssignment).where(RoleAssignment.user_id == existing.id))).scalars().all()
    assert len(n) == 2


# ─── Impersonation ───────────────────────────────────────────────────────────

async def test_impersonation_flow_with_audit(client, db):
    org = await make_org(db)
    sa = await make_user(db, role="super_admin", mfa=True, name="Real Admin")
    target = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id), name="Cliente")
    s = await login_as(client, sa)
    r = await client.post("/api/v1/auth/impersonation", json={"user_id": str(target.id), "reason": "curto"},
                          headers=s.headers)
    assert r.status_code == 422
    r = await client.post("/api/v1/auth/impersonation",
                          json={"user_id": str(target.id), "reason": "Suporte ao chamado #123 do cliente"},
                          headers=s.headers)
    assert r.status_code == 200, r.text
    imp = r.json()
    assert imp["impersonation"]["real_user"]["name"] == "Real Admin"
    from app.core.tokens import decode

    claims = decode(imp["access_token"])
    assert claims["imp_by"] == str(sa.id) and claims["sub"] == str(target.id) and claims["imp_id"]
    h = auth_headers(imp["access_token"])
    me = (await client.get("/api/v1/me", headers=h)).json()
    assert me["user"]["id"] == str(target.id)
    assert me["impersonation"]["active"] is True and me["impersonation"]["real_user"]["id"] == str(sa.id)
    # ação durante impersonation: auditada com usuário real e impersonado
    r = await client.patch("/api/v1/me", json={"theme": "dark"}, headers=h)
    assert r.status_code == 200
    rows = (await db.execute(select(AuditLog).where(AuditLog.action.in_(
        ["user.profile_updated", "impersonation.request"])))).scalars().all()
    assert rows and all(a.actor_user_id == target.id and a.real_user_id == sa.id for a in rows)
    # ações sensíveis bloqueadas durante impersonation
    assert (await client.post("/api/v1/auth/password/change", json={"current_password": "x", "new_password": "y"},
                              headers=h)).status_code in (403, 422)
    assert (await client.post("/api/v1/auth/logout-all", headers=h)).status_code == 403
    # encerrar
    assert (await client.delete("/api/v1/auth/impersonation", headers=h)).status_code == 204
    assert (await client.get("/api/v1/me", headers=h)).status_code == 401
    assert (await client.get("/api/v1/me", headers=s.headers)).status_code == 200  # sessão real intacta
    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "auth.impersonation_started" in actions and "auth.impersonation_ended" in actions
    start = (await db.execute(select(AuditLog).where(AuditLog.action == "auth.impersonation_started"))).scalar_one()
    assert start.reason == "Suporte ao chamado #123 do cliente" and start.actor_user_id == sa.id


async def test_impersonation_forbidden_against_admins_and_masters_and_without_permission(client, db):
    sa = await make_user(db, role="super_admin", mfa=True)
    master = await make_user(db, role="super_admin", mfa=True, admin_master_seat=2)
    adm = await make_user(db, role="admin", mfa=True)
    fin = await make_user(db, role="financial", mfa=True)
    s = await login_as(client, sa)
    for t in (master, adm, fin):
        r = await client.post("/api/v1/auth/impersonation", json={"user_id": str(t.id), "reason": "motivo suficiente aqui"},
                              headers=s.headers)
        assert r.status_code == 403, t.email
    s_adm = await login_as(client, adm)
    emp = await make_user(db, role="employee")
    r = await client.post("/api/v1/auth/impersonation", json={"user_id": str(emp.id), "reason": "motivo suficiente aqui"},
                          headers=s_adm.headers)
    assert r.status_code == 403  # admin não tem users.impersonate


async def test_impersonation_ends_when_real_session_logs_out(client_factory, db):
    from tests.factories import CSRF

    org = await make_org(db)
    sa = await make_user(db, role="super_admin", mfa=True)
    target = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    c = client_factory()
    s = await login_as(c, sa)
    imp = (await c.post("/api/v1/auth/impersonation", json={"user_id": str(target.id), "reason": "motivo suficiente aqui"},
                        headers=s.headers)).json()
    h = auth_headers(imp["access_token"])
    assert (await c.get("/api/v1/me", headers=h)).status_code == 200
    assert (await c.post("/api/v1/auth/logout", headers={**CSRF, **s.headers})).status_code == 204
    assert (await c.get("/api/v1/me", headers=h)).status_code == 401
