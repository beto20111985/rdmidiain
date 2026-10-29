"""RBAC por escopo, cross-tenant, escalonamento e independência dos Admin Master."""
import uuid

from sqlalchemy import select

from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
from tests.factories import assign, login_as, make_establishment, make_org, make_user, totp_code


async def _org_manager_role(db) -> str:
    """Perfil personalizado com gestão de usuários (para testar escopo ORGANIZATION)."""
    role = Role(id=uuid.uuid4(), slug="org_manager", name="Gestor da organização", is_system=False)
    db.add(role)
    await db.flush()
    from app.core.permissions import ROLE_TEMPLATES

    # gestor = todas as permissões do anunciante + gestão de usuários (deriva do template:
    # acompanha mudanças de perfil sem enfraquecer o teste)
    codes = sorted(ROLE_TEMPLATES["advertiser"].permissions | {"users.view", "users.manage"})
    for code in codes:
        pid = (await db.execute(select(Permission.id).where(Permission.code == code))).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=pid))
    await db.commit()
    return "org_manager"


async def test_cross_tenant_users_invisible_and_404(client, db):
    org_a, org_b = await make_org(db), await make_org(db)
    est_a = await make_establishment(db, org_a)
    slug = await _org_manager_role(db)
    mgr_a = await make_user(db, role=slug, scope=("ORGANIZATION", org_a.id))
    user_a = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_a.id))
    user_est_a = await make_user(db, role="establishment_owner", scope=("ESTABLISHMENT", est_a.id))
    user_b = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_b.id))
    sa = await make_user(db, role="super_admin", mfa=True)
    s = await login_as(client, mgr_a)
    r = await client.get("/api/v1/users", headers=s.headers)
    assert r.status_code == 200
    ids = {i["id"] for i in r.json()["items"]}
    assert ids == {str(mgr_a.id), str(user_a.id), str(user_est_a.id)}
    assert str(user_b.id) not in ids and str(sa.id) not in ids
    for path in (f"/api/v1/users/{user_b.id}", f"/api/v1/users/{sa.id}"):
        assert (await client.get(path, headers=s.headers)).status_code == 404
    r = await client.patch(f"/api/v1/users/{user_b.id}", json={"is_active": False}, headers=s.headers)
    assert r.status_code == 404
    r = await client.patch(f"/api/v1/users/{sa.id}", json={"is_active": False}, headers=s.headers)
    assert r.status_code == 404  # org_admin não altera super admin
    r = await client.patch(f"/api/v1/users/{user_a.id}", json={"name": "Renomeado"}, headers=s.headers)
    assert r.status_code == 200 and r.json()["name"] == "Renomeado"
    # sem users.view (anunciante) → 403
    s2 = await login_as(client, user_b)
    assert (await client.get("/api/v1/users", headers=s2.headers)).status_code == 403


