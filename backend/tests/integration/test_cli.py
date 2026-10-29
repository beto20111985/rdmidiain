"""CLI administrativa (AÇÃO 003: bootstrap-masters restrito, recover-master, security-verify; senhas,
catálogo, seed-dev, contas de teste)."""
import asyncio

import pytest
from sqlalchemy import select, text

from app import cli
from app.models.users import User
from tests.factories import get_user, login_as, make_user


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def masters_env(monkeypatch):
    monkeypatch.setenv("RD_MASTER1_EMAIL", "Master.Um@example.com")
    monkeypatch.setenv("RD_MASTER1_NAME", "Master Um")
    monkeypatch.setenv("RD_MASTER1_PASSWORD", "SenhaMasterUm2026")
    monkeypatch.setenv("RD_MASTER2_EMAIL", "master.dois@example.com")
    monkeypatch.setenv("RD_MASTER2_NAME", "Master Dois")
    monkeypatch.setenv("RD_MASTER2_PASSWORD", "SenhaMasterDois2026")


# ─── AÇÃO 003: bootstrap-masters restrito (automação/recuperação; ADR_011 D8) ──

async def test_bootstrap_masters_requires_initialized_instance(db, redis, masters_env):
    with pytest.raises(SystemExit) as ei:
        await cli.cmd_bootstrap_masters([1])
    assert "bootstrap-instance" in str(ei.value)
    assert (await db.execute(text("select count(*) from users"))).scalar() == 0


async def test_bootstrap_masters_seat1_only_on_virgin_instance(db, redis, masters_env, capsys):
    from tests.factories import init_approval_mode

    await init_approval_mode(db, "DUAL")
    await cli.cmd_bootstrap_masters([1])
    out = capsys.readouterr().out
    assert "SenhaMaster" not in out and "master.um@example.com" not in out and "ma***@example.com" in out
    rows = (await db.execute(text(
        "select u.email, u.admin_master_seat, u.must_change_password, u.totp_enabled, r.slug, ra.scope_type "
        "from users u join role_assignments ra on ra.user_id = u.id join roles r on r.id = ra.role_id"))).all()
    assert [tuple(r) for r in rows] == [("master.um@example.com", 1, True, False, "super_admin", "GLOBAL")]
    assert (await db.execute(text("select source from admin_master_seat_events"))).scalar() == "CLI_BOOTSTRAP"
    with pytest.raises(SystemExit):  # nunca reatribui/transfere (não é mais "idempotente por sobrescrita")
        await cli.cmd_bootstrap_masters([1])
    from app.modules.security.approval_mode import verify_integrity

    assert (await verify_integrity(db)).ok


async def test_bootstrap_masters_seat2_rules(db, redis, masters_env, mail):
    from tests.sec_helpers import active_master

    a = await active_master(db, mode="SINGLE")
    with pytest.raises(SystemExit) as ei:
        await cli.cmd_bootstrap_masters([2])
    assert "ÚNICA" in str(ei.value)
    await db.execute(text("SET session_replication_role = replica"))  # troca de modo só p/ o cenário de teste
    await db.execute(text("DELETE FROM instance_approval_mode_history"))
    await db.execute(text("DELETE FROM admin_master_seat_events"))
    await db.execute(text("SET session_replication_role = origin"))
    await db.commit()
    from tests.factories import adopt_master_seats, init_approval_mode

    await init_approval_mode(db, "DUAL")
    await adopt_master_seats(db, a)
    await cli.cmd_bootstrap_masters([2])
    row = (await db.execute(text(
        "select email, must_change_password, totp_enabled from users where admin_master_seat = 2"))).one()
    assert tuple(row) == ("master.dois@example.com", True, False)
    with pytest.raises(SystemExit):
        await cli.cmd_bootstrap_masters([2])  # ocupado
    notif = (await db.execute(text(
        "select count(*) from notifications where event_type = 'security.master_seat_assigned'"))).scalar()
    assert notif >= 1


