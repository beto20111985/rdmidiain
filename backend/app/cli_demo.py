"""
RD Mídia Indoor — dados de DEMONSTRAÇÃO ponta a ponta (``python -m app.cli seed-demo``).

SOMENTE ``ENVIRONMENT=development``. Idempotente (reexecutar não duplica: cada passo
procura o que já existe por chaves determinísticas). Removível com ``--purge``.

Usa os SERVIÇOS/rotas dos domínios em processo (as mesmas funções que a API chama, com
contexto de usuário real carregado do RBAC) — nada de SQL cru para criar dados:
organização RD e 4 parceiros (distribuidora de bebidas em Goiânia-GO, supermercado em
São Paulo-SP, padaria em Manaus-AM, clínica odontológica em Rio Branco-AC) com
estabelecimentos nos fusos corretos, planos do parceiro, contrato de parceria com aceite
REGISTRADO COMO DEMONSTRAÇÃO, 2–3 telas por estabelecimento; 3 anunciantes (supermercado
— salgadinho e bebida —, academia, pizzaria) com segmentos; tabela de preços ATIVA, 2
planos e 1 cupom; mídias geradas (Pillow 1920×1080 / 1080×1920 e vídeo de 30 s via ffmpeg)
processadas pelo pipeline REAL até READY; criativos; conteúdo interno e fallback;
campanhas em RASCUNHO, EM_ANALISE e ATIVA (checkout real + webhook ASSINADO do provedor
FALSO de desenvolvimento + aprovação RD).

Transparência (nada de simulação apresentada como real):
- dinheiro: provedor de pagamento FALSO (``MOCK_PAYMENT``); o webhook é gerado e assinado
  pelo caminho de desenvolvimento do W-FIN (auditoria ``dev.webhook_simulated``);
- aceite de contrato: registro MANUAL rotulado "DEMONSTRAÇÃO" (sem assinatura real);
- ATIVA exige a confirmação (ack) de um player: o seed registra a aplicação do manifesto
  SEM dispositivo (``applied_by_device`` nulo) e audita ``seed.demo.player_ack_simulated``;
  a campanha fica ATIVA mas NÃO "reproduzindo" (não há prova de exibição);
- usuários de demonstração com senha ALEATÓRIA não impressa (``must_change_password``).

``--purge``: apaga o que pertence às organizações de demonstração (fecho por chaves
estrangeiras, conferido) e os itens compartilhados criados pelo seed que ninguém mais usa;
usuários de demonstração citados na auditoria são DESATIVADOS (a cadeia de auditoria é
imutável e não é tocada). Exige superusuário do PostgreSQL (atravessa os triggers
append-only com ``session_replication_role=replica``, apenas nesta transação).
"""
from __future__ import annotations

import io
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

DEMO = "DEMONSTRAÇÃO"
DEMO_PREFIX = f"{DEMO} — "
DEMO_EMAIL_LIKE = "demonstracao.%@example.com"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
PRICE_TABLE_NAME = f"{DEMO_PREFIX}Tabela de preços"
PLAN_NAMES = (f"{DEMO_PREFIX}Plano Mensal Essencial", f"{DEMO_PREFIX}Plano Avulso 7 dias")
COUPON_CODE = "DEMONSTRACAO10"
PARTNER_PLAN_NAMES = (f"{DEMO_PREFIX}Parceiro com TV própria", f"{DEMO_PREFIX}Parceiro com TV da RD (comodato)")
TEMPLATE_TITLE = f"{DEMO_PREFIX}Contrato de parceria (modelo de exemplo)"
MEDIA_PURPOSE = "DEMO_SEED"
CHECKOUT_KEYS = {"ativa": "seed-demo-academia-v1", "analise": "seed-demo-salgadinho-v1"}


def _out(msg: str) -> None:
    print(msg, flush=True)


def _email(local: str) -> str:
    return f"demonstracao.{local}@example.com"


# ─── organizações / locais (dados fixos ⇒ idempotência) ──────────────────────
# base de 12 posições do CNPJ — dígitos verificadores calculados (make_cnpj). Parceiros/RD usam o CNPJ
# ALFANUMÉRICO (IN RFB 2.229/2024); anunciantes (que pagam) usam CNPJ numérico fictício: o pagador do
# W-FIN ainda só aceita CNPJ numérico (achado registrado no relatório W-DB).
ORGS = {
    "rd": dict(base="DEMORD000001", name=f"{DEMO_PREFIX}RD Mídia Indoor", roles=["ADVERTISER"],
               city="Goiânia", state="GO", phone="6230000001"),
    "distribuidora": dict(base="DEMOPA000001", name=f"{DEMO_PREFIX}Distribuidora Cerrado Bebidas",
                          roles=["PARTNER"], city="Goiânia", state="GO", phone="6230000002"),
    "supermercado_sp": dict(base="DEMOPA000002", name=f"{DEMO_PREFIX}Supermercado Paulistano",
                            roles=["PARTNER"], city="São Paulo", state="SP", phone="1130000003"),
    "padaria": dict(base="DEMOPA000003", name=f"{DEMO_PREFIX}Padaria Encontro das Águas", roles=["PARTNER"],
                    city="Manaus", state="AM", phone="9230000004"),
    "clinica": dict(base="DEMOPA000004", name=f"{DEMO_PREFIX}Clínica Odontológica Acre Sorriso",
                    roles=["PARTNER"], city="Rio Branco", state="AC", phone="6830000005"),
    "anunc_super": dict(base="990000000101", name=f"{DEMO_PREFIX}Rede de Supermercados Bom Preço",
                        roles=["ADVERTISER"], city="Goiânia", state="GO", phone="6230000006"),
    "anunc_academia": dict(base="990000000102", name=f"{DEMO_PREFIX}Academia Movimento", roles=["ADVERTISER"],
                           city="Goiânia", state="GO", phone="6230000007"),
    "anunc_pizzaria": dict(base="990000000103", name=f"{DEMO_PREFIX}Pizzaria Forno a Lenha", roles=["ADVERTISER"],
                           city="São Paulo", state="SP", phone="1130000008"),
}

