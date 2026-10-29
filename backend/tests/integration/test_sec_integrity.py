"""AÇÃO 003 — integridade verificável e falha FECHADA no dinheiro (ADR_011 D10; SPEC §4.1, §8).

Selos recalculados (modo + eventos de assento), titulares conferidos; adulteração direta no banco (como
superusuário, ``session_replication_role=replica``) ⇒ ``security-verify`` = 1, operações financeiras 503
``SECURITY_INTEGRITY_ALERT`` e rotas não financeiras seguem 200. Prontidão DUAL com 2º Master pendente ⇒
409 ``SECOND_MASTER_REQUIRED``. ``/security/approval-mode`` e ``/security/integrity``.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app import cli
from app.core.errors import DomainError
from app.modules.security import approval_mode as am
from tests.factories import login_as, make_user
from tests.sec_helpers import API, active_master


async def _tamper(db, sql: str) -> None:
    await db.execute(text("SET session_replication_role = replica"))
    for stmt in [x.strip() for x in sql.split(";") if x.strip()]:
        await db.execute(text(stmt))
    await db.execute(text("SET session_replication_role = origin"))
    await db.commit()


async def test_integrity_ok_then_tampered_mode_row_freezes_money_only(client, db, redis, capsys):
    a = await active_master(db, mode="SINGLE")
    s = await login_as(client, a)
    assert (await am.verify_integrity(db)).ok
    assert await cli.cmd_security_verify() == 0
    assert "OK" in capsys.readouterr().out
    # ENV/rd_app não mudam o modo; adulteração direta (superusuário) quebra o selo
    await _tamper(db, "UPDATE instance_approval_mode_history SET to_mode = 'DUAL'")
    rep = await am.verify_integrity(db)
    assert not rep.ok and any("selo inválido" in p for p in rep.problems)
    assert await cli.cmd_security_verify() == 1
    out = capsys.readouterr().out
    assert "FALHOU" in out and "selo inválido na versão 1" in out
    # dinheiro congelado (503) …
    with pytest.raises(DomainError) as ei:
        await am.ensure_financial_authorization_allowed(db)
    assert ei.value.status == 503 and ei.value.code == "SECURITY_INTEGRITY_ALERT"
    await db.rollback()
    r = await client.post(f"{API}/financial/approvals",
                          json={"action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "SINGLE"},
                                "reason": "teste de integridade com selo adulterado"},
                          headers={**s.headers, "Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 503 and r.json()["error"]["code"] == "SECURITY_INTEGRITY_ALERT", r.text
    # … o resto do sistema funciona
    assert (await client.get(f"{API}/me", headers=s.headers)).status_code == 200
    assert (await client.get(f"{API}/organizations", headers=s.headers)).status_code == 200
    r = await client.get(f"{API}/security/integrity", headers=s.headers)
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["problems"]
    r = await client.get(f"{API}/security/approval-mode", headers=s.headers)
    assert r.status_code == 200 and r.json()["integrity"] == {"ok": False}
    # auditoria + alerta crítico (transação independente; deduplicados)
    acts = (await db.execute(text(
        "SELECT count(*) FROM audit_logs WHERE action = 'security.integrity_check_failed'"))).scalar()
    assert acts >= 1
    n = (await db.execute(text(
        "SELECT count(*) FROM notifications WHERE event_type = 'security.integrity_alert'"))).scalar()
    assert n == 1
    # métrica
    from app.modules.websocket.metrics import render_metrics

    assert "rd_security_integrity_ok 0" in await render_metrics()


@pytest.mark.parametrize("sql,needle", [
    ("UPDATE users SET admin_master_seat = NULL", "evento selado sem titular"),
    ("UPDATE admin_master_seat_events SET app_seal = repeat('a', 64)", "selo inválido no evento"),
    ("DELETE FROM admin_master_seat_events", "titular sem evento selado"),
    ("UPDATE instance_approval_mode_history SET prev_seal = repeat('1', 64)", "encadeamento quebrado"),
])
async def test_tampering_variants_detected(db, redis, sql, needle):
    await active_master(db)
    assert (await am.verify_integrity(db)).ok
    await _tamper(db, sql)
    rep = await am.verify_integrity(db)
    assert not rep.ok and any(needle in p for p in rep.problems), rep.problems


async def test_holder_swapped_directly_is_detected(db, redis):
    a = await active_master(db)
    other = await make_user(db, role="employee")
    await _tamper(db, f"UPDATE users SET admin_master_seat = NULL WHERE id = '{a.id}'; "
                      f"UPDATE users SET admin_master_seat = 1 WHERE id = '{other.id}'")
    rep = await am.verify_integrity(db)
    assert not rep.ok and any("titular diverge" in p for p in rep.problems)


async def test_readiness_second_master_required_and_not_initialized(client, db, redis, mail):
    with pytest.raises(DomainError) as ei:
        await am.ensure_financial_authorization_allowed(db)
    assert ei.value.status == 503 and ei.value.code == "INSTANCE_NOT_INITIALIZED"
    await db.rollback()
    a = await active_master(db, mode="DUAL")
    s = await login_as(client, a)  # antes dos rollbacks (que expiram os objetos da sessão)
    with pytest.raises(DomainError) as ei:
        await am.ensure_financial_authorization_allowed(db)
    assert ei.value.status == 409 and ei.value.code == "SECOND_MASTER_REQUIRED"
    assert ei.value.details == {"approval_mode": "DUAL", "second_master": "NOT_INVITED"}
    assert "segundo Administrador Master" in ei.value.message
    await db.rollback()
    from tests.factories import totp_code

    r = await client.post(f"{API}/security/masters/invites",
                          json={"full_name": "Sócio B", "email": "b@empresa.com.br", "totp_code": totp_code(a)},
                          headers=s.headers)
    assert r.status_code == 201
    with pytest.raises(DomainError) as ei:
        await am.ensure_financial_authorization_allowed(db)
    assert ei.value.details["second_master"] == "INVITE_PENDING"
    await db.rollback()
    # com o 2º Master ATIVO (fábrica + assento selado) a prontidão libera; o modo não muda
    await active_master(db, seat=2, mode="DUAL")
    st = await am.ensure_financial_authorization_allowed(db)
    assert st.mode == "DUAL" and st.version == 1
    await db.rollback()


async def test_approval_mode_endpoint_visibility(client, db, redis):
    adm = await make_user(db, role="admin", mfa=True)
    s_adm = await login_as(client, adm)
    r = await client.get(f"{API}/security/approval-mode", headers=s_adm.headers)
    assert r.status_code == 503 and r.json()["error"]["code"] == "INSTANCE_NOT_INITIALIZED"
    a = await active_master(db, mode="DUAL")
    r = await client.get(f"{API}/security/approval-mode", headers=s_adm.headers)
    body = r.json()
    assert r.status_code == 200 and body["mode"] == "DUAL" and body["mode_version"] == 1
    assert body["initialized_source"] == "BOOTSTRAP_ENV" and body["active_masters"] == 1
    assert body["dual_approval_ready"] is False and body["pending_change_request"] is None
    # admin com settings.view mas sem audit.view GLOBAL? (admin tem audit.view) ⇒ vê histórico
    assert body["history"] is not None
    emp = await make_user(db, role="employee")
    s_emp = await login_as(client, emp)
    assert (await client.get(f"{API}/security/approval-mode", headers=s_emp.headers)).status_code == 403
    fin = await make_user(db, role="financial", mfa=True)
    s_fin = await login_as(client, fin)
    r = await client.get(f"{API}/security/approval-mode", headers=s_fin.headers)
    assert r.status_code == 200 and r.json()["history"] is None and r.json()["integrity"] is None
    s_a = await login_as(client, a)
    r = await client.get(f"{API}/security/approval-mode", headers=s_a.headers)
    h = r.json()["history"]
    assert h[0]["kind"] == "INITIALIZED" and h[0]["to_mode"] == "DUAL" and r.json()["integrity"] == {"ok": True}
    assert r.json()["env"] == {"divergence": False, "unsupported_var_present": False}
    assert (await client.get(f"{API}/security/integrity", headers=s_fin.headers)).status_code == 403


async def test_security_watch_beat_observer(db, redis):
    from app.core.redis import rkey
    from app.workers.tasks.security import _security_watch

    await active_master(db, mode="DUAL")
    snap = await _security_watch()
    assert snap == {"mode": "DUAL", "mode_version": 1, "active_masters": 1, "integrity_ok": 1, "env_divergence": 0}
    # observador: versão diferente da última observada ⇒ notificação (mesmo dedupe da aplicação)
    await redis.set(rkey("security", "observed_mode_version"), "0")
    await _security_watch()
    await _security_watch()
    n = (await db.execute(text(
        "SELECT count(*) FROM notifications WHERE event_type = 'security.approval_mode_changed'"))).scalar()
    assert n == 1
    await _tamper(db, "UPDATE instance_approval_mode_history SET reason = 'adulterado'")
    snap = await _security_watch()
    assert snap["integrity_ok"] == 0
    assert (await db.execute(text(
        "SELECT count(*) FROM audit_logs WHERE action = 'security.integrity_check_failed' "
        "AND after_data->>'origin' = 'beat'"))).scalar() == 1
