"""W-ORG — onboarding: auto-cadastro (token por e-mail, header), wizard por etapas, aprovação da RD,
convite do responsável (W1), aceite eletrônico do contrato ativando o estabelecimento; convite do comercial;
recusa; token nunca em query string."""
import re
import uuid

from sqlalchemy import select

from app.models.establishments import Establishment
from app.models.installations import Installation
from app.models.notifications import Notification
from app.models.organizations import Organization
from tests.factories import make_user, new_password
from tests.integration.org_api.helpers import audit_actions, new_cnpj, staff_session

HDR = "X-RD-Onboarding-Token"


def _token_from(mail, email):
    m = mail.last_to(email)
    assert m is not None, "e-mail com o link não enviado"
    tok = re.search(r"token=([A-Za-z0-9_\-]+)", m.text).group(1)
    return tok


async def _fill_public(client, tok, cnpj):
    h = {HDR: tok}
    steps = {
        "company": {"name": "Distribuidora Boa Bebida LTDA", "document": cnpj, "responsible_name": "Carlos",
                    "phone": "62988887777"},
        "establishment": {"name": "Boa Bebida — Matriz", "responsible_name": "Carlos", "phone": "62988887777"},
        "address": {"address": "Av. Goiás", "number": "1000", "neighborhood": "Centro", "city": "Goiânia",
                    "state": "GO", "postal_code": "74010-010", "lat": -16.68, "lng": -49.25, "geo_source": "GPS"},
        "screens": {"planned_screens": 2, "tv_provision": "RD_TV_COMODATO"},
    }
    for step, body in steps.items():
        r = await vclient_put(client, f"/api/v1/onboarding/public/application/steps/{step}", body, h)
        assert r.status_code == 200, (step, r.text)
    return h


async def vclient_put(client, url, body, headers):
    return await client.put(url, json=body, headers=headers)


