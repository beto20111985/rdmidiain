"""Criativos: código RD imutável/sem caracteres proibidos/único com retry; durações 30/60/120
(tolerância −1,0 s/+0,04 s); versões = novo código; escopo por organização. PostgreSQL real."""
import re
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.authz import AuthContext
from app.core.ids import FORBIDDEN_CHARS, IdGenerationError, new_creative_code
from app.models.campaigns import Creative
from app.modules.campaigns import creatives as CR
from tests.camp_support import make_media
from tests.factories import login_as, make_org, make_user

CODE_RE = re.compile(r"^RD-[ABCDEFGHJMNPQRSTUVYZ23456789]{6}$")


async def _adv(db, client):
    org = await make_org(db)
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, u)
    return org, s


async def test_duracoes_aceitas_e_rejeitadas_via_api(db, client):
    org, s = await _adv(db, client)
    ok = {}
    for dur, ms in ((30, 30000), (60, 60000), (120, 120000), (30, 29200), (30, 30040)):
        m = await make_media(db, org, duration_ms=ms)
        r = await client.post("/api/v1/creatives", headers=s.headers,
                              json={"name": f"V{dur}", "media_file_id": str(m.id), "duration_seconds": dur})
        assert r.status_code == 201, (dur, ms, r.text)
        body = r.json()
        assert CODE_RE.match(body["code"]) and body["technical_status"] == "VALIDO"
        ok[(dur, ms)] = body["code"]
    # 15/45/90 nunca
    for dur in (15, 45, 90):
        m = await make_media(db, org, duration_ms=dur * 1000)
        r = await client.post("/api/v1/creatives", headers=s.headers,
                              json={"name": "xx", "media_file_id": str(m.id), "duration_seconds": dur})
        assert r.status_code == 400 and r.json()["error"]["code"] == "DURATION_NOT_ALLOWED", r.text
    # 31,2 s e 30,5 s em slot de 30 s: rejeitados (nunca cortar); 31,2 s também não cabe em 60
    for ms, dur in ((31200, 30), (30500, 30), (31200, 60), (28900, 30)):
        m = await make_media(db, org, duration_ms=ms)
        r = await client.post("/api/v1/creatives", headers=s.headers,
                              json={"name": "xx", "media_file_id": str(m.id), "duration_seconds": dur})
        assert r.status_code == 400 and r.json()["error"]["code"] == "DURATION_MISMATCH", (ms, dur, r.text)
    # imagem: duração configurável dentro de 30/60/120
    img = await make_media(db, org, kind="IMAGE", duration_ms=None)
    for dur in (30, 60, 120):
        r = await client.post("/api/v1/creatives", headers=s.headers,
                              json={"name": "img", "media_file_id": str(img.id), "duration_seconds": dur})
        assert r.status_code == 201, r.text
    r = await client.post("/api/v1/creatives", headers=s.headers,
                          json={"name": "img", "media_file_id": str(img.id), "duration_seconds": 45})
    assert r.status_code == 400
    n = (await db.execute(select(Creative).where(Creative.org_id == org.id))).scalars().all()
    assert len(n) == 8  # 5 vídeos válidos + 3 imagens


def test_duration_rule_boundaries_unit():
    assert CR.duration_problem("VIDEO", 30, 29000) is None
    assert CR.duration_problem("VIDEO", 30, 30040) is None
    assert CR.duration_problem("VIDEO", 30, 30041)["code"] == "DURATION_MISMATCH"
    assert CR.duration_problem("VIDEO", 30, 28999)["code"] == "DURATION_MISMATCH"
    assert CR.duration_problem("VIDEO", 120, 120000) is None
    assert CR.duration_problem("IMAGE", 90, None)["code"] == "DURATION_NOT_ALLOWED"


async def test_midia_de_outra_org_e_nao_pronta(db, client):
    org, s = await _adv(db, client)
    other = await make_org(db)
    m_other = await make_media(db, other)
    r = await client.post("/api/v1/creatives", headers=s.headers,
                          json={"name": "xx", "media_file_id": str(m_other.id), "duration_seconds": 30})
    assert r.status_code == 404
    pend = await make_media(db, org, status="PROCESSING")
    r = await client.post("/api/v1/creatives", headers=s.headers,
                          json={"name": "xx", "media_file_id": str(pend.id), "duration_seconds": 30})
    assert r.status_code == 409 and r.json()["error"]["code"] == "MEDIA_NOT_READY"
    # anunciante não cria conteúdo interno/institucional
    m = await make_media(db, org)
    r = await client.post("/api/v1/creatives", headers=s.headers,
                          json={"name": "xx", "media_file_id": str(m.id), "duration_seconds": 30, "kind": "INTERNAL"})
    assert r.status_code == 403


async def test_codigo_imutavel_no_banco(db):
    org = await make_org(db)
    m = await make_media(db, org)
    c = await CR.create_creative(db, AuthContext.system("system"), org_id=org.id, name="a", media_file_id=m.id,
                                 duration_seconds=30)
    await db.commit()
    cid, mid, oid = c.id, m.id, org.id
    with pytest.raises(DBAPIError):
        await db.execute(text("UPDATE creatives SET code = 'RD-AAAAAA' WHERE id = :i"), {"i": cid})
        await db.commit()
    await db.rollback()
    # formato inválido é recusado pelo CHECK
    with pytest.raises(IntegrityError):
        await db.execute(text(
            "INSERT INTO creatives (id, org_id, code, name, kind, media_file_id, duration_seconds, orientation, "
            "status, technical_status, version, created_at, updated_at) VALUES (:i, :o, 'RD-AAAAA1', 'x', "
            "'EXTERNAL', :m, 30, 'HORIZONTAL', 'RASCUNHO', 'VALIDO', 1, now(), now())"),
            {"i": uuid.uuid4(), "o": oid, "m": mid})
    await db.rollback()
    # duração fora de 30/60/120 recusada pelo CHECK do banco
    with pytest.raises(IntegrityError):
        await db.execute(text("UPDATE creatives SET duration_seconds = 45 WHERE id = :i"), {"i": cid})
    await db.rollback()
    # outras colunas continuam editáveis (só o código é imutável)
    await db.execute(text("UPDATE creatives SET name = 'renomeado' WHERE id = :i"), {"i": cid})
    await db.commit()