async def test_bootstrap_masters_refuses_with_pending_invite_and_existing_email(client, db, masters_env, mail):
    from tests.factories import login_as, totp_code
    from tests.sec_helpers import active_master

    a = await active_master(db)
    s = await login_as(client, a)
    r = await client.post("/api/v1/security/masters/invites",
                          json={"full_name": "Sócio B", "email": "b@empresa.com.br", "totp_code": totp_code(a)},
                          headers=s.headers)
    assert r.status_code == 201
    with pytest.raises(SystemExit) as ei:
        await cli.cmd_bootstrap_masters([2])
    assert "convite pendente" in str(ei.value)


async def test_force_reassign_is_rejected(db, redis, masters_env):
    with pytest.raises(SystemExit) as ei:
        await cli.cmd_bootstrap_masters([1], force_reassign=True)
    assert "removido" in str(ei.value)
    with pytest.raises(SystemExit):
        cli.main(["bootstrap-masters", "--seat", "1", "--force-reassign"])


def test_bootstrap_rejects_weak_password(clean_state, masters_env, monkeypatch):
    monkeypatch.setenv("RD_MASTER1_PASSWORD", "fraca")
    with pytest.raises(Exception):
        _run(cli.cmd_bootstrap_masters([1]))


async def test_master_from_cli_must_do_first_access_on_login(client, db, masters_env):
    from tests.factories import init_approval_mode

    await init_approval_mode(db, "DUAL")
    await cli.cmd_bootstrap_masters([1])
    r = await client.post("/api/v1/auth/login", json={"login": "master.um@example.com", "password": "SenhaMasterUm2026"})
    assert r.status_code == 200 and r.json()["status"] == "mfa_setup_required"
    assert r.json()["user"]["is_admin_master"] is True
    assert r.json()["setup"]["next_step"] == "PASSWORD_CHANGE"


async def test_set_password_and_lock_password_refuse_masters(db, redis, monkeypatch):
    from tests.sec_helpers import active_master

    a = await active_master(db)
    monkeypatch.setenv("RD_NEW_PASSWORD", "DefinidaPelaCli2026")
    with pytest.raises(SystemExit) as ei:
        await cli.cmd_set_password(a.email, must_change=True)
    assert "recover-master" in str(ei.value)
    with pytest.raises(SystemExit):
        await cli.cmd_lock_password(a.email)
    fresh = await get_user(db, a.id)
    assert fresh.totp_enabled and not fresh.must_change_password


async def test_recover_master_resets_factors_and_notifies(client, db, monkeypatch, tmp_path, capsys):
    from tests.factories import adopt_master_seats
    from tests.sec_helpers import active_master, strong_password

    a = await active_master(db, name="Sócia A")
    b = await make_user(db, role="super_admin", mfa=True, admin_master_seat=2, name="Sócio B")
    await adopt_master_seats(db, b)
    sa = await login_as(client, a)
    with pytest.raises(SystemExit):
        await cli.cmd_recover_master(1, "curto")
    tmp = strong_password("Rec")
    f = tmp_path / "recovery_password"
    f.write_text(tmp, encoding="utf-8")
    monkeypatch.setenv("RD_RECOVERY_PASSWORD_FILE", str(f))
    await cli.cmd_recover_master(1, "perdeu o celular e a senha financeira")
    out = capsys.readouterr().out
    assert tmp not in out and a.email not in out and "Assento 1" in out
    u = await get_user(db, a.id)
    assert u.must_change_password and not u.totp_enabled and u.totp_secret_enc is None
    assert u.financial_password_hash is None and u.master_recovered_at is not None and u.admin_master_seat == 1
    assert (await client.get("/api/v1/me", headers=sa.headers)).status_code == 401  # sessões revogadas
    ev = (await db.execute(text("select seat, event, source from admin_master_seat_events order by id desc limit 1"))).one()
    assert tuple(ev) == (1, "RECOVERED", "CLI_RECOVERY")
    aud = (await db.execute(text(
        "select reason from audit_logs where action = 'admin_master.recovered_cli'"))).scalar()
    assert aud == "perdeu o celular e a senha financeira"
    rec = {r[0] for r in (await db.execute(text(
        "select user_id from notifications where event_type = 'security.master_recovered'"))).all()}
    assert rec == {a.id, b.id}  # os DOIS titulares
    from app.modules.security.approval_mode import verify_integrity

    assert (await verify_integrity(db)).ok
    # mesmo titular, novo primeiro acesso completo (senha temporária → troca obrigatória)
    r = await client.post("/api/v1/auth/login", json={"login": a.email, "password": tmp})
    assert r.status_code == 200 and r.json()["status"] == "mfa_setup_required"
    assert r.json()["setup"]["next_step"] == "PASSWORD_CHANGE"
    with pytest.raises(SystemExit):
        await cli.cmd_recover_master(3, "x" * 12)


