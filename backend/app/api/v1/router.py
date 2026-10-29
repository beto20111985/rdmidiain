"""
RD Mídia Indoor — API v1 Router principal.

Organizado por SEÇÕES DE DOMÍNIO. Cada agente adiciona/troca linhas apenas na sua
seção (não reordenar as demais). Rotas novas usam ``app.core.authz.require``.
"""
from fastapi import APIRouter

api_router = APIRouter()

# ─── FUNDAÇÃO: AUTH / IDENTIDADE / USUÁRIOS / AUDITORIA / CONFIG / LGPD (W1) ─
from app.api.v1.endpoints.auth import router as auth_router  # noqa: E402
from app.api.v1.endpoints.me import router as me_router  # noqa: E402
from app.api.v1.endpoints.users import roles_router, router as users_router  # noqa: E402
from app.api.v1.endpoints.audit import router as audit_router  # noqa: E402
from app.api.v1.endpoints.settings import router as settings_router  # noqa: E402
from app.api.v1.endpoints.lgpd import router as lgpd_router  # noqa: E402
from app.api.v1.endpoints.realtime import router as realtime_router  # noqa: E402

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(me_router, prefix="/me", tags=["me"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(roles_router, tags=["rbac"])
api_router.include_router(audit_router, prefix="/audit", tags=["audit"])
api_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_router.include_router(lgpd_router, prefix="/lgpd", tags=["lgpd"])
api_router.include_router(realtime_router, prefix="/realtime", tags=["realtime"])

# ─── SEGURANÇA DA INSTÂNCIA (AÇÃO 003 — modo de aprovação, assentos de Master, integridade) ───
from app.api.v1.endpoints.security import router as security_router  # noqa: E402

api_router.include_router(security_router, prefix="/security", tags=["security"])

# ─── DOMÍNIO: ORGANIZAÇÕES / ESTABELECIMENTOS / TELAS / DISPOSITIVOS ─────────
from app.api.v1.endpoints.organizations import router as orgs_router  # noqa: E402
from app.api.v1.endpoints.establishments import router as estabs_router  # noqa: E402
from app.api.v1.endpoints.screens import router as screens_router  # noqa: E402
from app.api.v1.endpoints.devices import router as devices_router  # noqa: E402

api_router.include_router(orgs_router, prefix="/organizations", tags=["organizations"])
api_router.include_router(estabs_router, prefix="/establishments", tags=["establishments"])
api_router.include_router(screens_router, prefix="/screens", tags=["screens"])
api_router.include_router(devices_router, prefix="/devices", tags=["devices"])

# W-ORG: taxonomia, catálogo de disponibilidade, contratos/planos/comodato, onboarding, instalações, manutenção
from app.api.v1.endpoints.screens import catalog_router  # noqa: E402
from app.api.v1.endpoints.taxonomy import router as taxonomy_router  # noqa: E402
from app.api.v1.endpoints.contracts import (  # noqa: E402
    comodato_router,
    plans_router,
    router as contracts_router,
)
from app.api.v1.endpoints.onboarding import router as onboarding_router  # noqa: E402
from app.api.v1.endpoints.advertiser_signup import router as advertiser_signup_router  # noqa: E402
from app.api.v1.endpoints.installations import (  # noqa: E402
    maintenance_router,
    router as installations_router,
)

api_router.include_router(catalog_router, prefix="/catalog", tags=["catalog"])
api_router.include_router(taxonomy_router, prefix="/taxonomy", tags=["taxonomy"])
api_router.include_router(contracts_router, prefix="/contracts", tags=["contracts"])
api_router.include_router(plans_router, prefix="/partner-plans", tags=["partner-plans"])
api_router.include_router(comodato_router, prefix="/comodato-agreements", tags=["comodato"])
api_router.include_router(onboarding_router, prefix="/onboarding", tags=["onboarding"])
api_router.include_router(advertiser_signup_router, prefix="/advertiser-signup", tags=["advertiser-signup"])
api_router.include_router(installations_router, prefix="/installations", tags=["installations"])
api_router.include_router(maintenance_router, prefix="/maintenance", tags=["maintenance"])

# ─── DOMÍNIO: MÍDIA ──────────────────────────────────────────────────────────
from app.api.v1.endpoints.media import router as media_router  # noqa: E402

api_router.include_router(media_router, prefix="/media", tags=["media"])

# ─── DOMÍNIO: CAMPANHAS / INVENTÁRIO / SCHEDULER ─────────────────────────────
from app.api.v1.endpoints.campaigns import creatives_router, router as campaigns_router  # noqa: E402  (W-CAMP)
from app.api.v1.endpoints.pricing import router as pricing_router  # noqa: E402  (W-CAMP)
from app.api.v1.endpoints.conflicts import router as conflicts_router  # noqa: E402  (W-CAMP)
from app.api.v1.endpoints.checkout import router as checkout_router  # noqa: E402  (W-CAMP)
from app.api.v1.endpoints.inventory import router as inventory_router  # noqa: E402
from app.api.v1.endpoints.scheduler import router as scheduler_router  # noqa: E402
from app.api.v1.endpoints.internal_content import router as internal_content_router  # noqa: E402  (W-INV)

api_router.include_router(campaigns_router, prefix="/campaigns", tags=["campaigns"])
api_router.include_router(creatives_router, prefix="/creatives", tags=["creatives"])
api_router.include_router(pricing_router, prefix="/pricing", tags=["pricing"])
api_router.include_router(conflicts_router, prefix="/conflicts", tags=["conflicts"])
api_router.include_router(checkout_router, prefix="/checkout", tags=["checkout"])
api_router.include_router(inventory_router, prefix="/inventory", tags=["inventory"])
api_router.include_router(scheduler_router, prefix="/scheduler", tags=["scheduler"])
api_router.include_router(internal_content_router, prefix="/internal-content", tags=["internal-content"])

# ─── DOMÍNIO: PLAYBACK / PROOF-OF-PLAY / PLAYER (W-PLAY) ─────────────────────
from app.api.v1.endpoints.playback import router as playback_router  # noqa: E402
from app.api.v1.endpoints.player import router as player_router  # noqa: E402
from app.api.v1.endpoints.devices import screen_router as device_screen_router  # noqa: E402

api_router.include_router(playback_router, prefix="/playback", tags=["playback"])
api_router.include_router(player_router, prefix="/player", tags=["player"])
# /screens/{id}/pairing|presence|commands (rotas de dispositivo por tela — W-PLAY)
api_router.include_router(device_screen_router, tags=["devices"])
# rota estática /screens/presence (lote) precisa vir ANTES de /screens/{screen_id} (W-ORG)
for _r in [r for r in api_router.routes if getattr(r, "path", "") == "/screens/presence"]:
    api_router.routes.remove(_r)
    api_router.routes.insert(0, _r)

# ─── DOMÍNIO: FINANCEIRO / PAGAMENTOS ────────────────────────────────────────
from app.api.v1.endpoints.financial import router as financial_router  # noqa: E402
from app.api.v1.endpoints.withdrawals import router as withdrawals_router  # noqa: E402
from app.api.v1.endpoints.approvals import router as approvals_router  # noqa: E402
from app.api.v1.endpoints.accounting import router as accounting_router  # noqa: E402
from app.api.v1.endpoints.webhooks import router as webhooks_router  # noqa: E402

api_router.include_router(financial_router, prefix="/financial", tags=["financial"])
api_router.include_router(withdrawals_router, prefix="/financial", tags=["financial-withdrawals"])
api_router.include_router(approvals_router, prefix="/financial/approvals", tags=["financial-approvals"])
api_router.include_router(accounting_router, prefix="/financial", tags=["financial-accounting"])
api_router.include_router(webhooks_router, prefix="/webhooks", tags=["webhooks"])

# ─── DOMÍNIO: CRM / DESIGNER / SUPORTE / RELATÓRIOS / ESTOQUE ────────────────
from app.api.v1.endpoints.crm import router as crm_router  # noqa: E402
from app.api.v1.endpoints.support import router as support_router  # noqa: E402
from app.api.v1.endpoints.reports import router as reports_router  # noqa: E402
from app.api.v1.endpoints.equipment import router as equipment_router  # noqa: E402

api_router.include_router(crm_router, prefix="/crm", tags=["crm"])
api_router.include_router(support_router, prefix="/support", tags=["support"])
api_router.include_router(reports_router, prefix="/reports", tags=["reports"])
api_router.include_router(equipment_router, prefix="/equipment", tags=["equipment"])
from app.api.v1.endpoints.designer import router as designer_router  # noqa: E402

api_router.include_router(designer_router, prefix="/design", tags=["design"])
from app.api.v1.endpoints.templates import router as templates_router  # noqa: E402

api_router.include_router(templates_router, prefix="/templates", tags=["templates"])
from app.api.v1.endpoints.dashboards import router as dashboards_router  # noqa: E402

api_router.include_router(dashboards_router, prefix="/dashboards", tags=["dashboards"])

# ─── DOMÍNIO: NOTIFICAÇÕES ───────────────────────────────────────────────────
from app.api.v1.endpoints.notifications import router as notif_router  # noqa: E402

api_router.include_router(notif_router, prefix="/notifications", tags=["notifications"])

# ─── DOMÍNIO: CONTROL PLANE / WHITE-LABEL ────────────────────────────────────
from app.api.v1.endpoints.control_plane import router as cp_router  # noqa: E402

api_router.include_router(cp_router, prefix="/control-plane", tags=["control-plane"])
from app.api.v1.endpoints.instance import router as instance_router  # noqa: E402

api_router.include_router(instance_router, prefix="/instance", tags=["instance"])
