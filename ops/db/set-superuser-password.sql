-- =============================================================================
-- Alinha a senha do SUPERUSUÁRIO do PostgreSQL ao Docker secret
-- /run/secrets/postgres_superuser_password (volume já inicializado: o
-- POSTGRES_PASSWORD_FILE da imagem só vale no initdb). Usado na janela integrada
-- e em rotações. Executar DENTRO do container postgres (socket local):
--
--   docker compose exec -T postgres sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' < ops/db/set-superuser-password.sql
--
-- A senha é lida do arquivo pelo próprio psql (backquote) — nunca aparece em
-- linha de comando, variável de ambiente ou log do servidor.
-- =============================================================================
\set QUIET on
\set pw `cat /run/secrets/postgres_superuser_password`
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
ALTER ROLE CURRENT_USER PASSWORD :'pw';
\unset pw
\unset QUIET
\echo 'senha do superusuário alinhada ao secret postgres_superuser_password'