async def test_security_verify_cli(db, redis, capsys):
    from tests.sec_helpers import active_master

    await active_master(db)
    assert await cli.cmd_security_verify() == 0
    assert "OK" in capsys.readouterr().out


async def test_set_password_and_lock_password(client, db, monkeypatch):
    u = await make_user(db, role="employee")
    s = await login_as(client, u)
    monkeypatch.setenv("RD_NEW_PASSWORD", "DefinidaPelaCli2026")
    await asyncio.to_thread(_run, cli.cmd_set_password(u.email, must_change=True))
    assert (await client.get("/api/v1/me", headers=s.headers)).status_code == 401  # sessões revogadas
    r = await client.post("/api/v1/auth/login", json={"login": u.email, "password": "DefinidaPelaCli2026"})
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    await asyncio.to_thread(_run, cli.cmd_lock_password(u.email))
    r = await client.post("/api/v1/auth/login", json={"login": u.email, "password": "DefinidaPelaCli2026"})
    assert r.status_code == 401
    fresh = await get_user(db, u.id)
    assert fresh.hashed_password.startswith("$argon2id$")


def test_sync_permissions_idempotent(clean_state, capsys):
    _run(cli.cmd_sync_permissions())
    out = capsys.readouterr().out
    assert "+0/-0" in out  # catálogo já sincronizado pelo harness


def test_seed_dev_without_known_passwords(clean_state, capsys):
    _run(cli.cmd_seed_dev())
    _run(cli.cmd_seed_dev())  # idempotente

    async def users():
        from app.core.database import worker_session

        async with worker_session() as db:
            return (await db.execute(select(User.email, User.must_change_password))).all()

    rows = _run(users())
    assert len(rows) == 8 and all(m for _, m in rows) and all(e.endswith("@example.com") for e, _ in rows)
    assert "senha" not in capsys.readouterr().out.lower().replace("nenhuma senha conhecida", "")


def test_test_accounts_commands_refuse_outside_development(clean_state):
    from app.core.config import settings

    assert settings.ENVIRONMENT == "test"
    with pytest.raises(SystemExit):
        _run(cli.cmd_create_test_accounts())
    with pytest.raises(SystemExit):
        _run(cli.cmd_delete_test_accounts())


def test_test_accounts_lifecycle_in_development(clean_state, monkeypatch, tmp_path):
    import json
    import os
    import stat

    from app.core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    target = tmp_path / ".devtest" / "test_accounts.json"
    monkeypatch.setattr(cli, "TEST_ACCOUNTS_FILE", target)
    _run(cli.cmd_create_test_accounts())
    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data["accounts"]) == set(cli.TEST_PROFILES)
    assert "super_admin" not in data["accounts"]
    assert data["accounts"]["admin"]["totp_secret"] and data["accounts"]["employee"]["totp_secret"] is None
    assert data["accounts"]["advertiser"]["org_ids"] == [data["organizations"]["ADV"]]
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    _run(cli.cmd_create_test_accounts())  # idempotente (regrava senhas)
    _run(cli.cmd_delete_test_accounts())
    assert not target.exists()

    async def remaining():
        from app.core.database import worker_session

        async with worker_session() as db:
            return (await db.execute(text(
                "select count(*) from users where email like 'test.midiaindoor.002+%' and is_active"))).scalar_one()

    assert _run(remaining()) == 0