# estabelecimento: (org, nome, cidade, UF, fuso IANA, categoria (slug), segmentos, CEP, plano do parceiro, telas)
ESTABLISHMENTS = [
    dict(org="distribuidora", name=f"{DEMO_PREFIX}Distribuidora Cerrado (Setor Campinas)", city="Goiânia",
         state="GO", tz="America/Sao_Paulo", category="distribuidora-de-bebidas", segments=["bebidas"],
         postal_code="74515020", plan=0, lat=-16.6655, lng=-49.2860,
         screens=[("Balcão de pedidos", "HORIZONTAL"), ("Área de espera", "HORIZONTAL")]),
    dict(org="supermercado_sp", name=f"{DEMO_PREFIX}Supermercado Paulistano (Pinheiros)", city="São Paulo",
         state="SP", tz="America/Sao_Paulo", category="supermercado", segments=["alimentos", "higiene", "limpeza"],
         postal_code="05422010", plan=1, lat=-23.5670, lng=-46.6910,
         screens=[("Entrada", "HORIZONTAL"), ("Caixas", "HORIZONTAL"), ("Corredor de bebidas", "VERTICAL")]),
    dict(org="padaria", name=f"{DEMO_PREFIX}Padaria Encontro das Águas (Centro)", city="Manaus", state="AM",
         tz="America/Manaus", category="padaria", segments=["alimentos"], postal_code="69005010", plan=0,
         lat=-3.1316, lng=-60.0233, screens=[("Balcão", "HORIZONTAL"), ("Mesas", "VERTICAL")]),
    dict(org="clinica", name=f"{DEMO_PREFIX}Clínica Acre Sorriso (Bosque)", city="Rio Branco", state="AC",
         tz="America/Rio_Branco", category="clinica", segments=["higiene"], postal_code="69900000", plan=0,
         lat=-9.9750, lng=-67.8100, screens=[("Recepção", "HORIZONTAL"), ("Sala de espera", "HORIZONTAL")]),
]

ADVERTISER_PROFILES = {
    "anunc_super": dict(category="supermercado", segments=["alimentos", "bebidas"]),
    "anunc_academia": dict(category="academia", segments=[]),
    "anunc_pizzaria": dict(category="restaurante", segments=["delivery"]),
}

# criativos: chave → (org, nome, tipo, formato, segmentos do anúncio, texto)
CREATIVES = {
    "academia_video": ("anunc_academia", f"{DEMO_PREFIX}Academia Movimento — vídeo 30 s", "EXTERNAL", "video",
                       [], "Academia Movimento — matrícula com 1º mês grátis"),
    "salgadinho": ("anunc_super", f"{DEMO_PREFIX}Bom Preço — salgadinho crocante", "EXTERNAL", "vertical",
                   ["snacks"], "Salgadinho Crocante — leve 3 pague 2"),
    "bebida": ("anunc_super", f"{DEMO_PREFIX}Bom Preço — refrigerante gelado", "EXTERNAL", "horizontal",
               ["bebidas-nao-alcoolicas"], "Refrigerante 2 L gelado no Bom Preço"),
    "pizzaria": ("anunc_pizzaria", f"{DEMO_PREFIX}Forno a Lenha — rodízio de quinta", "EXTERNAL", "horizontal",
                 ["delivery"], "Rodízio de pizza às quintas — delivery"),
    "interno": ("rd", f"{DEMO_PREFIX}RD — conteúdo interno do estabelecimento", "INTERNAL", "horizontal", [],
                "Conteúdo interno do estabelecimento"),
    "fallback": ("rd", f"{DEMO_PREFIX}RD — tela padrão (fallback)", "FALLBACK", "horizontal", [],
                 "RD Mídia Indoor — sua marca aqui"),
}

CAMPAIGNS = {
    "rascunho": dict(org="anunc_pizzaria", name=f"{DEMO_PREFIX}Pizzaria — rodízio (rascunho)",
                     creative="pizzaria", categories=["alimentacao"], start_in=7, days=14, freq=2),
    "bebida": dict(org="anunc_super", name=f"{DEMO_PREFIX}Bom Preço — refrigerante (bloqueio na distribuidora)",
                   creative="bebida", categories=["alimentacao"], start_in=3, days=7, freq=2),
    "analise": dict(org="anunc_super", name=f"{DEMO_PREFIX}Bom Preço — salgadinho (em análise)",
                    creative="salgadinho", categories=["alimentacao", "saude"], start_in=1, days=7, freq=2,
                    coupon=COUPON_CODE),
    "ativa": dict(org="anunc_academia", name=f"{DEMO_PREFIX}Academia Movimento — matrícula (ativa)",
                  creative="academia_video", categories=["alimentacao", "saude"], start_in=0, days=30, freq=4),
}

CONTRACT_BODY = (
    "MODELO DE EXEMPLO PARA DEMONSTRAÇÃO — SEM VALIDADE JURÍDICA.\n\n"
    "Este texto existe apenas para demonstrar o fluxo de contrato de parceria da plataforma RD Mídia Indoor "
    "em ambiente de desenvolvimento: o estabelecimento parceiro cede espaço de exibição em suas telas; a RD "
    "comercializa o inventário e repassa a comissão conforme o plano do parceiro. O texto definitivo é "
    "definido pela RD e aceito pelo cliente no primeiro acesso."
)


@dataclass
class Ctx:
    rd: Any = None
    adv: dict = field(default_factory=dict)  # org key → ctx do usuário anunciante


# ─── utilidades ──────────────────────────────────────────────────────────────

def _today_sp() -> date:
    return datetime.now(timezone.utc).astimezone(SAO_PAULO).date()


async def _user_ctx(db, user):
    from app.core.authz import AuthContext, load_grants

    return AuthContext(kind="user", user=user, grants=await load_grants(db, user.id))


async def _ensure_user(db, local: str, name: str, role: str, scope_type: str = "GLOBAL", scope_id=None):
    from app.cli import _ensure_assignment
    from app.core.passwords import random_unusable_hash
    from app.models.users import User

    email = _email(local)
    u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if u is None:
        u = User(id=uuid.uuid4(), email=email, full_name=f"{DEMO_PREFIX}{name}", hashed_password=random_unusable_hash(),
                 is_active=True, is_verified=True, must_change_password=True)
        db.add(u)
        await db.flush()
    elif not u.is_active:  # reexecução depois de --purge (usuário desativado e sem perfis)
        u.is_active = True
        u.hashed_password = random_unusable_hash()
        u.must_change_password = True
    await _ensure_assignment(db, u.id, role, scope_type, scope_id, None)
    await db.commit()
    return u


