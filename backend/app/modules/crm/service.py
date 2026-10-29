"""
RD Mídia Indoor — CRM / Comercial (AÇÃO 002 / W-BIZ).

Fluxo: lead (funil) → proposta (itens/pacotes, desconto) → [aprovação de desconto
acima do limite, ``crm.discount_approve``, nunca pelo próprio vendedor] → envio
(PDF, validade 30 dias) → aceite → CONVERSÃO em pedido pelo checkout do W-CAMP
(``create_order_from_proposal``: preço calculado no servidor, hold de inventário,
cobrança CloudPayPag) → atribuição de venda ao vendedor (``sales_attributions``,
lida pelo W-FIN para a comissão comercial).

Escopo: CRM é da equipe RD. ``crm.view|manage`` em GLOBAL vê tudo; em CITY vê leads/
propostas daquela cidade; o vendedor responsável sempre vê os seus. Fora do escopo → 404.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Optional, Sequence

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, city_key as make_city_key
from app.core.errors import DomainError, NotFound
from app.core.ids import generate_unique, random_safe
from app.core.money import apply_bps, ensure_bps, ensure_cents, format_brl
from app.core.settings_service import SettingDef, get_setting, register
from app.core.timeutils import DEFAULT_TIMEZONE, end_of_local_day_utc, local_date, utcnow
from app.models.crm import (
    Lead,
    LeadActivity,
    LeadStage,
    LeadStageEvent,
    Proposal,
    ProposalItem,
    ProposalStatus,
    SalesAttribution,
    SalesRep,
)

log = structlog.get_logger("rd.crm")

# ─── Configurações (registry do W1) ──────────────────────────────────────────
from app.core import events as _events  # noqa: E402

_events.register_event_type("crm.lead_queue", "crm.view")  # fila do comercial (tempo real)

register(SettingDef("crm.discount_limit_bps", 1000, "int",
                    "Desconto máximo (bps) que o vendedor concede sem aprovação; acima exige crm.discount_approve. "
                    "Valor inicial 10% — parâmetro a confirmar pelo proprietário (D-07).",
                    min=0, max=10000, category="comercial"))
register(SettingDef("crm.proposal_validity_days", 30, "int", "Validade da proposta comercial (dias).",
                    min=1, max=365, category="comercial"))
register(SettingDef("crm.seller_commission_bps", 0, "int",
                    "Comissão padrão do vendedor sobre a venda (bps). 0 até o proprietário definir (D-07); "
                    "cada representante pode ter percentual próprio.",
                    financial=True, min=0, max=10000, category="comercial"))

S = ProposalStatus
EDITABLE = {S.RASCUNHO.value}
# A validade (30 dias) conta a partir do ENVIO ao cliente; rascunhos não vencem.
ACTIVE_FOR_EXPIRY = {S.ENVIADA.value, S.ACEITA.value}
TERMINAL = {S.CONVERTIDA.value, S.CANCELADA.value, S.RECUSADA.value, S.EXPIRADA.value}

LEAD_TRANSITIONS: dict[str, set[str]] = {
    "NOVO": {"CONTATO", "QUALIFICADO", "PROPOSTA", "PERDIDO"},
    "CONTATO": {"QUALIFICADO", "PROPOSTA", "NEGOCIACAO", "PERDIDO"},
    "QUALIFICADO": {"PROPOSTA", "NEGOCIACAO", "PERDIDO", "CONTATO"},
    "PROPOSTA": {"NEGOCIACAO", "GANHO", "PERDIDO", "QUALIFICADO"},
    "NEGOCIACAO": {"PROPOSTA", "GANHO", "PERDIDO"},
    "GANHO": set(),
    "PERDIDO": {"CONTATO"},  # reabertura
}


# ─── Escopo ──────────────────────────────────────────────────────────────────

def can_access(ctx: AuthContext, perm: str, *, city_key: Optional[str], owner_id: Any) -> bool:
    if ctx.can(perm, city=city_key) if city_key else ctx.can(perm, global_only=True):
        return True
    return owner_id is not None and owner_id == ctx.user_id and ctx.has_any(perm)


def scope_clause(ctx: AuthContext, perm: str, city_col, owner_col):
    return or_(ctx.scope_filter(perm, city_col=city_col), owner_col == ctx.user_id)


async def get_lead(db: AsyncSession, ctx: AuthContext, lead_id: uuid.UUID, perm: str = "crm.view", *,
                   for_update: bool = False) -> Lead:
    q = select(Lead).where(Lead.id == lead_id)
    if for_update:
        q = q.with_for_update()
    lead = (await db.execute(q)).scalar_one_or_none()
    if lead is None or not can_access(ctx, perm, city_key=lead.city_key, owner_id=lead.owner_id):
        raise NotFound()
    return lead


async def get_proposal(db: AsyncSession, ctx: AuthContext, proposal_id: uuid.UUID, perm: str = "crm.view", *,
                       for_update: bool = False) -> Proposal:
    q = select(Proposal).where(Proposal.id == proposal_id)
    if for_update:
        q = q.with_for_update()
    p = (await db.execute(q)).scalar_one_or_none()
    if p is None or not can_access(ctx, perm, city_key=p.city_key, owner_id=p.seller_id):
        raise NotFound()
    return p


def _city(uf: Optional[str], city: Optional[str]) -> Optional[str]:
    return make_city_key(uf, city) if uf and city else None


# ─── Leads ───────────────────────────────────────────────────────────────────

async def create_lead(db: AsyncSession, ctx: AuthContext, data: dict) -> Lead:
    ck = _city(data.get("uf"), data.get("city"))
    if not (ctx.can("crm.manage", city=ck) if ck else ctx.can("crm.manage", global_only=True)):
        # vendedor com escopo de cidade só cria leads da sua cidade
        raise DomainError("CRM_SCOPE", "Você só pode cadastrar leads da sua região.", status=403)
    await _assert_client_org(db, data.get("org_id"))
    owner = data.get("owner_id") or ctx.user_id
    lead = Lead(
        id=uuid.uuid4(), name=data["name"].strip(), company=data.get("company"), email=data.get("email"),
        phone=data.get("phone"), source=data.get("source") or "manual", interest=data.get("interest"),
        stage=LeadStage.NOVO.value, owner_id=owner, city=data.get("city"), uf=(data.get("uf") or None),
        city_key=ck, org_id=data.get("org_id"), estimated_value_cents=data.get("estimated_value_cents"),
        notes=data.get("notes"), created_by=ctx.user_id, stage_changed_at=utcnow(),
    )
    if lead.estimated_value_cents is not None:
        ensure_cents(lead.estimated_value_cents, name="estimated_value_cents")
    db.add(lead)
    await db.flush()
    db.add(LeadStageEvent(lead_id=lead.id, from_stage=None, to_stage=lead.stage, actor_id=ctx.user_id))
    await audit(db, ctx, "crm.lead_created", "lead", lead.id, after={"name": lead.name, "city_key": ck})
    return lead


# ─── Lead de entrada (auto-cadastro / "falar com o comercial") ──────────────

OPEN_STAGES_EXCLUDED = ("GANHO", "PERDIDO")
_CTRL_RE = __import__("re").compile(r"[\x00-\x1f\x7f]+")


def _clean(v: Optional[str], max_len: int) -> Optional[str]:
    if v is None:
        return None
    out = " ".join(_CTRL_RE.sub(" ", str(v)).split())[:max_len]
    return out or None


def _clean_phone(v: Optional[str]) -> Optional[str]:
    import re as _re

    if not v:
        return None
    out = _re.sub(r"[^0-9+()\- ]", "", str(v)).strip()[:30]
    return out or None


async def upsert_inbound_lead(
    db: AsyncSession,
    *,
    source: str = "AUTO_CADASTRO",
    name: str,
    company: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    org_id: Any,
    city: Optional[str],
    uf: Optional[str],
    interest: str = "ANUNCIANTE",
    notes: Optional[str] = None,
) -> tuple[Lead, bool]:
    """Lead de ENTRADA criado pelo SISTEMA (W-ORG: auto-cadastro do anunciante, E2#1021–#1022).

    Não exige ``crm.manage`` do chamador (fluxo público já autenticado por token de e-mail). Dedupe:
    lead EM ABERTO (estágio ≠ GANHO/PERDIDO) com o mesmo ``org_id`` OU o mesmo e-mail (normalizado)
    → ``(lead, False)`` + atividade registrada; senão cria ``(lead, True)`` sem responsável (fila do
    comercial). Serializado por advisory lock (org/e-mail) — cadastros simultâneos não duplicam. Publica
    ``crm.lead_queue`` após o commit (escopo cidade ou GLOBAL, permissão ``crm.view``), notifica a equipe
    ``crm.manage`` (GLOBAL + regional da cidade) e audita com origem ``system``. Roda na transação do
    chamador (sem commit próprio)."""
    from sqlalchemy import func as _f, text as _text

    from app.core.ids import BR_UFS
    from app.modules.notifications.service import StaffRecipients, notify, publish_event_after_commit

    src = (_clean(source, 50) or "AUTO_CADASTRO").upper().replace(" ", "_")
    nm = _clean(name, 255)
    if not nm or len(nm) < 2:
        raise DomainError("LEAD_NAME_REQUIRED", "Informe o nome do contato.", status=400)
    comp = _clean(company, 255)
    em = (_clean(email, 255) or "").lower() or None
    if em and ("@" not in em or " " in em):
        em = None
    ph = _clean_phone(phone)
    cty = _clean(city, 100)
    u = (_clean(uf, 2) or "").upper() or None
    if u not in BR_UFS:
        u = None
    interest = interest if interest in ("ANUNCIANTE", "PARCEIRO", "AMBOS") else "ANUNCIANTE"
    nts = _clean(notes, 2000)
    org_uuid = uuid.UUID(str(org_id)) if org_id else None
    ck = _city(u, cty)

    keys = sorted(k for k in (f"org:{org_uuid}" if org_uuid else None, f"email:{em}" if em else None) if k)
    for k in keys:
        await db.execute(_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "rd.crm.inbound:" + k})
    conds = []
    if org_uuid:
        conds.append(Lead.org_id == org_uuid)
    if em:
        conds.append(_f.lower(Lead.email) == em)
    existing = None
    if conds:
        existing = (await db.execute(
            select(Lead).where(or_(*conds), Lead.stage.not_in(OPEN_STAGES_EXCLUDED))
            .order_by(Lead.created_at.desc()).limit(1).with_for_update())).scalar_one_or_none()
    if existing is not None:
        if existing.org_id is None and org_uuid is not None:
            existing.org_id = org_uuid
        act = LeadActivity(lead_id=existing.id, kind="NOTA", actor_id=None,
                           description=_clean(f"Novo contato pelo canal {src}: {nm}"
                                              + (f" ({comp})" if comp else "") + (f" — {nts}" if nts else ""), 5000))
        db.add(act)
        await db.flush()
        await audit(db, None, "crm.lead_inbound_repeated", "lead", existing.id,
                    after={"source": src, "activity_id": act.id}, origin="system", org_id=org_uuid)
        recips = [existing.owner_id] if existing.owner_id else [StaffRecipients("crm.manage")] + (
            [StaffRecipients("crm.manage", city_key=existing.city_key)] if existing.city_key else [])
        await notify(db, recips, "crm.lead_inbound",
                     {"lead_id": str(existing.id), "name": nm, "company": comp, "city": cty, "source": src},
                     cooldown_s=3600, entity=("lead", existing.id), channels=["IN_APP"])
        publish_event_after_commit(db, "crm.lead_queue",
                                   {"lead_id": str(existing.id), "created": False, "stage": existing.stage},
                                   city=existing.city_key)
        return existing, False

    lead = Lead(id=uuid.uuid4(), name=nm, company=comp, email=em, phone=ph, source=src, interest=interest,
                stage=LeadStage.NOVO.value, owner_id=None, city=cty, uf=u, city_key=ck, org_id=org_uuid, notes=nts,
                created_by=None, stage_changed_at=utcnow())
    db.add(lead)
    await db.flush()
    db.add(LeadStageEvent(lead_id=lead.id, from_stage=None, to_stage=lead.stage, actor_id=None, reason=src))
    await audit(db, None, "crm.lead_inbound_created", "lead", lead.id,
                after={"source": src, "city_key": ck, "org_id": org_uuid}, origin="system", org_id=org_uuid)
    await notify(db, [StaffRecipients("crm.manage")] + ([StaffRecipients("crm.manage", city_key=ck)] if ck else []),
                 "crm.lead_inbound", {"lead_id": str(lead.id), "name": nm, "company": comp, "city": cty,
                                      "source": src},
                 dedupe_key=f"crm.lead_inbound:{lead.id}", entity=("lead", lead.id))
    publish_event_after_commit(db, "crm.lead_queue", {"lead_id": str(lead.id), "created": True, "stage": lead.stage},
                               city=ck)
    return lead, True


async def change_lead_stage(db: AsyncSession, ctx: AuthContext, lead_id: uuid.UUID, to_stage: str,
                            reason: Optional[str] = None) -> Lead:
    lead = await get_lead(db, ctx, lead_id, "crm.manage", for_update=True)
    if to_stage not in LeadStage.__members__:
        raise DomainError("LEAD_STAGE_INVALID", "Estágio inválido.", status=400)
    if to_stage == lead.stage:
        return lead
    if to_stage not in LEAD_TRANSITIONS.get(lead.stage, set()):
        raise DomainError("LEAD_INVALID_TRANSITION", "Mudança de estágio não permitida.", status=409,
                          details={"from": lead.stage, "to": to_stage})
    if to_stage == "PERDIDO" and not (reason and reason.strip()):
        raise DomainError("LEAD_LOST_REASON_REQUIRED", "Informe o motivo da perda.", status=400)
    before = lead.stage
    lead.stage = to_stage
    lead.stage_changed_at = utcnow()
    if to_stage == "PERDIDO":
        lead.lost_reason = reason.strip()[:500]
    db.add(LeadStageEvent(lead_id=lead.id, from_stage=before, to_stage=to_stage, reason=reason, actor_id=ctx.user_id))
    await audit(db, ctx, "crm.lead_stage_changed", "lead", lead.id, before={"stage": before}, after={"stage": to_stage},
                reason=reason)
    return lead


async def add_activity(db: AsyncSession, ctx: AuthContext, lead_id: uuid.UUID, kind: str, description: str,
                       occurred_at=None) -> LeadActivity:
    lead = await get_lead(db, ctx, lead_id, "crm.manage")
    a = LeadActivity(lead_id=lead.id, kind=kind, description=description.strip(), occurred_at=occurred_at or utcnow(),
                     actor_id=ctx.user_id)
    db.add(a)
    await db.flush()
    return a


async def pipeline(db: AsyncSession, ctx: AuthContext) -> dict:
    """Funil: quantidade e valor estimado por estágio (dentro do escopo)."""
    from sqlalchemy import func

    rows = (
        await db.execute(
            select(Lead.stage, func.count(), func.coalesce(func.sum(Lead.estimated_value_cents), 0))
            .where(scope_clause(ctx, "crm.view", Lead.city_key, Lead.owner_id))
            .group_by(Lead.stage)
        )
    ).all()
    by = {s: (int(c), int(v)) for s, c, v in rows}
    stages = [s.value for s in LeadStage]
    total = sum(c for c, _ in by.values())
    won = by.get("GANHO", (0, 0))[0]
    lost = by.get("PERDIDO", (0, 0))[0]
    return {
        "stages": [{"stage": s, "count": by.get(s, (0, 0))[0], "estimated_value_cents": by.get(s, (0, 0))[1]}
                   for s in stages],
        "total": total,
        "conversion_rate_bps": (won * 10000 // (won + lost)) if (won + lost) else None,
    }


# ─── Propostas ───────────────────────────────────────────────────────────────

async def _new_number(db: AsyncSession) -> str:
    year = local_date(utcnow(), DEFAULT_TIMEZONE).year

    async def exists(code: str) -> bool:
        return (await db.execute(select(Proposal.id).where(Proposal.number == code))).first() is not None

    return await generate_unique(lambda: f"PROP-{year}-{random_safe(6)}", exists)


def _recompute(p: Proposal, items: Sequence[ProposalItem]) -> None:
    subtotal = sum(i.line_total_cents for i in items)
    ensure_cents(subtotal)
    ensure_bps(p.discount_bps)
    p.subtotal_cents = subtotal
    p.discount_cents = apply_bps(subtotal, p.discount_bps)
    p.total_cents = subtotal - p.discount_cents


async def load_items(db: AsyncSession, proposal_id: uuid.UUID) -> list[ProposalItem]:
    return list(
        (await db.execute(select(ProposalItem).where(ProposalItem.proposal_id == proposal_id)
                          .order_by(ProposalItem.position, ProposalItem.id))).scalars()
    )


def _build_items(proposal_id: uuid.UUID, items: Sequence[dict]) -> list[ProposalItem]:
    out = []
    for pos, it in enumerate(items):
        qty = it.get("quantity", 1)
        unit = it["unit_price_cents"]
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise DomainError("PROPOSAL_ITEM_INVALID", "Quantidade deve ser inteira e positiva.", status=400)
        ensure_cents(unit, name="unit_price_cents")
        out.append(ProposalItem(id=uuid.uuid4(), proposal_id=proposal_id, position=pos,
                                package_code=it.get("package_code"), description=it["description"].strip(),
                                quantity=qty, unit_price_cents=unit, line_total_cents=qty * unit,
                                details=it.get("details") or None))
    return out


async def _assert_client_org(db: AsyncSession, org_id: Any) -> None:
    """Cliente de proposta/lead nunca é a organização da própria instância."""
    if org_id is None:
        return
    from app.modules.instance.service import instance_org_ids

    if uuid.UUID(str(org_id)) in await instance_org_ids(db):
        raise DomainError("CLIENT_IS_INSTANCE", "A organização da própria instância não pode ser cliente.", status=400)


async def client_options(db: AsyncSession, ctx: AuthContext, *, search: Optional[str], limit: int = 20) -> list[dict]:
    """Organizações elegíveis como CLIENTE de proposta: ativas, papel ANUNCIANTE (ou agência), excluída a
    organização da própria instância. Só o necessário para o seletor (id, nome, fantasia, cidade/UF, papéis)."""
    from sqlalchemy import func

    from app.models.organizations import Organization
    from app.modules.instance.service import instance_org_ids

    if not ctx.has_any("crm.manage"):
        raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403)
    q = select(Organization).where(
        Organization.is_active.is_(True),
        or_(Organization.business_roles.any("ADVERTISER"), Organization.business_roles.any("AGENCY")))
    own = await instance_org_ids(db)
    if own:
        q = q.where(Organization.id.not_in(own))
    if search and search.strip():
        term = search.strip().replace("%", "").replace("_", "")
        doc = "".join(ch for ch in term if ch.isalnum()).upper()
        conds = [Organization.name.ilike(f"%{term}%"), Organization.trade_name.ilike(f"%{term}%")]
        if doc:
            conds.append(func.upper(Organization.document).like(f"%{doc}%"))
        q = q.where(or_(*conds))
    rows = (await db.execute(q.order_by(Organization.name, Organization.id).limit(limit))).scalars()
    return [{"id": str(o.id), "name": o.name, "trade_name": o.trade_name, "city": o.city, "uf": o.state,
             "business_roles": list(o.business_roles or [])} for o in rows]


async def create_proposal(db: AsyncSession, ctx: AuthContext, data: dict) -> Proposal:
    lead = None
    if data.get("lead_id"):
        lead = await get_lead(db, ctx, data["lead_id"], "crm.manage")
    org_id = data.get("org_id") or (lead.org_id if lead else None)
    await _assert_client_org(db, org_id)
    if lead is None and org_id is None:
        raise DomainError("PROPOSAL_TARGET_REQUIRED", "Informe o lead ou a organização da proposta.", status=400)
    ck = lead.city_key if lead else _city(data.get("uf"), data.get("city"))
    if lead is None and not (ctx.can("crm.manage", city=ck) if ck else ctx.can("crm.manage", global_only=True)):
        raise DomainError("CRM_SCOPE", "Você só pode criar propostas da sua região.", status=403)
    discount_bps = data.get("discount_bps", 0)
    ensure_bps(discount_bps, name="discount_bps")
    validity = int(await get_setting(db, "crm.proposal_validity_days"))
    p = Proposal(
        id=uuid.uuid4(), number=await _new_number(db), lead_id=lead.id if lead else None, org_id=org_id,
        seller_id=(lead.owner_id if lead and lead.owner_id else ctx.user_id), city_key=ck,
        title=data["title"].strip(), status=S.RASCUNHO.value, discount_bps=discount_bps,
        discount_justification=data.get("discount_justification"), validity_days=validity, notes=data.get("notes"),
        subtotal_cents=0, discount_cents=0, total_cents=0, version=1,
    )
    db.add(p)
    await db.flush()
    items = _build_items(p.id, data.get("items") or [])
    for it in items:
        db.add(it)
    _recompute(p, items)
    p.valid_until = local_date(utcnow(), DEFAULT_TIMEZONE) + timedelta(days=validity)
    p.expires_at = end_of_local_day_utc(p.valid_until, DEFAULT_TIMEZONE)
    await db.flush()
    if lead is not None and lead.stage in ("NOVO", "CONTATO", "QUALIFICADO"):
        await change_lead_stage(db, ctx, lead.id, "PROPOSTA")
    await audit(db, ctx, "crm.proposal_created", "proposal", p.id,
                after={"number": p.number, "total_cents": p.total_cents, "discount_bps": p.discount_bps})
    return p


async def update_proposal(db: AsyncSession, ctx: AuthContext, proposal_id: uuid.UUID, changes: dict) -> Proposal:
    p = await get_proposal(db, ctx, proposal_id, "crm.manage", for_update=True)
    if p.status not in EDITABLE:
        raise DomainError("PROPOSAL_NOT_EDITABLE", "Somente propostas em rascunho podem ser alteradas.", status=409,
                          details={"status": p.status})
    before = {"total_cents": p.total_cents, "discount_bps": p.discount_bps}
    if changes.get("org_id") is not None:
        await _assert_client_org(db, changes["org_id"])
    for f in ("title", "notes", "discount_justification", "org_id"):
        if f in changes and changes[f] is not None:
            setattr(p, f, changes[f])
    if "discount_bps" in changes and changes["discount_bps"] is not None:
        ensure_bps(changes["discount_bps"], name="discount_bps")
        p.discount_bps = changes["discount_bps"]
    items = await load_items(db, p.id)
    if changes.get("items") is not None:
        for it in items:
            await db.delete(it)
        await db.flush()
        items = _build_items(p.id, changes["items"])
        for it in items:
            db.add(it)
    _recompute(p, items)
    p.version += 1
    # qualquer alteração invalida aprovação anterior de desconto
    p.discount_approved_by = p.discount_approved_at = None
    p.discount_approval_required = False
    await db.flush()
    await audit(db, ctx, "crm.proposal_updated", "proposal", p.id, before=before,
                after={"total_cents": p.total_cents, "discount_bps": p.discount_bps, "version": p.version})
    return p


async def submit_proposal(db: AsyncSession, ctx: AuthContext, proposal_id: uuid.UUID) -> Proposal:
    from app.modules.notifications.service import StaffRecipients, notify

    p = await get_proposal(db, ctx, proposal_id, "crm.manage", for_update=True)
    if p.status != S.RASCUNHO.value:
        raise DomainError("PROPOSAL_INVALID_TRANSITION", "A proposta não está em rascunho.", status=409,
                          details={"status": p.status})
    items = await load_items(db, p.id)
    if not items or p.total_cents <= 0:
        raise DomainError("PROPOSAL_EMPTY", "Inclua ao menos um item com valor.", status=400)
    limit = int(await get_setting(db, "crm.discount_limit_bps"))
    p.discount_limit_bps_snapshot = limit
    if p.discount_bps > limit:
        if not (p.discount_justification or "").strip():
            raise DomainError("DISCOUNT_JUSTIFICATION_REQUIRED",
                              "Desconto acima do limite exige justificativa para aprovação.", status=400)
        p.discount_approval_required = True
        p.status = S.AGUARDANDO_APROVACAO.value
        await notify(db, [StaffRecipients("crm.discount_approve")] +
                     ([StaffRecipients("crm.discount_approve", city_key=p.city_key)] if p.city_key else []),
                     "crm.discount_approval_requested",
                     {"proposal_number": p.number, "discount": f"{p.discount_bps / 100:.2f}%".replace(".", ","),
                      "limit": f"{limit / 100:.2f}%".replace(".", ","), "proposal_id": str(p.id)},
                     dedupe_key=f"crm.discount_approval:{p.id}:{p.version}", entity=("proposal", p.id))
    else:
        p.discount_approval_required = False
        p.status = S.APROVADA.value
    await audit(db, ctx, "crm.proposal_submitted", "proposal", p.id,
                after={"status": p.status, "discount_bps": p.discount_bps, "limit_bps": limit})
    return p


async def decide_discount(db: AsyncSession, ctx: AuthContext, proposal_id: uuid.UUID, approve: bool,
                          reason: Optional[str]) -> Proposal:
    from app.modules.notifications.service import notify

    p = await get_proposal(db, ctx, proposal_id, "crm.view", for_update=True)
    allowed = ctx.can("crm.discount_approve", city=p.city_key) if p.city_key else ctx.can("crm.discount_approve",
                                                                                            global_only=True)
    if not allowed:
        raise DomainError("FORBIDDEN", "Você não tem permissão para aprovar descontos.", status=403,
                          details={"permission": "crm.discount_approve"})
    if p.status != S.AGUARDANDO_APROVACAO.value:
        raise DomainError("PROPOSAL_INVALID_TRANSITION", "A proposta não aguarda aprovação de desconto.", status=409,
                          details={"status": p.status})
    if ctx.user_id == p.seller_id:
        raise DomainError("SELF_APPROVAL_FORBIDDEN", "O vendedor não pode aprovar o próprio desconto.", status=403)
    if not approve and not (reason and reason.strip()):
        raise DomainError("REASON_REQUIRED", "Informe o motivo da recusa.", status=400)
    if approve:
        p.status = S.APROVADA.value
        p.discount_approved_by = ctx.user_id
        p.discount_approved_at = utcnow()
    else:
        p.status = S.RASCUNHO.value
        p.discount_approved_by = p.discount_approved_at = None
    p.discount_decision_reason = reason
    await audit(db, ctx, "crm.discount_approved" if approve else "crm.discount_rejected", "proposal", p.id,
                before={"status": S.AGUARDANDO_APROVACAO.value},
                after={"status": p.status, "discount_bps": p.discount_bps}, reason=reason)
    await notify(db, [p.seller_id], "crm.discount_decided",
                 {"proposal_number": p.number, "decision": "aprovado" if approve else "recusado", "proposal_id": str(p.id)},
                 dedupe_key=f"crm.discount_decided:{p.id}:{p.version}:{int(approve)}", channels=["IN_APP", "EMAIL"])
    return p


def _expired(p: Proposal) -> bool:
    return p.expires_at is not None and utcnow() > p.expires_at


def effective_status(p: Proposal) -> str:
    """Status exibido: vencida antes do job diário ainda conta como EXPIRADA."""
    return S.EXPIRADA.value if (p.status in ACTIVE_FOR_EXPIRY and _expired(p)) else p.status


async def _expire_if_needed(db: AsyncSession, ctx: Any, p: Proposal) -> None:
    # Só rejeita: a transição persistente para EXPIRADA é feita pelo job diário
    # (aqui a transação da request seria desfeita pelo próprio erro).
    if p.status in ACTIVE_FOR_EXPIRY and _expired(p):
        raise DomainError("PROPOSAL_EXPIRED", "Proposta vencida (validade de 30 dias).", status=409,
                          details={"valid_until": p.valid_until.isoformat() if p.valid_until else None})


async def render_pdf(db: AsyncSession, p: Proposal) -> bytes:
    from app.models.organizations import Organization
    from app.models.users import User
    from app.modules.instance.service import get_brand
    from app.modules.reports.pdf import Heading, KeyValues, Para, PdfDoc, Table, render_pdf as _render

    items = await load_items(db, p.id)
    lead = await db.get(Lead, p.lead_id) if p.lead_id else None
    org = await db.get(Organization, p.org_id) if p.org_id else None
    seller = await db.get(User, p.seller_id)
    b = await get_brand(db)
    brand = b.name
    client = (org.name if org else None) or (lead.company if lead and lead.company else (lead.name if lead else "—"))
    draft = p.status in (S.RASCUNHO.value, S.AGUARDANDO_APROVACAO.value)
    valid = p.valid_until.strftime("%d/%m/%Y") if p.valid_until else "—"
    blocks: list = [
        KeyValues([
            ("Proposta", p.number),
            ("Situação", "RASCUNHO — sem validade comercial" if draft else p.status.replace("_", " ").title()),
            ("Cliente", client),
            ("Contato", (lead.name if lead else "—")),
            ("Vendedor", seller.full_name if seller else "—"),
            ("Validade", f"{p.validity_days} dias — válida até {valid} (horário de Brasília)"),
        ]),
        Heading("Itens"),
        Table(["Pacote / descrição", "Qtd.", "Valor unitário", "Total"],
              [[(f"[{i.package_code}] " if i.package_code else "") + i.description, i.quantity,
                format_brl(i.unit_price_cents), format_brl(i.line_total_cents)] for i in items],
              col_widths=[95, 15, 35, 35], align_right=[1, 2, 3]),
        KeyValues([
            ("Subtotal", format_brl(p.subtotal_cents)),
            ("Desconto", f"{p.discount_bps / 100:.2f}%".replace(".", ",") + f" ({format_brl(p.discount_cents)})"),
            ("Total", format_brl(p.total_cents)),
        ]),
        Heading("Condições", level=2),
        Para("Valores em reais (R$). O valor final do pedido é calculado no checkout com base nos pacotes desta "
             "proposta e no desconto aprovado. Pagamento exclusivamente pela CloudPayPag. Após o checkout, o "
             "inventário fica reservado por 2 horas aguardando o pagamento. A veiculação depende da aprovação do "
             "conteúdo pela RD.", small=True),
    ]
    if p.notes:
        blocks += [Heading("Observações", level=2), Para(p.notes, small=True)]
    return _render(PdfDoc(title=f"Proposta comercial {p.number}", subtitle=p.title, blocks=blocks, **b.pdf_kwargs(),
                          footer=f"{brand} · Proposta {p.number} · válida até {valid}",
                          keywords=f"proposta {p.number}"))


async def send_proposal(db: AsyncSession, ctx: AuthContext, proposal_id: uuid.UUID,
                        email_to: Optional[str] = None) -> tuple[Proposal, bytes, bool]:
    """APROVADA → ENVIADA: fixa a validade (data de envio + 30 dias), gera e guarda o PDF;
    envia por e-mail ao contato quando informado/disponível. → (proposta, pdf, enviado_por_email)."""
    from app.core.email import OutgoingEmail, get_email_sender
    from app.core.storage import build_key, get_storage
    from app.modules.instance.service import email_html, get_brand

    p = await get_proposal(db, ctx, proposal_id, "crm.manage", for_update=True)
    if p.status != S.APROVADA.value:
        raise DomainError("PROPOSAL_INVALID_TRANSITION",
                          "Somente propostas aprovadas podem ser enviadas (desconto acima do limite exige aprovação).",
                          status=409, details={"status": p.status})
    now = utcnow()
    p.sent_at = now
    p.valid_until = local_date(now, DEFAULT_TIMEZONE) + timedelta(days=p.validity_days)
    p.expires_at = end_of_local_day_utc(p.valid_until, DEFAULT_TIMEZONE)
    p.status = S.ENVIADA.value
    pdf = await render_pdf(db, p)
    obj = await get_storage().put_stream(build_key("crm", "proposals", p.id, f"v{p.version}.pdf"), pdf,
                                         "application/pdf", max_bytes=20 * 1024 * 1024)
    p.pdf_key, p.pdf_sha256 = obj.key, obj.sha256
    await audit(db, ctx, "crm.proposal_sent", "proposal", p.id,
                after={"status": p.status, "valid_until": p.valid_until, "pdf_sha256": obj.sha256})
    lead = await db.get(Lead, p.lead_id) if p.lead_id else None
    to = email_to or (lead.email if lead else None)
    emailed = False
    if to:
        b = await get_brand(db)
        brand = b.name
        await db.flush()
        subject = f"{brand} — Proposta comercial {p.number}"
        text = (f"Olá{(' ' + lead.name) if lead else ''},\n\nSegue a proposta comercial {p.number} ({p.title}), "
                f"no valor total de {format_brl(p.total_cents)}, válida até {p.valid_until.strftime('%d/%m/%Y')}.\n\n"
                f"{brand}")
        await get_email_sender().send(OutgoingEmail(
            to=to, subject=subject, text=text, html=email_html(b, f"Proposta comercial {p.number}", text),
            attachments=[(f"proposta-{p.number}.pdf", "application/pdf", pdf)],
            tags={"proposal_id": str(p.id)},
        ))
        emailed = True
    return p, pdf, emailed


async def record_client_decision(db: AsyncSession, ctx: AuthContext, proposal_id: uuid.UUID, accepted: bool,
                                 note: Optional[str]) -> Proposal:
    p = await get_proposal(db, ctx, proposal_id, "crm.manage", for_update=True)
    if p.status != S.ENVIADA.value:
        raise DomainError("PROPOSAL_INVALID_TRANSITION", "A proposta não está enviada ao cliente.", status=409,
                          details={"status": p.status})
    await _expire_if_needed(db, ctx, p)
    p.status = S.ACEITA.value if accepted else S.RECUSADA.value
    p.accepted_at = utcnow() if accepted else None
    p.decided_note = note
    await audit(db, ctx, "crm.proposal_accepted" if accepted else "crm.proposal_declined", "proposal", p.id,
                after={"status": p.status}, reason=note)
    if not accepted and p.lead_id:
        lead = await db.get(Lead, p.lead_id)
        if lead and lead.stage not in ("GANHO", "PERDIDO"):
            await change_lead_stage(db, ctx, lead.id, "NEGOCIACAO" if lead.stage == "PROPOSTA" else lead.stage)
    return p


async def cancel_proposal(db: AsyncSession, ctx: AuthContext, proposal_id: uuid.UUID, reason: str) -> Proposal:
    p = await get_proposal(db, ctx, proposal_id, "crm.manage", for_update=True)
    if p.status in TERMINAL:
        raise DomainError("PROPOSAL_INVALID_TRANSITION", "Proposta já encerrada.", status=409, details={"status": p.status})
    before = p.status
    p.status = S.CANCELADA.value
    await audit(db, ctx, "crm.proposal_cancelled", "proposal", p.id, before={"status": before},
                after={"status": p.status}, reason=reason)
    return p


async def seller_commission_bps(db: AsyncSession, seller_id: uuid.UUID) -> tuple[int, Optional[uuid.UUID]]:
    rep = (await db.execute(select(SalesRep).where(SalesRep.user_id == seller_id, SalesRep.is_active.is_(True)))).scalar_one_or_none()
    if rep is not None and rep.commission_bps is not None and rep.compensation in ("COMISSAO", "AMBOS"):
        return rep.commission_bps, rep.id
    if rep is not None and rep.compensation == "SALARIO_FIXO":
        return 0, rep.id
    return int(await get_setting(db, "crm.seller_commission_bps")), (rep.id if rep else None)


CONVERTIBLE_ISSUES = {
    "SEM_DATAS": "Defina as datas de início e fim.",
    "INICIO_NO_PASSADO": "A data de início já passou.",
    "SEM_SEGMENTACAO": "Defina a segmentação da campanha.",
}


def _convertible_cond(Campaign):
    # mesma regra do checkout do W-CAMP (campaigns.checkout._validate_campaign_for_checkout)
    return or_(Campaign.status == "RASCUNHO",
               (Campaign.status == "REJEITADA") & Campaign.requires_new_payment.is_(True))


async def convertible_campaigns(db: AsyncSession, ctx: AuthContext, proposal_id: uuid.UUID) -> list[dict]:
    """Campanhas da organização-cliente da proposta que podem ir para a conversão (só o necessário)."""
    from sqlalchemy import func

    from app.models.campaigns import Campaign, CampaignTarget

    p = await get_proposal(db, ctx, proposal_id, "crm.manage")
    if p.org_id is None:
        raise DomainError("PROPOSAL_ORG_REQUIRED", "Vincule a proposta a uma organização (cliente).", status=400)
    camps = list((await db.execute(
        select(Campaign).where(Campaign.org_id == p.org_id, _convertible_cond(Campaign))
        .order_by(Campaign.created_at.desc()).limit(200))).scalars())
    targets = {}
    if camps:
        targets = dict((await db.execute(
            select(CampaignTarget.campaign_id, func.count()).where(
                CampaignTarget.campaign_id.in_([c.id for c in camps]), CampaignTarget.is_exclude.is_(False))
            .group_by(CampaignTarget.campaign_id))).all())
    today = local_date(utcnow(), DEFAULT_TIMEZONE)
    out = []
    for c in camps:
        issues = []
        if c.start_date is None or c.end_date is None:
            issues.append("SEM_DATAS")
        elif c.start_date < today:
            issues.append("INICIO_NO_PASSADO")
        if not targets.get(c.id):
            issues.append("SEM_SEGMENTACAO")
        out.append({"id": str(c.id), "name": c.name, "status": c.status,
                    "start_date": c.start_date.isoformat() if c.start_date else None,
                    "end_date": c.end_date.isoformat() if c.end_date else None, "duration_s": c.duration_s,
                    "ready": not issues, "issues": [{"code": i, "message": CONVERTIBLE_ISSUES[i]} for i in issues]})
    return out


async def convert_proposal(db: AsyncSession, ctx: AuthContext, proposal_id: uuid.UUID, *, campaign_ids: list,
                           idempotency_key: str, coupon_code: Optional[str] = None) -> dict:
    """ACEITA → CONVERTIDA: cria o pedido pelo MESMO checkout do W-CAMP (preço no servidor,
    hold de inventário 2 h, cobrança CloudPayPag) e registra a atribuição ao vendedor."""
    p = await get_proposal(db, ctx, proposal_id, "crm.manage", for_update=True)
    if p.status == S.CONVERTIDA.value and p.order_id is not None:
        raise DomainError("PROPOSAL_ALREADY_CONVERTED", "Proposta já convertida em pedido.", status=409,
                          details={"order_id": str(p.order_id)})
    if p.status != S.ACEITA.value:
        raise DomainError("PROPOSAL_INVALID_TRANSITION", "Somente propostas aceitas pelo cliente viram pedido.",
                          status=409, details={"status": p.status})
    if _expired(p):
        raise DomainError("PROPOSAL_EXPIRED", "Proposta vencida (validade de 30 dias).", status=409)
    if p.org_id is None:
        raise DomainError("PROPOSAL_ORG_REQUIRED", "Vincule a proposta a uma organização (cliente) antes de converter.",
                          status=400)
    if not campaign_ids:
        raise DomainError("PROPOSAL_CAMPAIGNS_REQUIRED", "Informe as campanhas montadas a partir da proposta.", status=400)
    from app.models.campaigns import Campaign

    wanted = {uuid.UUID(str(x)) for x in campaign_ids}
    found = dict((await db.execute(select(Campaign.id, Campaign.org_id).where(Campaign.id.in_(wanted)))).all())
    if set(found) != wanted or any(o != p.org_id for o in found.values()):
        raise NotFound()  # campanha inexistente ou de outra organização (não vaza existência)
    try:  # W-CAMP: mesma rotina do checkout (preço no servidor, hold 2 h, cobrança)
        from app.modules.campaigns.checkout import create_order_from_proposal
    except ImportError:  # pragma: no cover - depende do W-CAMP
        raise DomainError("CHECKOUT_UNAVAILABLE", "Checkout indisponível no momento.", status=503) from None
    # O checkout roda com contexto de SISTEMA: o vendedor (equipe RD, sem checkout.create na
    # organização do cliente) já foi autorizado aqui (crm.manage no escopo da proposta) e o
    # aceite do cliente está registrado; a auditoria do CRM identifica o vendedor.
    sys_ctx = AuthContext.system("system")
    result = await create_order_from_proposal(
        db, sys_ctx, proposal_id=p.id, org_id=p.org_id, campaign_ids=list(campaign_ids), idempotency_key=idempotency_key,
        discount_bps=p.discount_bps, discount_approved_by=p.discount_approved_by, coupon_code=coupon_code,
    )
    order = result.order
    order_total = _order_total(order)
    p.status = S.CONVERTIDA.value
    p.order_id = order.id
    p.converted_at = utcnow()
    bps, rep_id = await seller_commission_bps(db, p.seller_id)
    existing = (await db.execute(select(SalesAttribution).where(SalesAttribution.order_id == order.id))).scalar_one_or_none()
    if existing is None:
        db.add(SalesAttribution(proposal_id=p.id, order_id=order.id, seller_id=p.seller_id, sales_rep_id=rep_id,
                                org_id=p.org_id, amount_cents=order_total, commission_bps=bps,
                                commission_cents=apply_bps(order_total, bps), status="PENDENTE"))
    if p.lead_id:
        lead = await db.get(Lead, p.lead_id)
        if lead and lead.stage != "GANHO" and "GANHO" in LEAD_TRANSITIONS.get(lead.stage, set()):
            await change_lead_stage(db, ctx, lead.id, "GANHO")
    await db.flush()
    await audit(db, ctx, "crm.proposal_converted", "proposal", p.id,
                after={"order_id": order.id, "order_total_cents": order_total, "proposal_total_cents": p.total_cents,
                       "seller_commission_bps": bps}, org_id=p.org_id)
    return {"proposal": p, "order_id": order.id, "order_total_cents": order_total,
            "proposal_total_cents": p.total_cents, "checkout": getattr(result, "response", None)}


def _order_total(order: Any) -> int:
    for attr in ("total_cents", "amount_cents", "final_amount_cents"):
        v = getattr(order, attr, None)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    raise DomainError("ORDER_TOTAL_UNKNOWN", "Não foi possível ler o total do pedido.", status=500)


async def expire_due(db: AsyncSession) -> int:
    """Job diário: marca EXPIRADA as propostas ativas vencidas."""
    rows = list(
        (await db.execute(
            select(Proposal).where(Proposal.status.in_(list(ACTIVE_FOR_EXPIRY)), Proposal.expires_at < utcnow())
            .with_for_update(skip_locked=True)
        )).scalars()
    )
    for p in rows:
        before = p.status
        p.status = S.EXPIRADA.value
        await audit(db, None, "crm.proposal_expired", "proposal", p.id, before={"status": before},
                    after={"status": p.status}, origin="worker")
    return len(rows)
