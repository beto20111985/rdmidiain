"""AÇÃO 003 / agente E — o BANCO recusa, mesmo com a credencial do papel de runtime ``rd_app``.

Banco DESCARTÁVEL (``rdtest_mig_*``) migrado pela cadeia Alembic até o head (0007), com os
privilégios de produção para ``rd_app`` (GRANT DML em tudo + os REVOKE das migrations/DDL —
como o db-init + migrate fazem no dev). Cada teste roda numa transação desfeita no fim:
preparação como superusuário (funções oficiais ``SECURITY DEFINER`` + selos reais), depois
``SET LOCAL ROLE rd_app`` e cada tentativa num SAVEPOINT.

Cobertura (SPEC §15, E): escrita direta nas trilhas; assento/ativação/e-mail/TOTP/senha financeira/
``super_admin`` de Master; convite aceito direto, 2º convite pendente, convite em SINGLE; transição de
modo com pedido pendente/aprovador não-Master/versão errada/pedido reutilizado/selo errado/sem evento;
pedido DUAL com 1 Master ativo; pedido SINGLE em DUAL; aprovação pelo próprio solicitante/não-Master;
3º assento; ``MASTER_INVITE`` com convite expirado/revogado/e-mail diferente/usuário sem TOTP;
``BOOTSTRAP_ENV`` sem troca de senha obrigatória; sombra por tabela TEMPORÁRIA; EXECUTE das definer.
"""
from __future__ import annotations

import json
import re
import secrets
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.db import ddl
from app.modules.financial import ddl as fin_ddl
from app.modules.security import ddl as sec_ddl
from app.modules.security import seal
from tests.migrations.test_migrations import _url, alembic, q, scratch_db

FIN_HASH = "$2b$12$" + "F" * 53  # formato bcrypt (valor fictício)
PWD_HASH = "$2b$12$" + "P" * 53
REASON = "Motivo de teste com mais de vinte caracteres"


def grant_runtime_like_production(db: str) -> bool:
    """GRANT DML em tudo ao rd_app (db-init) + os REVOKE das migrations/DDL. False se o papel não existe."""
    if not q(db, "select exists (select 1 from pg_roles where rolname = 'rd_app')")[0][0]:
        return False
    stmts = [
        "GRANT USAGE ON SCHEMA public TO rd_app",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rd_app",
        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO rd_app",
    ]
    stmts += [f"REVOKE UPDATE, DELETE, TRUNCATE ON {t} FROM rd_app" for t in ddl.APPEND_ONLY_TABLES]
    stmts += fin_ddl.revoke_statements("rd_app") + sec_ddl.revoke_statements("rd_app")
    eng = create_engine(_url(db))
    try:
        with eng.begin() as c:
            for s in stmts:
                c.execute(text(s))
    finally:
        eng.dispose()
    return True