async def test_self_signup_full_flow_until_contract_activation(client, db, mail, storage):
    admin_user = await make_user(db, role="admin", mfa=True)  # equipe existente antes do envio (recebe o aviso)
    email = f"dono{uuid.uuid4().hex[:6]}@example.com"
    r = await client.post("/api/v1/onboarding/public/signup", json={"email": email})
    assert r.status_code == 202
    tok = _token_from(mail, email)
    # token em query string NÃO é aceito
    r = await client.get("/api/v1/onboarding/public/application", params={"token": tok})
    assert r.status_code == 401
    r = await client.get("/api/v1/onboarding/public/application", headers={HDR: "x" * 40})
    assert r.status_code == 401
    r = await client.get("/api/v1/onboarding/public/application", headers={HDR: tok})
    assert r.status_code == 200 and r.json()["status"] == "INVITED"
    assert [s["key"] for s in r.json()["steps"]] == ["company", "establishment", "address", "screens"]
    # submit antes de completar
    r = await client.post("/api/v1/onboarding/public/application/submit", headers={HDR: tok})
    assert r.status_code == 400 and r.json()["error"]["code"] == "ONBOARDING_INCOMPLETE"
    # etapa da equipe RD não pode ser preenchida pelo público
    r = await client.put("/api/v1/onboarding/public/application/steps/commercial",
                         json={"pays_monthly_fee": False}, headers={HDR: tok})
    assert r.status_code == 403
    # CNPJ inválido
    r = await client.put("/api/v1/onboarding/public/application/steps/company",
                         json={"name": "X LTDA", "document": "11.222.333/0001-80"}, headers={HDR: tok})
    assert r.status_code == 400 and r.json()["error"]["code"] == "DOCUMENT_INVALID"
    cnpj = new_cnpj()
    h = await _fill_public(client, tok, cnpj)
    # documento opcional (PDF real mínimo)
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    r = await client.post("/api/v1/onboarding/public/application/documents", data={"kind": "COMPANY_REGISTRATION"},
                          files={"file": ("contrato social.pdf", pdf, "application/pdf")}, headers=h)
    assert r.status_code == 201 and r.json()["content_type"] == "application/pdf"
    r = await client.post("/api/v1/onboarding/public/application/documents", data={"kind": "OTHER"},
                          files={"file": ("foto.jpg", b"isto nao e imagem", "image/jpeg")}, headers=h)
    assert r.status_code == 415
    r = await client.post("/api/v1/onboarding/public/application/submit", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "SUBMITTED"
    app_id = r.json()["id"]
    # edição bloqueada após envio
    r = await client.put("/api/v1/onboarding/public/application/steps/screens",
                         json={"planned_screens": 9, "tv_provision": "OWN_TV"}, headers=h)
    assert r.status_code == 409

    # equipe RD recebe notificação e aprova
    from tests.factories import login_as

    adm = await login_as(client, admin_user)
    notes = (await db.execute(select(Notification).where(Notification.user_id == admin_user.id))).scalars().all()
    assert any(n.event_type == "onboarding.submitted" for n in notes)
    tech = await make_user(db, role="technician", scope=("CITY", "GO:goiania"))
    r = await client.put(f"/api/v1/onboarding/{app_id}/steps/installation",
                         json={"technician_user_id": str(tech.id), "installation_date": "2026-10-20"},
                         headers=adm.headers)
    assert r.status_code == 409  # já enviado: dados de instalação são definidos antes do envio
    r = await client.post(f"/api/v1/onboarding/{app_id}/approve", headers=adm.headers)
    assert r.status_code == 200, r.text
    org_id, est_id = r.json()["org_id"], r.json()["establishment_id"]
    org = await db.get(Organization, uuid.UUID(org_id))
    est = await db.get(Establishment, uuid.UUID(est_id))
    assert org.document == cnpj and org.business_roles == ["PARTNER"]
    assert est.onboarding_status == "AWAITING_CONTRACT" and est.timezone == "America/Sao_Paulo"
    assert est.city_key == "GO:goiania" and est.geo_source == "GPS"
    r = await client.post(f"/api/v1/onboarding/{app_id}/approve", headers=adm.headers)
    assert r.status_code == 409
    assert "onboarding.approved" in await audit_actions(db, app_id)

    # o próprio admin que aprovou convida o responsável (W1: users.manage GLOBAL concede perfis de cliente)
    # → aceite do convite → aceite do contrato (login = assinatura)
    r = await client.post("/api/v1/auth/invites", json={"email": email, "role": "establishment_owner",
                                                        "scope_type": "ORGANIZATION", "scope_id": org_id},
                          headers=adm.headers)
    assert r.status_code == 201, r.text
    invite_tok = re.search(r"token=([A-Za-z0-9_\-]+)", mail.last_to(email).text).group(1)
    r = await client.post("/api/v1/auth/invites/accept",
                          json={"token": invite_tok, "name": "Carlos Dono", "password": new_password()})
    assert r.status_code == 200, r.text
    owner_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    tpl = (await client.post("/api/v1/contracts/templates", json={
        "kind": "PARTNERSHIP", "title": "Contrato de Parceria",
        "body": "CONTRATO DE PARCERIA RD MÍDIA INDOOR — texto integral de teste com mais de cinquenta caracteres."},
        headers=adm.headers)).json()
    await client.post(f"/api/v1/contracts/templates/{tpl['id']}/publish", headers=adm.headers)
    r = await client.post("/api/v1/contracts/accept", json={"template_id": tpl["id"], "org_id": org_id,
                                                            "establishment_id": est_id}, headers=owner_headers)
    assert r.status_code == 200, r.text
    assert r.json()["establishment_activated"] == est_id
    est = await db.get(Establishment, uuid.UUID(est_id), populate_existing=True)
    assert est.onboarding_status == "ACTIVE"


async def test_sales_invite_with_staff_steps_and_installation_scheduled(client, db, mail, storage):
    sales = await staff_session(client, db, "sales", ("CITY", "GO:goiania"))
    email = f"convite{uuid.uuid4().hex[:6]}@example.com"
    r = await client.post("/api/v1/onboarding/invites", json={"email": email}, headers=sales.headers)
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    tok = _token_from(mail, email)
    h = await _fill_public(client, tok, new_cnpj())
    adm = await staff_session(client, db, "admin")
    tech = await make_user(db, role="technician", scope=("CITY", "GO:goiania"))
    r = await client.put(f"/api/v1/onboarding/{app_id}/steps/installation",
                         json={"technician_user_id": str(tech.id), "installation_date": "2026-10-20"},
                         headers=adm.headers)
    assert r.status_code == 200, r.text
    r = await client.put(f"/api/v1/onboarding/{app_id}/steps/commercial",
                         json={"pays_monthly_fee": True}, headers=adm.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "MONTHLY_FEE_REQUIRED"
    r = await client.post("/api/v1/onboarding/public/application/submit", headers=h)
    assert r.status_code == 200
    # comercial (escopo CITY) não aprova
    r = await client.post(f"/api/v1/onboarding/{app_id}/approve", headers=sales.headers)
    assert r.status_code == 403
    r = await client.post(f"/api/v1/onboarding/{app_id}/approve", headers=adm.headers)
    assert r.status_code == 200, r.text
    est = await db.get(Establishment, uuid.UUID(r.json()["establishment_id"]))
    assert est.responsible_technician_id == tech.id and est.installation_date.isoformat() == "2026-10-20"
    inst = (await db.execute(select(Installation).where(Installation.establishment_id == est.id))).scalar_one()
    assert inst.technician_user_id == tech.id and inst.status == "SCHEDULED"


async def test_reject_and_duplicate_document(client, db, mail, admin):
    from tests.integration.org_api.helpers import mk_org

    existing = await mk_org(db)
    email = f"rej{uuid.uuid4().hex[:6]}@example.com"
    await client.post("/api/v1/onboarding/public/signup", json={"email": email})
    tok = _token_from(mail, email)
    h = await _fill_public(client, tok, existing.document)
    r = await client.post("/api/v1/onboarding/public/application/submit", headers=h)
    app_id = r.json()["id"]
    r = await client.post(f"/api/v1/onboarding/{app_id}/approve", headers=admin.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_ALREADY_REGISTERED"
    r = await client.post(f"/api/v1/onboarding/{app_id}/reject", json={"reason": "CNPJ já é parceiro; usar cadastro"},
                          headers=admin.headers)
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert "não foi aprovado" in mail.last_to(email).text


async def test_signup_does_not_enumerate_and_is_rate_limited(client, db, mail):
    email = f"rl{uuid.uuid4().hex[:6]}@example.com"
    codes = []
    for _ in range(4):
        r = await client.post("/api/v1/onboarding/public/signup", json={"email": email})
        codes.append((r.status_code, r.json()["status"]))
    assert codes == [(202, "accepted")] * 4
    assert len([m for m in mail.outbox if m.to == email]) == 3  # limite por e-mail/dia (resposta idêntica)
