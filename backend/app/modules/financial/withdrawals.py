"""
RD Mídia Indoor — Chaves PIX e saques/PIX OUT (ADR_009 itens 4–5; A5 D3.6, D4; P001 §86–§88; P002 item 11).

Chave PIX do estabelecimento:
- Só ``pix_keys.manage`` (admin/suporte autorizado) cria/troca; o estabelecimento não
  altera (403/404). Valor cifrado (AES-GCM, AAD = estabelecimento). Auditoria e
  notificação ao proprietário (``pix_key.changed``). A nova chave só vale após
  ``pix.key_cooldown_hours`` (12 h); a anterior é revogada na troca (saques
  bloqueados durante o período — decisão conservadora).
- Defesa em profundidade (REV — saque pago para chave revogada): (1) a troca/revogação da chave
  CANCELA todo saque do estabelecimento ainda não executado (``REQUESTED`` — manual, automático do
  dia 1 ou devolução de caução aprovada) com motivo ``PIX_KEY_CHANGED``, devolvendo a reserva ao
  saldo disponível, com auditoria e notificação; (2) ``execute_withdrawal`` revalida SOB LOCK, antes
  de qualquer chamada externa, que o destino é a chave VIGENTE, não revogada e fora do período de
  12 h — senão cancela sem chamar o gateway (sem retry automático).

Saque do estabelecimento (Financeiro → Saques; nunca na dashboard):
- ``withdrawals.request`` no escopo do estabelecimento + TOTP + senha financeira;
  SEM dupla aprovação. Lock por estabelecimento (advisory lock) ⇒ dois saques
  concorrentes do mesmo saldo: um falha. Saldo negativo (``EST:Receivable``) ⇒ 423.
- Reserva no ledger (Available → InTransit) na mesma transação; execução no worker.

Execução de PIX OUT (todas as origens) — ``execute_withdrawal``:
1. transação 1: REQUESTED → EXECUTING (gravado ANTES da chamada; aprovação → EXECUTING);
2. ``POST /withdrawals`` SEM retry (provedor não tem idempotência);
3. transação 2: aceito ⇒ PROCESSING/COMPLETED; 4xx ⇒ FAILED (reserva liberada);
   timeout/5xx ⇒ UNKNOWN + alerta + conciliação (nunca reenviar automaticamente).
Status final via ``GET /withdrawals/{external_id}`` (``refresh_withdrawal``).
"""
from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Callable, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.crypto import decrypt_str, encrypt_str, hmac_hex
from app.core.errors import Conflict, DomainError, Locked, NotFound
from app.core.money import ensure_cents
from app.core.settings_service import get_setting
from app.core.timeutils import isoformat_z, utcnow
from app.models.financial import (
    EstablishmentPixKey,
    FinancialApprovalEvent,
    FinancialApprovalRequest,
    PIX_KEY_TYPES,
    Withdrawal,
    WithdrawalKind as WK,
    WithdrawalStatus as WS,
)
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import common, ledger

log = structlog.get_logger("rd.financial.withdrawals")

_DIG = re.compile(r"\D")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RANDOM = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ─── Chave PIX ───────────────────────────────────────────────────────────────

_PHONE_KEY_ALLOWED = re.compile(r"^[0-9\s()+\-.]+$")


def normalize_pix_key(key_type: str, value: str) -> str:
    """Normaliza a chave PIX removendo SOMENTE máscara — nunca descarta letras/caracteres.

    CNPJ alfanumérico como chave: o formato de ``pix_key`` (tipo ``cnpj``) da CloudPayPag é
    documentado apenas por exemplo numérico ⇒ 422 ``PIX_KEY_CNPJ_ALPHANUMERIC_NOT_SUPPORTED``
    (BLOQUEADO POR DEPENDÊNCIA EXTERNA), em vez de enviar uma chave adulterada.
    """
    from app.modules.organizations.documents import is_valid_cnpj, strip_mask

    if key_type not in PIX_KEY_TYPES:
        raise DomainError("PIX_KEY_INVALID", "Tipo de chave PIX inválido.")
    v = (value or "").strip()
    if key_type == "cpf":
        v = strip_mask(v)
        ok = len(v) == 11 and v.isdigit() and v.isascii()
    elif key_type == "cnpj":
        v = strip_mask(v)
        if len(v) == 14 and not v.isdigit() and is_valid_cnpj(v):
            raise DomainError("PIX_KEY_CNPJ_ALPHANUMERIC_NOT_SUPPORTED",
                              "Chave PIX com CNPJ alfanumérico ainda não é aceita pelo meio de pagamento (CloudPayPag). "
                              "Cadastre outra chave (e-mail, telefone ou aleatória).", status=422,
                              details={"status": "BLOQUEADO POR DEPENDÊNCIA EXTERNA"})
        ok = len(v) == 14 and v.isdigit() and v.isascii()
    elif key_type == "email":
        v = v.lower()
        ok = bool(_EMAIL.match(v)) and len(v) <= 77
    elif key_type == "phone":
        ok = bool(_PHONE_KEY_ALLOWED.match(v))
        d = _DIG.sub("", v) if ok else ""
        if not d.startswith("55"):
            d = "55" + d
        v = "+" + d
        ok = ok and 12 <= len(d) <= 13
    else:
        v = v.lower()
        ok = bool(_RANDOM.match(v))
    if not ok:
        raise DomainError("PIX_KEY_INVALID", "Chave PIX inválida para o tipo informado.")
    return v


