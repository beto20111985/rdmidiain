-- =============================================================================
-- RD Mídia Indoor — papéis do PostgreSQL (IDEMPOTENTE). Executado por ops/db/db-init.sh
-- (serviço one-shot `db-init`) como SUPERUSUÁRIO, conectado ao banco da instância.
--
-- Papéis:
--   rd_owner  — dono do schema/objetos; usado SOMENTE pelas migrations (Alembic, serviço `migrate`).
--   rd_app    — runtime da API/workers: apenas DML (SELECT/INSERT/UPDATE/DELETE). Sem DDL,
--               sem TRUNCATE, sem superuser, sem BYPASSRLS.
--   rd_backup — leitura total para pg_dump (pg_read_all_data + BYPASSRLS), sem escrita.
--
-- Tabelas append-only: o REVOKE UPDATE, DELETE, TRUNCATE ... FROM rd_app é aplicado pelas
-- MIGRATIONS do backend (lista esperada em ops/README.md: audit_log(s), ledger_*,
-- webhook_events, eventos brutos de playback após ingestão). Este script NUNCA
-- re-concede privilégios a uma tabela em que rd_app já tenha entrada explícita de ACL,
-- para não desfazer esses REVOKEs ao ser re-executado.
--
-- Variáveis psql (-v): db_name, superuser, transfer_ownership (on|off)
-- Variáveis de ambiente (lidas com \getenv, nunca na linha de comando):
--   RD_OWNER_PW, RD_APP_PW, RD_BACKUP_PW
-- =============================================================================
\set ON_ERROR_STOP on
\set QUIET on

-- Não registrar o texto destes comandos (contêm senhas) nos logs do servidor.
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SET lock_timeout = '5s';
SET statement_timeout = '120s';

\getenv owner_pw  RD_OWNER_PW
\getenv app_pw    RD_APP_PW
\getenv backup_pw RD_BACKUP_PW

-- ─── 1. Papéis ───────────────────────────────────────────────────────────────
SELECT format('CREATE ROLE %I LOGIN', r)
  FROM unnest(ARRAY['rd_owner', 'rd_app', 'rd_backup']) AS r
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
\gexec

ALTER ROLE rd_owner  WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT PASSWORD :'owner_pw';
ALTER ROLE rd_app    WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT PASSWORD :'app_pw';
ALTER ROLE rd_backup WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS   INHERIT PASSWORD :'backup_pw';

-- Proteções de runtime (evita transações presas segurando locks — A2)
ALTER ROLE rd_app    SET idle_in_transaction_session_timeout = '5min';
ALTER ROLE rd_backup SET default_transaction_read_only = on;

GRANT pg_read_all_data TO rd_backup;

-- ─── 2. Banco ────────────────────────────────────────────────────────────────
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db_name" TO rd_owner, rd_app;
GRANT CONNECT            ON DATABASE :"db_name" TO rd_backup;
-- rd_owner (migrations) pode criar extensões "trusted" (ex.: pgcrypto) e schemas
GRANT CREATE             ON DATABASE :"db_name" TO rd_owner;

-- ─── 3. Schema public ───────────────────────────────────────────────────────
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO rd_owner;
GRANT USAGE         ON SCHEMA public TO rd_app, rd_backup;

-- ─── 4. Privilégios padrão para objetos FUTUROS ──────────────────────────────
-- Objetos criados pelas migrations como rd_owner:
ALTER DEFAULT PRIVILEGES FOR ROLE rd_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rd_owner IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO rd_app;
-- Transitório: enquanto migrations ainda rodarem como o superusuário legado.
ALTER DEFAULT PRIVILEGES FOR ROLE :"superuser" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rd_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :"superuser" IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO rd_app;

-- ─── 5. Objetos EXISTENTES: concede só onde rd_app ainda não tem ACL explícita ─
DO $rd$
DECLARE
  r record;
  app_oid oid := (SELECT oid FROM pg_roles WHERE rolname = 'rd_app');
  n_tab int := 0;
  n_seq int := 0;
