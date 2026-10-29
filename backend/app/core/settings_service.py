"""
RD Mídia Indoor — Configurações de negócio com registry tipado (system_settings).

    from app.core.settings_service import get_setting, set_setting
    days = await get_setting(db, "proof_of_play.raw_retention_days")     # → 90 (default do registry)
    await set_setting(db, ctx, "proof_of_play.raw_retention_days", 120, reason="...")

- Os defaults vivem no REGISTRY (código). O banco guarda apenas valores alterados.
- ``set_setting`` valida tipo/faixa, usa concorrência otimista (``expected_version``),
  grava histórico append-only e auditoria na mesma transação.
- Chaves ``financial=True`` só podem ser alteradas pela EXECUÇÃO de um pedido de
  autorização financeira (``SETTING_CHANGE``) já autorizado no modo efetivo da instância
  — dupla aprovação (DUAL) ou aprovação única (SINGLE) — que chama
  ``set_setting(..., authorized_request=<pedido>)``. O pedido é RECONFERIDO no banco
  (status ``APPROVED``, ação, chave, valor, autorizador coerente com o modo, ainda não
  executado); sem pedido / pedido inválido → 409 ``SETTING_REQUIRES_DUAL_APPROVAL`` (código
  mantido por compatibilidade). Não existe mais booleano de "dupla aprovação" (ADR_011 D4).
- Agentes de domínio podem registrar novas chaves com ``register(SettingDef(...))``
  no import do seu módulo (ou adicionando aqui na seção do domínio).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, DomainError, NotFound
from app.core.timeutils import utcnow


@dataclass(frozen=True)
class SettingDef:
    key: str
    default: Any
    type: str  # int | str | bool | list[int] | enum
    description: str
    financial: bool = False
    choices: Optional[tuple] = None
    min: Optional[int] = None
    max: Optional[int] = None
    validator: Optional[Callable[[Any], None]] = None
    category: str = "geral"


def _multiple_of_30(v: Any) -> None:
    if v % 30 != 0:
        raise ValueError("deve ser múltiplo de 30 segundos")


def _offline_days(v: Any) -> None:
    if len(v) == 0 or sorted(v) != list(v) or len(set(v)) != len(v):
        raise ValueError("lista crescente sem repetição")


REGISTRY: dict[str, SettingDef] = {}


def register(d: SettingDef) -> SettingDef:
    if d.key in REGISTRY and REGISTRY[d.key] != d:
        raise ValueError(f"configuração já registrada com outra definição: {d.key}")
    validate_value(d, d.default)
    REGISTRY[d.key] = d
    return d


def validate_value(d: SettingDef, value: Any) -> Any:
    def _int(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("deve ser inteiro")
        if d.min is not None and v < d.min:
            raise ValueError(f"mínimo {d.min}")
        if d.max is not None and v > d.max:
            raise ValueError(f"máximo {d.max}")
        return v

    try:
        if d.type == "int":
            _int(value)
        elif d.type == "bool":
            if not isinstance(value, bool):
                raise ValueError("deve ser booleano")
        elif d.type == "str":
            if not isinstance(value, str):
                raise ValueError("deve ser texto")
        elif d.type == "enum":
            if value not in (d.choices or ()):
                raise ValueError(f"valores aceitos: {', '.join(d.choices or ())}")
        elif d.type == "list[int]":
            if not isinstance(value, list):
                raise ValueError("deve ser lista de inteiros")
            for v in value:
                _int(v)
        else:  # pragma: no cover - definição inválida
            raise ValueError(f"tipo desconhecido {d.type}")
        if d.validator:
            d.validator(value)
    except ValueError as exc:
        raise DomainError("SETTING_INVALID", f"Valor inválido para {d.key}: {exc}", status=400,
                          details={"key": d.key}) from None
    return value


# ─── DEFAULTS INICIAIS (AÇÃO 002) ───────────────────────────────────────────
for _d in [
    SettingDef("campaign.approval_mode", "MANUAL", "enum", "Aprovação de campanha após pagamento (MANUAL = RD revisa).",
               choices=("MANUAL", "AUTO"), category="campanhas"),
    # Mandato P002#6 / P001§49: reserva de 2 h — faixa travada (mudar exige decisão do proprietário + código).
    SettingDef("inventory.reservation_hours", 2, "int", "Horas de reserva de inventário antes do pagamento (fixo: 2 h).",
               min=2, max=2, category="inventário"),
    SettingDef("commission.model", "A_POOL", "enum", "Modelo de comissão (A = pool fixo; B = receita atribuída).",
               financial=True, choices=("A_POOL", "B_ATTRIBUTED"), category="financeiro"),
    SettingDef("commission.pool_bps", 500, "int", "Pool de comissão em basis points (500 = 5%).",
               financial=True, min=0, max=10000, category="financeiro"),
    SettingDef("pricing.min_margin_bps", 4000, "int", "Margem mínima em basis points (4000 = 40%).",
               financial=True, min=0, max=10000, category="financeiro"),
    SettingDef("player.offline_alert_days", [7, 28], "list[int]", "Dias offline para alertas (aviso, crítico).",
               min=1, max=365, validator=_offline_days, category="players"),
    SettingDef("cycle.default_internal_s", 120, "int", "Ciclo padrão: segundos de conteúdo interno.",
               min=0, max=3600, validator=_multiple_of_30, category="scheduler"),
    SettingDef("cycle.default_external_s", 120, "int", "Ciclo padrão: segundos de conteúdo externo.",
               min=0, max=3600, validator=_multiple_of_30, category="scheduler"),
    # Mandato P002#11 / P001§87: nova chave PIX bloqueada por 12 h — nunca menos (aumentar exige dupla aprovação).
    SettingDef("pix.key_cooldown_hours", 12, "int", "Bloqueio de saque após troca de chave PIX (horas; mínimo 12).",
               financial=True, min=12, max=720, category="financeiro"),
    SettingDef("refund.window_days", 7, "int", "Janela de reembolso (dias).",
               financial=True, min=0, max=365, category="financeiro"),
    SettingDef("proof_of_play.raw_retention_days", 90, "int", "Retenção de eventos brutos de proof-of-play (dias).",
               min=30, max=3650, category="proof-of-play"),
]:
    register(_d)


def get_def(key: str) -> SettingDef:
    d = REGISTRY.get(key)
    if d is None:
        raise NotFound("SETTING_NOT_FOUND", "Configuração inexistente.")
    return d


async def get_setting(db: AsyncSession, key: str, default: Any = ...) -> Any:
    from app.models.settings import SystemSetting

    d = REGISTRY.get(key)
    row = await db.get(SystemSetting, key)
    if row is not None:
        return row.value
    if default is not ...:
        return default
    if d is None:
        raise KeyError(key)
    return d.default


async def get_all_settings(db: AsyncSession) -> list[dict]:
    from app.models.settings import SystemSetting

    rows = {r.key: r for r in (await db.execute(select(SystemSetting))).scalars()}
    out = []
    for key, d in sorted(REGISTRY.items()):
        r = rows.get(key)
        out.append(
            {
                "key": key,
                "value": r.value if r else d.default,
                "default": d.default,
                "type": d.type,
                "choices": list(d.choices) if d.choices else None,
                "min": d.min,
                "max": d.max,
                "financial": d.financial,
                "category": d.category,
                "description": d.description,
                "version": r.version if r else 0,
                "updated_at": r.updated_at if r else None,
                "updated_by": r.updated_by if r else None,
            }
        )
    return out


async def set_setting(
    db: AsyncSession,
    ctx: Any,
    key: str,
    value: Any,
    *,
    reason: Optional[str] = None,
    expected_version: Optional[int] = None,
    authorized_request: Any = None,
) -> dict:
    from app.core.audit import audit
    from app.core.logging import current_request_meta
    from app.models.settings import SystemSetting, SystemSettingHistory

    d = get_def(key)
    authorization: Optional[dict] = None
    if d.financial:
        authorization = await _check_authorized_request(db, authorized_request, key, value)
    validate_value(d, value)
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key).with_for_update())
    ).scalar_one_or_none()
    current_version = row.version if row else 0
    if expected_version is not None and expected_version != current_version:
        raise Conflict("SETTING_VERSION_CONFLICT", "A configuração foi alterada por outra pessoa. Recarregue.",
                       details={"current_version": current_version})
    old_value = row.value if row else d.default
    actor_id = getattr(ctx, "user_id", None)
    new_version = current_version + 1
    if row is None:
        row = SystemSetting(key=key, value=value, version=new_version, updated_by=actor_id, updated_at=utcnow())
        db.add(row)
    else:
        row.value = value
        row.version = new_version
        row.updated_by = actor_id
        row.updated_at = utcnow()
    db.add(
        SystemSettingHistory(
            key=key,
            old_value=old_value,
            new_value=value,
            version=new_version,
            changed_by=actor_id,
            reason=_history_reason(reason, authorization),
            request_id=current_request_meta().request_id,
        )
    )
    after: dict = {"value": value, "version": new_version}
    if authorization:
        after.update(authorization)
    await audit(db, ctx, "settings.updated", "system_setting", key, before={"value": old_value},
                after=after, reason=reason)
    await db.flush()
    return {"key": key, "value": value, "version": new_version}


# ─── Autorização das chaves financeiras (ADR_011 D4; SPEC 003 §4.3) ─────────

def _json_equal(a: Any, b: Any) -> bool:
    """Igualdade JSON estrita (``true`` ≠ ``1``; ``1`` ≠ ``1.0`` em tipo)."""
    import json

    def _dump(v: Any) -> str:
        return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    try:
        return _dump(a) == _dump(b)
    except (TypeError, ValueError):
        return False


def _requires_authorization(key: str) -> DomainError:
    return DomainError(
        "SETTING_REQUIRES_DUAL_APPROVAL",
        "Esta configuração financeira exige autorização financeira (aprovação única ou dupla, conforme a instância).",
        status=409,
        details={"key": key},
    )


async def _check_authorized_request(db: AsyncSession, req: Any, key: str, value: Any) -> dict:
    """Confere — no BANCO, não no objeto recebido — que ``req`` é um pedido ``SETTING_CHANGE`` desta
    chave/valor, ``APPROVED`` no modo dele (DUAL: aprovado por OUTRO Master; SINGLE: autorizado pelo
    próprio solicitante) e ainda não executado. Um bug ou chamada direta não altera chave financeira."""
    from app.models.financial import FinancialApprovalRequest as R

    rid = getattr(req, "id", None)
    if req is None or rid is None:
        raise _requires_authorization(key)
    row = (await db.execute(
        select(R.status, R.action_type, R.payload, R.approval_mode, R.requested_by, R.approved_by,
               R.self_authorized_by, R.execution_state).where(R.id == rid)
    )).one_or_none()
    if row is None:
        raise _requires_authorization(key)
    payload = row.payload or {}
    if row.approval_mode == "DUAL":
        authorized_by = row.approved_by
        coherent = (row.approved_by is not None and row.approved_by != row.requested_by
                    and row.self_authorized_by is None)
    elif row.approval_mode == "SINGLE":
        authorized_by = row.self_authorized_by
        coherent = row.self_authorized_by is not None and row.self_authorized_by == row.requested_by and row.approved_by is None
    else:
        authorized_by, coherent = None, False
    if not (
        coherent
        and row.status == "APPROVED"
        and row.action_type == "SETTING_CHANGE"
        and row.execution_state == "NOT_STARTED"
        and payload.get("key") == key
        and "value" in payload
        and _json_equal(payload.get("value"), value)
    ):
        raise _requires_authorization(key)
    return {"approval_request_id": str(rid), "approval_mode": row.approval_mode,
            "requested_by": str(row.requested_by), "authorized_by": str(authorized_by)}


def _history_reason(reason: Optional[str], authorization: Optional[dict]) -> Optional[str]:
    if not authorization:
        return reason
    tag = f"[pedido {authorization['approval_request_id']} · modo {authorization['approval_mode']}]"
    if reason and authorization["approval_request_id"] in reason:
        return reason
    return f"{tag} {reason}".strip() if reason else tag