def mask_pix_key(key_type: str, v: str) -> str:
    if key_type in ("cpf", "cnpj"):
        return f"***{v[3:6]}***{v[-2:]}"
    if key_type == "email":
        local, _, dom = v.partition("@")
        return f"{local[:2]}***@{dom}"
    if key_type == "phone":
        return f"{v[:5]}*****{v[-2:]}"
    return f"{v[:4]}…{v[-4:]}"


def _key_aad(establishment_id: Any) -> str:
    return f"est:{establishment_id}"


def pix_key_public(k: EstablishmentPixKey, now=None) -> dict:
    now = now or utcnow()
    return {
        "id": str(k.id), "establishment_id": str(k.establishment_id), "key_type": k.key_type,
        "key_masked": k.key_masked, "holder_name": k.holder_name, "holder_document_masked": k.holder_document_masked,
        "active_from": k.active_from, "created_at": k.created_at, "revoked_at": k.revoked_at,
        "status": ("REVOKED" if k.revoked_at else ("ACTIVE" if k.active_from <= now else "COOLDOWN")),
    }


async def _establishment(db: AsyncSession, establishment_id: Any):
    from app.models.establishments import Establishment

    est = await db.get(Establishment, common.as_uuid(establishment_id))
    if est is None or getattr(est, "deleted_at", None) is not None:
        raise NotFound()
    return est


KEY_BOUND_KINDS = (WK.ESTABLISHMENT.value, WK.DEPOSIT.value)  # destino = chave PIX do estabelecimento
PIX_KEY_CHANGED = "PIX_KEY_CHANGED"


async def cancel_pending_for_key_change(db: AsyncSession, est: Any, *, cause: str) -> list[uuid.UUID]:
    """Cancela saques do estabelecimento ainda NÃO executados (``REQUESTED``) cujo destino é a chave
    PIX do estabelecimento. Chamar com o advisory lock ``est_wallet`` já obtido (mesma ordem de locks
    de ``execute_withdrawal``). A reserva volta ao saldo disponível (``fail``)."""
    rows = (await db.execute(
        select(Withdrawal).where(Withdrawal.establishment_id == est.id, Withdrawal.kind.in_(KEY_BOUND_KINDS),
                                 Withdrawal.status == WS.REQUESTED.value)
        .order_by(Withdrawal.requested_at, Withdrawal.id).with_for_update()
    )).scalars().all()
    for w in rows:
        await fail(db, w, f"{PIX_KEY_CHANGED}: {cause}; o valor voltou ao saldo — solicite novo saque após o "
                          "período de segurança da nova chave", final=WS.CANCELLED.value)
        common.publish_after_commit(db, "withdrawal.updated", withdrawal_public_event(w), org_id=est.org_id,
                                    establishment_id=est.id)
    return [w.id for w in rows]


async def set_pix_key(db: AsyncSession, ctx: Any, establishment_id: Any, *, key_type: str, key_value: str,
                      holder_name: str, holder_document: str, reason: str) -> EstablishmentPixKey:
    est = await _establishment(db, establishment_id)
    ctx.ensure("pix_keys.manage", org_id=est.org_id, establishment_id=est.id)
    if ctx.impersonation is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
    if not reason or len(reason.strip()) < 5:
        raise DomainError("REASON_REQUIRED", "Informe o motivo da alteração.")
    value = normalize_pix_key(key_type, key_value)
    from app.modules.organizations.documents import DocumentError, normalize_document

    try:  # titular: CPF ou CNPJ (inclusive alfanumérico) com dígito verificador; não vai à CloudPayPag
        doc, _doc_type = normalize_document(holder_document)
    except DocumentError:
        raise DomainError("PIX_HOLDER_DOCUMENT_INVALID", "CPF/CNPJ do titular inválido.") from None
    if not holder_name or len(holder_name.strip()) < 3:
        raise DomainError("PIX_HOLDER_NAME_INVALID", "Nome do titular inválido.")
    await ledger.advisory_lock(db, "est_wallet", est.id)
    now = utcnow()
    cur = (await db.execute(
        select(EstablishmentPixKey).where(EstablishmentPixKey.establishment_id == est.id,
                                          EstablishmentPixKey.revoked_at.is_(None)).with_for_update()
    )).scalar_one_or_none()
    before = pix_key_public(cur, now) if cur else None
    if cur is not None:
        cur.revoked_at, cur.revoked_by, cur.revoke_reason = now, ctx.user_id, "substituída"
        await db.flush()
    hours = int(await get_setting(db, "pix.key_cooldown_hours"))
    k = EstablishmentPixKey(
        id=uuid.uuid4(), establishment_id=est.id, key_type=key_type,
        key_value_enc=encrypt_str(value, purpose="pix_key", aad=_key_aad(est.id)),
        key_fingerprint=hmac_hex(f"{key_type}:{value}", purpose="pix_key_fp"), key_masked=mask_pix_key(key_type, value),
        holder_name=holder_name.strip()[:200], holder_document_enc=encrypt_str(doc, purpose="pix_key", aad=_key_aad(est.id)),
        holder_document_masked=f"***{doc[3:6]}***{doc[-2:]}", active_from=now + timedelta(hours=hours),
        created_by=ctx.user_id,
    )
    db.add(k)
    await db.flush()
    cancelled = await cancel_pending_for_key_change(db, est, cause="chave PIX alterada antes da execução do saque")
    await audit(db, ctx, "pix_key.changed", "establishment", est.id, org_id=est.org_id, reason=reason,
                before=before, after={**pix_key_public(k, now),
                                      "cancelled_withdrawals": [str(i) for i in cancelled]})
    from app.modules.notifications.service import EstablishmentRecipients

    await common.notify(db, [EstablishmentRecipients(est.id, "withdrawals.view")], "pix_key.changed",
                        {"blocked_until": isoformat_z(k.active_from)}, dedupe_key=f"pix_key.changed:{k.id}",
                        org_id=est.org_id, entity=("establishment", est.id))
    common.publish_after_commit(db, "withdrawal.updated", {"establishment_id": str(est.id), "pix_key": "changed"},
                                org_id=est.org_id, establishment_id=est.id)
    return k