class Sec:
    """Operações de preparação/ataque sobre UMA conexão (transação aberta pelo chamador)."""

    def __init__(self, conn):
        self.c = conn

    # ── básico ──
    def x(self, sql: str, **p):
        return self.c.execute(text(sql), p)

    def one(self, sql: str, **p):
        return self.x(sql, **p).scalar()

    def as_app(self) -> None:
        self.x("SET LOCAL ROLE rd_app")

    def as_owner(self) -> None:
        self.x("RESET ROLE")

    def denied(self, sql: str, match: str | None = None, **p) -> str:
        """Executa num SAVEPOINT; exige erro do banco (e ``match`` na mensagem). Desfaz o savepoint."""
        sp = self.c.begin_nested()
        try:
            self.x(sql, **p)
        except DBAPIError as exc:
            sp.rollback()
            msg = str(exc.orig)
            if match is not None:
                assert match in msg, msg
            return msg
        sp.rollback()
        raise AssertionError(f"o banco ACEITOU: {sql}")

    def allowed(self, sql: str, **p):
        sp = self.c.begin_nested()
        try:
            out = self.x(sql, **p)
            rows = out.all() if out.returns_rows else None
        except Exception:
            sp.rollback()
            raise
        sp.commit()
        return rows

    # ── usuários / modo / assentos ──
    def user(self, label: str = "u", *, must_change: bool = True, ready: bool = False, seat: int | None = None) -> uuid.UUID:
        uid = uuid.uuid4()
        self.x("INSERT INTO users (id, email, hashed_password, full_name, is_active, is_verified, must_change_password, "
               "admin_master_seat) VALUES (:i, :e, :h, :n, true, true, :m, :s)",
               i=uid, e=f"{label}.{uid.hex[:8]}@rd-teste.com.br", h=PWD_HASH, n=f"Teste {label}", m=must_change, s=seat)
        if ready:
            self.ready(uid)
        return uid

    def ready(self, uid) -> None:
        """Fatores completos (como o próprio usuário faria no primeiro acesso)."""
        self.x("UPDATE users SET must_change_password = false, totp_enabled = true, totp_secret_enc = 'v1:teste', "
               "totp_enabled_at = now(), financial_password_hash = :f, financial_password_changed_at = now() "
               "WHERE id = :u", f=FIN_HASH, u=uid)

    def init_mode(self, mode: str, *, source: str = "BOOTSTRAP_ENV", env: str | None = None):
        env = env if env is not None else ("true" if mode == "DUAL" else "false")
        s = seal.mode_seal(prev_seal=seal.GENESIS, mode_version=1, from_mode=None, to_mode=mode, kind="INITIALIZED",
                           source=source, approval_request_id=None, requested_by=None, authorized_by=None,
                           reason=None, env_value=env)
        return self.x("SELECT approval_mode, mode_version, created FROM rd_security_init_approval_mode(:m, :s, :e, :seal)",
                      m=mode, s=source, e=env, seal=s).one()

    def seat_head(self) -> str:
        return self.one("SELECT app_seal FROM admin_master_seat_events ORDER BY id DESC LIMIT 1") or seal.GENESIS

    def assign(self, uid, seat: int, source: str, *, invite=None, actor=None, prev: str | None = None) -> None:
        self.x("SELECT pg_advisory_xact_lock(:k)", k=sec_ddl.LOCK_MASTER_SEATS)
        prev = prev if prev is not None else self.seat_head()
        s = seal.seat_seal(prev_seal=prev, seat=seat, user_id=uid, event="ASSIGNED", source=source,
                           invite_id=invite, actor_user_id=actor)
        self.x("SELECT rd_security_assign_master_seat(:u, CAST(:s AS smallint), :src, :inv, :act, :prev, :seal)",
               u=uid, s=seat, src=source, inv=invite, act=actor, prev=prev, seal=s)

    def has_seal_columns(self) -> bool:
        """0009+: ``admin_master_invites.seal`` e ``users.master_credential_seal`` existem."""
        if not hasattr(self, "_seals"):
            self._seals = bool(self.one("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
                                        "table_name = 'admin_master_invites' AND column_name = 'seal')"))
        return self._seals

    def invite(self, invited_by, *, seat: int = 2, email: str | None = None,
               expired: bool = False, seal: str | None = "auto") -> tuple[uuid.UUID, uuid.UUID, str]:
        """Convite PENDING. 0009+: com ``seal`` (padrão: valor de teste — o banco não confere o HMAC)."""
        inv, act = uuid.uuid4(), uuid.uuid4()
        email = email or f"b.{act.hex[:8]}@rd-teste.com.br"
        # expirado: criado há 4 dias, venceu há 1 hora (now() é fixo na transação)
        created = "now() - interval '4 days'" if expired else "now()"
        expires = "now() - interval '1 hour'" if expired else "now() + interval '72 hours'"
        cols, vals, extra = "", "", {}
        if self.has_seal_columns():
            cols, vals = ", seal", ", :seal"
            extra["seal"] = secrets.token_hex(32) if seal == "auto" else seal
        self.x("INSERT INTO admin_master_invites (id, seat, email, full_name, token_hash, activation_user_id, invited_by, "
               f"status, created_at, expires_at{cols}) VALUES (:i, CAST(:s AS smallint), :e, 'Sócio B', :t, :a, :by, "
               f"'PENDING', {created}, {expires}{vals})",
               i=inv, s=seat, e=email, t=secrets.token_hex(32), a=act, by=invited_by, **extra)
        return inv, act, email

    def master_b_by_invite(self, a) -> uuid.UUID:
        inv, act, email = self.invite(a)
        self.x("INSERT INTO users (id, email, hashed_password, full_name, is_active, is_verified) "
               "VALUES (:i, :e, :h, 'Sócio B', true, true)", i=act, e=email, h=PWD_HASH)
        self.ready(act)
        self.assign(act, 2, "MASTER_INVITE", invite=inv, actor=act)
        return act

    def dual_two_masters(self) -> tuple[uuid.UUID, uuid.UUID]:
        self.init_mode("DUAL")
        a = self.user("a")
        self.assign(a, 1, "BOOTSTRAP_ENV")
        self.ready(a)
        b = self.master_b_by_invite(a)
        return a, b

    def single_master(self) -> uuid.UUID:
        self.init_mode("SINGLE")
        a = self.user("a")
        self.assign(a, 1, "BOOTSTRAP_ENV")
        self.ready(a)
        return a

    # ── pedidos financeiros ──
    def request(self, requested_by, mode: str, *, action: str = "LEDGER_ADJUSTMENT", payload: dict | None = None,
                reason: str = REASON, status: str = "PENDING", rid=None) -> uuid.UUID:
        rid = rid or uuid.uuid4()
        self.x("INSERT INTO financial_approval_requests (id, action_type, payload, payload_sha256, amount_cents, reason, "
               "requested_by, status, approval_mode, expires_at, execution_state, idempotency_key) VALUES "
               "(:i, :a, CAST(:p AS jsonb), :h, NULL, :r, :by, :st, :m, now() + interval '1 day', 'NOT_STARTED', :k)",
               i=rid, a=action, p=json.dumps(payload or {"x": 1}), h="0" * 64, r=reason, by=requested_by, st=status,
               m=mode, k=f"idem-{rid}")
        return rid

    def event(self, rid, ev: str, actor) -> None:
        self.x("INSERT INTO financial_approval_events (id, request_id, event, actor_user_id) "
               "VALUES (gen_random_uuid(), :r, :e, :a)", r=rid, e=ev, a=actor)

    def mode_head(self):
        return self.x("SELECT mode_version, to_mode, app_seal FROM instance_approval_mode_history "
                      "ORDER BY mode_version DESC LIMIT 1").one()

    def mode_change_request(self, requester, *, target: str, from_mode: str, version: int) -> uuid.UUID:
        return self.request(requester, from_mode, action="APPROVAL_MODE_CHANGE",
                            payload={"target_mode": target, "from_mode": from_mode, "expected_version": version})

    def transition_sql_params(self, rid, *, prev: str | None = None) -> dict:
        ver, mode, head = self.mode_head()
        r = self.x("SELECT requested_by, approved_by, self_authorized_by, reason, payload FROM financial_approval_requests "
                   "WHERE id = :i", i=rid).one()
        target = r.payload.get("target_mode")
        s = seal.mode_seal(prev_seal=head, mode_version=ver + 1, from_mode=mode, to_mode=target,
                           kind="DUAL_TO_SINGLE" if mode == "DUAL" else "SINGLE_TO_DUAL", source="APPROVAL_REQUEST",
                           approval_request_id=rid, requested_by=r.requested_by,
                           authorized_by=r.approved_by or r.self_authorized_by, reason=r.reason, env_value=None)
        return {"r": rid, "prev": prev if prev is not None else head, "seal": s}


TRANSITION_SQL = "SELECT rd_security_apply_mode_transition(:r, :prev, :seal)"


@pytest.fixture(scope="module")
def sec_db(_test_database):
    with scratch_db() as db:
        alembic(db, "upgrade", "head")
        if not grant_runtime_like_production(db):
            pytest.skip("papel rd_app inexistente neste cluster (criado pelo db-init)")
        yield db


@pytest.fixture
def sec(sec_db):
    eng = create_engine(_url(sec_db))
    conn = eng.connect()
    trans = conn.begin()
    try:
        yield Sec(conn)
    finally:
        trans.rollback()
        conn.close()
        eng.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# Trilhas seladas: nem o rd_app nem o dono escrevem direto
# ═════════════════════════════════════════════════════════════════════════════

def test_trails_reject_direct_writes_by_runtime_role(sec):
    a, b = sec.dual_two_masters()
    sec.as_app()
    head = sec.mode_head()
    sec.denied("INSERT INTO instance_approval_mode_history (mode_version, from_mode, to_mode, kind, source, "
               "approval_request_id, requested_by, authorized_by, reason, prev_seal, app_seal) VALUES "
               "(2, 'DUAL', 'SINGLE', 'DUAL_TO_SINGLE', 'APPROVAL_REQUEST', NULL, :a, :b, :r, :p, :s)",
               "permission denied", a=a, b=b, r=REASON, p=head.app_seal, s="a" * 64)
    sec.denied("UPDATE instance_approval_mode_history SET to_mode = 'SINGLE'", "permission denied")
    sec.denied("DELETE FROM instance_approval_mode_history", "permission denied")
    sec.denied("TRUNCATE instance_approval_mode_history", "permission denied")
    sec.denied("INSERT INTO admin_master_seat_events (seat, user_id, event, source, prev_seal, app_seal) VALUES "
               "(2, :u, 'ASSIGNED', 'CLI_BOOTSTRAP', :p, :s)", "permission denied", u=a, p=sec.seat_head(), s="b" * 64)
    sec.denied("UPDATE admin_master_seat_events SET user_id = :u", "permission denied", u=a)
    sec.denied("DELETE FROM admin_master_seat_events", "permission denied")
    sec.denied("TRUNCATE admin_master_seat_events", "permission denied")
    sec.denied("SELECT setval('admin_master_seat_events_id_seq', 1000)", "permission denied")
    # leitura continua permitida (verificação de integridade pela aplicação)
    assert sec.one("SELECT count(*) FROM instance_approval_mode_history") == 1
    assert sec.one("SELECT count(*) FROM admin_master_seat_events") == 2
    # e o trigger append-only vale até para o dono/superusuário
    sec.as_owner()
    sec.denied("UPDATE instance_approval_mode_history SET to_mode = 'SINGLE'", "RD_APPEND_ONLY")
    sec.denied("DELETE FROM admin_master_seat_events", "RD_APPEND_ONLY")
    sec.denied("TRUNCATE instance_approval_mode_history", "RD_APPEND_ONLY")


