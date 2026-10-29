"""AÇÃO 003 — convite e ativação do 2º Administrador Master (ADR_011 D7; SPEC §5.1, §5.2).

Um convite pendente por vez; token só no e-mail ao convidado (fragmento), nunca na API/auditoria; reenvio
troca o token; revogação; expiração; ativação atômica (senha + TOTP + senha financeira do PRÓPRIO B);
token reutilizado/expirado/forjado ⇒ 400; 5 TOTP errados ⇒ LOCKED; 3º Master impossível; alerta de mesmo
dispositivo; status sem segredos; evento WS ``security.status_changed``.
"""
from __future__ import annotations

import json
import secrets

import pyotp
from sqlalchemy import text

from app.core import totp as totp_mod
from tests.factories import auth_headers, login_as, make_user
from tests.sec_helpers import API, active_master, capture_events, strong_password, token_from_mail

B_EMAIL = "socio.b@empresa-parceira.com.br"


def _err(r) -> str:
    return r.json()["error"]["code"]


async def _invite(client, sa, *, email=B_EMAIL, name="Sócio B", code=None):
    from tests.factories import totp_code

    return await client.post(f"{API}/security/masters/invites",
                             json={"full_name": name, "email": email, "totp_code": code or totp_code(sa.user)},
                             headers=sa.headers)


async def _activate(client, token, *, password=None, fin=None, code=None):
    r = await client.post(f"{API}/auth/master-activation/totp", json={"token": token})
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/")
    password = password or strong_password("Bpw")
    fin = fin or ("FinB-" + secrets.token_hex(5))
    code = code or pyotp.TOTP(secret).now()
    r = await client.post(f"{API}/auth/master-activation/complete",
                          json={"token": token, "password": password, "totp_code": code, "financial_password": fin})
    return r, secret, password, fin


