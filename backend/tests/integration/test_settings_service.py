"""Configurações de negócio: defaults do registry, validação, versão, histórico append-only, auditoria;
chaves financeiras só pelo pedido de autorização financeira (AÇÃO 003 — ADR_011 D4)."""
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.authz import AuthContext
from app.core.errors import Conflict, DomainError
from app.core.settings_service import REGISTRY, get_setting, set_setting
from app.core.timeutils import utcnow
from app.models.financial import FinancialApprovalRequest
from app.modules.financial import approvals, common
from tests.factories import login_as, make_user
from tests.fin_helpers import approve_svc, ctx_for, make_admin_masters

EXPECTED_DEFAULTS = {
    "campaign.approval_mode": "MANUAL",
    "inventory.reservation_hours": 2,
    "commission.model": "A_POOL",
    "commission.pool_bps": 500,
    "pricing.min_margin_bps": 4000,
    "player.offline_alert_days": [7, 28],
    "cycle.default_internal_s": 120,
    "cycle.default_external_s": 120,
    "pix.key_cooldown_hours": 12,
    "refund.window_days": 7,
    "proof_of_play.raw_retention_days": 90,
}


async def test_defaults(db):
    for k, v in EXPECTED_DEFAULTS.items():
        assert await get_setting(db, k) == v
    assert {k for k, d in REGISTRY.items() if d.financial} >= {
        "commission.model", "commission.pool_bps", "pricing.min_margin_bps", "pix.key_cooldown_hours",
        "refund.window_days",
    }


async def test_set_validates_versions_history_and_audit(db):
    ctx = AuthContext.system("cli")
    key = "proof_of_play.raw_retention_days"  # configuração comum (não é mandato travado)
    with pytest.raises(DomainError):
        await set_setting(db, ctx, key, 10)  # abaixo do mínimo (30)
    with pytest.raises(DomainError):
        await set_setting(db, ctx, "cycle.default_internal_s", 45)  # não múltiplo de 30
    with pytest.raises(DomainError):
        await set_setting(db, ctx, key, 95.5)
    await db.rollback()
    out = await set_setting(db, ctx, key, 120, reason="teste")
    await db.commit()
    assert out["version"] == 1 and await get_setting(db, key) == 120
    with pytest.raises(Conflict):
        await set_setting(db, ctx, key, 150, expected_version=0)
    await db.rollback()
    await set_setting(db, ctx, key, 150, expected_version=1)
    await db.commit()
    hist = (await db.execute(text("SELECT old_value, new_value, version FROM system_setting_history ORDER BY id"))).all()
    assert [(h.old_value, h.new_value, h.version) for h in hist] == [(90, 120, 1), (120, 150, 2)]
    n = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'settings.updated'"))).scalar_one()
    assert n == 2
    with pytest.raises(DBAPIError):
        await db.execute(text("DELETE FROM system_setting_history"))
    await db.rollback()


async def test_mandates_cannot_be_loosened(db, mail, redis):
    """P002#6 (reserva de 2 h) e P002#11 (chave PIX nova bloqueada por 12 h) não podem ser afrouxados por configuração."""
    ctx = AuthContext.system("cli")
    for value in (1, 3, 48):
        with pytest.raises(DomainError) as ei:
            await set_setting(db, ctx, "inventory.reservation_hours", value, reason="teste")
        assert ei.value.code == "SETTING_INVALID"
        await db.rollback()
    a, b = await make_admin_masters(db)
    for value in (0, 6, 11):  # nem o pedido de autorização financeira aceita afrouxar o mandato
        with pytest.raises(DomainError) as ei:
            await approvals.create_request(db, await ctx_for(db, a), "SETTING_CHANGE",
                                           {"key": "pix.key_cooldown_hours", "value": value}, "afrouxar o bloqueio PIX")
        assert ei.value.code == "SETTING_INVALID"
        await db.rollback()
    # aumentar o bloqueio continua possível, mas só com pedido autorizado (aqui: dupla aprovação)
    with pytest.raises(DomainError) as ei:
        await set_setting(db, ctx, "pix.key_cooldown_hours", 24, reason="teste")
    assert ei.value.code == "SETTING_REQUIRES_DUAL_APPROVAL"
    await db.rollback()
    req = await approvals.create_request(db, await ctx_for(db, a), "SETTING_CHANGE",
                                         {"key": "pix.key_cooldown_hours", "value": 24}, "aumentar o bloqueio PIX")
    await db.commit()
    await approve_svc(db, b, req, mail)
    db.expire_all()
    assert await get_setting(db, "pix.key_cooldown_hours") == 24
    assert await get_setting(db, "inventory.reservation_hours") == 2