def test_definer_functions_execute_only_for_runtime_role(sec):
    for sig in sec_ddl.DEFINER_FUNCTIONS:
        assert sec.one("SELECT has_function_privilege('rd_app', :f, 'EXECUTE')", f=sig) is True, sig
        public = sec.one("SELECT coalesce(bool_or(a.grantee = 0), false) FROM pg_proc p, aclexplode(p.proacl) a "
                         "WHERE p.oid = CAST(:f AS regprocedure)", f=sig)
        assert public is False, sig
        assert sec.one("SELECT prosecdef FROM pg_proc WHERE oid = CAST(:f AS regprocedure)", f=sig) is True
        cfg = sec.one("SELECT array_to_string(proconfig, ';') FROM pg_proc WHERE oid = CAST(:f AS regprocedure)", f=sig)
        assert cfg == "search_path=pg_catalog, public, pg_temp", (sig, cfg)
    if sec.one("SELECT exists (SELECT 1 FROM pg_roles WHERE rolname = 'rd_backup')"):
        assert sec.one("SELECT has_function_privilege('rd_backup', :f, 'EXECUTE')",
                       f=sec_ddl.DEFINER_FUNCTIONS[2]) is False


def test_raise_log_never_carries_seals_hashes_or_secrets():
    src = "\n".join(sec_ddl.function_statements() + sec_ddl.trigger_statements())
    logs = re.findall(r"RAISE LOG '([^']*)',([^;]*);", src)
    assert len(logs) >= 5
    for fmt, args in logs:
        blob = (fmt + args).lower()
        for forbidden in ("seal", "hash", "secret", "password", "token", "email", "reason"):
            assert forbidden not in blob, (forbidden, fmt, args)


# ═════════════════════════════════════════════════════════════════════════════
# Usuários titulares de assento
# ═════════════════════════════════════════════════════════════════════════════

def test_runtime_role_cannot_touch_master_seat_identity_or_factors(sec):
    a, b = sec.dual_two_masters()
    other = sec.user("comum", must_change=False)
    sec.as_app()
    # assento: atribuir, trocar, liberar, inserir já com assento
    sec.denied("UPDATE users SET admin_master_seat = 2 WHERE id = :u", "RD_MASTER_SEAT_PROTECTED", u=other)
    sec.denied("UPDATE users SET admin_master_seat = NULL WHERE id = :u", "RD_MASTER_SEAT_PROTECTED", u=b)
    sec.denied("UPDATE users SET admin_master_seat = 2 WHERE id = :u", "RD_MASTER_SEAT_PROTECTED", u=a)
    sec.denied("INSERT INTO users (id, email, hashed_password, full_name, admin_master_seat) VALUES "
               "(gen_random_uuid(), 'novo.master@rd-teste.com.br', 'x', 'X', 1)", "RD_MASTER_SEAT_PROTECTED")
    # ativação / identidade / anonimização / apagar / recuperação
    sec.denied("UPDATE users SET is_active = false WHERE id = :u", "RD_MASTER_PROTECTED", u=b)
    sec.denied("UPDATE users SET email = 'outro@rd-teste.com.br' WHERE id = :u", "RD_MASTER_PROTECTED", u=b)
    sec.denied("UPDATE users SET anonymized_at = now() WHERE id = :u", "RD_MASTER_PROTECTED", u=b)
    sec.denied("UPDATE users SET master_recovered_at = now() WHERE id = :u", "RD_MASTER_PROTECTED", u=a)
    sec.denied("DELETE FROM users WHERE id = :u", "RD_MASTER_PROTECTED", u=b)
    # TOTP: desligar ou re-chavear; senha financeira: apagar
    sec.denied("UPDATE users SET totp_enabled = false WHERE id = :u", "RD_MASTER_TOTP_PROTECTED", u=b)
    sec.denied("UPDATE users SET totp_secret_enc = 'v1:atacante' WHERE id = :u", "RD_MASTER_TOTP_PROTECTED", u=b)
    sec.denied("UPDATE users SET financial_password_hash = NULL WHERE id = :u",
               "RD_MASTER_FINANCIAL_PASSWORD_PROTECTED", u=b)
    # super_admin do Master: remover/alterar
    sec.denied("DELETE FROM role_assignments WHERE user_id = :u", "RD_MASTER_ROLE_PROTECTED", u=b)
    sec.denied("UPDATE role_assignments SET expires_at = now() WHERE user_id = :u", "RD_MASTER_ROLE_PROTECTED", u=a)
    # 0009 (D1): troca de senha/senha financeira de titular SEM novo selo da aplicação ⇒ recusada
    new_pwd, new_fin = "$2b$12$" + "Q" * 53, "$2b$12$" + "N" * 53
    sec.denied("UPDATE users SET hashed_password = :h, password_changed_at = now() WHERE id = :u",
               "RD_MASTER_CREDENTIAL_SEAL_REQUIRED", h=new_pwd, u=a)
    sec.denied("UPDATE users SET must_change_password = true WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED", u=a)
    sec.denied("UPDATE users SET financial_password_hash = :f WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED",
               f=new_fin, u=a)
    # controle positivo: o que o próprio Master faz pela aplicação continua permitido — COM novo selo (a aplicação
    # confere o HMAC no login/ativação/verify_integrity; aqui o banco só exige o rastro)
    sec.allowed("UPDATE users SET hashed_password = :h, password_changed_at = now(), last_login = now(), "
                "failed_login_attempts = 1, locked_until = NULL, theme = 'dark', master_credential_seal = :s "
                "WHERE id = :u", h=new_pwd, s=secrets.token_hex(32), u=a)
    sec.allowed("UPDATE users SET financial_password_hash = :f, master_credential_seal = :s WHERE id = :u",
                f=new_fin, s=secrets.token_hex(32), u=a)
    # colunas que não são credencial não exigem selo
    sec.allowed("UPDATE users SET last_login = now(), failed_login_attempts = 0, totp_last_step = 1 WHERE id = :u", u=a)
    # selo não volta a NULL; repetir o MESMO selo numa troca de credencial também é recusado
    sec.denied("UPDATE users SET master_credential_seal = NULL WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED", u=a)
    sec.denied("UPDATE users SET hashed_password = :h WHERE id = :u", "RD_MASTER_CREDENTIAL_SEAL_REQUIRED",
               h=PWD_HASH, u=a)
    sec.allowed("UPDATE users SET is_active = false WHERE id = :u", u=other)  # não-Master: regra comum
    assert sec.one("SELECT rd_active_master_count()") == 2


