"""
RD Mídia Indoor — Harness de testes (AÇÃO 002 / W1). PostgreSQL e Redis REAIS.

Como funciona
-------------
- Cria um banco DESCARTÁVEL ``rdtest_<uuid8>`` no servidor do ambiente (rd-postgres),
  aplica o schema e o DROPA no fim da sessão (mesmo em falha). Nunca toca o banco
  principal (``settings.DATABASE_URL`` é reapontado ANTES de qualquer engine existir).
  - Modo rápido (padrão): ``Base.metadata.create_all`` + ``app.db.ddl.apply_all_sync``
    (funções/triggers) + sincronização do catálogo RBAC.
  - ``RD_TEST_SCHEMA=alembic``: ``alembic upgrade head`` (inclui a migration 0003).
- Isolamento: commits REAIS (como em produção — transações independentes, locks,
  triggers) e limpeza por ``TRUNCATE`` após cada teste que usa o banco
  (``session_replication_role=replica`` para atravessar os triggers append-only;
  perfis de sistema e catálogo de permissões são preservados).
- Redis real, db 15, prefixo exclusivo ``rdtest:<uuid8>:`` — chaves removidas após
  cada teste.
- ``ENVIRONMENT=test`` → engine com NullPool (um event loop por teste).

Fixtures principais: ``db`` (AsyncSession), ``db_engine``, ``client``
(httpx + ASGITransport), ``client_factory``, ``mail`` (caixa de saída em memória),
``redis`` (cliente async). Factories em ``tests/factories.py``.

Rodar (container de teste — o rd-backend conecta como ``rd_app``, sem permissão para criar bancos):
    docker compose --profile test run --rm backend-tests python -m pytest -q
    docker compose --profile test run --rm backend-tests python -m pytest -q -m "unit"   # sem banco
    docker compose --profile test run --rm -e RD_TEST_SCHEMA=alembic backend-tests python -m pytest -q -m integration
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

# ─── Ambiente de teste (ANTES de importar a app) ─────────────────────────────
RUN_ID = uuid.uuid4().hex[:8]
TEST_DB = f"rdtest_{RUN_ID}"
os.environ["ENVIRONMENT"] = "test"
os.environ["DEBUG"] = "false"
os.environ["EMAIL_BACKEND"] = "memory"
os.environ.pop("CAPTCHA_PROVIDER", None)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import settings  # noqa: E402

_ORIGINAL_SYNC_URL = settings.database_url_sync
assert TEST_DB.startswith("rdtest_")
settings.DATABASE_URL = make_url(settings.DATABASE_URL).set(database=TEST_DB).render_as_string(hide_password=False)


def _redis_db15(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/15", p.query, p.fragment))


settings.REDIS_URL = _redis_db15(settings.REDIS_URL)
settings.REDIS_KEY_PREFIX = f"rdtest:{RUN_ID}:"
TEST_ORIGIN = "http://testserver"
if TEST_ORIGIN not in settings.CORS_ORIGINS:
    settings.CORS_ORIGINS.append(TEST_ORIGIN)
settings.FRONTEND_URL = TEST_ORIGIN

import app.models  # noqa: E402,F401
from app.core.database import Base, create_engine as create_async_engine_rd, session_factory  # noqa: E402

PRESERVED_TABLES = {"alembic_version", "permissions", "roles", "role_permissions"}


def _sync_url(dbname: str) -> str:
    return make_url(_ORIGINAL_SYNC_URL).set(database=dbname).render_as_string(hide_password=False)


def _admin_engine():
    return create_engine(_sync_url("postgres"), isolation_level="AUTOCOMMIT")


def _drop_test_db() -> None:
    assert TEST_DB.startswith("rdtest_")
    eng = _admin_engine()
    with eng.connect() as c:
        c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"),
                  {"n": TEST_DB})
        c.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
    eng.dispose()


def _build_schema() -> None:
    mode = os.environ.get("RD_TEST_SCHEMA", "fast")
    if mode == "alembic":
        from alembic import command
        from alembic.config import Config

        os.environ["ALEMBIC_DATABASE_URL"] = _sync_url(TEST_DB)
        try:
            command.upgrade(Config(os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")), "head")
        finally:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        return
    from app.core.rbac_sync import sync_catalog_sync
    from app.db import ddl

    eng = create_engine(_sync_url(TEST_DB))
    with eng.begin() as conn:
        for stmt in ddl.extension_statements() + ddl.base_functions():
            conn.execute(text(stmt))
        Base.metadata.create_all(conn)
        ddl.apply_all_sync(conn)
        sync_catalog_sync(conn)
    eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    eng = _admin_engine()
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    eng.dispose()
    try:
        _build_schema()
        yield TEST_DB
    finally:
        _drop_test_db()
        _cleanup_redis()


_NONEMPTY_SQL: str | None = None


def _nonempty_sql(c) -> str:
    """Consulta única que devolve as tabelas com linhas (limpeza só do que foi usado)."""
    global _NONEMPTY_SQL
    if _NONEMPTY_SQL is None:
        names = [r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))]
        tables = sorted(n for n in names if n not in PRESERVED_TABLES)
        _NONEMPTY_SQL = " UNION ALL ".join(
            f"SELECT '{t}' WHERE EXISTS (SELECT 1 FROM \"{t}\")" for t in tables
        )
    return _NONEMPTY_SQL


def _cleanup_db() -> None:
    """Limpa o banco de teste entre testes.

    - Encerra conexões remanescentes de event loops já fechados (tarefas em segundo
      plano que não terminaram): entre testes nenhuma conexão legítima fica aberta.
    - DELETE apenas nas tabelas com linhas, com ``session_replication_role=replica``
      (atravessa triggers append-only e FKs) — muito mais rápido que TRUNCATE de
      centenas de tabelas.
    - ``lock_timeout``/``statement_timeout``: bloqueio vira erro claro, não travamento.
    """
    eng = create_engine(_sync_url(TEST_DB))
    try:
        with eng.begin() as c:
            c.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = current_database() AND pid <> pg_backend_pid()")
            )
        with eng.begin() as c:
            c.execute(text("SET LOCAL lock_timeout = '15s'"))
            c.execute(text("SET LOCAL statement_timeout = '60s'"))
            c.execute(text("SET LOCAL session_replication_role = replica"))
            sql = _nonempty_sql(c)
            dirty = [r[0] for r in c.execute(text(sql))] if sql else []
            for t in dirty:
                c.execute(text(f'DELETE FROM "{t}"'))
            c.execute(text("DELETE FROM roles WHERE NOT is_system"))
    finally:
        eng.dispose()


async def _drain_background_tasks() -> None:
    """Aguarda tarefas assíncronas pós-commit da app (ex.: notificações) antes do teardown."""
    try:
        from app.modules.notifications.service import drain as _drain_notifications
    except ImportError:
        return
    import asyncio

    await asyncio.wait_for(_drain_notifications(), timeout=30)


def _cleanup_redis() -> None:
    import redis

    r = redis.Redis.from_url(settings.REDIS_URL)
    try:
        keys = list(r.scan_iter(match=settings.REDIS_KEY_PREFIX + "*", count=1000))
        if keys:
            r.delete(*keys)
    finally:
        r.close()


@pytest.fixture
def clean_state(_test_database):
    """Garante banco/redis limpos ao FINAL do teste (commits reais durante o teste)."""
    yield
    _cleanup_db()
    _cleanup_redis()


@pytest_asyncio.fixture
async def db_engine(clean_state):
    eng = create_async_engine_rd(nullpool=True)
    yield eng
    await _drain_background_tasks()
    await eng.dispose()


@pytest_asyncio.fixture
async def db(db_engine):
    async with session_factory(db_engine)() as s:
        yield s


@pytest_asyncio.fixture
async def redis(clean_state):
    from app.core.redis import close_redis, get_redis

    r = await get_redis()
    yield r
    await close_redis()


@pytest.fixture
def mail():
    from app.core.email import MemoryEmailSender, set_email_sender

    sender = MemoryEmailSender()
    set_email_sender(sender)
    yield sender
    set_email_sender(None)


def _make_client():
    import httpx

    from app.main import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False, client=("127.0.0.1", 50000)),
        base_url=TEST_ORIGIN,
        headers={"User-Agent": "pytest-rd/1.0 (Windows NT 10.0) Chrome/120.0"},
    )


@pytest_asyncio.fixture
async def client(db_engine, mail):
    from app.core.redis import close_redis

    async with _make_client() as c:
        yield c
        await _drain_background_tasks()
    await close_redis()


@pytest_asyncio.fixture
async def client_factory(db_engine, mail):
    from app.core.redis import close_redis

    clients = []

    def _new():
        c = _make_client()
        clients.append(c)
        return c

    yield _new
    await _drain_background_tasks()
    for c in clients:
        await c.aclose()
    await close_redis()


def pytest_collection_modifyitems(config, items):
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        for marker in ("unit", "integration", "concurrency", "adversarial", "migrations", "e2e", "workers"):
            if f"/tests/{marker}/" in path:
                item.add_marker(getattr(pytest.mark, marker))
