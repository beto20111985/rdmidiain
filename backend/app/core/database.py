"""
RD Mídia Indoor — Banco de dados (SQLAlchemy 2 async + asyncpg).

APIs da fundação:
- ``Base``: base declarativa com naming convention (todos os models herdam dela).
- ``get_db``: dependência FastAPI — **commit no sucesso, rollback em erro**.
- ``independent_session()``: sessão em transação SEPARADA (commit próprio), para
  efeitos que precisam persistir mesmo quando a request falha (ex.: contadores de
  falha de login, auditoria de tentativa negada). Usa ``lock_timeout`` para nunca
  ficar pendurada esperando um lock da própria request.
- ``worker_session()``: sessão para Celery/scripts. Cria um engine com NullPool por
  execução e o descarta ao final → funciona em chamadas repetidas de
  ``asyncio.run`` no mesmo processo (cada uma com um event loop novo).
- ``get_db_context()``: alias compatível de ``worker_session()`` (código legado).

Nunca use ``echo=True`` (SQL com parâmetros vaza tokens/hashes em log).
"""
from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from typing import AsyncGenerator, AsyncIterator

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings

# Convenção de nomes para constraints — facilita migrations Alembic
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base para todos os modelos SQLAlchemy"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _engine_kwargs(nullpool: bool) -> dict:
    kw: dict = {"echo": False, "pool_pre_ping": not nullpool}
    if nullpool:
        kw["poolclass"] = NullPool
    else:
        kw.update(pool_size=settings.DB_POOL_SIZE, max_overflow=settings.DB_MAX_OVERFLOW, pool_recycle=3600)
    return kw


def create_engine(url: str | None = None, *, nullpool: bool | None = None) -> AsyncEngine:
    """Cria um engine novo (use para casos especiais: testes, scripts)."""
    use_null = settings.DB_NULLPOOL if nullpool is None else nullpool
    return create_async_engine(url or settings.DATABASE_URL, **_engine_kwargs(bool(use_null)))


# Engine da API: um por event loop (na API há um único loop; em testes/NullPool um
# por teste). Evita o erro "attached to a different loop" do asyncpg.
_engines: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncEngine]" = weakref.WeakKeyDictionary()
_fallback_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    global _fallback_engine
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if _fallback_engine is None:
            _fallback_engine = create_engine()
        return _fallback_engine
    eng = _engines.get(loop)
    if eng is None:
        eng = create_engine()
        _engines[loop] = eng
    return eng


def session_factory(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine or get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def async_session_factory() -> AsyncSession:
    """Compat: ``async with async_session_factory() as s`` (código legado)."""
    return session_factory()()


class _EngineProxy:
    """Compat para ``from app.core.database import engine`` (main.py legado)."""

    def __getattr__(self, item):
        return getattr(get_engine(), item)


engine = _EngineProxy()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency FastAPI: commit no sucesso, rollback em qualquer erro."""
    async with session_factory()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def independent_session(lock_timeout_ms: int = 5000) -> AsyncIterator[AsyncSession]:
    """Transação separada da request, com commit próprio.

    ``lock_timeout`` evita espera infinita caso a transação da própria request
    segure um lock necessário (ex.: lock da cadeia de auditoria).
    """
    async with session_factory()() as session:
        try:
            await session.execute(text(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}ms'"))
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def worker_session(url: str | None = None) -> AsyncIterator[AsyncSession]:
    """Sessão para Celery/scripts: engine NullPool por execução (seguro p/ asyncio.run repetido)."""
    eng = create_engine(url, nullpool=True)
    try:
        async with session_factory(eng)() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
    finally:
        await eng.dispose()


# Compat: workers/scripts legados usam get_db_context()
get_db_context = worker_session


async def dispose_engines() -> None:
    for eng in list(_engines.values()):
        await eng.dispose()
    _engines.clear()