def test_third_seat_is_impossible(sec):
    a, b = sec.dual_two_masters()
    c = sec.user("c")
    # nem o dono consegue um 3º assento: CHECK (1,2) + UNIQUE
    sec.denied("UPDATE users SET admin_master_seat = 3 WHERE id = :u", "ck_users_admin_master_seat_valid", u=c)
    sec.denied("UPDATE users SET admin_master_seat = 1 WHERE id = :u", "uq_users_admin_master_seat", u=c)
    sec.as_app()
    for seat in (1, 2):
        sec.denied("SELECT rd_security_assign_master_seat(:u, CAST(:s AS smallint), 'CLI_BOOTSTRAP', NULL, NULL, :p, :x)",
                   "RD_MASTER_SEAT_OCCUPIED", u=c, s=seat, p=sec.seat_head(), x="c" * 64)
    sec.denied("SELECT rd_security_assign_master_seat(:u, CAST(3 AS smallint), 'CLI_BOOTSTRAP', NULL, NULL, :p, :x)",
               "RD_MASTER_SEAT_INVALID", u=c, p=sec.seat_head(), x="c" * 64)


def test_bootstrap_seat_requires_temporary_password_and_empty_instance(sec):
    sec.init_mode("SINGLE")
    pronto = sec.user("pronto", must_change=False)
    sec.as_app()
    sec.denied("SELECT rd_security_assign_master_seat(:u, CAST(1 AS smallint), 'BOOTSTRAP_ENV', NULL, NULL, :p, :x)",
               "RD_MASTER_SEAT_INVALID_STATE", u=pronto, p=seal.GENESIS, x="d" * 64)
    sec.as_owner()
    a = sec.user("a")
    sec.as_app()
    sec.assign(a, 1, "BOOTSTRAP_ENV")
    # segundo bootstrap (outro usuário, assento 2) recusado
    sec.as_owner()
    z = sec.user("z")
    sec.as_app()
    sec.denied("SELECT rd_security_assign_master_seat(:u, CAST(2 AS smallint), 'BOOTSTRAP_ENV', NULL, NULL, :p, :x)",
               "RD_MASTER_SEAT_INVALID", u=z, p=sec.seat_head(), x="e" * 64)
    # assento 2 pela CLI só em DUAL
    sec.denied("SELECT rd_security_assign_master_seat(:u, CAST(2 AS smallint), 'CLI_BOOTSTRAP', NULL, NULL, :p, :x)",
               "RD_MASTER_SEAT_INVALID_STATE", u=z, p=sec.seat_head(), x="e" * 64)
    # selo encadeado errado
    sec.denied("SELECT rd_security_assign_master_seat(:u, CAST(2 AS smallint), 'CLI_BOOTSTRAP', NULL, NULL, :p, :x)",
               "RD_SEAL_CHAIN_MISMATCH", u=z, p=seal.GENESIS, x="e" * 64)
    assert sec.one("SELECT count(*) FROM admin_master_seat_events") == 1


def test_seat_functions_require_initialized_mode_and_valid_seal(sec):
    a = sec.user("a")
    sec.as_app()
    sec.denied("SELECT rd_security_assign_master_seat(:u, CAST(1 AS smallint), 'BOOTSTRAP_ENV', NULL, NULL, :p, :x)",
               "RD_INSTANCE_NOT_INITIALIZED", u=a, p=seal.GENESIS, x="f" * 64)
    sec.denied("SELECT * FROM rd_security_init_approval_mode('TRIPLE', 'BOOTSTRAP_ENV', NULL, :x)",
               "RD_APPROVAL_MODE_INVALID", x="f" * 64)
    sec.denied("SELECT * FROM rd_security_init_approval_mode('SINGLE', 'LEGACY_DUAL_ADOPTED', NULL, :x)",
               "RD_APPROVAL_MODE_INVALID", x="f" * 64)
    sec.denied("SELECT * FROM rd_security_init_approval_mode('SINGLE', 'BOOTSTRAP_ENV', 'talvez', :x)",
               "RD_APPROVAL_MODE_INVALID", x="f" * 64)
    sec.denied("SELECT * FROM rd_security_init_approval_mode('SINGLE', 'BOOTSTRAP_ENV', NULL, 'nao-hex')", "RD_SEAL_INVALID")
    # idempotente: segunda inicialização não grava e devolve o estado atual (mesmo pedindo outro modo)
    assert tuple(sec.init_mode("DUAL")) == ("DUAL", 1, True)
    assert tuple(sec.init_mode("SINGLE")) == ("DUAL", 1, False)
    assert sec.one("SELECT count(*) FROM instance_approval_mode_history") == 1
    assert sec.one("SELECT rd_security_current_approval_mode()") == "DUAL"


def test_legacy_adoption_seals_existing_holder_once(sec):
    sec.init_mode("DUAL", source="LEGACY_DUAL_ADOPTED")
    legado = sec.user("legado", must_change=False, seat=1)  # assento gravado pela CLI antiga (dono)
    sec.ready(legado)
    sec.as_app()
    sec.assign(legado, 1, "LEGACY_ADOPTED")
    assert sec.one("SELECT rd_is_active_master(:u)", u=legado) is True
    sec.denied("SELECT rd_security_assign_master_seat(:u, CAST(1 AS smallint), 'LEGACY_ADOPTED', NULL, NULL, :p, :x)",
               "RD_MASTER_SEAT_INVALID_STATE", u=legado, p=sec.seat_head(), x="9" * 64)


# ═════════════════════════════════════════════════════════════════════════════
# Convite do 2º Master
# ═════════════════════════════════════════════════════════════════════════════

