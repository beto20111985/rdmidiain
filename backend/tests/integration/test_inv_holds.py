"""W-INV — reserva 2 h, conversão, pagamento tardio, expiração (lazy + sweeper), exclusividade,
idempotência e mudança de política — PostgreSQL REAL (commits reais)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
import time_machine
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.authz import AuthContext
from app.core.errors import Conflict, DomainError
from app.models.inventory import BookingScreen, InventoryHold
from app.models.rotation import RotationPolicy
from app.modules.inventory import service as inv
from app.modules.inventory.policies import create_policy
from tests.inv_support import (
    backdate_hold,
    competitor_link,
    db_today,
    mk_campaign,
    mk_establishment,
    mk_org,
    mk_screen,
    pattern,
    ref,
)

SYS = AuthContext.system("system")
FULL = dict(duration_units=4, freq_value=15)  # 120 s, 15/h = TODO o externo de 2+2


@pytest_asyncio.fixture
async def env(db):
    owner = await mk_org(db, "Dono")
    est = await mk_establishment(db, owner)
    s1 = await mk_screen(db, est)
    s2 = await mk_screen(db, est)
    adv = await mk_org(db, "Anunciante A")
    adv2 = await mk_org(db, "Anunciante B")
    today = await db_today(db)
    return SimpleNamespace(owner=owner, est=est, s1=s1, s2=s2, adv=adv, adv2=adv2, today=today,
                           d1=today + timedelta(days=1), d3=today + timedelta(days=3))


async def _hold(db, camp, screens, pats, key=None, **kw):
    h = await inv.hold(db, SYS, camp.id, [s.id for s in screens], pats, key or str(uuid.uuid4()), **kw)
    await db.commit()
    r = ref(h)
    r.replayed, r.held_screen_ids, r.excluded = h.replayed, h.held_screen_ids, h.excluded
    return r


async def test_hold_ttl_is_exactly_two_hours_by_db_clock(db, env):
    camp = await mk_campaign(db, env.adv)
    h = await _hold(db, camp, [env.s1], [pattern(env.d1, env.d3, freq_value=2)])
    row = (await db.execute(text("SELECT hold_expires_at - created_at, status FROM inventory_holds WHERE id = :i"),
                            {"i": h.id})).one()
    assert row[0] == timedelta(hours=2) and row[1] == "HELD"
    bs = (await db.execute(select(BookingScreen).where(BookingScreen.hold_id == h.id))).scalars().all()
    assert len(bs) == 1 and bs[0].status == "HELD" and bs[0].conflict_result == "ALLOW"
    assert bs[0].rotation_snapshot["internal_seconds"] == 120 and bs[0].tz_snapshot == "America/Sao_Paulo"


async def test_capacity_exhausted_is_refused_never_overbooked(db, env):
    c1, c2 = await mk_campaign(db, env.adv), await mk_campaign(db, env.adv2)
    await _hold(db, c1, [env.s1], [pattern(env.d1, env.d3, **FULL)])
    with pytest.raises(Conflict) as ei:
        await _hold(db, c2, [env.s1], [pattern(env.d1, env.d1, freq_value=1)])
    assert ei.value.code == "INVENTORY_UNAVAILABLE"
    await db.rollback()
    # dia sem sobreposição cabe
    h = await _hold(db, c2, [env.s1], [pattern(env.d3 + timedelta(days=1), env.d3 + timedelta(days=1))])
    assert h.status == "HELD"


async def test_idempotency_replay_and_divergent_body(db, env):
    camp = await mk_campaign(db, env.adv)
    pats = [pattern(env.d1, env.d3, freq_value=2)]
    h1 = await _hold(db, camp, [env.s1], pats, key="k-1")
    h2 = await _hold(db, camp, [env.s1], pats, key="k-1")
    assert h1.id == h2.id and h2.replayed is True
    with pytest.raises(Conflict) as ei:
        await _hold(db, camp, [env.s1], [pattern(env.d1, env.d3, freq_value=3)], key="k-1")
    assert ei.value.code == "IDEMPOTENCY_KEY_REUSED"
    await db.rollback()
    # replay vale mesmo depois de expirar (A6 §10.2: devolve o mesmo hold)
    await backdate_hold(db, h1.id, 7300)
    h3 = await _hold(db, camp, [env.s1], pats, key="k-1")
    assert h3.id == h1.id


async def test_one_active_hold_per_campaign(db, env):
    camp = await mk_campaign(db, env.adv)
    await _hold(db, camp, [env.s1], [pattern(env.d1, env.d1)], key="a")
    with pytest.raises(Conflict) as ei:
        await _hold(db, camp, [env.s2], [pattern(env.d1, env.d1)], key="b")
    assert ei.value.code == "HOLD_ALREADY_ACTIVE"


async def test_partial_mode_and_all_or_nothing(db, env):
    c1, c2 = await mk_campaign(db, env.adv), await mk_campaign(db, env.adv2)
    await _hold(db, c1, [env.s1], [pattern(env.d1, env.d1, **FULL)])
    with pytest.raises(Conflict) as ei:
        await _hold(db, c2, [env.s1, env.s2], [pattern(env.d1, env.d1)], key="all")
    assert ei.value.details["available"] == 1 and ei.value.details["requested"] == 2
    await db.rollback()
    h = await _hold(db, c2, [env.s1, env.s2], [pattern(env.d1, env.d1)], key="part", partial=True)
    assert h.held_screen_ids == [env.s2.id] and h.mode == "PARTIAL" and h.screens_held == 1
    assert h.excluded[0]["reason"] == "INVENTORY_UNAVAILABLE"


async def test_unsellable_and_ineligible_duration(db, env):
    off = await mk_screen(db, env.est, sellable=False)
    camp = await mk_campaign(db, env.adv)
    q = await inv.quote_capacity(db, SYS, [off.id, env.s1.id], [pattern(env.d1, env.d1)], campaign_id=camp.id)
    by = {s.screen_id: s for s in q.screens}
    assert by[off.id].reason == "SCREEN_NOT_SELLABLE" and by[env.s1.id].feasible
    # tela com política 3+1: 120 s inelegível (nunca cortar)
    now = (await db.execute(text("SELECT now()"))).scalar_one()
    await create_policy(db, SYS, scope="SCREEN", scope_id=env.s2.id, internal_seconds=180, external_seconds=60,
                        effective_from=now + timedelta(seconds=5), reason="parceiro premium 3+1")
    await db.commit()
    q = await inv.quote_capacity(db, SYS, [env.s2.id], [pattern(env.d1, env.d1, duration_units=4)],
                                 campaign_id=camp.id)
    assert q.screens[0].reason == "DURATION_NOT_ELIGIBLE" and not q.feasible
    q = await inv.quote_capacity(db, SYS, [env.s2.id], [pattern(env.d1, env.d1, duration_units=2)],
                                 campaign_id=camp.id)
    assert q.feasible and q.screens[0].units_per_hour == 30


async def test_incompatible_frequency_refused_at_quote(db, env):
    camp = await mk_campaign(db, env.adv)
    q = await inv.quote_capacity(db, SYS, [env.s1.id], [pattern(env.d1, env.d1, freq_value=6,
                                                                min_interval_seconds=600)], campaign_id=camp.id)
    assert not q.feasible and q.screens[0].reason == "FREQUENCY_INCOMPATIBLE"


async def test_competitor_exclusivity(db, env):
    await competitor_link(db, env.adv, env.adv2)
    c1, c2 = await mk_campaign(db, env.adv), await mk_campaign(db, env.adv2)
    await _hold(db, c1, [env.s1], [pattern(env.d1, env.d3, exclusivity="COMPETITOR_EXCLUSIVE")])
    with pytest.raises(Conflict) as ei:
        await _hold(db, c2, [env.s1], [pattern(env.d3, env.d3)])
    assert ei.value.details["screens"][0]["reason"] == "EXCLUSIVITY_CONFLICT"
    await db.rollback()
    ok = await _hold(db, c2, [env.s1], [pattern(env.d3 + timedelta(days=1), env.d3 + timedelta(days=2))])
    assert ok.status == "HELD"
    # booking exclusivo também é recusado se já houver concorrente reservado no período
    c3 = await mk_campaign(db, env.adv)
    with pytest.raises(Conflict) as ei:
        await _hold(db, c3, [env.s1], [pattern(env.d3 + timedelta(days=2), env.d3 + timedelta(days=2),
                                               exclusivity="COMPETITOR_EXCLUSIVE")])
    assert ei.value.details["screens"][0]["reason"] == "EXCLUSIVITY_CONFLICT"


async def test_convert_in_time_and_idempotent(db, env):
    camp = await mk_campaign(db, env.adv)
    h = await _hold(db, camp, [env.s1, env.s2], [pattern(env.d1, env.d3)])
    r = await inv.convert_hold(db, h.id)
    await db.commit()
    assert (r.status, r.already_processed, r.refund_required) == ("CONVERTED", False, False)
    st = (await db.execute(select(BookingScreen.status).where(BookingScreen.hold_id == h.id))).scalars().all()
    assert st == ["BOOKED", "BOOKED"]
    r2 = await inv.convert_hold(db, h.id)
    assert r2.status == "CONVERTED" and r2.already_processed


async def test_python_clock_is_irrelevant_db_clock_rules(db, env):
    """O TTL é do relógio do BANCO: avançar o relógio do Python 3 h não expira a reserva."""
    camp = await mk_campaign(db, env.adv)
    h = await _hold(db, camp, [env.s1], [pattern(env.d1, env.d1)])
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(hours=3), tick=False):
        r = await inv.convert_hold(db, h.id)
        await db.commit()
    assert r.status == "CONVERTED" and r.reason is None  # caminho "no prazo"


async def test_expiry_lazy_then_sweeper_idempotent(db, env):
    c1, c2 = await mk_campaign(db, env.adv), await mk_campaign(db, env.adv2)
    h1 = await _hold(db, c1, [env.s1], [pattern(env.d1, env.d1, **FULL)])
    with pytest.raises(Conflict):
        await _hold(db, c2, [env.s1], [pattern(env.d1, env.d1)], key="before")
    await db.rollback()
    await backdate_hold(db, h1.id, 2 * 3600 - 59)  # ainda vale (59 s)
    with pytest.raises(Conflict):
        await _hold(db, c2, [env.s1], [pattern(env.d1, env.d1)], key="almost")
    await db.rollback()
    await backdate_hold(db, h1.id, 60)  # passou de 2 h pelo relógio do banco
    # LAZY: sem sweeper, a capacidade já está livre para a próxima venda
    h2 = await _hold(db, c2, [env.s1], [pattern(env.d1, env.d1)], key="after")
    assert h2.status == "HELD"
    st = (await db.get(InventoryHold, h1.id, populate_existing=True)).status
    assert st == "HELD", "o sweeper ainda não rodou (expiração lazy)"
    out = await inv.sweep_expired_holds(db)
    await db.commit()
    assert [o["hold_id"] for o in out] == [str(h1.id)]
    db.expire_all()
    assert (await db.get(InventoryHold, h1.id)).status == "EXPIRED"
    bs = (await db.execute(select(BookingScreen.status).where(BookingScreen.hold_id == h1.id))).scalars().all()
    assert set(bs) == {"EXPIRED"}
    assert await inv.sweep_expired_holds(db) == []  # idempotente
    await db.commit()


async def test_late_payment_recaptures_when_capacity_allows(db, env):
    camp = await mk_campaign(db, env.adv)
    h = await _hold(db, camp, [env.s1], [pattern(env.d1, env.d1, freq_value=3)])
    await backdate_hold(db, h.id, 3 * 3600)
    await inv.sweep_expired_holds(db)
    await db.commit()
    r = await inv.convert_hold(db, h.id)
    await db.commit()
    assert (r.status, r.reason, r.refund_required) == ("CONVERTED", "RECAPTURED", False)
    bs = (await db.execute(select(BookingScreen.status).where(BookingScreen.hold_id == h.id))).scalars().all()
    assert set(bs) == {"BOOKED"}


async def test_late_payment_without_capacity_is_paid_without_inventory(db, env):
    c1, c2 = await mk_campaign(db, env.adv), await mk_campaign(db, env.adv2)
    h1 = await _hold(db, c1, [env.s1], [pattern(env.d1, env.d1, **FULL)])
    await backdate_hold(db, h1.id, 2 * 3600 + 5)
    h2 = await _hold(db, c2, [env.s1], [pattern(env.d1, env.d1, **FULL)])  # vendeu a capacidade liberada
    await inv.convert_hold(db, h2.id)
    await db.commit()
    r = await inv.convert_hold(db, h1.id)  # pagamento tardio de c1
    await db.commit()
    assert (r.status, r.refund_required) == ("PAID_WITHOUT_INVENTORY", True)
    db.expire_all()
    s1 = (await db.execute(select(BookingScreen.status).where(BookingScreen.hold_id == h1.id))).scalars().all()
    s2 = (await db.execute(select(BookingScreen.status).where(BookingScreen.hold_id == h2.id))).scalars().all()
    assert set(s1) == {"EXPIRED"} and set(s2) == {"BOOKED"}, "nunca overbooking"
    again = await inv.convert_hold(db, h1.id)
    assert again.already_processed and again.status == "PAID_WITHOUT_INVENTORY"
    n = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'inventory.paid_without_inventory'"))
         ).scalar_one()
    assert n == 1


async def test_release_rules(db, env):
    c1 = await mk_campaign(db, env.adv)
    h = await _hold(db, c1, [env.s1], [pattern(env.d1, env.d1, **FULL)])
    r = await inv.release_hold(db, h.id, "PAYMENT_EXPIRED")
    await db.commit()
    assert r.status == "CANCELLED"
    c2 = await mk_campaign(db, env.adv2)
    h2 = await _hold(db, c2, [env.s1], [pattern(env.d1, env.d1, **FULL)])  # capacidade voltou
    await inv.convert_hold(db, h2.id)
    await db.commit()
    with pytest.raises(Conflict) as ei:
        await inv.release_hold(db, h2.id, "PAYMENT_EXPIRED")
    assert ei.value.code == "HOLD_CONVERTED"
    await db.rollback()
    r = await inv.release_hold(db, h2.id, "SUPERSEDED_BY_REVISION")
    await db.commit()
    assert r.status == "CANCELLED"
    assert (await inv.release_hold(db, h2.id, "x")).already_processed


async def test_revision_hold_replaces_converted_without_double_counting(db, env):
    camp = await mk_campaign(db, env.adv)
    h1 = await _hold(db, camp, [env.s1], [pattern(env.d1, env.d1, **FULL)])
    await inv.convert_hold(db, h1.id)
    await db.commit()
    # nova revisão do mesmo período: sem replaces conta em dobro e não cabe
    with pytest.raises(Conflict):
        await _hold(db, camp, [env.s1], [pattern(env.d1, env.d1, **FULL)], key="rev-no")
    await db.rollback()
    h2 = await _hold(db, camp, [env.s1], [pattern(env.d1, env.d1, **FULL)], key="rev", replaces_hold_id=h1.id)
    assert h2.status == "HELD" and h2.replaces_hold_id == h1.id


async def test_rotation_policy_db_checks_and_versions(db, env):
    now = (await db.execute(text("SELECT now()"))).scalar_one()
    db.add(RotationPolicy(id=uuid.uuid4(), scope="GLOBAL", scope_id=None, internal_seconds=120, external_seconds=150,
                          effective_from=now, reason="inválida C=270"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    with pytest.raises(DomainError) as ei:
        await create_policy(db, SYS, scope="GLOBAL", scope_id=None, internal_seconds=120, external_seconds=150,
                            effective_from=now + timedelta(hours=1), reason="C não divide 3600")
    assert ei.value.code == "ROTATION_CYCLE_NOT_DIVISOR"
    with pytest.raises(DomainError) as ei:
        await create_policy(db, SYS, scope="GLOBAL", scope_id=None, internal_seconds=120, external_seconds=120,
                            effective_from=now - timedelta(hours=1), reason="retroativa")
    assert ei.value.code == "POLICY_NOT_RETROACTIVE"
    p1 = await create_policy(db, SYS, scope="ESTABLISHMENT", scope_id=env.est.id, internal_seconds=180,
                             external_seconds=60, effective_from=now + timedelta(days=10), reason="versão 1 do teste")
    await db.commit()
    p2 = await create_policy(db, SYS, scope="ESTABLISHMENT", scope_id=env.est.id, internal_seconds=120,
                             external_seconds=120, effective_from=now + timedelta(days=20), reason="versão 2 do teste")
    id1, id2 = p1.id, p2.id
    await db.commit()
    db.expire_all()
    p1 = await db.get(RotationPolicy, id1)
    p2 = await db.get(RotationPolicy, id2)
    assert p1.effective_to == p2.effective_from and p2.version == 2 and p2.effective_to is None
    # EXCLUDE (btree_gist): vigências sobrepostas do mesmo escopo são impossíveis no banco
    db.add(RotationPolicy(id=uuid.uuid4(), scope="ESTABLISHMENT", scope_id=env.est.id, internal_seconds=60,
                          external_seconds=60, effective_from=now + timedelta(days=15), reason="sobreposta"))
    with pytest.raises((IntegrityError, DBAPIError)):
        await db.commit()
    await db.rollback()


async def test_policy_change_blocked_if_it_reduces_contracted_capacity(db, env):
    camp = await mk_campaign(db, env.adv)
    h = await _hold(db, camp, [env.s1], [pattern(env.d1, env.d3, duration_units=4, freq_value=2)])  # 120 s
    await inv.convert_hold(db, h.id)
    await db.commit()
    now = (await db.execute(text("SELECT now()"))).scalar_one()
    with pytest.raises(Conflict) as ei:  # 3+1 ⇒ bloco externo de 60 s: o 120 s contratado não caberia
        await create_policy(db, SYS, scope="SCREEN", scope_id=env.s1.id, internal_seconds=180, external_seconds=60,
                            effective_from=now + timedelta(minutes=1), reason="reduziria o contratado")
    assert ei.value.code == "POLICY_REDUCES_CONTRACTED_CAPACITY"
    assert ei.value.details["screens"][0]["screen_id"] == str(env.s1.id)
    await db.rollback()
    # a outra tela (sem contratação) pode mudar; e mudar DEPOIS do fim do contrato também
    await create_policy(db, SYS, scope="SCREEN", scope_id=env.s2.id, internal_seconds=180, external_seconds=60,
                        effective_from=now + timedelta(minutes=1), reason="sem contratação afetada")
    await create_policy(db, SYS, scope="SCREEN", scope_id=env.s1.id, internal_seconds=180, external_seconds=60,
                        effective_from=now + timedelta(days=10), reason="após o fim do contratado")
    await db.commit()


async def test_contracted_exhibitions_per_local_day(db, env):
    camp = await mk_campaign(db, env.adv)
    pats = [pattern(env.d1, env.d3, freq_value=2, time_bands=[[8 * 60, 18 * 60]]),
            pattern(env.d1, env.d1, freq_kind="SPECIFIC_TIME", specific_times=["12:00", "19:30"], freq_value=2,
                    duration_units=2)]
    h = await _hold(db, camp, [env.s1], pats)
    assert await inv.contracted_exhibitions(db, camp.id) == {}  # HELD não conta
    await inv.convert_hold(db, h.id)
    await db.commit()
    got = await inv.contracted_exhibitions(db, camp.id)
    assert got == {env.s1.id: {env.d1: 2 * 10 + 2, env.d1 + timedelta(days=1): 20, env.d3: 20}}


async def test_partner_plan_rotation_applies_and_explicit_scope_wins(db, env):
    from app.models.contracts import PartnerPlan
    from app.models.establishments import Establishment

    plan = PartnerPlan(id=uuid.uuid4(), name="Parceiro Premium", slug=f"premium-{uuid.uuid4().hex[:6]}",
                       tv_provision="OWN_TV", deposit_required=False, installation_fee_required=False,
                       upgrade_policy="FREE", default_internal_s=180, default_external_s=60)
    db.add(plan)
    await db.flush()
    est = await db.get(Establishment, env.est.id)
    est.partner_plan_id = plan.id
    await db.commit()
    st = await inv.screen_inventory_view(db, env.s1.id)
    assert (st["policy"]["scope"], st["policy"]["units_per_hour"]) == ("PARTNER_PLAN", 30)
    now = (await db.execute(text("SELECT now()"))).scalar_one()
    await create_policy(db, SYS, scope="SCREEN", scope_id=env.s1.id, internal_seconds=120, external_seconds=120,
                        effective_from=now + timedelta(seconds=2), reason="tela específica volta ao 2+2")
    await db.commit()
    q = await inv.quote_capacity(db, SYS, [env.s1.id, env.s2.id], [pattern(env.d1, env.d1, duration_units=4)],
                                 campaign_id=(await mk_campaign(db, env.adv)).id)
    by = {s.screen_id: s for s in q.screens}
    assert by[env.s1.id].feasible and by[env.s2.id].reason == "DURATION_NOT_ELIGIBLE"


async def test_maintenance_screen_stays_in_inventory(db, env):
    from app.models.screens import Screen

    scr = await db.get(Screen, env.s1.id)
    scr.status = "MAINTENANCE"
    await db.commit()
    camp = await mk_campaign(db, env.adv)
    h = await _hold(db, camp, [env.s1], [pattern(env.d1, env.d1)])
    assert h.held_screen_ids == [env.s1.id]


async def test_screen_permanent_removal_suspends_and_opens_report_without_redistribution(db, env):
    from app.models.inventory import ScreenRetirement
    from app.modules.screens import service as screens_service

    camp = await mk_campaign(db, env.adv)
    h = await _hold(db, camp, [env.s1, env.s2], [pattern(env.d1, env.d3)])
    await inv.convert_hold(db, h.id)
    await db.commit()
    # pelo hook OFICIAL do W-ORG (listener registrado no import de inventory.service)
    await screens_service.screen_permanently_removed(db, [env.s1.id], "TV furtada — estabelecimento encerrou")
    await db.commit()
    rows = dict((await db.execute(select(BookingScreen.screen_id, BookingScreen.status)
                                  .where(BookingScreen.hold_id == h.id))).all())
    assert rows == {env.s1.id: "SUSPENDED", env.s2.id: "BOOKED"}, "sem redistribuição automática"
    rep = (await db.execute(select(ScreenRetirement))).scalars().all()
    assert len(rep) == 1 and rep[0].status == "OPEN" and rep[0].affected[0]["campaign_id"] == str(camp.id)
    st = await inv.screen_inventory_view(db, env.s1.id)
    assert st["sellable"] is False
    q = await inv.quote_capacity(db, SYS, [env.s1.id], [pattern(env.d1, env.d1)])
    assert q.screens[0].reason == "SCREEN_NOT_SELLABLE"