async def test_privilege_escalation_blocked(client, db):
    org_a, org_b = await make_org(db), await make_org(db)
    slug = await _org_manager_role(db)
    mgr = await make_user(db, role=slug, scope=("ORGANIZATION", org_a.id))
    other = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_a.id))
    s = await login_as(client, mgr)
    # não se atribui perfil
    r = await client.post(f"/api/v1/users/{mgr.id}/assignments", json={"role": "advertiser", "scope_type": "ORGANIZATION",
                                                                       "scope_id": str(org_a.id)}, headers=s.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SELF_ASSIGNMENT"
    # não concede perfis acima do seu
    for role in ("super_admin", "admin", "establishment_owner"):
        r = await client.post(f"/api/v1/users/{other.id}/assignments", json={"role": role, "scope_type": "ORGANIZATION",
                                                                             "scope_id": str(org_a.id)},
                              headers=s.headers)
        assert r.status_code == 403, role
    r = await client.post(f"/api/v1/users/{other.id}/assignments", json={"role": "admin"}, headers=s.headers)
    assert r.status_code == 403
    # não concede em outra organização
    r = await client.post(f"/api/v1/users/{other.id}/assignments", json={"role": "advertiser", "scope_type": "ORGANIZATION",
                                                                         "scope_id": str(org_b.id)}, headers=s.headers)
    assert r.status_code == 403
    # perfil dentro do próprio alcance → ok
    r = await client.post(f"/api/v1/users/{other.id}/assignments", json={"role": "agency", "scope_type": "ORGANIZATION",
                                                                         "scope_id": str(org_a.id)}, headers=s.headers)
    assert r.status_code == 201


async def test_admin_cannot_manage_super_admin_and_deactivation_revokes_sessions(client_factory, db):
    adm = await make_user(db, role="admin", mfa=True)
    sup = await make_user(db, role="super_admin", mfa=True)
    emp = await make_user(db, role="employee")
    c1, c2 = client_factory(), client_factory()
    s = await login_as(c1, adm)
    se = await login_as(c2, emp)
    r = await c1.patch(f"/api/v1/users/{sup.id}", json={"is_active": False}, headers=s.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "USER_NOT_MANAGEABLE"
    r = await c1.patch(f"/api/v1/users/{emp.id}", json={"is_active": False}, headers=s.headers)
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (await c2.get("/api/v1/me", headers=se.headers)).status_code == 401
    r = await c1.patch(f"/api/v1/users/{adm.id}", json={"name": "Eu"}, headers=s.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SELF_MANAGEMENT"


async def test_admin_master_independence(client_factory, db):
    a = await make_user(db, role="super_admin", mfa=True, admin_master_seat=1, name="Master A")
    b = await make_user(db, role="super_admin", mfa=True, admin_master_seat=2, name="Master B")
    plain_sa = await make_user(db, role="super_admin", mfa=True)
    ca, cb = client_factory(), client_factory()
    sa = await login_as(ca, a)
    sb = await login_as(cb, b)
    me = (await ca.get("/api/v1/me", headers=sa.headers)).json()
    assert me["user"]["is_admin_master"] is True and "finance.approve" in me["permissions"]
    # A não vê dados sensíveis de B
    r = await ca.get(f"/api/v1/users/{b.id}", headers=sa.headers)
    assert r.status_code == 200
    assert r.json() == {"id": str(b.id), "name": "Master B", "is_active": True, "is_admin_master": True,
                        "restricted": True}
    listing = (await ca.get("/api/v1/users", headers=sa.headers)).json()["items"]
    b_row = next(i for i in listing if i["id"] == str(b.id))
    assert "email" not in b_row and "assignments" not in b_row
    # A não altera senha/e-mail/2FA/sessões/is_active de B
    reason = {"reason": "teste de independência dos masters"}
    for method, path, body in [
        ("patch", f"/api/v1/users/{b.id}", {"is_active": False}),
        ("patch", f"/api/v1/users/{b.id}", {"email": "outro@example.com"}),
        ("post", f"/api/v1/users/{b.id}/mfa/reset", reason),
        ("post", f"/api/v1/users/{b.id}/sessions/revoke-all", reason),
        ("post", f"/api/v1/users/{b.id}/unlock", reason),
        ("post", f"/api/v1/users/{b.id}/assignments", {"role": "employee"}),
    ]:
        r = await getattr(ca, method)(path, json=body, headers=sa.headers)
        assert r.status_code in (403, 404), (path, r.status_code)
    # outro super admin também não
    sp = await login_as(client_factory(), plain_sa)
    r = await ca.patch(f"/api/v1/users/{a.id}", json={"is_active": False}, headers=sp.headers)
    assert r.status_code == 403
    # B continua íntegro
    assert (await cb.get("/api/v1/me", headers=sb.headers)).status_code == 200
    # finance.approve não é efetivo para super admin comum
    me_plain = (await ca.get("/api/v1/me", headers=sp.headers)).json()
    assert "finance.approve" not in me_plain["permissions"]
    assert totp_code  # noqa


async def test_admin_master_seats_unique_in_db(db):
    import pytest
    from sqlalchemy.exc import IntegrityError

    await make_user(db, admin_master_seat=1)
    with pytest.raises(IntegrityError):
        await make_user(db, admin_master_seat=1)
    await db.rollback()
    with pytest.raises(IntegrityError):
        await make_user(db, admin_master_seat=3)
    await db.rollback()


async def test_assignments_add_remove_and_roles_catalog(client, db):
    org = await make_org(db)
    adm = await make_user(db, role="admin", mfa=True)
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, adm)
    r = await client.post(f"/api/v1/users/{u.id}/assignments", json={"role": "technician", "scope_type": "CITY",
                                                                     "scope_value": "GO:Goiânia"}, headers=s.headers)
    assert r.status_code == 201 and r.json()["scope_value"] == "GO:goiania"
    aid = r.json()["id"]
    r = await client.post(f"/api/v1/users/{u.id}/assignments", json={"role": "technician", "scope_type": "CITY",
                                                                     "scope_value": "GO:goiania"}, headers=s.headers)
    assert r.status_code == 409
    assert (await client.delete(f"/api/v1/users/{u.id}/assignments/{aid}", headers=s.headers)).status_code == 204
    roles = (await client.get("/api/v1/roles", headers=s.headers)).json()["items"]
    assert {r["slug"] for r in roles} >= {"super_admin", "admin", "advertiser", "agency"}
    perms = (await client.get("/api/v1/permissions", headers=s.headers)).json()["items"]
    assert len(perms) == 69


async def test_custom_role_cannot_exceed_creator_permissions(client, db):
    adm = await make_user(db, role="admin", mfa=True)
    s = await login_as(client, adm)
    r = await client.post("/api/v1/roles", json={"slug": "gestor_x", "name": "Gestor X",
                                                 "permissions": ["campaigns.view", "finance.manage"]}, headers=s.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ROLE_ESCALATION"
    r = await client.post("/api/v1/roles", json={"slug": "gestor_x", "name": "Gestor X",
                                                 "permissions": ["campaigns.view", "users.view"]}, headers=s.headers)
    assert r.status_code == 201
    r = await client.post("/api/v1/roles", json={"slug": "admin", "name": "Dup", "permissions": []}, headers=s.headers)
    assert r.status_code == 409
    sys_role = (await db.execute(select(Role).where(Role.slug == "advertiser"))).scalar_one()
    r = await client.patch(f"/api/v1/roles/{sys_role.id}", json={"name": "Novo nome"}, headers=s.headers)
    assert r.status_code == 403


async def test_city_scope_user_listing(client, db):
    tech_role = await _org_manager_role(db)  # noqa: F841 (apenas garante catálogo)
    sales = await make_user(db, role="sales", scope=("CITY", "GO:goiania"))
    await assign(db, sales, "employee", ("CITY", "GO:goiania"))
    s = await login_as(client, sales)
    assert (await client.get("/api/v1/users", headers=s.headers)).status_code == 403  # sales não tem users.view
    ra = (await db.execute(select(RoleAssignment).where(RoleAssignment.user_id == sales.id))).scalars().all()
    assert {a.scope_value for a in ra} == {"GO:goiania"}


async def test_global_user_manager_grants_client_profiles_only_in_org_scope(client, db, mail):
    """Decisão AÇÃO 002: users.manage GLOBAL concede perfis de CLIENTE só em ORGANIZATION/ESTABLISHMENT."""
    org_x, org_y = await make_org(db), await make_org(db)
    est_x = await make_establishment(db, org_x)
    adm = await make_user(db, role="admin", mfa=True)
    target = await make_user(db)
    s = await login_as(client, adm)
    # admin concede establishment_owner na org X (apesar de não ter withdrawals.request) ✔
    r = await client.post(f"/api/v1/users/{target.id}/assignments",
                          json={"role": "establishment_owner", "scope_type": "ORGANIZATION", "scope_id": str(org_x.id)},
                          headers=s.headers)
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/invites", json={"email": "dono.est@example.com", "role": "establishment_owner",
                                                         "scope_type": "ESTABLISHMENT", "scope_id": str(est_x.id)},
                          headers=s.headers)
    assert r.status_code == 201, r.text
    # ...mas não em escopo GLOBAL/CITY
    r = await client.post("/api/v1/auth/invites", json={"email": "x@example.com", "role": "establishment_owner"},
                          headers=s.headers)
    assert r.status_code == 403
    # perfis de staff acima dos próprios continuam proibidos ✘
    other = await make_user(db)
    r = await client.post(f"/api/v1/users/{other.id}/assignments", json={"role": "financial"}, headers=s.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ROLE_ESCALATION"
    # gestor de organização X não concede na organização Y ✘
    slug = await _org_manager_role(db)
    mgr_x = await make_user(db, role=slug, scope=("ORGANIZATION", org_x.id))
    sm = await login_as(client, mgr_x)
    r = await client.post("/api/v1/auth/invites", json={"email": "y@example.com", "role": "advertiser",
                                                         "scope_type": "ORGANIZATION", "scope_id": str(org_y.id)},
                          headers=sm.headers)
    assert r.status_code == 404
    rows = (await db.execute(select(RoleAssignment).where(RoleAssignment.user_id == target.id))).scalars().all()
    assert [(a.scope_type, a.scope_id) for a in rows] == [("ORGANIZATION", org_x.id)]


async def test_list_users_role_filter_respects_scope(client, db):
    org_a, org_b = await make_org(db), await make_org(db)
    adm = await make_user(db, role="admin", mfa=True)
    t1 = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Téc GO")
    t2 = await make_user(db, role="technician", name="Téc Global")
    des = await make_user(db, role="designer", name="Designer")
    adv_a = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_a.id))
    adv_b = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_b.id))
    s = await login_as(client, adm)
    r = await client.get("/api/v1/users", params={"role": "technician"}, headers=s.headers)
    assert r.status_code == 200 and {i["id"] for i in r.json()["items"]} == {str(t1.id), str(t2.id)}
    r = await client.get("/api/v1/users", params=[("role", "technician"), ("role", "designer")], headers=s.headers)
    assert {i["id"] for i in r.json()["items"]} == {str(t1.id), str(t2.id), str(des.id)}
    r = await client.get("/api/v1/users", params={"role": "perfil_inexistente"}, headers=s.headers)
    assert r.json()["total"] == 0
    # gestor da org A filtrando advertisers: só os da própria organização
    slug = await _org_manager_role(db)
    mgr = await make_user(db, role=slug, scope=("ORGANIZATION", org_a.id))
    sm = await login_as(client, mgr)
    r = await client.get("/api/v1/users", params={"role": "advertiser"}, headers=sm.headers)
    assert {i["id"] for i in r.json()["items"]} == {str(adv_a.id)}
    assert str(adv_b.id) not in r.text