def test_invite_state_machine_is_enforced_by_the_database(sec):
    sec.init_mode("DUAL")
    a = sec.user("a")
    sec.assign(a, 1, "BOOTSTRAP_ENV")
    sec.ready(a)
    comum = sec.user("comum", must_change=False)
    sec.as_app()
    inv, act, email = sec.invite(a)
    # um PENDENTE por vez (índice único parcial)
    sec.denied("INSERT INTO admin_master_invites (id, seat, email, full_name, token_hash, activation_user_id, invited_by, "
               "status, expires_at, seal) VALUES (gen_random_uuid(), 2, 'c@rd-teste.com.br', 'C', :t, gen_random_uuid(), :a, "
               "'PENDING', now() + interval '72 hours', 'selo-de-teste')", "uq_admin_master_invites_pending_seat",
               t=secrets.token_hex(32), a=a)
    # convidante não-Master; nasce fora de PENDING; validade > 7 dias
    sec.denied("INSERT INTO admin_master_invites (id, seat, email, full_name, token_hash, activation_user_id, invited_by, "
               "status, expires_at, seal) VALUES (gen_random_uuid(), 1, 'c@rd-teste.com.br', 'C', :t, gen_random_uuid(), :a, "
               "'PENDING', now() + interval '72 hours', 'selo-de-teste')", "RD_MASTER_INVITE_INVALID_STATE",
               t=secrets.token_hex(32), a=a)  # assento 1 ocupado
    sec.denied("INSERT INTO admin_master_invites (id, seat, email, full_name, token_hash, activation_user_id, invited_by, "
               "status, expires_at, seal) VALUES (gen_random_uuid(), 2, 'c@rd-teste.com.br', 'C', :t, gen_random_uuid(), :c, "
               "'PENDING', now() + interval '72 hours', 'selo-de-teste')", "RD_MASTER_INVITE_INVALID_STATE",
               t=secrets.token_hex(32), c=comum)
    sec.denied("INSERT INTO admin_master_invites (id, seat, email, full_name, token_hash, activation_user_id, invited_by, "
               "status, accepted_at, accepted_user_id, expires_at, seal) VALUES (gen_random_uuid(), 2, 'c@rd-teste.com.br', 'C', "
               ":t, :x, :a, 'ACCEPTED', now(), :x, now() + interval '72 hours', 'selo-de-teste')", "RD_MASTER_INVITE_INVALID_STATE",
               t=secrets.token_hex(32), a=a, x=uuid.uuid4())
    # aceitar direto (sem a função de ativação) / trocar identidade / apagar
    sec.denied("UPDATE admin_master_invites SET status = 'ACCEPTED', accepted_at = now(), accepted_user_id = "
               "activation_user_id WHERE id = :i", "RD_MASTER_INVITE_INVALID_STATE", i=inv)
    sec.denied("UPDATE admin_master_invites SET email = 'atacante@rd-teste.com.br' WHERE id = :i",
               "RD_MASTER_INVITE_IMMUTABLE", i=inv)
    sec.denied("UPDATE admin_master_invites SET activation_user_id = gen_random_uuid() WHERE id = :i",
               "RD_MASTER_INVITE_IMMUTABLE", i=inv)
    sec.denied("DELETE FROM admin_master_invites WHERE id = :i", "permission denied", i=inv)
    sec.denied("TRUNCATE admin_master_invites", "permission denied")
    # 0009 (D1): convite sem selo da aplicação; token trocado sem novo selo; selo removido ⇒ recusados
    sec.denied("INSERT INTO admin_master_invites (id, seat, email, full_name, token_hash, activation_user_id, invited_by, "
               "status, expires_at) VALUES (gen_random_uuid(), 2, 'c@rd-teste.com.br', 'C', :t, gen_random_uuid(), :a, "
               "'PENDING', now() + interval '72 hours')", "RD_MASTER_INVITE_SEAL_REQUIRED", t=secrets.token_hex(32), a=a)
    sec.denied("UPDATE admin_master_invites SET token_hash = :t WHERE id = :i", "RD_MASTER_INVITE_SEAL_REQUIRED",
               t=secrets.token_hex(32), i=inv)
    sec.denied("UPDATE admin_master_invites SET seal = :s WHERE id = :i", "RD_MASTER_INVITE_SEAL_REQUIRED",
               s=secrets.token_hex(32), i=inv)
    sec.denied("UPDATE admin_master_invites SET seal = NULL WHERE id = :i", "RD_MASTER_INVITE_SEAL_REQUIRED", i=inv)
    # operações legítimas do fluxo (reenvio = token + selo juntos; tentativa errada) continuam permitidas
    sec.allowed("UPDATE admin_master_invites SET token_hash = :t, seal = :s, sent_count = sent_count + 1, "
                "last_sent_at = now(), failed_attempts = failed_attempts + 1 WHERE id = :i",
                t=secrets.token_hex(32), s=secrets.token_hex(32), i=inv)
    sec.allowed("UPDATE admin_master_invites SET failed_attempts = failed_attempts + 1 WHERE id = :i", i=inv)
    # estado final é imutável
    sec.allowed("UPDATE admin_master_invites SET status = 'REVOKED', revoked_at = now(), revoked_by = :a, "
                "revoke_reason = 'teste' WHERE id = :i", a=a, i=inv)
    sec.denied("UPDATE admin_master_invites SET status = 'PENDING' WHERE id = :i", "RD_MASTER_INVITE_INVALID_STATE", i=inv)
    sec.denied("UPDATE admin_master_invites SET failed_attempts = 0 WHERE id = :i", "RD_MASTER_INVITE_INVALID_STATE", i=inv)
    # mesmo o dono não apaga convite (trigger)
    sec.as_owner()
    sec.denied("DELETE FROM admin_master_invites WHERE id = :i", "RD_APPEND_ONLY", i=inv)


def test_invite_rejected_in_single_mode(sec):
    a = sec.single_master()
    sec.as_app()
    sec.denied("INSERT INTO admin_master_invites (id, seat, email, full_name, token_hash, activation_user_id, invited_by, "
               "status, expires_at, seal) VALUES (gen_random_uuid(), 2, 'b@rd-teste.com.br', 'B', :t, gen_random_uuid(), :a, "
               "'PENDING', now() + interval '72 hours', 'selo-de-teste')", "RD_MASTER_INVITE_INVALID_STATE", t=secrets.token_hex(32), a=a)


@pytest.mark.parametrize("case", ["expired", "revoked", "other_email", "no_totp", "no_fin_password",
                                  "must_change", "other_user", "single_mode"])
def test_master_invite_activation_preconditions(sec, case):
    sec.init_mode("DUAL")
    a = sec.user("a")
    sec.assign(a, 1, "BOOTSTRAP_ENV")
    sec.ready(a)
    inv, act, email = sec.invite(a, expired=(case == "expired"))
    user_id, user_email = act, email
    if case == "other_email":
        user_email = "outra.pessoa@rd-teste.com.br"
    if case == "other_user":
        user_id = uuid.uuid4()
    sec.x("INSERT INTO users (id, email, hashed_password, full_name, is_active, is_verified) "
          "VALUES (:i, :e, :h, 'B', true, true)", i=user_id, e=user_email, h=PWD_HASH)
    sec.ready(user_id)
    if case == "revoked":
        sec.x("UPDATE admin_master_invites SET status = 'REVOKED', revoked_at = now() WHERE id = :i", i=inv)
    elif case == "no_totp":
        sec.x("UPDATE users SET totp_enabled = false, totp_secret_enc = NULL WHERE id = :u", u=user_id)
    elif case == "no_fin_password":
        sec.x("UPDATE users SET financial_password_hash = NULL WHERE id = :u", u=user_id)
    elif case == "must_change":
        sec.x("UPDATE users SET must_change_password = true WHERE id = :u", u=user_id)
    elif case == "single_mode":
        # força a cabeça do modo para SINGLE por uma transição legítima seria longa: troca a linha como dono
        # (sem triggers) só para provar a checagem de modo da função
        sec.x("SET LOCAL session_replication_role = replica")
        sec.x("UPDATE instance_approval_mode_history SET to_mode = 'SINGLE'")
        sec.x("SET LOCAL session_replication_role = origin")
    expected = {"no_totp": "RD_MASTER_SEAT_USER_INVALID", "no_fin_password": "RD_MASTER_SEAT_USER_INVALID",
                "must_change": "RD_MASTER_SEAT_USER_INVALID"}.get(case, "RD_MASTER_INVITE_INVALID")
    sec.as_app()
    prev = sec.seat_head()
    s = seal.seat_seal(prev_seal=prev, seat=2, user_id=user_id, event="ASSIGNED", source="MASTER_INVITE",
                       invite_id=inv, actor_user_id=user_id)
    sec.denied("SELECT rd_security_assign_master_seat(:u, CAST(2 AS smallint), 'MASTER_INVITE', :i, :u, :p, :s)",
               expected, u=user_id, i=inv, p=prev, s=s)
    assert sec.one("SELECT admin_master_seat FROM users WHERE id = :u", u=user_id) is None


