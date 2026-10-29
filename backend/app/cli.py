"""
RD Mídia Indoor — CLI administrativa.

    python -m app.cli sync-permissions
    python -m app.cli bootstrap-instance       # serviço one-shot `bootstrap` (AÇÃO 003): modo + Master primário
    python -m app.cli bootstrap-masters --seat 1|2    # automação/recuperação restrita (sem --force-reassign)
    python -m app.cli recover-master --seat N --reason "..."   # recuperação controlada (mesmo titular)
    python -m app.cli security-verify          # integridade das trilhas seladas (0 = OK, 1 = problemas)
    python -m app.cli set-password <email> [--must-change]    # recusa titular de assento
    python -m app.cli lock-password <email>                   # recusa titular de assento
    python -m app.cli seed-dev
    python -m app.cli audit-verify
    python -m app.cli create-test-accounts     # só development; credenciais em /app/.devtest (0600)
    python -m app.cli delete-test-accounts     # só development; limpeza final
    python -m app.cli seed-demo [--purge]      # só development; DEMONSTRAÇÃO ponta a ponta (app/cli_demo.py)

Segredos NUNCA são impressos. Senhas vêm de variáveis de ambiente/arquivo ou de prompt
interativo (getpass):
- bootstrap-instance: ``RD_BOOTSTRAP_*`` (ver ``app.modules.security.bootstrap``; senha SOMENTE por arquivo).
- bootstrap-masters: ``RD_MASTER{1,2}_EMAIL``, ``RD_MASTER{1,2}_NAME``,
  ``RD_MASTER{1,2}_PASSWORD`` (ou ``..._PASSWORD_FILE``) — senha TEMPORÁRIA (troca obrigatória).
- recover-master: ``RD_RECOVERY_PASSWORD_FILE`` (ou prompt sem eco) — senha temporária.
- set-password: ``RD_NEW_PASSWORD`` (ou ``RD_NEW_PASSWORD_FILE``).

AÇÃO 003 (ADR_011): o Admin Master primário nasce pelo serviço ``bootstrap``; o 2º Master por convite
(painel). Assentos só mudam pelas funções ``SECURITY DEFINER`` do banco (eventos selados). Nenhum comando
altera o modo de aprovação depois da inicialização, cria um 3º Master ou transfere assento.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from app.core.authz import AuthContext
from app.core.config import settings


def _out(msg: str) -> None:
    print(msg, flush=True)


def _env_secret(name: str) -> Optional[str]:
    v = os.environ.get(name)
    if v:
        return v
    f = os.environ.get(f"{name}_FILE")
    if f:
        return Path(f).read_text(encoding="utf-8").rstrip("\r\n")
    return None


def _prompt(label: str, *, secret: bool = False, default: Optional[str] = None) -> str:
    if not sys.stdin.isatty():
        raise SystemExit(f"Valor ausente para '{label}' e terminal não interativo (defina a variável de ambiente).")
    if secret:
        a = getpass.getpass(f"{label}: ")
        b = getpass.getpass(f"{label} (confirmação): ")
        if a != b:
            raise SystemExit("As senhas não conferem.")
        return a
    v = input(f"{label}{f' [{default}]' if default else ''}: ").strip()
    return v or (default or "")


def _mask(email: str) -> str:
    from app.core.email import mask_email

    return mask_email(email)


def _org_values(business_role: str) -> dict:
    """Valores de tipo de organização para o schema legado (bitmask ``type``) e o novo (``business_roles``)."""
    return {
        "business_roles": [business_role],
        "type": {"ADVERTISER": 1, "PARTNER": 2 | 4, "AGENCY": 1}.get(business_role, 0),
    }


def _geo_values(uf: str, city: str) -> dict:
    """Colunas geográficas derivadas (presentes no schema novo de establishments)."""
    from app.core.authz import city_key

    regions = {
        "NORTE": "AC AP AM PA RO RR TO", "NORDESTE": "AL BA CE MA PB PE PI RN SE",
        "CENTRO_OESTE": "DF GO MT MS", "SUDESTE": "ES MG RJ SP", "SUL": "PR RS SC",
    }
    region = next(r for r, ufs in regions.items() if uf in ufs.split())
    return {"city_key": city_key(uf, city), "region": region}


async def _reflect(db, name: str):
    """Tabela refletida do BANCO (não do model): os comandos de CLI funcionam mesmo quando
    os models de domínio estão à frente do schema aplicado (migrations pendentes)."""
    from sqlalchemy import MetaData, Table

    return await db.run_sync(lambda s: Table(name, MetaData(), autoload_with=s.connection()))


async def _ensure_row(db, table: str, match: dict, values: dict) -> uuid.UUID:
    from sqlalchemy import and_, insert, update

    from app.core.timeutils import utcnow

    t = await _reflect(db, table)
    cols = set(t.c.keys())
    cond = and_(*[t.c[k] == v for k, v in match.items()])
    row = (await db.execute(select(t.c.id).where(cond))).first()
    if row is not None:
        if "is_active" in cols:
            await db.execute(update(t).where(t.c.id == row[0]).values(is_active=True))
        return row[0]
    now = utcnow()
    vals = {k: v for k, v in {"id": uuid.uuid4(), "created_at": now, "updated_at": now, **match, **values}.items()
            if k in cols}
    missing = [c.name for c in t.c if not c.nullable and c.server_default is None and c.name not in vals]
    if missing:
        raise SystemExit(f"Schema atual de '{table}' exige colunas sem valor conhecido: {missing}")
    await db.execute(insert(t).values(**vals))
    return vals["id"]


CLI_CTX = AuthContext.system(origin="cli")


async def cmd_sync_permissions() -> None:
    from app.core.database import worker_session
    from app.core.rbac_sync import sync_catalog

    from app.core.audit import audit

    async with worker_session() as db:
        rep = await sync_catalog(db)
        if rep.permissions_created or rep.permissions_removed or rep.roles_created or rep.roles_updated:
            await audit(db, CLI_CTX, "rbac.catalog_synced", "rbac_catalog", None, after={
                "permissions_created": rep.permissions_created, "permissions_removed": rep.permissions_removed,
                "roles_created": rep.roles_created, "roles_updated": rep.roles_updated,
                "links_added": rep.links_added, "links_removed": rep.links_removed,
            })
    _out(f"Catálogo sincronizado: {rep.summary()}")


async def _ensure_assignment(db, user_id: uuid.UUID, slug: str, scope_type: str = "GLOBAL",
                             scope_id=None, scope_value=None) -> bool:
    from app.models.rbac import Role, RoleAssignment

    role = (await db.execute(select(Role).where(Role.slug == slug))).scalar_one()
    exists = (
        await db.execute(
            select(RoleAssignment.id).where(
                RoleAssignment.user_id == user_id, RoleAssignment.role_id == role.id,
                RoleAssignment.scope_type == scope_type,
                RoleAssignment.scope_id.is_not_distinct_from(scope_id),
                RoleAssignment.scope_value.is_not_distinct_from(scope_value),
            )
        )
    ).first()
    if exists:
        return False
    db.add(RoleAssignment(user_id=user_id, role_id=role.id, scope_type=scope_type, scope_id=scope_id,
                          scope_value=scope_value))
    return True


def _cli_master_guard_message() -> str:
    return "Recusado: titular de assento de Admin Master. Use 'python -m app.cli recover-master --seat N --reason ...'."


async def cmd_bootstrap_masters(seats: list[int], force_reassign: bool = False) -> None:
    """Automação/headless RESTRITA (AÇÃO 003 — ADR_011 D8). Nunca altera o modo nem transfere assento.

    - assento 1: só se NUNCA houve evento de assento e não há titular (modo já inicializado);
    - assento 2: modo DUAL, assento 1 ocupado, assento 2 vago, sem convite pendente; e-mail novo.
    O Master nasce com senha TEMPORÁRIA (``must_change_password=true``), sem TOTP nem senha financeira.
    """
    if force_reassign:
        raise SystemExit("--force-reassign foi removido (AÇÃO 003): assento de Admin Master nunca é transferido. "
                         "Para recuperar acesso do mesmo titular use recover-master.")
    from app.core.audit import audit
    from app.core.database import worker_session
    from app.core.passwords import hash_password, validate_password_policy
    from app.core.rbac_sync import sync_catalog
    from app.core.timeutils import utcnow
    from app.models.security import AdminMasterInvite, AdminMasterSeatEvent
    from app.models.users import User
    from app.modules.auth import service as svc
    from app.modules.notifications.service import drain
    from app.modules.security import approval_mode as am
    from app.modules.security import ddl, masters
    from app.modules.security.notifications import notify_seat_assigned_cli, status_changed_for_masters

    for seat in seats:
        email = os.environ.get(f"RD_MASTER{seat}_EMAIL") or _prompt(f"E-mail do Admin Master {seat}")
        email = svc.normalize_email(email)
        if not svc._EMAIL_RE.match(email):
            raise SystemExit("E-mail inválido.")
        name = os.environ.get(f"RD_MASTER{seat}_NAME") or _prompt(f"Nome do Admin Master {seat}",
                                                                   default=f"Admin Master {seat}")
        password = _env_secret(f"RD_MASTER{seat}_PASSWORD") or _prompt(f"Senha TEMPORÁRIA do Admin Master {seat}",
                                                                       secret=True)
        validate_password_policy(password, email)

        async with worker_session() as db:
            from sqlalchemy import text as _text

            await db.execute(_text("SELECT pg_advisory_xact_lock(:k)"), {"k": ddl.LOCK_BOOTSTRAP})
            await sync_catalog(db)
            state = await am.current_state(db)
            if state is None:
                raise SystemExit("Instância não inicializada: execute 'python -m app.cli bootstrap-instance' "
                                 "(serviço bootstrap) primeiro.")
            holders = await masters.seat_holders(db)
            any_event = (await db.execute(select(AdminMasterSeatEvent.id).limit(1))).first() is not None
            if seat == 1:
                if 1 in holders or any_event:
                    raise SystemExit("O assento 1 já foi atribuído nesta instância. Assento nunca é transferido; "
                                     "para recuperar acesso do titular use recover-master.")
            else:
                if state.mode != "DUAL":
                    raise SystemExit("Modo de aprovação ÚNICA: não existe segundo Admin Master nesta instância.")
                if 1 not in holders:
                    raise SystemExit("O assento 2 exige o assento 1 ocupado.")
                if 2 in holders:
                    raise SystemExit("O assento 2 já está ocupado. Para recuperar acesso do titular use recover-master.")
                pending = (await db.execute(
                    select(AdminMasterInvite.id).where(AdminMasterInvite.status == "PENDING",
                                                       AdminMasterInvite.expires_at > utcnow()))).first()
                if pending is not None:
                    raise SystemExit("Existe convite pendente para o 2º Admin Master (painel). Revogue-o antes.")
            if (await db.execute(select(User.id).where(User.email == email))).first() is not None:
                raise SystemExit("Este e-mail já pertence a uma conta existente; use outro e-mail.")
            target = User(id=uuid.uuid4(), email=email, full_name=name, hashed_password=hash_password(password),
                          is_active=True, is_verified=True, password_changed_at=utcnow(), must_change_password=True)
            db.add(target)
            await db.flush()
            await masters.assign_seat(db, user_id=target.id, seat=seat, source="CLI_BOOTSTRAP")
            await audit(db, CLI_CTX, "admin_master.bootstrapped", "user", target.id,
                        after={"seat": seat, "source": "CLI_BOOTSTRAP", "email": _mask(email)})
            await notify_seat_assigned_cli(db, seat=seat, name=name, user_id=target.id)
            await status_changed_for_masters(db, reason="master_seat_assigned")
        await drain()
        _out(f"Assento {seat}: {_mask(email)} criado com senha TEMPORÁRIA. No primeiro acesso o titular troca a "
             "senha e configura TOTP e senha financeira.")


async def cmd_bootstrap_instance() -> int:
    """Serviço one-shot ``bootstrap`` (SPEC §10). Retorna o código de saída (0 ok; 2 configuração; 1 inesperado)."""
    from app.core.errors import DomainError
    from app.modules.security.bootstrap import BootstrapConfigError, bootstrap_instance

    try:
        result = await bootstrap_instance()
    except BootstrapConfigError as exc:
        print("ERRO de configuração do bootstrap (nada foi gravado):", file=sys.stderr, flush=True)
        for prob in exc.problems:
            print(f"  - {prob}", file=sys.stderr, flush=True)
        return 2
    except DomainError as exc:
        print(f"ERRO: {exc.code}: {exc.message}", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # nunca imprimir valores (podem conter segredos): só o tipo
        print(f"ERRO inesperado no bootstrap ({type(exc).__name__}). Nada foi gravado.", file=sys.stderr, flush=True)
        return 1
    for line in result.lines():
        _out(line)
    return 0


def _file_or_prompt_password(env_file_var: str, label: str) -> str:
    path = os.environ.get(env_file_var)
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            raise SystemExit(f"{env_file_var}: arquivo inexistente ou ilegível.") from None
    return _prompt(label, secret=True)


async def cmd_recover_master(seat: int, reason: str) -> None:
    """Recuperação controlada do titular do assento (ADR_011 D8): zera TOTP/códigos/senha financeira, senha
    temporária com troca obrigatória, evento selado RECOVERED, sessões revogadas, auditoria e notificação a
    TODOS os titulares. Nunca troca o titular."""
    from app.core.audit import audit
    from app.core.database import worker_session
    from app.core.passwords import hash_password, validate_password_policy
    from app.models.users import User
    from app.modules.auth import service as svc
    from app.modules.notifications.service import drain
    from app.modules.security import masters
    from app.modules.security.notifications import notify_master_recovered, status_changed_for_masters

    reason = (reason or "").strip()
    if len(reason) < 10:
        raise SystemExit("Informe --reason com pelo menos 10 caracteres.")
    if seat not in (1, 2):
        raise SystemExit("Assento inválido (1 ou 2).")
    async with worker_session() as db:
        holder = (await db.execute(select(User).where(User.admin_master_seat == seat))).scalar_one_or_none()
        if holder is None:
            raise SystemExit(f"O assento {seat} está vago: nada a recuperar.")
        email = holder.email
    password = _file_or_prompt_password("RD_RECOVERY_PASSWORD_FILE", f"Senha TEMPORÁRIA do assento {seat}")
    validate_password_policy(password, email)
    revoked: list = []
    async with worker_session() as db:
        holder = (await db.execute(select(User).where(User.admin_master_seat == seat))).scalar_one_or_none()
        if holder is None or holder.email != email:
            raise SystemExit("O titular do assento mudou durante a operação; execute novamente.")
        await masters.record_recovery(db, user_id=holder.id, new_password_hash=hash_password(password))
        revoked = await svc.revoke_all_user_sessions(db, holder.id, "master_recovered")
        await audit(db, CLI_CTX, "admin_master.recovered_cli", "user", holder.id, reason=reason,
                    after={"seat": seat, "sessions_revoked": len(revoked)})
        await notify_master_recovered(db, seat=seat, reason=reason)
        await status_changed_for_masters(db, reason="master_recovered")
    await svc.after_commit_revocations(revoked)
    await drain()
    _out(f"Assento {seat} ({_mask(email)}): credenciais recuperadas. No próximo acesso o titular troca a senha "
         "temporária e configura de novo TOTP e senha financeira; operações financeiras desse Master ficam "
         "bloqueadas temporariamente. Os titulares foram notificados.")


async def cmd_security_verify() -> int:
    """Integridade das trilhas seladas (modo + assentos) e titulares. 0 = OK; 1 = problemas."""
    from app.core.database import worker_session
    from app.modules.security import approval_mode as am
    from app.modules.security import metrics as sec_metrics

    async with worker_session() as db:
        report = await am.verify_integrity(db)
        state = await am.current_state(db)
        snap = await sec_metrics.compute_snapshot(db, integrity=report)
    await sec_metrics.store_snapshot(snap)
    mode = f"{state.mode} (versão {state.version})" if state else "NÃO INICIALIZADO"
    if report.ok:
        _out(f"Integridade da segurança: OK — modo {mode}; Masters ativos: {snap['active_masters']}.")
        return 0
    _out(f"Integridade da segurança: FALHOU — modo {mode}. Problemas:")
    for prob in report.problems:
        _out(f"  - {prob}")
    return 1


async def cmd_set_password(email: str, must_change: bool) -> None:
    from app.core.audit import audit
    from app.core.database import worker_session
    from app.core.passwords import validate_password_policy
    from app.models.users import User
    from app.modules.auth import service as svc

    email = svc.normalize_email(email)
    async with worker_session() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is not None and user.admin_master_seat is not None:
            raise SystemExit(_cli_master_guard_message())
    password = _env_secret("RD_NEW_PASSWORD") or _prompt("Nova senha", secret=True)
    validate_password_policy(password, email)
    async with worker_session() as db:
        user = (await db.execute(select(User).where(User.email == email).with_for_update())).scalar_one_or_none()
        if user is None:
            raise SystemExit("Usuário não encontrado.")
        if user.admin_master_seat is not None:
            raise SystemExit(_cli_master_guard_message())
        await svc.apply_password(db, user, password)
        user.must_change_password = must_change
        user.failed_login_attempts = 0
        user.locked_until = None
        revoked = await svc.revoke_all_user_sessions(db, user.id, "cli_set_password")
        await audit(db, CLI_CTX, "user.password_set_cli", "user", user.id,
                    after={"must_change_password": must_change, "sessions_revoked": len(revoked)})
    await svc.after_commit_revocations(revoked)
    _out(f"Senha definida para {_mask(email)} (sessões anteriores encerradas).")


async def cmd_lock_password(email: str) -> None:
    from app.core.audit import audit
    from app.core.database import worker_session
    from app.core.passwords import random_unusable_hash
    from app.models.users import User
    from app.modules.auth import service as svc

    email = svc.normalize_email(email)
    async with worker_session() as db:
        user = (await db.execute(select(User).where(User.email == email).with_for_update())).scalar_one_or_none()
        if user is None:
            raise SystemExit("Usuário não encontrado.")
        if user.admin_master_seat is not None:
            raise SystemExit(_cli_master_guard_message())
        user.hashed_password = random_unusable_hash()
        user.must_change_password = True
        revoked = await svc.revoke_all_user_sessions(db, user.id, "cli_lock_password")
        await audit(db, CLI_CTX, "user.password_invalidated_cli", "user", user.id,
                    after={"sessions_revoked": len(revoked)})
    await svc.after_commit_revocations(revoked)
    _out(f"Senha de {_mask(email)} invalidada (hash aleatório; ninguém a conhece). "
         "Use set-password para definir uma nova.")


DEMO_DOMAIN = "example.com"


async def cmd_seed_dev() -> None:
    """Dados de DEMONSTRAÇÃO (prefixo DEMO). Usuários sem senha conhecida (must_change_password)."""
    if settings.is_production:
        raise SystemExit("seed-dev é proibido em produção.")
    from app.core.audit import audit
    from app.core.authz import city_key
    from app.core.database import worker_session
    from app.core.passwords import random_unusable_hash
    from app.core.rbac_sync import sync_catalog
    from app.models.users import User

    async with worker_session() as db:
        await sync_catalog(db)

        async def org(name: str, doc: str, business_role: str) -> uuid.UUID:
            return await _ensure_row(db, "organizations", {"document": doc},
                                     {"name": name, "document_type": "CNPJ", "is_active": True,
                                      **_org_values(business_role)})

        mercado_id = await org("DEMO — Mercado Exemplo", "DEMO-000000001", "PARTNER")
        anunciante_id = await org("DEMO — Anunciante Exemplo", "DEMO-000000002", "ADVERTISER")
        await _ensure_row(db, "establishments", {"org_id": mercado_id, "name": "DEMO — Mercado Exemplo (Centro)"},
                          {"city": "Goiânia", "state": "GO", "country": "BRA", "timezone": "America/Sao_Paulo",
                           "is_active": True, **_geo_values("GO", "Goiânia")})

        people = [
            ("demo.admin", "DEMO Admin", "admin", "GLOBAL", None, None),
            ("demo.funcionario", "DEMO Funcionário", "employee", "GLOBAL", None, None),
            ("demo.financeiro", "DEMO Financeiro", "financial", "GLOBAL", None, None),
            ("demo.designer", "DEMO Designer", "designer", "GLOBAL", None, None),
            ("demo.comercial", "DEMO Comercial", "sales", "CITY", None, city_key("GO", "Goiânia")),
            ("demo.tecnico", "DEMO Técnico", "technician", "CITY", None, city_key("GO", "Goiânia")),
            ("demo.estabelecimento", "DEMO Estabelecimento", "establishment_owner", "ORGANIZATION", mercado_id, None),
            ("demo.anunciante", "DEMO Anunciante", "advertiser", "ORGANIZATION", anunciante_id, None),
        ]
        created = 0
        for local, name, role, stype, sid, sval in people:
            email = f"{local}@{DEMO_DOMAIN}"
            u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if u is None:
                u = User(id=uuid.uuid4(), email=email, full_name=name, hashed_password=random_unusable_hash(),
                         is_active=True, is_verified=True, must_change_password=True)
                db.add(u)
                await db.flush()
                created += 1
            await _ensure_assignment(db, u.id, role, stype, sid, sval)
        await audit(db, CLI_CTX, "seed.dev", "seed", "demo", after={"users_created": created})
    _out(f"Dados DEMO garantidos ({created} usuário(s) novo(s), e-mails @{DEMO_DOMAIN}). Nenhuma senha conhecida: "
         "use 'python -m app.cli set-password <email>' para definir uma.")


TEST_PREFIX = "TEST_MIDIAINDOOR_002_"
TEST_EMAIL_FMT = "test.midiaindoor.002+{profile}@example.com"
TEST_EMAIL_LIKE = "test.midiaindoor.002+%@example.com"
TEST_ACCOUNTS_FILE = Path(os.environ.get("RD_DEVTEST_DIR", "/app/.devtest")) / "test_accounts.json"
# perfil → (escopo, org de teste, MFA obrigatório). super_admin NÃO (Admin Master só pelo bootstrap/convite — AÇÃO 003).
TEST_PROFILES: dict[str, tuple[str, Optional[str], bool]] = {
    "admin": ("GLOBAL", None, True),
    "financial": ("GLOBAL", None, True),
    "employee": ("GLOBAL", None, False),
    "designer": ("GLOBAL", None, False),
    "sales": ("GLOBAL", None, False),
    "technician": ("GLOBAL", None, False),
    "establishment_owner": ("ORGANIZATION", "ESTAB", False),
    "advertiser": ("ORGANIZATION", "ADV", False),
    "agency": ("ORGANIZATION", "ADV", False),
}


def _require_development() -> None:
    if settings.ENVIRONMENT != "development":
        raise SystemExit(f"Recusado: este comando só roda com ENVIRONMENT=development (atual: {settings.ENVIRONMENT}).")


def _write_secret_file(path: Path, data: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


async def cmd_create_test_accounts() -> None:
    """Contas de TESTE identificáveis (uma por perfil) — somente development.

    Senhas e segredos TOTP são gerados aqui e gravados SOMENTE em
    ``/app/.devtest/test_accounts.json`` (0600). Nada sensível vai para stdout/log.
    """
    _require_development()
    import secrets as _secrets

    from app.core import totp as _totp
    from app.core.audit import audit
    from app.core.database import worker_session
    from app.core.passwords import hash_password, validate_password_policy
    from app.core.rbac_sync import sync_catalog
    from app.core.timeutils import utcnow
    from app.models.rbac import RoleAssignment
    from app.models.users import User
    from app.modules.auth import service as svc

    out: dict = {"_aviso": "Contas de TESTE da AÇÃO 002 (development). Não compartilhar. Remover com delete-test-accounts.",
                 "accounts": {}}
    revoked_all: list = []
    async with worker_session() as db:
        await sync_catalog(db)

        async def test_org(key: str, business_role: str) -> uuid.UUID:
            return await _ensure_row(db, "organizations", {"name": f"{TEST_PREFIX}ORG_{key}"},
                                     {"document": f"TEST002-{key}"[:18], "document_type": "CNPJ", "is_active": True,
                                      **_org_values(business_role)})

        orgs = {
            "ESTAB": await test_org("ESTAB", "PARTNER"),
            "ADV": await test_org("ADV", "ADVERTISER"),
        }
        est_id = await _ensure_row(db, "establishments", {"name": f"{TEST_PREFIX}ESTABELECIMENTO_1"},
                                   {"org_id": orgs["ESTAB"], "city": "Goiânia", "state": "GO", "country": "BRA",
                                    "timezone": "America/Sao_Paulo", "is_active": True,
                                    **_geo_values("GO", "Goiânia")})
        out["organizations"] = {k: str(v) for k, v in orgs.items()}
        out["establishment_ids"] = [str(est_id)]

        now = utcnow()
        for profile, (stype, org_key, mfa) in TEST_PROFILES.items():
            email = TEST_EMAIL_FMT.format(profile=profile)
            password = f"Tst{_secrets.token_urlsafe(18)}9a"
            validate_password_policy(password, email)
            u = (await db.execute(select(User).where(User.email == email).with_for_update())).scalar_one_or_none()
            if u is None:
                u = User(id=uuid.uuid4(), email=email, full_name=f"{TEST_PREFIX}{profile}",
                         hashed_password=hash_password(password), is_active=True, is_verified=True)
                db.add(u)
                await db.flush()
            u.full_name = f"{TEST_PREFIX}{profile}"
            u.hashed_password = hash_password(password)
            u.password_changed_at = now
            u.is_active = True
            u.anonymized_at = None
            u.must_change_password = False
            u.failed_login_attempts = 0
            u.locked_until = None
            u.admin_master_seat = None
            secret = None
            if mfa:
                secret = _totp.new_secret()
                u.totp_secret_enc = svc.encrypt_totp_secret(u.id, secret)
                u.totp_pending_secret_enc = None
                u.totp_enabled = True
                u.totp_enabled_at = now
                u.totp_last_step = None
            else:
                u.totp_enabled = False
                u.totp_secret_enc = None
                u.totp_pending_secret_enc = None
            # atribuições: exatamente a do perfil de teste
            from sqlalchemy import delete as _delete

            await db.execute(_delete(RoleAssignment).where(RoleAssignment.user_id == u.id))
            org_id = orgs[org_key] if org_key else None
            await _ensure_assignment(db, u.id, profile, stype, org_id, None)
            revoked_all += await svc.revoke_all_user_sessions(db, u.id, "test_accounts_reset")
            out["accounts"][profile] = {
                "email": email,
                "password": password,
                "totp_secret": secret,
                "org_ids": [str(org_id)] if org_id else [],
                "scope_type": stype,
            }
        await audit(db, CLI_CTX, "test_accounts.created", "test_accounts", "AÇÃO 002",
                    after={"profiles": sorted(TEST_PROFILES), "organizations": out["organizations"]})
    await svc.after_commit_revocations(revoked_all)
    _write_secret_file(TEST_ACCOUNTS_FILE, out)
    _out(f"Contas de teste garantidas: {len(TEST_PROFILES)} perfis ({', '.join(TEST_PROFILES)}). "
         f"Credenciais gravadas em {TEST_ACCOUNTS_FILE} (0600). Nenhum segredo foi impresso.")


async def cmd_delete_test_accounts() -> None:
    """Remove as contas/orgs de teste. O que tem referência obrigatória é ANONIMIZADO/desativado."""
    _require_development()
    from sqlalchemy import delete as _delete
    from sqlalchemy import or_ as _or
    from sqlalchemy.exc import DBAPIError

    from app.core.audit import audit
    from app.core.database import worker_session
    from app.models.users import User
    from app.modules.auth import service as svc

    deleted_users = anonymized_users = deleted_orgs = deactivated_orgs = 0
    revoked_all: list = []
    async with worker_session() as db:
        users = (
            await db.execute(
                select(User).where(_or(User.email.like(TEST_EMAIL_LIKE), User.full_name.like(f"{TEST_PREFIX}%")))
            )
        ).scalars().all()
        for u in users:
            try:
                async with db.begin_nested():
                    await db.execute(_delete(User).where(User.id == u.id))
                deleted_users += 1
            except DBAPIError:
                # referenciado por auditoria/histórico append-only (FK ou trigger RD_APPEND_ONLY que recusa o
                # ON DELETE SET NULL) ou por outros registros → anonimizar em vez de apagar
                from app.api.v1.endpoints.lgpd import anonymize_user

                fresh = await db.get(User, u.id)
                revoked_all += await anonymize_user(db, fresh)
                fresh.full_name = f"{TEST_PREFIX}removido"
                anonymized_users += 1
        from sqlalchemy import update as _update

        org_t = await _reflect(db, "organizations")
        est_t = await _reflect(db, "establishments")
        org_ids = [r[0] for r in (await db.execute(select(org_t.c.id).where(org_t.c.name.like(f"{TEST_PREFIX}%")))).all()]
        for oid in org_ids:
            try:
                async with db.begin_nested():
                    await db.execute(_delete(est_t).where(est_t.c.org_id == oid))
                    await db.execute(_delete(org_t).where(org_t.c.id == oid))
                deleted_orgs += 1
            except DBAPIError:  # referências obrigatórias/append-only → só desativa
                await db.execute(_update(org_t).where(org_t.c.id == oid).values(is_active=False))
                deactivated_orgs += 1
        await audit(db, CLI_CTX, "test_accounts.deleted", "test_accounts", "AÇÃO 002",
                    after={"deleted_users": deleted_users, "anonymized_users": anonymized_users,
                           "deleted_orgs": deleted_orgs, "deactivated_orgs": deactivated_orgs})
    await svc.after_commit_revocations(revoked_all)
    try:
        TEST_ACCOUNTS_FILE.unlink()
    except FileNotFoundError:
        pass
    _out(f"Contas de teste: {deleted_users} removida(s), {anonymized_users} anonimizada(s) (referenciadas pela "
         f"auditoria append-only); organizações: {deleted_orgs} removida(s), {deactivated_orgs} desativada(s). "
         "Registros de auditoria permanecem (append-only), vinculados aos ids anonimizados.")


async def cmd_audit_verify() -> None:
    from app.core.audit import verify_chain
    from app.core.database import worker_session

    async with worker_session() as db:
        rep = await verify_chain(db)
    _out(f"Cadeia de auditoria: {'OK' if rep.ok else 'VIOLADA'} — linhas verificadas: {rep.rows_checked}"
         + ("" if rep.ok else f"; primeira divergência seq={rep.first_bad_seq}: {rep.problem}"))
    if not rep.ok:
        raise SystemExit(2)


def cmd_bootstrap_masters_reject_force() -> None:
    raise SystemExit("--force-reassign foi removido (AÇÃO 003): assento de Admin Master nunca é transferido. "
                     "Para recuperar acesso do mesmo titular use recover-master.")


def main(argv: Optional[list[str]] = None) -> None:
    from app.core.errors import DomainError

    p = argparse.ArgumentParser(prog="python -m app.cli", description="RD Mídia Indoor — CLI administrativa")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-permissions")
    sub.add_parser("bootstrap-instance")
    b = sub.add_parser("bootstrap-masters")
    b.add_argument("--seat", type=int, choices=[1, 2], required=True)
    b.add_argument("--force-reassign", action="store_true", help=argparse.SUPPRESS)  # removido: recusado
    rm = sub.add_parser("recover-master")
    rm.add_argument("--seat", type=int, choices=[1, 2], required=True)
    rm.add_argument("--reason", required=True)
    sub.add_parser("security-verify")
    sp = sub.add_parser("set-password")
    sp.add_argument("email")
    sp.add_argument("--must-change", action="store_true")
    lp = sub.add_parser("lock-password")
    lp.add_argument("email")
    sub.add_parser("seed-dev")
    sub.add_parser("audit-verify")
    sub.add_parser("create-test-accounts")
    sub.add_parser("delete-test-accounts")
    sd = sub.add_parser("seed-demo")  # W-DB (AÇÃO 002): app/cli_demo.py
    sd.add_argument("--purge", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.cmd == "sync-permissions":
            asyncio.run(cmd_sync_permissions())
        elif a.cmd == "bootstrap-instance":
            raise SystemExit(asyncio.run(cmd_bootstrap_instance()))
        elif a.cmd == "bootstrap-masters":
            if a.force_reassign:
                cmd_bootstrap_masters_reject_force()
            asyncio.run(cmd_bootstrap_masters([a.seat]))
        elif a.cmd == "recover-master":
            asyncio.run(cmd_recover_master(a.seat, a.reason))
        elif a.cmd == "security-verify":
            raise SystemExit(asyncio.run(cmd_security_verify()))
        elif a.cmd == "set-password":
            asyncio.run(cmd_set_password(a.email, a.must_change))
        elif a.cmd == "lock-password":
            asyncio.run(cmd_lock_password(a.email))
        elif a.cmd == "seed-dev":
            asyncio.run(cmd_seed_dev())
        elif a.cmd == "audit-verify":
            asyncio.run(cmd_audit_verify())
        elif a.cmd == "create-test-accounts":
            asyncio.run(cmd_create_test_accounts())
        elif a.cmd == "delete-test-accounts":
            asyncio.run(cmd_delete_test_accounts())
        elif a.cmd == "seed-demo":
            from app import cli_demo

            asyncio.run(cli_demo.cmd_seed_demo_purge() if a.purge else cli_demo.cmd_seed_demo())
    except DomainError as exc:
        raise SystemExit(f"Erro: {exc.message}") from None


if __name__ == "__main__":
    main()
