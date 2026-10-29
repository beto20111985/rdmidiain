"""
RD Mídia Indoor — Serviço de organizações (W-ORG; P001 §16; ETAPA2 #17–#25).

- Papéis de negócio explícitos (``business_roles``), CPF/CNPJ validados.
- Escopo: ``organizations.view|manage`` por ORGANIZATION (a própria) ou CITY (sede).
- Agência → clientes: vínculo ``agency_clients``; o acesso da equipe da agência à
  organização cliente é uma atribuição RBAC do perfil ``agency`` com escopo
  ORGANIZATION do cliente (``agency_client_grants`` guarda a atribuição criada; encerrar
  o vínculo remove as atribuições).
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, Grant, can_grant_role_in_scope, city_key
from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.core.permissions import SCOPE_ORGANIZATION
from app.core.timeutils import utcnow
from app.models.organizations import (
    BUSINESS_ROLE_LABELS,
    BUSINESS_ROLES,
    AgencyClient,
    AgencyClientGrant,
    AgencyClientStatus,
    BusinessRole,
    Organization,
)
from app.modules.organizations.documents import DocumentError, format_document, normalize_document


def org_scope(org: Organization) -> dict:
    return {"org_id": org.id, "city": org.city_key}


def ensure_org(ctx: AuthContext, perm: str, org: Organization) -> None:
    ctx.ensure(perm, org_id=org.id, city=org.city_key)


def org_filter(ctx: AuthContext, perm: str):
    return ctx.scope_filter(perm, org_col=Organization.id, city_col=Organization.city_key)


def validate_roles(roles: Optional[list[str]]) -> list[str]:
    out = []
    for r in roles or []:
        v = str(r).strip().upper()
        if v not in BUSINESS_ROLES:
            raise DomainError("BUSINESS_ROLE_INVALID", "Papel de negócio inválido.", status=400,
                              details={"allowed": list(BUSINESS_ROLES)})
        if v not in out:
            out.append(v)
    if not out:
        raise DomainError("BUSINESS_ROLE_REQUIRED", "Informe ao menos um papel (anunciante, parceiro, agência…).",
                          status=400)
    return out


def check_document(raw: str, document_type: Optional[str]) -> tuple[str, str]:
    try:
        return normalize_document(raw, document_type)
    except DocumentError as e:
        raise DomainError("DOCUMENT_INVALID", str(e), status=400) from None


async def ensure_document_free(db: AsyncSession, doc: str, exclude_id: Optional[uuid.UUID] = None) -> None:
    q = select(Organization.id).where(Organization.document == doc)
    if exclude_id is not None:
        q = q.where(Organization.id != exclude_id)
    if (await db.execute(q)).scalar_one_or_none() is not None:
        raise Conflict("DOCUMENT_ALREADY_REGISTERED", "CPF/CNPJ já cadastrado.")


def apply_address(org: Organization, data: dict) -> None:
    from app.modules.establishments.geo import normalize_postal_code, normalize_uf

    for f in ("address", "number", "complement", "neighborhood", "city"):
        if f in data:
            setattr(org, f, (data[f] or None) and str(data[f]).strip() or None)
    if "state" in data:
        org.state = normalize_uf(data["state"]) if data["state"] else None
    if "postal_code" in data:
        org.postal_code = normalize_postal_code(data["postal_code"])
    org.city_key = city_key(org.state, org.city) if org.state and org.city else None


def org_out(org: Organization, *, full: bool = True) -> dict:
    from app.modules.organizations.common import iso

    d = {
        "id": str(org.id),
        "name": org.name,
        "trade_name": org.trade_name,
        "document": org.document,
        "document_formatted": format_document(org.document, org.document_type),
        "document_type": org.document_type,
        "person_type": "PF" if org.document_type == "CPF" else "PJ",
        "business_roles": list(org.business_roles or []),
        "business_role_labels": [BUSINESS_ROLE_LABELS.get(r, r) for r in (org.business_roles or [])],
        "is_active": org.is_active,
        "kyc_status": org.kyc_status,
        "city": org.city,
        "state": org.state,
        "city_key": org.city_key,
    }
    if full:
        d.update({
            "responsible_name": org.responsible_name,
            "email": org.email,
            "phone": org.phone,
            "address": org.address,
            "number": org.number,
            "complement": org.complement,
            "neighborhood": org.neighborhood,
            "postal_code": org.postal_code,
            "deactivated_at": iso(org.deactivated_at),
            "deactivation_reason": org.deactivation_reason,
            "kyc_reviewed_at": iso(org.kyc_reviewed_at),
            "kyc_notes": org.kyc_notes,
            "notes": org.notes,
            "created_at": iso(org.created_at),
            "updated_at": iso(org.updated_at),
        })
    return d


_PAYER_FIELD_MSG = {
    "email": "E-mail obrigatório para anunciante (exigido no pagamento).",
    "phone": "Telefone com DDD obrigatório para anunciante (exigido no pagamento).",
    "document": "CPF/CNPJ obrigatório para anunciante (exigido no pagamento).",
}


def ensure_payer_data(org: Organization, fields: Optional[set] = None) -> None:
    """Empresa que COMPRA (papel ADVERTISER — dona das campanhas, logo a pagadora) precisa, no cadastro, dos
    MESMOS dados que o pagamento exige (``financial.payments.payer_missing``; AÇÃO 004, decisão do
    proprietário). ``fields`` restringe a checagem aos campos alterados (edição NÃO é retroativa: empresa
    antiga sem telefone continua editável e só é barrada no pagamento, como antes). PARTNER/AGENCY/FRANCHISE:
    sem mudança."""
    if BusinessRole.ADVERTISER.value not in (org.business_roles or []):
        return
    from app.modules.financial.payments import payer_missing

    missing = [m for m in payer_missing(org.email, org.phone, org.document) if fields is None or m in fields]
    if missing:
        raise DomainError(
            "ADVERTISER_PAYER_DATA_REQUIRED",
            "Empresa anunciante precisa de e-mail e telefone com DDD — os mesmos dados exigidos no pagamento.",
            status=422,
            details={"missing": missing, "errors": [{"loc": ["body", m], "msg": _PAYER_FIELD_MSG[m]} for m in missing]},
        )


async def create_org(db: AsyncSession, ctx: AuthContext, data: dict) -> Organization:
    doc, dtype = check_document(data["document"], data.get("document_type"))
    roles = validate_roles(data.get("business_roles"))
    await ensure_document_free(db, doc)
    org = Organization(
        id=uuid.uuid4(),
        name=data["name"].strip(),
        trade_name=(data.get("trade_name") or None),
        document=doc,
        document_type=dtype,
        business_roles=roles,
        responsible_name=data.get("responsible_name"),
        email=(str(data["email"]).lower() if data.get("email") else None),
        phone=data.get("phone"),
        notes=data.get("notes"),
        is_active=True,
        created_by=ctx.user_id,
    )
    apply_address(org, data)
    # quem tem escopo CITY só cria organizações sediadas na sua cidade
    if not ctx.can("organizations.manage", global_only=True):
        if org.city_key is None or not ctx.can("organizations.manage", city=org.city_key):
            raise Forbidden("ORG_OUT_OF_SCOPE", "Cadastre organizações da sua cidade de atuação.")
    ensure_payer_data(org)
    db.add(org)
    await db.flush()
    await audit(db, ctx, "organization.created", "organization", org.id, after=org_out(org), org_id=org.id)
    return org


async def update_org(db: AsyncSession, ctx: AuthContext, org: Organization, data: dict) -> dict:
    before_full = org_out(org)
    was_advertiser = BusinessRole.ADVERTISER.value in (org.business_roles or [])
    if "document" in data and data["document"] is not None:
        doc, dtype = check_document(data["document"], data.get("document_type"))
        if doc != org.document:
            await ensure_document_free(db, doc, exclude_id=org.id)
        org.document, org.document_type = doc, dtype
    if "business_roles" in data and data["business_roles"] is not None:
        org.business_roles = validate_roles(data["business_roles"])
    for f in ("name", "trade_name", "responsible_name", "phone", "notes"):
        if f in data and data[f] is not None:
            setattr(org, f, str(data[f]).strip() or None if f != "name" else str(data[f]).strip())
    if "email" in data and data["email"] is not None:
        org.email = str(data["email"]).lower()
    # passou a ser anunciante ⇒ checa tudo (como na criação); já era ⇒ só o que a edição mexeu
    ensure_payer_data(org, None if not was_advertiser else
                      {f for f in ("email", "phone", "document") if data.get(f) is not None})
    addr = {k: data[k] for k in ("address", "number", "complement", "neighborhood", "city", "state", "postal_code")
            if k in data and data[k] is not None}
    if addr:
        apply_address(org, addr)
        if not ctx.can("organizations.manage", global_only=True) and not ctx.can(
            "organizations.manage", org_id=org.id, city=org.city_key
        ):
            raise Forbidden("ORG_OUT_OF_SCOPE", "Endereço fora da sua área de atuação.")
    after_full = org_out(org)
    changes = {k: (before_full[k], after_full[k]) for k in after_full if before_full.get(k) != after_full[k]}
    if changes:
        await db.flush()
        await audit(db, ctx, "organization.updated", "organization", org.id,
                    before={k: v[0] for k, v in changes.items()}, after={k: v[1] for k, v in changes.items()},
                    org_id=org.id)
    return changes


async def set_kyc(db: AsyncSession, ctx: AuthContext, org: Organization, status: str, notes: Optional[str]) -> None:
    if status not in ("PENDING", "APPROVED", "REJECTED"):
        raise DomainError("KYC_STATUS_INVALID", "Status de KYC inválido.", status=400)
    before = {"kyc_status": org.kyc_status}
    org.kyc_status = status
    org.kyc_notes = notes
    org.kyc_reviewed_by = ctx.user_id
    org.kyc_reviewed_at = utcnow()
    await db.flush()
    await audit(db, ctx, "organization.kyc_reviewed", "organization", org.id, before=before,
                after={"kyc_status": status}, reason=notes, org_id=org.id)


async def set_active(db: AsyncSession, ctx: AuthContext, org: Organization, active: bool, reason: str) -> list:
    """Inativar a organização inativa todos os seus estabelecimentos (dispara remoção definitiva das telas)."""
    from app.models.establishments import Establishment
    from app.modules.screens.service import deactivate_establishment

    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    if org.is_active == active:
        raise Conflict("ORG_STATE_UNCHANGED", "A organização já está neste estado.")
    affected = []
    if not active:
        ests = (await db.execute(
            select(Establishment).where(Establishment.org_id == org.id, Establishment.is_active.is_(True))
            .order_by(Establishment.id).with_for_update()
        )).scalars().all()
        for est in ests:
            await deactivate_establishment(db, ctx, est, f"Organização inativada: {reason}")
            affected.append(est)
        org.deactivated_at = utcnow()
        org.deactivation_reason = reason
    else:
        org.deactivated_at = None
        org.deactivation_reason = None
    org.is_active = active
    await db.flush()
    await audit(db, ctx, "organization.activated" if active else "organization.deactivated", "organization", org.id,
                before={"is_active": not active}, after={"is_active": active,
                                                          "establishments_deactivated": len(affected)},
                reason=reason, org_id=org.id)
    return affected


# ─── Agência → clientes ──────────────────────────────────────────────────────

async def link_agency(db: AsyncSession, ctx: AuthContext, agency: Organization, client: Organization) -> AgencyClient:
    if not agency.has_role(BusinessRole.AGENCY):
        raise DomainError("ORG_NOT_AGENCY", "A organização não tem o papel de agência.", status=400)
    if not client.has_role(BusinessRole.ADVERTISER):
        raise DomainError("ORG_NOT_ADVERTISER", "O cliente da agência deve ser anunciante.", status=400)
    if agency.id == client.id:
        raise DomainError("AGENCY_SELF_LINK", "A agência não pode ser cliente de si mesma.", status=400)
    exists = (await db.execute(select(AgencyClient).where(
        AgencyClient.agency_org_id == agency.id, AgencyClient.client_org_id == client.id,
        AgencyClient.status == AgencyClientStatus.ACTIVE.value))).scalar_one_or_none()
    if exists is not None:
        raise Conflict("AGENCY_LINK_EXISTS", "Vínculo já existe.")
    link = AgencyClient(id=uuid.uuid4(), agency_org_id=agency.id, client_org_id=client.id,
                        status=AgencyClientStatus.ACTIVE.value, created_by=ctx.user_id)
    db.add(link)
    await db.flush()
    await audit(db, ctx, "agency.client_linked", "agency_client", link.id,
                after={"agency_org_id": str(agency.id), "client_org_id": str(client.id)}, org_id=client.id)
    return link


async def _agency_role(db: AsyncSession):
    from app.models.rbac import Permission, Role, RolePermission

    role = (await db.execute(select(Role).where(Role.slug == "agency"))).scalar_one_or_none()
    if role is None:
        raise DomainError("ROLE_NOT_FOUND", "Perfil de agência não encontrado (sincronize o catálogo).", status=500)
    perms = frozenset((await db.execute(
        select(Permission.code).join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role.id))).scalars())
    return role, perms


async def grant_agency_user(db: AsyncSession, ctx: AuthContext, link: AgencyClient, user_id: uuid.UUID) -> AgencyClientGrant:
    from app.models.rbac import RoleAssignment
    from app.models.users import User

    if link.status != AgencyClientStatus.ACTIVE.value:
        raise Conflict("AGENCY_LINK_ENDED", "Vínculo encerrado.")
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise NotFound()
    member = (await db.execute(select(RoleAssignment.id).where(
        RoleAssignment.user_id == user.id, RoleAssignment.scope_type == SCOPE_ORGANIZATION,
        RoleAssignment.scope_id == link.agency_org_id))).first()
    if member is None:
        raise DomainError("USER_NOT_AGENCY_MEMBER", "O usuário não pertence à agência.", status=400)
    role, perms = await _agency_role(db)
    g = Grant(assignment_id=uuid.uuid4(), role_id=role.id, role_slug=role.slug, role_mfa_required=role.mfa_required,
              scope_type=SCOPE_ORGANIZATION, scope_id=link.client_org_id, scope_value=None, permissions=perms)
    if not can_grant_role_in_scope(ctx, role.slug, perms, g):
        raise Forbidden("ROLE_ESCALATION", "Você não pode conceder este acesso.")
    ra = (await db.execute(select(RoleAssignment).where(
        RoleAssignment.user_id == user.id, RoleAssignment.role_id == role.id,
        RoleAssignment.scope_type == SCOPE_ORGANIZATION, RoleAssignment.scope_id == link.client_org_id,
    ))).scalar_one_or_none()
    if ra is None:
        ra = RoleAssignment(id=uuid.uuid4(), user_id=user.id, role_id=role.id, scope_type=SCOPE_ORGANIZATION,
                            scope_id=link.client_org_id, scope_value=None, granted_by=ctx.user_id)
        db.add(ra)
        await db.flush()
    gr = AgencyClientGrant(id=uuid.uuid4(), agency_client_id=link.id, user_id=user.id, role_assignment_id=ra.id,
                           created_by=ctx.user_id)
    db.add(gr)
    await db.flush()
    await audit(db, ctx, "agency.access_granted", "agency_client", link.id,
                after={"user_id": str(user.id), "role": "agency", "scope_org_id": str(link.client_org_id)},
                org_id=link.client_org_id)
    return gr


async def end_agency_link(db: AsyncSession, ctx: AuthContext, link: AgencyClient, reason: str) -> int:
    from app.models.rbac import RoleAssignment

    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    if link.status != AgencyClientStatus.ACTIVE.value:
        raise Conflict("AGENCY_LINK_ENDED", "Vínculo já encerrado.")
    grants = (await db.execute(select(AgencyClientGrant).where(
        AgencyClientGrant.agency_client_id == link.id, AgencyClientGrant.revoked_at.is_(None)))).scalars().all()
    revoked = 0
    now = utcnow()
    for g in grants:
        if g.role_assignment_id is not None:
            ra = await db.get(RoleAssignment, g.role_assignment_id)
            if ra is not None:
                await db.delete(ra)
                revoked += 1
        g.revoked_at = now
        g.role_assignment_id = None
    link.status = AgencyClientStatus.ENDED.value
    link.ended_at = now
    link.ended_by = ctx.user_id
    link.end_reason = reason
    await db.flush()
    await audit(db, ctx, "agency.client_unlinked", "agency_client", link.id,
                before={"status": "ACTIVE"}, after={"status": "ENDED", "assignments_revoked": revoked},
                reason=reason, org_id=link.client_org_id)
    return revoked


def agency_link_out(link: AgencyClient, client: Optional[Organization] = None) -> dict:
    from app.modules.organizations.common import iso

    return {
        "id": str(link.id),
        "agency_org_id": str(link.agency_org_id),
        "client_org_id": str(link.client_org_id),
        "client_name": client.name if client else None,
        "status": link.status,
        "created_at": iso(link.created_at),
        "ended_at": iso(link.ended_at),
        "end_reason": link.end_reason,
    }


def search_clause(term: str):
    t = f"%{term.strip()}%"
    return or_(Organization.name.ilike(t), Organization.trade_name.ilike(t), Organization.document.ilike(
        "%" + "".join(ch for ch in term if ch.isalnum()).upper() + "%"))