def test_master_invite_activation_happy_path_marks_invite_and_grants_role(sec):
    a, b = sec.dual_two_masters()
    row = sec.x("SELECT i.status, i.accepted_user_id = :b, u.admin_master_seat, rd_is_active_master(:b) "
                "FROM admin_master_invites i JOIN users u ON u.id = :b WHERE i.activation_user_id = :b", b=b).one()
    assert tuple(row) == ("ACCEPTED", True, 2, True)
    ev = sec.x("SELECT seat, event, source, invite_id IS NOT NULL, coalesce(actor_user_id = :b, false) "
               "FROM admin_master_seat_events ORDER BY id", b=b).all()
    assert [tuple(r) for r in ev] == [(1, "ASSIGNED", "BOOTSTRAP_ENV", False, False), (2, "ASSIGNED", "MASTER_INVITE", True, True)]
    # cadeia encadeada: prev_seal de cada evento = app_seal do anterior
    chain = sec.x("SELECT prev_seal, app_seal FROM admin_master_seat_events ORDER BY id").all()
    assert chain[0].prev_seal == seal.GENESIS and chain[1].prev_seal == chain[0].app_seal


def test_recovery_only_for_seat_holder_and_resets_factors(sec):
    a, b = sec.dual_two_masters()
    comum = sec.user("comum", must_change=False)
    sec.x("INSERT INTO user_recovery_codes (id, user_id, code_hash) VALUES (gen_random_uuid(), :b, :h)",
          b=b, h="1" * 64)
    sec.as_app()
    prev = sec.seat_head()
    s = seal.seat_seal(prev_seal=prev, seat=2, user_id=comum, event="RECOVERED", source="CLI_RECOVERY",
                       invite_id=None, actor_user_id=None)
    sec.denied("SELECT rd_security_recover_master(:u, :h, NULL, :p, :s)", "RD_MASTER_RECOVERY_INVALID",
               u=comum, h=PWD_HASH, p=prev, s=s)
    s = seal.seat_seal(prev_seal=prev, seat=2, user_id=b, event="RECOVERED", source="CLI_RECOVERY",
                       invite_id=None, actor_user_id=None)
    sec.denied("SELECT rd_security_recover_master(:u, :h, NULL, :p, :s)", "RD_SEAL_CHAIN_MISMATCH",
               u=b, h=PWD_HASH, p=seal.GENESIS, s=s)
    assert sec.one("SELECT rd_security_recover_master(:u, :h, NULL, :p, :s)", u=b, h=PWD_HASH, p=prev, s=s) == 2
    row = sec.x("SELECT admin_master_seat, must_change_password, totp_enabled, totp_secret_enc, financial_password_hash, "
                "master_recovered_at IS NOT NULL, is_active FROM users WHERE id = :b", b=b).one()
    assert tuple(row) == (2, True, False, None, None, True, True)
    assert sec.one("SELECT count(*) FROM user_recovery_codes WHERE user_id = :b", b=b) == 0
    assert sec.one("SELECT rd_is_active_master(:b)", b=b) is False  # sem fatores ⇒ não é Master ativo
    assert sec.one("SELECT event || '/' || source FROM admin_master_seat_events ORDER BY id DESC LIMIT 1") == \
        "RECOVERED/CLI_RECOVERY"


# ═════════════════════════════════════════════════════════════════════════════
# Pedidos financeiros × modo de aprovação
# ═════════════════════════════════════════════════════════════════════════════

def test_request_insert_guard(sec):
    a = sec.user("a")
    sec.as_app()
    msg = sec.denied(
        "INSERT INTO financial_approval_requests (id, action_type, payload, payload_sha256, reason, requested_by, status, "
        "approval_mode, expires_at, execution_state, idempotency_key) VALUES (gen_random_uuid(), 'LEDGER_ADJUSTMENT', "
        "'{}', :h, :r, :a, 'PENDING', 'DUAL', now() + interval '1 day', 'NOT_STARTED', 'k0')",
        "RD_INSTANCE_NOT_INITIALIZED", h="0" * 64, r=REASON, a=a)
    assert "RD_INSTANCE_NOT_INITIALIZED" in msg
    sec.as_owner()
    sec.init_mode("DUAL")
    sec.assign(a, 1, "BOOTSTRAP_ENV")
    sec.ready(a)
    comum = sec.user("comum", must_change=False)
    sec.as_app()
    insert = ("INSERT INTO financial_approval_requests (id, action_type, payload, payload_sha256, reason, requested_by, "
              "status, approval_mode, expires_at, execution_state, idempotency_key) VALUES (gen_random_uuid(), "
              "'LEDGER_ADJUSTMENT', '{}', :h, :r, :by, :st, :m, now() + interval '1 day', 'NOT_STARTED', :k)")
    # DUAL com 1 Master ativo
    sec.denied(insert, "RD_SECOND_MASTER_REQUIRED", h="0" * 64, r=REASON, by=a, st="PENDING", m="DUAL", k="k1")
    # pedido de outro modo; solicitante não-Master
    sec.denied(insert, "RD_APPROVAL_MODE_MISMATCH", h="0" * 64, r=REASON, by=a, st="PENDING", m="SINGLE", k="k2")
    sec.denied(insert, "RD_REQUESTER_NOT_ACTIVE_MASTER", h="0" * 64, r=REASON, by=comum, st="PENDING", m="DUAL", k="k3")
    # com o 2º Master ativo: aceita PENDING; recusa nascer em estado final
    sec.as_owner()
    b = sec.master_b_by_invite(a)
    sec.as_app()
    sec.allowed(insert, h="0" * 64, r=REASON, by=a, st="PENDING", m="DUAL", k="k4")
    sec.denied(insert, "RD_APPROVAL_INVALID_STATE", h="0" * 64, r=REASON, by=a, st="REJECTED", m="DUAL", k="k5")
    # nascer APPROVED só com aprovação válida (DUAL: outro Master ativo)
    sec.denied(insert.replace("execution_state, idempotency_key", "execution_state, idempotency_key, approved_by, approved_at")
               .replace("'NOT_STARTED', :k)", "'NOT_STARTED', :k, :ap, now())"),
               "RD_APPROVER_NOT_ACTIVE_MASTER", h="0" * 64, r=REASON, by=a, st="APPROVED", m="DUAL", k="k6", ap=comum)
    assert b is not None


