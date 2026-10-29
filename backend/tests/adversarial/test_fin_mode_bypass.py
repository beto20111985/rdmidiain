"""Tentativas de contornar o modo de aprovação financeira (AÇÃO 003; ADR_011 grill-me 1, 4, 6, 12; SPEC 003
§4, §8, §15-C) — PostgreSQL real com os guards/funções reais.

- ENV trocado + ``bootstrap-instance`` reexecutado NÃO muda o modo efetivo (só alerta de divergência).
- "A aprova como B", autoaprovação, autorização única num pedido DUAL, pedido forjado sem evento, troca de
  modo de pedido por UPDATE: recusados pela aplicação E pelo banco.
- Integridade quebrada (selo) ⇒ 503 ``SECURITY_INTEGRITY_ALERT`` SOMENTE no dinheiro.
- Master recuperado ⇒ 423 ``MASTER_RECENTLY_RECOVERED`` até o fim do bloqueio.
- A API não deixa o cliente escolher o modo nem forjar ``from_mode``/``expected_version``.
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.errors import DomainError
from app.core.settings_service import get_setting
from app.core.timeutils import utcnow
from app.models.financial import FinancialApprovalRequest
from app.models.security import InstanceApprovalModeHistory
from app.models.users import User
from app.modules.financial import approvals, common
from app.modules.security import approval_mode
from tests.factories import login_as, make_user, totp_code
from tests.fin_helpers import (  # noqa: F401
    ctx_for, fin_env, http_approval_code, make_admin_masters, make_single_master,
)

API = "/api/v1/financial/approvals"
REASON = "Mudança do modo de aprovação decidida pelos sócios em ata"
DUMMY = "123456"


def _err(r) -> str:
    return r.json()["error"]["code"]


async def _mode(db) -> tuple:
    s = await approval_mode.current_state(db)
    return s.mode, s.version


async def test_env_flip_and_bootstrap_rerun_never_change_effective_mode(db, client_factory, fin_env, mail):
    """Grill-me #1: trocar ``RD_BOOTSTRAP_DUAL_MASTER_APPROVAL`` para false e reexecutar o bootstrap não
    desliga a dupla aprovação de uma instância ativa (o nome alternativo também é ignorado)."""
    from app.modules.security.bootstrap import bootstrap_instance

    a, b = await make_admin_masters(db)
    ca = client_factory()
    sa = await login_as(ca, a)
    r = await ca.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "commission.pool_bps", "value": 1},
                                 "reason": "pool mínimo sem o sócio"}, headers=sa.headers)
    assert r.status_code == 201 and r.json()["approval_mode"] == "DUAL"
    req = r.json()
    res = await bootstrap_instance({"RD_BOOTSTRAP_DUAL_MASTER_APPROVAL": "false", "RD_DUAL_MASTER_APPROVAL": "false"})
    assert res.mode == "DUAL" and res.mode_version == 1 and not res.mode_created
    db.expire_all()
    assert await _mode(db) == ("DUAL", 1)
    assert (await db.execute(select(func.count()).select_from(InstanceApprovalModeHistory))).scalar_one() == 1
    # A tenta "autorizar sozinho" o próprio pedido: continua exigindo o outro Master
    body = {"totp_code": totp_code(a), "financial_password": a.fin_password, "payload_sha256": req["payload_sha256"],
            "email_code": DUMMY}
    r = await ca.post(f"{API}/{req['id']}/authorize", json=body, headers=sa.headers)
    assert r.status_code == 409 and _err(r) == "APPROVAL_MODE_DUAL"
    r = await ca.post(f"{API}/{req['id']}/approve", json=body, headers=sa.headers)
    assert r.status_code == 403 and _err(r) == "SELF_APPROVAL_FORBIDDEN"
    r = await ca.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "commission.pool_bps", "value": 2},
                                 "reason": "pool mínimo sem o sócio"}, headers=sa.headers)
    assert r.status_code == 201 and r.json()["approval_mode"] == "DUAL"
    db.expire_all()
    assert await get_setting(db, "commission.pool_bps") == 500


async def test_unilateral_and_cross_mode_forgeries_are_refused(db, redis, fin_env, mail):
    a, b = await make_admin_masters(db)
    m = await approvals.create_request(db, await ctx_for(db, a), "APPROVAL_MODE_CHANGE", {"target_mode": "SINGLE"},
                                       REASON, step_up={"totp_code": totp_code(a), "financial_password": a.fin_password})
    await db.commit()
    mid, sha = m.id, m.payload_sha256
    # aplicação: autorização única num pedido DUAL e autoaprovação
    with pytest.raises(DomainError) as ei:
        await approvals.authorize(db, await ctx_for(db, a), mid, totp_code=DUMMY, financial_password=a.fin_password,
                                  payload_sha256=sha, email_code=DUMMY)
    assert ei.value.code == "APPROVAL_MODE_DUAL"
    await db.rollback()
    with pytest.raises(DomainError) as ei:
        await approvals.approve(db, await ctx_for(db, a), mid, totp_code=DUMMY, financial_password=a.fin_password,
                                payload_sha256=sha, email_code=DUMMY)
    assert ei.value.code == "SELF_APPROVAL_FORBIDDEN"
    await db.rollback()
    # chamada direta da transição com o pedido ainda PENDENTE: o banco recusa (função definer)
    req = await db.get(FinancialApprovalRequest, mid)
    with pytest.raises((DomainError, DBAPIError)):
        await approval_mode.apply_transition(db, await ctx_for(db, a), req)
    await db.rollback()
    # banco (superusuário, triggers ativos): A "aprova como B" / autoautoriza / troca o modo do pedido
    for sql in (
        "UPDATE financial_approval_requests SET status='APPROVED', approved_by=requested_by, approved_at=now() WHERE id=:i",
        "UPDATE financial_approval_requests SET status='APPROVED', self_authorized_by=requested_by, "
        "self_authorized_at=now() WHERE id=:i",
        "UPDATE financial_approval_requests SET approval_mode='SINGLE' WHERE id=:i",
    ):
        with pytest.raises(DBAPIError):
            await db.execute(text(sql).bindparams(i=mid))
            await db.commit()
        await db.rollback()
    # pedido SINGLE inserido com a instância em DUAL
    payload = {"key": "commission.pool_bps", "value": 1}
    forged = FinancialApprovalRequest(id=uuid.uuid4(), action_type="SETTING_CHANGE", payload=payload,
                                      payload_sha256=common.canonical_sha256(payload), reason="forjado no banco",
                                      requested_by=a.id, status="APPROVED", self_authorized_by=a.id,
                                      self_authorized_at=utcnow(), expires_at=utcnow() + timedelta(hours=1),
                                      execution_state="NOT_STARTED", idempotency_key=uuid.uuid4().hex,
                                      approval_mode="SINGLE")
    db.add(forged)
    with pytest.raises(DBAPIError) as ei:
        await db.commit()
    assert "RD_APPROVAL_MODE_MISMATCH" in str(ei.value)
    mapped = approvals.map_db_error(ei.value)
    assert mapped is not None and mapped.code == "APPROVAL_MODE_CHANGED" and mapped.status == 409
    await db.rollback()
    db.expunge_all()
    # aprovação forjada contornando os triggers (replica): sem o evento APPROVED do aprovador, a função
    # definer não aplica a transição
    await db.execute(text("SET LOCAL session_replication_role = replica"))
    await db.execute(text("UPDATE financial_approval_requests SET status='APPROVED', approved_by=:b, approved_at=now() "
                          "WHERE id=:i").bindparams(b=b.id, i=mid))
    await db.commit()
    req = await db.get(FinancialApprovalRequest, mid)
    with pytest.raises((DomainError, DBAPIError)):
        await approval_mode.apply_transition(db, await ctx_for(db, b), req)
    await db.rollback()
    assert await _mode(db) == ("DUAL", 1)
    assert (await approval_mode.verify_integrity(db)).ok


async def test_integrity_failure_freezes_only_money(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    r = await cb.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "commission.pool_bps", "value": 1},
                                 "reason": "pedido anterior à adulteração"}, headers=sb.headers)
    assert r.status_code == 201
    req = r.json()
    # adulteração direta da trilha (superusuário contornando o append-only): DUAL → SINGLE sem selo válido
    await db.execute(text("SET LOCAL session_replication_role = replica"))
    await db.execute(text("UPDATE instance_approval_mode_history SET to_mode='SINGLE' WHERE mode_version=1"))
    await db.commit()
    assert not (await approval_mode.verify_integrity(db)).ok
    r = await ca.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "commission.pool_bps", "value": 2},
                                 "reason": "tentativa durante o alerta"}, headers=sa.headers)
    assert r.status_code == 503 and _err(r) == "SECURITY_INTEGRITY_ALERT"
    body = {"totp_code": DUMMY, "financial_password": a.fin_password, "payload_sha256": req["payload_sha256"],
            "email_code": DUMMY}
    for path in ("authorize", "approve"):
        r = await ca.post(f"{API}/{req['id']}/{path}", json=body, headers=sa.headers)
        assert r.status_code == 503 and _err(r) == "SECURITY_INTEGRITY_ALERT", (path, r.text)
    r = await ca.post("/api/v1/financial/payables", json={
        "description": "Servidor", "category": "infraestrutura", "amount_cents": 100,
        "competence_date": utcnow().date().isoformat(), "reason": "tentativa durante o alerta"}, headers=sa.headers)
    assert r.status_code == 503 and _err(r) == "SECURITY_INTEGRITY_ALERT"
    # o resto do sistema segue funcionando
    for path in ("/api/v1/me", API, f"{API}/{req['id']}", "/api/v1/settings"):
        assert (await ca.get(path, headers=sa.headers)).status_code == 200, path
    db.expire_all()
    assert (await db.get(FinancialApprovalRequest, uuid.UUID(req["id"]))).status == "PENDING"
    assert await get_setting(db, "commission.pool_bps") == 500


async def test_recovered_master_is_blocked_until_cooldown_ends(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    r = await cb.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "refund.window_days", "value": 9},
                                 "reason": "janela de reembolso"}, headers=sb.headers)
    req = r.json()
    await db.execute(update(User).where(User.id == a.id).values(master_recovered_at=utcnow()))
    await db.commit()
    code = totp_code(a)
    ecode = await http_approval_code(ca, sa, req["id"], mail, a)
    body = {"totp_code": code, "financial_password": a.fin_password, "payload_sha256": req["payload_sha256"],
            "email_code": ecode}
    r = await ca.post(f"{API}/{req['id']}/approve", json=body, headers=sa.headers)
    assert r.status_code == 423 and _err(r) == "MASTER_RECENTLY_RECOVERED"
    assert r.json()["error"]["details"]["blocked_until"]
    await db.execute(update(User).where(User.id == a.id).values(master_recovered_at=utcnow() - timedelta(hours=25)))
    await db.commit()
    r = await ca.post(f"{API}/{req['id']}/approve", json=body, headers=sa.headers)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert await get_setting(db, "refund.window_days") == 9


async def test_client_cannot_choose_mode_or_forge_mode_change_payload(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    ca = client_factory()
    sa = await login_as(ca, a)
    r = await ca.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "commission.pool_bps", "value": 1},
                                 "reason": "escolhendo o modo no corpo", "approval_mode": "SINGLE"}, headers=sa.headers)
    assert r.status_code == 422
    for payload in ({"target_mode": "SINGLE", "from_mode": "SINGLE", "expected_version": 1},
                    {"target_mode": "DUAL"}, {"target_mode": "single"}, {}):
        r = await ca.post(API, json={"action_type": "APPROVAL_MODE_CHANGE", "payload": payload, "reason": REASON},
                          headers=sa.headers)
        assert r.status_code == 400 and _err(r) == "APPROVAL_MODE_CHANGE_INVALID", (payload, r.text)
    # super admin SEM assento não solicita, não aprova nem autoriza
    intruder = await make_user(db, role="super_admin", mfa=True)
    ci = client_factory()
    si = await login_as(ci, intruder)
    r = await ci.post(API, json={"action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "SINGLE"},
                                 "reason": REASON}, headers=si.headers)
    assert r.status_code == 403
    m = await approvals.create_request(db, await ctx_for(db, a), "APPROVAL_MODE_CHANGE", {"target_mode": "SINGLE"},
                                       REASON, step_up={"totp_code": totp_code(a), "financial_password": a.fin_password})
    await db.commit()
    for path in ("authorize", "approve"):
        r = await ci.post(f"{API}/{m.id}/{path}", json={"totp_code": DUMMY, "financial_password": "x" * 8,
                                                       "payload_sha256": m.payload_sha256, "email_code": DUMMY},
                          headers=si.headers)
        assert r.status_code == 403, (path, r.text)
    assert await _mode(db) == ("DUAL", 1)


async def test_financial_authorization_blocked_right_after_recover_master_cli(db, client, fin_env, mail, monkeypatch,
                                                                              tmp_path):
    """``recover-master`` (CLI real) ⇒ o titular refaz o primeiro acesso e, mesmo com os fatores novos, a
    operação financeira fica bloqueada (423 ``MASTER_RECENTLY_RECOVERED``) durante o período de carência."""
    from types import SimpleNamespace

    from app import cli
    from tests.sec_helpers import complete_first_access, strong_password

    a = await make_single_master(db)
    tmp = strong_password("Rec")
    f = tmp_path / "recovery_password"
    f.write_text(tmp, encoding="utf-8")
    monkeypatch.setenv("RD_RECOVERY_PASSWORD_FILE", str(f))
    await cli.cmd_recover_master(1, "perdeu o celular e a senha financeira")
    m = SimpleNamespace(email=a.email, password=tmp, totp_secret=None, _last_step=None, fin_password=None)
    await complete_first_access(client, m)  # senha nova → TOTP novo → senha financeira nova
    r = await client.post(API, json={"action_type": "SETTING_CHANGE", "payload": {"key": "refund.window_days", "value": 9},
                                     "reason": "janela de reembolso"}, headers=m.headers)
    assert r.status_code == 201, r.text
    req = r.json()
    ecode = await http_approval_code(client, SimpleNamespace(headers=m.headers), req["id"], mail, a)
    r = await client.post(f"{API}/{req['id']}/authorize", json={
        "totp_code": DUMMY, "financial_password": m.fin_password, "payload_sha256": req["payload_sha256"],
        "email_code": ecode}, headers=m.headers)
    assert r.status_code == 423 and _err(r) == "MASTER_RECENTLY_RECOVERED", r.text
    assert r.json()["error"]["details"]["blocked_until"]
    db.expire_all()
    assert await get_setting(db, "refund.window_days") == 7
    assert (await approval_mode.verify_integrity(db)).ok
