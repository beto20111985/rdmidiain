"""Kill → lift com o gerador REAL do W-INV (PostgreSQL real): cada passo gera versão nova do
manifesto, o criativo entra/sai de ``revoked`` e ``screen.sync`` é publicado após o commit.

Regressão (pós-janela, achado no E2E do W-PWEB): com ``autoflush=False`` o ``lift_kill`` não
fazia flush e a regeneração na MESMA transação ainda via o kill ativo → nenhuma versão nova.

Determinismo (a suíte completa roda com a máquina carregada):
- sem 2FA no caminho (perfil próprio com as permissões necessárias, sem ``mfa_required``) —
  nenhuma dependência da janela de 30 s do TOTP;
- o horizonte do manifesto é por DIA LOCAL do estabelecimento: o fuso é escolhido de modo que a
  meia-noite local esteja a ≥ 1 h de agora (virar o dia no meio do teste geraria versão extra);
- assinatura ISOLADA no canal da tela deste teste (``events:screen:<id>``), confirmada pelo Redis
  antes da ação, e espera POR CONDIÇÃO com prazo folgado (sem sleep fixo).
"""
from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core import events
from app.models.manifests import ManifestVersion
from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
from tests.factories import login_as, make_establishment, make_org, make_user
from tests.player_support import make_screen, pair_via_panel

EVENT_TIMEOUT_S = 30.0
CANDIDATE_TZS = ("America/Sao_Paulo", "Europe/Lisbon", "Asia/Tokyo")  # deslocamentos distintos (≥ 3 h)


def _tz_far_from_midnight() -> str:
    now = datetime.now(timezone.utc)
    for tz in CANDIDATE_TZS:
        local = now.astimezone(ZoneInfo(tz))
        minutes = local.hour * 60 + local.minute
        if 60 <= minutes <= 22 * 60 + 59:  # ≥ 1 h depois e ≥ 1 h antes da meia-noite local
            return tz
    raise AssertionError("nenhum fuso candidato longe da meia-noite (impossível com os deslocamentos usados)")


async def _operator(db):
    """Usuário com perfil próprio (GLOBAL) sem exigência de 2FA."""
    role = Role(id=uuid.uuid4(), slug=f"tkill_{secrets.token_hex(4)}", name="Teste kill", is_system=False)
    db.add(role)
    await db.flush()
    for code in ("screens.view", "screens.pair", "campaigns.kill"):
        p = (await db.execute(select(Permission).where(Permission.code == code))).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=p.id))
    u = await make_user(db)
    db.add(RoleAssignment(id=uuid.uuid4(), user_id=u.id, role_id=role.id, scope_type="GLOBAL"))
    await db.commit()
    return u


async def _latest(db, screen_id) -> int:
    row = (await db.execute(select(func.max(ManifestVersion.version)).where(ManifestVersion.screen_id == screen_id)
                            .execution_options(populate_existing=True))).scalar_one()
    return int(row or 0)


async def _wait_sync(ps, screen_id, version: int, seen: set) -> list[int]:
    """Coleta ``screen.sync`` da tela até chegar ``version`` (ou estourar o prazo)."""
    got: list[int] = []
    loop = asyncio.get_running_loop()
    end = loop.time() + EVENT_TIMEOUT_S
    while version not in got and loop.time() < end:
        m = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if not m:
            continue
        env = json.loads(m["data"])
        if env.get("type") != "screen.sync" or env.get("data", {}).get("screen_id") != str(screen_id):
            continue
        if env["id"] in seen:
            continue
        seen.add(env["id"])
        got.append(env["data"]["version"])
    return got


async def test_kill_then_lift_generates_new_versions_and_publishes_sync(client, db, redis):
    org = await make_org(db)
    est = await make_establishment(db, org, tz=_tz_far_from_midnight())
    screen = await make_screen(db, est)
    op = await _operator(db)
    s = await login_as(client, op)
    dev = await pair_via_panel(client, s.headers, screen.id)

    r = await client.get("/api/v1/player/manifest", headers=dev.headers)
    assert r.status_code == 200, r.text
    v0 = r.json()["manifest"]["version"]
    assert await _latest(db, screen.id) == v0

    ps = redis.pubsub()
    await ps.subscribe(events.channel_screen(screen.id))
    loop = asyncio.get_running_loop()
    end = loop.time() + EVENT_TIMEOUT_S
    confirmed = False
    while not confirmed and loop.time() < end:  # confirmação do SUBSCRIBE pelo servidor antes de agir
        m = await ps.get_message(ignore_subscribe_messages=False, timeout=1.0)
        confirmed = bool(m and m.get("type") == "subscribe")
    assert confirmed, "assinatura Redis não confirmada"
    cid = str(uuid.uuid4())
    seen: set = set()
    try:
        r = await client.post("/api/v1/playback/kills", json={"creative_id": cid, "screen_ids": [str(screen.id)],
                                                               "reason": "ordem judicial"}, headers=s.headers)
        assert r.status_code == 201, r.text
        kill_id = r.json()["items"][0]["id"]
        v1 = await _latest(db, screen.id)
        assert v1 == v0 + 1
        assert await _wait_sync(ps, screen.id, v1, seen) == [v1]
        m1 = (await client.get("/api/v1/player/manifest", headers=dev.headers)).json()["manifest"]
        assert m1["version"] == v1 and cid in m1["revoked"]

        r = await client.post(f"/api/v1/playback/kills/{kill_id}/lift", json={"reason": "decisão revertida"},
                              headers=s.headers)
        assert r.status_code == 200, r.text
        assert r.json()["lifted_at"] is not None
        v2 = await _latest(db, screen.id)
        assert v2 == v1 + 1, "lift não gerou versão nova do manifesto"
        assert await _wait_sync(ps, screen.id, v2, seen) == [v2]
        r = await client.get("/api/v1/player/manifest", headers={**dev.headers, "If-None-Match": f'"{v1}"'})
        assert r.status_code == 200
        m2 = r.json()["manifest"]
        assert m2["version"] == v2 and cid not in m2["revoked"]
    finally:
        await ps.unsubscribe()
        await ps.aclose()
