"""AÇÃO 003 — D1 (revisor I): Master-fantoche pelo papel de runtime ``rd_app`` SEM a chave da aplicação.

(a) convite inserido direto no banco (``token_hash`` escolhido, convidante = Master ativo) ⇒ inspect/totp/complete
    recusados (resposta genérica) + alerta; integridade falha; reenvio não "lava"; revogação limpa.
(b) troca direta de ``hashed_password``/``must_change_password`` de titular (com selo "lixo", que o banco exige)
    ⇒ login (antes da senha), sessão, refresh recusados (401 genérico) + alerta; ``recover-master`` restaura.
Também: anti-lavagem do ``before_flush``, UPDATE em massa restrito, adoção TOFU 1× idempotente, fluxos legítimos
mantêm os selos válidos, concorrência reenvio × ativação sem 500 (D2).

As escritas do atacante rodam numa conexão própria com ``SET LOCAL ROLE rd_app`` (privilégios de produção no banco
descartável do harness); a aplicação conecta como superusuário (isento dos gatilhos, como o dono).
"""
from __future__ import annotations

import asyncio
import secrets
import uuid

import pyotp
import pytest
from sqlalchemy import create_engine, select, text, update

from app import cli
from app.core.crypto import sha256_hex
from app.core.passwords import hash_password
from app.models.users import User
from app.modules.notifications.service import drain
from app.modules.security import approval_mode as am
from app.modules.security import credentials
from tests.factories import CSRF, adopt_master_seats, auth_headers, get_user, login_as, make_user, totp_code
from tests.sec_helpers import API, active_master, bootstrap_master, complete_first_access, strong_password, token_from_mail

_GRANTED: set[str] = set()


def as_rd_app(sql: str, **params) -> None:
    """Executa ``sql`` como ``rd_app`` (privilégios de produção) no banco de teste; commit."""
    from tests.conftest import TEST_DB, _sync_url

    eng = create_engine(_sync_url(TEST_DB))
    try:
        with eng.begin() as c:
            if not c.execute(text("select exists (select 1 from pg_roles where rolname = 'rd_app')")).scalar():
                pytest.skip("papel rd_app inexistente neste cluster")
            if TEST_DB not in _GRANTED:
                from app.db import ddl
                from app.modules.financial import ddl as fin_ddl
                from app.modules.security import ddl as sec_ddl

                for s in ["GRANT USAGE ON SCHEMA public TO rd_app",
                          "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rd_app",
                          "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO rd_app",
                          *[f"REVOKE UPDATE, DELETE, TRUNCATE ON {t} FROM rd_app" for t in ddl.APPEND_ONLY_TABLES],
                          *fin_ddl.revoke_statements("rd_app"), *sec_ddl.revoke_statements("rd_app")]:
                    c.execute(text(s))
                _GRANTED.add(TEST_DB)
        with eng.begin() as c:
            c.execute(text("SET LOCAL ROLE rd_app"))
            c.execute(text(sql), params)
    finally:
        eng.dispose()


def rd_app_refused(sql: str, code: str, **params) -> None:
    with pytest.raises(Exception) as ei:
        as_rd_app(sql, **params)
    assert code in str(ei.value)


async def _count(db, sql: str, **p) -> int:
    return int((await db.execute(text(sql), p)).scalar() or 0)


GARBAGE = "f" * 64


# ─── (a) convite forjado ──────────────────────────────────────────────────────

