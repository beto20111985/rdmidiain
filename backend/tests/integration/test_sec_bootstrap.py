"""AÇÃO 003 — bootstrap da instância (SPEC §1, §10; ADR_011 D1/D5). PostgreSQL/Redis reais.

Banco vazio (SINGLE e DUAL), idempotência sequencial, legado ⇒ DUAL adotado, ENV alterado depois ⇒
ignorado + divergência auditada uma vez, produção sem segredo/variável/e-mail real ⇒ código 2 sem gravar,
senha em texto recusada, e-mail de conta existente recusado, saída sem segredos.
"""
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import text

from app import cli
from app.core.config import settings
from app.modules.security import bootstrap as boot
from tests.factories import make_user

DEV_EMAIL = "master.boot@rd-midiaindoor.localhost"


def _pw() -> str:
    return f"Boot{secrets.token_hex(8)}9a"


def env_for(tmp_path, *, dual="true", email=DEV_EMAIL, name="Master Boot", pw=None, write_file=True) -> dict:
    pw = pw or _pw()
    f = tmp_path / f"bootstrap_master_password_{secrets.token_hex(3)}"
    if write_file:
        f.write_text(pw + "\n", encoding="utf-8")
    env = {"RD_BOOTSTRAP_MASTER_EMAIL": email, "RD_BOOTSTRAP_MASTER_NAME": name,
           "RD_BOOTSTRAP_MASTER_PASSWORD_FILE": str(f)}
    if dual is not None:
        env["RD_BOOTSTRAP_DUAL_MASTER_APPROVAL"] = dual
    env["_pw"] = pw
    return env


def _clean(env: dict) -> dict:
    return {k: v for k, v in env.items() if not k.startswith("_")}


async def counts(db) -> dict:
    row = (await db.execute(text(
        "SELECT (SELECT count(*) FROM instance_approval_mode_history), "
        "(SELECT count(*) FROM admin_master_seat_events), "
        "(SELECT count(*) FROM users WHERE admin_master_seat = 1), "
        "(SELECT count(*) FROM users WHERE admin_master_seat IS NOT NULL), "
        "(SELECT count(*) FROM users)"))).one()
    return {"modes": row[0], "events": row[1], "seat1": row[2], "seats": row[3], "users": row[4]}


@pytest.mark.parametrize("dual,mode", [("true", "DUAL"), ("FALSE", "SINGLE")])
async def test_bootstrap_empty_db_creates_mode_and_primary_master(db, redis, tmp_path, dual, mode):
    env = env_for(tmp_path, dual=dual)
    res = await boot.bootstrap_instance(_clean(env))
    assert res.mode == mode and res.mode_version == 1 and res.mode_created and res.mode_source == "BOOTSTRAP_ENV"
    assert res.seat1_status == "criado" and res.seat1_email_masked == "ma***@rd-midiaindoor.localhost"
    out = "\n".join(res.lines())
    assert env["_pw"] not in out and DEV_EMAIL not in out
    assert await counts(db) == {"modes": 1, "events": 1, "seat1": 1, "seats": 1, "users": 1}
    u = (await db.execute(text(
        "SELECT u.email, u.must_change_password, u.totp_enabled, u.financial_password_hash IS NULL, u.is_verified, "
        "r.slug, ra.scope_type FROM users u JOIN role_assignments ra ON ra.user_id = u.id "
        "JOIN roles r ON r.id = ra.role_id"))).all()
    assert [tuple(x) for x in u] == [(DEV_EMAIL, True, False, True, True, "super_admin", "GLOBAL")]
    h = (await db.execute(text("SELECT to_mode, kind, source, env_value FROM instance_approval_mode_history"))).one()
    assert tuple(h) == (mode, "INITIALIZED", "BOOTSTRAP_ENV", dual.lower())
    ev = (await db.execute(text("SELECT seat, event, source FROM admin_master_seat_events"))).one()
    assert tuple(ev) == (1, "ASSIGNED", "BOOTSTRAP_ENV")
    acts = {r[0] for r in (await db.execute(text("SELECT action FROM audit_logs"))).all()}
    assert {"security.approval_mode_initialized", "admin_master.bootstrapped"} <= acts
    # a senha inicial NÃO está em lugar nenhum da auditoria
    dump = (await db.execute(text("SELECT coalesce(string_agg(after_data::text, ' '), '') FROM audit_logs"))).scalar()
    assert env["_pw"] not in dump
    from app.modules.security.approval_mode import verify_integrity

    assert (await verify_integrity(db)).ok