def test_approval_update_guard_dual_and_single(sec):
    a, b = sec.dual_two_masters()
    comum = sec.user("comum", must_change=False)
    sec.as_app()
    rid = sec.request(a, "DUAL")
    # autoaprovação e aprovador não-Master: trigger (antes do CHECK); "autorização única" num pedido DUAL também
    sec.denied("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :a, approved_at = now() "
               "WHERE id = :r", "RD_APPROVER_NOT_ACTIVE_MASTER", a=a, r=rid)
    sec.denied("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :c, approved_at = now() "
               "WHERE id = :r", "RD_APPROVER_NOT_ACTIVE_MASTER", c=comum, r=rid)
    sec.denied("UPDATE financial_approval_requests SET status = 'APPROVED', self_authorized_by = :a, "
               "self_authorized_at = now() WHERE id = :r", "RD_APPROVER_NOT_ACTIVE_MASTER", a=a, r=rid)
    # misturar os fluxos sem mudar o status: CHECK mode_consistent
    sec.denied("UPDATE financial_approval_requests SET self_authorized_by = :a, self_authorized_at = now() "
               "WHERE id = :r", "ck_financial_approval_requests_mode_consistent", a=a, r=rid)
    sec.denied("UPDATE financial_approval_requests SET approval_mode = 'SINGLE' WHERE id = :r", "RD_IMMUTABLE", r=rid)
    # mesmo sem os triggers (dono em modo réplica) o CHECK impede a autoaprovação
    sec.as_owner()
    sec.x("SET LOCAL session_replication_role = replica")
    sec.denied("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :a, approved_at = now() "
               "WHERE id = :r", "ck_financial_approval_requests_approver_not_requester", a=a, r=rid)
    sec.x("SET LOCAL session_replication_role = origin")
    sec.as_app()
    # aprovação legítima de B
    sec.allowed("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :b, approved_at = now() "
                "WHERE id = :r", b=b, r=rid)
    sec.denied("UPDATE financial_approval_requests SET approved_by = :a WHERE id = :r", "RD_IMMUTABLE", a=a, r=rid)


def test_single_mode_self_authorization_rules(sec):
    a = sec.single_master()
    sec.as_owner()
    outro = sec.user("outro", must_change=False)
    sec.as_app()
    rid = sec.request(a, "SINGLE")
    # "aprovação" (fluxo DUAL) num pedido SINGLE; autorização por outra pessoa; rejeição por terceiro
    sec.denied("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :o, approved_at = now() "
               "WHERE id = :r", "RD_SELF_AUTHORIZER_INVALID", o=outro, r=rid)
    sec.denied("UPDATE financial_approval_requests SET approved_by = :o, approved_at = now() WHERE id = :r",
               "ck_financial_approval_requests_mode_consistent", o=outro, r=rid)
    sec.denied("UPDATE financial_approval_requests SET status = 'APPROVED', self_authorized_by = :o, "
               "self_authorized_at = now() WHERE id = :r", "RD_SELF_AUTHORIZER_INVALID", o=outro, r=rid)
    sec.denied("UPDATE financial_approval_requests SET status = 'REJECTED', rejected_by = :o, rejected_at = now() "
               "WHERE id = :r", "ck_financial_approval_requests_mode_consistent", o=outro, r=rid)
    # Master que perdeu os fatores (ex.: recuperado) não autoriza
    sec.as_owner()
    sec.x("UPDATE users SET totp_enabled = false, totp_secret_enc = NULL WHERE id = :a", a=a)
    sec.as_app()
    sec.denied("UPDATE financial_approval_requests SET status = 'APPROVED', self_authorized_by = :a, "
               "self_authorized_at = now() WHERE id = :r", "RD_SELF_AUTHORIZER_INVALID", a=a, r=rid)
    sec.as_owner()
    sec.ready(a)
    sec.as_app()
    sec.allowed("UPDATE financial_approval_requests SET status = 'APPROVED', self_authorized_by = :a, "
                "self_authorized_at = now() WHERE id = :r", a=a, r=rid)
    sec.denied("UPDATE financial_approval_requests SET self_authorized_at = now() - interval '1 day' WHERE id = :r",
               "RD_IMMUTABLE", r=rid)


def test_temp_table_cannot_shadow_security_tables(sec):
    a, b = sec.dual_two_masters()
    sec.as_app()
    # o rd_app tem TEMPORARY: tenta "sombrear" a trilha de modo e os usuários
    sec.allowed("CREATE TEMP TABLE instance_approval_mode_history (to_mode text, mode_version int, app_seal text)")
    sec.allowed("INSERT INTO instance_approval_mode_history VALUES ('SINGLE', 99, 'x')")
    sec.allowed("CREATE TEMP TABLE users (id uuid, admin_master_seat smallint, is_active boolean)")
    assert sec.one("SELECT rd_security_current_approval_mode()") == "DUAL"
    assert sec.one("SELECT rd_active_master_count()") == 2
    sec.denied("INSERT INTO financial_approval_requests (id, action_type, payload, payload_sha256, reason, requested_by, "
               "status, approval_mode, expires_at, execution_state, idempotency_key, self_authorized_by, self_authorized_at) "
               "VALUES (gen_random_uuid(), 'RD_WITHDRAWAL', '{}', :h, :r, :a, 'APPROVED', 'SINGLE', now() + interval '1 day', "
               "'NOT_STARTED', 'kt', :a, now())", "RD_APPROVAL_MODE_MISMATCH", h="0" * 64, r=REASON, a=a)


def test_request_from_old_mode_cannot_be_approved_after_mode_change(sec):
    a, b = sec.dual_two_masters()
    sec.as_app()
    old = sec.request(a, "DUAL")
    ver, _, _ = sec.mode_head()
    rid = sec.mode_change_request(a, target="SINGLE", from_mode="DUAL", version=ver)
    sec.x("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :b, approved_at = now() "
          "WHERE id = :r", b=b, r=rid)
    sec.event(rid, "APPROVED", b)
    assert sec.one(TRANSITION_SQL, **sec.transition_sql_params(rid)) == 2
    assert sec.one("SELECT rd_security_current_approval_mode()") == "SINGLE"
    sec.denied("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :b, approved_at = now() "
               "WHERE id = :r", "RD_APPROVAL_MODE_MISMATCH", b=b, r=old)


# ═════════════════════════════════════════════════════════════════════════════
# Transição de modo pela função definer
# ═════════════════════════════════════════════════════════════════════════════

