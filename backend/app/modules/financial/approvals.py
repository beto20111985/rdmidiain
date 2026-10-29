"""
RD Mídia Indoor — Autorização financeira dos Admin Master: aprovação DUPLA × ÚNICA
(ADR_009 item 4; ADR_011 D2–D4/D10; SPEC 003 §4; A5 D3; P001 §83–§85; P002 item 10).

O modo efetivo da INSTÂNCIA (``app.modules.security.approval_mode``; trilha selada no
banco, nunca ENV) é gravado no pedido (``approval_mode``, snapshot imutável):

- **DUAL** (inalterado): Admin Master A SOLICITA (``create_request``) → PENDING (alerta ao
  outro sócio) → Admin Master B APROVA (``approve``) com TOTP (anti-replay) + senha
  financeira + código por e-mail → execução. Ninguém aprova o próprio pedido (validação +
  CHECK no banco). Com o 2º Master pendente/inativo nenhuma criação/aprovação passa
  (409 ``SECOND_MASTER_REQUIRED``).
- **SINGLE** (fluxo EXPLÍCITO, rota própria): o solicitante AUTORIZA o próprio pedido
  (``authorize``) com TOTP + senha financeira + código por e-mail (finalidade
  ``self_authorization``). ``approved_by`` fica NULL; ``self_authorized_by`` =
  solicitante (CHECK ``mode_consistent``). Nunca "A aprova como B".
- **Mudança de modo** (``APPROVAL_MODE_CHANGE``): SINGLE→DUAL por autorização única;
  DUAL→SINGLE: A solicita com step-up (TOTP + senha financeira) e B aprova com os três
  fatores. A execução chama ``approval_mode.apply_transition`` (função SECURITY DEFINER
  que revalida tudo no banco) e cancela os pendentes do modo antigo.

Serialização: toda criação/aprovação/autorização passa por
``ensure_financial_authorization_allowed`` (lock consultivo compartilhado; EXCLUSIVO para
``APPROVAL_MODE_CHANGE``) — que também verifica a integridade das trilhas seladas (falha ⇒
503 ``SECURITY_INTEGRITY_ALERT`` somente no dinheiro). Aprovação com
``SELECT … FOR UPDATE`` (duas aprovações concorrentes: uma vence). Payload/valor imutáveis
(trigger) e conferidos por SHA-256. Expira em ``finance.approval_expiry_hours`` (24 h).

Execução:
- Ações internas (despesa, ajuste, exceção de comissão, configuração financeira,
  resolução de conciliação, reembolso contábil, mudança de modo) executam NA MESMA
  transação da aprovação/autorização (atômico).
- PIX OUT (retirada da RD, reembolso, conta a pagar) cria ``withdrawals`` e é executado
  no worker: EXECUTING gravado antes da chamada; sem retry automático; timeout ⇒ UNKNOWN.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any, AsyncIterator, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.core.settings_service import REGISTRY, get_setting, set_setting
from app.core.timeutils import utcnow
from app.models.financial import (
    ApprovalAction as AA,
    ApprovalStatus as AS,
    EstablishmentCommissionSetting,
    ExecutionState as ES,
    FinancialApprovalEvent,
    FinancialApprovalRequest,
    PIX_KEY_TYPES,
    Payable,
    Refund,
    RefundStatus as RS,
    WithdrawalKind as WK,
)
from app.modules.financial import common, ledger, security

log = structlog.get_logger("rd.financial.approvals")

# Modos de aprovação da instância (SPEC 003 §2.1/§4) e ação de mudança de modo (§4.2).
SINGLE = "SINGLE"
DUAL = "DUAL"
APPROVAL_MODES = (SINGLE, DUAL)
MODE_CHANGE = "APPROVAL_MODE_CHANGE"
REASON_MIN = 10
MODE_CHANGE_REASON_MIN = 20
FACTORS = ["totp", "financial_password", "email"]

ACTION_LABELS = {
    AA.RD_WITHDRAWAL.value: "retirada (PIX OUT) da RD",
    AA.REFUND_PIX_OUT.value: "reembolso ao anunciante",
    AA.EXPENSE_CREATE.value: "despesa manual",
    AA.EXPENSE_UPDATE.value: "alteração de despesa",
    AA.PAYABLE_SETTLEMENT.value: "pagamento de conta a pagar",
    AA.LEDGER_ADJUSTMENT.value: "ajuste/crédito manual",
    AA.COMMISSION_ADJUSTMENT.value: "ajuste de comissão",
    AA.COMMISSION_EXCEPTION.value: "exceção de comissão de estabelecimento",
    AA.SETTING_CHANGE.value: "alteração de configuração financeira",
    AA.RECONCILIATION_ADJUSTMENT.value: "resolução de conciliação",
    AA.DEPOSIT_REFUND.value: "devolução de caução do parceiro",
    MODE_CHANGE: "mudança do modo de aprovação financeira",
}


def _int(v: Any, name: str, *, positive: bool = True, allow_zero: bool = False) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise DomainError("APPROVAL_PAYLOAD_INVALID", f"{name} deve ser inteiro (centavos).")
    if positive and (v < 0 or (v == 0 and not allow_zero)):
        raise DomainError("APPROVAL_PAYLOAD_INVALID", f"{name} inválido.")
    return v


def _pix(payload: dict, out: dict, wid_hint: Optional[str] = None) -> None:
    """Cifra a chave PIX de destino no payload (o valor nunca fica em claro no banco)."""
    from app.core.crypto import encrypt_str
    from app.modules.financial.withdrawals import mask_pix_key, normalize_pix_key

    t = payload.get("pix_key_type")
    if t not in PIX_KEY_TYPES:
        raise DomainError("PIX_KEY_INVALID", "Tipo de chave PIX inválido.")
    v = normalize_pix_key(t, str(payload.get("pix_key") or ""))
    out["pix_key_type"] = t
    out["pix_key_masked"] = mask_pix_key(t, v)
    out["pix_key_enc"] = encrypt_str(v, purpose="approval_pix", aad=wid_hint or "approval")


def _pix_value(payload: dict) -> str:
    from app.core.crypto import decrypt_str

    return decrypt_str(payload["pix_key_enc"], purpose="approval_pix", aad="approval")


def _legs_payload(legs: Any) -> list[dict]:
    if not isinstance(legs, list) or len(legs) < 2:
        raise DomainError("APPROVAL_PAYLOAD_INVALID", "Informe ao menos duas partidas.")
    out = []
    for leg in legs:
        code = str(leg.get("account_code") or "")
        ledger.account_def(code)
        amt = leg.get("amount_cents")
        if isinstance(amt, bool) or not isinstance(amt, int) or amt == 0:
            raise DomainError("APPROVAL_PAYLOAD_INVALID", "Partida com valor inválido.")
        out.append({"account_code": code, "owner_id": str(leg["owner_id"]) if leg.get("owner_id") else None,
                    "amount_cents": amt, "memo": (str(leg.get("memo"))[:200] if leg.get("memo") else None)})
    if sum(x["amount_cents"] for x in out) != 0:
        raise DomainError("LEDGER_UNBALANCED", "Ajuste desbalanceado (débitos ≠ créditos).")
    return out


def _mode_change_payload(p: dict, state: Any) -> dict:
    """``APPROVAL_MODE_CHANGE``: o cliente informa SOMENTE ``target_mode``; o servidor preenche
    ``from_mode``/``expected_version`` com o estado efetivo (conferidos de novo no banco)."""
    if state is None:  # pragma: no cover - create_request sempre informa o estado
        raise DomainError("APPROVAL_MODE_CHANGE_INVALID", "Mudança de modo inválida.")
    if set(p) != {"target_mode"} or p.get("target_mode") not in APPROVAL_MODES:
        raise DomainError("APPROVAL_MODE_CHANGE_INVALID",
                          "Mudança de modo inválida: informe somente o modo de destino (SINGLE ou DUAL).")
    if p["target_mode"] == state.mode:
        raise DomainError("APPROVAL_MODE_CHANGE_INVALID", "Mudança de modo inválida: a instância já usa este modo.",
                          details={"approval_mode": state.mode})
    return {"target_mode": p["target_mode"], "from_mode": state.mode, "expected_version": int(state.version)}


async def validate_payload(db: AsyncSession, action: str, payload: dict, *,
                           mode_state: Any = None) -> tuple[dict, Optional[int]]:
    """Valida/normaliza o payload; devolve (payload_canonico, valor_em_centavos)."""
    p = dict(payload or {})
    if action == MODE_CHANGE:
        return _mode_change_payload(p, mode_state), None
    if action == AA.RD_WITHDRAWAL.value:
        out = {"amount_cents": _int(p.get("amount_cents"), "amount_cents"),
               "purpose": str(p.get("purpose") or "retirada dos sócios")[:200]}
        _pix(p, out)
        return out, out["amount_cents"]
    if action == AA.REFUND_PIX_OUT.value:
        ref = await db.get(Refund, common.as_uuid(p.get("refund_id")))
        if ref is None:
            raise NotFound()
        if ref.status not in (RS.AWAITING_APPROVAL.value, RS.FAILED.value, RS.BLOCKED_EXTERNAL_DEPENDENCY.value):
            raise Conflict("REFUND_NOT_PAYABLE", "Reembolso não está aguardando aprovação/pagamento.")
        out = {"refund_id": str(ref.id), "await_provider_refund": bool(p.get("await_provider_refund"))}
        if not out["await_provider_refund"]:
            _pix(p, out)
        return out, ref.refund_cents
    if action == AA.EXPENSE_CREATE.value:
        from app.modules.financial.accounting import validate_expense_payload

        out = validate_expense_payload(p)
        if await ledger.is_closed(db, date.fromisoformat(out["competence_date"])):
            raise DomainError("LEDGER_PERIOD_CLOSED", "Competência em período fechado.", status=409)
        return out, out["amount_cents"]
    if action == AA.EXPENSE_UPDATE.value:
        pay = await db.get(Payable, common.as_uuid(p.get("payable_id")))
        if pay is None:
            raise NotFound()
        if pay.status != "OPEN":
            raise Conflict("PAYABLE_NOT_OPEN", "Somente contas em aberto podem ser alteradas.")
        out = {"payable_id": str(pay.id)}
        for k in ("description", "category", "competence_date", "due_date", "supplier_name", "supplier_document"):
            if p.get(k) is not None:
                out[k] = str(p[k])
        if p.get("amount_cents") is not None:
            out["amount_cents"] = _int(p["amount_cents"], "amount_cents")
        return out, out.get("amount_cents", pay.amount_cents)
    if action == AA.PAYABLE_SETTLEMENT.value:
        pay = await db.get(Payable, common.as_uuid(p.get("payable_id")))
        if pay is None:
            raise NotFound()
        if pay.status != "OPEN":
            raise Conflict("PAYABLE_NOT_OPEN", "Conta já paga/cancelada.")
        method = p.get("method")
        if method not in ("PIX_OUT", "EXTERNAL"):
            raise DomainError("APPROVAL_PAYLOAD_INVALID", "method deve ser PIX_OUT ou EXTERNAL.")
        out = {"payable_id": str(pay.id), "method": method}
        if method == "PIX_OUT":
            _pix(p, out)
        return out, pay.amount_cents
    if action in (AA.LEDGER_ADJUSTMENT.value, AA.RECONCILIATION_ADJUSTMENT.value):
        out: dict = {"description": str(p.get("description") or "")[:300]}
        if p.get("legs"):
            out["legs"] = _legs_payload(p["legs"])
        if p.get("competence_date"):
            d = date.fromisoformat(str(p["competence_date"]))
            if await ledger.is_closed(db, d):
                raise DomainError("LEDGER_PERIOD_CLOSED", "Competência em período fechado.", status=409)
            out["competence_date"] = d.isoformat()
        if action == AA.RECONCILIATION_ADJUSTMENT.value:
            from app.models.financial import ReconciliationItem

            item = await db.get(ReconciliationItem, common.as_uuid(p.get("item_id")))
            if item is None:
                raise NotFound()
            if item.status != "DIVERGENT":
                raise Conflict("RECONCILIATION_ITEM_RESOLVED", "Item já resolvido.")
            out["item_id"] = str(item.id)
            wr = p.get("withdrawal_resolution")
            if wr:
                if wr.get("outcome") not in ("COMPLETED", "FAILED"):
                    raise DomainError("APPROVAL_PAYLOAD_INVALID", "outcome deve ser COMPLETED ou FAILED.")
                out["withdrawal_resolution"] = {"withdrawal_id": str(common.as_uuid(wr["withdrawal_id"])),
                                                "outcome": wr["outcome"],
                                                "external_id": (str(wr["external_id"])[:80] if wr.get("external_id") else None),
                                                "fee_cents": wr.get("fee_cents")}
        elif "legs" not in out:
            raise DomainError("APPROVAL_PAYLOAD_INVALID", "Informe as partidas do ajuste.")
        amount = sum(x["amount_cents"] for x in out.get("legs", []) if x["amount_cents"] > 0) or 0
        return out, amount
    if action == AA.COMMISSION_ADJUSTMENT.value:
        from app.models.establishments import Establishment

        est = await db.get(Establishment, common.as_uuid(p.get("establishment_id")))
        if est is None:
            raise NotFound()
        amt = p.get("amount_cents")
        if isinstance(amt, bool) or not isinstance(amt, int) or amt == 0:
            raise DomainError("APPROVAL_PAYLOAD_INVALID", "amount_cents deve ser inteiro ≠ 0 (+ crédito / − débito).")
        return {"establishment_id": str(est.id), "amount_cents": amt,
                "description": str(p.get("description") or "")[:300]}, abs(amt)
    if action == AA.COMMISSION_EXCEPTION.value:
        from app.models.establishments import Establishment

        est = await db.get(Establishment, common.as_uuid(p.get("establishment_id")))
        if est is None:
            raise NotFound()
        out = {"establishment_id": str(est.id)}
        for k, lo, hi in (("rate_bps", 0, 10_000), ("weight_milli", 0, 100_000)):
            if p.get(k) is not None:
                v = _int(p[k], k, allow_zero=True)
                if not lo <= v <= hi:
                    raise DomainError("APPROVAL_PAYLOAD_INVALID", f"{k} fora da faixa.")
                out[k] = v
        for k in ("receives_commission", "auto_payout"):
            if p.get(k) is not None:
                out[k] = bool(p[k])
        out["valid_from"] = str(p.get("valid_from") or utcnow().isoformat())
        if p.get("valid_to"):
            out["valid_to"] = str(p["valid_to"])
        return out, None
    if action == AA.DEPOSIT_REFUND.value:
        return await _validate_deposit_refund(db, p)
    if action == AA.SETTING_CHANGE.value:
        key = str(p.get("key") or "")
        d = REGISTRY.get(key)
        if d is None:
            raise NotFound("SETTING_NOT_FOUND", "Configuração inexistente.")
        if not d.financial:
            raise DomainError("SETTING_NOT_FINANCIAL", "Configuração não financeira: altere pelo endpoint de configurações.")
        from app.core.settings_service import validate_value

        validate_value(d, p.get("value"))
        out = {"key": key, "value": p.get("value")}
        if p.get("expected_version") is not None:
            out["expected_version"] = _int(p["expected_version"], "expected_version", allow_zero=True)
        return out, None
    raise DomainError("APPROVAL_ACTION_INVALID", "Ação de aprovação desconhecida.")


async def deposit_available(db: AsyncSession, establishment_id: Any, *, exclude_request_id: Any = None) -> int:
    """Caução retida − devoluções em andamento (pedidos pendentes e PIX OUT não concluídos)."""
    held = await ledger.balance_of(db, "EST:DepositHeld", establishment_id)
    rows = (await db.execute(select(FinancialApprovalRequest).where(
        FinancialApprovalRequest.action_type == AA.DEPOSIT_REFUND.value,
        FinancialApprovalRequest.payload["establishment_id"].astext == str(establishment_id),
        FinancialApprovalRequest.status.in_((AS.PENDING.value, AS.APPROVED.value)),
    ))).scalars().all()
    inflight = 0
    for r in rows:
        if exclude_request_id is not None and r.id == exclude_request_id:
            continue
        p = r.payload or {}
        if r.status == AS.PENDING.value:
            inflight += int(p.get("refund_cents", 0)) + int(p.get("retained_cents", 0))
        elif r.execution_state in (ES.PENDING.value, ES.EXECUTING.value, ES.UNKNOWN.value):
            inflight += int(p.get("refund_cents", 0))  # a retenção já baixou a caução na aprovação
    return held - inflight


async def _validate_deposit_refund(db: AsyncSession, p: dict) -> tuple[dict, int]:
    from app.models.establishments import Establishment

    est = await db.get(Establishment, common.as_uuid(p.get("establishment_id")))
    if est is None:
        raise NotFound()
    refund = _int(p.get("refund_cents", 0), "refund_cents", allow_zero=True)
    retained = _int(p.get("retained_cents", 0), "retained_cents", allow_zero=True)
    if refund + retained <= 0:
        raise DomainError("DEPOSIT_REFUND_INVALID", "Informe o valor a devolver e/ou a reter.")
    damage = str(p.get("damage_description") or "").strip()
    if retained and len(damage) < 5:
        raise DomainError("DEPOSIT_DAMAGE_DESCRIPTION_REQUIRED", "Descreva o dano que justifica a retenção.")
    available = await deposit_available(db, est.id)
    if refund + retained > available:
        raise Conflict("DEPOSIT_REFUND_EXCEEDS_HELD", "Valor acima da caução retida disponível.",
                       details={"available_cents": max(available, 0)})
    return {"establishment_id": str(est.id), "refund_cents": refund, "retained_cents": retained,
            "damage_description": damage[:300] or None}, refund


def request_public(r: FinancialApprovalRequest, *, viewer_id: Any = None) -> dict:
    payload = {k: v for k, v in (r.payload or {}).items() if k != "pix_key_enc"}
    mode = r.approval_mode
    pending = r.status == AS.PENDING.value
    is_requester = viewer_id is not None and str(viewer_id) == str(r.requested_by)
    return {
        "id": str(r.id), "action_type": r.action_type, "action_label": ACTION_LABELS.get(r.action_type, r.action_type),
        "payload": payload, "payload_sha256": r.payload_sha256, "amount_cents": r.amount_cents, "reason": r.reason,
        "requested_by": str(r.requested_by), "requested_at": r.requested_at, "status": r.status,
        "approval_mode": mode, "is_mode_change": r.action_type == MODE_CHANGE,
        "approved_by": str(r.approved_by) if r.approved_by else None, "approved_at": r.approved_at,
        "self_authorized_by": str(r.self_authorized_by) if r.self_authorized_by else None,
        "self_authorized_at": r.self_authorized_at,
        "rejected_by": str(r.rejected_by) if r.rejected_by else None, "rejection_reason": r.rejection_reason,
        "expires_at": r.expires_at, "execution_state": r.execution_state, "execution_ref": r.execution_ref,
        "execution_error": r.execution_error, "executed_at": r.executed_at,
        "requires_email_code": needs_email_code(r),
        # DUAL: só o OUTRO Master aprova. SINGLE: só o solicitante autoriza (rota /authorize).
        "can_approve": mode == DUAL and viewer_id is not None and not is_requester and pending,
        "can_authorize": mode == SINGLE and is_requester and pending,
    }


def needs_email_code(req: FinancialApprovalRequest) -> bool:
    """Código por e-mail em TODA aprovação/autorização financeira (REQ-DUP-005; ETAPA2 #689; matriz C-32)."""
    return True


async def user_names(db: AsyncSession, ids) -> dict:
    """Somente NOMES (sem e-mail/telefone/segurança): entre Admin Master o outro aparece apenas por nome (P001 §84)."""
    from app.models.users import User

    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    rows = await db.execute(select(User.id, User.full_name).where(User.id.in_(list(ids))))
    return {uid: name for uid, name in rows}


async def requests_public(db: AsyncSession, reqs, *, viewer_id: Any = None) -> list[dict]:
    reqs = list(reqs)
    names = await user_names(db, {u for r in reqs
                                  for u in (r.requested_by, r.approved_by, r.rejected_by, r.self_authorized_by)})
    out = []
    for r in reqs:
        d = request_public(r, viewer_id=viewer_id)
        d["requested_by_name"] = names.get(r.requested_by)
        d["approved_by_name"] = names.get(r.approved_by) if r.approved_by else None
        d["self_authorized_by_name"] = names.get(r.self_authorized_by) if r.self_authorized_by else None
        d["rejected_by_name"] = names.get(r.rejected_by) if r.rejected_by else None
        out.append(d)
    return out


async def request_public_named(db: AsyncSession, r: FinancialApprovalRequest, *, viewer_id: Any = None) -> dict:
    return (await requests_public(db, [r], viewer_id=viewer_id))[0]


def _event(db: AsyncSession, req: FinancialApprovalRequest, event: str, actor: Any, data: Optional[dict] = None) -> None:
    db.add(FinancialApprovalEvent(id=uuid.uuid4(), request_id=req.id, event=event, actor_user_id=actor, data=data or {}))


# ─── Modo da instância: prontidão, integridade e erros do banco ──────────────

async def _authorization_state(db: AsyncSession, *, exclusive: bool) -> Any:
    """Lock consultivo (compartilhado; EXCLUSIVO para mudança de modo) + integridade das trilhas
    seladas + prontidão do modo (DUAL exige 2 Masters ativos). Devolve ``ModeState``.
    Erros: 503 ``INSTANCE_NOT_INITIALIZED``/``SECURITY_INTEGRITY_ALERT``; 409 ``SECOND_MASTER_REQUIRED``."""
    from app.modules.security import approval_mode

    return await approval_mode.ensure_financial_authorization_allowed(db, exclusive=exclusive)


# Exceções ``RD_<CODIGO>`` dos guards/funções do banco (SPEC 003 §2.4) que o motor converte no
# envelope do contrato (nunca 500). Mensagens/códigos vêm de ``approval_mode.map_db_error`` (B), para
# a API inteira falar a mesma língua; demais ``RD_*`` (ledger, imutabilidade…) seguem como antes.
_MAPPED_DB_CODES = frozenset({
    "RD_SECOND_MASTER_REQUIRED", "RD_APPROVAL_MODE_MISMATCH", "RD_APPROVER_NOT_ACTIVE_MASTER",
    "RD_SELF_AUTHORIZER_INVALID", "RD_REQUESTER_NOT_ACTIVE_MASTER", "RD_INSTANCE_NOT_INITIALIZED",
    "RD_SEAL_CHAIN_MISMATCH", "RD_SEAL_INVALID", "RD_APPROVAL_MODE_INVALID", "RD_APPROVAL_INVALID_STATE",
    "RD_MODE_CHANGE_INVALID", "RD_MODE_CHANGE_REUSED",
})


def map_db_error(exc: BaseException) -> Optional[DomainError]:
    """Converte a exceção ``RD_<CODIGO>`` de um guard/função do banco no erro do contrato (ou None)."""
    from app.modules.security import approval_mode

    db_code = approval_mode.rd_error_code(exc)
    if db_code not in _MAPPED_DB_CODES:
        return None
    err = approval_mode.map_db_error(exc)
    if err is None:  # pragma: no cover - todos os códigos acima estão no mapa do B
        return None
    if err.code == "SECOND_MASTER_REQUIRED":
        err.details = {"approval_mode": DUAL, **(err.details or {})}
    log.warning("financial_db_guard_refused", db_code=db_code, code=err.code)
    return err


@asynccontextmanager
async def _db_errors() -> AsyncIterator[None]:
    try:
        yield
    except DBAPIError as exc:
        mapped = map_db_error(exc)
        if mapped is None:
            raise
        raise mapped from None


def _mode_single() -> Conflict:
    return Conflict("APPROVAL_MODE_SINGLE", "Esta instalação usa aprovação financeira única.",
                    details={"approval_mode": SINGLE})


def _mode_dual() -> Conflict:
    return Conflict("APPROVAL_MODE_DUAL",
                    "Esta instalação usa dupla aprovação: outro Administrador Master precisa aprovar.",
                    details={"approval_mode": DUAL})


def _check_same_mode(req: FinancialApprovalRequest, state: Any) -> None:
    """O pedido nasceu num modo; só pode ser decidido nesse mesmo modo (e, se for mudança de modo,
    na mesma versão da trilha)."""
    stale = req.approval_mode != state.mode
    if not stale and req.action_type == MODE_CHANGE:
        stale = (req.payload or {}).get("expected_version") != int(state.version)
    if stale:
        raise Conflict("APPROVAL_MODE_CHANGED", "O modo de aprovação mudou; crie uma nova solicitação.",
                       details={"request_mode": req.approval_mode, "approval_mode": state.mode})


async def _peek_action(db: AsyncSession, request_id: Any) -> str:
    """``action_type`` (imutável por trigger) lido SEM lock, para escolher o lock consultivo."""
    a = (await db.execute(select(FinancialApprovalRequest.action_type).where(
        FinancialApprovalRequest.id == common.as_uuid(request_id)))).scalar_one_or_none()
    if a is None:
        raise NotFound()
    return a


# ─── Fluxo ───────────────────────────────────────────────────────────────────

async def create_request(db: AsyncSession, ctx: Any, action_type: str, payload: dict, reason: str, *,
                         idempotency_key: Optional[str] = None,
                         step_up: Optional[dict] = None) -> FinancialApprovalRequest:
    """Cria o pedido no modo EFETIVO da instância (snapshot ``approval_mode``).

    ``step_up`` (``{"totp_code", "financial_password"}``) é obrigatório para
    ``APPROVAL_MODE_CHANGE`` em DUAL (DUAL→SINGLE: o solicitante prova os fatores já na
    solicitação); nos demais casos é ignorado (a autorização/aprovação exige os 3 fatores)."""
    common.require_admin_master(ctx)
    is_mode_change = action_type == MODE_CHANGE
    state = await _authorization_state(db, exclusive=is_mode_change)
    if action_type not in ACTION_LABELS:
        raise DomainError("APPROVAL_ACTION_INVALID", "Ação de aprovação desconhecida.")
    min_reason = MODE_CHANGE_REASON_MIN if is_mode_change else REASON_MIN
    if not reason or len(reason.strip()) < min_reason:
        raise DomainError("REASON_REQUIRED", f"Motivo obrigatório (mínimo {min_reason} caracteres).")
    key = idempotency_key or uuid.uuid4().hex
    prev = (await db.execute(select(FinancialApprovalRequest).where(FinancialApprovalRequest.idempotency_key == key))).scalar_one_or_none()
    if prev is not None:
        if prev.requested_by != ctx.user_id or prev.action_type != action_type:
            raise Conflict("IDEMPOTENCY_KEY_REUSED", "Chave de idempotência já usada com outro conteúdo.")
        return prev
    canonical, amount = await validate_payload(db, action_type, payload, mode_state=state)
    if is_mode_change and state.mode == DUAL:
        su = step_up or {}
        await security.verify_step_up(db, ctx, totp_code=su.get("totp_code"),
                                      financial_password=su.get("financial_password"),
                                      purpose="approval_mode_change_request")
    hours = int(await get_setting(db, "finance.approval_expiry_hours"))
    req = FinancialApprovalRequest(
        id=uuid.uuid4(), action_type=action_type, payload=canonical, payload_sha256=common.canonical_sha256(canonical),
        amount_cents=amount, reason=reason.strip()[:2000], requested_by=ctx.user_id, status=AS.PENDING.value,
        expires_at=utcnow() + timedelta(hours=hours), execution_state=ES.NOT_STARTED.value, idempotency_key=key,
        approval_mode=state.mode,
    )
    db.add(req)
    async with _db_errors():
        await db.flush()
    _event(db, req, "CREATED", ctx.user_id, {"amount_cents": amount, "approval_mode": state.mode})
    after = {"action_type": action_type, "amount_cents": amount, "payload_sha256": req.payload_sha256,
             "approval_mode": state.mode}
    if is_mode_change:
        after.update({"target_mode": canonical["target_mode"], "from_mode": canonical["from_mode"],
                      "expected_version": canonical["expected_version"],
                      "factors": ["totp", "financial_password"] if state.mode == DUAL else []})
    await audit(db, ctx, "finance.approval_requested", "financial_approval_request", req.id, after=after, reason=reason)
    if state.mode == DUAL:  # alerta ao(s) outro(s) Master(s) — somente na dupla aprovação
        others = [u for u in await common.admin_master_ids(db) if u != ctx.user_id]
        if others:
            await common.notify(db, others, "finance.approval_requested",
                                {"requester": getattr(ctx.user, "full_name", "Admin Master"),
                                 "action": ACTION_LABELS[action_type],
                                 "amount": common.brl(amount) if amount is not None else "—", "reason": req.reason[:200],
                                 "request_id": str(req.id)}, dedupe_key=f"finance.approval_requested:{req.id}",
                                entity=("financial_approval_request", req.id))
    common.publish_after_commit(db, "finance.approval_request", {"id": str(req.id), "status": req.status,
                                                                 "action_type": action_type,
                                                                 "approval_mode": state.mode})
    return req


async def _lock(db: AsyncSession, request_id: Any) -> FinancialApprovalRequest:
    req = (await db.execute(select(FinancialApprovalRequest).where(
        FinancialApprovalRequest.id == common.as_uuid(request_id)).with_for_update()
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if req is None:
        raise NotFound()
    return req


def _check_pending(req: FinancialApprovalRequest) -> None:
    if req.status != AS.PENDING.value:
        raise Conflict("APPROVAL_NOT_PENDING", "Pedido não está pendente.", details={"status": req.status})
    if req.expires_at <= utcnow():
        raise Conflict("APPROVAL_EXPIRED", "Pedido expirado: crie uma nova solicitação.")


def _check_actionable(ctx: Any, req: FinancialApprovalRequest) -> None:
    """DUAL: pendente, não expirado e decidido por OUTRO Master."""
    _check_pending(req)
    if req.requested_by == ctx.user_id:
        raise DomainError("SELF_APPROVAL_FORBIDDEN", "Você não pode aprovar/rejeitar a própria solicitação.", status=403)


def _check_requester(ctx: Any, req: FinancialApprovalRequest) -> None:
    """SINGLE: somente o próprio solicitante autoriza (e pede o código por e-mail)."""
    if req.requested_by != ctx.user_id:
        raise DomainError("ONLY_REQUESTER_AUTHORIZES",
                          "Somente quem solicitou pode autorizar no modo de aprovação única.", status=403)


def _check_payload(req: FinancialApprovalRequest, payload_sha256: Optional[str]) -> None:
    if common.canonical_sha256(req.payload) != req.payload_sha256:
        log.error("approval_payload_tampered", request_id=str(req.id))
        raise Conflict("APPROVAL_PAYLOAD_TAMPERED", "Conteúdo do pedido não confere com o registrado (adulteração).")
    if not payload_sha256 or payload_sha256 != req.payload_sha256:
        raise Conflict("APPROVAL_PAYLOAD_MISMATCH", "O conteúdo aprovado difere do pedido atual. Recarregue e revise.")


async def request_email_code(db: AsyncSession, ctx: Any, request_id: Any) -> dict:
    """DUAL: só o OUTRO Master (finalidade ``approval``). SINGLE: só o solicitante
    (finalidade ``self_authorization``) — um código nunca serve para a outra finalidade/pedido."""
    common.require_admin_master(ctx)
    req = await _lock(db, request_id)
    if req.approval_mode == SINGLE:
        _check_requester(ctx, req)
        _check_pending(req)
        purpose, verb = "self_authorization", "autorização"
    else:
        _check_actionable(ctx, req)
        purpose, verb = "approval", "aprovação"
    amount = common.brl(req.amount_cents) if req.amount_cents is not None else ""
    out = await security.send_action_code(
        db, ctx, purpose=purpose, resource=str(req.id),
        summary=f"{verb} de {ACTION_LABELS.get(req.action_type, req.action_type)} {amount} (pedido {str(req.id)[:8]})")
    _event(db, req, "EMAIL_CODE_SENT", ctx.user_id, {"purpose": purpose})
    return out


async def approve(db: AsyncSession, ctx: Any, request_id: Any, *, totp_code: Optional[str],
                  financial_password: Optional[str], payload_sha256: Optional[str],
                  email_code: Optional[str] = None) -> FinancialApprovalRequest:
    """DUAL: o OUTRO Master aprova com TOTP + senha financeira + código por e-mail."""
    common.require_admin_master(ctx)
    state = await _authorization_state(db, exclusive=await _peek_action(db, request_id) == MODE_CHANGE)
    req = await _lock(db, request_id)
    if req.approval_mode == SINGLE:
        raise _mode_single()
    _check_actionable(ctx, req)
    _check_same_mode(req, state)
    _check_payload(req, payload_sha256)
    await security.verify_step_up(db, ctx, totp_code=totp_code, financial_password=financial_password,
                                  purpose=f"approval:{req.action_type}")
    await security.verify_action_code(ctx, purpose="approval", resource=str(req.id), code=email_code)
    req.status = AS.APPROVED.value
    req.approved_by = ctx.user_id
    req.approved_at = utcnow()
    _event(db, req, "APPROVED", ctx.user_id)
    async with _db_errors():
        await db.flush()
        await _execute(db, ctx, req)
    await audit(db, ctx, "finance.approval_approved", "financial_approval_request", req.id,
                after={"action_type": req.action_type, "amount_cents": req.amount_cents,
                       "execution_state": req.execution_state, "requested_by": str(req.requested_by),
                       "approval_mode": DUAL, "factors": list(FACTORS)})
    await _notify_decision(db, req, "aprovado")
    return req


async def authorize(db: AsyncSession, ctx: Any, request_id: Any, *, totp_code: Optional[str],
                    financial_password: Optional[str], payload_sha256: Optional[str],
                    email_code: Optional[str]) -> FinancialApprovalRequest:
    """SINGLE (aprovação única): o PRÓPRIO solicitante autoriza com TOTP + senha financeira +
    código por e-mail (finalidade ``self_authorization``). Fluxo explícito, nunca "aprovar como B":
    ``approved_by`` fica NULL e ``self_authorized_by`` = solicitante (CHECK no banco)."""
    common.require_admin_master(ctx)
    state = await _authorization_state(db, exclusive=await _peek_action(db, request_id) == MODE_CHANGE)
    req = await _lock(db, request_id)
    if req.approval_mode == DUAL:
        raise _mode_dual()
    _check_requester(ctx, req)
    _check_pending(req)
    _check_same_mode(req, state)
    _check_payload(req, payload_sha256)
    await security.verify_step_up(db, ctx, totp_code=totp_code, financial_password=financial_password,
                                  purpose=f"self_authorization:{req.action_type}")
    await security.verify_action_code(ctx, purpose="self_authorization", resource=str(req.id), code=email_code)
    req.status = AS.APPROVED.value
    req.self_authorized_by = ctx.user_id
    req.self_authorized_at = utcnow()
    _event(db, req, "SELF_AUTHORIZED", ctx.user_id, {"factors": list(FACTORS)})
    async with _db_errors():
        await db.flush()
        await _execute(db, ctx, req)
    await audit(db, ctx, "finance.self_authorized", "financial_approval_request", req.id,
                after={"action_type": req.action_type, "amount_cents": req.amount_cents,
                       "execution_state": req.execution_state, "factors": list(FACTORS), "approval_mode": SINGLE})
    common.publish_after_commit(db, "finance.approval_request", {"id": str(req.id), "status": req.status,
                                                                 "execution_state": req.execution_state,
                                                                 "approval_mode": SINGLE})
    return req


async def reject(db: AsyncSession, ctx: Any, request_id: Any, reason: str) -> FinancialApprovalRequest:
    """DUAL: o outro Master rejeita. SINGLE: não existe rejeição (o solicitante cancela)."""
    common.require_admin_master(ctx)
    req = await _lock(db, request_id)
    if req.approval_mode == SINGLE:
        raise _mode_single()
    _check_actionable(ctx, req)
    if not reason or len(reason.strip()) < 5:
        raise DomainError("REASON_REQUIRED", "Informe o motivo da rejeição.")
    req.status = AS.REJECTED.value
    req.rejected_by, req.rejected_at, req.rejection_reason = ctx.user_id, utcnow(), reason.strip()[:2000]
    _event(db, req, "REJECTED", ctx.user_id, {"reason": reason[:300]})
    await audit(db, ctx, "finance.approval_rejected", "financial_approval_request", req.id, reason=reason)
    await _notify_decision(db, req, "rejeitado")
    return req


async def cancel(db: AsyncSession, ctx: Any, request_id: Any) -> FinancialApprovalRequest:
    common.require_admin_master(ctx)
    req = await _lock(db, request_id)
    if req.requested_by != ctx.user_id:
        raise DomainError("ONLY_REQUESTER_CANCELS", "Somente o solicitante pode cancelar.", status=403)
    if req.status != AS.PENDING.value:
        raise Conflict("APPROVAL_NOT_PENDING", "Pedido não está pendente.")
    req.status = AS.CANCELLED.value
    req.cancelled_at = utcnow()
    _event(db, req, "CANCELLED", ctx.user_id)
    await audit(db, ctx, "finance.approval_cancelled", "financial_approval_request", req.id)
    return req


async def cancel_pending_for_mode_change(db: AsyncSession, *, exclude_id: Any, ctx: Any = None) -> int:
    """Na MESMA transação da troca de modo: todo pedido PENDENTE (do modo antigo) vira CANCELLED
    (evento ``CANCELLED_MODE_CHANGE``, auditoria, aviso ao solicitante). Chamado sob o lock
    consultivo EXCLUSIVO do modo — nenhuma aprovação/autorização atravessa a troca."""
    rows = (await db.execute(select(FinancialApprovalRequest).where(
        FinancialApprovalRequest.status == AS.PENDING.value,
        FinancialApprovalRequest.id != common.as_uuid(exclude_id))
        .order_by(FinancialApprovalRequest.id).with_for_update()
        .execution_options(populate_existing=True))).scalars().all()
    now = utcnow()
    actor = getattr(ctx, "user_id", None)
    for r in rows:
        r.status = AS.CANCELLED.value
        r.cancelled_at = now
        _event(db, r, "CANCELLED_MODE_CHANGE", actor, {"mode_change_request_id": str(exclude_id)})
        await audit(db, ctx, "finance.approval_cancelled_mode_change", "financial_approval_request", r.id,
                    after={"action_type": r.action_type, "approval_mode": r.approval_mode,
                           "mode_change_request_id": str(exclude_id)},
                    reason="modo de aprovação financeira alterado")
        await common.notify(db, [r.requested_by], "finance.approval_decided",
                            {"action": ACTION_LABELS.get(r.action_type, r.action_type),
                             "amount": common.brl(r.amount_cents) if r.amount_cents is not None else "—",
                             "decision": "cancelado (o modo de aprovação financeira mudou; crie uma nova solicitação)",
                             "request_id": str(r.id)},
                            dedupe_key=f"finance.approval_decided:{r.id}", entity=("financial_approval_request", r.id))
        common.publish_after_commit(db, "finance.approval_request", {"id": str(r.id), "status": r.status,
                                                                     "execution_state": r.execution_state})
    if rows:
        await db.flush()
    return len(rows)


async def expire_requests(db: AsyncSession) -> int:
    rows = (await db.execute(select(FinancialApprovalRequest).where(
        FinancialApprovalRequest.status == AS.PENDING.value, FinancialApprovalRequest.expires_at <= utcnow())
        .with_for_update(skip_locked=True))).scalars().all()
    for r in rows:
        r.status = AS.EXPIRED.value
        _event(db, r, "EXPIRED", None)
        await audit(db, None, "finance.approval_expired", "financial_approval_request", r.id, origin="worker")
    return len(rows)


async def _notify_decision(db: AsyncSession, req: FinancialApprovalRequest, decision: str) -> None:
    await common.notify(db, [req.requested_by], "finance.approval_decided",
                        {"action": ACTION_LABELS.get(req.action_type, req.action_type),
                         "amount": common.brl(req.amount_cents) if req.amount_cents is not None else "—",
                         "decision": decision, "request_id": str(req.id)},
                        dedupe_key=f"finance.approval_decided:{req.id}", entity=("financial_approval_request", req.id))
    common.publish_after_commit(db, "finance.approval_request", {"id": str(req.id), "status": req.status,
                                                                 "execution_state": req.execution_state})


# ─── Execução ────────────────────────────────────────────────────────────────

def authorization_meta(approval: Any, authorizer_id: Any) -> dict:
    """``meta`` dos lançamentos gerados na execução: modo + solicitante + quem autorizou
    (DUAL: o outro Master; SINGLE: o próprio solicitante)."""
    return {"authorization_mode": getattr(approval, "approval_mode", None) or DUAL,
            "requested_by": str(approval.requested_by), "authorized_by": str(authorizer_id)}


def _mode_label(req: FinancialApprovalRequest) -> str:
    return "aprovação única" if req.approval_mode == SINGLE else "dupla aprovação"


async def post_manual_legs(db: AsyncSession, legs: list[dict], *, kind: str, key: str, description: str, approval: Any,
                           approver_id: Any, competence_date: Optional[str] = None) -> ledger.PostResult:
    resolved = []
    for leg in legs:
        d = ledger.account_def(leg["account_code"])
        acc = await ledger.account(db, leg["account_code"], owner_id=leg.get("owner_id"),
                                   org_id=await _owner_org(db, d.owner_type, leg.get("owner_id")))
        resolved.append((acc, int(leg["amount_cents"]), leg.get("memo")))
    return await ledger.post(db, kind=kind, idempotency_key=key, legs=resolved, description=description,
                             competence_date=date.fromisoformat(competence_date) if competence_date else None,
                             shift_if_closed=competence_date is None, approval_request_id=approval.id,
                             created_by=approver_id, ref_type="financial_approval_request", ref_id=approval.id,
                             meta=authorization_meta(approval, approver_id))


async def _owner_org(db: AsyncSession, owner_type: str, owner_id: Any) -> Optional[uuid.UUID]:
    if not owner_id:
        return None
    if owner_type == "ESTABLISHMENT":
        from app.models.establishments import Establishment

        est = await db.get(Establishment, common.as_uuid(owner_id))
        return getattr(est, "org_id", None)
    if owner_type == "ADVERTISER":
        return common.as_uuid(owner_id)
    return None


async def _execute(db: AsyncSession, ctx: Any, req: FinancialApprovalRequest) -> None:
    from app.modules.financial import withdrawals

    a, p, uid = req.action_type, req.payload, ctx.user_id
    if a == MODE_CHANGE:
        from app.modules.security import approval_mode

        # Função SECURITY DEFINER revalida no banco (pedido APPROVED/NOT_STARTED, versão, aprovador ou
        # autorizador Master ATIVO, evento gravado, pedido não reutilizado) ANTES de qualquer efeito aqui.
        new_state = await approval_mode.apply_transition(db, ctx, req)
        await cancel_pending_for_mode_change(db, exclude_id=req.id, ctx=ctx)
        req.execution_state, req.execution_ref = ES.EXECUTED.value, f"mode_version:{new_state.version}"
        req.executed_at = utcnow()
    elif a == AA.RD_WITHDRAWAL.value:
        w = await withdrawals.create_rd_pix_out(db, kind=WK.RD.value, amount_cents=p["amount_cents"],
                                                pix_key=_pix_value(p), pix_key_type=p["pix_key_type"], approval=req,
                                                ref_type="rd_withdrawal")
        req.execution_state, req.execution_ref = ES.PENDING.value, str(w.id)
    elif a == AA.REFUND_PIX_OUT.value:
        from app.modules.financial import refunds

        ref = await db.get(Refund, common.as_uuid(p["refund_id"]), with_for_update=True)
        if ref.status == RS.AWAITING_APPROVAL.value:
            await refunds.apply_refund(db, ref, req, uid)
        ref.approval_request_id = req.id
        if p.get("await_provider_refund"):
            ref.status = RS.BLOCKED_EXTERNAL_DEPENDENCY.value
            req.execution_state = ES.BLOCKED_EXTERNAL.value
            req.execution_error = "Estorno pela API CloudPayPag indisponível (dependência externa)."
        else:
            w = await withdrawals.create_rd_pix_out(db, kind=WK.REFUND.value, amount_cents=ref.refund_cents,
                                                    pix_key=_pix_value(p), pix_key_type=p["pix_key_type"],
                                                    approval=req, ref_type="refund", ref_id=ref.id)
            w.org_id = ref.org_id
            ref.status, ref.withdrawal_id = RS.PAYING.value, w.id
            req.execution_state, req.execution_ref = ES.PENDING.value, str(w.id)
    elif a == AA.EXPENSE_CREATE.value:
        from app.modules.financial import accounting

        pay = await accounting.create_expense(db, p, req, uid)
        req.execution_state, req.execution_ref, req.executed_at = ES.EXECUTED.value, str(pay.id), utcnow()
    elif a == AA.EXPENSE_UPDATE.value:
        from app.modules.financial import accounting

        pay = await accounting.update_expense(db, p, req, uid)
        req.execution_state, req.execution_ref, req.executed_at = ES.EXECUTED.value, str(pay.id), utcnow()
    elif a == AA.PAYABLE_SETTLEMENT.value:
        from app.modules.financial import accounting

        pay = await db.get(Payable, common.as_uuid(p["payable_id"]), with_for_update=True)
        if pay is None or pay.status != "OPEN":
            raise Conflict("PAYABLE_NOT_OPEN", "Conta já paga/cancelada.")
        pay.settlement_approval_id = req.id
        if p["method"] == "EXTERNAL":
            await accounting.settle_payable_external(db, pay, req, uid)
            req.execution_state, req.execution_ref, req.executed_at = ES.EXECUTED.value, str(pay.id), utcnow()
        else:
            w = await withdrawals.create_rd_pix_out(db, kind=WK.PAYABLE.value, amount_cents=pay.amount_cents,
                                                    pix_key=_pix_value(p), pix_key_type=p["pix_key_type"],
                                                    approval=req, ref_type="payable", ref_id=pay.id)
            req.execution_state, req.execution_ref = ES.PENDING.value, str(w.id)
    elif a == AA.LEDGER_ADJUSTMENT.value:
        res = await post_manual_legs(db, p["legs"], kind="ADJUSTMENT", key=f"adjustment:{req.id}",
                                     description=f"Ajuste aprovado: {p.get('description') or req.reason}"[:500],
                                     approval=req, approver_id=uid, competence_date=p.get("competence_date"))
        req.execution_state, req.execution_ref, req.executed_at = ES.EXECUTED.value, str(res.transaction_id), utcnow()
    elif a == AA.RECONCILIATION_ADJUSTMENT.value:
        from app.modules.financial import reconciliation

        out = await reconciliation.resolve_with_approval(db, p["item_id"], req, p, uid)
        req.execution_state, req.execution_ref, req.executed_at = ES.EXECUTED.value, out["item_id"], utcnow()
    elif a == AA.COMMISSION_ADJUSTMENT.value:
        res = await _commission_adjustment(db, req, uid)
        req.execution_state, req.execution_ref, req.executed_at = ES.EXECUTED.value, str(res.transaction_id), utcnow()
    elif a == AA.COMMISSION_EXCEPTION.value:
        row = await _commission_exception(db, req, uid)
        req.execution_state, req.execution_ref, req.executed_at = ES.EXECUTED.value, str(row.id), utcnow()
    elif a == AA.DEPOSIT_REFUND.value:
        await _deposit_refund(db, req, uid)
    elif a == AA.SETTING_CHANGE.value:
        await set_setting(db, ctx, p["key"], p["value"], reason=f"{_mode_label(req)} {req.id}: {req.reason}"[:500],
                          expected_version=p.get("expected_version"), authorized_request=req)
        req.execution_state, req.execution_ref, req.executed_at = ES.EXECUTED.value, p["key"], utcnow()
    else:  # pragma: no cover
        raise DomainError("APPROVAL_ACTION_INVALID", "Ação desconhecida.")
    _event(db, req, f"EXECUTION_{req.execution_state}", uid)
    await db.flush()


async def _commission_adjustment(db: AsyncSession, req: FinancialApprovalRequest, uid: Any) -> ledger.PostResult:
    from app.models.establishments import Establishment

    p = req.payload
    est = await db.get(Establishment, common.as_uuid(p["establishment_id"]))
    exp = await ledger.account(db, "RD:Expense:Commission")
    avail = await ledger.account(db, "EST:CommissionPayable:Available", owner_id=est.id, org_id=est.org_id)
    amt = int(p["amount_cents"])
    if amt > 0:
        legs = [(exp, amt, "ajuste de comissão (crédito)"), (avail, -amt, "crédito ao estabelecimento")]
    else:
        debit = -amt
        bal = max(await ledger.balance_of(db, "EST:CommissionPayable:Available", est.id), 0)
        take = min(bal, debit)
        legs = [(exp, -debit, "ajuste de comissão (débito)")]
        if take:
            legs.append((avail, take, "débito do saldo"))
        if debit - take:
            recv = await ledger.account(db, "EST:Receivable", owner_id=est.id, org_id=est.org_id)
            legs.append((recv, debit - take, "saldo negativo"))
    return await ledger.post(db, kind="COMMISSION_ADJUSTMENT", idempotency_key=f"commission:adjust:{req.id}",
                             legs=legs, description=f"Ajuste de comissão: {p.get('description') or req.reason}"[:500],
                             approval_request_id=req.id, created_by=uid, ref_type="establishment", ref_id=est.id,
                             org_id=est.org_id)


async def _commission_exception(db: AsyncSession, req: FinancialApprovalRequest, uid: Any) -> EstablishmentCommissionSetting:
    from datetime import datetime

    p = req.payload
    eid = common.as_uuid(p["establishment_id"])
    vf = datetime.fromisoformat(p["valid_from"].replace("Z", "+00:00"))
    vt = datetime.fromisoformat(p["valid_to"].replace("Z", "+00:00")) if p.get("valid_to") else None
    prev = (await db.execute(select(EstablishmentCommissionSetting).where(
        EstablishmentCommissionSetting.establishment_id == eid,
        (EstablishmentCommissionSetting.valid_to.is_(None)) | (EstablishmentCommissionSetting.valid_to > vf),
        EstablishmentCommissionSetting.valid_from < vf).with_for_update())).scalars().all()
    for r in prev:
        r.valid_to = vf
    row = EstablishmentCommissionSetting(
        id=uuid.uuid4(), establishment_id=eid, receives_commission=p.get("receives_commission"),
        rate_bps=p.get("rate_bps"), weight_milli=p.get("weight_milli"), auto_payout=p.get("auto_payout", True),
        valid_from=vf, valid_to=vt, approved_by=uid, approval_request_id=req.id, reason=req.reason,
    )
    db.add(row)
    await db.flush()
    return row


async def _deposit_refund(db: AsyncSession, req: FinancialApprovalRequest, uid: Any) -> None:
    """Retenção (dano) vira receita de serviço já na aprovação; devolução sai por PIX OUT no worker."""
    from app.models.establishments import Establishment
    from app.modules.financial import withdrawals

    p = req.payload
    est = await db.get(Establishment, common.as_uuid(p["establishment_id"]))
    await ledger.advisory_lock(db, "est_wallet", est.id)
    held = await ledger.balance_of(db, "EST:DepositHeld", est.id)
    others = held - await deposit_available(db, est.id, exclude_request_id=req.id)
    if p["refund_cents"] + p["retained_cents"] > held - others:
        raise Conflict("DEPOSIT_REFUND_EXCEEDS_HELD", "Caução retida insuficiente.", details={"held_cents": held})
    if p["retained_cents"]:
        dep = await ledger.account(db, "EST:DepositHeld", owner_id=est.id, org_id=est.org_id)
        svc = await ledger.account(db, "RD:Revenue:Services")
        await ledger.post(db, kind="DEPOSIT_RETENTION", idempotency_key=f"deposit:{req.id}:retained",
                          legs=[(dep, p["retained_cents"], "retenção de caução"),
                                (svc, -p["retained_cents"], f"dano: {p.get('damage_description') or ''}"[:200])],
                          description=f"Retenção de caução — {p.get('damage_description') or ''}"[:500],
                          approval_request_id=req.id, created_by=uid, ref_type="establishment", ref_id=est.id,
                          org_id=est.org_id)
    if p["refund_cents"]:
        w = await withdrawals.create_deposit_refund_pix_out(db, establishment_id=est.id, amount_cents=p["refund_cents"],
                                                            approval=req)
        req.execution_state, req.execution_ref = ES.PENDING.value, str(w.id)
    else:
        req.execution_state, req.execution_ref, req.executed_at = ES.EXECUTED.value, str(est.id), utcnow()