async def test_bootstrap_is_idempotent_sequential(db, redis, tmp_path):
    env = _clean(env_for(tmp_path, dual="true"))
    results = [await boot.bootstrap_instance(env) for _ in range(10)]
    assert [r.mode_created for r in results] == [True] + [False] * 9
    assert [r.seat1_status for r in results] == ["criado"] + ["existente"] * 9
    assert await counts(db) == {"modes": 1, "events": 1, "seat1": 1, "seats": 1, "users": 1}


async def test_env_changed_later_is_ignored_and_divergence_audited_once(db, redis, tmp_path, client):
    await boot.bootstrap_instance(_clean(env_for(tmp_path, dual="true")))
    changed = _clean(env_for(tmp_path, dual="false"))
    r1 = await boot.bootstrap_instance(changed)
    r2 = await boot.bootstrap_instance(changed)
    assert r1.mode == r2.mode == "DUAL" and not r1.mode_created
    assert any("IGNORADA" in w for w in r1.warnings)
    assert await counts(db) == {"modes": 1, "events": 1, "seat1": 1, "seats": 1, "users": 1}
    n = (await db.execute(text(
        "SELECT count(*) FROM audit_logs WHERE action = 'security.env_divergence_detected' "
        "AND after_data->>'kind' = 'approval_mode'"))).scalar()
    assert n == 1
    rep = await boot.read_bootstrap_env_report()
    assert rep["divergence"] is True and rep["env_value"] == "false" and rep["mode_version"] == 1
    # métrica de divergência (snapshot calculado sob demanda no scrape)
    from app.modules.websocket.metrics import render_metrics

    body = await render_metrics()
    assert "rd_security_env_divergence 1" in body and 'rd_security_approval_mode{mode="DUAL"} 1' in body
    # voltar ao valor certo limpa o relatório
    await boot.bootstrap_instance(_clean(env_for(tmp_path, dual="true")))
    assert (await boot.read_bootstrap_env_report())["divergence"] is False


async def test_unsupported_env_var_name_warns(db, redis, tmp_path):
    env = _clean(env_for(tmp_path, dual="true"))
    env["RD_DUAL_MASTER_APPROVAL"] = "false"
    res = await boot.bootstrap_instance(env)
    assert res.mode == "DUAL"
    assert any("RD_BOOTSTRAP_DUAL_MASTER_APPROVAL" in w and "RD_DUAL_MASTER_APPROVAL" in w for w in res.warnings)


async def test_legacy_installation_adopts_dual_and_seals_seats(db, redis, tmp_path):
    legacy = await make_user(db, role="super_admin", mfa=True, admin_master_seat=1, email="legado@empresa.com.br")
    res = await boot.bootstrap_instance(_clean(env_for(tmp_path, dual="false")))
    assert res.mode == "DUAL" and res.mode_source == "LEGACY_DUAL_ADOPTED" and res.legacy_adopted == [1]
    assert res.seat1_status == "existente"
    h = (await db.execute(text("SELECT to_mode, source, env_value FROM instance_approval_mode_history"))).one()
    assert tuple(h) == ("DUAL", "LEGACY_DUAL_ADOPTED", "false")
    ev = (await db.execute(text("SELECT seat, user_id, source FROM admin_master_seat_events"))).one()
    assert tuple(ev) == (1, legacy.id, "LEGACY_ADOPTED")
    assert (await db.execute(text(
        "SELECT count(*) FROM audit_logs WHERE action IN ('admin_master.legacy_adopted', "
        "'security.env_divergence_detected')"))).scalar() >= 2
    from app.modules.security.approval_mode import verify_integrity

    assert (await verify_integrity(db)).ok


async def test_existing_account_email_is_refused(db, redis, tmp_path):
    await make_user(db, role="employee", email=DEV_EMAIL)
    with pytest.raises(boot.BootstrapConfigError) as ei:
        await boot.bootstrap_instance(_clean(env_for(tmp_path)))
    assert "outra conta" in str(ei.value)
    c = await counts(db)
    assert c["modes"] == 0 and c["events"] == 0  # transação inteira desfeita