BEGIN
  FOR r IN
    SELECT c.oid, c.relkind, format('%I.%I', n.nspname, c.relname) AS fq
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
       AND NOT EXISTS (
             SELECT 1 FROM aclexplode(COALESCE(c.relacl, acldefault(
                      CASE WHEN c.relkind = 'S' THEN 's'::"char" ELSE 'r'::"char" END, c.relowner))) a
              WHERE a.grantee = app_oid)
  LOOP
    IF r.relkind = 'S' THEN
      EXECUTE format('GRANT USAGE, SELECT, UPDATE ON SEQUENCE %s TO rd_app', r.fq);
      n_seq := n_seq + 1;
    ELSE
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %s TO rd_app', r.fq);
      n_tab := n_tab + 1;
    END IF;
  END LOOP;
  RAISE NOTICE 'rd_app: privilégios concedidos em % tabela(s)/view(s) e % sequência(s) novas', n_tab, n_seq;
END
$rd$;

-- ─── 6. (Opcional) transferência de propriedade para rd_owner ────────────────
-- Executada só com -v transfer_ownership=on (janela integrada / instalação nova).
-- ALTER ... OWNER exige lock exclusivo: lock_timeout curto e falhas reportadas
-- (re-executar o db-init conclui o que ficou pendente).
\if :transfer_ownership
DO $rd$
DECLARE
  r record;
  ok int := 0;
  pend int := 0;
BEGIN
  -- tabelas, views, matviews, foreign tables e sequências avulsas
  FOR r IN
    SELECT c.relkind, format('%I.%I', n.nspname, c.relname) AS fq
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
       AND pg_get_userbyid(c.relowner) <> 'rd_owner'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
       -- sequência "owned by" coluna acompanha a tabela automaticamente
       AND NOT (c.relkind = 'S' AND EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a', 'i')))
     ORDER BY CASE WHEN c.relkind = 'S' THEN 1 ELSE 0 END
  LOOP
    BEGIN
      EXECUTE format('ALTER %s %s OWNER TO rd_owner',
        CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                       WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
        r.fq);
      ok := ok + 1;
    EXCEPTION WHEN lock_not_available THEN
      pend := pend + 1;
      RAISE WARNING 'lock indisponível em % — re-executar db-init', r.fq;
    END;
  END LOOP;

  -- funções/procedimentos do schema (exceto membros de extensões)
  FOR r IN
    SELECT p.oid::regprocedure::text AS sig, p.prokind
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND pg_get_userbyid(p.proowner) <> 'rd_owner'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO rd_owner',
                   CASE r.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END,
                   r.sig);
    ok := ok + 1;
  END LOOP;

  -- tipos avulsos (enum, domínio, composto avulso, range), exceto membros de extensões
  FOR r IN
    SELECT format('%I.%I', n.nspname, t.typname) AS fq, t.typtype
      FROM pg_type t
      JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE n.nspname = 'public'
       AND t.typtype IN ('e', 'd', 'r', 'c')
       AND (t.typtype <> 'c' OR (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c')
       AND pg_get_userbyid(t.typowner) <> 'rd_owner'
       AND NOT EXISTS (SELECT 1 FROM pg_depend d
                        WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO rd_owner',
                   CASE r.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, r.fq);
    ok := ok + 1;
  END LOOP;

  RAISE NOTICE 'rd_owner: % objeto(s) transferidos, % pendente(s) por lock', ok, pend;
END
$rd$;
\else
\echo 'transferência de propriedade: DESLIGADA (transfer_ownership=off)'
\endif

-- ─── 7. Verificação (sem segredos) ──────────────────────────────────────────
\unset QUIET
\echo '--- papéis'
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin
  FROM pg_roles WHERE rolname IN ('rd_owner', 'rd_app', 'rd_backup', :'superuser') ORDER BY 1;
\echo '--- resumo de privilégios de rd_app no schema public'
SELECT count(*) FILTER (WHERE has_table_privilege('rd_app', c.oid, 'SELECT'))   AS tabelas_select,
       count(*) FILTER (WHERE has_table_privilege('rd_app', c.oid, 'INSERT'))   AS tabelas_insert,
       count(*) FILTER (WHERE has_table_privilege('rd_app', c.oid, 'TRUNCATE')) AS tabelas_truncate,
       count(*)                                                                  AS tabelas_total,
       has_schema_privilege('rd_app', 'public', 'CREATE')                       AS app_pode_ddl,
       count(*) FILTER (WHERE pg_get_userbyid(c.relowner) = 'rd_owner')         AS dono_rd_owner
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p');