async def test_invite_and_activation_full_flow(client, db, mail, monkeypatch):
    events = capture_events(monkeypatch)
    a = await active_master(db, name="Sócia A")
    sa = await login_as(client, a)
    me = (await client.get(f"{API}/me", headers=sa.headers)).json()["security_setup"]
    assert me["next_step"] == "SECOND_MASTER" and me["steps"]["second_master"] == "NOT_INVITED"

    # TOTP errado do convidante ⇒ 400 e conta no lockout
    r = await _invite(client, sa, code="000000")
    assert r.status_code == 400 and _err(r) == "INVALID_MFA_CODE"
    r = await _invite(client, sa)
    assert r.status_code == 201, r.text
    inv = r.json()
    assert set(inv) == {"id", "seat", "full_name", "email_masked", "status", "expires_at", "invited_by_name",
                        "sent_count"}
    assert inv["seat"] == 2 and inv["status"] == "PENDING" and inv["email_masked"] == "so***@empresa-parceira.com.br"
    token = token_from_mail(mail, B_EMAIL)
    assert token not in r.text
    # e-mail SOMENTE ao convidado (nenhum ao convidante contendo o link)
    assert all(token not in m.text for m in mail.outbox if m.to.lower() != B_EMAIL)
    assert len([m for m in mail.outbox if token in m.text]) == 1
    # um convite pendente por vez
    r = await _invite(client, sa, email="outro@empresa-parceira.com.br", code="123456")  # recusado antes do TOTP
    assert r.status_code == 409 and _err(r) == "MASTER_INVITE_ALREADY_PENDING"
    # estado para A
    r = await client.get(f"{API}/security/masters", headers=sa.headers)
    body = r.json()
    assert r.status_code == 200 and token not in r.text
    assert body["seats"] == [{"seat": 1, "name": "Sócia A", "status": "ACTIVE", "is_self": True},
                             {"seat": 2, "name": None, "status": "VACANT", "is_self": False}]
    assert body["invite"]["id"] == inv["id"] and body["can_invite"] is False and body["approval_mode"] == "DUAL"
    st = (await client.get(f"{API}/me", headers=sa.headers)).json()["security_setup"]
    assert st["steps"]["second_master"] == "INVITE_PENDING" and st["dual_approval_ready"] is False

    # B inspeciona
    r = await client.post(f"{API}/auth/master-activation/inspect", json={"token": token})
    assert r.status_code == 200 and r.json()["full_name"] == "Sócio B"
    assert r.json()["email_masked"] == "so***@empresa-parceira.com.br"
    assert r.json()["steps"] == ["PASSWORD", "TOTP", "FINANCIAL_PASSWORD"]
    # ativação sem TOTP iniciado ⇒ recusada
    r = await client.post(f"{API}/auth/master-activation/complete",
                          json={"token": token, "password": strong_password(), "totp_code": "123456",
                                "financial_password": "FinB-12345678"})
    assert r.status_code == 400 and _err(r) == "MFA_SETUP_NOT_STARTED"
    # senha financeira igual à de login ⇒ recusada
    pw = strong_password("Bpw")
    r, _, _, _ = await _activate(client, token, password=pw, fin=pw)
    assert r.status_code == 400 and _err(r) == "FINANCIAL_PASSWORD_POLICY"
    # ativação OK
    r, secret, b_pw, b_fin = await _activate(client, token)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "ok" and out["access_token"] and len(out["recovery_codes"]) == 10
    assert out["user"]["is_admin_master"] is True and "rd_refresh" in r.headers.get("set-cookie", "")
    b_headers = auth_headers(out["access_token"])
    # B já entra com o painel liberado (sem passos pendentes)
    st_b = (await client.get(f"{API}/me", headers=b_headers)).json()["security_setup"]
    assert st_b["required"] is False and st_b["steps"]["second_master"] == "DONE" and st_b["dual_approval_ready"]
    assert (await client.get(f"{API}/users", headers=b_headers)).status_code == 200

    rows = (await db.execute(text(
        "SELECT u.admin_master_seat, u.must_change_password, u.totp_enabled, u.financial_password_hash IS NOT NULL, "
        "rd_is_active_master(u.id) FROM users u WHERE u.admin_master_seat IS NOT NULL ORDER BY 1"))).all()
    assert [tuple(x) for x in rows] == [(1, False, True, True, True), (2, False, True, True, True)]
    assert (await db.execute(text("SELECT rd_active_master_count()"))).scalar() == 2
    ev = (await db.execute(text("SELECT seat, source, invite_id IS NOT NULL FROM admin_master_seat_events ORDER BY id"))).all()
    assert [tuple(x) for x in ev] == [(1, "LEGACY_ADOPTED", False), (2, "MASTER_INVITE", True)]
    assert (await db.execute(text("SELECT status FROM admin_master_invites"))).scalar() == "ACCEPTED"
    act = (await db.execute(text(
        "SELECT after_data FROM audit_logs WHERE action = 'admin_master.activated'"))).scalar()
    assert act["seat"] == 2 and act["same_device_as_inviter"] is True  # mesmo IP+UA do cliente de teste
    notif = {r[0] for r in (await db.execute(text("SELECT event_type FROM notifications"))).all()}
    assert {"security.second_master_activated", "security.master_activation_same_device"} <= notif
    # token nunca auditado/registrado
    dump = (await db.execute(text(
        "SELECT coalesce(string_agg(coalesce(after_data::text,'') || coalesce(before_data::text,'') || "
        "coalesce(reason,''), ' '), '') FROM audit_logs"))).scalar()
    for secret_value in (token, b_pw, b_fin, secret):
        assert secret_value not in dump
    n_dump = (await db.execute(text(
        "SELECT coalesce(string_agg(title || body || data::text, ' '), '') FROM notifications"))).scalar()
    assert token not in n_dump

    # token reutilizado ⇒ inválido
    r = await client.post(f"{API}/auth/master-activation/inspect", json={"token": token})
    assert r.status_code == 400 and _err(r) == "MASTER_INVITE_INVALID"
    r = await client.post(f"{API}/auth/master-activation/complete",
                          json={"token": token, "password": strong_password(), "totp_code": "123456",
                                "financial_password": "FinB-12345678"})
    assert r.status_code == 400 and _err(r) == "MASTER_INVITE_INVALID"
    # A vê DONE; 3º Master impossível
    st = (await client.get(f"{API}/me", headers=sa.headers)).json()["security_setup"]
    assert st["steps"]["second_master"] == "DONE" and st["dual_approval_ready"] is True and st["next_step"] is None
    r = await _invite(client, sa, email="terceiro@empresa-parceira.com.br", code="123456")
    assert r.status_code == 409 and _err(r) == "ADMIN_MASTER_SEATS_FULL"
    m = (await client.get(f"{API}/security/masters", headers=sa.headers)).json()
    assert [s["status"] for s in m["seats"]] == ["ACTIVE", "ACTIVE"] and m["invite"] is None
    assert B_EMAIL not in json.dumps(m)  # nunca e-mail completo do outro Master
    from app.modules.notifications.service import drain

    await drain()
    reasons = {e[1].get("reason") for e in events.of_type("security.status_changed")}
    assert {"invite_created", "master_activated"} <= reasons
    from app.modules.security.approval_mode import verify_integrity

    assert (await verify_integrity(db)).ok


