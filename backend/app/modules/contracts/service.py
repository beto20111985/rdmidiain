"""
RD Mídia Indoor — Contratos e aceites (W-ORG; ETAPA2 #1152–#1158; P001 §113).

- Modelos versionados por tipo (PARTNERSHIP, ADVERTISER_TERMS, COMODATO); um único
  PUBLISHED por tipo; publicado ⇒ imutável (trigger de domínio).
- Aceite ELETRÔNICO: somente o próprio cliente logado (atribuição ORGANIZATION ou
  ESTABLISHMENT da organização — equipe RD com escopo GLOBAL não "assina" pelo cliente),
  nunca durante impersonation; grava versão, hash do texto, usuário, IP, user-agent,
  data do servidor e o PDF gerado (StorageProvider). Repetir o aceite da mesma versão
  devolve o aceite existente (idempotente; UNIQUE no banco).
- Registro MANUAL (#1158 "inicialmente pode ser manual"): contrato em papel registrado
  pela equipe RD, identificado como ``MANUAL_RECORD`` (nunca apresentado como aceite
  eletrônico).
- Aceite da PARCERIA ativa o estabelecimento que aguardava contrato.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.core.logging import current_request_meta
from app.core.permissions import SCOPE_ESTABLISHMENT, SCOPE_ORGANIZATION
from app.core.timeutils import utcnow
from app.models.contracts import (
    AcceptanceMethod,
    ComodatoAgreement,
    ContractAcceptance,
    ContractKind,
    ContractTemplate,
    ContractTemplateStatus,
)
from app.models.establishments import Establishment, OnboardingStatus
from app.models.organizations import Organization
from app.modules.organizations import files

KIND_LABELS = {
    ContractKind.PARTNERSHIP.value: "Contrato de parceria (estabelecimento)",
    ContractKind.ADVERTISER_TERMS.value: "Termos de uso do anunciante",
    ContractKind.COMODATO.value: "Contrato de comodato de equipamento",
}
# permissão que o signatário precisa ter NA organização (escopo não-GLOBAL)
SIGNER_PERMISSION = {
    ContractKind.PARTNERSHIP.value: "establishments.view",
    ContractKind.COMODATO.value: "establishments.view",
    ContractKind.ADVERTISER_TERMS.value: "campaigns.view",
}
MAX_PDF_BYTES = 5 * 1024 * 1024


def sha256_text(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def published_template(db: AsyncSession, kind: str) -> Optional[ContractTemplate]:
    return (
        await db.execute(
            select(ContractTemplate).where(
                ContractTemplate.kind == kind, ContractTemplate.status == ContractTemplateStatus.PUBLISHED.value
            )
        )
    ).scalar_one_or_none()


async def create_template(db: AsyncSession, ctx: Any, kind: str, title: str, body: str) -> ContractTemplate:
    if kind not in KIND_LABELS:
        raise DomainError("CONTRACT_KIND_INVALID", "Tipo de contrato inválido.", status=400)
    title, body = (title or "").strip(), (body or "").strip()
    if len(title) < 5 or len(body) < 50:
        raise DomainError("CONTRACT_TEXT_REQUIRED", "Informe título e texto completo do contrato.", status=400)
    # serializa a numeração de versão por tipo
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"rd:contract_tpl:{kind}"))))
    nxt = (await db.execute(select(func.coalesce(func.max(ContractTemplate.version), 0)).where(
        ContractTemplate.kind == kind))).scalar_one() + 1
    t = ContractTemplate(id=uuid.uuid4(), kind=kind, version=nxt, title=title, body=body, body_sha256=sha256_text(body),
                         status=ContractTemplateStatus.DRAFT.value, created_by=getattr(ctx, "user_id", None))
    db.add(t)
    await db.flush()
    await audit(db, ctx, "contract_template.created", "contract_template", t.id,
                after={"kind": kind, "version": nxt, "title": title, "body_sha256": t.body_sha256})
    return t


async def update_template(db: AsyncSession, ctx: Any, t: ContractTemplate, title: Optional[str],
                          body: Optional[str]) -> ContractTemplate:
    if t.status != ContractTemplateStatus.DRAFT.value:
        raise Conflict("CONTRACT_TEMPLATE_IMMUTABLE", "Modelo publicado não pode ser alterado; crie nova versão.")
    before = {"title": t.title, "body_sha256": t.body_sha256}
    if title is not None and title.strip():
        t.title = title.strip()
    if body is not None:
        if len(body.strip()) < 50:
            raise DomainError("CONTRACT_TEXT_REQUIRED", "Informe o texto completo do contrato.", status=400)
        t.body = body.strip()
        t.body_sha256 = sha256_text(t.body)
    await db.flush()
    await audit(db, ctx, "contract_template.updated", "contract_template", t.id, before=before,
                after={"title": t.title, "body_sha256": t.body_sha256})
    return t


async def publish_template(db: AsyncSession, ctx: Any, t: ContractTemplate) -> ContractTemplate:
    if t.status != ContractTemplateStatus.DRAFT.value:
        raise Conflict("CONTRACT_TEMPLATE_NOT_DRAFT", "Somente rascunhos podem ser publicados.")
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"rd:contract_tpl:{t.kind}"))))
    cur = await published_template(db, t.kind)
    now = utcnow()
    if cur is not None:
        cur.status = ContractTemplateStatus.RETIRED.value
        cur.retired_at = now
        await db.flush()
    t.status = ContractTemplateStatus.PUBLISHED.value
    t.published_at = now
    await db.flush()
    await audit(db, ctx, "contract_template.published", "contract_template", t.id,
                before={"previous_published_id": str(cur.id) if cur else None},
                after={"kind": t.kind, "version": t.version, "body_sha256": t.body_sha256})
    return t


def signer_covers_org(ctx: Any, perm: str, org: Organization, establishment_id: Optional[uuid.UUID],
                      org_establishment_ids: set) -> bool:
    """O usuário tem ``perm`` por atribuição do CLIENTE (ORGANIZATION ou ESTABLISHMENT da org)?"""
    for g in getattr(ctx, "grants", ()):
        if perm not in g.permissions:
            continue
        if g.scope_type == SCOPE_ORGANIZATION and str(g.scope_id) == str(org.id):
            return True
        if g.scope_type == SCOPE_ESTABLISHMENT and g.scope_id in org_establishment_ids and (
            establishment_id is None or str(g.scope_id) == str(establishment_id)
        ):
            return True
    return False


async def _subject(db: AsyncSession, kind: str, org_id: uuid.UUID, establishment_id: Optional[uuid.UUID],
                   agreement_id: Optional[uuid.UUID]) -> tuple[Organization, Optional[Establishment], Optional[ComodatoAgreement]]:
    org = await db.get(Organization, org_id)
    if org is None:
        raise NotFound()
    est = None
    if kind in (ContractKind.PARTNERSHIP.value, ContractKind.COMODATO.value):
        if establishment_id is None:
            raise DomainError("ESTABLISHMENT_REQUIRED", "Informe o estabelecimento do contrato.", status=400)
        est = await db.get(Establishment, establishment_id)
        if est is None or est.org_id != org.id:
            raise NotFound()
    elif establishment_id is not None:
        raise DomainError("ESTABLISHMENT_NOT_APPLICABLE", "Termos do anunciante são por organização.", status=400)
    ag = None
    if kind == ContractKind.COMODATO.value:
        if agreement_id is None:
            raise DomainError("COMODATO_AGREEMENT_REQUIRED", "Informe o acordo de comodato.", status=400)
        ag = await db.get(ComodatoAgreement, agreement_id)
        if ag is None or ag.establishment_id != est.id:
            raise NotFound()
    elif agreement_id is not None:
        raise DomainError("COMODATO_NOT_APPLICABLE", "Acordo de comodato só se aplica ao contrato de comodato.",
                          status=400)
    return org, est, ag


async def _existing(db: AsyncSession, template_id: uuid.UUID, org_id, est_id, ag_id) -> Optional[ContractAcceptance]:
    q = select(ContractAcceptance).where(ContractAcceptance.template_id == template_id,
                                         ContractAcceptance.org_id == org_id)
    q = q.where(ContractAcceptance.establishment_id.is_(None) if est_id is None
                else ContractAcceptance.establishment_id == est_id)
    q = q.where(ContractAcceptance.comodato_agreement_id.is_(None) if ag_id is None
                else ContractAcceptance.comodato_agreement_id == ag_id)
    return (await db.execute(q)).scalar_one_or_none()


async def _build_and_store(db: AsyncSession, acc_id: uuid.UUID, t: ContractTemplate, org: Organization,
                           est: Optional[Establishment], *, signer_name: str, signer_email: Optional[str],
                           method: str, ip: Optional[str], ua: Optional[str], accepted_at, extra: list[str]):
    from app.modules.contracts.pdf import render_acceptance_pdf
    from app.modules.organizations.documents import format_document

    pdf = await asyncio.to_thread(
        render_acceptance_pdf,
        acceptance_id=str(acc_id), title=t.title, kind_label=KIND_LABELS[t.kind], version=t.version, body=t.body,
        body_sha256=t.body_sha256, org_name=org.name, org_document=format_document(org.document, org.document_type),
        establishment_name=est.name if est else None, signer_name=signer_name, signer_email=signer_email,
        method_label="Aceite eletrônico (login no sistema)" if method == AcceptanceMethod.ELECTRONIC.value
        else "Registro manual de contrato assinado em papel",
        ip_address=ip, user_agent=ua, accepted_at=accepted_at,
        timezone_name=est.timezone if est else "America/Sao_Paulo", extra_lines=extra,
    )
    key = files.key_for("contracts", org.id, acc_id, "contrato.pdf")
    return await files.store(db, key, pdf, "application/pdf", max_bytes=MAX_PDF_BYTES)


async def _after_accept(db: AsyncSession, ctx: Any, t: ContractTemplate, est: Optional[Establishment]) -> bool:
    """Parceria aceita ⇒ estabelecimento sai de AWAITING_CONTRACT. Retorna True se ativou."""
    if t.kind != ContractKind.PARTNERSHIP.value or est is None:
        return False
    if est.onboarding_status != OnboardingStatus.AWAITING_CONTRACT.value:
        return False
    from app.modules.screens.service import refresh_sellable, screens_of

    est.onboarding_status = OnboardingStatus.ACTIVE.value
    screens = await screens_of(db, est.id, lock=True)
    await refresh_sellable(db, ctx, screens, est, "Contrato de parceria aceito")
    await audit(db, ctx, "establishment.contract_activated", "establishment", est.id,
                before={"onboarding_status": OnboardingStatus.AWAITING_CONTRACT.value},
                after={"onboarding_status": OnboardingStatus.ACTIVE.value}, org_id=est.org_id)
    from app.modules.screens.service import request_manifest_refresh

    await db.flush()
    await request_manifest_refresh(db, [s.id for s in screens if s.status != "DECOMMISSIONED"], "contract_activated")
    return True


async def accept(
    db: AsyncSession,
    ctx: Any,
    *,
    template_id: uuid.UUID,
    org_id: uuid.UUID,
    establishment_id: Optional[uuid.UUID],
    comodato_agreement_id: Optional[uuid.UUID],
    ip: Optional[str],
    user_agent: Optional[str],
) -> tuple[ContractAcceptance, bool, Optional[Establishment]]:
    """Aceite eletrônico. Retorna (aceite, criado_agora, estabelecimento_ativado|None)."""
    if getattr(ctx, "impersonation", None) is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Aceite de contrato não é permitido durante impersonation.",
                          status=403)
    t = await db.get(ContractTemplate, template_id)
    if t is None:
        raise NotFound()
    if t.status != ContractTemplateStatus.PUBLISHED.value:
        cur = await published_template(db, t.kind)
        raise Conflict("CONTRACT_VERSION_OUTDATED", "Esta versão do contrato não está mais vigente.",
                       details={"current_template_id": str(cur.id) if cur else None,
                                "current_version": cur.version if cur else None})
    org, est, ag = await _subject(db, t.kind, org_id, establishment_id, comodato_agreement_id)
    org_est_ids = set((await db.execute(select(Establishment.id).where(Establishment.org_id == org.id))).scalars())
    if not signer_covers_org(ctx, SIGNER_PERMISSION[t.kind], org, establishment_id, org_est_ids):
        # equipe RD (GLOBAL) também cai aqui: não assina pelo cliente
        if ctx.can(SIGNER_PERMISSION[t.kind], org_id=org.id):
            raise Forbidden("CONTRACT_SIGNER_NOT_CLIENT",
                            "Somente um usuário da própria organização pode aceitar o contrato.")
        raise NotFound()
    # serializa aceites do mesmo sujeito (idempotência sem corrida)
    await db.execute(select(func.pg_advisory_xact_lock(
        func.hashtext(f"rd:acc:{t.id}:{org.id}:{establishment_id}:{comodato_agreement_id}"))))
    prev = await _existing(db, t.id, org.id, establishment_id, comodato_agreement_id)
    if prev is not None:
        return prev, False, None
    acc_id = uuid.uuid4()
    now = utcnow()
    user = ctx.user
    stored = await _build_and_store(
        db, acc_id, t, org, est, signer_name=user.full_name, signer_email=user.email,
        method=AcceptanceMethod.ELECTRONIC.value, ip=ip, ua=user_agent, accepted_at=now, extra=[],
    )
    acc = ContractAcceptance(
        id=acc_id, template_id=t.id, kind=t.kind, version=t.version, body_sha256=t.body_sha256, org_id=org.id,
        establishment_id=est.id if est else None, comodato_agreement_id=ag.id if ag else None,
        method=AcceptanceMethod.ELECTRONIC.value, accepted_by_user_id=user.id, signer_name=user.full_name,
        ip_address=(ip or "unknown")[:64], user_agent=(user_agent or None) and user_agent[:500], accepted_at=now,
        pdf_storage_key=stored.key, pdf_sha256=stored.sha256, pdf_size=stored.size,
    )
    db.add(acc)
    try:
        await db.flush()
    except IntegrityError:
        raise Conflict("CONTRACT_ALREADY_ACCEPTED", "Contrato já aceito.") from None
    activated = await _after_accept(db, ctx, t, est)
    await audit(db, ctx, "contract.accepted", "contract_acceptance", acc.id,
                after={"kind": t.kind, "version": t.version, "body_sha256": t.body_sha256, "org_id": str(org.id),
                       "establishment_id": str(est.id) if est else None, "ip": acc.ip_address,
                       "pdf_sha256": stored.sha256}, org_id=org.id)
    return acc, True, (est if activated else None)


async def record_manual(
    db: AsyncSession,
    ctx: Any,
    *,
    template_id: uuid.UUID,
    org_id: uuid.UUID,
    establishment_id: Optional[uuid.UUID],
    comodato_agreement_id: Optional[uuid.UUID],
    signer_name: str,
    notes: str,
) -> tuple[ContractAcceptance, Optional[Establishment]]:
    t = await db.get(ContractTemplate, template_id)
    if t is None:
        raise NotFound()
    if t.status == ContractTemplateStatus.DRAFT.value:
        raise Conflict("CONTRACT_TEMPLATE_NOT_PUBLISHED", "Modelo ainda não publicado.")
    signer_name, notes = (signer_name or "").strip(), (notes or "").strip()
    if len(signer_name) < 3 or len(notes) < 20:
        raise DomainError("MANUAL_RECORD_DETAILS_REQUIRED",
                          "Informe quem assinou e a descrição do documento físico (mínimo 20 caracteres).", status=400)
    org, est, ag = await _subject(db, t.kind, org_id, establishment_id, comodato_agreement_id)
    await db.execute(select(func.pg_advisory_xact_lock(
        func.hashtext(f"rd:acc:{t.id}:{org.id}:{establishment_id}:{comodato_agreement_id}"))))
    if await _existing(db, t.id, org.id, establishment_id, comodato_agreement_id) is not None:
        raise Conflict("CONTRACT_ALREADY_ACCEPTED", "Já existe aceite/registro desta versão para este contratante.")
    acc_id = uuid.uuid4()
    now = utcnow()
    meta = current_request_meta()
    stored = await _build_and_store(
        db, acc_id, t, org, est, signer_name=signer_name, signer_email=None,
        method=AcceptanceMethod.MANUAL_RECORD.value, ip=None, ua=None, accepted_at=now,
        extra=[f"Registrado por: {ctx.user.full_name} ({ctx.user.email})", f"Documento físico: {notes}"],
    )
    acc = ContractAcceptance(
        id=acc_id, template_id=t.id, kind=t.kind, version=t.version, body_sha256=t.body_sha256, org_id=org.id,
        establishment_id=est.id if est else None, comodato_agreement_id=ag.id if ag else None,
        method=AcceptanceMethod.MANUAL_RECORD.value, recorded_by_user_id=ctx.user_id, signer_name=signer_name,
        ip_address=(meta.ip or None), user_agent=None, accepted_at=now, notes=notes,
        pdf_storage_key=stored.key, pdf_sha256=stored.sha256, pdf_size=stored.size,
    )
    db.add(acc)
    await db.flush()
    activated = await _after_accept(db, ctx, t, est)
    await audit(db, ctx, "contract.manual_recorded", "contract_acceptance", acc.id,
                after={"kind": t.kind, "version": t.version, "signer_name": signer_name, "notes": notes},
                reason=notes, org_id=org.id)
    return acc, (est if activated else None)


def template_out(t: ContractTemplate, include_body: bool = True) -> dict:
    from app.modules.organizations.common import iso

    d = {
        "id": str(t.id), "kind": t.kind, "kind_label": KIND_LABELS.get(t.kind), "version": t.version,
        "title": t.title, "body_sha256": t.body_sha256, "status": t.status,
        "published_at": iso(t.published_at), "retired_at": iso(t.retired_at), "created_at": iso(t.created_at),
    }
    if include_body:
        d["body"] = t.body
    return d


def acceptance_out(a: ContractAcceptance) -> dict:
    from app.modules.organizations.common import iso

    return {
        "id": str(a.id), "template_id": str(a.template_id), "kind": a.kind, "version": a.version,
        "body_sha256": a.body_sha256, "org_id": str(a.org_id),
        "establishment_id": str(a.establishment_id) if a.establishment_id else None,
        "comodato_agreement_id": str(a.comodato_agreement_id) if a.comodato_agreement_id else None,
        "method": a.method, "accepted_by_user_id": str(a.accepted_by_user_id) if a.accepted_by_user_id else None,
        "recorded_by_user_id": str(a.recorded_by_user_id) if a.recorded_by_user_id else None,
        "signer_name": a.signer_name, "ip_address": a.ip_address, "accepted_at": iso(a.accepted_at),
        "pdf": {"sha256": a.pdf_sha256, "size": a.pdf_size} if a.pdf_storage_key else None,
        "notes": a.notes,
    }
