"""
RD Mídia Indoor — Bootstrap da instância (ADR_011 D1/D5; SPEC §1, §10) e verificação de startup da API.

``bootstrap_instance()`` — executado pelo serviço one-shot ``bootstrap`` (``python -m app.cli
bootstrap-instance``), após ``migrate`` e antes de ``backend/worker/worker-beat``:

1. Lê e valida o ENV (somente aqui; ``app.core.config`` não conhece estas variáveis):
   ``RD_BOOTSTRAP_DUAL_MASTER_APPROVAL`` (true|false), ``RD_BOOTSTRAP_MASTER_EMAIL``,
   ``RD_BOOTSTRAP_MASTER_NAME`` (opcional), ``RD_BOOTSTRAP_MASTER_PASSWORD_FILE`` (SOMENTE arquivo).
   ``RD_BOOTSTRAP_MASTER_PASSWORD`` em texto ⇒ recusado. ``RD_DUAL_MASTER_APPROVAL`` ⇒ aviso (não lido).
2. Transação única sob ``pg_advisory_xact_lock(LOCK_BOOTSTRAP)``: inicializa o modo (só se a trilha estiver
   vazia; legado ⇒ DUAL adotado, ENV ignorado), adota assentos legados, cria o Master primário (assento 1)
   com ``must_change_password=true`` (sem TOTP/senha financeira) se o assento 1 nunca foi atribuído.
3. Divergências (ENV × modo efetivo; e-mail do ENV × titular) ⇒ WARNING + auditoria 1× — nunca alteram nada.

Idempotente (1 ou 10 execuções, em sequência ou concorrentes ⇒ 1 modo, 1 assento 1, 1 evento).
Erro de configuração ⇒ ``BootstrapConfigError`` (CLI sai com 2, nada gravado). Nunca imprime senha; e-mail
só mascarado.

``startup_security_check()`` — lifespan da API (não lê segredos): produção/staging recusam subir com a
instância não inicializada ou assento 1 vago; integridade falha ⇒ log CRITICAL (não impede subir).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings, smtp_from_is_placeholder

log = structlog.get_logger("rd.security.bootstrap")

ENV_DUAL = "RD_BOOTSTRAP_DUAL_MASTER_APPROVAL"
ENV_EMAIL = "RD_BOOTSTRAP_MASTER_EMAIL"
ENV_NAME = "RD_BOOTSTRAP_MASTER_NAME"
ENV_PASSWORD_FILE = "RD_BOOTSTRAP_MASTER_PASSWORD_FILE"
ENV_PASSWORD_PLAIN = "RD_BOOTSTRAP_MASTER_PASSWORD"
ENV_UNSUPPORTED = "RD_DUAL_MASTER_APPROVAL"
DEFAULT_NAME = "Administrador Master"
STRICT_ENVIRONMENTS = ("production", "staging")


class BootstrapConfigError(Exception):
    """Problema de configuração (mensagem só com NOMES de variáveis). CLI: código de saída 2."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass
class BootstrapEnv:
    dual_raw: Optional[str] = None  # valor normalizado ('true'/'false') ou None
    dual: Optional[bool] = None
    email: Optional[str] = None
    name: str = DEFAULT_NAME
    password_file: Optional[str] = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __repr__(self) -> str:  # nunca vazar caminhos/valores em tracebacks
        return f"BootstrapEnv(dual={self.dual!r}, problems={len(self.problems)}, warnings={len(self.warnings)})"


def strict_environment() -> bool:
    return settings.ENVIRONMENT in STRICT_ENVIRONMENTS


def normalize_dual(raw: Optional[str]) -> tuple[Optional[str], Optional[bool], bool]:
    """``(normalizado, booleano, válido)``. Ausente/vazio ⇒ ``(None, None, True)``."""
    if raw is None or not str(raw).strip():
        return None, None, True
    v = str(raw).strip().lower()
    if v == "true":
        return "true", True, True
    if v == "false":
        return "false", False, True
    return None, None, False


def read_env(environ: Optional[Mapping[str, str]] = None) -> BootstrapEnv:
    env = os.environ if environ is None else environ
    cfg = BootstrapEnv()
    if env.get(ENV_PASSWORD_PLAIN):
        cfg.problems.append(f"{ENV_PASSWORD_PLAIN} (texto) não é aceito: use {ENV_PASSWORD_FILE} (arquivo/segredo)")
    if ENV_UNSUPPORTED in env:
        cfg.warnings.append(f"{ENV_UNSUPPORTED} não é lida por este sistema: o nome correto é {ENV_DUAL} "
                            "(e só vale na primeira inicialização)")
    cfg.dual_raw, cfg.dual, ok = normalize_dual(env.get(ENV_DUAL))
    if not ok:
        cfg.problems.append(f"{ENV_DUAL} deve ser 'true' ou 'false'")
    email = (env.get(ENV_EMAIL) or "").strip().lower()
    cfg.email = email or None
    cfg.name = (env.get(ENV_NAME) or "").strip() or DEFAULT_NAME
    cfg.password_file = (env.get(ENV_PASSWORD_FILE) or "").strip() or None
    return cfg