async def revoke_pix_key(db: AsyncSession, ctx: Any, key_id: Any, reason: str) -> EstablishmentPixKey:
    k = await db.get(EstablishmentPixKey, common.as_uuid(key_id))
    if k is None:
        raise NotFound()
    est = await _establishment(db, k.establishment_id)
    ctx.ensure("pix_keys.manage", org_id=est.org_id, establishment_id=est.id)
    await ledger.advisory_lock(db, "est_wallet", est.id)  # mesma ordem de locks da troca/execução
    k = (await db.execute(select(EstablishmentPixKey).where(EstablishmentPixKey.id == k.id).with_for_update()
                          .execution_options(populate_existing=True))).scalar_one()
    if k.revoked_at is None:
        k.revoked_at, k.revoked_by, k.revoke_reason = utcnow(), ctx.user_id, (reason or "revogada")[:300]
        await db.flush()
        cancelled = await cancel_pending_for_key_change(db, est, cause="chave PIX revogada antes da execução do saque")
        await audit(db, ctx, "pix_key.revoked", "establishment", est.id, org_id=est.org_id, reason=reason,
                    after={**pix_key_public(k), "cancelled_withdrawals": [str(i) for i in cancelled]})
    return k


async def current_pix_key(db: AsyncSession, establishment_id: Any) -> Optional[EstablishmentPixKey]:
    return (await db.execute(
        select(EstablishmentPixKey).where(EstablishmentPixKey.establishment_id == common.as_uuid(establishment_id),
                                          EstablishmentPixKey.revoked_at.is_(None))
    )).scalar_one_or_none()


async def usable_pix_key(db: AsyncSession, establishment_id: Any) -> EstablishmentPixKey:
    k = await current_pix_key(db, establishment_id)
    if k is None:
        raise Conflict("PIX_KEY_REQUIRED", "Nenhuma chave PIX cadastrada. Solicite o cadastro ao suporte.")
    if k.active_from > utcnow():
        raise Locked("PIX_KEY_COOLDOWN", "Chave PIX recém-alterada: saques liberados após o período de segurança.",
                     details={"active_from": isoformat_z(k.active_from)})
    return k


def _pix_value(k: EstablishmentPixKey) -> str:
    return decrypt_str(k.key_value_enc, purpose="pix_key", aad=_key_aad(k.establishment_id))


def _destination(w: Withdrawal) -> str:
    if w.kind in (WK.ESTABLISHMENT.value, WK.DEPOSIT.value):
        return decrypt_str(w.destination_enc, purpose="pix_key", aad=_key_aad(w.establishment_id))
    return decrypt_str(w.destination_enc, purpose="pix_out", aad=f"wd:{w.id}")


def withdrawal_public(w: Withdrawal) -> dict:
    return {
        "id": str(w.id), "kind": w.kind, "origin": w.origin,
        "establishment_id": str(w.establishment_id) if w.establishment_id else None,
        "amount_cents": w.amount_cents, "sent_cents": w.sent_cents, "fee_cents": w.fee_cents, "net_cents": w.net_cents,
        "status": w.status, "destination_masked": w.destination_masked, "pix_key_type": w.pix_key_type,
        "provider_external_id": w.provider_external_id, "provider_status": w.provider_status,
        "failure_reason": w.failure_reason, "approval_request_id": str(w.approval_request_id) if w.approval_request_id else None,
        "requested_at": w.requested_at, "completed_at": w.completed_at,
    }


# ─── Saque do estabelecimento ────────────────────────────────────────────────

async def _est_account(db: AsyncSession, code: str, est: Any):
    return await ledger.account(db, code, owner_id=est.id, org_id=est.org_id)


