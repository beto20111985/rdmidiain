#!/bin/sh
# =============================================================================
# RD Mídia Indoor — db-init (one-shot, imagem postgres:16-alpine).
# Cria/atualiza os papéis rd_owner / rd_app / rd_backup (ops/db/roles.sql).
# Idempotente: pode rodar a cada `docker compose up`.
#
# Entradas:
#   PGHOST, PGPORT                       (padrão postgres:5432)
#   RD_DB_NAME                           banco da instância (padrão $POSTGRES_DB)
#   RD_DB_SUPERUSER                      superusuário (padrão $POSTGRES_USER)
#   RD_DB_SUPERUSER_PASSWORD_FILE        senha do superusuário (Docker secret) — preferido
#   POSTGRES_PASSWORD                    fallback TRANSITÓRIO (dev atual via .env)
#   RD_DB_TRANSFER_OWNERSHIP=on|off      transfere objetos para rd_owner (padrão off)
#   /run/secrets/db_owner_password, db_app_password, db_backup_password
# Nunca imprime senhas.
# =============================================================================
set -eu
umask 077

PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"
DB_NAME="${RD_DB_NAME:-${POSTGRES_DB:-rdmidiaindoor}}"
SUPERUSER="${RD_DB_SUPERUSER:-${POSTGRES_USER:-postgres}}"
TRANSFER="${RD_DB_TRANSFER_OWNERSHIP:-off}"
SECRETS_DIR="${RD_SECRETS_PATH:-/run/secrets}"

read_secret() {
  if [ ! -r "$1" ]; then
    echo "[db-init] ERRO: segredo ausente/ilegível: $1" >&2
    exit 1
  fi
  # remove CR/LF finais
  tr -d '\r\n' < "$1"
}

if [ -n "${RD_DB_SUPERUSER_PASSWORD_FILE:-}" ] && [ -r "${RD_DB_SUPERUSER_PASSWORD_FILE}" ]; then
  SUPERPASS="$(read_secret "$RD_DB_SUPERUSER_PASSWORD_FILE")"
  SRC="secret"
elif [ -n "${POSTGRES_PASSWORD:-}" ]; then
  SUPERPASS="$POSTGRES_PASSWORD"
  SRC="env (transitório)"
else
  echo "[db-init] ERRO: senha do superusuário não fornecida" >&2
  exit 1
fi
unset POSTGRES_PASSWORD

# .pgpass temporário (escapa '\' e ':'), removido ao sair
PGPASSFILE="$(mktemp)"
trap 'rm -f "$PGPASSFILE"' EXIT INT TERM
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g'; }
printf '%s:%s:*:%s:%s\n' "$PGHOST" "$PGPORT" "$(esc "$SUPERUSER")" "$(esc "$SUPERPASS")" > "$PGPASSFILE"
chmod 600 "$PGPASSFILE"
unset SUPERPASS
export PGPASSFILE PGHOST PGPORT

RD_OWNER_PW="$(read_secret "$SECRETS_DIR/db_owner_password")"
RD_APP_PW="$(read_secret "$SECRETS_DIR/db_app_password")"
RD_BACKUP_PW="$(read_secret "$SECRETS_DIR/db_backup_password")"
export RD_OWNER_PW RD_APP_PW RD_BACKUP_PW

echo "[db-init] banco=$DB_NAME host=$PGHOST:$PGPORT superusuário=$SUPERUSER (senha via $SRC) transfer_ownership=$TRANSFER"

i=0
until pg_isready -q -d "$DB_NAME" -U "$SUPERUSER"; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "[db-init] ERRO: PostgreSQL indisponível após 60 tentativas" >&2
    exit 1
  fi
  sleep 2
done

psql -X -w -U "$SUPERUSER" -d "$DB_NAME" \
     -v ON_ERROR_STOP=1 \
     -v db_name="$DB_NAME" \
     -v superuser="$SUPERUSER" \
     -v transfer_ownership="$TRANSFER" \
     -f "${RD_ROLES_SQL:-/opt/rd/db/roles.sql}"

echo "[db-init] concluído"