async def _approved_request(db, a, b, key: str, value, *, mode: str = "DUAL", execution_state: str = "NOT_STARTED"):
    """Pedido ``SETTING_CHANGE`` já autorizado inserido direto (o guard de INSERT do banco confere o modo
    e os Masters) — reproduz o estado em que ``_execute`` chama ``set_setting``."""
    payload = {"key": key, "value": value}
    kw = ({"approved_by": b.id, "approved_at": utcnow()} if mode == "DUAL"
          else {"self_authorized_by": a.id, "self_authorized_at": utcnow()})
    r = FinancialApprovalRequest(id=uuid.uuid4(), action_type="SETTING_CHANGE", payload=payload,
                                 payload_sha256=common.canonical_sha256(payload), reason="pedido de teste autorizado",
                                 requested_by=a.id, status="APPROVED", expires_at=utcnow() + timedelta(hours=1),
                                 execution_state=execution_state, idempotency_key=uuid.uuid4().hex,
                                 approval_mode=mode, **kw)
    db.add(r)
    await db.commit()
    return r


async def test_financial_setting_requires_authorized_request(db):
    """ADR_011 D4: sem booleano — a chave financeira só muda com o PEDIDO autorizado (conferido no banco)."""
    a, b = await make_admin_masters(db)
    ctx = AuthContext.system("cli")

    async def refused(req, value=700, key="commission.pool_bps"):
        with pytest.raises(DomainError) as ei:
            await set_setting(db, ctx, key, value, authorized_request=req)
        assert ei.value.code == "SETTING_REQUIRES_DUAL_APPROVAL" and ei.value.status == 409
        assert "aprovação única ou dupla" in ei.value.message
        await db.rollback()

    await refused(None)
    # objeto forjado em memória (não existe no banco) com status APPROVED
    await refused(SimpleNamespace(id=uuid.uuid4(), status="APPROVED", action_type="SETTING_CHANGE",
                                  payload={"key": "commission.pool_bps", "value": 700}))
    # pedido PENDENTE (ainda não autorizado)
    pending = await approvals.create_request(db, await ctx_for(db, a), "SETTING_CHANGE",
                                             {"key": "commission.pool_bps", "value": 700}, "pedido ainda pendente")
    await db.commit()
    pending_id = pending.id
    ok_id = (await _approved_request(db, a, b, "commission.pool_bps", 700)).id
    executed_id = (await _approved_request(db, a, b, "commission.pool_bps", 700, execution_state="EXECUTED")).id
    await refused(await db.get(FinancialApprovalRequest, pending_id))
    await refused(await db.get(FinancialApprovalRequest, ok_id), value=701)       # valor diferente do autorizado
    await refused(await db.get(FinancialApprovalRequest, ok_id), value=True)      # tipo diferente (JSON estrito)
    await refused(await db.get(FinancialApprovalRequest, ok_id), key="refund.window_days", value=700)  # outra chave
    await refused(await db.get(FinancialApprovalRequest, executed_id))  # pedido já executado não serve de novo
    ok = await db.get(FinancialApprovalRequest, ok_id)
    assert await get_setting(db, "commission.pool_bps") == 500
    out = await set_setting(db, ctx, "commission.pool_bps", 700, authorized_request=ok, reason="teste")
    await db.commit()
    assert out["version"] == 1 and await get_setting(db, "commission.pool_bps") == 700
    hist = (await db.execute(text("SELECT reason FROM system_setting_history WHERE key='commission.pool_bps'"))).scalar_one()
    assert str(ok.id) in hist and "DUAL" in hist
    aud = (await db.execute(text("SELECT after_data FROM audit_logs WHERE action='settings.updated' "
                                 "AND entity_id='commission.pool_bps'"))).scalar_one()
    assert aud["approval_request_id"] == str(ok.id) and aud["approval_mode"] == "DUAL"
    assert aud["authorized_by"] == str(b.id)


async def test_settings_api(client, db):
    admin = await make_user(db, role="admin", mfa=True)
    s = await login_as(client, admin)
    r = await client.get("/api/v1/settings", headers=s.headers)
    assert r.status_code == 200 and len(r.json()["items"]) == len(REGISTRY)
    r = await client.put("/api/v1/settings/proof_of_play.raw_retention_days", json={"value": 120}, headers=s.headers)
    assert r.status_code == 200 and r.json()["version"] == 1
    r = await client.put("/api/v1/settings/inventory.reservation_hours", json={"value": 3}, headers=s.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "SETTING_INVALID"
    r = await client.put("/api/v1/settings/commission.pool_bps", json={"value": 300}, headers=s.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "SETTING_REQUIRES_DUAL_APPROVAL"
    r = await client.put("/api/v1/settings/nao.existe", json={"value": 1}, headers=s.headers)
    assert r.status_code == 404
    emp = await make_user(db, role="employee")
    se = await login_as(client, emp)
    assert (await client.get("/api/v1/settings", headers=se.headers)).status_code == 403
