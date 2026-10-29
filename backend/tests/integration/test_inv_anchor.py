"""W-INV — âncora da TELA write-once (ADR_006 §3; RDPP §5.1), reset auditado e histórico append-only."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.errors import DomainError
from app.models.inventory import ScreenAnchorHistory, ScreenInventoryState
from app.modules.inventory import service as inv
from tests.factories import make_user
from tests.inv_support import mk_establishment, mk_org, mk_screen


async def _now_ms(db) -> int:
    return int((await db.execute(text("SELECT floor(extract(epoch FROM now()) * 1000)::bigint"))).scalar_one())


async def _screen(db):
    org = await mk_org(db)
    est = await mk_establishment(db, org)
    return await mk_screen(db, est)


async def test_first_start_sets_anchor_and_reboot_keeps_it(db):
    scr = await _screen(db)
    now = await _now_ms(db)
    a = await inv.set_anchor(db, scr.id, now - 1234, None)
    await db.commit()
    assert a == (now - 1234) - (now - 1234) % 1000  # arredondado ao segundo
    # reboot / outro aparelho: segunda tentativa devolve a ORIGINAL (write-once)
    b = await inv.set_anchor(db, scr.id, now + 5000, None)
    await db.commit()
    c = await inv.set_anchor(db, scr.id, None, None)
    await db.commit()
    assert a == b == c
    hist = (await db.execute(select(ScreenAnchorHistory).where(ScreenAnchorHistory.screen_id == scr.id))).scalars().all()
    assert len(hist) == 1 and hist[0].source == "PLAYER"
    st = await db.get(ScreenInventoryState, scr.id)
    assert st.anchor_source == "PLAYER"
    # o manifesto passa a levar a âncora
    m = (await inv.current_manifest(db, scr.id)).manifest
    await db.commit()
    assert m["anchor_ms"] == a


async def test_player_clock_skew_over_120s_uses_server_time(db):
    scr = await _screen(db)
    now = await _now_ms(db)
    a = await inv.set_anchor(db, scr.id, now - 10 * 60 * 1000, None)  # relógio 10 min atrasado
    await db.commit()
    assert abs(a - now) < 5000
    st = await db.get(ScreenInventoryState, scr.id)
    assert st.anchor_source == "SERVER"


async def test_trigger_blocks_direct_overwrite(db):
    scr = await _screen(db)
    await inv.set_anchor(db, scr.id, await _now_ms(db), None)
    await db.commit()
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("UPDATE screen_inventory_state SET anchor_utc = anchor_utc + interval '7 minutes' "
                              "WHERE screen_id = :s"), {"s": scr.id})
    assert "RD_ANCHOR_WRITE_ONCE" in str(ei.value)
    await db.rollback()


async def test_reset_requires_reason_and_writes_history(db):
    scr = await _screen(db)
    from types import SimpleNamespace

    from app.core.authz import AuthContext

    admin_id = (await make_user(db, role="super_admin", mfa=True)).id
    ctx = AuthContext(kind="user", user=SimpleNamespace(id=admin_id))
    first = await inv.set_anchor(db, scr.id, await _now_ms(db), None)
    await db.commit()
    with pytest.raises(DomainError):
        await inv.reset_anchor(db, ctx, scr.id, "curta")
    await db.rollback()
    with pytest.raises(DBAPIError) as ei:  # a função SQL também exige justificativa
        await db.execute(text("SELECT rd_reset_screen_anchor(CAST(:s AS uuid), NULL, 'x', NULL)"), {"s": scr.id})
    assert "RD_ANCHOR_RESET_REASON" in str(ei.value)
    await db.rollback()
    await inv.reset_anchor(db, ctx, scr.id, "Troca de grade pedida pelo cliente — chamado 123")
    await db.commit()
    db.expire_all()
    st = await db.get(ScreenInventoryState, scr.id)
    assert st.anchor_utc is None and st.anchor_source is None
    # próximo start válido define nova âncora
    new = await inv.set_anchor(db, scr.id, await _now_ms(db), None)
    await db.commit()
    assert isinstance(new, int) and first is not None  # após o reset, um novo start é aceito
    hist = (await db.execute(select(ScreenAnchorHistory).where(ScreenAnchorHistory.screen_id == scr.id)
                             .order_by(ScreenAnchorHistory.created_at))).scalars().all()
    assert [h.source for h in hist] == ["PLAYER", "ADMIN_RESET", "PLAYER"]
    assert hist[1].reason.startswith("Troca de grade") and hist[1].actor_user_id == admin_id
    n = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'screen.anchor_reset'"))).scalar_one()
    assert n == 1


async def test_anchor_history_is_append_only(db):
    scr = await _screen(db)
    await inv.set_anchor(db, scr.id, await _now_ms(db), None)
    await db.commit()
    with pytest.raises(DBAPIError) as ei:
        await db.execute(text("UPDATE screen_anchor_history SET reason = 'adulterado' WHERE screen_id = :s"),
                         {"s": scr.id})
    assert "RD_APPEND_ONLY" in str(ei.value)
    await db.rollback()
    with pytest.raises(DBAPIError):
        await db.execute(text("DELETE FROM screen_anchor_history WHERE screen_id = :s"), {"s": scr.id})
    await db.rollback()


async def test_device_replacement_keeps_anchor(db):
    """Âncora pertence à TELA: um aparelho novo (outro device_id) adota a âncora existente."""
    scr = await _screen(db)
    a = await inv.set_anchor(db, scr.id, await _now_ms(db) - 60_000, None)
    await db.commit()
    b = await inv.set_anchor(db, scr.id, await _now_ms(db), None)
    await db.commit()
    assert a == b
