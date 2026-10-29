"""W-ORG — contratos: versão publicada imutável, aceite eletrônico (versão/IP/data/usuário) com PDF real,
idempotência, equipe RD não assina pelo cliente, aceite append-only, ativação do estabelecimento."""
import hashlib
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.contracts import ContractAcceptance
from app.models.screens import Screen
from tests.factories import login_as, make_user
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org

BODY_V1 = ("CONTRATO DE PARCERIA — RD Mídia Indoor.\n\nCláusula 1. O PARCEIRO cede espaço para a instalação de telas.\n\n"
           "Cláusula 2. A RD controla integralmente o conteúdo exibido.")
BODY_V2 = BODY_V1 + "\n\nCláusula 3. Comissão conforme configuração do cadastro."


async def _publish(client, h, body=BODY_V1, kind="PARTNERSHIP"):
    r = await client.post("/api/v1/contracts/templates", json={"kind": kind, "title": "Contrato de Parceria",
                                                               "body": body}, headers=h)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = await client.post(f"/api/v1/contracts/templates/{tid}/publish", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_electronic_acceptance_generates_pdf_and_activates_establishment(client, db, admin, storage):
    org = await mk_org(db)
    est = await mk_est(db, org, onboarding_status="AWAITING_CONTRACT")
    scr = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id="TEL-GO-QQ22", name="Caixa", sellable=False)
    db.add(scr)
    await db.commit()
    tpl = await _publish(client, admin.headers)
    assert tpl["version"] == 1 and tpl["status"] == "PUBLISHED"
    assert tpl["body_sha256"] == hashlib.sha256(BODY_V1.encode()).hexdigest()

    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org.id), name="Maria Parceira")
    s = await login_as(client, owner)
    cur = (await client.get("/api/v1/contracts/templates/current", params={"kind": "PARTNERSHIP"},
                            headers=s.headers)).json()
    assert cur["id"] == tpl["id"] and cur["body"] == BODY_V1
    payload = {"template_id": tpl["id"], "org_id": str(org.id), "establishment_id": str(est.id)}
    r = await client.post("/api/v1/contracts/accept", json=payload,
                          headers={**s.headers, "User-Agent": "Mozilla/5.0 (Teste RD)"})
    assert r.status_code == 200, r.text
    acc = r.json()
    assert acc["created"] is True and acc["version"] == 1 and acc["method"] == "ELECTRONIC"
    assert acc["accepted_by_user_id"] == str(owner.id) and acc["ip_address"] == "127.0.0.1"
    assert acc["accepted_at"] and acc["establishment_activated"] == str(est.id)
    assert acc["body_sha256"] == tpl["body_sha256"]
    # PDF real
    r = await client.get(f"/api/v1/contracts/acceptances/{acc['id']}/pdf", headers=s.headers)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    pdf = r.content
    assert pdf[:5] == b"%PDF-" and b"%%EOF" in pdf[-1024:] and len(pdf) > 1500
    assert hashlib.sha256(pdf).hexdigest() == acc["pdf"]["sha256"] == r.headers["x-content-sha256"]
    row = await db.get(ContractAcceptance, uuid.UUID(acc["id"]))
    assert row.user_agent == "Mozilla/5.0 (Teste RD)" and row.signer_name == "Maria Parceira"
    # idempotente: repetir devolve o mesmo aceite
    r = await client.post("/api/v1/contracts/accept", json=payload, headers=s.headers)
    assert r.status_code == 200 and r.json()["id"] == acc["id"] and r.json()["created"] is False
    # estabelecimento ativado e telas vendáveis
    est_out = (await client.get(f"/api/v1/establishments/{est.id}", headers=s.headers)).json()
    assert est_out["onboarding_status"] == "ACTIVE" and est_out["screens_sellable"] == 1
    assert "contract.accepted" in await audit_actions(db, acc["id"])
    # append-only
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("UPDATE contract_acceptances SET ip_address='1.1.1.1' WHERE id=:i"),
                         {"i": uuid.UUID(acc["id"])})
        await db.commit()
    assert "RD_APPEND_ONLY" in str(ei.value)
    await db.rollback()