async def create_establishment_withdrawal(db: AsyncSession, est: Any, amount_cents: int, *, requested_by: Any,
                                          idempotency_key: str, origin: str = "MANUAL", ctx: Any = None) -> Withdrawal:
    ensure_cents(amount_cents)
    if amount_cents <= 0:
        raise DomainError("WITHDRAWAL_AMOUNT_INVALID", "Valor do saque deve ser maior que zero.")
    await ledger.advisory_lock(db, "est_wallet", est.id)
    prev = (await db.execute(select(Withdrawal).where(Withdrawal.idempotency_key == idempotency_key))).scalar_one_or_none()
    if prev is not None:
        if prev.establishment_id != est.id or prev.amount_cents != amount_cents:
            raise Conflict("IDEMPOTENCY_KEY_REUSED", "Chave de idempotência já usada com outro conteúdo.")
        return prev
    if await ledger.balance_of(db, "EST:Receivable", est.id) > 0:
        raise Locked("WITHDRAWAL_BLOCKED_NEGATIVE_BALANCE",
                     "Saque bloqueado: há saldo negativo por estorno de comissão já sacada.")
    key = await usable_pix_key(db, est.id)
    available = await ledger.balance_of(db, "EST:CommissionPayable:Available", est.id)
    if available < amount_cents:
        raise Conflict("INSUFFICIENT_BALANCE", "Saldo disponível insuficiente.",
                       details={"available_cents": max(available, 0)})
    w = Withdrawal(
        id=uuid.uuid4(), kind=WK.ESTABLISHMENT.value, origin=origin, establishment_id=est.id, org_id=est.org_id,
        pix_key_id=key.id, pix_key_type=key.key_type, destination_enc=key.key_value_enc,
        destination_masked=key.key_masked, amount_cents=amount_cents, status=WS.REQUESTED.value,
        requested_by=requested_by, idempotency_key=idempotency_key,
    )
    db.add(w)
    await db.flush()
    await ledger.post(
        db, kind="WITHDRAWAL_RESERVE", idempotency_key=f"wd:{w.id}:reserve",
        legs=[(await _est_account(db, "EST:CommissionPayable:Available", est), amount_cents, "saque solicitado"),
              (await _est_account(db, "EST:CommissionPayable:InTransit", est), -amount_cents, "saque em andamento")],
        description=f"Reserva de saque {w.id}", created_by=requested_by, ref_type="withdrawal", ref_id=w.id,
        org_id=est.org_id,
    )
    await audit(db, ctx, "withdrawal.requested", "withdrawal", w.id, org_id=est.org_id,
                after={"amount_cents": amount_cents, "origin": origin, "destination": key.key_masked,
                       "establishment_id": str(est.id)}, origin=None if ctx is not None else "worker")
    from app.modules.notifications.service import EstablishmentRecipients

    await common.notify(db, [EstablishmentRecipients(est.id, "withdrawals.view")], "withdrawal.requested",
                        {"amount": common.brl(amount_cents)}, dedupe_key=f"withdrawal.requested:{w.id}",
                        org_id=est.org_id, entity=("withdrawal", w.id))
    common.publish_after_commit(db, "withdrawal.updated", withdrawal_public_event(w), org_id=est.org_id,
                                establishment_id=est.id)
    common.dispatch_after_commit(db, "app.workers.tasks.financial.execute_withdrawal", w.id)
    return w


def withdrawal_public_event(w: Withdrawal) -> dict:
    return {"id": str(w.id), "status": w.status, "amount_cents": w.amount_cents, "kind": w.kind}


def withdrawal_code_resource(establishment_id: Any, amount_cents: int) -> str:
    """O código por e-mail do saque fica vinculado ao estabelecimento E ao valor."""
    return f"{establishment_id}:{int(amount_cents)}"


async def send_withdrawal_code(db: AsyncSession, ctx: Any, establishment_id: Any, amount_cents: int) -> dict:
    from app.modules.financial import security

    est = await _establishment(db, establishment_id)
    ctx.ensure("withdrawals.request", org_id=est.org_id, establishment_id=est.id)
    ensure_cents(amount_cents)
    if amount_cents <= 0:
        raise DomainError("WITHDRAWAL_AMOUNT_INVALID", "Valor do saque deve ser maior que zero.")
    return await security.send_action_code(
        db, ctx, purpose="withdrawal", resource=withdrawal_code_resource(est.id, amount_cents),
        summary=f"saque de {common.brl(amount_cents)} ({est.name})")


async def request_withdrawal(db: AsyncSession, ctx: Any, establishment_id: Any, amount_cents: int, *,
                             totp_code: Optional[str], financial_password: Optional[str],
                             idempotency_key: Optional[str], email_code: Optional[str] = None) -> Withdrawal:
    from app.modules.financial import security

    est = await _establishment(db, establishment_id)
    ctx.ensure("withdrawals.request", org_id=est.org_id, establishment_id=est.id)
    if not idempotency_key:
        raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "Envie o header Idempotency-Key.")
    prev = (await db.execute(select(Withdrawal).where(Withdrawal.idempotency_key == idempotency_key))).scalar_one_or_none()
    if prev is not None:
        if prev.establishment_id != est.id or prev.amount_cents != amount_cents:
            raise Conflict("IDEMPOTENCY_KEY_REUSED", "Chave de idempotência já usada com outro conteúdo.")
        return prev
    await security.verify_step_up(db, ctx, totp_code=totp_code, financial_password=financial_password,
                                  purpose="withdrawal")
    await security.verify_action_code(ctx, purpose="withdrawal",
                                      resource=withdrawal_code_resource(est.id, amount_cents), code=email_code)
    return await create_establishment_withdrawal(db, est, amount_cents, requested_by=ctx.user_id,
                                                 idempotency_key=idempotency_key, origin="MANUAL", ctx=ctx)