def _master_problems(cfg: BootstrapEnv) -> tuple[list[str], Optional[str]]:
    """Valida e-mail/nome/arquivo/política para criar o assento 1. Devolve (problemas, senha)."""
    from app.core.passwords import password_policy_errors
    from app.modules.auth import service as svc

    problems: list[str] = []
    if not cfg.email:
        problems.append(f"{ENV_EMAIL} obrigatório para criar o Administrador Master primário")
    elif not svc._EMAIL_RE.match(cfg.email) or len(cfg.email) > 255:
        problems.append(f"{ENV_EMAIL} inválido")
    elif strict_environment() and smtp_from_is_placeholder(cfg.email):
        problems.append(f"{ENV_EMAIL} usa domínio reservado/de desenvolvimento (proibido em {settings.ENVIRONMENT})")
    if len(cfg.name) < 2 or len(cfg.name) > 255:
        problems.append(f"{ENV_NAME} inválido (2 a 255 caracteres)")
    password: Optional[str] = None
    if not cfg.password_file:
        problems.append(f"{ENV_PASSWORD_FILE} obrigatório (arquivo com a senha inicial)")
    else:
        try:
            password = Path(cfg.password_file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            problems.append(f"{ENV_PASSWORD_FILE}: arquivo inexistente ou ilegível")
        else:
            if not password:
                problems.append(f"{ENV_PASSWORD_FILE}: arquivo vazio")
            else:
                errs = password_policy_errors(password, cfg.email)
                if errs:
                    problems.append(f"{ENV_PASSWORD_FILE}: senha fora da política ({'; '.join(errs)})")
    return problems, (None if problems else password)


@dataclass
class BootstrapResult:
    mode: str
    mode_version: int
    mode_created: bool
    mode_source: Optional[str]
    seat1_status: str  # criado | existente | vago
    seat1_email_masked: Optional[str]
    legacy_adopted: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    credential_seals_adopted: list[int] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"Modo de aprovação financeira: {self.mode} (versão {self.mode_version}) — "
            + (f"inicializado agora ({self.mode_source})" if self.mode_created else "já inicializado"),
            f"Assento 1: {self.seat1_email_masked or '—'} ({self.seat1_status})",
        ]
        if self.legacy_adopted:
            out.append(f"Assentos legados adotados e selados: {', '.join(map(str, self.legacy_adopted))}")
        if self.credential_seals_adopted:
            out.append("Selo de credencial adotado (TOFU) para o(s) assento(s): "
                       f"{', '.join(map(str, self.credential_seals_adopted))}")
        out += [f"AVISO: {w}" for w in self.warnings]
        return out


async def _has_legacy(db: AsyncSession) -> bool:
    row = (await db.execute(text(
        "SELECT EXISTS (SELECT 1 FROM users WHERE admin_master_seat IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM financial_approval_requests)"))).scalar_one()
    return bool(row)


async def _audit_once(db: AsyncSession, action: str, dedupe: dict, after: dict) -> bool:
    """Auditoria 1× por conteúdo de ``dedupe`` (campos comparados em ``after_data``)."""
    from app.core.audit import audit
    from app.core.authz import AuthContext
    from app.models.audit import AuditLog

    q = select(AuditLog.id).where(AuditLog.action == action)
    for k, v in dedupe.items():
        q = q.where(AuditLog.after_data[k].astext == str(v))
    if (await db.execute(q.limit(1))).first() is not None:
        return False
    await audit(db, AuthContext.system(origin="system"), action, "instance_security", None,
                after={**after, **dedupe}, origin="system")
    return True


def env_divergence_local(state: Any, environ: Optional[Mapping[str, str]] = None) -> dict:
    """Divergência vista PELO PROCESSO atual (o ENV de bootstrap normalmente só existe no serviço
    ``bootstrap``)."""
    env = os.environ if environ is None else environ
    norm, dual, ok = normalize_dual(env.get(ENV_DUAL))
    divergence = bool(state is not None and ok and dual is not None and (state.mode == "DUAL") != dual)
    return {"divergence": divergence, "env_value": norm, "unsupported_var_present": ENV_UNSUPPORTED in env}


