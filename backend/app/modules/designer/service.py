"""
RD Mídia Indoor — Produção de arte / Designer (AÇÃO 002 / W-BIZ).

Pedido de arte é um SERVIÇO PAGO à parte (ETAPA2 #370, #1427): o cliente (anunciante
ou estabelecimento parceiro) envia o briefing com anexos; a RD orça (ou marca como
negociado no contrato do parceiro/isento); após o pagamento o pedido entra na fila do
designer. O cliente revisa cada versão: aprova FORMALMENTE (data/hora, IP, user-agent,
usuário, sha256 da versão) ou pede alteração (consome 1 das 3 revisões incluídas). A
4ª alteração é BLOQUEADA até uma revisão extra ser aprovada pela RD e, se cobrada, paga.
Arte aprovada fica TRAVADA (trigger no banco): qualquer mudança gera nova versão que
precisa de nova aprovação. Publicação: a versão aprovada vira mídia da organização pela
ingestão do W-MEDIA (mesma validação/pipeline do upload).

Permissões: cliente ``design.request`` (abrir/acompanhar) e ``design.approve`` (decidir)
no escopo da organização/estabelecimento; equipe ``design.view`` (ver) e ``design.work``
(fila, versões, orçamento, publicação) em escopo GLOBAL. Fora do escopo → 404.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.errors import DomainError, NotFound
from app.core.ids import generate_unique, random_safe
from app.core.logging import current_request_meta
from app.core.money import ensure_cents
from app.core.settings_service import SettingDef, get_setting, register
from app.core.timeutils import utcnow
from app.models.designer import (
    DesignApproval,
    DesignAttachment,
    DesignComment,
    DesignEvent,
    DesignExtraRevision,
    DesignRequest,
    DesignStatus,
    DesignVersion,
)

log = structlog.get_logger("rd.designer")

register(SettingDef("design.included_revisions", 3, "int", "Revisões de arte incluídas em cada pedido.",
                    min=0, max=20, category="design"))
register(SettingDef("design.extra_revision_policy", "COBRAR", "enum",
                    "Revisão além das incluídas: COBRAR (exige aprovação da RD e pagamento, se houver valor) ou "
                    "BLOQUEAR (não permite).", choices=("COBRAR", "BLOQUEAR"), category="design"))
register(SettingDef("design.extra_revision_price_cents", 0, "int",
                    "Valor sugerido da revisão extra (centavos); a RD confirma o valor ao aprovar.",
                    min=0, max=10_000_000, category="design"))

D = DesignStatus
OPEN_FOR_WORK = {D.NA_FILA.value, D.EM_DESIGN.value, D.ALTERACAO_SOLICITADA.value}
TERMINAL = {D.CANCELADA.value}
FHD = {"HORIZONTAL": (1920, 1080), "VERTICAL": (1080, 1920)}
VERSION_LIMITS = {"IMAGEM": 10 * 1024 * 1024, "VIDEO": 200 * 1024 * 1024}


# ─── Escopo ──────────────────────────────────────────────────────────────────

def is_worker(ctx: AuthContext) -> bool:
    return ctx.can("design.work", global_only=True)


def is_staff_viewer(ctx: AuthContext) -> bool:
    return ctx.can("design.view", global_only=True) or is_worker(ctx)


def can_see(ctx: AuthContext, r: DesignRequest) -> bool:
    if is_staff_viewer(ctx):
        return True
    for perm in ("design.request", "design.approve"):
        if ctx.can(perm, org_id=r.org_id, establishment_id=r.establishment_id):
            return True
    return False


def can_client_decide(ctx: AuthContext, r: DesignRequest) -> bool:
    return ctx.can("design.approve", org_id=r.org_id, establishment_id=r.establishment_id)


def list_filter(ctx: AuthContext):
    if is_staff_viewer(ctx):
        from sqlalchemy import true

        return true()
    return or_(
        ctx.scope_filter("design.request", org_col=DesignRequest.org_id, establishment_col=DesignRequest.establishment_id),
        ctx.scope_filter("design.approve", org_col=DesignRequest.org_id, establishment_col=DesignRequest.establishment_id),
    )


async def get_request(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID, *, for_update: bool = False) -> DesignRequest:
    q = select(DesignRequest).where(DesignRequest.id == request_id)
    if for_update:
        q = q.with_for_update()
    r = (await db.execute(q)).scalar_one_or_none()
    if r is None or not can_see(ctx, r):
        raise NotFound()
    return r


def _require_worker(ctx: AuthContext) -> None:
    if not is_worker(ctx):
        raise DomainError("FORBIDDEN", "Ação exclusiva da equipe de design.", status=403,
                          details={"permission": "design.work"})


async def _event(db: AsyncSession, ctx: Any, r: DesignRequest, from_status: Optional[str], note: Optional[str] = None,
                 data: Optional[dict] = None) -> None:
    db.add(DesignEvent(request_id=r.id, actor_id=getattr(ctx, "user_id", None), from_status=from_status,
                       to_status=r.status, note=note, data=data))


async def _publish_rt(r: DesignRequest) -> None:
    from app.core import events

    try:
        await events.publish("designer.updated", {"id": str(r.id), "code": r.code, "status": r.status},
                             org_id=r.org_id)
    except Exception as exc:  # tempo real é best-effort; a fonte de verdade é o banco
        log.warning("designer_event_publish_failed", error_type=type(exc).__name__)


def _client_recipients(r: DesignRequest):
    from app.modules.notifications.service import EstablishmentRecipients, OrgRecipients

    recips = [OrgRecipients(r.org_id, "design.approve")]
    if r.establishment_id:
        recips.append(EstablishmentRecipients(r.establishment_id, "design.approve"))
    return recips


def _vars(r: DesignRequest, **extra) -> dict:
    return {"request_code": r.code, "title": r.title, "request_id": str(r.id), **extra}


async def _limit(r: DesignRequest) -> int:
    return r.revisions_included + r.extra_revisions


# ─── Criação / orçamento / pagamento ─────────────────────────────────────────

async def create_request(db: AsyncSession, ctx: AuthContext, data: dict) -> DesignRequest:
    from app.models.establishments import Establishment
    from app.models.organizations import Organization
    from app.modules.notifications.service import StaffRecipients, notify

    org_id = data["org_id"]
    est_id = data.get("establishment_id")
    if await db.get(Organization, org_id) is None:
        raise NotFound()
    if est_id is not None:
        est = await db.get(Establishment, est_id)
        if est is None or est.org_id != org_id:
            raise NotFound()
    worker = is_worker(ctx)
    if not worker and not ctx.can("design.request", org_id=org_id, establishment_id=est_id):
        raise NotFound()
    kind = data.get("kind") or ("CONTEUDO_INTERNO" if est_id else "ANUNCIO_EXTERNO")
    if kind == "CONTEUDO_INTERNO" and est_id is None:
        raise DomainError("ESTABLISHMENT_REQUIRED", "Conteúdo interno exige o estabelecimento.", status=400)
    media_type = data.get("media_type") or "IMAGEM"
    duration = int(data.get("duration_s") or 30)
    if duration not in (30, 60, 120):
        raise DomainError("DURATION_INVALID", "Duração deve ser 30, 60 ou 120 segundos.", status=400)

    async def exists(code: str) -> bool:
        return (await db.execute(select(DesignRequest.id).where(DesignRequest.code == code))).first() is not None

    r = DesignRequest(
        id=uuid.uuid4(), code=await generate_unique(lambda: f"ART-{random_safe(6)}", exists), org_id=org_id,
        establishment_id=est_id, kind=kind, title=data["title"].strip(), briefing=data["briefing"].strip(),
        texts=data.get("texts") or None, references=data.get("references") or None,
        orientation=data.get("orientation") or "HORIZONTAL", media_type=media_type, duration_s=duration,
        status=D.BRIEFING.value, requested_by=ctx.user_id, billing="A_DEFINIR", price_cents=0,
        payment_status="NAO_APLICAVEL", revisions_included=int(await get_setting(db, "design.included_revisions")),
        extra_revisions=0, revisions_used=0, due_at=data.get("due_at"), version=1,
    )
    db.add(r)
    await db.flush()
    await _event(db, ctx, r, None, "Pedido aberto")
    await audit(db, ctx, "design.request_created", "design_request", r.id,
                after={"code": r.code, "kind": kind, "org_id": org_id}, org_id=org_id)
    if worker and data.get("billing") in ("NEGOCIADO", "ISENTO", "COBRAR"):
        await set_quote(db, ctx, r.id, data["billing"], int(data.get("price_cents") or 0), r=r)
    await notify(db, [StaffRecipients("design.work")], "design.request_created", _vars(r),
                 dedupe_key=f"design.request_created:{r.id}", channels=["IN_APP"])
    await _publish_rt(r)
    return r


async def set_quote(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID, billing: str, price_cents: int,
                    *, r: Optional[DesignRequest] = None, note: Optional[str] = None) -> DesignRequest:
    _require_worker(ctx)
    r = r or await get_request(db, ctx, request_id, for_update=True)
    if r.status != D.BRIEFING.value:
        raise DomainError("DESIGN_INVALID_TRANSITION", "O orçamento só pode ser definido na etapa de briefing.",
                          status=409, details={"status": r.status})
    if billing not in ("COBRAR", "NEGOCIADO", "ISENTO"):
        raise DomainError("BILLING_INVALID", "Forma de cobrança inválida.", status=400)
    ensure_cents(price_cents, name="price_cents")
    if billing == "COBRAR" and price_cents <= 0:
        raise DomainError("PRICE_REQUIRED", "Informe o valor do serviço de criação.", status=400)
    if billing in ("NEGOCIADO", "ISENTO") and not (note or "").strip() and price_cents:
        raise DomainError("NOTE_REQUIRED", "Justifique o valor negociado/isento.", status=400)
    before = r.status
    r.billing, r.price_cents = billing, (price_cents if billing == "COBRAR" else 0)
    if billing == "COBRAR":
        from app.modules.designer import billing as bill

        r.payment_status = "PENDENTE"
        r.payment_order_id = await bill.create_charge(db, ctx, org_id=r.org_id, amount_cents=r.price_cents,
                                                      reference_type="design_request", reference_id=r.id,
                                                      description=f"Criação de arte {r.code}")
        r.status = D.AGUARDANDO_PAGAMENTO.value
    else:
        r.payment_status = "NAO_APLICAVEL"
        r.status = D.NA_FILA.value
    await _event(db, ctx, r, before, note or f"Orçamento: {billing}", {"price_cents": r.price_cents})
    await audit(db, ctx, "design.quoted", "design_request", r.id, before={"status": before},
                after={"status": r.status, "billing": billing, "price_cents": r.price_cents}, reason=note, org_id=r.org_id)
    await _publish_rt(r)
    return r


async def on_payment_confirmed(db: AsyncSession, request_id: uuid.UUID, payment_order_id: uuid.UUID) -> DesignRequest:
    """Callback do financeiro (referência ``design_request``) — idempotente."""
    r = (await db.execute(select(DesignRequest).where(DesignRequest.id == request_id).with_for_update())).scalar_one_or_none()
    if r is None:
        raise NotFound()
    if r.payment_status == "PAGO":
        return r
    if r.payment_order_id is not None and r.payment_order_id != payment_order_id:
        raise DomainError("PAYMENT_MISMATCH", "Pagamento não corresponde ao pedido de arte.", status=409)
    before = r.status
    r.payment_status = "PAGO"
    if r.status == D.AGUARDANDO_PAGAMENTO.value:
        r.status = D.NA_FILA.value
    await _event(db, None, r, before, "Pagamento confirmado", {"payment_order_id": str(payment_order_id)})
    await audit(db, None, "design.payment_confirmed", "design_request", r.id, after={"status": r.status},
                origin="system", org_id=r.org_id)
    await _publish_rt(r)
    return r


# ─── Fila do designer ────────────────────────────────────────────────────────

async def assign(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID, designer_id: Optional[uuid.UUID]) -> DesignRequest:
    from app.modules.notifications.service import StaffRecipients, resolve_recipients

    _require_worker(ctx)
    r = await get_request(db, ctx, request_id, for_update=True)
    target = designer_id or ctx.user_id
    if target != ctx.user_id and target not in await resolve_recipients(db, [StaffRecipients("design.work")]):
        raise DomainError("DESIGNER_INVALID", "O usuário indicado não é designer da equipe.", status=400)
    if r.status in (D.BRIEFING.value, D.AGUARDANDO_PAGAMENTO.value, D.CANCELADA.value):
        raise DomainError("DESIGN_INVALID_TRANSITION", "Pedido ainda não liberado para a fila (orçamento/pagamento).",
                          status=409, details={"status": r.status})
    before = r.status
    r.assigned_to = target
    if r.status in (D.NA_FILA.value, D.ALTERACAO_SOLICITADA.value):
        r.status = D.EM_DESIGN.value
    await _event(db, ctx, r, before, "Atribuído", {"designer_id": str(target)})
    await audit(db, ctx, "design.assigned", "design_request", r.id, before={"status": before, "assigned_to": None},
                after={"status": r.status, "assigned_to": target}, org_id=r.org_id)
    await _publish_rt(r)
    return r


async def queue(db: AsyncSession, ctx: AuthContext, *, mine: bool = False, status: Optional[str] = None) -> dict:
    _require_worker(ctx)
    active = [D.NA_FILA.value, D.EM_DESIGN.value, D.ALTERACAO_SOLICITADA.value, D.EM_REVISAO.value, D.APROVADA.value]
    cond = [DesignRequest.status.in_([status] if status else active)]
    if mine:
        cond.append(DesignRequest.assigned_to == ctx.user_id)
    rows = list((await db.execute(
        select(DesignRequest).where(*cond)
        .order_by(DesignRequest.due_at.asc().nulls_last(), DesignRequest.created_at.asc()).limit(500)
    )).scalars())
    counts = dict((await db.execute(
        select(DesignRequest.status, func.count()).where(DesignRequest.status.in_(active)).group_by(DesignRequest.status)
    )).all())
    return {"items": rows, "counts": {s: int(counts.get(s, 0)) for s in active},
            "unassigned": sum(1 for x in rows if x.assigned_to is None)}


# ─── Versões ─────────────────────────────────────────────────────────────────

async def _next_number(db: AsyncSession, request_id: uuid.UUID) -> int:
    return int((await db.execute(select(func.coalesce(func.max(DesignVersion.number), 0))
                                 .where(DesignVersion.request_id == request_id))).scalar_one()) + 1


async def add_version(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID, stored, *, notes: Optional[str],
                      source: str = "UPLOAD", template_id: Optional[uuid.UUID] = None) -> DesignVersion:
    """``stored``: arquivo já gravado no storage (``files.Stored``). Valida formato/dimensões."""
    from app.core.storage import get_storage
    from app.modules.designer.files import image_size
    from app.modules.notifications.service import notify

    _require_worker(ctx)
    r = await get_request(db, ctx, request_id, for_update=True)
    allowed_states = {D.EM_DESIGN.value, D.ALTERACAO_SOLICITADA.value, D.NA_FILA.value, D.EM_REVISAO.value,
                      D.APROVADA.value, D.PUBLICADA.value}
    if r.status not in allowed_states:
        await get_storage().delete(stored.key)
        raise DomainError("DESIGN_INVALID_TRANSITION", "Pedido não está em produção.", status=409,
                          details={"status": r.status})
    width = height = None
    try:
        if r.media_type == "IMAGEM":
            if stored.fmt not in ("png", "jpeg"):
                raise DomainError("FILE_TYPE_NOT_ALLOWED", "A arte de imagem deve ser PNG ou JPG.", status=415)
            data = await get_storage().read_bytes(stored.key, max_bytes=VERSION_LIMITS["IMAGEM"])
            width, height = image_size(data)
            if (width, height) != FHD[r.orientation]:
                raise DomainError("ART_RESOLUTION_INVALID",
                                  f"A arte deve ter {FHD[r.orientation][0]}×{FHD[r.orientation][1]} px "
                                  f"({'horizontal' if r.orientation == 'HORIZONTAL' else 'vertical'}).",
                                  status=400, details={"width": width, "height": height})
        else:
            if stored.fmt != "mp4":
                raise DomainError("FILE_TYPE_NOT_ALLOWED", "A arte em vídeo deve ser MP4.", status=415)
    except DomainError:
        await get_storage().delete(stored.key)
        raise
    except Exception:
        await get_storage().delete(stored.key)
        raise DomainError("FILE_CORRUPTED", "Arquivo de arte inválido ou corrompido.", status=400) from None

    # versões ainda em revisão são substituídas; a APROVADA (travada) permanece intacta
    pending = (await db.execute(select(DesignVersion).where(
        DesignVersion.request_id == r.id, DesignVersion.locked.is_(False),
        DesignVersion.status.in_(["EM_REVISAO", "ALTERACAO_SOLICITADA"])))).scalars()
    for old in pending:
        old.status = "SUBSTITUIDA"
    v = DesignVersion(id=uuid.uuid4(), request_id=r.id, number=await _next_number(db, r.id), source=source,
                      template_id=template_id, storage_key=stored.key, filename=stored.filename,
                      content_type=stored.content_type, size_bytes=stored.size, sha256=stored.sha256, width=width,
                      height=height, notes=notes, status="EM_REVISAO", locked=False, created_by=ctx.user_id)
    db.add(v)
    before = r.status
    if r.assigned_to is None:
        r.assigned_to = ctx.user_id
    r.status = D.EM_REVISAO.value
    r.version += 1
    await db.flush()
    note = "Nova versão para revisão"
    if before in (D.APROVADA.value, D.PUBLICADA.value):
        note = "Alteração após aprovação: nova versão exige nova aprovação"
    await _event(db, ctx, r, before, note, {"version": v.number, "sha256": v.sha256})
    await audit(db, ctx, "design.version_added", "design_request", r.id, before={"status": before},
                after={"status": r.status, "version": v.number, "sha256": v.sha256}, org_id=r.org_id)
    await notify(db, _client_recipients(r), "design.version_ready", _vars(r, version=v.number),
                 dedupe_key=f"design.version_ready:{v.id}", org_id=r.org_id)
    await _publish_rt(r)
    return v


async def get_version(db: AsyncSession, r: DesignRequest, version_id: uuid.UUID) -> DesignVersion:
    v = await db.get(DesignVersion, version_id)
    if v is None or v.request_id != r.id:
        raise NotFound()
    return v


async def decide(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID, version_id: uuid.UUID, decision: str,
                 comment: Optional[str]) -> DesignRequest:
    from app.modules.notifications.service import StaffRecipients, notify

    r = await get_request(db, ctx, request_id, for_update=True)
    if not can_client_decide(ctx, r):
        raise DomainError("FORBIDDEN", "Somente o cliente aprova a arte.", status=403,
                          details={"permission": "design.approve"})
    v = (await db.execute(select(DesignVersion).where(DesignVersion.id == version_id).with_for_update())).scalar_one_or_none()
    if v is None or v.request_id != r.id:
        raise NotFound()
    latest = await _next_number(db, r.id) - 1
    if r.status != D.EM_REVISAO.value or v.status != "EM_REVISAO" or v.number != latest:
        raise DomainError("DESIGN_INVALID_TRANSITION", "Esta versão não está aguardando a sua revisão.", status=409,
                          details={"status": r.status, "version_status": v.status})
    meta = current_request_meta()
    before = r.status
    recipients = [StaffRecipients("design.work")] if r.assigned_to is None else [r.assigned_to]
    if decision == "APROVAR":
        v.status, v.locked = "APROVADA", True
        r.status = D.APROVADA.value
        r.approved_version_id, r.approved_at = v.id, utcnow()
        db.add(DesignApproval(request_id=r.id, version_id=v.id, decision="APROVADA", comment=comment,
                              user_id=ctx.user_id, real_user_id=ctx.real_user_id, ip_address=meta.ip,
                              user_agent=(meta.user_agent or "")[:500] or None, version_sha256=v.sha256))
        await _event(db, ctx, r, before, "Aprovação formal do cliente", {"version": v.number, "sha256": v.sha256})
        await audit(db, ctx, "design.approved", "design_request", r.id, before={"status": before},
                    after={"status": r.status, "version": v.number, "sha256": v.sha256, "ip": meta.ip}, org_id=r.org_id)
        await notify(db, recipients + _client_recipients(r), "design.approved", _vars(r, version=v.number),
                     dedupe_key=f"design.approved:{v.id}", org_id=r.org_id)
    elif decision == "SOLICITAR_ALTERACAO":
        if not (comment and len(comment.strip()) >= 3):
            raise DomainError("COMMENT_REQUIRED", "Descreva a alteração desejada.", status=400)
        limit = await _limit(r)
        if r.revisions_used >= limit:
            raise DomainError(
                "DESIGN_REVISION_LIMIT_REACHED",
                f"As {r.revisions_included} revisões incluídas foram usadas. Solicite uma revisão extra.",
                status=409, details={"included": r.revisions_included, "extra": r.extra_revisions,
                                     "used": r.revisions_used},
            )
        r.revisions_used += 1
        v.status = "ALTERACAO_SOLICITADA"
        r.status = D.ALTERACAO_SOLICITADA.value
        db.add(DesignApproval(request_id=r.id, version_id=v.id, decision="ALTERACAO_SOLICITADA", comment=comment,
                              user_id=ctx.user_id, real_user_id=ctx.real_user_id, ip_address=meta.ip,
                              user_agent=(meta.user_agent or "")[:500] or None, version_sha256=v.sha256,
                              revision_number=r.revisions_used))
        db.add(DesignComment(request_id=r.id, version_id=v.id, author_id=ctx.user_id, body=comment.strip(),
                             is_internal=False))
        await _event(db, ctx, r, before, f"Alteração solicitada (revisão {r.revisions_used}/{limit})",
                     {"version": v.number})
        await audit(db, ctx, "design.changes_requested", "design_request", r.id, before={"status": before},
                    after={"status": r.status, "revisions_used": r.revisions_used, "limit": limit}, org_id=r.org_id)
        await notify(db, recipients, "design.changes_requested",
                     _vars(r, version=v.number, comment=comment.strip()[:300]),
                     dedupe_key=f"design.changes_requested:{v.id}", channels=["IN_APP", "EMAIL"])
        if r.revisions_used >= limit:
            await notify(db, _client_recipients(r), "design.revision_limit", _vars(r, included=r.revisions_included),
                         dedupe_key=f"design.revision_limit:{r.id}:{limit}", org_id=r.org_id)
    else:
        raise DomainError("DECISION_INVALID", "Decisão inválida.", status=400)
    r.version += 1
    await db.flush()
    await _publish_rt(r)
    return r


async def reopen(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID, comment: str) -> DesignRequest:
    """Alteração DEPOIS da aprovação. Cliente: consome revisão (mesmo limite). Equipe: correção
    interna sem consumir revisão. Em ambos os casos a versão aprovada continua travada e a
    próxima versão exige nova aprovação."""
    from app.modules.notifications.service import StaffRecipients, notify

    r = await get_request(db, ctx, request_id, for_update=True)
    if r.status not in (D.APROVADA.value, D.PUBLICADA.value):
        raise DomainError("DESIGN_INVALID_TRANSITION", "Somente artes aprovadas podem ser reabertas.", status=409,
                          details={"status": r.status})
    if not (comment and len(comment.strip()) >= 3):
        raise DomainError("COMMENT_REQUIRED", "Descreva a alteração desejada.", status=400)
    before = r.status
    if can_client_decide(ctx, r) and not is_worker(ctx):
        limit = await _limit(r)
        if r.revisions_used >= limit:
            raise DomainError("DESIGN_REVISION_LIMIT_REACHED",
                              f"As {r.revisions_included} revisões incluídas foram usadas. Solicite uma revisão extra.",
                              status=409, details={"included": r.revisions_included, "extra": r.extra_revisions,
                                                   "used": r.revisions_used})
        r.revisions_used += 1
        r.status = D.ALTERACAO_SOLICITADA.value
        db.add(DesignComment(request_id=r.id, version_id=r.approved_version_id, author_id=ctx.user_id,
                             body=comment.strip(), is_internal=False))
    elif is_worker(ctx):
        r.status = D.EM_DESIGN.value
        db.add(DesignComment(request_id=r.id, version_id=r.approved_version_id, author_id=ctx.user_id,
                             body=comment.strip(), is_internal=True))
    else:
        raise DomainError("FORBIDDEN", "Sem permissão para reabrir a arte.", status=403)
    r.version += 1
    await _event(db, ctx, r, before, "Reaberta após aprovação", {"revisions_used": r.revisions_used})
    await audit(db, ctx, "design.reopened", "design_request", r.id, before={"status": before},
                after={"status": r.status, "revisions_used": r.revisions_used}, reason=comment, org_id=r.org_id)
    await notify(db, [r.assigned_to] if r.assigned_to else [StaffRecipients("design.work")], "design.changes_requested",
                 _vars(r, version=0, comment=comment.strip()[:300]), dedupe_key=f"design.reopen:{r.id}:{r.version}",
                 channels=["IN_APP"])
    await _publish_rt(r)
    return r


# ─── Revisão extra ───────────────────────────────────────────────────────────

async def request_extra_revision(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID,
                                 reason: Optional[str]) -> DesignExtraRevision:
    from app.modules.notifications.service import StaffRecipients, notify

    r = await get_request(db, ctx, request_id, for_update=True)
    if not (can_client_decide(ctx, r) or ctx.can("design.request", org_id=r.org_id, establishment_id=r.establishment_id)):
        raise DomainError("FORBIDDEN", "Somente o cliente solicita revisão extra.", status=403)
    if await get_setting(db, "design.extra_revision_policy") == "BLOQUEAR":
        raise DomainError("DESIGN_EXTRA_REVISION_DISABLED", "Revisões extras não estão disponíveis.", status=409)
    if r.revisions_used < await _limit(r):
        raise DomainError("DESIGN_REVISIONS_AVAILABLE", "Ainda há revisões incluídas disponíveis.", status=409)
    open_ = (await db.execute(select(DesignExtraRevision.id).where(
        DesignExtraRevision.request_id == r.id,
        DesignExtraRevision.status.in_(["SOLICITADA", "AGUARDANDO_PAGAMENTO"])))).first()
    if open_:
        raise DomainError("DESIGN_EXTRA_REVISION_OPEN", "Já existe uma revisão extra em andamento.", status=409)
    x = DesignExtraRevision(request_id=r.id, status="SOLICITADA",
                            price_cents=int(await get_setting(db, "design.extra_revision_price_cents")),
                            requested_by=ctx.user_id, reason=reason)
    db.add(x)
    await db.flush()
    await _event(db, ctx, r, r.status, "Revisão extra solicitada", {"extra_revision_id": str(x.id)})
    await audit(db, ctx, "design.extra_revision_requested", "design_request", r.id, after={"extra_revision_id": x.id},
                org_id=r.org_id)
    await notify(db, [StaffRecipients("design.work")], "design.request_created",
                 _vars(r, title=f"{r.title} — revisão extra solicitada"), dedupe_key=f"design.extra:{x.id}",
                 channels=["IN_APP"])
    return x


async def decide_extra_revision(db: AsyncSession, ctx: AuthContext, extra_id: uuid.UUID, approve: bool,
                                price_cents: Optional[int], note: Optional[str]) -> DesignExtraRevision:
    _require_worker(ctx)
    x = (await db.execute(select(DesignExtraRevision).where(DesignExtraRevision.id == extra_id).with_for_update())).scalar_one_or_none()
    if x is None:
        raise NotFound()
    r = await get_request(db, ctx, x.request_id, for_update=True)
    if x.status != "SOLICITADA":
        raise DomainError("DESIGN_INVALID_TRANSITION", "Revisão extra já decidida.", status=409,
                          details={"status": x.status})
    x.decided_by, x.decided_at, x.decision_note = ctx.user_id, utcnow(), note
    if not approve:
        x.status = "RECUSADA"
    else:
        price = x.price_cents if price_cents is None else price_cents
        ensure_cents(price, name="price_cents")
        x.price_cents = price
        if price > 0:
            from app.modules.designer import billing as bill

            x.payment_order_id = await bill.create_charge(db, ctx, org_id=r.org_id, amount_cents=price,
                                                          reference_type="design_extra_revision", reference_id=x.id,
                                                          description=f"Revisão extra da arte {r.code}")
            x.status = "AGUARDANDO_PAGAMENTO"
        else:
            if not (note and note.strip()):
                raise DomainError("NOTE_REQUIRED", "Justifique a revisão extra sem custo.", status=400)
            await _release_extra(db, ctx, x, r)
    await audit(db, ctx, "design.extra_revision_decided", "design_request", r.id,
                after={"extra_revision_id": x.id, "status": x.status, "price_cents": x.price_cents}, reason=note,
                org_id=r.org_id)
    return x


async def _release_extra(db: AsyncSession, ctx: Any, x: DesignExtraRevision, r: DesignRequest) -> None:
    x.status = "LIBERADA"
    x.released_at = utcnow()
    r.extra_revisions += 1
    r.version += 1
    await _event(db, ctx, r, r.status, "Revisão extra liberada", {"extra_revision_id": str(x.id)})


async def on_extra_revision_paid(db: AsyncSession, extra_id: uuid.UUID, payment_order_id: uuid.UUID) -> DesignExtraRevision:
    x = (await db.execute(select(DesignExtraRevision).where(DesignExtraRevision.id == extra_id).with_for_update())).scalar_one_or_none()
    if x is None:
        raise NotFound()
    if x.status == "LIBERADA":
        return x
    if x.status != "AGUARDANDO_PAGAMENTO" or (x.payment_order_id and x.payment_order_id != payment_order_id):
        raise DomainError("PAYMENT_MISMATCH", "Pagamento não corresponde à revisão extra.", status=409)
    r = (await db.execute(select(DesignRequest).where(DesignRequest.id == x.request_id).with_for_update())).scalar_one()
    await _release_extra(db, None, x, r)
    return x


# ─── Comentários ─────────────────────────────────────────────────────────────

async def add_comment(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID, body: str,
                      version_id: Optional[uuid.UUID], is_internal: bool) -> DesignComment:
    r = await get_request(db, ctx, request_id)
    if is_internal and not is_worker(ctx):
        raise DomainError("FORBIDDEN", "Comentário interno é exclusivo da equipe RD.", status=403)
    if version_id is not None:
        await get_version(db, r, version_id)
    c = DesignComment(request_id=r.id, version_id=version_id, author_id=ctx.user_id, body=body.strip(),
                      is_internal=is_internal)
    db.add(c)
    await db.flush()
    await _publish_rt(r)
    return c


async def list_comments(db: AsyncSession, ctx: AuthContext, r: DesignRequest) -> list[DesignComment]:
    q = select(DesignComment).where(DesignComment.request_id == r.id).order_by(DesignComment.created_at)
    if not is_staff_viewer(ctx):
        q = q.where(DesignComment.is_internal.is_(False))
    return list((await db.execute(q)).scalars())


# ─── Publicação / cancelamento ───────────────────────────────────────────────

async def publish(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID):
    from app.core.storage import get_storage
    from app.modules.media.service import ingest_generated_file
    from app.modules.notifications.service import notify

    _require_worker(ctx)
    r = await get_request(db, ctx, request_id, for_update=True)
    if r.status != D.APROVADA.value or r.approved_version_id is None:
        raise DomainError("DESIGN_NOT_APPROVED", "Somente arte aprovada formalmente pelo cliente pode ser publicada.",
                          status=409, details={"status": r.status})
    v = await db.get(DesignVersion, r.approved_version_id)
    if v is None or not v.locked or v.status != "APROVADA":
        raise DomainError("DESIGN_NOT_APPROVED", "Versão aprovada inválida.", status=409)
    data = await get_storage().read_bytes(v.storage_key, max_bytes=VERSION_LIMITS["VIDEO"])
    import hashlib

    if hashlib.sha256(data).hexdigest() != v.sha256:
        raise DomainError("DESIGN_INTEGRITY", "O arquivo aprovado não confere com o registrado.", status=409)
    media = await ingest_generated_file(db, ctx, r.org_id, v.filename, v.content_type, data, "DESIGN_ART",
                                        name=f"{r.title} ({r.code} v{v.number})")
    before = r.status
    r.status = D.PUBLICADA.value
    r.published_media_id, r.published_version_id, r.published_at = media.id, v.id, utcnow()
    r.version += 1
    await _event(db, ctx, r, before, "Publicada na biblioteca de mídias", {"media_id": str(media.id)})
    await audit(db, ctx, "design.published", "design_request", r.id, before={"status": before},
                after={"status": r.status, "media_id": media.id, "version": v.number}, org_id=r.org_id)
    await notify(db, _client_recipients(r), "design.published", _vars(r), dedupe_key=f"design.published:{v.id}",
                 org_id=r.org_id)
    await _publish_rt(r)
    return r, media


async def cancel(db: AsyncSession, ctx: AuthContext, request_id: uuid.UUID, reason: str) -> DesignRequest:
    r = await get_request(db, ctx, request_id, for_update=True)
    if r.status in (D.CANCELADA.value, D.PUBLICADA.value):
        raise DomainError("DESIGN_INVALID_TRANSITION", "Pedido encerrado.", status=409, details={"status": r.status})
    if not is_worker(ctx):
        if not ctx.can("design.request", org_id=r.org_id, establishment_id=r.establishment_id):
            raise DomainError("FORBIDDEN", "Sem permissão para cancelar.", status=403)
        if r.status not in (D.BRIEFING.value, D.AGUARDANDO_PAGAMENTO.value):
            raise DomainError("DESIGN_CANCEL_NOT_ALLOWED",
                              "Após o início da produção, o cancelamento é tratado com a RD (suporte).", status=409)
    before = r.status
    r.status, r.cancel_reason = D.CANCELADA.value, reason
    r.version += 1
    await _event(db, ctx, r, before, reason)
    await audit(db, ctx, "design.cancelled", "design_request", r.id, before={"status": before},
                after={"status": r.status}, reason=reason, org_id=r.org_id)
    await _publish_rt(r)
    return r


async def add_attachment(db: AsyncSession, ctx: AuthContext, r: DesignRequest, kind: str, stored) -> DesignAttachment:
    if r.status in (D.CANCELADA.value, D.PUBLICADA.value):
        from app.core.storage import get_storage

        await get_storage().delete(stored.key)
        raise DomainError("DESIGN_INVALID_TRANSITION", "Pedido encerrado.", status=409)
    a = DesignAttachment(request_id=r.id, kind=kind, storage_key=stored.key, filename=stored.filename,
                         content_type=stored.content_type, size_bytes=stored.size, sha256=stored.sha256,
                         uploaded_by=ctx.user_id)
    db.add(a)
    await db.flush()
    await audit(db, ctx, "design.attachment_added", "design_request", r.id,
                after={"attachment_id": a.id, "kind": kind, "sha256": a.sha256, "size": a.size_bytes}, org_id=r.org_id)
    return a


# Callbacks de pagamento (financeiro → designer). Importado pela API (rotas) e pelo
# worker (app.workers.tasks.maintenance) para valer em todos os processos.
from app.modules.designer import billing as _billing  # noqa: E402

_billing.register_callbacks()