async def simulate(db: AsyncSession, ctx: Any, establishment_id: Any, amount_cents: int) -> dict:
    est = await _establishment(db, establishment_id)
    ctx.ensure("withdrawals.request", org_id=est.org_id, establishment_id=est.id)
    ensure_cents(amount_cents)
    if amount_cents <= 0:
        raise DomainError("WITHDRAWAL_AMOUNT_INVALID", "Valor do saque deve ser maior que zero.")
    key = await usable_pix_key(db, est.id)
    available = await ledger.balance_of(db, "EST:CommissionPayable:Available", est.id)
    blocked = await ledger.balance_of(db, "EST:Receivable", est.id) > 0
    sim = await cpp.get_provider().simulate_withdrawal(amount_cents=amount_cents, pix_key=_pix_value(key),
                                                       pix_key_type=key.key_type)
    return {"amount_cents": amount_cents, "fee_cents": sim.fee_cents, "net_cents": sim.net_cents,
            "available_cents": available, "sufficient": available >= amount_cents and not blocked,
            "blocked_negative_balance": blocked, "provider_operational": sim.operational,
            "destination_masked": key.key_masked}


# ─── PIX OUT da RD (criado na aprovação dupla) ───────────────────────────────

def encrypt_destination(withdrawal_id: uuid.UUID, value: str) -> str:
    return encrypt_str(value, purpose="pix_out", aad=f"wd:{withdrawal_id}")


async def create_rd_pix_out(db: AsyncSession, *, kind: str, amount_cents: int, pix_key: str, pix_key_type: str,
                            approval: FinancialApprovalRequest, ref_type: Optional[str] = None,
                            ref_id: Any = None) -> Withdrawal:
    if kind == WK.ESTABLISHMENT.value:
        raise ValueError("use create_establishment_withdrawal")
    ensure_cents(amount_cents)
    value = normalize_pix_key(pix_key_type, pix_key)
    wid = uuid.uuid4()
    w = Withdrawal(
        id=wid, kind=kind, origin="MANUAL", pix_key_type=pix_key_type, destination_enc=encrypt_destination(wid, value),
        destination_masked=mask_pix_key(pix_key_type, value), amount_cents=amount_cents, status=WS.REQUESTED.value,
        approval_request_id=approval.id, ref_type=ref_type, ref_id=(str(ref_id) if ref_id else None),
        requested_by=approval.requested_by, idempotency_key=f"approval:{approval.id}",
    )
    db.add(w)
    await db.flush()
    common.dispatch_after_commit(db, "app.workers.tasks.financial.execute_withdrawal", w.id)
    return w


async def create_deposit_refund_pix_out(db: AsyncSession, *, establishment_id: Any, amount_cents: int,
                                       approval: FinancialApprovalRequest) -> Withdrawal:
    """Devolução de caução: PIX OUT para a chave ATIVA do estabelecimento (respeita as 12 h)."""
    est = await _establishment(db, establishment_id)
    key = await usable_pix_key(db, est.id)
    w = Withdrawal(
        id=uuid.uuid4(), kind=WK.DEPOSIT.value, origin="MANUAL", establishment_id=est.id, org_id=est.org_id,
        pix_key_id=key.id, pix_key_type=key.key_type, destination_enc=key.key_value_enc,
        destination_masked=key.key_masked, amount_cents=amount_cents, status=WS.REQUESTED.value,
        approval_request_id=approval.id, ref_type="deposit_refund", ref_id=str(approval.id),
        requested_by=approval.requested_by, idempotency_key=f"approval:{approval.id}",
    )
    db.add(w)
    await db.flush()
    common.dispatch_after_commit(db, "app.workers.tasks.financial.execute_withdrawal", w.id)
    return w


# ─── Execução (worker; sem retry) ────────────────────────────────────────────

SessionScope = Callable[[], Any]


@asynccontextmanager
async def _default_scope() -> AsyncIterator[AsyncSession]:
    from app.core.database import worker_session

    async with worker_session() as s:
        yield s


async def _approval_state(db: AsyncSession, w: Withdrawal, state: str, *, ref: Optional[str] = None,
                          error: Optional[str] = None) -> None:
    if w.approval_request_id is None:
        return
    req = await db.get(FinancialApprovalRequest, w.approval_request_id, with_for_update=True)
    if req is None or req.execution_state == state:
        return
    req.execution_state = state
    if ref:
        req.execution_ref = ref
    if error:
        req.execution_error = error[:500]
    if state == "EXECUTED":
        req.executed_at = utcnow()
    db.add(FinancialApprovalEvent(id=uuid.uuid4(), request_id=req.id, event=state, actor_user_id=None,
                                  data={"withdrawal_id": str(w.id), **({"error": error[:200]} if error else {})}))


async def _key_problem(db: AsyncSession, w: Withdrawal) -> Optional[str]:
    """Destino do saque ≠ chave PIX VIGENTE do estabelecimento ⇒ motivo (código: mensagem); senão None."""
    k = await db.get(EstablishmentPixKey, w.pix_key_id) if w.pix_key_id else None
    if k is None or k.establishment_id != w.establishment_id:
        return f"{PIX_KEY_CHANGED}: saque sem chave PIX cadastrada válida"
    if k.revoked_at is not None:
        return f"{PIX_KEY_CHANGED}: a chave PIX do saque foi revogada/substituída"
    cur = await current_pix_key(db, w.establishment_id)
    if cur is None or cur.id != k.id or w.destination_enc != k.key_value_enc:
        return f"{PIX_KEY_CHANGED}: o destino do saque não é a chave PIX vigente"
    if k.active_from > utcnow():
        return f"PIX_KEY_COOLDOWN: chave PIX em período de segurança até {isoformat_z(k.active_from)}"
    return None


