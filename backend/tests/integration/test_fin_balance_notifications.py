"""Correção (g): notificações de saldo do estabelecimento (W-BIZ ``notify``).

- ``balance.available`` na liberação da competência (dia 1: Pending → Available), só para os
  usuários do estabelecimento (permissão ``withdrawals.view``), só com o VALOR — nenhum dado
  comercial do anunciante; idempotente por competência (liberação repetida não duplica);
- ``finance.balance_negative`` (tipo do W-FIN) quando um estorno de comissão JÁ SACADA deixa o saldo
  negativo (saques bloqueados); idempotente por evento de estorno.
"""
import uuid
from datetime import timedelta
from fractions import Fraction

from sqlalchemy import func, select

from app.models.notifications import Notification
from app.modules.financial import commission, common, ledger
from tests.fin_helpers import (  # noqa: F401
    create_order,
    est_with_owner,
    fin_env,
    make_screen,
    media_item,
    pay,
    payer_org,
    plan_provider,
    pop,
)

PREV_END = ledger.month_start(ledger.today_local()) - timedelta(days=1)
PREV = ledger.month_start(PREV_END)
DAY = PREV + timedelta(days=4)


async def _released(db):
    """Estabelecimento com dono + comissão de R$ 2,50 fechada e liberada na competência anterior."""
    org, est, owner = await est_with_owner(db, mfa=False)
    scr = await make_screen(db, est)
    adv = await payer_org(db)
    cid = uuid.uuid4()
    order = await create_order(db, adv, [media_item(cid, 5000, DAY, DAY)], 5000)
    assert await pay(db, order) == "PROCESSED"
    plan_provider({cid: {scr.id: {DAY: 100}}})
    await commission.accrue_commission_for_events(db, pop(cid, scr.id, DAY, 100))
    await db.commit()
    await commission.close_competence(db, PREV)
    await db.commit()
    return org, est, owner, adv, cid, order


async def _notes(db, user_id, event_type):
    return list((await db.execute(select(Notification).where(
        Notification.user_id == user_id, Notification.event_type == event_type))).scalars())


async def test_release_emits_balance_available_once_to_establishment_only(db, fin_env):
    org, est, owner, adv, cid, order = await _released(db)
    _o2, _e2, other_owner = await est_with_owner(db, mfa=False)  # outro estabelecimento sem comissão
    assert await _notes(db, owner.id, "balance.available") == []  # fechamento não libera nem avisa
    rel = await commission.release_competence(db, PREV)
    await db.commit()
    assert rel["released"] == 250
    [n] = await _notes(db, owner.id, "balance.available")
    assert n.data == {"amount": common.brl(250)} and common.brl(250) in n.body
    assert n.dedupe_key == f"balance.available:{PREV.isoformat()}:{est.id}"
    blob = f"{n.title} {n.body} {n.data} {n.link}"
    assert adv.name not in blob and str(cid) not in blob and str(order.id) not in blob  # sem dados do anunciante
    assert await _notes(db, other_owner.id, "balance.available") == []
    # liberação repetida (idempotente) não duplica o aviso
    again = await commission.release_competence(db, PREV)
    await db.commit()
    assert again["already"] is True
    assert len(await _notes(db, owner.id, "balance.available")) == 1


async def test_clawback_of_withdrawn_commission_emits_negative_balance_once(db, fin_env):
    org, est, owner, adv, cid, order = await _released(db)
    await commission.release_competence(db, PREV)
    await db.commit()
    # comissão disponível saiu (pagamento ao estabelecimento em trânsito)
    avail = await ledger.account(db, "EST:CommissionPayable:Available", owner_id=est.id, org_id=est.org_id)
    transit = await ledger.account(db, "EST:CommissionPayable:InTransit", owner_id=est.id, org_id=est.org_id)
    await ledger.post(db, kind="TEST_PAYOUT", idempotency_key=f"payout:{uuid.uuid4()}", legs=[(avail, 250), (transit, -250)],
                      description="saque de teste")
    await db.commit()
    (terms,) = await commission.terms_of(db, cid)
    r = await commission.clawback_terms(db, terms, fraction=Fraction(1), key="refund:test", reason="estorno integral")
    await db.commit()
    assert r["closed_clawback_cents"] == 250 and r["establishments"][str(est.id)]["negative_balance"] == 250
    [n] = await _notes(db, owner.id, common.EVENT_BALANCE_NEGATIVE)
    assert n.data == {"amount": common.brl(250)} and adv.name not in f"{n.title} {n.body}"
    assert (await commission.establishment_balance(db, est.id))["withdrawals_blocked"] is True
    # mesmo estorno repetido: nada novo no ledger nem nas notificações
    r2 = await commission.clawback_terms(db, terms, fraction=Fraction(1), key="refund:test", reason="estorno integral")
    await db.commit()
    assert r2["closed_clawback_cents"] == 0
    assert len(await _notes(db, owner.id, common.EVENT_BALANCE_NEGATIVE)) == 1
    assert (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.event_type == common.EVENT_BALANCE_NEGATIVE))).scalar_one() == 1
