"""AÇÃO 003 / agente E — migration 0009: selos de credencial/convite (D1 do revisor I: Master-fantoche).

Ataque D1 (papel de runtime ``rd_app`` SEM a chave da aplicação):
(a) INSERT de convite com ``token_hash`` escolhido (convidante = Master ativo, assento 2) ⇒ ativação pela API com
    os fatores do atacante;
(b) UPDATE de ``hashed_password``/``must_change_password`` de um titular (ex.: assento 1 pendente de primeiro acesso)
    ⇒ login ⇒ setup com os fatores do atacante.

Cada cenário roda como ``rd_app`` (privilégios de produção) em DOIS bancos descartáveis: na 0008 (o banco ACEITAVA —
prova) e no head (0009 — recusado sem selo; com selo "lixo" o banco aceita e deixa rastro: quem recusa é a
aplicação no login/ativação e em ``verify_integrity`` — testes do agente B).
"""
from __future__ import annotations

import secrets
import uuid

import pytest
from sqlalchemy import create_engine

from tests.adversarial.test_sec_db_rd_app import PWD_HASH, Sec, grant_runtime_like_production
from tests.migrations.test_migrations import _url, alembic, scratch_db

ATTACKER_HASH = "$2b$12$" + "A" * 53  # hash bcrypt fictício "do atacante"


