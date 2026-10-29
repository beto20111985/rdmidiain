"""Proposta CRM → pedido (desconto comercial com limite/aprovador), receita atribuída por tela (modelo B)
e pagamento sem inventário (força análise manual mesmo com aprovação automática). PostgreSQL real."""
import uuid

import pytest

from app.core.authz import AuthContext, load_grants
from app.core.errors import DomainError
from app.core.settings_service import set_setting
from app.models.pricing import Order
from app.modules.campaigns import checkout as CK
from app.modules.campaigns import service as S
from tests.camp_support import FakeDeps
from tests.factories import make_user
from tests.integration.test_camp_lifecycle import new_campaign, status_of, world

SYS = AuthContext.system("system")


@pytest.fixture
def deps(monkeypatch):
    return FakeDeps().install(monkeypatch)


async def _ctx(db, user):
    return AuthContext(kind="user", user=user, grants=await load_grants(db, user.id))


async def test_proposta_desconto_dentro_do_limite_e_acima_com_aprovador(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    seller = await make_user(db, role="admin")  # vendedor da RD com checkout.create global
    seller_ctx = await _ctx(db, seller)
    full = 2 * 24 * 7 * 2 * 10
    c1 = await new_campaign(client, s, db=db, org=org)
    res = await CK.create_order_from_proposal(db, seller_ctx, proposal_id=uuid.uuid4(), org_id=org.id,
                                              campaign_ids=[uuid.UUID(c1)], idempotency_key=f"p-{uuid.uuid4().hex}",
                                              discount_bps=1000)  # = limite padrão (10%) sem aprovador
    await db.commit()
    assert res.order.total_cents == full - full // 10 and res.order.kind == "PROPOSAL"
    c2 = await new_campaign(client, s, db=db, org=org)
    seller_id = seller.id
    no_perm_id = (await make_user(db, role="employee")).id
    boss_id = (await make_user(db, role="super_admin", mfa=True)).id
    kw = dict(proposal_id=uuid.uuid4(), org_id=org.id, campaign_ids=[uuid.UUID(c2)], discount_bps=2000)

    async def attempt(approver):
        from app.models.users import User

        ctx = await _ctx(db, await db.get(User, seller_id))
        try:
            return await CK.create_order_from_proposal(db, ctx, idempotency_key=f"p-{uuid.uuid4().hex}",
                                                       discount_approved_by=approver, **kw)
        except DomainError as exc:
            await db.rollback()
            return exc.code

    assert await attempt(None) == "DISCOUNT_APPROVAL_REQUIRED"
    assert await attempt(seller_id) == "DISCOUNT_SELF_APPROVAL"
    assert await attempt(no_perm_id) == "DISCOUNT_APPROVER_INVALID"
    res = await attempt(boss_id)
    boss = type("B", (), {"id": boss_id})
    await db.commit()
    o = await db.get(Order, res.order.id)
    assert o.total_cents == full - full * 2000 // 10000 and o.commercial_discount_approved_by == boss.id


async def test_receita_atribuida_por_tela_soma_exata(db, client, deps):
    t, org, ests, screens, s = await world(db, client, n=3)
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    cid = await new_campaign(client, s, db=db, org=org)
    r = await client.post(f"/api/v1/campaigns/{cid}/checkout", headers={**s.headers, "Idempotency-Key": "rev-attr-01"},
                          json={})
    o = await db.get(Order, uuid.UUID(r.json()["order_id"]))
    await CK.on_order_paid(db, o.id, o.payment_order_id)
    await db.commit()
    shares = await S.attributed_revenue_by_screen(db, uuid.UUID(cid))
    assert set(shares) == {sc.id for sc in screens}
    assert sum(shares.values()) == r.json()["items"][0]["media_cents"]
    assert len(set(shares.values())) == 1  # telas iguais ⇒ partes iguais


async def test_pagamento_sem_inventario_forca_analise_manual(db, client, deps):
    t, org, ests, screens, s = await world(db, client)
    await set_setting(db, SYS, "campaign.approval_mode", "AUTO", reason="teste")
    await db.commit()
    cid = await new_campaign(client, s, db=db, org=org)
    r = await client.post(f"/api/v1/campaigns/{cid}/checkout", headers={**s.headers, "Idempotency-Key": "no-inv-0001"},
                          json={})
    deps.convert_status = "PAID_WITHOUT_INVENTORY"
    o = await db.get(Order, uuid.UUID(r.json()["order_id"]))
    await CK.on_order_paid(db, o.id, o.payment_order_id)
    await db.commit()
    c = await status_of(db, cid)
    assert c.status == "EM_ANALISE" and c.payment_issue == "PAID_WITHOUT_INVENTORY" and c.live_revision_id is None