async def test_rd_staff_cannot_sign_for_client_and_other_org_gets_404(client, db, admin, storage):
    org, other = await mk_org(db), await mk_org(db)
    est = await mk_est(db, org, onboarding_status="AWAITING_CONTRACT")
    tpl = await _publish(client, admin.headers)
    payload = {"template_id": tpl["id"], "org_id": str(org.id), "establishment_id": str(est.id)}
    r = await client.post("/api/v1/contracts/accept", json=payload, headers=admin.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CONTRACT_SIGNER_NOT_CLIENT"
    intruder = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", other.id))
    s = await login_as(client, intruder)
    r = await client.post("/api/v1/contracts/accept", json=payload, headers=s.headers)
    assert r.status_code == 404
    rows = (await db.execute(select(ContractAcceptance))).scalars().all()
    assert rows == []


async def test_outdated_version_rejected_and_published_text_immutable(client, db, admin, storage):
    org = await mk_org(db)
    est = await mk_est(db, org, onboarding_status="AWAITING_CONTRACT")
    org_id, est_id = org.id, est.id  # o rollback abaixo expira os objetos da sessão
    v1 = await _publish(client, admin.headers)
    r = await client.patch(f"/api/v1/contracts/templates/{v1['id']}", json={"body": BODY_V2}, headers=admin.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONTRACT_TEMPLATE_IMMUTABLE"
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("UPDATE contract_templates SET body='alterado' WHERE id=:i"), {"i": uuid.UUID(v1["id"])})
        await db.commit()
    assert "RD_CONTRACT_TEMPLATE_IMMUTABLE" in str(ei.value)
    await db.rollback()
    v2 = await _publish(client, admin.headers, BODY_V2)
    assert v2["version"] == 2
    lst = (await client.get("/api/v1/contracts/templates", params={"kind": "PARTNERSHIP"},
                            headers=admin.headers)).json()["items"]
    assert {(t["version"], t["status"]) for t in lst} == {(1, "RETIRED"), (2, "PUBLISHED")}
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org_id))
    s = await login_as(client, owner)
    r = await client.post("/api/v1/contracts/accept",
                          json={"template_id": v1["id"], "org_id": str(org_id), "establishment_id": str(est_id)},
                          headers=s.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONTRACT_VERSION_OUTDATED"
    assert r.json()["error"]["details"]["current_version"] == 2
    r = await client.post("/api/v1/contracts/accept",
                          json={"template_id": v2["id"], "org_id": str(org_id), "establishment_id": str(est_id)},
                          headers=s.headers)
    assert r.status_code == 200 and r.json()["version"] == 2


async def test_manual_record_is_labeled_and_generates_pdf(client, db, admin, storage):
    org = await mk_org(db)
    est = await mk_est(db, org, onboarding_status="AWAITING_CONTRACT")
    tpl = await _publish(client, admin.headers)
    r = await client.post("/api/v1/contracts/acceptances/manual", json={
        "template_id": tpl["id"], "org_id": str(org.id), "establishment_id": str(est.id),
        "signer_name": "José Proprietário", "notes": "Via física assinada em 07/10/2026, arquivada na pasta 12"},
        headers=admin.headers)
    assert r.status_code == 201, r.text
    acc = r.json()
    assert acc["method"] == "MANUAL_RECORD" and acc["accepted_by_user_id"] is None
    assert acc["establishment_activated"] == str(est.id)
    pdf = (await client.get(f"/api/v1/contracts/acceptances/{acc['id']}/pdf", headers=admin.headers)).content
    assert pdf.startswith(b"%PDF-")


async def test_advertiser_terms_acceptance(client, db, admin, storage):
    adv_org = await mk_org(db, roles=("ADVERTISER",))
    tpl = await _publish(client, admin.headers, kind="ADVERTISER_TERMS",
                         body="TERMOS DE USO DO ANUNCIANTE.\n\nO anunciante declara que o conteúdo é lícito e próprio.")
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", adv_org.id))
    s = await login_as(client, adv)
    r = await client.post("/api/v1/contracts/accept", json={"template_id": tpl["id"], "org_id": str(adv_org.id)},
                          headers=s.headers)
    assert r.status_code == 200 and r.json()["kind"] == "ADVERTISER_TERMS"
    lst = (await client.get("/api/v1/contracts/acceptances", params={"org_id": str(adv_org.id)},
                            headers=s.headers)).json()
    assert [a["id"] for a in lst["items"]] == [r.json()["id"]]