async def execute_withdrawal(withdrawal_id: Any, *, scope: Optional[SessionScope] = None) -> str:
    """Executa um PIX OUT REQUESTED exatamente uma vez (sem retry automático)."""
    scope = scope or _default_scope
    wid = common.as_uuid(withdrawal_id)
    # 1) EXECUTING gravado antes da chamada externa
    async with scope() as db:
        pre = await db.get(Withdrawal, wid)
        if pre is None:
            raise NotFound()
        if pre.kind in KEY_BOUND_KINDS and pre.establishment_id is not None:
            # mesma ordem de locks da troca de chave (advisory do estabelecimento → linha do saque)
            await ledger.advisory_lock(db, "est_wallet", pre.establishment_id)
        w = (await db.execute(select(Withdrawal).where(Withdrawal.id == wid).with_for_update()
                              .execution_options(populate_existing=True))).scalar_one()
        if w.status != WS.REQUESTED.value:
            return w.status
        if w.kind in KEY_BOUND_KINDS:
            problem = await _key_problem(db, w)
            if problem is not None:  # NUNCA chama o gateway: cancela (reserva devolvida) — sem retry automático
                await fail(db, w, problem, final=WS.CANCELLED.value)
                await _alert(db, "Saque abortado antes do PIX OUT",
                             f"O saque {w.id} ({common.brl(w.amount_cents)}) não foi enviado: {problem}.", str(w.id))
                common.publish_after_commit(db, "withdrawal.updated", withdrawal_public_event(w),
                                            org_id=w.org_id, establishment_id=w.establishment_id)
                return w.status
        w.status = WS.EXECUTING.value
        w.executing_at = utcnow()
        await _approval_state(db, w, "EXECUTING")
        kind, amount, key_type = w.kind, w.amount_cents, w.pix_key_type
        destination = _destination(w)
    provider = cpp.get_provider()
    fee_added = 0
    outcome, result, error = "UNKNOWN", None, None
    try:
        if kind in (WK.REFUND.value, WK.PAYABLE.value, WK.DEPOSIT.value):  # RD arca a taxa: destinatário recebe o devido
            sim = await provider.simulate_withdrawal(amount_cents=amount, pix_key=destination, pix_key_type=key_type)
            fee_added = sim.fee_cents
        result = await provider.create_withdrawal(amount_cents=amount + fee_added, pix_key=destination,
                                                  pix_key_type=key_type)
        outcome = "ACCEPTED"
    except (cpp.ProviderTimeout, cpp.ProviderTemporaryError, cpp.ProviderUnavailable, cpp.ProviderProtocolError) as exc:
        outcome, error = "UNKNOWN", exc.code
    except cpp.ProviderError as exc:
        outcome, error = "FAILED", f"{exc.code}: {(exc.details or {}).get('provider_message') or ''}"[:300]
    except Exception as exc:  # erro inesperado APÓS EXECUTING: tratar como desconhecido (nunca repetir)
        outcome, error = "UNKNOWN", type(exc).__name__
    # 2) resultado
    async with scope() as db:
        w = await db.get(Withdrawal, wid, with_for_update=True)
        if outcome == "ACCEPTED":
            w.sent_cents = result.amount_cents
            w.provider_withdrawal_id = result.withdrawal_id
            w.provider_external_id = result.external_id
            w.provider_status = result.status
            w.fee_cents, w.net_cents = result.fee_cents, result.net_cents
            if result.status == "completed":
                w.status = WS.PROCESSING.value
                await db.flush()
                await settle(db, w)
            elif result.status in ("failed", "cancelled"):
                w.status = WS.PROCESSING.value
                await db.flush()
                await fail(db, w, result.failure_reason or result.status, final=WS.FAILED.value
                           if result.status == "failed" else WS.CANCELLED.value)
            else:
                w.status = WS.PROCESSING.value
        elif outcome == "FAILED":
            await fail(db, w, error or "recusado pela CloudPayPag")
        else:
            w.status = WS.UNKNOWN.value
            w.failure_reason = (error or "timeout")[:300]
            await _approval_state(db, w, "UNKNOWN", error=error)
            from app.modules.financial import reconciliation

            await reconciliation.open_item(db, "WITHDRAWAL_UNKNOWN", f"withdrawal:{w.id}", internal_ref=str(w.id),
                                           internal_amount_cents=w.amount_cents,
                                           detail={"kind": w.kind, "error": error,
                                                   "note": "resultado desconhecido; NÃO reenviar — conferir no painel "
                                                           "CloudPayPag e resolver por ajuste aprovado"})
            await _alert(db, "PIX OUT com resultado desconhecido",
                         f"O saque {w.id} ({common.brl(w.amount_cents)}) ficou sem resposta da CloudPayPag. "
                         "Não reenvie; confira na CloudPayPag.", str(w.id))
        await db.flush()
        common.publish_after_commit(db, "withdrawal.updated", withdrawal_public_event(w),
                                    **({"org_id": w.org_id, "establishment_id": w.establishment_id}
                                       if w.establishment_id else {}))
        return w.status


