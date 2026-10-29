"""/me (contrato §3), PATCH /me, consentimentos, exportação e exclusão (LGPD)."""
from sqlalchemy import select, text

from app.models.users import User
from tests.factories import assign, get_user, login_as, make_establishment, make_org, make_user


async def test_me_shape_contexts_and_permissions(client, db):
    org_m = await make_org(db, name="Mercado X")
    org_a = await make_org(db, name="Anunciante Y")
    u = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org_m.id), name="Dona")
    await assign(db, u, "advertiser", ("ORGANIZATION", org_a.id))
    s = await login_as(client, u)
    me = (await client.get("/api/v1/me", headers=s.headers)).json()
    assert set(me) >= {"user", "permissions", "assignments", "contexts", "impersonation", "mfa"}
    assert set(me["user"]) >= {"id", "name", "email", "phone", "mfa_enabled", "is_admin_master", "theme"}
    assert me["user"]["theme"] == "system" and me["user"]["is_admin_master"] is False
    assert "withdrawals.request" in me["permissions"] and "checkout.create" in me["permissions"]
    assert "finance.view" not in me["permissions"]
    keys = {c["key"]: c for c in me["contexts"]}
    assert keys == {
        f"est:{org_m.id}": {"key": f"est:{org_m.id}", "label": "Minha unidade — Mercado X", "kind": "ESTABLISHMENT",
                            "org_id": str(org_m.id)},
        f"adv:{org_a.id}": {"key": f"adv:{org_a.id}", "label": "Minhas campanhas — Anunciante Y", "kind": "ADVERTISER",
                            "org_id": str(org_a.id)},
    }
    labels = {a["role"]: a["scope_label"] for a in me["assignments"]}
    assert labels == {"establishment_owner": "Mercado X", "advertiser": "Anunciante Y"}
    assert me["impersonation"] == {"active": False, "real_user": None, "reason": None}
    assert me["mfa"] == {"required": False, "configured": False}


async def test_me_platform_context_for_rd_staff_and_establishment_scope(client, db):
    org = await make_org(db, name="Rede Z")
    est = await make_establishment(db, org)
    staff = await make_user(db, role="employee")
    s = await login_as(client, staff)
    me = (await client.get("/api/v1/me", headers=s.headers)).json()
    assert me["contexts"] == [{"key": "rd", "label": "RD Mídia Indoor", "kind": "PLATFORM"}]
    owner = await make_user(db, role="establishment_owner", scope=("ESTABLISHMENT", est.id))
    s2 = await login_as(client, owner)
    me2 = (await client.get("/api/v1/me", headers=s2.headers)).json()
    assert me2["contexts"][0]["key"] == f"est:{org.id}"
    assert me2["assignments"][0]["scope_label"] == est.name


async def test_patch_me_theme_name_phone(client, db):
    u = await make_user(db, role="employee")
    other = await make_user(db, role="employee", phone="5562988887777")
    s = await login_as(client, u)
    r = await client.patch("/api/v1/me", json={"theme": "dark"}, headers=s.headers)
    assert r.status_code == 200 and r.json()["user"]["theme"] == "dark"
    assert (await get_user(db, u.id)).theme == "dark"
    r = await client.patch("/api/v1/me", json={"theme": "neon"}, headers=s.headers)
    assert r.status_code == 422
    r = await client.patch("/api/v1/me", json={"name": "Novo Nome", "phone": "(62) 98888-7777"}, headers=s.headers)
    assert r.status_code == 409  # telefone de outra conta
    r = await client.patch("/api/v1/me", json={"name": "Novo Nome", "phone": "(62) 91111-2222"}, headers=s.headers)
    assert r.status_code == 200 and r.json()["user"]["phone"] == "5562911112222"
    assert r.json()["user"]["name"] == "Novo Nome"
    original_email = u.email
    r = await client.patch("/api/v1/me", json={"email": "trocar@example.com"}, headers=s.headers)
    assert r.status_code == 200 and (await get_user(db, u.id)).email == original_email  # e-mail não muda por aqui
    assert other.id


async def test_consents_and_data_export(client, db):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    r = await client.post("/api/v1/me/consents", json={"document": "terms"}, headers=s.headers)
    assert r.status_code == 201
    r = await client.post("/api/v1/me/consents", json={"document": "privacy", "version": "2026-10-01"}, headers=s.headers)
    assert r.status_code == 201
    r = await client.get("/api/v1/me/consents", headers=s.headers)
    assert {c["document"] for c in r.json()["consents"]} == {"terms", "privacy"}
    row = (await db.execute(text("SELECT ip_address, user_agent FROM consent_records LIMIT 1"))).one()
    assert row.ip_address == "127.0.0.1" and row.user_agent
    exp = await client.get("/api/v1/me/data-export", headers=s.headers)
    assert exp.status_code == 200
    data = exp.json()
    assert data["profile"]["email"] == u.email and len(data["consents"]) == 2 and data["sessions"]
    assert "hashed_password" not in exp.text and "totp" not in exp.text.replace("mfa_enabled", "")


async def test_deletion_request_and_anonymization(client_factory, db):
    org = await make_org(db)
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id), phone="5562977776666", name="Titular")
    dpo = await make_user(db, role="super_admin", mfa=True)
    original_email, original_password = u.email, u.password
    c1, c2 = client_factory(), client_factory()
    s = await login_as(c1, u)
    await c1.post("/api/v1/me/consents", json={"document": "terms"}, headers=s.headers)
    r = await c1.post("/api/v1/me/deletion-request", json={"reason": "Não uso mais"}, headers=s.headers)
    assert r.status_code == 202 and r.json()["status"] == "PENDING"
    r2 = await c1.post("/api/v1/me/deletion-request", json={}, headers=s.headers)
    assert r2.json()["id"] == r.json()["id"]  # idempotente enquanto pendente
    # titular não processa o próprio pedido (sem lgpd.manage)
    assert (await c1.get("/api/v1/lgpd/deletion-requests", headers=s.headers)).status_code == 403
    sd = await login_as(c2, dpo)
    lst = (await c2.get("/api/v1/lgpd/deletion-requests", params={"status": "PENDING"}, headers=sd.headers)).json()
    assert lst["total"] == 1
    r = await c2.post(f"/api/v1/lgpd/deletion-requests/{r.json()['id']}/process", json={"notes": "ok"}, headers=sd.headers)
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    anon = await get_user(db, u.id)
    assert anon.anonymized_at and not anon.is_active and anon.phone is None
    assert anon.full_name == "Titular anonimizado" and anon.email.endswith("@anonimizado.invalid")
    assert (await c1.get("/api/v1/me", headers=s.headers)).status_code == 401  # sessões revogadas
    # registros obrigatórios preservados (consentimento e auditoria)
    assert (await db.execute(text("SELECT count(*) FROM consent_records"))).scalar_one() == 1
    n = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'lgpd.user_anonymized'"))).scalar_one()
    assert n == 1
    r = await c1.post("/api/v1/auth/login", json={"login": original_email, "password": original_password})
    assert r.status_code == 401
    assert (await db.execute(select(User).where(User.email == original_email))).scalar_one_or_none() is None
