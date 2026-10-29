"""W-INV — programação SEGURA da tela para o ESTABELECIMENTO (FE-OPS):
``GET /api/v1/scheduler/screens/{id}/programming`` — entradas só ``{code, kind}``."""
from __future__ import annotations

import uuid
from datetime import timedelta

from app.core.authz import AuthContext
from app.modules.inventory import content as C
from app.modules.inventory import service as inv
from tests.factories import login_as, make_user
from tests.inv_support import (
    db_today,
    link,
    make_live,
    mk_campaign,
    mk_creative,
    mk_establishment,
    mk_org,
    mk_screen,
    pattern,
)

SYS = AuthContext.system("system")


async def test_owner_sees_programming_without_advertiser_data(client, db):
    owner = await mk_org(db, "Mercado do Dono")
    est = await mk_establishment(db, owner)
    scr = await mk_screen(db, est)
    adv = await mk_org(db, "Anunciante Secreto LTDA")
    ext = await mk_creative(db, adv)
    camp = await mk_campaign(db, adv, name="Campanha Confidencial")
    await link(db, camp, ext)
    today = await db_today(db)
    pat = pattern(today + timedelta(days=1), today + timedelta(days=1), freq_value=2,
                  time_bands=[[8 * 60, 20 * 60]], min_interval_seconds=600)
    h = await inv.hold(db, SYS, camp.id, [scr.id], [pat], str(uuid.uuid4()))
    await db.commit()
    await inv.convert_hold(db, h.id)
    await db.commit()
    await make_live(db, camp, h.id, [scr.id], [ext.id], pat)
    piece = await mk_creative(db, owner, kind="INTERNAL", duration=60)
    await C.add_internal(db, SYS, establishment_id=est.id, creative_id=piece.id)
    await db.commit()

    uo = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", owner.id))
    s = await login_as(client, uo)
    url = f"/api/v1/scheduler/screens/{scr.id}/programming?hours=48"
    r = await client.get(url, headers=s.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    entries = [e for b in body["blocks"] for e in b["entries"]]
    assert {"code": ext.code, "kind": "EXTERNAL"} in entries
    assert all(set(e) == {"code", "kind"} for e in entries), "entradas só com code e kind"
    assert body["internal_pattern"][0][0] == {"code": piece.code, "name": "Peça", "duration_ms": 60000}
    # NENHUM dado do anunciante/campanha/criativo (ids, nome, organização)
    for secret in (str(camp.id), str(adv.id), str(ext.id), str(ext.media_file_id), "Anunciante Secreto",
                   "Campanha Confidencial", "campaign_id", "advertiser_id", "creative_id", "price"):
        assert secret not in r.text, secret

    # escopo ESTABLISHMENT do dono também vê
    ue = await make_user(db, role="establishment_owner", scope=("ESTABLISHMENT", est.id))
    se = await login_as(client, ue)
    assert (await client.get(url, headers=se.headers)).status_code == 200

    # dono de OUTRA organização → 404 (não vaza existência)
    other = await mk_org(db, "Outro Mercado")
    ux = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", other.id))
    sx = await login_as(client, ux)
    r = await client.get(url, headers=sx.headers)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"

    # anunciante (sem screens.view/playlists.view) → 403; sem credencial → 401
    ua = await make_user(db, role="advertiser", scope=("ORGANIZATION", adv.id))
    sa = await login_as(client, ua)
    assert (await client.get(url, headers=sa.headers)).status_code == 403
    assert (await client.get(url)).status_code == 401
    # tela inexistente → 404
    assert (await client.get(f"/api/v1/scheduler/screens/{uuid.uuid4()}/programming",
                             headers=s.headers)).status_code == 404
