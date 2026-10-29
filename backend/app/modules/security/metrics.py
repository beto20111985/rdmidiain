"""
RD Mídia Indoor — Métricas Prometheus da segurança da instância (SPEC §7; regras ``rd-security`` do F).

Gauges (servidos por ``app.modules.websocket.metrics.render_metrics``):
- ``rd_security_approval_mode{mode="SINGLE"|"DUAL"}`` — 1 no modo atual, 0 no outro;
- ``rd_security_approval_mode_version`` — versão do modo (alerta em qualquer mudança);
- ``rd_security_active_masters`` — Masters ativos (``rd_active_master_count``);
- ``rd_security_integrity_ok`` — 1/0 (selos das trilhas + titulares conferem);
- ``rd_security_env_divergence`` — 1 se ``RD_BOOTSTRAP_*`` diverge do modo efetivo.

Valores calculados pelo beat (a cada 15 min), pelo startup da API e — se o snapshot sumir do Redis — sob
demanda no scrape (trava NX; nunca por scrape repetido). Snapshot no Redis com TTL de 1 h.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("rd.security.metrics")

SNAPSHOT_TTL_S = 3600


def _key(*parts: str) -> str:
    from app.core.redis import rkey

    return rkey("security", *parts)


async def compute_snapshot(db: AsyncSession, *, integrity: Any = None) -> dict:
    from app.modules.security import approval_mode as am
    from app.modules.security.bootstrap import env_status
    from app.modules.security.masters import active_master_count

    state = await am.current_state(db)
    report = integrity if integrity is not None else await am.verify_integrity(db)
    env = await env_status(state)
    return {
        "mode": state.mode if state else None,
        "mode_version": state.version if state else 0,
        "active_masters": await active_master_count(db),
        "integrity_ok": 1 if report.ok else 0,
        "env_divergence": 1 if env["divergence"] else 0,
    }


async def store_snapshot(snapshot: dict) -> None:
    try:
        from app.core.redis import get_redis

        r = await get_redis()
        await r.set(_key("metrics_snapshot"), json.dumps(snapshot), ex=SNAPSHOT_TTL_S)
    except Exception as exc:  # métrica é best-effort
        log.warning("security_metrics_store_failed", error_type=type(exc).__name__)


async def read_snapshot(*, compute_if_missing: bool = True) -> Optional[dict]:
    from app.core.redis import get_redis

    r = await get_redis()
    raw = await r.get(_key("metrics_snapshot"))
    if raw:
        return json.loads(raw)
    if not compute_if_missing or not await r.set(_key("metrics_lock"), "1", nx=True, ex=30):
        return None
    from app.core.database import session_factory

    try:
        async with session_factory()() as db:
            snap = await compute_snapshot(db)
    except Exception as exc:
        log.error("security_metrics_compute_failed", error_type=type(exc).__name__)
        return None
    await store_snapshot(snap)
    return snap


def write_metrics(w: Any, snap: dict) -> None:
    """Escreve os gauges no ``_Writer`` do /metrics."""
    mode = snap.get("mode")
    if mode in ("SINGLE", "DUAL"):
        for m in ("SINGLE", "DUAL"):
            w.metric("rd_security_approval_mode", 1 if m == mode else 0,
                     help_text="Modo de aprovacao financeira efetivo da instancia (1 = atual)", labels={"mode": m})
    w.metric("rd_security_approval_mode_version", int(snap.get("mode_version") or 0),
             help_text="Versao do modo de aprovacao financeira (0 = nao inicializado)")
    w.metric("rd_security_active_masters", int(snap.get("active_masters") or 0),
             help_text="Administradores Master ativos (assento + TOTP + senha financeira + setup completo)")
    w.metric("rd_security_integrity_ok", int(snap.get("integrity_ok") or 0),
             help_text="1 se os selos do modo/assentos e os titulares conferem; 0 = falha (dinheiro congelado)")
    w.metric("rd_security_env_divergence", int(snap.get("env_divergence") or 0),
             help_text="1 se RD_BOOTSTRAP_DUAL_MASTER_APPROVAL diverge do modo efetivo (variavel ignorada)")


async def mark_integrity_failed() -> None:
    """Falha detectada fora do beat (D1: selo de credencial/convite): zera ``rd_security_integrity_ok`` já
    (o próximo snapshot completo recalcula tudo)."""
    from app.core.redis import get_redis

    r = await get_redis()
    raw = await r.get(_key("metrics_snapshot"))
    snap = json.loads(raw) if raw else {"mode": None, "mode_version": 0, "active_masters": 0, "env_divergence": 0}
    snap["integrity_ok"] = 0
    await r.set(_key("metrics_snapshot"), json.dumps(snap), ex=SNAPSHOT_TTL_S)