async def record_env_divergence(db: AsyncSession, state: Any, *, env_value: str) -> None:
    """WARNING + auditoria ``security.env_divergence_detected`` 1× por (valor, versão) + aviso IN_APP."""
    from app.modules.security.notifications import notify_env_divergence

    log.warning("security_env_divergence", env_var=ENV_DUAL, env_value=env_value, effective_mode=state.mode,
                mode_version=state.version)
    created = await _audit_once(db, "security.env_divergence_detected",
                                {"kind": "approval_mode", "env_value": env_value, "mode_version": state.version},
                                {"effective_mode": state.mode})
    if created:
        await notify_env_divergence(db, env_value=env_value, mode=state.mode, version=state.version)


async def publish_bootstrap_env_report(report: dict) -> None:
    """Relatório do último ``bootstrap`` no Redis (lido por /metrics e /security/approval-mode)."""
    try:
        from app.core.redis import get_redis, rkey

        r = await get_redis()
        await r.set(rkey("security", "bootstrap_env_report"), json.dumps(report))
    except Exception as exc:  # best-effort: a auditoria já registrou
        log.warning("bootstrap_env_report_unavailable", error_type=type(exc).__name__)


async def read_bootstrap_env_report() -> Optional[dict]:
    try:
        from app.core.redis import get_redis, rkey

        r = await get_redis()
        raw = await r.get(rkey("security", "bootstrap_env_report"))
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def env_status(state: Any) -> dict:
    """``{"divergence", "unsupported_var_present"}``: ENV deste processo OU relatório do último bootstrap
    (válido para a versão atual do modo)."""
    local = env_divergence_local(state)
    rep = await read_bootstrap_env_report()
    divergence = local["divergence"]
    unsupported = local["unsupported_var_present"]
    if rep and state is not None and int(rep.get("mode_version") or 0) == state.version:
        divergence = divergence or bool(rep.get("divergence"))
        unsupported = unsupported or bool(rep.get("unsupported_var_present"))
    return {"divergence": divergence, "unsupported_var_present": unsupported}


async def bootstrap_instance(environ: Optional[Mapping[str, str]] = None) -> BootstrapResult:
    """Algoritmo da SPEC §10. Levanta ``BootstrapConfigError`` (nada gravado) em erro de configuração."""
    from app.core.audit import audit
    from app.core.authz import AuthContext
    from app.core.database import worker_session
    from app.core.email import mask_email
    from app.core.passwords import hash_password
    from app.core.rbac_sync import sync_catalog
    from app.core.timeutils import utcnow
    from app.models.security import AdminMasterSeatEvent
    from app.models.users import User
    from app.modules.security import approval_mode as am
    from app.modules.security import ddl, masters

    cfg = read_env(environ)
    for w in cfg.warnings:
        log.warning("security_unsupported_env_var", message=w)
    if cfg.problems:
        raise BootstrapConfigError(cfg.problems)
    ctx = AuthContext.system(origin="system")
    warnings = list(cfg.warnings)
    adopted: list[int] = []
    async with worker_session() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ddl.LOCK_BOOTSTRAP})
        await sync_catalog(db)
        # D1 (TOFU, uma vez): titulares sem selo de credencial (instalações anteriores à 0009) passam a ser
        # selados AGORA com o estado atual. Janela migration→bootstrap = risco residual documentado (ADR_011).
        from app.modules.security.credentials import adopt_null_credential_seals

        cred_adopted = await adopt_null_credential_seals(db, ctx)
        state = await am.current_state(db)
        created_mode, source = False, None
        if state is None:
            legacy = await _has_legacy(db)
            if legacy:
                mode, source = "DUAL", "LEGACY_DUAL_ADOPTED"  # ENV ignorado (divergência sinalizada abaixo)
            else:
                if cfg.dual is None:
                    raise BootstrapConfigError([f"{ENV_DUAL} obrigatório na primeira inicialização (true|false)"])
                mode, source = ("DUAL" if cfg.dual else "SINGLE"), "BOOTSTRAP_ENV"
            state, created_mode = await am.initialize(db, mode=mode, source=source, env_value=cfg.dual_raw)
            if legacy:
                evs = {int(s) for (s,) in (await db.execute(select(AdminMasterSeatEvent.seat))).all()}
                for seat, holder in sorted((await masters.seat_holders(db)).items()):
                    if seat in evs:
                        continue
                    await masters.assign_seat(db, user_id=holder.id, seat=seat, source="LEGACY_ADOPTED")
                    await audit(db, ctx, "admin_master.legacy_adopted", "user", holder.id, after={"seat": seat},
                                origin="system")
                    adopted.append(seat)
        holders = await masters.seat_holders(db)
        seat1_ever = (await db.execute(
            select(AdminMasterSeatEvent.id).where(AdminMasterSeatEvent.seat == 1).limit(1))).first() is not None
        seat1 = holders.get(1)
        if seat1 is None and not seat1_ever:
            problems, password = _master_problems(cfg)
            if problems:
                raise BootstrapConfigError(problems)
            if (await db.execute(select(User.id).where(User.email == cfg.email))).first() is not None:
                raise BootstrapConfigError([f"{ENV_EMAIL}: e-mail já pertence a outra conta; use outro e-mail"])
            import uuid as _uuid

            now = utcnow()
            user = User(id=_uuid.uuid4(), email=cfg.email, full_name=cfg.name, hashed_password=hash_password(password),
                        password_changed_at=now, must_change_password=True, is_verified=True, is_active=True)
            db.add(user)
            await db.flush()
            await masters.assign_seat(db, user_id=user.id, seat=1, source="BOOTSTRAP_ENV")
            await audit(db, ctx, "admin_master.bootstrapped", "user", user.id,
                        after={"seat": 1, "source": "BOOTSTRAP_ENV", "email": mask_email(cfg.email)}, origin="system")
            seat1_status, seat1_email = "criado", cfg.email
            log.warning("admin_master_bootstrapped", seat=1, email=mask_email(cfg.email))
        elif seat1 is None:
            seat1_status, seat1_email = "vago", None
            warnings.append("assento 1 vago com histórico de atribuição: verifique a integridade (security-verify)")
        else:
            seat1_status, seat1_email = "existente", seat1.email
            if cfg.email and cfg.email != seat1.email.lower():
                warnings.append(f"{ENV_EMAIL} diverge do titular do assento 1 (ignorado; o titular não muda)")
                from app.core.crypto import hmac_hex

                await _audit_once(db, "security.env_divergence_detected",
                                  {"kind": "master_email", "env_email_ref": hmac_hex(cfg.email,
                                                                                     purpose="audit_dedupe")[:16]},
                                  {"env_email": mask_email(cfg.email), "holder_email": mask_email(seat1.email)})
        local = env_divergence_local(state, environ)
        if local["divergence"]:
            warnings.append(f"{ENV_DUAL}={local['env_value']} diverge do modo efetivo {state.mode} "
                            f"(versão {state.version}); a variável só vale na primeira inicialização e foi IGNORADA")
            await record_env_divergence(db, state, env_value=local["env_value"])
        result = BootstrapResult(mode=state.mode, mode_version=state.version, mode_created=created_mode,
                                 mode_source=source, seat1_status=seat1_status,
                                 seat1_email_masked=mask_email(seat1_email) if seat1_email else None,
                                 legacy_adopted=adopted, warnings=warnings,
                                 credential_seals_adopted=cred_adopted)
    await publish_bootstrap_env_report({
        "divergence": bool(local["divergence"]), "env_value": local["env_value"],
        "mode_version": state.version, "unsupported_var_present": local["unsupported_var_present"],
    })
    from app.modules.notifications.service import drain

    await drain()
    return result


