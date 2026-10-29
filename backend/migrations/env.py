"""
RD Mídia Indoor — Configuração do Ambiente Alembic
Suporta modo online (psycopg2 síncrono) e offline (SQL dump).
Importa todos os modelos para que o autogenerate detecte todas as tabelas.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Garantir que o diretório raiz do projeto está no path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importar settings ANTES dos modelos para resolver dependências
from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402

# Importar TODOS os modelos (registro central em app/models/__init__.py).
import app.models  # noqa: F401,E402

# Configuração do Alembic a partir do alembic.ini
config = context.config

# Sobrescrever a URL do banco com a URL síncrona das settings
# (Alembic usa psycopg2, não asyncpg)
# ALEMBIC_DATABASE_URL permite apontar para um banco descartável (testes de
# migration / harness RD_TEST_SCHEMA=alembic) sem alterar a configuração da app.
config.set_main_option(
    "sqlalchemy.url",
    (os.environ.get("ALEMBIC_DATABASE_URL") or settings.database_url_sync).replace("%", "%%"),
)

# Configuração de logging a partir do alembic.ini
if config.config_file_name is not None:
    # disable_existing_loggers=False: rodando em processo (testes, CLI), o padrão do fileConfig
    # DESATIVARIA todos os loggers já existentes (uvicorn.error, rd.*), silenciando-os até o fim
    # do processo — inclusive os de segurança.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# MetaData alvo para autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Executa migrations em modo offline (sem conexão ao banco).
    Gera SQL que pode ser executado manualmente.

    Modo offline: alembic upgrade head --sql > migration.sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Importante: incluir schemas e comparar tipos corretamente
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Executa migrations em modo online (conexão ativa ao banco).
    Usa psycopg2 síncrono (NÃO asyncpg) — Alembic não suporta async nativamente.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # NullPool: não reutilizar conexões em migrations
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Comparar tipos de colunas e defaults do servidor
            compare_type=True,
            compare_server_default=True,
            include_schemas=True,
            # Renderizar as constraints com nomes explícitos
            render_as_batch=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