async def test_assignable_users_minimal_and_scoped(client, db):
    """Técnico vê colegas técnicos da sua cidade (sem users.view; só id+nome)."""
    tec_go = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Ana GO")
    col_go = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Bruno GO")
    tec_sp = await make_user(db, role="technician", scope=("CITY", "SP:sao-paulo"), name="Caio SP")
    tec_global = await make_user(db, role="technician", name="Dora Global")
    inativo = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Eva Inativa",
                              is_active=False)
    emp = await make_user(db, role="employee", scope=("CITY", "GO:goiania"))  # sem maintenance.manage
    s = await login_as(client, tec_go)
    assert (await client.get("/api/v1/users", headers=s.headers)).status_code == 403  # sem users.view
    r = await client.get("/api/v1/users/assignable", params={"permission": "maintenance.manage"}, headers=s.headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert {i["id"] for i in items} == {str(tec_go.id), str(col_go.id)}
    assert all(set(i) == {"id", "name"} for i in items)  # nada sensível
    assert str(tec_sp.id) not in r.text and str(tec_global.id) not in r.text and str(inativo.id) not in r.text
    assert tec_go.email not in r.text
    # recurso fora do escopo do técnico → 404
    r = await client.get("/api/v1/users/assignable", params={"permission": "maintenance.manage", "city": "SP:sao-paulo"},
                         headers=s.headers)
    assert r.status_code == 404
    # permissão que o técnico não tem → 403; permissão inexistente → 400
    r = await client.get("/api/v1/users/assignable", params={"permission": "finance.view"}, headers=s.headers)
    assert r.status_code == 403
    r = await client.get("/api/v1/users/assignable", params={"permission": "nao.existe"}, headers=s.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "PERMISSION_UNKNOWN"
    se = await login_as(client, emp)
    r = await client.get("/api/v1/users/assignable", params={"permission": "maintenance.manage"}, headers=se.headers)
    assert r.status_code == 403


async def test_assignable_users_global_actor_and_resource_filter(client, db):
    org = await make_org(db)
    est = await make_establishment(db, org, city="São Paulo", state="SP")
    adm = await make_user(db, role="admin", mfa=True, name="Admin")
    tec_go = await make_user(db, role="technician", scope=("CITY", "GO:goiania"), name="Ana GO")
    tec_sp = await make_user(db, role="technician", scope=("CITY", "SP:sao-paulo"), name="Caio SP")
    tec_global = await make_user(db, role="technician", name="Dora Global")
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, adm)
    r = await client.get("/api/v1/users/assignable", params={"permission": "maintenance.manage"}, headers=s.headers)
    ids = {i["id"] for i in r.json()["items"]}
    assert {str(tec_go.id), str(tec_sp.id), str(tec_global.id), str(adm.id)} <= ids and str(adv.id) not in ids
    # filtro por cidade: técnicos que cobrem SP (escopo da cidade + globais), não os de GO
    r = await client.get("/api/v1/users/assignable", params={"permission": "maintenance.manage", "city": "SP:São Paulo"},
                         headers=s.headers)
    ids = {i["id"] for i in r.json()["items"]}
    assert str(tec_sp.id) in ids and str(tec_global.id) in ids and str(tec_go.id) not in ids
    # filtro por estabelecimento (cidade derivada do estabelecimento)
    r = await client.get("/api/v1/users/assignable",
                         params={"permission": "maintenance.manage", "establishment_id": str(est.id)}, headers=s.headers)
    ids = {i["id"] for i in r.json()["items"]}
    assert str(tec_sp.id) in ids and str(tec_go.id) not in ids
    r = await client.get("/api/v1/users/assignable",
                         params={"permission": "maintenance.manage", "establishment_id": str(uuid.uuid4())},
                         headers=s.headers)
    assert r.status_code == 404


async def test_assignable_finance_approve_only_admin_masters(client, db):
    m1 = await make_user(db, role="super_admin", mfa=True, admin_master_seat=1, name="Master 1")
    m2 = await make_user(db, role="super_admin", mfa=True, admin_master_seat=2, name="Master 2")
    plain = await make_user(db, role="super_admin", mfa=True, name="SA comum")
    s = await login_as(client, m1)
    r = await client.get("/api/v1/users/assignable", params={"permission": "finance.approve"}, headers=s.headers)
    assert r.status_code == 200
    assert {i["id"] for i in r.json()["items"]} == {str(m1.id), str(m2.id)} and str(plain.id) not in r.text
    sp = await login_as(client, plain)
    r = await client.get("/api/v1/users/assignable", params={"permission": "finance.approve"}, headers=sp.headers)
    assert r.status_code == 403  # finance.approve só é efetiva para os Admin Master
