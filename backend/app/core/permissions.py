"""
RD Mídia Indoor — Catálogo de permissões e perfis padrão (contrato §3.1 e §3.3).

Fonte ÚNICA em código. ``sync_catalog(db)`` sincroniza com o banco de forma
idempotente (CLI ``python -m app.cli sync-permissions``; também chamado no seed e
na migration 0003):
- cria/atualiza ``permissions`` (código, nome, módulo); códigos que saíram do
  catálogo são removidos (e de todos os perfis);
- cria/atualiza os perfis de sistema (``roles.is_system=True``) com exatamente as
  permissões definidas aqui. Perfis personalizados (``is_system=False``) não são
  tocados, exceto pela remoção de códigos que deixaram de existir.

Para adicionar uma permissão: inclua em ``CATALOG`` (e nos perfis adequados) e rode
``sync-permissions``. O teste ``tests/unit/test_permissions_catalog.py`` garante
que toda permissão usada por rotas existe aqui.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# ─── CATÁLOGO (§3.1) ─────────────────────────────────────────────────────────
CATALOG: dict[str, str] = {
    "dashboard.view": "Ver painel",
    "organizations.view": "Ver organizações",
    "organizations.manage": "Gerenciar organizações",
    "users.view": "Ver usuários",
    "users.manage": "Gerenciar usuários e atribuições",
    "users.impersonate": "Personificar usuário (impersonation)",
    "roles.manage": "Gerenciar perfis personalizados",
    "establishments.view": "Ver estabelecimentos",
    "establishments.manage": "Gerenciar estabelecimentos",
    "screens.view": "Ver telas",
    "screens.manage": "Gerenciar telas",
    "screens.pair": "Parear dispositivos",
    "screens.command": "Enviar comandos às telas",
    "media.view": "Ver mídias",
    "media.upload": "Enviar mídias",
    "media.publish": "Publicar mídias",
    "media.delete": "Excluir mídias",
    "playlists.view": "Ver playlists",
    "playlists.reorder": "Reordenar playlist",
    "playlists.manage": "Gerenciar playlists",
    "campaigns.view": "Ver campanhas",
    "campaigns.manage": "Criar/editar campanhas",
    "campaigns.approve": "Aprovar/rejeitar campanhas",
    "campaigns.kill": "Interromper campanha imediatamente",
    "creatives.view": "Ver criativos",
    "creatives.manage": "Gerenciar criativos",
    "taxonomy.manage": "Gerenciar categorias/segmentos/concorrência",
    "conflicts.override": "Liberar exceção de conflito de concorrência",
    "inventory.view": "Ver inventário",
    "inventory.reserve": "Reservar inventário",
    "pricing.view": "Ver preços",
    "pricing.manage": "Gerenciar preços e planos",
    "checkout.create": "Iniciar checkout/pagamento",
    "payments.view": "Ver pagamentos",
    "finance.view": "Ver financeiro",
    "finance.manage": "Operar financeiro",
    "finance.approve": "Aprovar operação financeira (dupla aprovação)",
    "finance.reconcile": "Conciliar financeiro",
    "commissions.view": "Ver comissões",
    "commissions.adjust": "Ajustar comissões",
    "withdrawals.view": "Ver saques",
    "withdrawals.request": "Solicitar saque",
    "pix_keys.manage": "Alterar chave PIX",
    "crm.view": "Ver CRM",
    "crm.manage": "Gerenciar CRM",
    "crm.discount_approve": "Aprovar desconto comercial",
    "design.view": "Ver solicitações de design",
    "design.work": "Executar trabalhos de design",
    "design.request": "Solicitar design",
    "design.approve": "Aprovar arte",
    "templates.manage": "Gerenciar templates",
    "support.view": "Ver suporte",
    "support.create": "Abrir chamado",
    "support.manage": "Gerenciar chamados",
    "equipment.view": "Ver estoque/equipamentos",
    "equipment.manage": "Gerenciar estoque/equipamentos",
    "maintenance.manage": "Gerenciar manutenções",
    "reports.view": "Ver relatórios",
    "reports.export": "Exportar relatórios",
    "reports.financial": "Ver relatórios financeiros",
    "notifications.manage": "Gerenciar notificações",
    "settings.view": "Ver configurações",
    "settings.manage": "Alterar configurações",
    "audit.view": "Ver auditoria",
    "audit.export": "Exportar auditoria",
    "control_plane.view": "Ver control plane",
    "control_plane.manage": "Operar control plane",
    "whitelabel.manage": "Gerenciar white-label",
    "lgpd.manage": "Processar solicitações LGPD",
}
ALL_PERMISSIONS: frozenset[str] = frozenset(CATALOG)

# Permissões bloqueadas durante impersonation (ações financeiras/segurança — A7 P1)
IMPERSONATION_BLOCKED: frozenset[str] = frozenset(
    {
        "users.impersonate",
        "users.manage",
        "roles.manage",
        "finance.manage",
        "finance.approve",
        "finance.reconcile",
        "commissions.adjust",
        "withdrawals.request",
        "pix_keys.manage",
        "settings.manage",
        "lgpd.manage",
        "whitelabel.manage",
        "control_plane.manage",
        "audit.export",
    }
)

# Somente efetiva para os dois Admin Master (contrato §3.1)
ADMIN_MASTER_ONLY: frozenset[str] = frozenset({"finance.approve"})

SCOPE_GLOBAL = "GLOBAL"
SCOPE_ORGANIZATION = "ORGANIZATION"
SCOPE_ESTABLISHMENT = "ESTABLISHMENT"
SCOPE_CITY = "CITY"
SCOPE_TYPES = (SCOPE_GLOBAL, SCOPE_ORGANIZATION, SCOPE_ESTABLISHMENT, SCOPE_CITY)


@dataclass(frozen=True)
class RoleTemplate:
    slug: str
    name: str
    description: str
    permissions: frozenset[str]
    typical_scopes: tuple[str, ...]
    mfa_required: bool = False
    rd_staff: bool = False  # equipe da RD (contexto "rd" no /me)
    context_kind: str | None = None  # ESTABLISHMENT | ADVERTISER para perfis de cliente


def _p(*codes: str) -> frozenset[str]:
    return frozenset(codes)


_ADVERTISER = _p(
    "dashboard.view",
    "campaigns.view",
    "campaigns.manage",
    "creatives.view",
    "creatives.manage",
    "media.view",
    "media.upload",
    "media.delete",  # decisão do coordenador (AÇÃO 002): anunciante exclui a própria mídia sem referência
    "checkout.create",
    "payments.view",
    "design.request",
    "design.approve",
    "reports.view",
    "support.view",
    "support.create",
    "inventory.view",
)

_ADMIN = ALL_PERMISSIONS - _p(
    "finance.approve",
    "finance.manage",
    "finance.reconcile",
    "commissions.adjust",
    "withdrawals.request",
    "users.impersonate",
    "whitelabel.manage",
    "control_plane.manage",
    "lgpd.manage",
    "audit.export",
)

ROLE_TEMPLATES: dict[str, RoleTemplate] = {
    t.slug: t
    for t in [
        RoleTemplate(
            "super_admin",
            "Super Admin",
            "Acesso total. Exclusivo dos Administradores Master (assentos 1 e 2): concedido somente pelas "
            "funções de assento (bootstrap/convite), nunca pela API.",
            ALL_PERMISSIONS,
            (SCOPE_GLOBAL,),
            mfa_required=True,
            rd_staff=True,
        ),
        RoleTemplate(
            "admin",
            "Admin",
            "Operação completa da plataforma; vê o financeiro, sem aprovar/operar dinheiro.",
            _ADMIN,
            (SCOPE_GLOBAL,),
            mfa_required=True,
            rd_staff=True,
        ),
        RoleTemplate(
            "employee",
            "Funcionário",
            "Operação (estabelecimentos, telas, mídia, campanhas, suporte) sem financeiro e sem ações "
            "remotas no player.",
            _p(
                "dashboard.view",
                "organizations.view",
                "establishments.view",
                "establishments.manage",
                "screens.view",
                "screens.manage",
                "screens.pair",
                # screens.command NÃO: ações remotas no player (reiniciar app/dispositivo, ressincronizar,
                # limpar cache, baixar novamente, atualizar versão, PIN técnico) são sensíveis e só
                # admin/técnico (E2 #1386–#1391; REQ-RBAC-006).
                "media.view",
                "media.upload",
                "media.publish",
                "playlists.view",
                "playlists.reorder",
                "playlists.manage",
                "campaigns.view",
                "campaigns.manage",
                "creatives.view",
                "creatives.manage",
                "inventory.view",
                "inventory.reserve",
                "support.view",
                "support.create",
                "support.manage",
                "equipment.view",
                "reports.view",
            ),
            (SCOPE_GLOBAL, SCOPE_CITY),
            rd_staff=True,
        ),
        RoleTemplate(
            "financial",
            "Financeiro",
            "Financeiro (exceto aprovação), pagamentos, comissões, saques (ver) e relatórios financeiros.",
            _p(
                "dashboard.view",
                "organizations.view",
                "finance.view",
                "finance.manage",
                "finance.reconcile",
                "payments.view",
                "commissions.view",
                "commissions.adjust",
                "withdrawals.view",
                "reports.view",
                "reports.export",
                "reports.financial",
            ),
            (SCOPE_GLOBAL,),
            mfa_required=True,
            rd_staff=True,
        ),
        RoleTemplate(
            "designer",
            "Designer",
            "Fila de design, templates e mídia.",
            _p("dashboard.view", "design.view", "design.work", "templates.manage", "media.view", "media.upload"),
            (SCOPE_GLOBAL,),
            rd_staff=True,
        ),
        RoleTemplate(
            "sales",
            "Comercial",
            "CRM (sem aprovar desconto), preços (ver) e organizações.",
            _p(
                "dashboard.view",
                "crm.view",
                "crm.manage",
                "pricing.view",
                "organizations.view",
                "organizations.manage",
                "establishments.view",
                "inventory.view",
            ),
            (SCOPE_GLOBAL, SCOPE_CITY),
            rd_staff=True,
        ),
        RoleTemplate(
            "technician",
            "Técnico",
            "Equipamentos, manutenção, pareamento de telas, ações remotas no player e suporte.",
            _p(
                "dashboard.view",
                "equipment.view",
                "equipment.manage",
                "maintenance.manage",
                "screens.view",
                "screens.pair",
                "screens.command",  # ações remotas no player (E2 #1386–#1391; REQ-RBAC-006)
                "support.view",
                "support.manage",
                "establishments.view",
            ),
            (SCOPE_GLOBAL, SCOPE_CITY),
            rd_staff=True,
        ),
        RoleTemplate(
            "establishment_owner",
            "Estabelecimento",
            "Dono/gestor de estabelecimento parceiro.",
            _p(
                "dashboard.view",
                "establishments.view",
                "screens.view",
                "playlists.view",
                "playlists.reorder",
                "commissions.view",
                "withdrawals.view",
                "withdrawals.request",
                "reports.view",
                "support.view",
                "support.create",
                "design.request",  # ETAPA2 #1425–#1433 (arte para conteúdo interno)
                "design.approve",
            ),
            (SCOPE_ORGANIZATION, SCOPE_ESTABLISHMENT),
            context_kind="ESTABLISHMENT",
        ),
        RoleTemplate(
            "advertiser",
            "Anunciante",
            "Cliente anunciante: campanhas, criativos, checkout e relatórios da própria organização.",
            _ADVERTISER,
            (SCOPE_ORGANIZATION,),
            context_kind="ADVERTISER",
        ),
        RoleTemplate(
            "agency",
            "Agência",
            "Igual ao anunciante, em cada organização cliente atribuída.",
            _ADVERTISER,
            (SCOPE_ORGANIZATION,),
            context_kind="ADVERTISER",
        ),
    ]
}

MFA_REQUIRED_ROLES: frozenset[str] = frozenset(s for s, t in ROLE_TEMPLATES.items() if t.mfa_required)
# Ações remotas no player (E2 #1386–#1391): somente admin/técnico (além do super_admin).
REMOTE_PLAYER_ACTION_PERMISSIONS: frozenset[str] = frozenset({"screens.command"})
REMOTE_PLAYER_ACTION_ROLES: frozenset[str] = frozenset({"super_admin", "admin", "technician"})

# Perfis de CLIENTE: quem tem users.manage em escopo GLOBAL pode concedê-los, somente em
# escopo ORGANIZATION/ESTABLISHMENT (decisão do coordenador, AÇÃO 002).
CLIENT_ROLES: frozenset[str] = frozenset(s for s, t in ROLE_TEMPLATES.items() if t.context_kind is not None)
RD_STAFF_ROLES: frozenset[str] = frozenset(s for s, t in ROLE_TEMPLATES.items() if t.rd_staff)


def is_valid_permission(code: str) -> bool:
    return code in ALL_PERMISSIONS


def assert_valid(codes: Iterable[str]) -> None:
    bad = sorted(set(codes) - ALL_PERMISSIONS)
    if bad:
        raise ValueError(f"Permissões fora do catálogo: {bad}")


# Mapa de códigos legados (rotas antigas "recurso:acao") → catálogo. Usado só pelo
# shim de compatibilidade ``app.modules.auth.deps.require_permission`` enquanto os
# agentes de domínio reescrevem as rotas; o shim exige escopo GLOBAL (sem vazamento
# entre organizações).
LEGACY_PERMISSION_MAP: dict[str, str] = {
    "users:list": "users.view",
    "users:read": "users.view",
    "users:create": "users.manage",
    "users:update": "users.manage",
    "users:delete": "users.manage",
    "orgs:list": "organizations.view",
    "orgs:read": "organizations.view",
    "orgs:create": "organizations.manage",
    "orgs:update": "organizations.manage",
    "orgs:delete": "organizations.manage",
    "establishments:list": "establishments.view",
    "establishments:read": "establishments.view",
    "establishments:create": "establishments.manage",
    "establishments:update": "establishments.manage",
    "establishments:delete": "establishments.manage",
    "screens:list": "screens.view",
    "screens:read": "screens.view",
    "screens:create": "screens.manage",
    "screens:update": "screens.manage",
    "screens:manage": "screens.manage",
    "screens:command": "screens.command",
    "devices:list": "screens.view",
    "devices:pair": "screens.pair",
    "devices:revoke": "screens.manage",
    "media:list": "media.view",
    "media:read": "media.view",
    "media:upload": "media.upload",
    "media:delete": "media.delete",
    "campaigns:list": "campaigns.view",
    "campaigns:read": "campaigns.view",
    "campaigns:create": "campaigns.manage",
    "campaigns:update": "campaigns.manage",
    "campaigns:submit": "campaigns.manage",
    "campaigns:approve": "campaigns.approve",
    "campaigns:checkout": "checkout.create",
    "inventory:read": "inventory.view",
    "inventory:reserve": "inventory.reserve",
    "scheduler:manage": "screens.manage",
    "playback:read": "reports.view",
    "financial:list": "finance.view",
    "financial:ledger": "finance.view",
    "financial:balance": "finance.view",
    "financial:create_payment": "finance.manage",
    "financial:payout": "finance.manage",
    "financial:calculate": "pricing.view",
    "equipment:read": "equipment.view",
    "equipment:write": "equipment.manage",
    "crm:read": "crm.view",
    "crm:write": "crm.manage",
    "support:manage": "support.manage",
    "reports:read": "reports.view",
    "reports:financial": "reports.financial",
    "notifications:write": "notifications.manage",
}

assert_valid(LEGACY_PERMISSION_MAP.values())
for _t in ROLE_TEMPLATES.values():
    assert_valid(_t.permissions)


def module_of(code: str) -> str:
    return code.split(".", 1)[0]