async def _alert(db: AsyncSession, title: str, message: str, ref: str) -> None:
    from app.modules.notifications.service import StaffRecipients

    await common.notify(db, [StaffRecipients("finance.reconcile")], "finance.execution_alert",
                        {"title": title, "message": message, "ref": ref}, dedupe_key=f"fin:alert:{ref}:{title}"[:255])


async def settle(db: AsyncSession, w: Withdrawal) -> None:
    """Saque concluído na CloudPayPag: baixa contábil conforme a origem."""
    if w.status == WS.COMPLETED.value:
        return
    cash = await ledger.account(db, "RD:Cash:CloudPayPag")
    fee = w.fee_cents or 0
    sent = w.sent_cents or w.amount_cents
    if w.kind == WK.ESTABLISHMENT.value:
        from app.models.establishments import Establishment

        est = await db.get(Establishment, w.establishment_id)
        legs = [(await _est_account(db, "EST:CommissionPayable:InTransit", est), w.amount_cents, "saque pago"),
                (cash, -sent, "PIX OUT")]
        if sent != w.amount_cents:
            legs.append((await ledger.account(db, "RD:Expense:GatewayFees"), w.amount_cents - sent, "diferença"))
    elif w.kind == WK.RD.value:
        legs = [(await ledger.account(db, "RD:Equity:Withdrawals"), sent - fee, "retirada dos sócios"),
                (await ledger.account(db, "RD:Expense:GatewayFees"), fee, "taxa de saque"), (cash, -sent, "PIX OUT")]
    elif w.kind == WK.DEPOSIT.value:
        from app.models.establishments import Establishment

        est = await db.get(Establishment, w.establishment_id)
        legs = [(await _est_account(db, "EST:DepositHeld", est), w.amount_cents, "caução devolvida"),
                (await ledger.account(db, "RD:Expense:GatewayFees"), sent - w.amount_cents, "taxa de PIX OUT (RD)"),
                (cash, -sent, "PIX OUT")]
    else:
        liab_code = "ADV:RefundPayable" if w.kind == WK.REFUND.value else "RD:AccountsPayable"
        if w.kind == WK.REFUND.value:
            from app.models.financial import Refund

            ref = await db.get(Refund, common.as_uuid(w.ref_id)) if w.ref_id else None
            liab = await ledger.account(db, liab_code, owner_id=ref.org_id, org_id=ref.org_id)
        else:
            liab = await ledger.account(db, liab_code)
        legs = [(liab, w.amount_cents, "pago"), (await ledger.account(db, "RD:Expense:GatewayFees"), sent - w.amount_cents,
                                                  "taxa de PIX OUT (RD)"), (cash, -sent, "PIX OUT")]
    await ledger.post(db, kind="WITHDRAWAL_SETTLE", idempotency_key=f"wd:{w.id}:settle", legs=legs,
                      description=f"PIX OUT concluído {w.id}", ref_type="withdrawal", ref_id=w.id, org_id=w.org_id,
                      approval_request_id=w.approval_request_id, external_provider="cloudpaypag",
                      external_ref=w.provider_external_id, meta={"fee_cents": fee, "sent_cents": sent})
    w.status = WS.COMPLETED.value
    w.completed_at = utcnow()
    if (w.net_cents is not None and w.kind in (WK.REFUND.value, WK.PAYABLE.value, WK.DEPOSIT.value)
            and w.net_cents != w.amount_cents):
        from app.modules.financial import reconciliation

        await reconciliation.open_item(db, "WITHDRAWAL_NET_MISMATCH", f"wdnet:{w.id}", internal_ref=str(w.id),
                                       external_ref=w.provider_external_id, internal_amount_cents=w.amount_cents,
                                       external_amount_cents=w.net_cents)
    await _approval_state(db, w, "EXECUTED", ref=w.provider_external_id)
    await _on_terminal(db, w)
    await audit(db, None, "withdrawal.completed", "withdrawal", w.id, org_id=w.org_id, origin="worker",
                after={"amount_cents": w.amount_cents, "fee_cents": fee, "external_id": w.provider_external_id})
    if w.kind == WK.ESTABLISHMENT.value:
        from app.modules.notifications.service import EstablishmentRecipients

        await common.notify(db, [EstablishmentRecipients(w.establishment_id, "withdrawals.view")], "withdrawal.paid",
                            {"amount": common.brl(w.amount_cents)}, dedupe_key=f"withdrawal.paid:{w.id}",
                            org_id=w.org_id, entity=("withdrawal", w.id))


async def fail(db: AsyncSession, w: Withdrawal, reason: str, *, final: str = WS.FAILED.value) -> None:
    """Falha definitiva (nenhum dinheiro saiu): libera a reserva do estabelecimento."""
    if w.status in (WS.FAILED.value, WS.CANCELLED.value, WS.COMPLETED.value):
        return
    if w.kind == WK.ESTABLISHMENT.value:
        from app.models.establishments import Establishment

        est = await db.get(Establishment, w.establishment_id)
        await ledger.post(
            db, kind="WITHDRAWAL_RELEASE", idempotency_key=f"wd:{w.id}:release",
            legs=[(await _est_account(db, "EST:CommissionPayable:InTransit", est), w.amount_cents, "saque não realizado"),
                  (await _est_account(db, "EST:CommissionPayable:Available", est), -w.amount_cents, "devolvido ao saldo")],
            description=f"Saque não realizado {w.id}", ref_type="withdrawal", ref_id=w.id, org_id=est.org_id,
        )
    w.status = final
    w.failure_reason = (reason or "")[:300]
    await _approval_state(db, w, "FAILED", error=reason)
    await _on_terminal(db, w)
    await audit(db, None, "withdrawal.failed", "withdrawal", w.id, org_id=w.org_id, origin="worker",
                after={"reason": (reason or "")[:200], "status": final})
    if w.kind == WK.ESTABLISHMENT.value:
        from app.modules.notifications.service import EstablishmentRecipients

        await common.notify(db, [EstablishmentRecipients(w.establishment_id, "withdrawals.view")], "withdrawal.failed",
                            {"amount": common.brl(w.amount_cents), "reason": (reason or "")[:120]},
                            dedupe_key=f"withdrawal.failed:{w.id}", org_id=w.org_id, entity=("withdrawal", w.id))


