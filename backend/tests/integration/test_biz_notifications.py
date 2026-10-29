"""W-BIZ — Notificações: dedupe, cooldown, preferências, e-mail real (caixa capturada),
push indisponível registrado, unread-count, templates, escopo de destinatários, evento
tempo real e rollback. PostgreSQL + Redis reais."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update

from app.core.database import session_factory
from app.core.redis import rkey
from app.models.notifications import Notification, NotificationCooldown, NotificationDelivery
from app.modules.notifications import service as svc
from app.modules.notifications.push import PUSH_UNAVAILABLE_DETAIL
from tests.factories import login_as, make_org, make_user


@pytest.fixture(autouse=True)
async def _drain_after_test(clean_state):
    """Espera os envios pós-commit do notify antes do teardown (evita transação órfã)."""
    yield
    from app.modules.notifications.service import drain

    await drain()


async def _count(db, model, *conds) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(*conds))).scalar_one()


async def test_dedupe_same_key_creates_once(db, mail):
    u = await make_user(db)
    r1 = await svc.notify(db, [u.id], "campaign.approved", {"campaign_name": "Verão", "campaign_id": "c1"},
                          dedupe_key="campaign.approved:c1")
    await db.commit()
    r2 = await svc.notify(db, [u.id], "campaign.approved", {"campaign_name": "Verão", "campaign_id": "c1"},
                          dedupe_key="campaign.approved:c1")
    await db.commit()
    await svc.drain()
    assert r1.created_count == 1 and r2.created_count == 0 and r2.deduped == 1
    assert await _count(db, Notification, Notification.user_id == u.id) == 1
    # um único e-mail (a segunda chamada não reenvia)
    assert len([m for m in mail.outbox if m.to == u.email]) == 1


async def test_cooldown_suppresses_then_allows_after_window(db, mail):
    u = await make_user(db)
    screen = str(uuid.uuid4())
    data = {"screen_code": "TEL-GO-7Q3M", "screen_name": "Recepção", "offline_since": "10:00", "screen_id": screen}
    a = await svc.notify(db, [u.id], "screen.offline", data, entity=("screen", screen))
    await db.commit()
    b = await svc.notify(db, [u.id], "screen.offline", data, entity=("screen", screen))
    await db.commit()
    assert a.created_count == 1 and b.created_count == 0 and b.cooldown_suppressed == 1
    # outra tela não é afetada pelo cooldown da primeira
    c = await svc.notify(db, [u.id], "screen.offline", {**data, "screen_id": "outra"}, entity=("screen", "outra"))
    await db.commit()
    assert c.created_count == 1
    # janela expirada (6 h padrão) → volta a notificar
    await db.execute(
        update(NotificationCooldown)
        .where(NotificationCooldown.user_id == u.id, NotificationCooldown.entity_key == f"screen:{screen}")
        .values(last_sent_at=func.now() - text("interval '7 hours'"))
    )
    await db.commit()
    d = await svc.notify(db, [u.id], "screen.offline", data, entity=("screen", screen))
    await db.commit()
    assert d.created_count == 1
    await svc.drain()


async def test_cooldown_is_atomic_under_concurrency(db_engine, db, mail):
    u = await make_user(db)
    sf = session_factory(db_engine)

    async def one():
        async with sf() as s:
            r = await svc.notify(s, [u.id], "screen.offline", {"screen_code": "TEL-GO-AAAA", "screen_id": "x"},
                                 entity=("screen", "x"), cooldown_s=3600)
            await s.commit()
            return r.created_count

    results = await asyncio.gather(*[one() for _ in range(6)])
    await svc.drain()
    assert sum(results) == 1
    assert await _count(db, Notification, Notification.user_id == u.id) == 1


async def test_email_sent_after_commit_and_captured(db, mail):
    u = await make_user(db, name="Ana")
    res = await svc.notify(db, [u.id], "payment.approved", {"reference": "PED-123", "amount_cents": 123456})
    # antes do commit nada sai
    await asyncio.sleep(0)
    assert mail.last_to(u.email) is None
    await db.commit()
    await svc.drain()
    msg = mail.last_to(u.email)
    assert msg is not None
    assert msg.subject == "Pagamento aprovado"
    assert "PED-123" in msg.text and "R$ 1.234,56" in msg.text
    d = (await db.execute(select(NotificationDelivery).where(NotificationDelivery.notification_id == res.created[0],
                                                              NotificationDelivery.channel == "EMAIL"))).scalar_one()
    await db.refresh(d)
    assert d.status == "SENT" and d.sent_at is not None


async def test_rollback_creates_nothing_and_sends_nothing(db_engine, db, mail):
    u = await make_user(db)
    async with session_factory(db_engine)() as s:
        await svc.notify(s, [u.id], "payment.approved", {"reference": "X", "amount_cents": 100})
        await s.rollback()
    await svc.drain()
    assert await _count(db, Notification, Notification.user_id == u.id) == 0
    assert mail.last_to(u.email) is None


async def test_push_registered_unavailable_never_sent(db, mail):
    u = await make_user(db)
    res = await svc.notify(db, [u.id], "screen.offline", {"screen_code": "TEL-SP-2345", "screen_id": "p"},
                           entity=("screen", "p"))
    await db.commit()
    await svc.drain()
    assert res.push_unavailable == 1
    rows = {d.channel: d for d in (await db.execute(
        select(NotificationDelivery).where(NotificationDelivery.notification_id == res.created[0]))).scalars()}
    assert rows["PUSH"].status == "UNAVAILABLE"
    assert rows["PUSH"].detail == PUSH_UNAVAILABLE_DETAIL and "push: indisponível" in rows["PUSH"].detail
    assert rows["PUSH"].sent_at is None
    assert rows["IN_APP"].status == "SENT"


async def test_smtp_failure_goes_to_retry_with_backoff(db, mail):
    class Boom:
        async def send(self, message):
            raise ConnectionError("smtp down")

    from app.core.email import set_email_sender

    set_email_sender(Boom())
    u = await make_user(db)
    res = await svc.notify(db, [u.id], "campaign.started", {"campaign_name": "C", "campaign_id": "1"})
    await db.commit()
    await svc.drain()
    d = (await db.execute(select(NotificationDelivery).where(NotificationDelivery.notification_id == res.created[0],
                                                              NotificationDelivery.channel == "EMAIL"))).scalar_one()
    await db.refresh(d)
    assert d.status == "PENDING" and d.attempts == 1 and d.next_attempt_at is not None
    assert "ConnectionError" in (d.detail or "")
    # SMTP volta: o job de retry envia quando a janela de backoff vence
    set_email_sender(mail)
    await db.execute(update(NotificationDelivery).where(NotificationDelivery.id == d.id)
                     .values(next_attempt_at=func.now() - text("interval '1 second'")))
    await db.commit()
    stats = await svc.dispatch_deliveries(None)
    assert stats["sent"] == 1
    await db.refresh(d)
    assert d.status == "SENT" and d.attempts == 2
    assert mail.last_to(u.email) is not None


async def test_recipients_by_org_and_permission_respect_scope(db, mail):
    org_a, org_b = await make_org(db), await make_org(db)
    a1 = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_a.id))
    a2 = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_a.id))
    b1 = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_b.id))
    staff = await make_user(db, role="employee")  # campaigns.view GLOBAL — não entra em OrgRecipients
    inactive = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_a.id), is_active=False)
    res = await svc.notify(db, [(org_a.id, "campaigns.view")], "campaign.approved",
                           {"campaign_name": "A", "campaign_id": "x"}, org_id=org_a.id)
    await db.commit()
    await svc.drain()
    got = set((await db.execute(select(Notification.user_id).where(Notification.event_type == "campaign.approved"))).scalars())
    assert got == {a1.id, a2.id}
    assert b1.id not in got and staff.id not in got and inactive.id not in got
    assert res.recipients == 2
    staff_res = await svc.notify(db, [("GLOBAL", "campaigns.approve")], "campaign.awaiting_approval",
                                 {"campaign_name": "A", "campaign_id": "x"})
    await db.commit()
    assert staff_res.recipients == 0  # employee não tem campaigns.approve


async def test_preferences_api_and_effect(client, db, mail):
    org = await make_org(db)
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, u)
    r = await client.put("/api/v1/notifications/preferences", headers=s.headers,
                         json={"items": [{"event_type": "campaign.approved", "channel": "EMAIL", "enabled": False}]})
    assert r.status_code == 200, r.text
    ca = next(i for i in r.json()["items"] if i["event_type"] == "campaign.approved")
    assert {c["channel"]: c["enabled"] for c in ca["channels"]}["EMAIL"] is False
    push = next(c for c in ca["channels"] if c["channel"] == "PUSH")
    assert push["available"] is False and "indisponível" in push["unavailable_reason"]

    res = await svc.notify(db, [u.id], "campaign.approved", {"campaign_name": "P", "campaign_id": "1"})
    await db.commit()
    await svc.drain()
    assert res.created_count == 1 and res.email_queued == 0
    chans = set((await db.execute(select(NotificationDelivery.channel).where(
        NotificationDelivery.notification_id == res.created[0]))).scalars())
    assert chans == {"IN_APP"}
    assert mail.last_to(u.email) is None

    # desliga IN_APP também → nenhum canal → nada criado
    r = await client.put("/api/v1/notifications/preferences", headers=s.headers,
                         json={"items": [{"event_type": "campaign.approved", "channel": "IN_APP", "enabled": False}]})
    assert r.status_code == 200
    res2 = await svc.notify(db, [u.id], "campaign.approved", {"campaign_name": "P2", "campaign_id": "2"})
    await db.commit()
    assert res2.created_count == 0 and res2.skipped_by_preference == 1

    # crítico: IN_APP não pode ser desligado
    r = await client.put("/api/v1/notifications/preferences", headers=s.headers,
                         json={"items": [{"event_type": "pix_key.changed", "channel": "IN_APP", "enabled": False}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "NOTIFICATION_PREFERENCE_LOCKED"


async def test_unread_count_list_and_mark_read(client, db, mail):
    u = await make_user(db, role="advertiser", scope=("ORGANIZATION", (await make_org(db)).id))
    other = await make_user(db)
    for i in range(3):
        await svc.notify(db, [u.id], "campaign.started", {"campaign_name": f"C{i}", "campaign_id": str(i)},
                         channels=["IN_APP"])
    foreign = await svc.notify(db, [other.id], "campaign.started", {"campaign_name": "X", "campaign_id": "x"},
                               channels=["IN_APP"])
    await db.commit()
    s = await login_as(client, u)
    r = await client.get("/api/v1/notifications/unread-count", headers=s.headers)
    assert r.status_code == 200 and r.json() == {"count": 3}
    r = await client.get("/api/v1/notifications?page_size=2", headers=s.headers)
    body = r.json()
    assert body["total"] == 3 and len(body["items"]) == 2 and body["page"] == 1 and body["page_size"] == 2
    first = body["items"][0]["id"]
    assert (await client.post(f"/api/v1/notifications/{first}/read", headers=s.headers)).status_code == 204
    assert (await client.get("/api/v1/notifications/unread-count", headers=s.headers)).json() == {"count": 2}
    # notificação de outro usuário → 404 (não vaza existência)
    r = await client.post(f"/api/v1/notifications/{foreign.created[0]}/read", headers=s.headers)
    assert r.status_code == 404
    r = await client.post("/api/v1/notifications/read-all", headers=s.headers)
    assert r.json() == {"updated": 2}
    assert (await client.get("/api/v1/notifications/unread-count", headers=s.headers)).json() == {"count": 0}
    assert (await client.get("/api/v1/notifications/unread-count")).status_code == 401


async def test_templates_admin_validation_and_use(client, db, mail):
    admin = await make_user(db, role="super_admin", mfa=True)
    s = await login_as(client, admin)
    bad = await client.put("/api/v1/notifications/templates/campaign.approved/EMAIL", headers=s.headers,
                           json={"title_template": "Oi {{senha_do_cliente}}", "body_template": "x"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "TEMPLATE_UNKNOWN_VARIABLE"
    bad2 = await client.put("/api/v1/notifications/templates/campaign.approved/EMAIL", headers=s.headers,
                            json={"title_template": "{{campaign_name.__class__}}", "body_template": "x"})
    assert bad2.status_code == 400
    ok = await client.put("/api/v1/notifications/templates/campaign.approved/EMAIL", headers=s.headers,
                          json={"title_template": "Aprovada: {{campaign_name}}",
                                "body_template": "Olá! A campanha {{campaign_name}} está aprovada."})
    assert ok.status_code == 200 and ok.json()["version"] == 1
    u = await make_user(db)
    await svc.notify(db, [u.id], "campaign.approved", {"campaign_name": "Inverno", "campaign_id": "9"})
    await db.commit()
    await svc.drain()
    m = mail.last_to(u.email)
    assert m.subject == "Aprovada: Inverno" and "A campanha Inverno está aprovada." in m.text
    # anunciante não administra templates
    org = await make_org(db)
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    sa = await login_as(client, adv)
    r = await client.get("/api/v1/notifications/templates", headers=sa.headers)
    assert r.status_code == 403
    # reset volta ao padrão
    assert (await client.delete("/api/v1/notifications/templates/campaign.approved/EMAIL", headers=s.headers)).status_code == 204


async def test_realtime_event_published_after_commit(db, redis, mail):
    u = await make_user(db)
    pubsub = redis.pubsub()
    await pubsub.subscribe(rkey("events", "user", u.id))
    await pubsub.get_message(timeout=1)  # confirmação de subscribe
    await svc.notify(db, [u.id], "campaign.ended", {"campaign_name": "Fim", "campaign_id": "e"}, channels=["IN_APP"])
    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.3)
    assert msg is None  # nada antes do commit
    await db.commit()
    await svc.drain()
    msg = None
    for _ in range(20):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg:
            break
    await pubsub.unsubscribe()
    await pubsub.aclose()
    assert msg is not None
    env = json.loads(msg["data"])
    assert env["type"] == "notification.created" and env["scope"]["user_id"] == str(u.id)
    assert env["data"]["unread_count"] == 1 and env["data"]["title"] == "Campanha encerrada"


async def test_unknown_event_type_rejected(db):
    u = await make_user(db)
    with pytest.raises(ValueError):
        await svc.notify(db, [u.id], "evento.inexistente", {})


# ─── Pós-janela 2: formatação de valores na renderização (fuso do estabelecimento) ─────────

def test_format_helper_timezones_and_day_rollover():
    from datetime import datetime, timezone

    from app.modules.notifications.templating import format_datetime, format_value, screen_label

    t = datetime(2026, 10, 8, 0, 16, 15, 394000, tzinfo=timezone.utc)
    assert format_datetime(t, "America/Sao_Paulo") == "07/10/2026 21:16"
    assert format_datetime(t, "America/Manaus") == "07/10/2026 20:16"
    assert format_datetime(t, "America/Rio_Branco") == "07/10/2026 19:16"
    assert format_datetime("2026-10-08T00:16:15.394000Z", "America/Rio_Branco") == "07/10/2026 19:16"
    # virada de dia: 03:30Z é 08/10 em São Paulo e ainda 07/10 em Manaus/Rio Branco
    assert format_datetime("2026-10-08T03:30:00+00:00", "America/Sao_Paulo") == "08/10/2026 00:30"
    assert format_datetime("2026-10-08T03:30:00Z", "America/Manaus") == "07/10/2026 23:30"
    assert format_datetime("2026-10-08T04:59:00Z", "America/Rio_Branco") == "07/10/2026 23:59"
    assert format_datetime("2026-10-08T05:00:00Z", "America/Rio_Branco") == "08/10/2026 00:00"
    assert format_datetime(datetime(2026, 10, 8, 2, 0), "America/Sao_Paulo") == "07/10/2026 23:00"  # ingênuo = UTC
    assert format_datetime("2026-10-07", "America/Manaus") == "07/10/2026"
    # códigos públicos e textos livres não são tocados; enums conhecidos viram pt-BR
    assert format_value("screen_code", "TEL-AM-AB12", "America/Manaus") == "TEL-AM-AB12"
    assert format_value("reason", "CHECKSUM", "America/Manaus") == "CHECKSUM"
    assert format_value("priority", "HIGH", "x") == "alta" and format_value("component", "TVBOX", "x") == "TV Box"
    assert format_value("status", "BLOQUEADO_DEPENDENCIA", "x") == "bloqueado por dependência externa"
    assert format_value("status", "AGUARDANDO_CLIENTE", "x") == "aguardando cliente"
    assert screen_label("Recepção", "TEL-AM-AB12") == "Recepção (TEL-AM-AB12)"
    assert screen_label("TEL-AM-AB12", "TEL-AM-AB12") == "TEL-AM-AB12" and screen_label(None, "TEL-AM-AB12") == "TEL-AM-AB12"


async def test_offline_alert_from_player_uses_screen_name_and_establishment_timezone(db, mail):
    """Produtor REAL (W-PLAY ``offline_alerts``): nome cadastrado + hora local do estabelecimento."""
    import secrets as _secrets
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update as _update

    from app.models.devices import Device
    from app.models.screens import Screen
    from app.modules.playback.maintenance import offline_alerts
    from tests.factories import make_establishment

    since = datetime(2026, 10, 8, 0, 16, 15, 394000, tzinfo=timezone.utc)
    bodies = {}
    for tz, uf, city, name in (("America/Manaus", "AM", "Manaus", "Recepção"),
                               ("America/Rio_Branco", "AC", "Rio Branco", None)):
        org = await make_org(db)
        est = await make_establishment(db, org, city=city, state=uf, tz=tz)
        owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org.id))
        from app.core.ids import new_screen_code

        code = new_screen_code(uf)
        scr = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=code, name=name or code)
        db.add(scr)
        await db.flush()
        dev = Device(id=uuid.uuid4(), screen_id=scr.id, public_key="x", key_alg="ES256",
                     key_fingerprint=_secrets.token_hex(32), platform="web", status="ACTIVE",
                     paired_at=since - timedelta(days=30), pairing_method="PANEL", presence="offline", last_seen_at=since)
        db.add(dev)
        await db.flush()
        await db.execute(_update(Screen).where(Screen.id == scr.id).values(device_id=dev.id))
        await db.commit()
        bodies[tz] = (owner, code)
    sent = await offline_alerts(db, now=since + timedelta(hours=1))
    await db.commit()
    await svc.drain()
    assert sent >= 2
    owner_am, code_am = bodies["America/Manaus"]
    n = (await db.execute(select(Notification).where(Notification.user_id == owner_am.id))).scalar_one()
    assert n.title == f"Tela offline: Recepção ({code_am})"
    assert n.body == f"A tela Recepção ({code_am}) está offline desde 07/10/2026 20:16."
    assert n.data["offline_since"].startswith("2026-10-08T00:16:15")  # dado cru preservado para o frontend
    owner_ac, code_ac = bodies["America/Rio_Branco"]
    n2 = (await db.execute(select(Notification).where(Notification.user_id == owner_ac.id))).scalar_one()
    assert n2.body == f"A tela {code_ac} está offline desde 07/10/2026 19:16."  # nome = código → sem parênteses
    m = mail.last_to(owner_am.email)
    assert m is not None and "07/10/2026 20:16" in m.text and "2026-10-08T" not in m.text and "Z" not in m.subject
    push = (await db.execute(select(NotificationDelivery).where(
        NotificationDelivery.notification_id == n.id, NotificationDelivery.channel == "PUSH"))).scalar_one()
    assert "07/10/2026 20:16" in push.content  # mesmo texto para push (registrado como indisponível)


async def test_raw_values_from_other_producers_are_formatted(db, mail):
    """ISO cru (W-FIN), datas e enums em inglês (W-ORG) e status de control plane viram pt-BR."""
    import app.modules.organizations.notifications  # noqa: F401  (registra maintenance.assigned)
    from tests.factories import make_establishment

    org = await make_org(db)
    est = await make_establishment(db, org, city="Rio Branco", state="AC", tz="America/Rio_Branco")
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org.id))
    tech = await make_user(db, role="technician")
    await svc.notify(db, [svc.EstablishmentRecipients(est.id, "withdrawals.view")], "pix_key.changed",
                     {"blocked_until": "2026-10-08T03:30:00Z"}, channels=["IN_APP"])
    await svc.notify(db, [tech.id], "maintenance.assigned", {"establishment_name": "Loja", "order_id": "o1",
                                                              "component": "TVBOX", "priority": "URGENT"},
                     channels=["IN_APP"])
    await svc.notify(db, [tech.id], "control_plane.rollout_finished", {"version": "2.0.0", "status": "SIMULADO",
                                                                       "rollout_id": "r1"}, channels=["IN_APP"])
    await db.commit()
    pix = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalar_one()
    assert "até 07/10/2026 22:30" in pix.body  # Rio Branco (UTC−5), não "2026-10-08T03:30:00Z"
    bodies = {n.event_type: (n.title, n.body) for n in (await db.execute(
        select(Notification).where(Notification.user_id == tech.id))).scalars()}
    assert bodies["maintenance.assigned"][1] == "Manutenção (TV Box, prioridade urgente) em Loja designada a você."
    assert bodies["control_plane.rollout_finished"][0] == "Rollout 2.0.0: simulado (sem deploy real)"
    assert "SIMULADO" not in str(bodies) and "TVBOX" not in str(bodies) and "URGENT" not in str(bodies)
