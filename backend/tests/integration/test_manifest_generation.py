"""W-INV — gerador ÚNICO de manifesto RDPP/1 no PostgreSQL real: só APROVADO + mídia READY,
validade 23:59:59.999 local por fuso, versão monotônica sob lock, assinatura ES256 verificável,
sha256 por mídia, conteúdo interno, emergência, revogação e ``screen.sync`` após o commit."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core import events
from app.core.authz import AuthContext
from app.core.database import session_factory
from app.models.campaigns import Campaign
from app.models.manifests import ManifestVersion
from app.modules.inventory import content as C
from app.modules.inventory import service as inv
from app.modules.playback import protocol as P
from tests.inv_support import (
    db_today,
    link,
    make_live,
    mk_campaign,
    mk_creative,
    mk_establishment,
    mk_media,
    mk_org,
    mk_screen,
    pattern,
)

SYS = AuthContext.system("system")


def local_ms(d, tz: str, t: time) -> int:
    dt = datetime.combine(d, t).replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


async def _setup(db, tz="America/Sao_Paulo", state="GO", creative_status="APROVADO", ready=True, days=3,
                 live_status="APROVADA"):
    owner = await mk_org(db, "Dono")
    est = await mk_establishment(db, owner, tz=tz, state=state)
    scr = await mk_screen(db, est, state=state)
    adv = await mk_org(db, "Anunciante")
    cr = await mk_creative(db, adv, status=creative_status, ready=ready)
    camp = await mk_campaign(db, adv)
    await link(db, camp, cr)
    today = await db_today(db, tz)
    pat = pattern(today + timedelta(days=1), today + timedelta(days=days), freq_value=2,
                  time_bands=[[8 * 60, 20 * 60]], min_interval_seconds=600)
    h = await inv.hold(db, SYS, camp.id, [scr.id], [pat], str(uuid.uuid4()))
    await db.commit()
    await inv.convert_hold(db, h.id)
    await db.commit()
    await make_live(db, camp, h.id, [scr.id], [cr.id], pat, status=live_status)
    return SimpleNamespace(owner=owner, est=est, scr=scr, adv=adv, cr=cr, camp=camp, hold_id=h.id, pat=pat,
                           today=today, tz=tz)


def _ext(m):
    return {k: v for k, v in m["creatives"].items() if v["kind"] == "EXTERNAL"}


async def test_manifest_contains_approved_ready_creative_with_local_validity_and_valid_signature(db):
    e = await _setup(db)
    sm = await inv.generate_manifest(db, e.scr.id, reason="test")
    await db.commit()
    m = sm.manifest
    ext = _ext(m)
    assert len(ext) == 1
    c = next(iter(ext.values()))
    assert c["code"] == e.cr.code and c["campaign_id"] == str(e.camp.id) and c["advertiser_id"] == str(e.adv.id)
    d_from = e.today + timedelta(days=1)
    d_to = e.today + timedelta(days=3)
    assert c["valid_from_ms"] == local_ms(d_from, e.tz, time(0, 0))
    assert c["valid_until_ms"] == local_ms(d_to, e.tz, time(23, 59, 59, 999000))
    # 2 inserções/h × 12 h × dias dentro do horizonte
    key = next(iter(ext))
    n = sum(1 for es in m["external_plan"].values() for x in es if x["c"] == key)
    assert n == 2 * 12 * 3
    # mídia: sha256/size da mídia READY
    media = (await db.execute(text("SELECT sha256, size_bytes FROM media_files WHERE id = :i"),
                              {"i": e.cr.media_file_id})).one()
    mm = m["media"][c["media"]]
    assert (mm["sha256"], mm["size"], mm["media_id"]) == (media[0], media[1], str(e.cr.media_file_id))
    # assinatura ES256 sobre RDJ1 verificável com a chave pública publicada
    keys = inv.server_public_keys()
    assert sm.key_id == keys[0]["key_id"]
    assert P.verify_es256(keys[0]["public_key"], m, sm.signature)
    tampered = json.loads(json.dumps(m))
    tampered["revoked"] = ["x"]
    assert not P.verify_es256(keys[0]["public_key"], tampered, sm.signature)
    # bytes gravados = RDJ1 exato; consulta por versão devolve o mesmo objeto
    assert await inv.get_manifest_version(db, e.scr.id, sm.version) == m
    row = await db.get(ManifestVersion, (e.scr.id, sm.version))
    import hashlib

    assert row.content_sha256 == hashlib.sha256(P.canonical_bytes(m)).hexdigest()
    assert str(e.camp.id) in {str(x) for x in row.campaign_ids}


@pytest.mark.parametrize("tz,state", [("America/Manaus", "AM"), ("America/Rio_Branco", "AC")])
async def test_valid_until_local_per_timezone(db, tz, state):
    e = await _setup(db, tz=tz, state=state)
    m = (await inv.generate_manifest(db, e.scr.id)).manifest
    await db.commit()
    c = next(iter(_ext(m).values()))
    assert c["valid_until_ms"] == local_ms(e.today + timedelta(days=3), tz, time(23, 59, 59, 999000))
    assert m["timezone"] == tz


async def test_only_approved_creatives_with_ready_media_and_playable_campaigns(db):
    e = await _setup(db)
    # criativo NÃO aprovado e mídia NÃO pronta na mesma campanha: não entram
    pend = await mk_creative(db, e.adv, status="EM_ANALISE")
    notready = await mk_creative(db, e.adv, ready=False)
    await link(db, e.camp, pend, 1)
    await link(db, e.camp, notready, 2)
    # outra campanha contratada (BOOKED) mas não aprovada (RASCUNHO, sem revisão vigente): não toca
    adv2 = await mk_org(db)
    cr2 = await mk_creative(db, adv2)
    camp2 = await mk_campaign(db, adv2)
    await link(db, camp2, cr2)
    h2 = await inv.hold(db, SYS, camp2.id, [e.scr.id], [e.pat], str(uuid.uuid4()))
    await db.commit()
    await inv.convert_hold(db, h2.id)
    await db.commit()
    m = (await inv.generate_manifest(db, e.scr.id)).manifest
    await db.commit()
    codes = {v["code"] for v in m["creatives"].values()}
    assert e.cr.code in codes
    assert pend.code not in codes and notready.code not in codes and cr2.code not in codes


async def test_version_monotonic_unchanged_content_reuses_version_and_changes_bump(db):
    e = await _setup(db)
    v1 = await inv.generate_manifest(db, e.scr.id)
    await db.commit()
    v1b = await inv.generate_manifest(db, e.scr.id)
    await db.commit()
    assert v1b.version == v1.version and not v1b.created and v1b.plan_sha256 == v1.plan_sha256
    # conteúdo interno novo ⇒ nova versão
    internal = await mk_creative(db, e.owner, kind="INTERNAL", duration=60)
    await C.add_internal(db, SYS, establishment_id=e.est.id, creative_id=internal.id)
    await db.commit()
    v2 = await inv.current_manifest(db, e.scr.id)
    assert v2.version == v1.version + 1
    pat = v2.manifest["internal_pattern"]
    k = [kk for kk, v in v2.manifest["creatives"].items() if v["creative_id"] == str(internal.id)][0]
    assert pat == [[{"c": k, "d": 60000}, {"c": k, "d": 60000}]]  # peça de 1 min repete no bloco de 2 min


async def test_concurrent_generation_gets_distinct_monotonic_versions(db, db_engine):
    e = await _setup(db)
    base = (await inv.generate_manifest(db, e.scr.id)).version
    await db.commit()

    async def gen():
        async with session_factory(db_engine)() as s:
            sm = await inv.generate_manifest(s, e.scr.id, reason="race", force=True)
            await s.commit()
            return sm.version

    vs = await asyncio.gather(gen(), gen(), gen())
    assert sorted(vs) == [base + 1, base + 2, base + 3]


async def test_ten_minutes_of_internal_pieces_rotate(db):
    e = await _setup(db)
    ids = []
    for _ in range(10):
        cr = await mk_creative(db, e.owner, kind="INTERNAL", duration=60)
        await C.add_internal(db, SYS, establishment_id=e.est.id, creative_id=cr.id)
        ids.append(str(cr.id))
    await db.commit()
    m = (await inv.current_manifest(db, e.scr.id)).manifest
    await db.commit()
    rev = {k: v["creative_id"] for k, v in m["creatives"].items()}
    pat = [[rev[x["c"]] for x in blk] for blk in m["internal_pattern"]]
    assert pat == [ids[0:2], ids[2:4], ids[4:6], ids[6:8], ids[8:10]]
    # unidades externas não vendidas vêm preenchidas com INTERNO
    kinds = [m["creatives"][x["c"]]["kind"] for es in m["external_plan"].values() for x in es]
    assert "INTERNAL" in kinds


async def test_emergency_fallback_and_revoked_in_manifest(db):
    e = await _setup(db)
    admin_id = (await db.execute(text("SELECT gen_random_uuid()"))).scalar_one()
    from tests.factories import make_user

    admin_id = (await make_user(db, role="super_admin", mfa=True)).id
    ctx = AuthContext(kind="user", user=SimpleNamespace(id=admin_id))
    inst = await mk_creative(db, e.owner, kind="INSTITUTIONAL")
    fb = await mk_creative(db, e.owner, kind="FALLBACK")
    await C.add_fallback(db, ctx, scope="GLOBAL", creative_id=fb.id, purpose="RD_DEFAULT")
    now = (await db.execute(text("SELECT now()"))).scalar_one()
    em, n = await C.create_emergency(db, ctx, title="Evacuação", creative_id=inst.id, starts_at=now,
                                     ends_at=now + timedelta(hours=1), target_kind="STATES", target_ids=["GO"],
                                     reason="Simulado de evacuação do prédio")
    await db.commit()
    assert n == 1
    from app.models.devices import CreativeKill

    db.add(CreativeKill(id=uuid.uuid4(), creative_id=e.cr.id, screen_id=None, reason="kill de teste"))
    await db.commit()
    m = (await inv.current_manifest(db, e.scr.id)).manifest
    await db.commit()
    m = (await inv.generate_manifest(db, e.scr.id, reason="kill")).manifest
    await db.commit()
    assert m["emergency"][0]["id"] == str(em.id)
    assert m["creatives"][m["emergency"][0]["c"]]["creative_id"] == str(inst.id)
    assert [m["creatives"][f["c"]]["creative_id"] for f in m["fallback"]] == [str(fb.id)]
    assert m["revoked"] == [str(e.cr.id)]
    # cancelar a emergência gera nova versão sem ela
    await C.cancel_emergency(db, ctx, em.id, "Simulado encerrado pela brigada")
    await db.commit()
    m2 = (await inv.current_manifest(db, e.scr.id)).manifest
    assert m2["emergency"] == [] and m2["version"] > m["version"]


async def test_invalidate_publishes_screen_sync_only_after_commit(db, redis):
    e = await _setup(db)
    ps = redis.pubsub()
    await ps.subscribe(events.channel_screen(e.scr.id))
    await ps.get_message(timeout=1)  # confirmação de subscribe
    out = await inv.invalidate_screens(db, [e.scr.id], "test")
    assert await ps.get_message(timeout=0.3) is None, "nada antes do commit"
    await db.commit()
    msg = None
    for _ in range(20):
        msg = await ps.get_message(timeout=0.5)
        if msg and msg.get("type") == "message":
            break
    assert msg and msg["type"] == "message"
    env = json.loads(msg["data"])
    assert env["type"] == "screen.sync" and env["data"]["version"] == out[e.scr.id]
    assert env["scope"]["screen_id"] == str(e.scr.id)
    # rollback não publica
    await inv.generate_manifest(db, e.scr.id, force=True)
    from app.modules.inventory.manifests import _defer_sync

    _defer_sync(db, info=None, sm=SimpleNamespace(screen_id=e.scr.id, version=999), reason="x")
    await db.rollback()
    got = await ps.get_message(timeout=0.5)
    assert got is None
    await ps.aclose()


async def test_campaign_goes_syncing_and_ack_is_recorded(db):
    e = await _setup(db)
    sm = await inv.generate_manifest(db, e.scr.id)
    await db.commit()
    st = (await db.execute(select(Campaign.status).where(Campaign.id == e.camp.id))).scalar_one()
    assert st == "SINCRONIZANDO"  # transição pedida ao W-CAMP (APROVADA → SINCRONIZANDO)
    ok = await inv.mark_manifest_applied(db, e.scr.id, sm.version, None)
    await db.commit()
    row = await db.get(ManifestVersion, (e.scr.id, sm.version))
    assert ok and row.applied_at is not None


async def test_paused_campaign_keeps_inventory_but_plays_internal(db):
    e = await _setup(db, live_status="PAUSADA")
    m = (await inv.generate_manifest(db, e.scr.id)).manifest
    await db.commit()
    assert _ext(m) == {}
    # capacidade continua reservada (nenhum terceiro ocupa as mesmas posições além do livre)
    q = await inv.quote_capacity(db, SYS, [e.scr.id], [pattern(e.today + timedelta(days=1),
                                                              e.today + timedelta(days=1), duration_units=4,
                                                              freq_value=15)])
    assert not q.feasible


async def test_preview_grid_and_screen_state(db):
    e = await _setup(db)
    g = await inv.preview_grid(db, e.scr.id, hours=48)
    codes = {x["code"] for b in g["blocks"] for x in b["entries"]}
    assert e.cr.code in codes
    st = await inv.screen_inventory_view(db, e.scr.id)
    assert st["policy"]["units_per_hour"] == 60 and st["policy"]["scope"] == "DEFAULT"


async def test_renewed_campaign_plays_original_and_extension_without_gap(db):
    """Renovação (W-CAMP, revisão RENEWAL): hold novo só para o período adicional; a revisão
    vigente lista os dois holds em vigor. O manifesto toca os dois períodos sem interrupção e a
    contagem contratada não duplica."""
    owner = await mk_org(db, "Dono")
    est = await mk_establishment(db, owner)
    scr = await mk_screen(db, est)
    adv = await mk_org(db, "Anunciante")
    cr = await mk_creative(db, adv)
    camp = await mk_campaign(db, adv)
    await link(db, camp, cr)
    today = await db_today(db)
    d1, d2, d3, d4 = (today + timedelta(days=i) for i in (1, 2, 3, 4))
    base = dict(freq_value=2, time_bands=[[8 * 60, 20 * 60]], min_interval_seconds=600)
    p_orig, p_ext = pattern(d1, d2, **base), pattern(d3, d4, **base)
    h1 = await inv.hold(db, SYS, camp.id, [scr.id], [p_orig], "orig")
    h1_id = h1.id
    await db.commit()
    await inv.convert_hold(db, h1_id)
    await db.commit()
    h2 = await inv.hold(db, SYS, camp.id, [scr.id], [p_ext], "renewal")  # sem replaces: período novo
    h2_id = h2.id
    await db.commit()
    await inv.convert_hold(db, h2_id)
    await db.commit()
    await make_live(db, camp, h2_id, [scr.id], [cr.id], pattern(d1, d4, **base), inherited_hold_ids=[h1_id],
                    start_date=d1)
    m = (await inv.generate_manifest(db, scr.id, reason="renewal")).manifest
    await db.commit()
    tz = "America/Sao_Paulo"
    ext = {k: v for k, v in m["creatives"].items() if v["kind"] == "EXTERNAL"}
    windows = sorted((v["valid_from_ms"], v["valid_until_ms"]) for v in ext.values())
    assert windows == [(local_ms(d1, tz, time(0, 0)), local_ms(d2, tz, time(23, 59, 59, 999000))),
                       (local_ms(d3, tz, time(0, 0)), local_ms(d4, tz, time(23, 59, 59, 999000)))]
    assert windows[0][1] + 1 == windows[1][0], "sem interrupção entre o período original e a extensão"
    per_day: dict = {}
    for b, es in m["external_plan"].items():
        for e in es:
            if e["c"] in ext:
                day = datetime.fromtimestamp(int(b) / 1000, tz=ZoneInfo(tz)).date()
                per_day[day] = per_day.get(day, 0) + 1
    assert per_day == {d1: 24, d2: 24, d3: 24, d4: 24}
    got = await inv.contracted_exhibitions(db, camp.id)
    assert got == {scr.id: {d1: 24, d2: 24, d3: 24, d4: 24}}  # sem dupla contagem
    # ocupação: as duas contratações contam (24 un./dia cada período), nada em dobro
    av = await inv.availability(db, screen_ids=[scr.id], date_from=d1, date_to=d4)
    assert [d["used_units"] for d in av["days"]] == [24, 24, 24, 24]


async def test_manifest_content_retained_at_least_35_days_for_late_offline_pop(db):
    """PoP offline pode chegar com 28+ dias de atraso: conteúdo e refs de versões de até 35 dias
    continuam disponíveis; a poda só remove BYTES de versões mais antigas (refs ficam para sempre)."""
    from app.core.errors import DomainError
    from app.core.settings_service import set_setting
    from app.modules.inventory.manifests import content_retention_days, prune_manifest_content

    e = await _setup(db)
    versions = []
    for _ in range(8):
        versions.append((await inv.generate_manifest(db, e.scr.id, reason="ret", force=True)).version)
        await db.commit()
    v_old, v_30, v_34 = versions[0], versions[1], versions[2]  # fora das 5 últimas
    for v, days in ((v_old, 40), (v_30, 30), (v_34, 34)):
        await db.execute(text("UPDATE manifest_versions SET generated_at = now() - make_interval(days => :d) "
                              "WHERE screen_id = :s AND version = :v"), {"d": days, "s": e.scr.id, "v": v})
    await db.commit()
    assert await content_retention_days(db) == 35
    assert await prune_manifest_content(db) == 1  # só a de 40 dias
    await db.commit()
    db.expire_all()
    assert await inv.get_manifest_version(db, e.scr.id, v_30) is not None  # 30 dias: conteúdo íntegro
    assert await inv.get_manifest_version(db, e.scr.id, v_34) is not None
    assert await inv.get_manifest_version(db, e.scr.id, v_old) is None  # bytes podados…
    for v in (v_old, v_30, v_34):  # …mas as referências respondem (validação de PoP)
        refs = await inv.get_manifest_refs(db, e.scr.id, v)
        assert refs is not None and str(e.cr.id) in refs["creative_ids"] and str(e.camp.id) in refs["campaign_ids"]
    # retenção configurável, nunca abaixo de 35 dias
    with pytest.raises(DomainError) as ei:
        await set_setting(db, SYS, "manifest.content_retention_days", 30, reason="tentativa abaixo do mínimo")
    assert ei.value.code == "SETTING_INVALID"
    await db.rollback()
    await set_setting(db, SYS, "manifest.content_retention_days", 45, reason="PoP offline com mais atraso")
    await db.commit()
    await db.execute(text("UPDATE manifest_versions SET generated_at = now() - interval '44 days' "
                          "WHERE screen_id = :s AND version = :v"), {"s": e.scr.id, "v": v_30})
    await db.commit()
    assert await prune_manifest_content(db) == 0  # 44 dias < 45 configurados
    await db.commit()
    db.expire_all()
    assert await inv.get_manifest_version(db, e.scr.id, v_30) is not None