def _png(width: int, height: int, title: str, subtitle: str, color: tuple) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), color)
    d = ImageDraw.Draw(img)

    def font(size: int):
        # A fonte embutida do Pillow não tem glifos latinos acentuados (Ç, Ã…); DejaVu tem.
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)

    big = font(max(60, width // 17))
    mid = font(max(36, width // 32))
    small = font(max(28, width // 48))
    d.rectangle([0, 0, width, height // 7], fill=(20, 20, 20))
    d.text((width // 2, height // 14), DEMO, font=big, fill=(255, 210, 0), anchor="mm")
    d.text((width // 2, height // 2), title, font=mid, fill=(255, 255, 255), anchor="mm")
    d.text((width // 2, height // 2 + height // 10), subtitle, font=small, fill=(235, 235, 235), anchor="mm")
    d.text((width // 2, height - height // 14), "Conteúdo de demonstração — RD Mídia Indoor (ambiente de desenvolvimento)",
           font=small, fill=(200, 200, 200), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _video_30s(title: str, subtitle: str) -> bytes:
    """MP4 H.264 1920×1080, 30 s exatos (900 quadros a 30 fps), sem áudio."""
    with tempfile.TemporaryDirectory(prefix="rd-demo-") as tmp:
        frame = Path(tmp) / "frame.png"
        frame.write_bytes(_png(1920, 1080, title, subtitle, (16, 94, 160)))
        out = Path(tmp) / "demo.mp4"
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "30",
               "-i", str(frame), "-frames:v", "900", "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "superfast",
               "-profile:v", "high", "-movflags", "+faststart", "-an", str(out)]
        subprocess.run(cmd, check=True, timeout=300)
        return out.read_bytes()


# ─── etapas ──────────────────────────────────────────────────────────────────

async def _taxonomy(db) -> dict:
    from app.modules.taxonomy import service as tax

    await tax.ensure_default_content_policies(db)
    ids = await tax.ensure_reference_taxonomy(db)
    await db.commit()
    return ids


async def _orgs(db, rd_ctx) -> dict:
    from app.api.v1.endpoints import organizations as org_api
    from app.models.organizations import Organization
    from app.modules.organizations.documents import make_cnpj

    out = {}
    for key, o in ORGS.items():
        doc = make_cnpj(o["base"])
        org = (await db.execute(select(Organization).where(Organization.document == doc))).scalar_one_or_none()
        if org is None:
            body = org_api.OrgCreateIn(
                name=o["name"], document=doc, document_type="CNPJ", business_roles=o["roles"],
                trade_name=o["name"].replace(DEMO_PREFIX, ""), responsible_name=f"{DEMO} Responsável",
                email=_email(f"org.{key}"), phone=o["phone"], city=o["city"], state=o["state"],
                notes="Organização de DEMONSTRAÇÃO criada por 'python -m app.cli seed-demo' (removível com --purge).")
            res = await org_api.create_organization(body=body, ctx=rd_ctx, db=db)
            org = await db.get(Organization, uuid.UUID(str(res["id"])))
        out[key] = org
    return out


async def _advertiser_profiles(db, rd_ctx, orgs: dict, tax_ids: dict) -> None:
    from app.api.v1.endpoints import organizations as org_api

    for key, p in ADVERTISER_PROFILES.items():
        body = org_api.AdvertiserProfileIn(category_id=tax_ids[p["category"]],
                                           segment_ids=[tax_ids[f"segment:{s}"] for s in p["segments"]])
        await org_api.set_advertiser_profile(org_id=orgs[key].id, body=body, ctx=rd_ctx, db=db)


async def _partner_plans(db, rd_ctx) -> list:
    from app.api.v1.endpoints import contracts as ctr_api
    from app.models.contracts import PartnerPlan

    plans = []
    specs = [
        dict(name=PARTNER_PLAN_NAMES[0], tv_provision="OWN_TV", deposit_required=False,
             installation_fee_required=False, upgrade_policy="NEGOTIATED", receives_commission=True,
             description="DEMONSTRAÇÃO: estabelecimento usa a própria TV; recebe comissão; sem mensalidade."),
        dict(name=PARTNER_PLAN_NAMES[1], tv_provision="RD_TV_COMODATO", default_tv_inches=43, deposit_required=True,
             deposit_cents=30000, installation_fee_required=True, installation_fee_cents=15000,
             upgrade_policy="PAID", upgrade_fee_cents=20000, receives_commission=True,
             description="DEMONSTRAÇÃO: TV da RD em comodato com caução e taxa de instalação (valores fictícios)."),
    ]
    for spec in specs:
        p = (await db.execute(select(PartnerPlan).where(PartnerPlan.name == spec["name"]))).scalar_one_or_none()
        if p is None:
            res = await ctr_api.create_plan(body=ctr_api.PlanIn(**spec), ctx=rd_ctx, db=db)
            p = await db.get(PartnerPlan, uuid.UUID(str(res["id"])))
        plans.append(p)
    return plans


async def _partnership_template(db, rd_ctx):
    from app.modules.contracts import service as ctr

    t = await ctr.published_template(db, "PARTNERSHIP")
    if t is None:
        t = await ctr.create_template(db, rd_ctx, "PARTNERSHIP", TEMPLATE_TITLE, CONTRACT_BODY)
        await ctr.publish_template(db, rd_ctx, t)
        await db.commit()
    return t


async def _establishments(db, rd_ctx, orgs: dict, plans: list, tax_ids: dict, template) -> list:
    from app.api.v1.endpoints import contracts as ctr_api
    from app.api.v1.endpoints import screens as scr_api
    from app.models.contracts import ContractAcceptance
    from app.models.establishments import Establishment
    from app.models.screens import Screen
    from app.modules.establishments import service as est_svc

    out = []
    for spec in ESTABLISHMENTS:
        org = orgs[spec["org"]]
        est = (await db.execute(select(Establishment).where(Establishment.org_id == org.id,
                                                            Establishment.name == spec["name"]))).scalar_one_or_none()
        if est is None:
            est = await est_svc.create_establishment(db, rd_ctx, {
                "org_id": org.id, "name": spec["name"], "city": spec["city"], "state": spec["state"],
                "timezone": spec["tz"], "category_id": tax_ids[spec["category"]],
                "segment_ids": [tax_ids[f"segment:{s}"] for s in spec["segments"]],
                "postal_code": spec["postal_code"], "lat": spec["lat"], "lng": spec["lng"], "geo_source": "MANUAL",
                "partner_plan_id": plans[spec["plan"]].id, "receives_commission": True,
                "email": _email(f"local.{spec['org']}"), "phone": ORGS[spec["org"]]["phone"],
                "responsible_name": f"{DEMO} Gerente do local",
                "notes": "Estabelecimento de DEMONSTRAÇÃO (seed-demo).",
            })
            await db.commit()
        for name, orientation in spec["screens"]:
            sname = f"{DEMO_PREFIX}{name}"
            exists = (await db.execute(select(Screen.id).where(Screen.establishment_id == est.id,
                                                               Screen.name == sname))).first()
            if exists is None:
                await scr_api.create_screen(body=scr_api.ScreenIn(
                    establishment_id=est.id, name=sname, orientation=orientation, inches=43 if orientation == "HORIZONTAL" else 50,
                    resolution_w=1920 if orientation == "HORIZONTAL" else 1080,
                    resolution_h=1080 if orientation == "HORIZONTAL" else 1920,
                    notes="Tela de DEMONSTRAÇÃO (sem player pareado)."), ctx=rd_ctx, db=db)
        accepted = (await db.execute(select(ContractAcceptance.id).where(
            ContractAcceptance.org_id == org.id, ContractAcceptance.establishment_id == est.id))).first()
        if accepted is None:
            await ctr_api.record_manual(body=ctr_api.ManualIn(
                template_id=template.id, org_id=org.id, establishment_id=est.id,
                signer_name=f"{DEMO} — Responsável pelo estabelecimento (sem assinatura real)",
                notes="Aceite registrado como DEMONSTRAÇÃO pelo seed-demo em ambiente de desenvolvimento; "
                      "não houve assinatura de pessoa real."), ctx=rd_ctx, db=db)
        await db.refresh(est)
        out.append(est)
    return out


async def _pricing(db, rd_ctx) -> dict:
    from app.api.v1.endpoints import pricing as pr_api
    from app.models.pricing import Coupon, Plan, PriceTable

    info: dict = {"table": None, "plans": [], "coupon": None, "reused_table": False}
    active = (await db.execute(select(PriceTable).where(PriceTable.status == "ATIVA"))).scalar_one_or_none()
    if active is not None and active.name != PRICE_TABLE_NAME:
        info.update(table=active, reused_table=True)  # nunca arquiva uma tabela real
    elif active is None:
        t = (await db.execute(select(PriceTable).where(PriceTable.name == PRICE_TABLE_NAME,
                                                       PriceTable.status == "RASCUNHO"))).scalar_one_or_none()
        if t is None:
            res = await pr_api.create_table(body=pr_api.TableIn(
                name=PRICE_TABLE_NAME, notes="Valores FICTÍCIOS de demonstração (centavos por inserção)."),
                ctx=rd_ctx, db=db)
            t = await db.get(PriceTable, uuid.UUID(str(res["id"])))
            items = [("DURATION_BASE", "30", "BASE_CENTS", 12), ("DURATION_BASE", "60", "BASE_CENTS", 22),
                     ("DURATION_BASE", "120", "BASE_CENTS", 40), ("SCREEN_CLASS", "PREMIUM", "MULTIPLIER_BPS", 13000),
                     ("EXCLUSIVITY", "COMPETITOR_EXCLUSIVE", "MULTIPLIER_BPS", 15000),
                     ("FEE", "ART", "FIXED_CENTS", 15000), ("COST_GATEWAY", "*", "PERCENT_BPS", 199),
                     ("TAX", "ISS", "PERCENT_BPS", 500)]
            for kind, key, mode, value in items:
                await pr_api.add_item(table_id=t.id, body=pr_api.ItemIn(
                    kind=kind, key=key, mode=mode, value=value, description=f"{DEMO}: valor fictício"),
                    ctx=rd_ctx, db=db)
        await pr_api.activate_table(table_id=t.id, ctx=rd_ctx, db=db)
        await db.commit()
        info["table"] = t
    else:
        info["table"] = active
    plan_specs = [
        dict(name=PLAN_NAMES[0], period_type="MENSAL", freq_value=2, screens_min=1, screens_max=20,
             description="DEMONSTRAÇÃO: plano mensal, 2 inserções/h."),
        dict(name=PLAN_NAMES[1], period_type="AVULSO", period_days=7, freq_value=4, screens_min=1, screens_max=10,
             description="DEMONSTRAÇÃO: 7 dias, 4 inserções/h."),
    ]
    for spec in plan_specs:
        p = (await db.execute(select(Plan).where(Plan.name == spec["name"]))).scalar_one_or_none()
        if p is None:
            res = await pr_api.create_plan(body=pr_api.PlanIn(kind="PREDEFINED", duration_s=30, **spec),
                                           ctx=rd_ctx, db=db)
            p = await db.get(Plan, uuid.UUID(str(res["id"])))
        if p.status != "ATIVO":
            await pr_api.activate_plan(plan_id=p.id, ctx=rd_ctx, db=db)
        await db.commit()
        info["plans"].append(p)
    c = (await db.execute(select(Coupon).where(Coupon.code == COUPON_CODE))).scalar_one_or_none()
    if c is None:
        now = datetime.now(timezone.utc)
        await pr_api.create_coupon(body=pr_api.CouponIn(
            code=COUPON_CODE, description=f"{DEMO}: 10% de desconto (cupom fictício)", discount_type="PERCENT",
            discount_bps=1000, valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=90),
            max_uses=100, max_uses_per_org=5), ctx=rd_ctx, db=db)
        await db.commit()
        c = (await db.execute(select(Coupon).where(Coupon.code == COUPON_CODE))).scalar_one()
    info["coupon"] = c
    return info


async def _creatives(db, ctxs: Ctx, orgs: dict, tax_ids: dict) -> dict:
    from app.models.campaigns import Creative
    from app.modules.campaigns import creatives as CR
    from app.modules.media import pipeline
    from app.modules.media import service as media_service

    out = {}
    for key, (org_key, name, kind, fmt, segs, ad_text) in CREATIVES.items():
        org = orgs[org_key]
        cr = (await db.execute(select(Creative).where(Creative.org_id == org.id, Creative.name == name,
                                                      Creative.archived_at.is_(None)))).scalar_one_or_none()
        if cr is None:
            ctx = ctxs.rd if org_key == "rd" else ctxs.adv[org_key]
            if fmt == "video":
                data, filename, ctype = _video_30s(ad_text, org.name.replace(DEMO_PREFIX, "")), f"{key}.mp4", "video/mp4"
            elif fmt == "vertical":
                data, filename, ctype = _png(1080, 1920, ad_text, org.name.replace(DEMO_PREFIX, ""), (160, 60, 20)), \
                    f"{key}.png", "image/png"
            else:
                data, filename, ctype = _png(1920, 1080, ad_text, org.name.replace(DEMO_PREFIX, ""), (30, 110, 60)), \
                    f"{key}.png", "image/png"
            m = await media_service.ingest_generated_file(db, ctx, org.id, filename, ctype, data, MEDIA_PURPOSE,
                                                          name=f"{DEMO_PREFIX}{key}")
            await db.commit()
            outcome = await pipeline.process_media(m.id)  # pipeline REAL (ffprobe/ffmpeg/Pillow) em processo
            await db.refresh(m)
            if m.status != "READY":
                raise SystemExit(f"Mídia de demonstração '{key}' não ficou READY ({m.status}: {m.status_code}; "
                                 f"resultado {getattr(outcome, 'result', outcome)}).")
            cr = await CR.create_creative(db, ctx, org_id=org.id, name=name, media_file_id=m.id, duration_seconds=30,
                                          kind=kind, ad_text=ad_text,
                                          segment_ids=[tax_ids[f"segment:{s}"] for s in segs])
            await db.commit()
        if kind in ("INTERNAL", "FALLBACK") and cr.status != "APROVADO":
            # revisão de conteúdo RD pelo serviço oficial (mesma regra da rota POST /creatives/{id}/review)
            await CR.review_creative(db, ctxs.rd, cr.id, approve=True)
            await db.commit()
        out[key] = cr
    return out


async def _internal_and_fallback(db, rd_ctx, ests: list, creatives: dict) -> None:
    from app.models.content import FallbackContent, InternalContent
    from app.modules.inventory import content as C

    for est in ests:
        if (await db.execute(select(InternalContent.id).where(InternalContent.establishment_id == est.id,
                                                              InternalContent.creative_id == creatives["interno"].id))
                ).first() is None:
            await C.add_internal(db, rd_ctx, establishment_id=est.id, creative_id=creatives["interno"].id)
            await db.commit()
        if (await db.execute(select(FallbackContent.id).where(FallbackContent.establishment_id == est.id,
                                                              FallbackContent.creative_id == creatives["fallback"].id))
                ).first() is None:
            await C.add_fallback(db, rd_ctx, scope="ESTABLISHMENT", establishment_id=est.id,
                                 creative_id=creatives["fallback"].id, purpose="RD_DEFAULT")
            await db.commit()


async def _pay_in_dev(db, rd_ctx, payment_order_id) -> str:
    """Webhook ASSINADO do provedor FALSO (mesmo caminho de /financial/dev/simulate-webhook) e processamento
    em processo (o que o worker faria)."""
    from app.core.audit import audit
    from app.core.config import settings
    from app.models.financial import PaymentOrder
    from app.modules.financial import cloudpaypag as cpp
    from app.modules.financial import payments

    cfg = cpp.get_config()
    if settings.ENVIRONMENT != "development" or cfg.mode != "fake":
        raise SystemExit("Pagamento de demonstração exige ENVIRONMENT=development e provedor FALSO (MOCK_PAYMENT).")
    o = await db.get(PaymentOrder, payment_order_id)
    raw, headers = cpp.FakeCloudPayPag(cfg).build_webhook(
        status="paid", external_ref=o.provider_external_ref, order_id=str(o.id), amount_cents=o.amount_cents,
        fee_cents=0, method=o.method)
    res = await payments.ingest_webhook(db, raw, headers, cfg.instance_slug)
    await audit(db, rd_ctx, "dev.webhook_simulated", "payment_order", o.id, org_id=o.org_id,
                after={"status": "paid", "fake": True, "source": "seed-demo"})
    await db.commit()
    if res.event_id is None:
        raise SystemExit(f"Webhook de demonstração recusado: {res.status_code} {res.body}")
    status = await payments.process_webhook_event(db, res.event_id)
    await db.commit()
    return status


async def _campaigns(db, ctxs: Ctx, orgs: dict, tax_ids: dict, creatives: dict) -> dict:
    from app.models.campaigns import Campaign
    from app.models.pricing import Order
    from app.modules.campaigns import checkout as CK
    from app.modules.campaigns import service as S

    today = _today_sp()
    out = {}
    for key in ("rascunho", "bebida", "analise", "ativa"):
        spec = CAMPAIGNS[key]
        org = orgs[spec["org"]]
        ctx = ctxs.adv[spec["org"]]
        c = (await db.execute(select(Campaign).where(Campaign.org_id == org.id,
                                                     Campaign.name == spec["name"]))).scalar_one_or_none()
        if c is None:
            start = today + timedelta(days=spec["start_in"])
            c = await S.create_campaign(db, ctx, org_id=org.id, data={
                "name": spec["name"], "description": "Campanha de DEMONSTRAÇÃO (seed-demo).",
                "period_type": "AVULSO", "start_date": start, "end_date": start + timedelta(days=spec["days"] - 1),
                "duration_s": 30, "freq_kind": "PER_HOUR", "freq_value": spec["freq"]},
                targets=[{"type": "CATEGORY", "value": str(tax_ids[cat]), "exclude": False}
                         for cat in spec["categories"]])
            await S.add_creative(db, ctx, c.id, creatives[spec["creative"]].id)
            await db.commit()
        if key in ("analise", "ativa") and c.status == "RASCUNHO":
            res = await CK.checkout(db, ctx, campaign_ids=[c.id], idempotency_key=CHECKOUT_KEYS[key],
                                    coupon_code=spec.get("coupon"), method="pix")
            await db.commit()
            if res.failed:
                raise SystemExit(f"Checkout de demonstração falhou (cobrança não criada): {res.response}")
            order = await db.get(Order, res.order.id)
            await _pay_in_dev(db, ctxs.rd, order.payment_order_id)
            await db.refresh(c)
        if key == "ativa" and c.status == "EM_ANALISE":
            await S.approve(db, ctxs.rd, c.id, note="Aprovado pela equipe RD (DEMONSTRAÇÃO).")
            await db.commit()
            await db.refresh(c)
        if key == "ativa" and c.status in ("APROVADA", "AGENDADA", "SINCRONIZANDO"):
            await _simulated_player_ack(db, ctxs.rd, c)
            await db.refresh(c)
        out[key] = c
    return out


async def _simulated_player_ack(db, rd_ctx, c) -> None:
    """ATIVA exige ack do player; sem player real o seed registra a aplicação SEM dispositivo e audita."""
    from app.core.audit import audit
    from app.models.inventory import BookingScreen
    from app.modules.inventory import service as inv

    screen_ids = sorted({r[0] for r in (await db.execute(
        select(BookingScreen.screen_id).where(BookingScreen.campaign_id == c.id, BookingScreen.status == "BOOKED")
    )).all()}, key=str)
    for sid in screen_ids:
        sm = await inv.current_manifest(db, sid)
        await db.commit()
        await inv.mark_manifest_applied(db, sid, sm.version, None)
        await audit(db, rd_ctx, "seed.demo.player_ack_simulated", "screen", sid, org_id=c.org_id,
                    after={"manifest_version": sm.version, "campaign_id": str(c.id), "device_id": None},
                    reason="DEMONSTRAÇÃO: confirmação de aplicação do manifesto SIMULADA (nenhum player real); "
                           "a campanha fica ATIVA mas não 'reproduzindo' (sem prova de exibição).")
        await db.commit()


async def _verify(db, rd_ctx, ests: list, creatives: dict, campaigns: dict) -> list[str]:
    from app.models.screens import Screen
    from app.modules.campaigns import service as S
    from app.modules.conflicts import service as CS
    from app.modules.inventory import service as inv

    lines = []
    dist = ests[0]
    d = await CS.evaluate_conflict(db, creatives["bebida"].id, dist.id, record=False, ctx=rd_ctx)
    lines.append(f"Conflito 'refrigerante' × distribuidora: {d.result} ({d.reason_code})")
    if d.result != "BLOCK":
        raise SystemExit("Verificação falhou: o anúncio de bebida deveria ser BLOQUEADO na distribuidora de bebidas.")
    res = await S.resolve_campaign(db, campaigns["bebida"])
    blocked = {str(k): getattr(v, "reason_code", None) for k, v in res.excluded_conflict_establishments.items()}
    lines.append(f"Campanha de bebida: {len(res.screens)} tela(s) elegível(is); distribuidora excluída "
                 f"({blocked.get(str(dist.id))})")
    if str(dist.id) not in blocked:
        raise SystemExit("Verificação falhou: a distribuidora não foi excluída da segmentação da campanha de bebida.")
    padaria = ests[2]
    sid = (await db.execute(select(Screen.id).where(Screen.establishment_id == padaria.id)
                            .order_by(Screen.name).limit(1))).scalar_one()
    sm = await inv.current_manifest(db, sid)
    await db.commit()
    m = sm.manifest
    by_key = {k: cr.get("code", "?") for k, cr in (m.get("creatives") or {}).items()}
    codes = sorted(by_key.values())
    active_code = creatives["academia_video"].code
    active_entries = sum(1 for entries in (m.get("external_plan") or {}).values() for e in entries
                         if by_key.get(e.get("c")) == active_code)
    lines.append(f"Manifesto {m.get('screen_code')} v{sm.version} ({m.get('timezone')}): criativos {codes}; "
                 f"{active_entries} entrada(s) do criativo {active_code} (campanha ATIVA) no plano externo do "
                 f"horizonte; conteúdo interno {len(m.get('internal_pattern') or [])}; fallback "
                 f"{len(m.get('fallback') or [])}; assinatura {sm.key_id}")
    if creatives["academia_video"].code not in codes:
        raise SystemExit("Verificação falhou: o criativo da campanha ATIVA não está no manifesto da tela demo.")
    return lines


async def cmd_seed_demo() -> None:
    from app.cli import _require_development
    from app.core.audit import audit
    from app.core.database import worker_session
    from app.core.rbac_sync import sync_catalog
    from app.modules.financial import common as fin_common
    from app.modules.media import service as media_service

    _require_development()
    # nada vai para o Celery (o worker pode estar com código antigo): tudo em processo
    media_service.set_enqueuer(lambda _media_id: None)
    fin_common.set_task_dispatcher(lambda _name, _args: None)
    try:
        await _seed_demo(worker_session, audit, sync_catalog)
    finally:
        media_service.set_enqueuer(None)
        fin_common.set_task_dispatcher(None)


async def _seed_demo(worker_session, audit, sync_catalog) -> None:
    async with worker_session() as db:
        await sync_catalog(db)
        await db.commit()
        tax_ids = await _taxonomy(db)
        ctxs = Ctx()
        rd_user = await _ensure_user(db, "rd.admin", "Equipe RD (admin)", "admin")
        ctxs.rd = await _user_ctx(db, rd_user)
        orgs = await _orgs(db, ctxs.rd)
        for key, o in ORGS.items():
            role = "advertiser" if "ADVERTISER" in o["roles"] and key != "rd" else "establishment_owner"
            if key == "rd":
                continue
            u = await _ensure_user(db, f"usuario.{key}", f"Usuário {o['name'].replace(DEMO_PREFIX, '')}", role,
                                   "ORGANIZATION", orgs[key].id)
            if role == "advertiser":
                ctxs.adv[key] = await _user_ctx(db, u)
        await _advertiser_profiles(db, ctxs.rd, orgs, tax_ids)
        plans = await _partner_plans(db, ctxs.rd)
        template = await _partnership_template(db, ctxs.rd)
        ests = await _establishments(db, ctxs.rd, orgs, plans, tax_ids, template)
        pricing = await _pricing(db, ctxs.rd)
        creatives = await _creatives(db, ctxs, orgs, tax_ids)
        await _internal_and_fallback(db, ctxs.rd, ests, creatives)
        campaigns = await _campaigns(db, ctxs, orgs, tax_ids, creatives)
        checks = await _verify(db, ctxs.rd, ests, creatives, campaigns)
        await audit(db, ctxs.rd, "seed.demo.completed", "seed", "demo",
                    after={"orgs": len(orgs), "establishments": len(ests),
                           "campaigns": {k: v.status for k, v in campaigns.items()}})
        await db.commit()
    await _drain()

    _out(f"{DEMO}: dados garantidos (idempotente). Nenhuma senha foi definida/impressa — "
         f"usuários '{DEMO_EMAIL_LIKE}' precisam de 'python -m app.cli set-password <email>' para login.")
    _out(f"  organizações: {len(orgs)} | estabelecimentos: {len(ests)} | tabela de preços: "
         f"{'reutilizada (já havia ATIVA)' if pricing['reused_table'] else pricing['table'].name} | "
         f"planos: {len(pricing['plans'])} | cupom: {pricing['coupon'].code}")
    for k, c in campaigns.items():
        _out(f"  campanha [{k}] {c.name.replace(DEMO_PREFIX, '')}: {c.status}")
    for line in checks:
        _out(f"  verificação: {line}")


async def _drain() -> None:
    try:
        from app.modules.notifications.service import drain

        await drain()
    except ImportError:
        pass
    from app.modules.financial.common import drain as fin_drain

    await fin_drain()


# ─── purge ───────────────────────────────────────────────────────────────────

KEEP_TABLES = {"audit_logs", "users", "roles", "permissions", "role_permissions", "alembic_version"}


async def _fk_graph(db) -> list[dict]:
    rows = (await db.execute(text("""
        SELECT c.conrelid::regclass::text AS tbl, c.confrelid::regclass::text AS ref, c.confdeltype AS del,
               ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(n, i)
                     JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.i) AS cols,
               ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(n, i)
                     JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.n ORDER BY k.i) AS refcols
          FROM pg_constraint c
         WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
    """))).mappings().all()
    return [dict(r) for r in rows]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _orphan_counts(db, graph: list[dict]) -> dict:
    out = {}
    for g in graph:
        if len(g["cols"]) != 1:
            continue
        n = (await db.execute(text(
            f"SELECT count(*) FROM {_q(g['tbl'])} t WHERE t.{_q(g['cols'][0])} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM {_q(g['ref'])} r WHERE r.{_q(g['refcols'][0])} = t.{_q(g['cols'][0])})"))).scalar()
        if n:
            out[f"{g['tbl']}.{g['cols'][0]}"] = n
    return out


async def cmd_seed_demo_purge() -> None:
    from app.cli import CLI_CTX, _require_development
    from app.core.audit import audit
    from app.core.database import worker_session
    from app.core.passwords import random_unusable_hash
    from app.core.storage import get_storage
    from app.modules.organizations.documents import make_cnpj

    _require_development()
    docs = [make_cnpj(o["base"]) for o in ORGS.values()]
    async with worker_session() as db:
        if not (await db.execute(text("SELECT current_setting('is_superuser') = 'on'"))).scalar():
            raise SystemExit("seed-demo --purge exige superusuário do PostgreSQL (atravessa triggers append-only "
                             "somente nesta transação). Rode com a conexão de superusuário de desenvolvimento.")
        org_ids = [r[0] for r in (await db.execute(text("SELECT id FROM organizations WHERE document = ANY(:d)"),
                                                   {"d": docs})).all()]
        user_ids = [r[0] for r in (await db.execute(text("SELECT id FROM users WHERE email LIKE :e"),
                                                    {"e": DEMO_EMAIL_LIKE})).all()]
        if not org_ids and not user_ids:
            _out(f"{DEMO}: nada a remover.")
            return
        await db.execute(text("SET LOCAL lock_timeout = '15s'"))
        await db.execute(text("SET LOCAL session_replication_role = replica"))
        await db.execute(text("CREATE TEMP TABLE purge_rows (tbl text NOT NULL, rid tid NOT NULL, "
                              "PRIMARY KEY (tbl, rid)) ON COMMIT DROP"))
        await db.execute(text("INSERT INTO purge_rows SELECT 'organizations', ctid FROM organizations "
                              "WHERE id = ANY(:ids)"), {"ids": org_ids})
        graph = await _fk_graph(db)
        orphans_before = await _orphan_counts(db, graph)  # referências já órfãs antes (não são desta remoção)
        # colunas de organização SEM chave estrangeira (ex.: ledger, PoP, eventos): também pertencem à demo
        fk_cols = {(g["tbl"], g["cols"][0]) for g in graph if len(g["cols"]) == 1}
        for tbl, col in (await db.execute(text(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                "AND data_type = 'uuid' AND column_name IN ('org_id', 'advertiser_org_id', 'owner_org_id', "
                "'scope_org_id')"))).all():
            if tbl in KEEP_TABLES or (tbl, col) in fk_cols:
                continue
            await db.execute(text(f"INSERT INTO purge_rows SELECT :t, ctid FROM {_q(tbl)} WHERE {_q(col)} = ANY(:ids) "
                                  "ON CONFLICT DO NOTHING"), {"t": tbl, "ids": org_ids})
        cascade = [g for g in graph if g["del"] != "n" and g["tbl"] not in KEEP_TABLES and g["ref"] not in {"users"}]
        while True:  # fecho: linhas que referenciam (FK sem SET NULL) linhas já marcadas
            added = 0
            for g in cascade:
                cols = ", ".join(f"t.{_q(c)}" for c in g["cols"])
                refcols = ", ".join(f"r.{_q(c)}" for c in g["refcols"])
                res = await db.execute(text(
                    f"INSERT INTO purge_rows SELECT :tbl, t.ctid FROM {_q(g['tbl'])} t "
                    f"WHERE ({cols}) IN (SELECT {refcols} FROM {_q(g['ref'])} r WHERE r.ctid IN "
                    f"(SELECT rid FROM purge_rows WHERE tbl = :ref)) ON CONFLICT DO NOTHING"),
                    {"tbl": g["tbl"], "ref": g["ref"]})
                added += res.rowcount or 0
            if not added:
                break
        # segurança: nada de outra organização entra no fecho
        foreign = []
        for (tbl,) in (await db.execute(text("SELECT DISTINCT tbl FROM purge_rows"))).all():
            has_org = (await db.execute(text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=:t "
                "AND column_name='org_id'"), {"t": tbl})).first()
            if has_org and tbl != "organizations":
                n = (await db.execute(text(
                    f"SELECT count(*) FROM {_q(tbl)} WHERE ctid IN (SELECT rid FROM purge_rows WHERE tbl=:t) "
                    f"AND org_id IS NOT NULL AND NOT (org_id = ANY(:ids))"), {"t": tbl, "ids": org_ids})).scalar()
                if n:
                    foreign.append(f"{tbl}={n}")
        if foreign:
            raise SystemExit(f"Recusado: o fecho de remoção alcança dados de OUTRAS organizações ({', '.join(foreign)}).")
        # FK com ON DELETE SET NULL vindas de linhas que ficam
        for g in graph:
            if g["del"] == "n" and len(g["cols"]) == 1 and g["ref"] != "users":
                await db.execute(text(
                    f"UPDATE {_q(g['tbl'])} t SET {_q(g['cols'][0])} = NULL WHERE t.{_q(g['cols'][0])} IN "
                    f"(SELECT r.{_q(g['refcols'][0])} FROM {_q(g['ref'])} r WHERE r.ctid IN "
                    f"(SELECT rid FROM purge_rows WHERE tbl = :ref)) AND t.ctid NOT IN "
                    f"(SELECT rid FROM purge_rows WHERE tbl = :tbl)"), {"tbl": g["tbl"], "ref": g["ref"]})
        # objetos de storage (mídias e PDFs de aceite) das linhas removidas
        keys = []
        if (await db.execute(text("SELECT 1 FROM purge_rows WHERE tbl='media_files' LIMIT 1"))).first():
            for row in (await db.execute(text(
                    "SELECT storage_key, preview_storage_key, source_storage_key FROM media_files "
                    "WHERE ctid IN (SELECT rid FROM purge_rows WHERE tbl='media_files')"))).all():
                keys += [k for k in row if k]
        if (await db.execute(text("SELECT 1 FROM purge_rows WHERE tbl='contract_acceptances' LIMIT 1"))).first():
            keys += [r[0] for r in (await db.execute(text(
                "SELECT pdf_storage_key FROM contract_acceptances WHERE pdf_storage_key IS NOT NULL AND ctid IN "
                "(SELECT rid FROM purge_rows WHERE tbl='contract_acceptances')"))).all()]
        counts = {t: n for t, n in (await db.execute(text(
            "SELECT tbl, count(*) FROM purge_rows GROUP BY tbl ORDER BY tbl"))).all()}
        for tbl in counts:
            await db.execute(text(f"DELETE FROM {_q(tbl)} WHERE ctid IN (SELECT rid FROM purge_rows WHERE tbl=:t)"),
                             {"t": tbl})
        # itens compartilhados criados pelo seed: removidos só se ninguém mais os usa
        shared = {
            "coupons": ("code = :v", COUPON_CODE),
            "plans": ("name = ANY(:v)", list(PLAN_NAMES)),
            "partner_plans": ("name = ANY(:v)", list(PARTNER_PLAN_NAMES)),
            "price_tables": ("name = :v", PRICE_TABLE_NAME),
            "contract_templates": ("title = :v", TEMPLATE_TITLE),
        }
        kept_shared = []
        for tbl, (cond, val) in shared.items():
            for (rid,) in (await db.execute(text(f"SELECT id FROM {tbl} WHERE {cond}"), {"v": val})).all():
                refs = 0
                for g in graph:
                    if g["ref"] == tbl and g["tbl"] not in (tbl, "price_items") and len(g["cols"]) == 1:
                        refs += (await db.execute(text(
                            f"SELECT count(*) FROM {_q(g['tbl'])} WHERE {_q(g['cols'][0])} = :id"), {"id": rid})).scalar()
                if refs:
                    kept_shared.append(f"{tbl}:{rid}")
                    continue
                if tbl == "price_tables":
                    await db.execute(text("DELETE FROM price_items WHERE table_id = :id"), {"id": rid})
                await db.execute(text(f"DELETE FROM {tbl} WHERE id = :id"), {"id": rid})
                counts[tbl] = counts.get(tbl, 0) + 1
        # usuários de demonstração: sem acesso; removidos só se a auditoria não os cita
        deact = deleted_users = 0
        for uid in user_ids:
            await db.execute(text("DELETE FROM role_assignments WHERE user_id = :u"), {"u": uid})
            await db.execute(text("DELETE FROM refresh_tokens WHERE session_id IN "
                                  "(SELECT id FROM user_sessions WHERE user_id = :u)"), {"u": uid})
            await db.execute(text("DELETE FROM user_sessions WHERE user_id = :u"), {"u": uid})
            await db.execute(text("DELETE FROM user_recovery_codes WHERE user_id = :u"), {"u": uid})
            await db.execute(text("DELETE FROM password_reset_codes WHERE user_id = :u"), {"u": uid})
            in_audit = (await db.execute(text("SELECT 1 FROM audit_logs WHERE actor_user_id = :u OR real_user_id = :u "
                                              "LIMIT 1"), {"u": uid})).first()
            other_refs = 0
            for g in graph:
                if g["ref"] == "users" and g["tbl"] not in ("audit_logs",) and len(g["cols"]) == 1:
                    other_refs += (await db.execute(text(
                        f"SELECT count(*) FROM {_q(g['tbl'])} WHERE {_q(g['cols'][0])} = :u"), {"u": uid})).scalar()
            if in_audit or other_refs:
                await db.execute(text("UPDATE users SET is_active = false, hashed_password = :h, "
                                      "must_change_password = true WHERE id = :u"),
                                 {"h": random_unusable_hash(), "u": uid})
                deact += 1
            else:
                await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
                deleted_users += 1
        await db.execute(text("SET LOCAL session_replication_role = origin"))
        # integridade: a remoção não cria NENHUMA referência órfã nova (senão desfaz tudo)
        after = await _orphan_counts(db, graph)
        orphans = [f"{k}={n}" for k, n in after.items() if n > orphans_before.get(k, 0)]
        if orphans:
            raise SystemExit(f"Remoção desfeita: deixaria referências órfãs ({', '.join(orphans)}).")
        await audit(db, CLI_CTX, "seed.demo.purged", "seed", "demo",
                    after={"rows": counts, "users_deactivated": deact, "users_deleted": deleted_users,
                           "shared_kept": kept_shared, "storage_objects": len(keys)})
        await db.commit()
    storage = get_storage()
    removed = 0
    for k in keys:
        try:
            if await storage.delete(k):
                removed += 1
        except Exception:  # objeto já ausente / chave inválida: segue
            pass
    _out(f"{DEMO}: removido — {sum(counts.values())} linha(s) em {len(counts)} tabela(s); "
         f"{removed} objeto(s) de storage; usuários desativados {deact}, apagados {deleted_users}"
         + (f"; compartilhados mantidos (em uso): {kept_shared}" if kept_shared else "")
         + ". A auditoria (append-only) foi preservada.")
