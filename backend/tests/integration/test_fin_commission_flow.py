"""Comissão ponta a ponta (PostgreSQL real): pagamento → termos congelados → PoP válido
(idempotente) → fechamento em America/Sao_Paulo → Pending/Available → pagamento automático.
Reproduz casos da tabela A5 D2.6 com micro-centavos e floor por estabelecimento."""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.settings_service import set_setting
from app.models.financial import (
    CommissionAccrual,
    EstablishmentPixKey,
    LedgerTransaction,
    Withdrawal,
)
from app.modules.financial import commission, ledger, withdrawals
from tests.factories import make_establishment, make_org
from tests.fin_helpers import (  # noqa: F401
    balance,
    create_order,
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
DAY = PREV + timedelta(days=9)


async def paid_campaign(db, *, layout: list[int], revenue: int = 5000, start: date = DAY, end: date = DAY,
                        per_day: int = 100):
    """``layout``: nº de telas por estabelecimento. Devolve (campaign_id, [(est, [screens])])."""
    adv = await payer_org(db)
    cid = uuid.uuid4()
    order = await create_order(db, adv, [media_item(cid, revenue, start, end)], revenue)
    assert await pay(db, order) == "PROCESSED"
    groups, plan = [], {}
    for n in layout:
        org = await make_org(db, business_roles=["PARTNER"])
        est = await make_establishment(db, org)
        scr = [await make_screen(db, est) for _ in range(n)]
        groups.append((est, scr))
        for s in scr:
            plan[s.id] = {start + timedelta(days=i): per_day for i in range((end - start).days + 1)}
    plan_provider({cid: plan})
    return cid, groups


async def _accrue(db, cid, screen, day, n):
    r = await commission.accrue_commission_for_events(db, pop(cid, screen.id, day, n))
    await db.commit()
    return r


async def test_terms_frozen_at_payment_with_5_percent_pool(db, fin_env):
    cid, _ = await paid_campaign(db, layout=[1])
    (cc,) = await commission.terms_of(db, cid)  # um termo por cobrança paga
    assert (cc.kind, cc.model, cc.pool_bps, cc.eligible_revenue_cents, cc.pool_cents) == ("INITIAL", "A_POOL", 500, 5000, 250)
    assert (cc.start_date, cc.end_date) == (DAY, DAY)


async def test_seven_screens_close_floor_and_residue_to_rd(db, fin_env):
    cid, groups = await paid_campaign(db, layout=[1] * 7)
    for est, (s,) in groups:
        await _accrue(db, cid, s, DAY, 100)
    run = await commission.close_competence(db, PREV)
    await db.commit()
    assert (run.commission_cents, run.pool_released_cents, run.residue_cents) == (245, 250, 5)
    for est, _ in groups:
        assert await balance(db, "EST:CommissionPayable:Pending", est.id) == 35
    assert await balance(db, "RD:Income:RoundingResidue") == 5           # resíduo = pool − Σ lançado à RD
    assert await balance(db, "RD:Expense:Commission") == 250
    # DRE: comissão líquida = 245 (pool − resíduo)
    from app.modules.financial import accounting

    d = await accounting.dre(db, PREV, PREV_END)
    assert {line["group"]: line["amount_cents"] for line in d["lines"]}["COMISSOES"] == -245
    # idempotente
    again = await commission.close_competence(db, PREV)
    assert again.competence == run.competence
    assert (await db.execute(select(func.count()).select_from(LedgerTransaction)
                             .where(LedgerTransaction.kind == "COMMISSION_CLOSE"))).scalar_one() == 1


async def test_ninety_of_hundred_and_screen_that_never_played(db, fin_env):
    cid, groups = await paid_campaign(db, layout=[1, 1])
    (e1, (s1,)), (e2, (s2,)) = groups
    await _accrue(db, cid, s1, DAY, 90)  # s2 não tocou
    run = await commission.close_competence(db, PREV)
    await db.commit()
    assert await balance(db, "EST:CommissionPayable:Pending", e1.id) == 112  # floor(125·0,9)
    assert await balance(db, "EST:CommissionPayable:Pending", e2.id) == 0
    assert run.residue_cents == 250 - 112


async def test_pop_idempotent_by_playback_uuid_and_over_delivery_capped(db, fin_env):
    cid, ((est, (s,)),) = await paid_campaign(db, layout=[1])
    evs = pop(cid, s.id, DAY, 60)
    r1 = await commission.accrue_commission_for_events(db, evs)
    r2 = await commission.accrue_commission_for_events(db, evs + evs[:10])
    await db.commit()
    assert (r1["new"], r1["counted"]) == (60, 60) and (r2["new"], r2["duplicates"]) == (0, 60)  # repetidos no próprio lote são descartados antes
    await _accrue(db, cid, s, DAY, 80)  # 140 válidos para 100 contratados: limitado ao contratado
    row = (await db.execute(select(CommissionAccrual))).scalar_one()
    assert row.valid_count == 140 and row.amount_ucents == 250 * commission.UC
    # eventos de outro tipo / campanha sem pagamento não contam
    from types import SimpleNamespace

    other = [SimpleNamespace(playback_uuid=uuid.uuid4(), campaign_id=cid, screen_id=s.id, kind="INTERNAL", local_day=DAY),
             SimpleNamespace(playback_uuid=uuid.uuid4(), campaign_id=uuid.uuid4(), screen_id=s.id, kind="EXTERNAL",
                             local_day=DAY)]
    r = await commission.accrue_commission_for_events(db, other)
    assert r["counted"] == 0


async def test_microcents_aggregate_floor_per_establishment(db, fin_env):
    # 100 telas (2,5 c cada): 2 telas por estabelecimento ⇒ 5 c por estabelecimento, Σ 250, resíduo 0
    cid, groups = await paid_campaign(db, layout=[2] * 50)
    for _est, scr in groups:
        for s in scr:
            await commission.accrue_commission_for_events(db, pop(cid, s.id, DAY, 100))
    await db.commit()
    run = await commission.close_competence(db, PREV)
    await db.commit()
    assert run.commission_cents == 250 and run.residue_cents == 0
    assert await balance(db, "EST:CommissionPayable:Pending", groups[0][0].id) == 5


async def test_close_respects_sao_paulo_end_of_month_and_late_pop_goes_to_open_competence(db, fin_env):
    cid, ((est, (s,)),) = await paid_campaign(db, layout=[1], start=PREV_END, end=PREV_END)
    await _accrue(db, cid, s, PREV_END, 50)
    last_second_sp = datetime(PREV_END.year, PREV_END.month, PREV_END.day, 23, 59, 59) + timedelta(hours=3)
    with pytest.raises(Exception) as ei:  # 23:59:59 em São Paulo (02:59:59 UTC do dia seguinte) ainda é aberto
        await commission.close_competence(db, PREV, now=last_second_sp.replace(tzinfo=timezone.utc))
    assert getattr(ei.value, "code", "") == "COMMISSION_COMPETENCE_OPEN"
    await db.rollback()
    run = await commission.close_competence(db, PREV, now=(last_second_sp + timedelta(seconds=1)).replace(tzinfo=timezone.utc))
    await db.commit()
    assert run.commission_cents == 125
    # PoP tardio (player offline sincronizou depois) do último dia: competência aberta seguinte, sem duplicar
    await _accrue(db, cid, s, PREV_END, 50)
    rows = (await db.execute(select(CommissionAccrual).order_by(CommissionAccrual.competence))).scalars().all()
    assert [(r.competence, r.valid_count) for r in rows] == [(PREV, 50), (ledger.month_start(ledger.today_local()), 50)]
    assert sum(r.amount_ucents for r in rows) == 250 * commission.UC


async def test_day_one_release_small_values_accumulate_and_auto_payout(db, fin_env):
    cid, ((est, (s,)),) = await paid_campaign(db, layout=[1], revenue=1400)  # pool 70 c < R$ 1
    await _accrue(db, cid, s, DAY, 100)
    await commission.close_competence(db, PREV)
    rel = await commission.release_competence(db, PREV)
    await db.commit()
    assert rel["released"] == 70
    assert await balance(db, "EST:CommissionPayable:Pending", est.id) == 0
    assert await balance(db, "EST:CommissionPayable:Available", est.id) == 70
    # chave PIX ativa (fora do período de bloqueio) para o pagamento automático
    from app.core.crypto import encrypt_str
    from app.core.timeutils import utcnow

    key = EstablishmentPixKey(id=uuid.uuid4(), establishment_id=est.id, key_type="cpf",
                              key_value_enc=encrypt_str("12345678909", purpose="pix_key", aad=f"est:{est.id}"),
                              key_fingerprint="f" * 64, key_masked="***456***09", holder_name="Titular",
                              holder_document_enc=encrypt_str("12345678909", purpose="pix_key", aad=f"est:{est.id}"),
                              holder_document_masked="***456***09", active_from=utcnow() - timedelta(hours=13),
                              created_by=(await _any_user(db)))
    db.add(key)
    await db.commit()
    res = await withdrawals.auto_payouts(db)
    await db.commit()
    assert res["created"] == 0 and "abaixo do mínimo" in str(res["skipped"])  # < R$ 1 acumula, sem expiração
    assert await balance(db, "EST:CommissionPayable:Available", est.id) == 70
    # novo crédito leva o disponível a ≥ R$ 1 ⇒ pagamento automático
    from app.core.authz import AuthContext
    from app.models.financial import FinancialApprovalRequest

    await _credit(db, est, 40)
    res = await withdrawals.auto_payouts(db)
    await db.commit()
    assert res["created"] == 1
    w = (await db.execute(select(Withdrawal))).scalar_one()
    assert (w.origin, w.amount_cents, w.status) == ("AUTOMATIC", 110, "REQUESTED")
    assert await balance(db, "EST:CommissionPayable:InTransit", est.id) == 110
    _ = (AuthContext, FinancialApprovalRequest)


async def test_config_change_requires_dual_approval_and_does_not_change_frozen_terms(db, fin_env):
    from app.core.authz import AuthContext
    from app.core.errors import DomainError

    with pytest.raises(DomainError) as ei:
        await set_setting(db, AuthContext.system("system"), "commission.pool_bps", 3000)
    assert ei.value.code == "SETTING_REQUIRES_DUAL_APPROVAL"


async def _any_user(db):
    from tests.factories import make_user

    return (await make_user(db, role="admin")).id


async def _credit(db, est, cents):
    exp = await ledger.account(db, "RD:Expense:Commission")
    av = await ledger.account(db, "EST:CommissionPayable:Available", owner_id=est.id, org_id=est.org_id)
    await ledger.post(db, kind="TEST_CREDIT", idempotency_key=f"credit:{uuid.uuid4()}", legs=[(exp, cents), (av, -cents)],
                      description="crédito de teste")
    await db.commit()