def test_mode_transition_dual_to_single_validations_and_success(sec):
    a, b = sec.dual_two_masters()
    comum = sec.user("comum", must_change=False)
    forged = sec.user("forjado", must_change=False)
    sec.as_app()
    ver, mode, head = sec.mode_head()
    assert (ver, mode) == (1, "DUAL")
    rid = sec.mode_change_request(a, target="SINGLE", from_mode="DUAL", version=ver)
    # pendente
    sec.denied(TRANSITION_SQL, "RD_MODE_CHANGE_INVALID", **sec.transition_sql_params(rid))
    sec.x("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :b, approved_at = now() "
          "WHERE id = :r", b=b, r=rid)
    # aprovado mas sem o evento APPROVED do aprovador
    sec.denied(TRANSITION_SQL, "RD_MODE_CHANGE_INVALID", **sec.transition_sql_params(rid))
    sec.event(rid, "APPROVED", b)
    # selo anterior errado; selo novo fora do formato
    sec.denied(TRANSITION_SQL, "RD_SEAL_CHAIN_MISMATCH", **sec.transition_sql_params(rid, prev="0" * 64))
    sec.denied(TRANSITION_SQL, "RD_SEAL_INVALID", **{**sec.transition_sql_params(rid), "seal": "zz"})
    # pedido de outra ação
    other = sec.request(a, "DUAL")
    sec.x("UPDATE financial_approval_requests SET status = 'APPROVED', approved_by = :b, approved_at = now() "
          "WHERE id = :r", b=b, r=other)
    sec.denied(TRANSITION_SQL, "RD_MODE_CHANGE_INVALID", **sec.transition_sql_params(other))
    # Pedidos FORJADOS por fora dos triggers (dono em modo réplica — simula linha gravada indevidamente):
    # a função definer revalida tudo e recusa mesmo assim.
    sec.as_owner()
    sec.x("SET LOCAL session_replication_role = replica")
    payload = json.dumps({"target_mode": "SINGLE", "from_mode": "DUAL", "expected_version": ver})
    insert_forged = ("INSERT INTO financial_approval_requests (id, action_type, payload, payload_sha256, reason, "
                     "requested_by, status, approval_mode, approved_by, approved_at, expires_at, execution_state, "
                     "idempotency_key) VALUES (:i, 'APPROVAL_MODE_CHANGE', CAST(:p AS jsonb), :h, :r, :a, 'APPROVED', "
                     "'DUAL', :ap, now(), now() + interval '1 day', 'NOT_STARTED', :k)")
    forged_ids = []
    for approver in (comum, forged):
        fid = uuid.uuid4()
        sec.x(insert_forged, i=fid, p=payload, h="0" * 64, r=REASON, a=a, ap=approver, k=f"forjado-{fid}")
        sec.event(fid, "APPROVED", approver)
        forged_ids.append(fid)
    # autoaprovação não pode nem ser gravada (CHECK vale mesmo em modo réplica)
    sec.denied(insert_forged, "ck_financial_approval_requests_approver_not_requester",
               i=uuid.uuid4(), p=payload, h="0" * 64, r=REASON, a=a, ap=a, k="forjado-auto")
    # solicitante que não é Master ativo (aprovador B legítimo)
    fid = uuid.uuid4()
    sec.x(insert_forged.replace(":a, 'APPROVED'", ":req, 'APPROVED'"), i=fid, p=payload, h="0" * 64, r=REASON,
          req=comum, ap=b, k=f"forjado-{fid}")
    sec.event(fid, "APPROVED", b)
    sec.x("SET LOCAL session_replication_role = origin")
    sec.as_app()
    for fid_ in forged_ids:
        sec.denied(TRANSITION_SQL, "RD_APPROVER_NOT_ACTIVE_MASTER", **sec.transition_sql_params(fid_))
    sec.denied(TRANSITION_SQL, "RD_REQUESTER_NOT_ACTIVE_MASTER", **sec.transition_sql_params(fid))

    # sucesso (pedido legítimo: A solicita, B aprova com evento)
    assert sec.one(TRANSITION_SQL, **sec.transition_sql_params(rid)) == 2
    row = sec.x("SELECT mode_version, from_mode, to_mode, kind, source, requested_by = :a, authorized_by = :b, "
                "prev_seal = :h FROM instance_approval_mode_history ORDER BY mode_version DESC LIMIT 1",
                a=a, b=b, h=head).one()
    assert tuple(row) == (2, "DUAL", "SINGLE", "DUAL_TO_SINGLE", "APPROVAL_REQUEST", True, True, True)
    # reutilizar o mesmo pedido; usar um pedido forjado depois da troca (modo/versão não conferem)
    sec.denied(TRANSITION_SQL, "RD_MODE_CHANGE_REUSED", **sec.transition_sql_params(rid))
    sec.denied(TRANSITION_SQL, "RD_APPROVAL_MODE_MISMATCH", **sec.transition_sql_params(forged_ids[0]))


def test_mode_transition_stale_version_and_single_to_dual(sec):
    a = sec.single_master()
    sec.as_app()
    ver, mode, _ = sec.mode_head()
    assert (ver, mode) == (1, "SINGLE")
    stale = sec.mode_change_request(a, target="DUAL", from_mode="SINGLE", version=ver + 5)
    sec.x("UPDATE financial_approval_requests SET status = 'APPROVED', self_authorized_by = :a, "
          "self_authorized_at = now() WHERE id = :r", a=a, r=stale)
    sec.event(stale, "SELF_AUTHORIZED", a)
    sec.denied(TRANSITION_SQL, "RD_APPROVAL_MODE_MISMATCH", **sec.transition_sql_params(stale))
    bad_target = sec.mode_change_request(a, target="SINGLE", from_mode="SINGLE", version=ver)
    sec.x("UPDATE financial_approval_requests SET status = 'APPROVED', self_authorized_by = :a, "
          "self_authorized_at = now() WHERE id = :r", a=a, r=bad_target)
    sec.event(bad_target, "SELF_AUTHORIZED", a)
    sec.denied(TRANSITION_SQL, "RD_MODE_CHANGE_INVALID", **sec.transition_sql_params(bad_target))
    ok = sec.mode_change_request(a, target="DUAL", from_mode="SINGLE", version=ver)
    sec.x("UPDATE financial_approval_requests SET status = 'APPROVED', self_authorized_by = :a, "
          "self_authorized_at = now() WHERE id = :r", a=a, r=ok)
    sec.denied(TRANSITION_SQL, "RD_MODE_CHANGE_INVALID", **sec.transition_sql_params(ok))  # sem SELF_AUTHORIZED
    sec.event(ok, "SELF_AUTHORIZED", a)
    assert sec.one(TRANSITION_SQL, **sec.transition_sql_params(ok)) == 2
    assert sec.x("SELECT kind, authorized_by = requested_by FROM instance_approval_mode_history "
                 "WHERE mode_version = 2").one() == ("SINGLE_TO_DUAL", True)
    # efeito imediato: DUAL com 1 Master ativo ⇒ novos pedidos bloqueados até ativar o 2º Master
    sec.denied("INSERT INTO financial_approval_requests (id, action_type, payload, payload_sha256, reason, requested_by, "
               "status, approval_mode, expires_at, execution_state, idempotency_key) VALUES (gen_random_uuid(), "
               "'LEDGER_ADJUSTMENT', '{}', :h, :r, :a, 'PENDING', 'DUAL', now() + interval '1 day', 'NOT_STARTED', 'kx')",
               "RD_SECOND_MASTER_REQUIRED", h="0" * 64, r=REASON, a=a)
    # DUAL → SINGLE com o 2º Master pendente é impossível (não há aprovador ativo)
    ver2, _, _ = sec.mode_head()
    sec.denied("INSERT INTO financial_approval_requests (id, action_type, payload, payload_sha256, reason, requested_by, "
               "status, approval_mode, expires_at, execution_state, idempotency_key) VALUES (gen_random_uuid(), "
               "'APPROVAL_MODE_CHANGE', CAST(:p AS jsonb), :h, :r, :a, 'PENDING', 'DUAL', now() + interval '1 day', "
               "'NOT_STARTED', 'ky')", "RD_SECOND_MASTER_REQUIRED",
               p=json.dumps({"target_mode": "SINGLE", "from_mode": "DUAL", "expected_version": ver2}),
               h="0" * 64, r=REASON, a=a)