async def test_resend_rotates_token_and_revoke(client, db, mail):
    a = await active_master(db)
    sa = await login_as(client, a)
    r = await _invite(client, sa)
    inv_id = r.json()["id"]
    t1 = token_from_mail(mail, B_EMAIL)
    r = await client.post(f"{API}/security/masters/invites/{inv_id}/resend", headers=sa.headers)
    assert r.status_code == 200 and r.json()["sent"] is True and "expires_at" in r.json()
    t2 = token_from_mail(mail, B_EMAIL)
    assert t1 != t2 and t2 not in r.text
    assert (await client.post(f"{API}/auth/master-activation/inspect", json={"token": t1})).status_code == 400
    assert (await client.post(f"{API}/auth/master-activation/inspect", json={"token": t2})).status_code == 200
    assert (await db.execute(text("SELECT sent_count FROM admin_master_invites"))).scalar() == 2
    # reenvio limitado (3/h por convite)
    codes = []
    for _ in range(3):
        codes.append((await client.post(f"{API}/security/masters/invites/{inv_id}/resend", headers=sa.headers)).status_code)
    assert codes[-1] == 429 and (await client.post(
        f"{API}/security/masters/invites/{inv_id}/resend", headers=sa.headers)).json()["error"]["code"] == \
        "MASTER_INVITE_RATE_LIMITED"
    # revogação
    r = await client.request("DELETE", f"{API}/security/masters/invites/{inv_id}", json={"reason": "convite enviado errado"},
                             headers=sa.headers)
    assert r.status_code == 204
    assert (await db.execute(text("SELECT status, revoke_reason FROM admin_master_invites"))).one() == \
        ("REVOKED", "convite enviado errado")
    assert (await client.post(f"{API}/auth/master-activation/inspect", json={"token": t2})).status_code == 400
    assert (await client.request("DELETE", f"{API}/security/masters/invites/{inv_id}", headers=sa.headers)).status_code == 404
    # depois de revogar, novo convite é permitido
    r = await _invite(client, sa)
    assert r.status_code == 201


async def test_expired_invite_is_invalid_and_replaced(client, db, mail):
    a = await active_master(db)
    sa = await login_as(client, a)
    r = await _invite(client, sa)
    t1 = token_from_mail(mail, B_EMAIL)
    await db.execute(text("UPDATE admin_master_invites SET expires_at = created_at + interval '1 millisecond'"))
    await db.commit()
    r = await client.post(f"{API}/auth/master-activation/inspect", json={"token": t1})
    assert r.status_code == 400 and _err(r) == "MASTER_INVITE_INVALID"
    st = (await client.get(f"{API}/me", headers=sa.headers)).json()["security_setup"]
    assert st["steps"]["second_master"] == "NOT_INVITED"  # expirado não conta como pendente
    r = await _invite(client, sa)
    assert r.status_code == 201
    rows = (await db.execute(text("SELECT status FROM admin_master_invites ORDER BY created_at"))).all()
    assert [x[0] for x in rows] == ["EXPIRED", "PENDING"]
    assert (await db.execute(text(
        "SELECT count(*) FROM audit_logs WHERE action = 'admin_master.invite_expired'"))).scalar() == 1


async def test_five_wrong_totp_lock_invite(client, db, mail):
    a = await active_master(db)
    sa = await login_as(client, a)
    await _invite(client, sa)
    token = token_from_mail(mail, B_EMAIL)
    r = await client.post(f"{API}/auth/master-activation/totp", json={"token": token})
    secret = r.json()["secret"]
    good = pyotp.TOTP(secret).now()
    wrong = f"{(int(good) + 1) % 10**6:06d}"
    payload = {"token": token, "password": strong_password(), "totp_code": wrong, "financial_password": "FinB-12345678"}
    codes = []
    for _ in range(5):
        r = await client.post(f"{API}/auth/master-activation/complete", json=payload)
        codes.append(_err(r))
    assert codes == ["INVALID_MFA_CODE"] * 4 + ["MASTER_INVITE_LOCKED"]
    assert (await db.execute(text("SELECT status, failed_attempts FROM admin_master_invites"))).one() == ("LOCKED", 5)
    r = await client.post(f"{API}/auth/master-activation/complete", json={**payload, "totp_code": good})
    assert r.status_code == 400 and _err(r) == "MASTER_INVITE_INVALID"
    assert (await db.execute(text("SELECT count(*) FROM users"))).scalar() == 1  # nenhum usuário criado