# ─── Verificação de startup da API (lifespan) ────────────────────────────────

async def startup_security_check() -> None:
    """Produção/staging: instância não inicializada ou assento 1 vago ⇒ ``StartupConfigError``.

    Development/test: WARNING. Integridade falha ⇒ log CRITICAL + auditoria/notificação (não impede subir).
    Divergência de ENV (se este processo tiver a variável) ⇒ WARNING + auditoria 1×. Atualiza o snapshot das
    métricas ``rd_security_*``. Não lê segredos.
    """
    from app.core.database import session_factory
    from app.core.startup_checks import StartupConfigError
    from app.modules.security import approval_mode as am
    from app.modules.security import metrics as sec_metrics
    from app.modules.security.masters import seat_holders

    strict = strict_environment()
    local_unsupported = ENV_UNSUPPORTED in os.environ
    if local_unsupported:
        log.warning("security_unsupported_env_var", env_var=ENV_UNSUPPORTED, correct_name=ENV_DUAL)
    try:
        async with session_factory()() as db:
            state = await am.current_state(db)
            holders = await seat_holders(db)
            if state is None or 1 not in holders:
                msg = "instância não inicializada: execute o serviço bootstrap"
                if strict:
                    raise StartupConfigError(msg)
                log.warning("security_instance_not_initialized", initialized=state is not None,
                            seat1=1 in holders)
            report = await am.verify_integrity(db)
            if not report.ok:
                await am.on_integrity_failure(report, origin="api_startup")
            local = env_divergence_local(state)
            if local["divergence"]:
                await record_env_divergence(db, state, env_value=local["env_value"])
            await db.commit()
            snap = await sec_metrics.compute_snapshot(db, integrity=report)
        await sec_metrics.store_snapshot(snap)
    except StartupConfigError:
        raise
    except Exception as exc:
        if strict:
            raise StartupConfigError(
                f"verificação de segurança da instância indisponível ({type(exc).__name__}): "
                "confira o banco e o serviço bootstrap") from None
        log.error("security_startup_check_failed", error_type=type(exc).__name__)
