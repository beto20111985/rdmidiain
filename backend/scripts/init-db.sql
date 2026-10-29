-- =============================================================================
-- RD Mídia Indoor — Script de Inicialização do Banco de Dados PostgreSQL
-- Execute este script ANTES de rodar as migrations Alembic
-- Requer conexão como superuser (postgres) ao banco de dados alvo
-- =============================================================================

-- Extensões necessárias
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";    -- Geração de UUIDs
CREATE EXTENSION IF NOT EXISTS "pg_trgm";      -- Busca trigram (LIKE eficiente)
CREATE EXTENSION IF NOT EXISTS "btree_gin";    -- Índices GIN para tipos btree
CREATE EXTENSION IF NOT EXISTS "unaccent";     -- Remoção de acentos para busca

-- =============================================================================
-- Configurações de locale e timezone do banco
-- =============================================================================

-- Configurar timezone padrão para UTC
-- Obs: altere no postgresql.conf para persistir além da sessão
SET timezone = 'UTC';

-- =============================================================================
-- Verificar se o banco está com encoding correto
-- =============================================================================

DO $$
BEGIN
    IF current_setting('server_encoding') != 'UTF8' THEN
        RAISE EXCEPTION 'O banco deve estar com encoding UTF8. Atual: %', current_setting('server_encoding');
    END IF;
END $$;

-- =============================================================================
-- Configurações de performance recomendadas
-- Estas configurações devem ser ajustadas no postgresql.conf para produção.
-- Aqui são apenas exemplos de referência:
--
-- shared_buffers = 256MB         (25% da RAM disponível)
-- effective_cache_size = 1GB     (75% da RAM disponível)
-- work_mem = 16MB
-- maintenance_work_mem = 256MB
-- random_page_cost = 1.1         (para SSD)
-- checkpoint_completion_target = 0.9
-- wal_buffers = 16MB
-- max_connections = 100
-- =============================================================================

-- =============================================================================
-- Schema padrão (public) — confirmar que existe
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS public;
GRANT ALL ON SCHEMA public TO current_user;

-- =============================================================================
-- Funções utilitárias
-- =============================================================================

-- Função para atualizar updated_at automaticamente via trigger
-- Usar em tabelas que não usam SQLAlchemy onupdate (ex: tabelas legadas)
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW() AT TIME ZONE 'UTC';
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Função para busca case-insensitive sem acentos (usando pg_trgm + unaccent)
CREATE OR REPLACE FUNCTION normalize_search(text_input TEXT)
RETURNS TEXT AS $$
BEGIN
    RETURN lower(unaccent(text_input));
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

-- =============================================================================
-- Notas para particionamento futuro de playback_events
-- =============================================================================
-- A tabela playback_events está preparada para particionamento por 'started_at'.
-- Para habilitar particionamento em produção quando o volume justificar:
--
-- 1. Renomear a tabela atual:
--    ALTER TABLE playback_events RENAME TO playback_events_legacy;
--
-- 2. Criar tabela particionada:
--    CREATE TABLE playback_events (
--        LIKE playback_events_legacy INCLUDING ALL
--    ) PARTITION BY RANGE (started_at);
--
-- 3. Criar partições mensais:
--    CREATE TABLE playback_events_2024_01
--        PARTITION OF playback_events
--        FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');
--
-- 4. Usar pg_partman para automação de partições futuras.
-- =============================================================================

-- Confirmação de inicialização bem-sucedida
DO $$
BEGIN
    RAISE NOTICE 'RD Mídia Indoor: inicialização do banco concluída com sucesso.';
    RAISE NOTICE 'Extensões instaladas: uuid-ossp, pg_trgm, btree_gin, unaccent';
    RAISE NOTICE 'Execute: alembic upgrade head  para criar as tabelas.';
END $$;
