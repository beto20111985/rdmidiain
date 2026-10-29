"""Catálogo de permissões × rotas × perfis (contrato §3.1/§3.3)."""
import re

from app.core import permissions as P

CONTRACT_CATALOG = (
    "dashboard.view · organizations.view|manage · users.view|manage|impersonate · roles.manage · "
    "establishments.view|manage · screens.view|manage|pair|command · media.view|upload|publish|delete · "
    "playlists.view|reorder|manage · campaigns.view|manage|approve|kill · creatives.view|manage · "
    "taxonomy.manage · conflicts.override · inventory.view|reserve · pricing.view|manage · checkout.create · "
    "payments.view · finance.view|manage|approve|reconcile · commissions.view|adjust · withdrawals.view|request · "
    "pix_keys.manage · crm.view|manage|discount_approve · design.view|work|request|approve · templates.manage · "
    "support.view|create|manage · equipment.view|manage · maintenance.manage · reports.view|export|financial · "
    "notifications.manage · settings.view|manage · audit.view|export · control_plane.view|manage · "
    "whitelabel.manage · lgpd.manage"
)


def _expand(spec: str) -> set[str]:
    out = set()
    for item in spec.split("·"):
        res, _, acts = item.strip().partition(".")
        for a in acts.split("|"):
            out.add(f"{res}.{a}")
    return out


def test_catalog_matches_contract_exactly():
    assert set(P.CATALOG) == _expand(CONTRACT_CATALOG)
    assert all(re.fullmatch(r"[a-z_]+\.[a-z_]+", c) for c in P.CATALOG)


def test_profiles_exist_and_reference_only_valid_codes():
    assert set(P.ROLE_TEMPLATES) == {
        "super_admin", "admin", "employee", "financial", "designer", "sales", "technician",
        "establishment_owner", "advertiser", "agency",
    }
    for t in P.ROLE_TEMPLATES.values():
        assert t.permissions <= P.ALL_PERMISSIONS, t.slug
        assert t.permissions, t.slug


def test_profile_rules_from_contract():
    T = P.ROLE_TEMPLATES
    assert T["super_admin"].permissions == P.ALL_PERMISSIONS
    assert "finance.view" in T["admin"].permissions and "finance.approve" not in T["admin"].permissions
    assert not {p for p in T["employee"].permissions if p.startswith(("finance.", "commissions.", "withdrawals."))}
    fin = T["financial"].permissions
    assert {"finance.view", "finance.manage", "finance.reconcile", "payments.view", "commissions.view",
            "commissions.adjust", "withdrawals.view", "reports.financial"} <= fin
    assert "finance.approve" not in fin
    assert {"design.view", "design.work", "templates.manage", "media.view", "media.upload"} <= T["designer"].permissions
    assert {"crm.view", "crm.manage", "pricing.view", "organizations.view", "organizations.manage"} <= T["sales"].permissions
    assert "crm.discount_approve" not in T["sales"].permissions
    assert {"equipment.view", "equipment.manage", "maintenance.manage", "screens.view", "screens.pair",
            "support.view", "support.manage"} <= T["technician"].permissions
    assert {"dashboard.view", "establishments.view", "screens.view", "playlists.reorder", "commissions.view",
            "withdrawals.view", "withdrawals.request", "reports.view", "support.create"} <= T["establishment_owner"].permissions
    adv = {"campaigns.view", "campaigns.manage", "creatives.view", "creatives.manage", "checkout.create",
           "payments.view", "design.request", "design.approve", "reports.view", "support.create"}
    assert adv <= T["advertiser"].permissions and T["agency"].permissions == T["advertiser"].permissions
    assert P.MFA_REQUIRED_ROLES == {"super_admin", "admin", "financial"}