async def test_invite_refused_in_single_mode_and_by_non_active_master(client, db):
    a = await active_master(db, mode="SINGLE")
    sa = await login_as(client, a)
    r = await _invite(client, sa, code="123456")
    assert r.status_code == 409 and _err(r) == "APPROVAL_MODE_SINGLE"
    m = (await client.get(f"{API}/security/masters", headers=sa.headers)).json()
    assert m["approval_mode"] == "SINGLE" and m["can_invite"] is False
    st = (await client.get(f"{API}/me", headers=sa.headers)).json()["security_setup"]
    assert st["steps"]["second_master"] == "NOT_REQUIRED" and st["next_step"] is None
    # não-Master (admin) ⇒ 403
    adm = await make_user(db, role="admin", mfa=True)
    s = await login_as(client, adm)
    r = await client.get(f"{API}/security/masters", headers=s.headers)
    assert r.status_code == 403 and _err(r) == "ADMIN_MASTER_REQUIRED"
    r = await client.post(f"{API}/security/masters/invites",
                          json={"full_name": "X Y", "email": "x@empresa.com.br", "totp_code": "123456"}, headers=s.headers)
    assert r.status_code == 403 and _err(r) == "ADMIN_MASTER_REQUIRED"


async def test_invite_email_validation_and_registered(client, db, mail):
    a = await active_master(db)
    sa = await login_as(client, a)
    other = await make_user(db, role="employee", email="ja.existe@empresa.com.br")
    r = await _invite(client, sa, email=other.email, code="123456")
    assert r.status_code == 409 and _err(r) == "EMAIL_ALREADY_REGISTERED"
    r = await _invite(client, sa, email="sem-arroba", code="123456")
    assert r.status_code == 400 and _err(r) == "EMAIL_INVALID"
    # e-mail registrado DEPOIS do convite ⇒ ativação recusada
    r = await _invite(client, sa)
    token = token_from_mail(mail, B_EMAIL)
    await make_user(db, role="employee", email=B_EMAIL)
    r, *_ = await _activate(client, token)
    assert r.status_code == 409 and _err(r) == "EMAIL_ALREADY_REGISTERED"


async def test_forged_token_and_rate_limit(client, db):
    forged = secrets.token_urlsafe(32)
    r = await client.post(f"{API}/auth/master-activation/inspect", json={"token": forged})
    assert r.status_code == 400 and _err(r) == "MASTER_INVITE_INVALID"
    r = await client.post(f"{API}/auth/master-activation/totp", json={"token": forged})
    assert r.status_code == 400 and _err(r) == "MASTER_INVITE_INVALID"
    statuses = [(await client.post(f"{API}/auth/master-activation/inspect", json={"token": forged})).status_code
                for _ in range(20)]
    assert statuses[-1] == 429


async def test_setup_pending_other_master_status(client, db):
    """Assento 2 ocupado por quem ainda não concluiu o setup ⇒ OTHER_MASTER_SETUP_PENDING."""
    from tests.factories import adopt_master_seats

    a = await active_master(db)
    b = await make_user(db, role="super_admin", admin_master_seat=2, must_change_password=True, financial_password=False)
    await adopt_master_seats(db, b)
    sa = await login_as(client, a)
    st = (await client.get(f"{API}/me", headers=sa.headers)).json()["security_setup"]
    assert st["steps"]["second_master"] == "OTHER_MASTER_SETUP_PENDING" and st["next_step"] == "SECOND_MASTER"
    m = (await client.get(f"{API}/security/masters", headers=sa.headers)).json()
    assert [s["status"] for s in m["seats"]] == ["ACTIVE", "SETUP_PENDING"] and m["can_invite"] is False
    assert totp_mod  # (import usado em outros testes)
