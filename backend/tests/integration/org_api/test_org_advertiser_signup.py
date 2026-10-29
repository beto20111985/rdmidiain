"""W-ORG pós-janela — auto-cadastro do ANUNCIANTE (REQ-ONB-004; ETAPA2 #1017–#1023).

PostgreSQL/Redis reais; usuário/perfil/consentimento pelas interfaces do W1 e lead pela do W-BIZ
(nenhum dublê). Cobre: caminho feliz até o login; token de uso único (reuso/expirado/forjado/query
string); validação sem consumir o token; e-mail existente sem enumeração; CNPJ alfanumérico; rate
limit e CAPTCHA (abstração ``app.core.captcha``); "falar com o comercial" gera UM lead; isolamento.
"""
import asyncio
import json
import re
import time
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.contracts import ContractAcceptance
from app.models.crm import Lead
from app.models.lgpd import ConsentRecord
from app.models.organizations import Organization
from app.models.rbac import Role, RoleAssignment
from app.models.taxonomy import AdvertiserSegment
from app.models.users import User
from app.modules.onboarding import advertiser as adv
from app.modules.organizations.documents import make_cnpj
from tests.factories import login_as, make_user, new_password
from tests.integration.org_api.helpers import audit_actions, mk_est, mk_org, new_cnpj, new_cpf

HDR = adv.TOKEN_HEADER
TERMS_BODY = ("TERMOS DE USO DO ANUNCIANTE — RD Mídia Indoor.\n\nO anunciante declara que o conteúdo é lícito, "
              "próprio e respeita a política de conteúdo da RD.")


async def _publish_terms(client, db):
    adm = await login_as(client, await make_user(db, role="admin", mfa=True))
    r = await client.post("/api/v1/contracts/templates", json={"kind": "ADVERTISER_TERMS",
                                                               "title": "Termos do Anunciante", "body": TERMS_BODY},
                          headers=adm.headers)
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/v1/contracts/templates/{r.json()['id']}/publish", headers=adm.headers)
    assert r.status_code == 200
    return r.json()


async def _segments(db):
    from app.modules.taxonomy.service import ensure_reference_taxonomy

    ids = await ensure_reference_taxonomy(db)
    await db.commit()
    return ids


def _token(mail, email):
    m = mail.last_to(email)
    assert m is not None
    found = re.search(r"token=([A-Za-z0-9_\-]+)", m.text)
    return found.group(1) if found else None


async def _start(client, email):
    r = await client.post("/api/v1/advertiser-signup/start", json={"email": email})
    assert r.status_code == 202
    return r.json()


def _body(terms, seg_ids, *, document=None, password=None, contact_sales=False, **org_kw):
    org = {"name": "Sabor & Cia Alimentos LTDA", "trade_name": "Sabor & Cia", "document": document or new_cnpj(),
           "phone": "62999998888", "city": "Goiânia", "state": "GO", "postal_code": "74.000-000",
           "segment_ids": [str(s) for s in seg_ids]}
    org.update(org_kw)
    return {
        "organization": org,
        "user": {"full_name": "Ana Anunciante", "password": password or new_password()},
        "consents": {"terms_template_id": terms["id"], "accept_terms": True, "terms_version": settings.TERMS_VERSION,
                     "privacy_version": settings.PRIVACY_VERSION, "accept_privacy": True, "marketing_opt_in": True},
        "contact_sales": contact_sales,
    }