@pytest.fixture(scope="module", params=["0008", "head"])
def seal_db(request, _test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", request.param)
        if not grant_runtime_like_production(db):
            pytest.skip("papel rd_app inexistente neste cluster (criado pelo db-init)")
        yield request.param, db


@pytest.fixture
def ctx(seal_db):
    rev, db = seal_db
    eng = create_engine(_url(db))
    conn = eng.connect()
    trans = conn.begin()
    try:
        yield rev, Sec(conn)
    finally:
        trans.rollback()
        conn.close()
        eng.dispose()


def sealed(rev: str) -> bool:
    return rev != "0008"


def attempt(s: Sec, rev: str, sql: str, code: str, **p) -> None:
    """Na 0008 o banco ACEITA (prova do D1); no head recusa com ``code``."""
    if sealed(rev):
        s.denied(sql, code, **p)
    else:
        s.allowed(sql, **p)


INVITE_SQL = ("INSERT INTO admin_master_invites (id, seat, email, full_name, token_hash, activation_user_id, invited_by, "
              "status, expires_at) VALUES (:i, 2, 'fantoche@rd-teste.com.br', 'Fantoche', :t, :act, :a, 'PENDING', "
              "now() + interval '72 hours')")


def test_invite_without_seal_is_refused_by_the_database(ctx):
    rev, s = ctx
    s.init_mode("DUAL")
    a = s.user("a")
    s.assign(a, 1, "BOOTSTRAP_ENV")
    s.ready(a)
    s.as_app()
    # (a) convite com token escolhido pelo atacante, sem o selo da aplicação
    attempt(s, rev, INVITE_SQL, "RD_MASTER_INVITE_SEAL_REQUIRED", i=uuid.uuid4(), t=secrets.token_hex(32),
            act=uuid.uuid4(), a=a)
    if sealed(rev):
        # com selo LIXO o banco aceita (não conhece a chave) — a ativação confere o HMAC e recusa (agente B)
        inv, _act, _email = s.invite(a, seal="selo-forjado-sem-a-chave")
        assert s.one("SELECT seal FROM admin_master_invites WHERE id = :i", i=inv) == "selo-forjado-sem-a-chave"
        # reenvio legítimo = token + selo juntos; token sozinho / selo removido ⇒ recusados
        s.denied("UPDATE admin_master_invites SET token_hash = :t WHERE id = :i", "RD_MASTER_INVITE_SEAL_REQUIRED",
                 t=secrets.token_hex(32), i=inv)
        s.denied("UPDATE admin_master_invites SET seal = NULL, token_hash = :t WHERE id = :i",
                 "RD_MASTER_INVITE_SEAL_REQUIRED", t=secrets.token_hex(32), i=inv)
        s.allowed("UPDATE admin_master_invites SET token_hash = :t, seal = :s, sent_count = sent_count + 1 "
                  "WHERE id = :i", t=secrets.token_hex(32), s=secrets.token_hex(32), i=inv)


def test_invite_token_cannot_be_swapped_without_new_seal(ctx):
    rev, s = ctx
    s.init_mode("DUAL")
    a = s.user("a")
    s.assign(a, 1, "BOOTSTRAP_ENV")
    s.ready(a)
    inv, _act, _email = s.invite(a)  # convite legítimo (dono; com selo no head)
    s.as_app()
    # atacante troca o token do convite legítimo por um que ele conhece (o link real fica inválido)
    attempt(s, rev, "UPDATE admin_master_invites SET token_hash = :t WHERE id = :i", "RD_MASTER_INVITE_SEAL_REQUIRED",
            t=secrets.token_hex(32), i=inv)


def test_pending_first_access_master_password_cannot_be_hijacked(ctx):
    """(b) assento 1 recém-criado pelo bootstrap (troca de senha pendente, sem TOTP): o atacante põe a SUA senha."""
    rev, s = ctx
    s.init_mode("DUAL")
    a = s.user("a")  # must_change_password = true
    s.assign(a, 1, "BOOTSTRAP_ENV")
    s.as_app()
    attempt(s, rev, "UPDATE users SET hashed_password = :h, must_change_password = false, password_changed_at = now() "
            "WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED", h=ATTACKER_HASH, u=a)
    if sealed(rev):
        s.denied("UPDATE users SET must_change_password = false WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED", u=a)
        # TOTP habilitado pelo atacante (primeiro acesso) também exige selo novo
        s.denied("UPDATE users SET totp_enabled = true, totp_secret_enc = 'v1:atacante' WHERE id = :u",
                 "RD_MASTER_CREDENTIAL_SEAL_REQUIRED", u=a)
        s.denied("UPDATE users SET financial_password_hash = :f WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED",
                 f=ATTACKER_HASH, u=a)
        # com selo LIXO o banco grava (rastro: selo ≠ HMAC da chave) — login/verify_integrity recusam (agente B)
        s.allowed("UPDATE users SET hashed_password = :h, must_change_password = false, master_credential_seal = :s "
                  "WHERE id = :u", h=ATTACKER_HASH, s="selo-forjado-sem-a-chave", u=a)
        assert s.one("SELECT master_credential_seal FROM users WHERE id = :u", u=a) == "selo-forjado-sem-a-chave"
        s.denied("UPDATE users SET master_credential_seal = NULL WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED", u=a)
        s.denied("UPDATE users SET master_credential_seal = '' WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED", u=a)


def test_active_master_credentials_need_new_seal_and_non_credentials_do_not(ctx):
    rev, s = ctx
    a, b = s.dual_two_masters()
    if sealed(rev):  # adoção pela aplicação (selo inicial)
        s.x("UPDATE users SET master_credential_seal = :s WHERE id IN (:a, :b)", s=secrets.token_hex(32), a=a, b=b)
    s.as_app()
    attempt(s, rev, "UPDATE users SET hashed_password = :h WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED",
            h=ATTACKER_HASH, u=b)
    attempt(s, rev, "UPDATE users SET financial_password_hash = :f WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED",
            f=ATTACKER_HASH, u=b)
    # login/lockout/anti-replay/preferências não são credencial: sem selo
    s.allowed("UPDATE users SET last_login = now(), failed_login_attempts = 2, locked_until = NULL, totp_last_step = 7, "
              "theme = 'dark', totp_pending_secret_enc = NULL WHERE id = :u", u=b)
    # usuário comum: regras de sempre (sem selo)
    s.as_owner()
    comum = s.user("comum", must_change=False)
    s.as_app()
    s.allowed("UPDATE users SET hashed_password = :h, must_change_password = true WHERE id = :u", h=PWD_HASH, u=comum)


def test_definer_functions_still_work_and_application_reseals(ctx):
    """``rd_security_assign_master_seat``/``rd_security_recover_master`` rodam como dono (isentos); a aplicação
    re-sela em seguida com UPDATE só do selo — permitido ao ``rd_app``."""
    rev, s = ctx
    s.init_mode("DUAL")
    a = s.user("a")
    s.as_app()
    s.assign(a, 1, "BOOTSTRAP_ENV")  # como rd_app, pela função definer
    if sealed(rev):
        s.allowed("UPDATE users SET master_credential_seal = :s WHERE id = :u", s=secrets.token_hex(32), u=a)
    s.as_owner()
    s.ready(a)
    if sealed(rev):
        s.x("UPDATE users SET master_credential_seal = :s WHERE id = :u", s=secrets.token_hex(32), u=a)
    b = s.master_b_by_invite(a)  # ativação por convite (definer)
    s.as_app()
    if sealed(rev):
        s.allowed("UPDATE users SET master_credential_seal = :s WHERE id = :u", s=secrets.token_hex(32), u=b)
    prev = s.seat_head()
    from app.modules.security import seal as seal_mod

    sl = seal_mod.seat_seal(prev_seal=prev, seat=2, user_id=b, event="RECOVERED", source="CLI_RECOVERY",
                            invite_id=None, actor_user_id=None)
    assert s.one("SELECT rd_security_recover_master(:u, :h, NULL, :p, :s)", u=b, h=PWD_HASH, p=prev, s=sl) == 2
    if sealed(rev):
        s.allowed("UPDATE users SET master_credential_seal = :s WHERE id = :u", s=secrets.token_hex(32), u=b)
    assert s.one("SELECT rd_active_master_count()") == 1  # B recuperado: sem fatores até o novo primeiro acesso