def test_remote_player_actions_only_admin_and_technician():
    """REQ-RBAC-006 / E2 #1386–#1391: ações remotas no player são sensíveis — só admin/técnico.

    Falha se qualquer outro perfil padrão (ex.: employee) voltar a receber ``screens.command``
    ou se o técnico/admin perderem.
    """
    T = P.ROLE_TEMPLATES
    holders = {slug for slug, t in T.items() if t.permissions & P.REMOTE_PLAYER_ACTION_PERMISSIONS}
    assert holders == {"super_admin", "admin", "technician"} == set(P.REMOTE_PLAYER_ACTION_ROLES)
    assert "screens.command" not in T["employee"].permissions
    assert "screens.command" in T["technician"].permissions
    # o técnico mantém o escopo típico (GLOBAL/CITY) — a permissão vale no escopo da atribuição
    assert T["technician"].typical_scopes == ("GLOBAL", "CITY")
    # interromper criativo (kill) é ação de conteúdo, não do técnico nem do funcionário
    assert "campaigns.kill" not in T["technician"].permissions and "campaigns.kill" not in T["employee"].permissions


def test_remote_command_routes_require_screens_command():
    """Toda rota de comando remoto/PIN técnico exige screens.command (e nada mais fraco)."""
    from fastapi.routing import APIRoute

    from app.main import app

    def perms(dep):
        out = set()
        call = getattr(dep, "call", None)
        if call is not None and hasattr(call, "__rd_permission__"):
            out.add(call.__rd_permission__)
        for d in dep.dependencies:
            out |= perms(d)
        return out

    targets = [r for r in app.routes if isinstance(r, APIRoute) and "POST" in r.methods
               and (r.path.endswith("/commands") or r.path.endswith("/tech-pin"))]
    assert len(targets) >= 3, [r.path for r in targets]
    for r in targets:
        assert "screens.command" in perms(r.dependant), r.path


def test_establishment_never_approves_rejects_or_blocks_ads():
    """REQ-ORG-009 / E2: o estabelecimento NÃO aprova, reprova nem bloqueia anúncios (reclama só por ticket).

    Falha se o perfil do estabelecimento ganhar qualquer permissão de decisão sobre anúncio, ou se as
    rotas de aprovação/reprovação de campanha deixarem de exigir ``campaigns.approve``.
    """
    from fastapi.routing import APIRoute

    from app.main import app

    owner = P.ROLE_TEMPLATES["establishment_owner"].permissions
    assert not owner & {"campaigns.approve", "campaigns.kill", "campaigns.manage", "creatives.manage",
                        "conflicts.override", "media.publish"}
    assert "support.create" in owner  # o canal de reclamação é o ticket

    def perms(dep):
        out = set()
        call = getattr(dep, "call", None)
        if call is not None and hasattr(call, "__rd_permission__"):
            out.add(call.__rd_permission__)
        for d in dep.dependencies:
            out |= perms(d)
        return out

    targets = [r for r in app.routes if isinstance(r, APIRoute) and "POST" in r.methods
               and re.fullmatch(r".*/campaigns/\{campaign_id\}/(approve|reject)", r.path)]
    assert len(targets) == 2, [r.path for r in targets]
    for r in targets:
        assert "campaigns.approve" in perms(r.dependant), r.path


def _route_permissions():
    """Coleta (rota, permissão) de todas as dependências marcadas com __rd_permission__."""
    from fastapi.routing import APIRoute

    from app.main import app

    found = []

    def walk(dependant, path):
        call = getattr(dependant, "call", None)
        if call is not None and hasattr(call, "__rd_permission__"):
            found.append((path, call.__rd_permission__, getattr(call, "__rd_legacy_code__", None)))
        for d in dependant.dependencies:
            walk(d, path)

    for r in app.routes:
        if isinstance(r, APIRoute):
            walk(r.dependant, f"{sorted(r.methods)} {r.path}")
    return found


def test_every_route_permission_exists_in_catalog():
    found = _route_permissions()
    assert len(found) > 50  # rotas novas + legadas
    bad = [(p, perm) for p, perm, _ in found if perm not in P.ALL_PERMISSIONS]
    assert bad == []
    legacy_unmapped = [(p, code) for p, _, code in found if code and code not in P.LEGACY_PERMISSION_MAP]
    assert legacy_unmapped == []


def test_require_rejects_unknown_permission_at_definition():
    import pytest

    from app.core.authz import require

    with pytest.raises(ValueError):
        require("campaigns.fly")
