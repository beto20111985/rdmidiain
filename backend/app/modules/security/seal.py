"""
RD Mídia Indoor — Selos das trilhas de segurança (AÇÃO 003 / ADR_011 D1, D10; SPEC §2.8).

Código PURO (sem banco). Cada linha de ``instance_approval_mode_history`` e de
``admin_master_seat_events`` carrega ``prev_seal`` (selo da linha anterior; ``GENESIS`` na
primeira) e ``app_seal`` = HMAC-SHA256 (chave derivada por HKDF da chave da aplicação —
``app.core.crypto.hmac_hex``) sobre a forma CANÔNICA dos campos + ``prev_seal``.

O banco só confere o ENCADEAMENTO (``p_prev_seal`` = cabeça atual, sob advisory lock) — ele não
conhece a chave. Quem recalcula e confere o HMAC é ``approval_mode.verify_integrity`` (agente B):
sem a chave, qualquer linha forjada/alterada quebra o selo.

Forma canônica: ``json.dumps(campos, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``;
valores ``str | int | None``; UUID → ``str(uuid)`` minúsculo com hífens (ids aceitos como ``UUID``
ou texto); inteiros nunca como ``bool``. Mudar a forma canônica ou a finalidade invalida todos os
selos existentes — exige procedimento de re-selagem (pendência registrada no ADR_011).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional, Union

from app.core.crypto import hmac_hex

GENESIS = "0" * 64
MODE_PURPOSE = "rd.security.approval_mode.v1"
SEAT_PURPOSE = "rd.security.master_seat.v1"

IdLike = Union[uuid.UUID, str, None]


def _value(v: Any) -> Union[str, int, None]:
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):  # bool é int em Python: nunca aceitar silenciosamente
        raise TypeError("valor booleano não é canônico em selos")
    if isinstance(v, int):
        return v
    if isinstance(v, uuid.UUID):
        return str(v)
    raise TypeError(f"tipo não canônico em selos: {type(v).__name__}")


def _uuid(v: IdLike) -> Optional[str]:
    """Id canônico: ``str(UUID)`` (minúsculo, com hífens) — aceita ``UUID`` ou texto."""
    if v is None:
        return None
    return str(v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)))


def _int(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError("inteiro esperado")
    return v


def canonical(fields: dict) -> str:
    """Serialização canônica (determinística) dos campos de uma linha selada."""
    return json.dumps({str(k): _value(v) for k, v in fields.items()},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def mode_seal(*, prev_seal: str, mode_version: int, from_mode: Optional[str], to_mode: str, kind: str,
              source: str, approval_request_id: IdLike, requested_by: IdLike, authorized_by: IdLike,
              reason: Optional[str], env_value: Optional[str]) -> str:
    """Selo de uma linha de ``instance_approval_mode_history`` (mesmos valores gravados na linha)."""
    return hmac_hex(canonical({
        "prev_seal": prev_seal,
        "mode_version": _int(mode_version),
        "from_mode": from_mode,
        "to_mode": to_mode,
        "kind": kind,
        "source": source,
        "approval_request_id": _uuid(approval_request_id),
        "requested_by": _uuid(requested_by),
        "authorized_by": _uuid(authorized_by),
        "reason": reason,
        "env_value": env_value,
    }), purpose=MODE_PURPOSE)


def seat_seal(*, prev_seal: str, seat: int, user_id: IdLike, event: str, source: str,
              invite_id: IdLike, actor_user_id: IdLike) -> str:
    """Selo de uma linha de ``admin_master_seat_events`` (mesmos valores gravados na linha)."""
    return hmac_hex(canonical({
        "prev_seal": prev_seal,
        "seat": _int(seat),
        "user_id": _uuid(user_id),
        "event": event,
        "source": source,
        "invite_id": _uuid(invite_id),
        "actor_user_id": _uuid(actor_user_id),
    }), purpose=SEAT_PURPOSE)


# ─── AÇÃO 003 — D1 (revisor I): selo de CREDENCIAL do titular e selo do CONVITE ────────────────
# O papel de runtime (``rd_app``) consegue escrever ``users``/``admin_master_invites``, mas não conhece a
# chave da aplicação: sem ela, não forja estes selos. A aplicação confere antes de aceitar login/sessão/
# step-up/setup/convite (falha FECHADA + alerta) e re-sela a cada escrita legítima.

INVITE_PURPOSE = "rd.security.master_invite.v1"
CREDENTIAL_PURPOSE = "rd.security.master_credential.v1"

_EPOCH = None


def ts_micros(dt: Any) -> Optional[int]:
    """Instante canônico: microssegundos inteiros desde a época UTC (exato; sem float)."""
    import datetime as _dt

    global _EPOCH
    if dt is None:
        return None
    if _EPOCH is None:
        _EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return (dt - _EPOCH) // _dt.timedelta(microseconds=1)


def _flag(v: Any) -> int:
    """Booleano como inteiro 0/1 (o canônico recusa ``bool``)."""
    return 1 if bool(v) else 0


def _email(v: Optional[str]) -> Optional[str]:
    return None if v is None else str(v).strip().lower()


def invite_seal(*, invite_id: IdLike, seat: int, email: str, invited_by: IdLike, token_hash: str,
                expires_at: Any) -> str:
    """Selo do convite do 2º Master (campos que definem QUEM pode ativar, ONDE e ATÉ QUANDO).

    Não cobre campos mutáveis do ciclo (``status``, ``failed_attempts``, ``sent_count``,
    ``totp_pending_secret_enc``) — o banco controla as transições deles.
    """
    return hmac_hex(canonical({
        "id": _uuid(invite_id),
        "seat": _int(int(seat)),
        "email": _email(email),
        "invited_by": _uuid(invited_by),
        "token_hash": token_hash,
        "expires_at": ts_micros(expires_at),
    }), purpose=INVITE_PURPOSE)


def credential_seal(*, user_id: IdLike, admin_master_seat: Optional[int], email: str, hashed_password: Optional[str],
                    must_change_password: Any, totp_enabled: Any, totp_secret_enc: Optional[str],
                    financial_password_hash: Optional[str], is_active: Any) -> str:
    """Selo da credencial do titular de assento (fatores + identidade + ativação).

    NÃO cobre ``totp_last_step``/``last_login``/``failed_login_attempts``/``locked_until`` (mudam a cada login).
    """
    return hmac_hex(canonical({
        "user_id": _uuid(user_id),
        "admin_master_seat": None if admin_master_seat is None else _int(int(admin_master_seat)),
        "email": _email(email),
        "hashed_password": hashed_password,
        "must_change_password": _flag(must_change_password),
        "totp_enabled": _flag(totp_enabled),
        "totp_secret_enc": totp_secret_enc,
        "financial_password_hash": financial_password_hash,
        "is_active": _flag(is_active),
    }), purpose=CREDENTIAL_PURPOSE)
