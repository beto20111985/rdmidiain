"""W-INV — concorrência REAL no PostgreSQL (conexões distintas, commits reais):
checkouts disputando o mesmo inventário, conjuntos de telas sobrepostos em ordens opostas
e corrida sweeper × webhook (A6 §10.3–§10.4)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from app.core.authz import AuthContext
from app.core.database import session_factory
from app.core.errors import Conflict, DomainError
from app.models.inventory import BookingScreen, InventoryHold
from app.modules.inventory import service as inv
from tests.inv_support import backdate_hold, db_today, mk_campaign, mk_establishment, mk_org, mk_screen, pattern

SYS = AuthContext.system("system")
FULL = dict(duration_units=4, freq_value=15)


async def _attempt(engine, campaign_id, screen_ids, pats, gate: asyncio.Event | None = None):
    """Um checkout em sua PRÓPRIA conexão/transação (como duas requisições)."""
    async with session_factory(engine)() as s:
        try:
            if gate is not None:
                await gate.wait()
            h = await inv.hold(s, SYS, campaign_id, screen_ids, pats, str(uuid.uuid4()))
            await s.commit()
            return ("ok", h.id)
        except (Conflict, DomainError) as exc:
            await s.rollback()
            return ("refused", exc.code)


async def _setup(db, n_screens=1):
    owner = await mk_org(db)
    est = await mk_establishment(db, owner)
    screens = [await mk_screen(db, est) for _ in range(n_screens)]
    today = await db_today(db)
    return screens, today + timedelta(days=1)


async def _held_units(db, screen_id) -> int:
    rows = (await db.execute(text("""
        SELECT bp.duration_units * bp.freq_value FROM booking_screens bs JOIN booking_patterns bp ON bp.id = bs.pattern_id
         WHERE bs.screen_id = :s AND (bs.status = 'BOOKED' OR (bs.status = 'HELD' AND bs.hold_expires_at > now()))
    """), {"s": screen_id})).scalars().all()
    return sum(rows)


async def test_two_concurrent_checkouts_exactly_one_wins(db, db_engine):
    (scr,), d = await _setup(db)
    for _ in range(5):  # várias rodadas para expor qualquer janela de corrida
        a = await mk_campaign(db, await mk_org(db))
        b = await mk_campaign(db, await mk_org(db))
        gate = asyncio.Event()
        tasks = [asyncio.create_task(_attempt(db_engine, c.id, [scr.id], [pattern(d, d, **FULL)], gate))
                 for c in (a, b)]
        await asyncio.sleep(0)
        gate.set()
        res = await asyncio.gather(*tasks)
        assert sorted(r[0] for r in res) == ["ok", "refused"], res
        assert [r[1] for r in res if r[0] == "refused"] == ["INVENTORY_UNAVAILABLE"]
        assert await _held_units(db, scr.id) == 60  # nunca acima da capacidade 2+2 (60 un./h)
        # libera para a próxima rodada
        winner = [r[1] for r in res if r[0] == "ok"][0]
        await inv.release_hold(db, winner, "TEST_ROUND")
        await db.commit()


async def test_twenty_concurrent_checkouts_for_last_capacity_one_success(db, db_engine):
    (scr,), d = await _setup(db)
    camps = [await mk_campaign(db, await mk_org(db)) for _ in range(20)]
    gate = asyncio.Event()
    tasks = [asyncio.create_task(_attempt(db_engine, c.id, [scr.id], [pattern(d, d, **FULL)], gate)) for c in camps]
    await asyncio.sleep(0)
    gate.set()
    res = await asyncio.gather(*tasks)
    assert sum(1 for r in res if r[0] == "ok") == 1
    assert all(r[1] in ("INVENTORY_UNAVAILABLE", "INVENTORY_BUSY") for r in res if r[0] == "refused")
    n = (await db.execute(select(func.count()).select_from(InventoryHold).where(InventoryHold.status == "HELD"))
         ).scalar_one()
    assert n == 1 and await _held_units(db, scr.id) == 60


async def test_overlapping_screen_sets_in_opposite_orders_do_not_deadlock(db, db_engine):
    screens, d = await _setup(db, n_screens=4)
    ids = [s.id for s in screens]
    for r in range(4):
        a = await mk_campaign(db, await mk_org(db))
        b = await mk_campaign(db, await mk_org(db))
        gate = asyncio.Event()
        order_a = ids[:3]
        order_b = list(reversed(ids[1:]))  # sobrepõe 2 telas, ordem inversa
        tasks = [asyncio.create_task(_attempt(db_engine, a.id, order_a, [pattern(d, d)], gate)),
                 asyncio.create_task(_attempt(db_engine, b.id, order_b, [pattern(d, d)], gate))]
        await asyncio.sleep(0)
        gate.set()
        res = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
        assert [x[0] for x in res] == ["ok", "ok"], res  # ambos cabem; nenhum deadlock / timeout
    held = (await db.execute(select(func.count()).select_from(BookingScreen))).scalar_one()
    assert held == 4 * (3 + 3)


async def test_sweeper_vs_webhook_race_on_expired_hold(db, db_engine):
    (scr,), d = await _setup(db)
    for _ in range(4):
        camp = await mk_campaign(db, await mk_org(db))
        async with session_factory(db_engine)() as s:
            h = await inv.hold(s, SYS, camp.id, [scr.id], [pattern(d, d, freq_value=2)], str(uuid.uuid4()))
            await s.commit()
            hid = h.id
        await backdate_hold(db, hid, 2 * 3600 + 30)

        async def sweeper():
            async with session_factory(db_engine)() as s:
                out = await inv.sweep_expired_holds(s)
                await s.commit()
                return out

        async def webhook():
            async with session_factory(db_engine)() as s:
                r = await inv.convert_hold(s, hid)
                await s.commit()
                return r

        out, r = await asyncio.gather(sweeper(), webhook())
        db.expire_all()
        final = (await db.get(InventoryHold, hid)).status
        statuses = set((await db.execute(select(BookingScreen.status).where(BookingScreen.hold_id == hid))).scalars())
        # pagamento tardio com capacidade disponível: o resultado é sempre CONVERTIDO e consistente
        assert r.status == "CONVERTED" and final == "CONVERTED" and statuses == {"BOOKED"}, (out, r, final, statuses)
        await inv.release_hold(db, hid, "SUPERSEDED_BY_REVISION")
        await db.commit()


async def test_sweeper_vs_webhook_on_valid_hold_never_expires_paid(db, db_engine):
    (scr,), d = await _setup(db)
    camp = await mk_campaign(db, await mk_org(db))
    async with session_factory(db_engine)() as s:
        h = await inv.hold(s, SYS, camp.id, [scr.id], [pattern(d, d)], "k")
        await s.commit()
        hid = h.id

    async def sweeper():
        async with session_factory(db_engine)() as s:
            out = await inv.sweep_expired_holds(s)
            await s.commit()
            return out

    async def webhook():
        async with session_factory(db_engine)() as s:
            r = await inv.convert_hold(s, hid)
            await s.commit()
            return r

    out, r = await asyncio.gather(sweeper(), webhook())
    assert out == [] and r.status == "CONVERTED" and r.reason is None
