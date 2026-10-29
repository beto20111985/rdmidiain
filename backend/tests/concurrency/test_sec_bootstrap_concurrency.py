"""AÇÃO 003 — bootstrap concorrente (SPEC §10): 10 execuções simultâneas (conexões reais distintas,
advisory lock ``LOCK_BOOTSTRAP`` + funções definer + UNIQUE/CHECK) ⇒ 1 modo, 1 assento 1, 1 evento."""
from __future__ import annotations

import asyncio
import secrets

from sqlalchemy import text

from app.modules.security import bootstrap as boot


def _env(tmp_path, dual: str) -> dict:
    f = tmp_path / f"pw_{secrets.token_hex(3)}"
    f.write_text(f"Conc{secrets.token_hex(8)}5x", encoding="utf-8")
    return {"RD_BOOTSTRAP_DUAL_MASTER_APPROVAL": dual, "RD_BOOTSTRAP_MASTER_EMAIL": "master.conc@rd-midiaindoor.localhost",
            "RD_BOOTSTRAP_MASTER_NAME": "Master Concorrente", "RD_BOOTSTRAP_MASTER_PASSWORD_FILE": str(f)}


async def test_ten_concurrent_bootstraps_single_outcome(db, redis, tmp_path):
    envs = [_env(tmp_path, "true" if i % 2 == 0 else "false") for i in range(10)]
    results = await asyncio.gather(*[boot.bootstrap_instance(e) for e in envs])
    assert sum(1 for r in results if r.mode_created) == 1
    assert sum(1 for r in results if r.seat1_status == "criado") == 1
    assert len({(r.mode, r.mode_version) for r in results}) == 1
    row = (await db.execute(text(
        "SELECT (SELECT count(*) FROM instance_approval_mode_history), (SELECT count(*) FROM admin_master_seat_events), "
        "(SELECT count(*) FROM users WHERE admin_master_seat = 1), (SELECT count(*) FROM users)"))).one()
    assert tuple(row) == (1, 1, 1, 1)
    from app.modules.security.approval_mode import verify_integrity

    assert (await verify_integrity(db)).ok