def test_geracao_massiva_sem_proibidos_e_sem_colisao_em_200k():
    codes = [new_creative_code() for _ in range(200_000)]
    assert all(CODE_RE.match(c) for c in codes)
    assert not any(set(c[3:]) & FORBIDDEN_CHARS for c in codes)
    dup = len(codes) - len(set(codes))
    # 28^6 ≈ 4,8e8: duplicatas esperadas ≈ 41 em 200 mil — o UNIQUE + retry resolve; aqui só
    # garantimos que não há viés grosseiro (muito acima do esperado indicaria gerador quebrado)
    assert dup < 200


async def test_unique_e_retry_em_colisao_forcada(db):
    org = await make_org(db)
    m = await make_media(db, org)
    ctx = AuthContext.system("system")
    first = await CR.create_creative(db, ctx, org_id=org.id, name="a", media_file_id=m.id, duration_seconds=30)
    await db.commit()
    # gerador devolve o código existente 2× antes de um novo → pré-checagem detecta e tenta de novo
    seq = iter([first.code, first.code, "RD-BCDEFG"])
    calls = []

    def gen():
        calls.append(1)
        return next(seq)

    c2 = await CR.create_creative(db, ctx, org_id=org.id, name="b", media_file_id=m.id, duration_seconds=30, gen=gen)
    await db.commit()
    assert c2.code == "RD-BCDEFG" and len(calls) == 3
    # corrida: sem pré-checagem, o INSERT bate no UNIQUE dentro de SAVEPOINT e o retry usa outro código
    seq2 = iter([first.code, "RD-CDEFGH"])
    c3 = Creative(id=uuid.uuid4(), org_id=org.id, name="c", kind="EXTERNAL", media_file_id=m.id,
                  duration_seconds=30, orientation="HORIZONTAL", status="RASCUNHO", technical_status="VALIDO",
                  version=1, code="RD-ZZZZZZ")
    await CR.insert_with_unique_code(db, c3, gen=lambda: next(seq2), precheck=False)
    await db.commit()
    assert c3.code == "RD-CDEFGH"
    codes = (await db.execute(select(Creative.code).where(Creative.org_id == org.id))).scalars().all()
    assert len(codes) == len(set(codes)) == 3
    # esgotamento de tentativas → erro explícito (nunca código duplicado)
    with pytest.raises(IdGenerationError):
        await CR.insert_with_unique_code(db, Creative(id=uuid.uuid4(), org_id=org.id, name="d", kind="EXTERNAL",
                                                      media_file_id=m.id, duration_seconds=30,
                                                      orientation="HORIZONTAL", status="RASCUNHO",
                                                      technical_status="VALIDO", version=1, code="RD-ZZZZZZ"),
                                         gen=lambda: first.code, max_attempts=3)
    await db.rollback()


async def test_nova_versao_gera_novo_codigo_e_preserva_anterior(db, client):
    org, s = await _adv(db, client)
    m = await make_media(db, org)
    r = await client.post("/api/v1/creatives", headers=s.headers,
                          json={"name": "v1", "media_file_id": str(m.id), "duration_seconds": 30, "ad_text": "A"})
    v1 = r.json()
    m2 = await make_media(db, org, duration_ms=60000)
    r = await client.post(f"/api/v1/creatives/{v1['id']}/versions", headers=s.headers,
                          json={"media_file_id": str(m2.id), "duration_seconds": 60, "ad_text": "B"})
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["code"] != v1["code"] and v2["version"] == 2 and v2["supersedes_id"] == v1["id"]
    assert v2["root_id"] == v1["id"]
    r = await client.get(f"/api/v1/creatives/{v1['id']}", headers=s.headers)
    assert r.json()["code"] == v1["code"] and r.json()["status"] == "RASCUNHO"
    assert [v["version"] for v in r.json()["versions"]] == [1, 2]
    # segunda versão paralela do mesmo criativo é recusada
    r = await client.post(f"/api/v1/creatives/{v1['id']}/versions", headers=s.headers, json={"ad_text": "C"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CREATIVE_VERSION_PENDING"


async def test_criativo_de_outra_org_404(db, client):
    org, s = await _adv(db, client)
    other = await make_org(db)
    m = await make_media(db, other)
    c = await CR.create_creative(db, AuthContext.system("system"), org_id=other.id, name="x", media_file_id=m.id,
                                 duration_seconds=30)
    await db.commit()
    assert (await client.get(f"/api/v1/creatives/{c.id}", headers=s.headers)).status_code == 404
    r = await client.get("/api/v1/creatives", headers=s.headers)
    assert all(i["org_id"] == str(org.id) for i in r.json()["items"])
    assert (await client.get(f"/api/v1/campaigns/by-code/{c.code}", headers=s.headers)).status_code == 404
