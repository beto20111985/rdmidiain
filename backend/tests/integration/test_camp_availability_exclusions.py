"""AÇÃO 004 — disponibilidade da etapa "Telas e locais": diagnóstico de EXCLUSÃO só para a RD (PostgreSQL real).

Contexto (TEL-DF-Y7ED): a tela casava a segmentação (UF=DF) mas ficava fora por duas regras legítimas —
(1) estabelecimento ``AWAITING_CONTRACT`` ⇒ ``screens.sellable=false`` ⇒ fora do inventário vendável;
(2) conflito ``SEGMENTO`` (anúncio "Bebidas" × estabelecimento que vende Bebidas/Cerveja).
As REGRAS não mudam. O defeito era de representação: ``POST /campaigns/availability`` devolvia 0 sem dizer
por quê. Agora a RD recebe o bloco aditivo ``exclusions`` (CONTAGENS por motivo, sem nomes/ids de
estabelecimento); o anunciante recebe a resposta IDÊNTICA à anterior (sem o bloco).

Também: cenário de regressão do item 36 (plano parceiro → estabelecimento DF → tela → player pareado →
contrato → mídia/criativo → anunciante → campanha → disponibilidade → alvo → reserva/pagamento falso →
aprovação → manifesto da tela com a campanha, lido pelo próprio player).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.manifests import ManifestVersion
from app.modules.campaigns import targeting as TG
from tests.camp_real import approval, camp, checkout_and_pay, create_campaign, set_payer, today_local
from tests.camp_support import Tax, make_est, make_price_table, make_screen, set_advertiser
from tests.factories import login_as, make_org, make_user
from tests.fin_helpers import fin_env  # noqa: F401  (fixture do W-FIN: provedor falso + segredo de webhook)
from tests.integration.org_api.helpers import mk_org, staff_session
from tests.player_support import pair_via_panel

AVAIL = "/api/v1/campaigns/availability"
DF = [{"type": "STATE", "value": "DF"}]
CONTRACT_BODY = ("CONTRATO DE PARCERIA — RD Mídia Indoor (teste AÇÃO 004).\n\nCláusula 1. O PARCEIRO cede espaço "
                 "para a instalação de telas.\n\nCláusula 2. A RD controla integralmente o conteúdo exibido.")
SUMMARY_KEYS = {"screens_available", "establishments_count", "screens_letterbox", "excluded_by_duration",
                "excluded_by_conflict_establishments", "by_region", "by_state", "by_city"}


@pytest.fixture
def storage(tmp_path):
    """Armazenamento local descartável (PDF do aceite do contrato) — igual ao fixture do W-ORG."""
    from app.core.storage import LocalStorageProvider, set_storage

    p = LocalStorageProvider(tmp_path / "storage")
    set_storage(p)
    yield p
    set_storage(None)


def _ooi(body: dict) -> dict:
    """{motivo: (telas, estabelecimentos)} de ``exclusions.out_of_inventory``."""
    return {r["reason"]: (r["screens"], r["establishments"]) for r in body["exclusions"]["out_of_inventory"]["by_reason"]}


def _conf(body: dict) -> dict:
    """{reason_code: (telas, estabelecimentos)} de ``exclusions.conflict``."""
    return {r["reason_code"]: (r["screens"], r["establishments"]) for r in body["exclusions"]["conflict"]["by_reason"]}


async def _publish_partnership(client, h) -> dict:
    r = await client.post("/api/v1/contracts/templates", json={"kind": "PARTNERSHIP", "title": "Contrato de Parceria",
                                                               "body": CONTRACT_BODY}, headers=h)
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/v1/contracts/templates/{r.json()['id']}/publish", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _record_partnership(client, h, tpl, org_id, est_id) -> dict:
    """Decisão do proprietário (AÇÃO 004): a equipe RD registra o aceite na aba Contratos (serviço REAL)."""
    r = await client.post("/api/v1/contracts/acceptances/manual", json={
        "template_id": tpl["id"], "org_id": str(org_id), "establishment_id": str(est_id),
        "signer_name": "Responsável do estabelecimento",
        "notes": "teste local AÇÃO 004 — sem contrato físico"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def _df_partner(client, db, admin, t, *, segments=()):
    """Estabelecimento DF criado PELA API (nasce ``AWAITING_CONTRACT``) + tela ACTIVE criada pela API."""
    partner = await mk_org(db)
    r = await client.post("/api/v1/establishments", headers=admin.headers, json={
        "org_id": str(partner.id), "name": "Bar Teste DF 004", "city": "Samambaia", "state": "DF",
        "postal_code": "72302107", "segment_ids": [str(s.id) for s in segments]})
    assert r.status_code == 201, r.text
    est = r.json()
    assert est["onboarding_status"] == "AWAITING_CONTRACT" and est["region"] == "CENTRO_OESTE"
    r = await client.post("/api/v1/screens", headers=admin.headers, json={
        "establishment_id": est["id"], "name": "TV NOVA", "inches": 43})
    assert r.status_code == 201, r.text
    scr = r.json()
    assert scr["status"] == "ACTIVE" and scr["sellable"] is False  # "Ativa" no cadastro, FORA do inventário
    return partner, est, scr


async def test_rd_ve_motivo_da_exclusao_contrato_e_segmento_anunciante_recebe_resposta_identica(db, client, storage):
    t = await Tax().create(db)
    admin = await staff_session(client, db, "admin")
    partner, est, scr = await _df_partner(client, db, admin, t, segments=[t.bebidas, t.cerveja])
    adv_org = await make_org(db)
    await set_advertiser(db, adv_org, category=t.academia)
    adv = await login_as(client, await make_user(db, role="advertiser", scope=("ORGANIZATION", adv_org.id)))
    r = await client.post("/api/v1/campaigns", headers=adv.headers, json={
        "name": "Primeira do anunciante", "targets": DF, "segment_ids": [str(t.bebidas.id)]})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    req = {"campaign_id": cid, "targets": DF, "duration_s": 30}

    # (1) aguardando o aceite do contrato ⇒ 0 disponível; a RD sabe POR QUÊ
    rd = await client.post(AVAIL, headers=admin.headers, json=req)
    assert rd.status_code == 200, rd.text
    rd = rd.json()
    assert rd["screens_available"] == 0
    assert "exclusions" in rd, "RD não recebe o diagnóstico de exclusão (filtro silencioso — item 14)"
    ex = rd["exclusions"]
    assert ex["screens_total"] == 1
    assert ex["out_of_inventory"]["screens"] == 1 and ex["out_of_inventory"]["establishments"] == 1
    assert _ooi(rd) == {"AWAITING_CONTRACT": (1, 1)}
    # 2ª camada (só orientação, regra intacta): depois do aceite, a MESMA tela ainda cairia por SEGMENTO
    assert ex["out_of_inventory"]["also_conflict"] == [{"reason_code": "SEGMENTO", "screens": 1, "establishments": 1}]
    assert ex["duration"] == {"screens": 0}
    assert ex["conflict"]["screens"] == 0 and ex["conflict"]["by_reason"] == []
    # sem nomes/ids de estabelecimento nem de tela no bloco (os nomes ficam no painel de conflitos da RD)
    blob = str(ex)
    for secret in (est["id"], est["name"], scr["id"], scr["friendly_id"], str(partner.id)):
        assert secret not in blob

    # anunciante: resposta IDÊNTICA à de hoje — sem o bloco, mesmas chaves e mesmos números
    a = await client.post(AVAIL, headers=adv.headers, json=req)
    assert a.status_code == 200, a.text
    assert "exclusions" not in a.json()
    assert set(a.json()) == SUMMARY_KEYS
    assert a.json() == {k: v for k, v in rd.items() if k != "exclusions"}
    assert est["name"] not in a.text and est["id"] not in a.text

    # (2) aceite registrado pela equipe RD (contracts.service real) ⇒ estabelecimento ACTIVE e tela vendável;
    #     a campanha com "bebidas" continua fora — agora por conflito SEGMENTO (contado por reason_code)
    tpl = await _publish_partnership(client, admin.headers)
    acc = await _record_partnership(client, admin.headers, tpl, partner.id, est["id"])
    assert acc["establishment_activated"] == est["id"]
    assert (await client.get(f"/api/v1/screens/{scr['id']}", headers=admin.headers)).json()["sellable"] is True
    rd = (await client.post(AVAIL, headers=admin.headers, json=req)).json()
    assert rd["screens_available"] == 0 and rd["excluded_by_conflict_establishments"] == 1
    assert rd["exclusions"]["out_of_inventory"]["screens"] == 0 and _ooi(rd) == {}
    assert rd["exclusions"]["out_of_inventory"]["also_conflict"] == []
    assert rd["exclusions"]["conflict"]["screens"] == 1 and rd["exclusions"]["conflict"]["establishments"] == 1
    assert _conf(rd) == {"SEGMENTO": (1, 1)}
    assert rd["exclusions"]["screens_total"] == 1
    a = (await client.post(AVAIL, headers=adv.headers, json=req)).json()
    assert "exclusions" not in a and a == {k: v for k, v in rd.items() if k != "exclusions"}

    # (3) sem o segmento em conflito (decisão do proprietário: tirar "Bebidas") ⇒ a tela ENTRA
    r = await client.put(f"/api/v1/campaigns/{cid}/segments", headers=adv.headers,
                         json={"segment_ids": [str(t.alimentos.id)]})
    assert r.status_code == 200, r.text
    rd = (await client.post(AVAIL, headers=admin.headers, json=req)).json()
    assert rd["screens_available"] == 1 and rd["establishments_count"] == 1
    assert rd["by_state"] == [{"state": "DF", "screens": 1}]
    # nome de exibição da cidade (com acento, do cadastro) — nunca nome de estabelecimento
    assert rd["by_city"] == [{"city_key": "DF:samambaia", "city": "Samambaia", "screens": 1}]
    assert rd["exclusions"]["screens_total"] == 0 and _ooi(rd) == {} and _conf(rd) == {}
    a = (await client.post(AVAIL, headers=adv.headers, json=req)).json()
    assert a["screens_available"] == 1 and "exclusions" not in a


async def test_motivos_fora_do_inventario_duracao_e_perfis(db, client, monkeypatch):
    """Cada motivo derivado dos CAMPOS (onboarding_status, is_active, deleted_at, status) — sem duplicar
    ``compute_sellable``; mesma segmentação (inclusões/exclusões) do ``targeting.resolve``."""
    t = await Tax().create(db)
    adv_org = await make_org(db)
    await set_advertiser(db, adv_org, category=t.academia)
    ok = await make_est(db)
    ok_screen = await make_screen(db, ok)  # vendável → disponível
    await make_screen(db, ok, status="INACTIVE", sellable=False)  # tela inativa
    await make_screen(db, ok, sellable=False)  # ACTIVE em estabelecimento ativo, mas marcada fora (outro)
    await make_screen(db, ok, status="DECOMMISSIONED")  # retirada definitivamente: NÃO é candidata
    maint = await make_screen(db, ok, status="MAINTENANCE")  # manutenção continua no inventário
    awaiting = await make_est(db)
    awaiting.onboarding_status = "AWAITING_CONTRACT"
    inactive = await make_est(db)
    inactive.is_active = False
    inactive.onboarding_status = "AWAITING_CONTRACT"  # inativo tem precedência como motivo
    deleted = await make_est(db)
    deleted.deleted_at = datetime.now(timezone.utc)
    other_city = await make_est(db, city="Anápolis")
    other_city.onboarding_status = "AWAITING_CONTRACT"
    excluded_nb = await make_est(db, neighborhood="Setor Oeste")
    excluded_nb.onboarding_status = "AWAITING_CONTRACT"
    await db.commit()
    await make_screen(db, awaiting, sellable=False)
    await make_screen(db, inactive, sellable=False)
    await make_screen(db, deleted)  # flag ainda true, estabelecimento excluído
    await make_screen(db, other_city, sellable=False)  # fora da segmentação: não conta
    await make_screen(db, excluded_nb, sellable=False)  # removido pela exclusão de bairro: não conta

    targets = [{"type": "CITY", "value": "GO:Goiânia"},
               {"type": "NEIGHBORHOOD", "value": "GO:Goiânia:Setor Oeste", "exclude": True}]
    body = {"org_id": str(adv_org.id), "targets": targets, "duration_s": 60}

    # bloco externo: a tela em manutenção só tem 30 s → fora por duração (já existia; agora também no bloco)
    real_ext = TG._external_seconds

    async def ext(db_, ids):
        got = await real_ext(db_, ids)
        return {sid: (30 if sid == maint.id else max(v, 60)) for sid, v in got.items()}

    monkeypatch.setattr(TG, "_external_seconds", ext)

    admin = await staff_session(client, db, "admin")
    r = await client.post(AVAIL, headers=admin.headers, json=body)
    assert r.status_code == 200, r.text
    rd = r.json()
    assert rd["screens_available"] == 1 and rd["excluded_by_duration"] == 1
    ex = rd["exclusions"]
    assert _ooi(rd) == {"ESTABLISHMENT_INACTIVE": (2, 2), "AWAITING_CONTRACT": (1, 1), "SCREEN_INACTIVE": (1, 1),
                        "NOT_SELLABLE": (1, 1)}
    assert [x["reason"] for x in ex["out_of_inventory"]["by_reason"]] == [
        "ESTABLISHMENT_INACTIVE", "AWAITING_CONTRACT", "SCREEN_INACTIVE", "NOT_SELLABLE"]  # ordem estável
    assert ex["out_of_inventory"]["screens"] == 5 and ex["out_of_inventory"]["establishments"] == 4
    assert ex["out_of_inventory"]["also_conflict"] == []
    assert rd["by_city"] == [{"city_key": "GO:goiania", "city": "Goiânia", "screens": 1}]
    assert ex["duration"] == {"screens": 1}
    assert ex["screens_total"] == 6
    blob = str(ex)
    for e in (ok, awaiting, inactive, deleted):
        assert str(e.id) not in blob and e.name not in blob
    assert str(ok_screen.id) not in blob

    # Comercial (equipe RD, GLOBAL, sem campaigns.*) também vê o diagnóstico
    sales = await staff_session(client, db, "sales")
    r = await client.post(AVAIL, headers=sales.headers, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["exclusions"] == ex

    # anunciante: mesma contagem pública, SEM o bloco
    adv = await login_as(client, await make_user(db, role="advertiser", scope=("ORGANIZATION", adv_org.id)))
    r = await client.post(AVAIL, headers=adv.headers, json={"targets": targets, "duration_s": 60})
    assert r.status_code == 200, r.text
    assert "exclusions" not in r.json() and set(r.json()) == SUMMARY_KEYS
    assert r.json() == {k: v for k, v in rd.items() if k != "exclusions"}


@pytest.mark.parametrize("kind", ["NEIGHBORHOOD", "POSTAL_CODE", "CATEGORY"])
async def test_exclusao_so_remove_quem_casa_estabelecimento_sem_o_campo_nao_some(db, kind):
    """Achado da AÇÃO 004 (ao escrever o teste acima): ``NOT (cond)`` com o campo do estabelecimento NULO dá
    NULL em SQL e o estabelecimento SUMIA da segmentação. Ex.: "Goiânia, exceto o bairro Setor Oeste" tirava
    TODOS os estabelecimentos de Goiânia sem bairro cadastrado (idem CEP e categoria). Regra oficial
    (A6 §10.5.2): exclusões removem SÓ quem casa a exclusão."""
    from app.models.campaigns import Campaign

    t = await Tax().create(db)
    org = await make_org(db)
    await set_advertiser(db, org, category=t.academia)
    bare = await make_est(db)  # Goiânia, sem bairro, sem CEP, sem categoria
    s_bare = await make_screen(db, bare)
    oeste = await make_est(db, neighborhood="Setor Oeste", postal_code="74110020", category=t.supermercado)
    s_oeste = await make_screen(db, oeste)
    c = Campaign(id=uuid.uuid4(), org_id=org.id, name="Probe", status="RASCUNHO", duration_s=30)
    db.add(c)
    await db.commit()
    exc = {"NEIGHBORHOOD": {"type": "NEIGHBORHOOD", "value": "GO:Goiânia:Setor Oeste"},
           "POSTAL_CODE": {"type": "POSTAL_CODE", "value": "7411"},
           "CATEGORY": {"type": "CATEGORY", "value": str(t.supermercado.id)}}[kind]
    res = await TG.resolve(db, TG.specs_from([{"type": "CITY", "value": "GO:Goiânia"}, {**exc, "exclude": True}]),
                           duration_s=30, campaign=c)
    assert s_oeste.id not in res.screen_ids, exc  # quem casa a exclusão sai
    assert s_bare.id in res.screen_ids, f"estabelecimento sem o campo sumiu com a exclusão {exc}"


@pytest.mark.usefixtures("fin_env")
async def test_regressao_item36_plano_estabelecimento_df_tela_player_campanha_manifesto(db, client, storage):
    """Item 36: o percurso do proprietário, automatizado. Protege contra regressão de qualquer elo."""
    t = await Tax().create(db)
    admin = await staff_session(client, db, "admin")
    # 1. plano de parceiro
    r = await client.post("/api/v1/partner-plans", headers=admin.headers, json={
        "name": "Plano Teste 004", "tv_provision": "RD_TV_COMODATO", "default_tv_inches": 32,
        "deposit_required": False, "installation_fee_required": False, "upgrade_policy": "NEGOTIATED"})
    assert r.status_code == 201, r.text
    plan = r.json()
    # 2. estabelecimento DF (CEP 72302107) com o plano — nasce aguardando contrato
    partner = await mk_org(db)
    r = await client.post("/api/v1/establishments", headers=admin.headers, json={
        "org_id": str(partner.id), "name": "Bar Regressao DF", "city": "Samambaia", "state": "DF",
        "postal_code": "72302107", "partner_plan_id": plan["id"], "segment_ids": [str(t.bebidas.id)]})
    assert r.status_code == 201, r.text
    est = r.json()
    assert est["partner_plan_id"] == plan["id"] and est["onboarding_status"] == "AWAITING_CONTRACT"
    assert est["region"] == "CENTRO_OESTE" and est["city_key"] == "DF:samambaia"
    # 3. tela (ACTIVE, ainda fora do inventário)
    r = await client.post("/api/v1/screens", headers=admin.headers, json={
        "establishment_id": est["id"], "name": "TV NOVA", "inches": 43})
    assert r.status_code == 201, r.text
    scr = r.json()
    assert scr["status"] == "ACTIVE" and scr["sellable"] is False
    # 4. player pareado pelo painel (ES256 real) e autenticado
    dev = await pair_via_panel(client, admin.headers, scr["id"])
    assert dev.screen_id == scr["id"]
    # 5. contrato de parceria registrado pela equipe RD ⇒ tela vendável
    tpl = await _publish_partnership(client, admin.headers)
    acc = await _record_partnership(client, admin.headers, tpl, partner.id, est["id"])
    assert acc["establishment_activated"] == est["id"]
    assert (await client.get(f"/api/v1/screens/{scr['id']}", headers=admin.headers)).json()["sellable"] is True
    # 6–7. anunciante + mídia válida/criativo + campanha (sem segmento em conflito) com alvo UF=DF
    adv_org = await make_org(db)
    await set_advertiser(db, adv_org, category=t.academia)
    await set_payer(db, adv_org.id)
    await make_price_table(db)
    await approval(db, "AUTO")
    adv = await login_as(client, await make_user(db, role="advertiser", scope=("ORGANIZATION", adv_org.id)))
    start = await today_local(db)
    cid = await create_campaign(client, adv, db, adv_org, start=start, targets=DF, name="Primeira campanha DF")
    # 8. disponibilidade (anunciante: só contagens; RD: sem exclusões)
    r = await client.post(AVAIL, headers=adv.headers, json={"campaign_id": cid, "targets": DF, "duration_s": 30})
    assert r.status_code == 200, r.text
    assert r.json()["screens_available"] == 1 and r.json()["by_state"] == [{"state": "DF", "screens": 1}]
    assert est["name"] not in r.text and est["id"] not in r.text and "exclusions" not in r.json()
    rd = (await client.post(AVAIL, headers=admin.headers,
                            json={"campaign_id": cid, "targets": DF, "duration_s": 30})).json()
    assert rd["screens_available"] == 1 and rd["exclusions"]["screens_total"] == 0
    # 9–10. segmentação salva ⇒ a tela elegível participa (visão pública do anunciante: só o código)
    r = await client.get(f"/api/v1/campaigns/{cid}/screens", headers=adv.headers)
    assert r.status_code == 200 and r.json()["source"] == "PREVISAO"
    assert [i["friendly_id"] for i in r.json()["items"]] == [scr["friendly_id"]]
    # reserva (hold 2 h) → pagamento pelo provedor FALSO (webhook assinado) → aprovação AUTO
    await checkout_and_pay(db, client, adv, cid)
    assert (await camp(db, cid)).status in ("APROVADA", "AGENDADA", "SINCRONIZANDO", "ATIVA")
    # 11. manifesto da tela inclui a campanha
    m = (await db.execute(select(ManifestVersion).where(ManifestVersion.screen_id == uuid.UUID(scr["id"]))
                          .order_by(ManifestVersion.version.desc()).limit(1)
                          .execution_options(populate_existing=True))).scalar_one()
    assert uuid.UUID(cid) in m.campaign_ids, "manifesto da tela não inclui a campanha aprovada"
    # 12. o PLAYER pareado recebe o manifesto com o criativo EXTERNO da campanha
    r = await client.get("/api/v1/player/manifest", headers=dev.headers)
    assert r.status_code == 200, r.text
    man = r.json()["manifest"]
    assert man["version"] >= m.version
    ext = [c for c in man["creatives"].values() if c.get("kind") == "EXTERNAL"]
    assert cid in {c.get("campaign_id") for c in ext}