async def test_happy_path_alphanumeric_cnpj_until_login(client, db, mail, storage):
    terms = await _publish_terms(client, db)
    ids = await _segments(db)
    email = f"adv{uuid.uuid4().hex[:6]}@example.com"
    t = (await client.get("/api/v1/advertiser-signup/terms")).json()
    assert t["advertiser_terms"]["id"] == terms["id"] and t["terms_version"] == settings.TERMS_VERSION
    assert t["password_policy"]["min_length"] == 10 and t["token_header"] == HDR
    opts = (await client.get("/api/v1/advertiser-signup/options")).json()
    assert str(ids["segment:snacks"]) in {s["id"] for s in opts["segments"]}
    assert {c["kind"] for c in opts["categories"]} == {"SECTOR", "CATEGORY"}
    await _start(client, email)
    tok = _token(mail, email)
    assert tok and "24 horas" in mail.last_to(email).text
    r = await client.get("/api/v1/advertiser-signup/application", headers={HDR: tok})
    assert r.status_code == 200 and r.json()["email"] == email
    cnpj = make_cnpj("AB12CD34EF56")  # CNPJ alfanumérico (IN RFB 2.229/2024)
    pwd = new_password()
    body = _body(terms, [ids["segment:snacks"], ids["segment:bebidas-nao-alcoolicas"]], document=cnpj, password=pwd,
                 category_id=str(ids["supermercado"]))
    body["organization"]["document"] = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}".lower()
    r = await client.post("/api/v1/advertiser-signup/complete", json=body,
                          headers={HDR: tok, "User-Agent": "Mozilla/5.0 (Cadastro RD)"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "ok" and out["access_token"] and "rd_refresh" in r.headers.get("set-cookie", "")
    org_id = out["organization"]["id"]
    assert out["organization"]["business_roles"] == ["ADVERTISER"] and out["organization"]["document_type"] == "CNPJ"
    # banco: organização, perfil, segmentos, papel, aceite e consentimentos
    org = await db.get(Organization, uuid.UUID(org_id))
    assert org.document == cnpj and org.city_key == "GO:goiania" and org.email == email
    segs = (await db.execute(select(AdvertiserSegment.segment_id).where(AdvertiserSegment.org_id == org.id))).scalars()
    assert set(segs) == {ids["segment:snacks"], ids["segment:bebidas-nao-alcoolicas"]}
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    roles = (await db.execute(select(Role.slug, RoleAssignment.scope_type, RoleAssignment.scope_id)
                              .join(Role, Role.id == RoleAssignment.role_id)
                              .where(RoleAssignment.user_id == user.id))).all()
    assert roles == [("advertiser", "ORGANIZATION", org.id)]
    acc = (await db.execute(select(ContractAcceptance).where(ContractAcceptance.org_id == org.id))).scalar_one()
    assert acc.kind == "ADVERTISER_TERMS" and acc.version == terms["version"] and acc.method == "ELECTRONIC"
    assert acc.accepted_by_user_id == user.id and acc.ip_address == "127.0.0.1"
    assert acc.user_agent == "Mozilla/5.0 (Cadastro RD)"
    docs = {(c.document, c.version) for c in (await db.execute(
        select(ConsentRecord).where(ConsentRecord.user_id == user.id))).scalars()}
    assert {("terms", settings.TERMS_VERSION), ("privacy", settings.PRIVACY_VERSION)} <= docs
    assert any(d == "marketing" for d, _ in docs)
    # sessão devolvida funciona; PDF do aceite é real
    h = {"Authorization": f"Bearer {out['access_token']}"}
    me = (await client.get("/api/v1/me", headers=h)).json()
    assert {(a["role"], a["scope_id"]) for a in me["assignments"]} == {("advertiser", org_id)}
    pdf = (await client.get(f"/api/v1/contracts/acceptances/{acc.id}/pdf", headers=h)).content
    assert pdf[:5] == b"%PDF-" and b"%%EOF" in pdf[-1024:]
    # login normal com a senha definida
    r = await client.post("/api/v1/auth/login", json={"login": email, "password": pwd})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    # token de uso único
    r = await client.post("/api/v1/advertiser-signup/complete", json=body, headers={HDR: tok})
    assert r.status_code == 401 and r.json()["error"]["code"] == "SIGNUP_TOKEN_INVALID"
    assert (await client.get("/api/v1/advertiser-signup/application", headers={HDR: tok})).status_code == 401
    acts = await audit_actions(db)
    for a in ("advertiser_signup.requested", "organization.created", "contract.accepted",
              "advertiser_signup.completed"):
        assert a in acts, a


async def test_existing_email_is_not_enumerated(client, db, mail, redis):
    existing = await make_user(db, role="advertiser", scope=("ORGANIZATION", (await mk_org(db, roles=("ADVERTISER",))).id))
    fresh = f"novo{uuid.uuid4().hex[:6]}@example.com"
    r1 = await client.post("/api/v1/advertiser-signup/start", json={"email": existing.email})
    r2 = await client.post("/api/v1/advertiser-signup/start", json={"email": fresh})
    assert (r1.status_code, r1.json()) == (r2.status_code, r2.json()) == (202, r1.json())
    m = mail.last_to(existing.email)
    assert m is not None and "já possui conta" in m.text and "token=" not in m.text
    assert _token(mail, fresh)
    keys = [k async for k in redis.scan_iter(match=settings.REDIS_KEY_PREFIX + "advsignup:*")]
    assert len(keys) == 1  # só o e-mail novo recebeu token


async def test_expired_forged_and_query_string_tokens_rejected(client, db, mail, redis):
    email = f"exp{uuid.uuid4().hex[:6]}@example.com"
    await _start(client, email)
    tok = _token(mail, email)
    r = await client.get("/api/v1/advertiser-signup/application", params={"token": tok})
    assert r.status_code == 401  # nunca por query string
    r = await client.get("/api/v1/advertiser-signup/application", headers={HDR: "x" * 43})
    assert r.status_code == 401
    key = adv._key(tok)
    data = json.loads(await redis.get(key))
    data["exp"] = int(time.time()) - 1
    await redis.set(key, json.dumps(data), ex=60)
    r = await client.get("/api/v1/advertiser-signup/application", headers={HDR: tok})
    assert r.status_code == 401 and r.json()["error"]["code"] == "SIGNUP_TOKEN_INVALID"


async def test_validation_errors_do_not_consume_token_and_duplicate_document(client, db, mail, storage):
    terms = await _publish_terms(client, db)
    ids = await _segments(db)
    email = f"val{uuid.uuid4().hex[:6]}@example.com"
    await _start(client, email)
    tok = _token(mail, email)
    h = {HDR: tok}
    seg = [ids["segment:snacks"]]
    cases = [
        (_body(terms, seg, document="11.222.333/0001-80"), 400, "DOCUMENT_INVALID"),
        (_body(terms, seg, password="curta1"), 400, "PASSWORD_POLICY"),
        (_body(terms, seg, password=f"{email.split('@')[0]}123456"), 400, "PASSWORD_POLICY"),
        (_body(terms, [uuid.uuid4()]), 400, "SEGMENT_NOT_FOUND"),
        (_body(terms, seg, state="XX"), 400, "ADDRESS_INVALID"),
    ]
    b = _body(terms, seg)
    b["consents"]["accept_privacy"] = False
    cases.append((b, 400, "CONSENT_REQUIRED"))
    b = _body(terms, seg)
    b["consents"]["privacy_version"] = "1999-01-01"
    cases.append((b, 409, "CONSENT_VERSION_OUTDATED"))
    b = _body(terms, seg)
    b["consents"]["terms_template_id"] = str(uuid.uuid4())
    cases.append((b, 409, "TERMS_VERSION_OUTDATED"))
    existing = await mk_org(db, roles=("ADVERTISER",))
    cases.append((_body(terms, seg, document=existing.document), 409, "DOCUMENT_ALREADY_REGISTERED"))
    b = _body(terms, seg)
    b["organization"]["segment_ids"] = []
    cases.append((b, 422, "VALIDATION_ERROR"))
    for body, status, code in cases:
        r = await client.post("/api/v1/advertiser-signup/complete", json=body, headers=h)
        assert (r.status_code, r.json()["error"]["code"]) == (status, code), r.text
    # token continua válido; nada foi criado
    assert (await client.get("/api/v1/advertiser-signup/application", headers=h)).status_code == 200
    assert (await db.execute(select(func.count()).select_from(User).where(User.email == email))).scalar_one() == 0
    # CPF (pessoa física) é aceito
    r = await client.post("/api/v1/advertiser-signup/complete", json=_body(terms, seg, document=new_cpf()), headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["organization"]["document_type"] == "CPF"


async def test_concurrent_completion_with_same_token_only_one_wins(client_factory, db, mail, storage):
    c1, c2 = client_factory(), client_factory()
    terms = await _publish_terms(c1, db)
    ids = await _segments(db)
    email = f"conc{uuid.uuid4().hex[:6]}@example.com"
    await _start(c1, email)
    tok = _token(mail, email)
    b1 = _body(terms, [ids["segment:snacks"]])
    b2 = _body(terms, [ids["segment:snacks"]])
    r1, r2 = await asyncio.gather(
        c1.post("/api/v1/advertiser-signup/complete", json=b1, headers={HDR: tok}),
        c2.post("/api/v1/advertiser-signup/complete", json=b2, headers={HDR: tok}),
    )
    assert sorted([r1.status_code, r2.status_code]) == [201, 401]
    assert (await db.execute(select(func.count()).select_from(User).where(User.email == email))).scalar_one() == 1


async def test_rate_limit_and_captcha(client, db, mail):
    from app.core.captcha import set_captcha_verifier

    email = f"rl{uuid.uuid4().hex[:6]}@example.com"
    for _ in range(4):
        r = await client.post("/api/v1/advertiser-signup/start", json={"email": email})
        assert r.status_code == 202
    assert len([m for m in mail.outbox if m.to == email]) == 3  # limite por e-mail/dia, resposta idêntica
    for i in range(6):  # total de 10 pedidos por IP/hora
        r = await client.post("/api/v1/advertiser-signup/start", json={"email": f"x{i}{email}"})
        assert r.status_code == 202
    r = await client.post("/api/v1/advertiser-signup/start", json={"email": f"z{email}"})
    assert r.status_code == 429

    class _Captcha:
        enabled = True

        async def verify(self, token, remote_ip):
            return token == "captcha-ok"

    from app.core.redis import get_redis

    r_ = await get_redis()
    async for k in r_.scan_iter(match=settings.REDIS_KEY_PREFIX + "rl:*"):
        await r_.delete(k)
    set_captcha_verifier(_Captcha())
    try:
        r = await client.post("/api/v1/advertiser-signup/start", json={"email": f"cap{email}"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "CAPTCHA_REQUIRED"
        r = await client.post("/api/v1/advertiser-signup/start",
                              json={"email": f"cap{email}", "captcha_token": "captcha-ok"})
        assert r.status_code == 202
        assert (await client.get("/api/v1/advertiser-signup/terms")).json()["captcha_required"] is True
    finally:
        set_captcha_verifier(None)


async def test_contact_sales_creates_single_lead(client, db, mail, storage):
    terms = await _publish_terms(client, db)
    ids = await _segments(db)
    email = f"lead{uuid.uuid4().hex[:6]}@example.com"
    await _start(client, email)
    tok = _token(mail, email)
    r = await client.post("/api/v1/advertiser-signup/complete",
                          json=_body(terms, [ids["segment:snacks"]], contact_sales=True), headers={HDR: tok})
    assert r.status_code == 201 and r.json()["sales_contact_requested"] is True
    org_id = uuid.UUID(r.json()["organization"]["id"])
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/api/v1/advertiser-signup/sales-contact", json={"org_id": str(org_id),
                                                                           "message": "Quero falar sobre pacotes"},
                          headers=h)
    assert r.status_code == 200 and r.json()["already_requested"] is True
    leads = (await db.execute(select(Lead).where(Lead.org_id == org_id))).scalars().all()
    assert len(leads) == 1 and leads[0].source == "AUTO_CADASTRO" and leads[0].email == email
    assert leads[0].city_key == "GO:goiania"
    assert "advertiser_signup.sales_contact_requested" in await audit_actions(db, org_id)


async def test_new_advertiser_sees_nothing_of_other_orgs(client, db, mail, storage):
    terms = await _publish_terms(client, db)
    ids = await _segments(db)
    other = await mk_org(db, "Concorrente Oculto", roles=("ADVERTISER", "PARTNER"))
    est = await mk_est(db, other, name="Loja Oculta")
    email = f"iso{uuid.uuid4().hex[:6]}@example.com"
    await _start(client, email)
    tok = _token(mail, email)
    r = await client.post("/api/v1/advertiser-signup/complete", json=_body(terms, [ids["segment:snacks"]]),
                          headers={HDR: tok})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.get("/api/v1/organizations", headers=h)).status_code == 403
    assert (await client.get(f"/api/v1/organizations/{other.id}", headers=h)).status_code == 403
    assert (await client.get("/api/v1/establishments", headers=h)).status_code == 403
    assert (await client.get(f"/api/v1/establishments/{est.id}", headers=h)).status_code == 403
    assert (await client.get("/api/v1/screens", headers=h)).status_code == 403
    r = await client.get("/api/v1/contracts/acceptances", params={"org_id": str(other.id)}, headers=h)
    assert r.status_code == 404
    r = await client.post("/api/v1/advertiser-signup/sales-contact", json={"org_id": str(other.id)}, headers=h)
    assert r.status_code == 404
    r = await client.get("/api/v1/catalog/availability", headers=h)
    assert r.status_code == 200 and "Loja Oculta" not in r.text and "Concorrente Oculto" not in r.text


@pytest.fixture(autouse=True)
def _reset_captcha():
    from app.core.captcha import set_captcha_verifier

    set_captcha_verifier(None)
    yield
    set_captcha_verifier(None)