async def test_forged_invite_by_rd_app_is_useless(client, db, mail, capsys):
    a = await active_master(db, name="Sócia A")
    sa = await login_as(client, a)  # antes dos rollbacks abaixo (que expiram os objetos da sessão)
    token = secrets.token_urlsafe(32)
    forged_id = uuid.uuid4()
    insert = ("INSERT INTO admin_master_invites (id, seat, email, full_name, token_hash, activation_user_id, invited_by, "
              "status, created_at, expires_at, failed_attempts, sent_count, last_sent_at{c}) VALUES (:id, 2, "
              "'fantoche@atacante.com.br', 'Fantoche', :th, :aid, :inv, 'PENDING', now(), now() + interval '72 hours', "
              "0, 1, now(){v})")
    # sem selo o banco recusa; com selo "lixo" aceita (e deixa rastro) — quem recusa é a aplicação
    rd_app_refused(insert.format(c="", v=""), "RD_MASTER_INVITE_SEAL_REQUIRED", id=forged_id, th=sha256_hex(token),
                   aid=uuid.uuid4(), inv=a.id)
    as_rd_app(insert.format(c=", seal", v=", :seal"), id=forged_id, th=sha256_hex(token), aid=uuid.uuid4(), inv=a.id,
              seal=GARBAGE)
    for path in ("inspect", "totp"):
        r = await client.post(f"{API}/auth/master-activation/{path}", json={"token": token})
        assert r.status_code == 400 and r.json()["error"]["code"] == "MASTER_INVITE_INVALID", r.text
    r = await client.post(f"{API}/auth/master-activation/complete",
                          json={"token": token, "password": strong_password(), "totp_code": "123456",
                                "financial_password": "Fin-atacante1"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "MASTER_INVITE_INVALID"
    assert await _count(db, "SELECT count(*) FROM users WHERE admin_master_seat = 2") == 0
    await drain()
    assert await _count(db, "SELECT count(*) FROM audit_logs WHERE action = 'security.master_invite_seal_invalid'") == 1
    assert await _count(db, "SELECT count(*) FROM notifications WHERE event_type = 'security.integrity_alert'") >= 1
    rep = await am.verify_integrity(db)
    assert not rep.ok and any("convite pendente do assento 2: selo inválido" in p for p in rep.problems)
    assert await cli.cmd_security_verify() == 1
    capsys.readouterr()
    with pytest.raises(Exception) as ei:
        await am.ensure_financial_authorization_allowed(db)
    assert getattr(ei.value, "code", "") == "SECURITY_INTEGRITY_ALERT"
    await db.rollback()
    # o Master vê o convite estranho; reenviar NÃO re-sela (senão o atacante receberia um token válido)
    m = (await client.get(f"{API}/security/masters", headers=sa.headers)).json()
    assert m["invite"]["id"] == str(forged_id)
    r = await client.post(f"{API}/security/masters/invites/{forged_id}/resend", headers=sa.headers)
    assert r.status_code == 503 and r.json()["error"]["code"] == "SECURITY_INTEGRITY_ALERT"
    assert not [x for x in mail.outbox if x.to == "fantoche@atacante.com.br"]
    r = await client.request("DELETE", f"{API}/security/masters/invites/{forged_id}",
                             json={"reason": "convite forjado no banco"}, headers=sa.headers)
    assert r.status_code == 204
    assert (await am.verify_integrity(db)).ok


async def test_forged_token_swap_on_legit_invite(client, db, mail):
    a = await active_master(db)
    sa = await login_as(client, a)
    r = await client.post(f"{API}/security/masters/invites",
                          json={"full_name": "Sócio B", "email": "b@empresa.com.br", "totp_code": totp_code(a)},
                          headers=sa.headers)
    assert r.status_code == 201
    inv_id = r.json()["id"]
    assert (await am.verify_integrity(db)).ok  # convite legítimo nasce selado
    evil = secrets.token_urlsafe(32)
    rd_app_refused("UPDATE admin_master_invites SET token_hash = :th WHERE id = :i", "RD_", th=sha256_hex(evil), i=inv_id)
    as_rd_app("UPDATE admin_master_invites SET token_hash = :th, seal = :s WHERE id = :i", th=sha256_hex(evil),
              s=GARBAGE, i=inv_id)
    r = await client.post(f"{API}/auth/master-activation/inspect", json={"token": evil})
    assert r.status_code == 400 and r.json()["error"]["code"] == "MASTER_INVITE_INVALID"


# ─── (b) credencial de titular trocada direto no banco ────────────────────────

async def test_pending_first_access_master_cannot_be_hijacked(client, db, redis):
    m = await bootstrap_master(dual=True)
    evil_pw = strong_password("Evil")
    # sem selo novo o banco recusa; com selo "lixo" aceita — a aplicação recusa o login ANTES da senha
    rd_app_refused("UPDATE users SET hashed_password = :h WHERE admin_master_seat = 1",
                   "RD_MASTER_CREDENTIAL_SEAL_REQUIRED", h=hash_password(evil_pw))
    as_rd_app("UPDATE users SET hashed_password = :h, master_credential_seal = :s WHERE admin_master_seat = 1",
              h=hash_password(evil_pw), s=GARBAGE)
    r_evil = await client.post(f"{API}/auth/login", json={"login": m.email, "password": evil_pw})
    r_wrong = await client.post(f"{API}/auth/login", json={"login": m.email, "password": "Errada-2026xx"})
    assert r_evil.status_code == 401 and r_evil.json() == r_wrong.json()  # nada revela o motivo
    r = await client.post(f"{API}/auth/login", json={"login": m.email, "password": m.password})
    assert r.status_code == 401  # falha FECHADA também para a senha legítima (o hash foi trocado)
    await drain()
    assert await _count(db, "SELECT count(*) FROM audit_logs WHERE action = "
                            "'security.master_credential_seal_invalid'") == 1  # deduplicado (1×/hora)
    rep = await am.verify_integrity(db)
    assert not rep.ok and any("selo de credencial do titular inválido" in p for p in rep.problems)
    from app.modules.websocket.metrics import render_metrics

    assert "rd_security_integrity_ok 0" in await render_metrics()
    # remédio: recover-master (shell + chave) re-sela o MESMO titular com senha temporária
    import os
    import tempfile

    tmp = strong_password("Rec")
    f = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
    f.write(tmp)
    f.close()
    os.environ["RD_RECOVERY_PASSWORD_FILE"] = f.name
    try:
        await cli.cmd_recover_master(1, "credencial adulterada no banco (teste D1)")
    finally:
        os.environ.pop("RD_RECOVERY_PASSWORD_FILE", None)
        os.unlink(f.name)
    assert (await am.verify_integrity(db)).ok
    r = await client.post(f"{API}/auth/login", json={"login": m.email, "password": tmp})
    assert r.status_code == 200 and r.json()["status"] == "mfa_setup_required"


async def test_active_master_session_and_refresh_refused_after_tampering(client, db, redis):
    a = await active_master(db)
    r = await client.post(f"{API}/auth/login", json={"login": a.email, "password": a.password})
    r = await client.post(f"{API}/auth/mfa/verify", json={"mfa_token": r.json()["mfa_token"], "code": totp_code(a)})
    assert r.status_code == 200
    h = auth_headers(r.json()["access_token"])
    assert (await client.get(f"{API}/me", headers=h)).status_code == 200
    as_rd_app("UPDATE users SET must_change_password = true, master_credential_seal = :s WHERE id = :i",
              s=GARBAGE, i=a.id)
    r = await client.get(f"{API}/me", headers=h)
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_TOKEN"
    r = await client.post(f"{API}/auth/refresh", headers=CSRF)
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_REFRESH"
    r = await client.post(f"{API}/auth/login", json={"login": a.email, "password": a.password})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_mfa_verify_refused_when_seal_broken_after_password_step(client, db, redis):
    a = await active_master(db)
    r = await client.post(f"{API}/auth/login", json={"login": a.email, "password": a.password})
    mfa_token = r.json()["mfa_token"]
    as_rd_app("UPDATE users SET financial_password_hash = :h, master_credential_seal = :s WHERE id = :i",
              h=hash_password("Fin-atacante1"), s=GARBAGE, i=a.id)
    r = await client.post(f"{API}/auth/mfa/verify", json={"mfa_token": mfa_token, "code": totp_code(a)})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_MFA_TOKEN"


async def test_flush_never_launders_a_tampered_state(db, redis):
    a = await active_master(db)
    a_id = a.id
    await db.execute(text("UPDATE users SET hashed_password = :h WHERE id = :i"),  # superusuário: só p/ o cenário
                     {"h": hash_password(strong_password()), "i": a_id})
    await db.commit()
    u = await get_user(db, a_id)
    u.financial_password_hash = hash_password("Fin-legitima9")  # escrita "legítima" posterior
    with pytest.raises(credentials.MasterCredentialSealError):
        await db.commit()
    await db.rollback()
    assert not credentials.credential_ok(await get_user(db, a_id))
    await drain()


async def test_orm_writes_reseal_and_bulk_update_is_restricted(db, redis):
    a = await active_master(db)
    before = (await get_user(db, a.id)).master_credential_seal
    u = await get_user(db, a.id)
    u.must_change_password = True
    await db.commit()
    u = await get_user(db, a.id)
    assert u.master_credential_seal != before and credentials.credential_ok(u)
    res = await db.execute(update(User).where(User.id == a.id).values(must_change_password=False))
    await db.commit()
    assert res.rowcount == 0  # UPDATE em massa de campo selado nunca alcança titular
    assert (await get_user(db, a.id)).must_change_password is True
    plain = await make_user(db, role="employee")
    res = await db.execute(update(User).where(User.id == plain.id).values(must_change_password=True))
    await db.commit()
    assert res.rowcount == 1


async def test_tofu_adoption_once_and_idempotent(db, redis, tmp_path):
    from app.modules.security import bootstrap as boot

    legacy = await make_user(db, role="super_admin", mfa=True, admin_master_seat=1, email="legado@empresa.com.br")
    await db.execute(text("UPDATE users SET master_credential_seal = NULL WHERE id = :i"), {"i": legacy.id})
    await db.commit()  # instalação anterior à 0009 (superusuário de teste: isento do gatilho)
    f = tmp_path / "pw"
    f.write_text(strong_password(), encoding="utf-8")
    env = {"RD_BOOTSTRAP_DUAL_MASTER_APPROVAL": "true", "RD_BOOTSTRAP_MASTER_EMAIL": "novo@rd-midiaindoor.localhost",
           "RD_BOOTSTRAP_MASTER_PASSWORD_FILE": str(f)}
    r1 = await boot.bootstrap_instance(env)
    r2 = await boot.bootstrap_instance(env)
    assert r1.credential_seals_adopted == [1] and r2.credential_seals_adopted == []
    assert any("TOFU" in line for line in r1.lines())
    assert await _count(db, "SELECT count(*) FROM audit_logs WHERE action = "
                            "'security.master_credential_seal_adopted'") == 1
    assert credentials.credential_ok(await get_user(db, legacy.id))
    assert (await am.verify_integrity(db)).ok


async def test_legitimate_flows_keep_seals_valid(client, db, mail):
    """bootstrap → setup completo → troca de senha → convite → reenvio → ativação: integridade sempre OK."""
    m = await bootstrap_master(dual=True)
    assert (await am.verify_integrity(db)).ok
    await complete_first_access(client, m)
    assert (await am.verify_integrity(db)).ok
    new_pw = strong_password("Troca")
    r = await client.post(f"{API}/auth/password/change", json={"current_password": m.password, "new_password": new_pw},
                          headers=m.headers)
    assert r.status_code == 204
    m.password = new_pw
    assert (await am.verify_integrity(db)).ok
    r = await client.post(f"{API}/security/masters/invites",
                          json={"full_name": "Sócio B", "email": "b@empresa.com.br", "totp_code": totp_code(m)},
                          headers=m.headers)
    assert r.status_code == 201, r.text
    inv_id = r.json()["id"]
    assert (await am.verify_integrity(db)).ok
    r = await client.post(f"{API}/security/masters/invites/{inv_id}/resend", headers=m.headers)
    assert r.status_code == 200
    assert (await am.verify_integrity(db)).ok
    token = token_from_mail(mail, "b@empresa.com.br")
    secret = (await client.post(f"{API}/auth/master-activation/totp", json={"token": token})).json()["secret"]
    r = await client.post(f"{API}/auth/master-activation/complete",
                          json={"token": token, "password": strong_password("Bpw"), "totp_code": pyotp.TOTP(secret).now(),
                                "financial_password": "FinB-" + secrets.token_hex(5)})
    assert r.status_code == 200, r.text
    rep = await am.verify_integrity(db)
    assert rep.ok, rep.problems
    rows = (await db.execute(select(User).where(User.admin_master_seat.is_not(None)))).scalars().all()
    assert len(rows) == 2 and all(credentials.credential_ok(u) for u in rows)
    st = await am.ensure_financial_authorization_allowed(db)  # DUAL com 2 Masters ativos e selados
    assert st.mode == "DUAL"
    await db.rollback()


async def test_concurrent_resend_and_complete_never_500(client_factory, db, mail):
    """D2: ativação × reenvio × revogação simultâneos seguem a mesma ordem de locks ⇒ nenhum 500."""
    a = await active_master(db)
    c0 = client_factory()
    sa = await login_as(c0, a)
    r = await c0.post(f"{API}/security/masters/invites",
                      json={"full_name": "Sócio B", "email": "b@empresa.com.br", "totp_code": totp_code(a)},
                      headers=sa.headers)
    inv_id = r.json()["id"]
    token = token_from_mail(mail, "b@empresa.com.br")
    secret = (await c0.post(f"{API}/auth/master-activation/totp", json={"token": token})).json()["secret"]
    code = pyotp.TOTP(secret).now()
    clients = [client_factory() for _ in range(5)]
    reqs = [
        clients[0].post(f"{API}/auth/master-activation/complete",
                        json={"token": token, "password": strong_password(), "totp_code": code,
                              "financial_password": "FinB-" + secrets.token_hex(5)}),
        clients[1].post(f"{API}/security/masters/invites/{inv_id}/resend", headers=sa.headers),
        clients[2].post(f"{API}/auth/master-activation/totp", json={"token": token}),
        clients[3].post(f"{API}/security/masters/invites/{inv_id}/resend", headers=sa.headers),
        clients[4].post(f"{API}/auth/master-activation/inspect", json={"token": token}),
    ]
    rs = await asyncio.gather(*reqs)
    assert all(r.status_code < 500 for r in rs), [(r.status_code, r.text) for r in rs]
    await drain()
    rep = await am.verify_integrity(db)
    assert rep.ok, rep.problems
    assert await _count(db, "SELECT count(*) FROM users WHERE admin_master_seat = 2") in (0, 1)
    assert adopt_master_seats  # (import compartilhado com outros testes)


async def test_readoption_after_column_drop_and_add(client, db, redis, tmp_path):
    """Reconciliação do dev (E: downgrade defensivo → 0009 final): a coluna volta NULL ⇒ o ``bootstrap-instance``
    re-adota (TOFU) e audita 1×; depois ``security-verify`` OK e o login do titular pendente NÃO é recusado."""
    from tests.conftest import TEST_DB, _sync_url

    m = await bootstrap_master(dual=True)  # Master do bootstrap já nasce selado (sem auditoria de adoção)
    assert await _count(db, "SELECT count(*) FROM audit_logs WHERE action = "
                            "'security.master_credential_seal_adopted'") == 0
    await db.commit()  # libera a conexão (NullPool) antes da alteração de schema
    eng = create_engine(_sync_url(TEST_DB))
    try:
        with eng.begin() as c:  # superusuário; DROP+ADD na MESMA transação (gatilhos citam a coluna)
            c.execute(text("ALTER TABLE users DROP COLUMN master_credential_seal"))
            c.execute(text("ALTER TABLE users ADD COLUMN master_credential_seal text"))
    finally:
        eng.dispose()
    rep = await am.verify_integrity(db)
    assert not rep.ok and any("selo de credencial ausente" in p for p in rep.problems)
    r = await client.post(f"{API}/auth/login", json={"login": m.email, "password": m.password})
    assert r.status_code == 401  # NULL = falha fechada até a adoção
    await db.commit()
    from app.modules.security import bootstrap as boot

    f = tmp_path / "pw"
    f.write_text(strong_password(), encoding="utf-8")
    env = {"RD_BOOTSTRAP_DUAL_MASTER_APPROVAL": "true", "RD_BOOTSTRAP_MASTER_EMAIL": m.email,
           "RD_BOOTSTRAP_MASTER_PASSWORD_FILE": str(f)}
    r1 = await boot.bootstrap_instance(env)
    r2 = await boot.bootstrap_instance(env)
    assert r1.credential_seals_adopted == [1] and r2.credential_seals_adopted == []
    assert await _count(db, "SELECT count(*) FROM audit_logs WHERE action = "
                            "'security.master_credential_seal_adopted'") == 1
    assert (await am.verify_integrity(db)).ok
    assert await cli.cmd_security_verify() == 0
    r = await client.post(f"{API}/auth/login", json={"login": m.email, "password": m.password})
    assert r.status_code == 200 and r.json()["status"] == "mfa_setup_required"
    assert r.json()["setup"]["next_step"] == "PASSWORD_CHANGE"