async def _on_terminal(db: AsyncSession, w: Withdrawal) -> None:
    if w.kind == WK.REFUND.value and w.ref_id:
        from app.modules.financial import refunds

        await refunds.on_payout_terminal(db, w)
    elif w.kind == WK.PAYABLE.value and w.ref_id:
        from app.modules.financial import accounting

        await accounting.on_payable_payout_terminal(db, w)


async def refresh_withdrawal(db: AsyncSession, w: Withdrawal) -> str:
    """Consulta ``GET /withdrawals/{external_id}`` e aplica o estado final (PROCESSING/UNKNOWN)."""
    if w.status not in (WS.PROCESSING.value, WS.UNKNOWN.value) or not w.provider_external_id:
        return w.status
    res = await cpp.get_provider().get_withdrawal(w.provider_external_id)
    w.provider_status = res.status
    if res.fee_cents is not None:
        w.fee_cents, w.net_cents = res.fee_cents, res.net_cents
    if res.status == "completed":
        if w.status == WS.UNKNOWN.value:
            w.status = WS.PROCESSING.value
            await db.flush()
        await settle(db, w)
    elif res.status in ("failed", "cancelled"):
        if w.status == WS.UNKNOWN.value:
            w.status = WS.PROCESSING.value
            await db.flush()
        await fail(db, w, res.failure_reason or res.status,
                   final=WS.FAILED.value if res.status == "failed" else WS.CANCELLED.value)
    await db.flush()
    return w.status


async def sweep_stuck_executing(db: AsyncSession, *, older_than: timedelta = timedelta(minutes=10)) -> int:
    """EXECUTING há mais de 10 min (worker caiu após o POST?) ⇒ UNKNOWN + alerta (sem reenvio)."""
    from app.modules.financial import reconciliation

    rows = (await db.execute(
        select(Withdrawal).where(Withdrawal.status == WS.EXECUTING.value,
                                 Withdrawal.executing_at < utcnow() - older_than).with_for_update(skip_locked=True)
    )).scalars().all()
    for w in rows:
        w.status = WS.UNKNOWN.value
        w.failure_reason = "execução interrompida (resultado desconhecido)"
        await _approval_state(db, w, "UNKNOWN", error="worker interrompido")
        await reconciliation.open_item(db, "WITHDRAWAL_UNKNOWN", f"withdrawal:{w.id}", internal_ref=str(w.id),
                                       internal_amount_cents=w.amount_cents, detail={"note": "EXECUTING expirado"})
        await _alert(db, "PIX OUT interrompido", f"O saque {w.id} ficou em execução sem resposta. Não reenvie.", str(w.id))
    return len(rows)


async def auto_payouts(db: AsyncSession) -> dict:
    """Dia 1: paga automaticamente quem tem disponível ≥ mínimo (abaixo acumula)."""
    from app.models.establishments import Establishment
    from app.models.financial import LedgerAccount
    from app.modules.financial import commission

    if not await get_setting(db, "commission.auto_payout_enabled"):
        return {"enabled": False, "created": 0}
    minimum = int(await get_setting(db, "commission.auto_payout_min_cents"))
    ids = [r[0] for r in (await db.execute(
        select(LedgerAccount.owner_id).where(LedgerAccount.code == "EST:CommissionPayable:Available")
    )).all()]
    created, skipped = 0, {}
    today = ledger.today_local().isoformat()
    pool_bps, _model = await commission.current_terms(db)
    for eid in ids:
        est = await db.get(Establishment, eid)
        if est is None:
            continue
        terms = await commission.effective_terms(db, eid, pool_bps)
        if not terms.auto_payout:
            skipped[str(eid)] = "auto_payout desativado"
            continue
        avail = await ledger.balance_of(db, "EST:CommissionPayable:Available", eid)
        if avail < minimum:
            skipped[str(eid)] = "abaixo do mínimo (acumula)"
            continue
        try:
            async with db.begin_nested():
                w = await create_establishment_withdrawal(db, est, avail, requested_by=None,
                                                          idempotency_key=f"auto:{eid}:{today}", origin="AUTOMATIC")
            if w.status != WS.REQUESTED.value:  # automático do dia já existia (ex.: cancelado pela troca de chave)
                skipped[str(eid)] = f"automático do dia já {w.status.lower()}"
                continue
            created += 1
        except DomainError as exc:
            skipped[str(eid)] = exc.code
    return {"enabled": True, "created": created, "skipped": skipped}