@pytest.mark.parametrize("mutate,needle", [
    (lambda e: e.pop("RD_BOOTSTRAP_DUAL_MASTER_APPROVAL"), "RD_BOOTSTRAP_DUAL_MASTER_APPROVAL"),
    (lambda e: e.update(RD_BOOTSTRAP_DUAL_MASTER_APPROVAL="talvez"), "RD_BOOTSTRAP_DUAL_MASTER_APPROVAL"),
    (lambda e: e.pop("RD_BOOTSTRAP_MASTER_EMAIL"), "RD_BOOTSTRAP_MASTER_EMAIL"),
    (lambda e: e.pop("RD_BOOTSTRAP_MASTER_PASSWORD_FILE"), "RD_BOOTSTRAP_MASTER_PASSWORD_FILE"),
    (lambda e: e.update(RD_BOOTSTRAP_MASTER_PASSWORD_FILE="/tmp/nao-existe-rd"), "RD_BOOTSTRAP_MASTER_PASSWORD_FILE"),
    (lambda e: e.update(RD_BOOTSTRAP_MASTER_PASSWORD="TextoClaro2026x"), "RD_BOOTSTRAP_MASTER_PASSWORD"),
])
async def test_config_problems_exit_2_and_write_nothing(db, redis, tmp_path, monkeypatch, capsys, mutate, needle):
    env = env_for(tmp_path)
    pw = env["_pw"]
    env = _clean(env)
    mutate(env)
    for k in [k for k in list(__import__("os").environ) if k.startswith(("RD_BOOTSTRAP_", "RD_DUAL_"))]:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    code = await cli.cmd_bootstrap_instance()
    cap = capsys.readouterr()
    assert code == 2
    assert needle in cap.err and pw not in cap.err + cap.out and "TextoClaro2026x" not in cap.err + cap.out
    assert await counts(db) == {"modes": 0, "events": 0, "seat1": 0, "seats": 0, "users": 0}


@pytest.mark.parametrize("email", ["master@example.com", "master@rd.localhost", "master@empresa.test", "x@localhost"])
async def test_production_refuses_placeholder_email(db, redis, tmp_path, monkeypatch, email):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(boot.BootstrapConfigError) as ei:
        await boot.bootstrap_instance(_clean(env_for(tmp_path, email=email)))
    assert "RD_BOOTSTRAP_MASTER_EMAIL" in str(ei.value)
    assert await counts(db) == {"modes": 0, "events": 0, "seat1": 0, "seats": 0, "users": 0}


async def test_production_without_variable_or_secret_exit_2(db, redis, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    env = _clean(env_for(tmp_path, email="master@rdmidiaindoor.com.br", write_file=False))
    with pytest.raises(boot.BootstrapConfigError) as ei:
        await boot.bootstrap_instance(env)
    assert "RD_BOOTSTRAP_MASTER_PASSWORD_FILE" in str(ei.value)
    env2 = _clean(env_for(tmp_path, email="master@rdmidiaindoor.com.br", dual=None))
    with pytest.raises(boot.BootstrapConfigError):
        await boot.bootstrap_instance(env2)
    assert await counts(db) == {"modes": 0, "events": 0, "seat1": 0, "seats": 0, "users": 0}
    # com tudo certo, produção inicializa (e-mail real, senha na política, só por arquivo)
    ok = await boot.bootstrap_instance(_clean(env_for(tmp_path, email="master@rdmidiaindoor.com.br")))
    assert ok.mode == "DUAL" and ok.seat1_status == "criado"


async def test_weak_password_in_file_is_refused(db, redis, tmp_path):
    with pytest.raises(boot.BootstrapConfigError) as ei:
        await boot.bootstrap_instance(_clean(env_for(tmp_path, pw="fraca")))
    assert "política" in str(ei.value)
    assert (await counts(db))["modes"] == 0


async def test_cli_bootstrap_instance_output_has_no_secrets(db, redis, tmp_path, monkeypatch, capsys):
    env = env_for(tmp_path, dual="true")
    for k, v in _clean(env).items():
        monkeypatch.setenv(k, v)
    assert await cli.cmd_bootstrap_instance() == 0
    assert await cli.cmd_bootstrap_instance() == 0
    cap = capsys.readouterr()
    assert "inicializado agora" in cap.out and "já inicializado" in cap.out
    assert env["_pw"] not in cap.out + cap.err and DEV_EMAIL not in cap.out + cap.err


async def test_api_startup_check_refuses_uninitialized_production(db, redis, monkeypatch):
    from app.core.startup_checks import StartupConfigError

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(StartupConfigError) as ei:
        await boot.startup_security_check()
    assert "bootstrap" in str(ei.value)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    await boot.startup_security_check()  # development/test: só WARNING
