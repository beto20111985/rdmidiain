#!/bin/sh
# =============================================================================
# RD Mídia Indoor — restaura um backup cifrado num banco.
#
#   rd-restore --file /backups/daily/rd_<db>_<ts>.dump.gpg --target-db <nome> [--create]
#              [--replace] [--allow-live-target]
#
#   --create             cria o banco destino (falha se já existir)
#   --replace            apaga o banco destino antes (somente se NÃO for o banco vivo,
#                        salvo --allow-live-target)
#   --allow-live-target  permite restaurar SOBRE o banco da instância (desastre real:
#                        parar backend/worker/beat antes — ver ops/README.md)
#
# Executa como SUPERUSUÁRIO (serviço one-shot db-restore). Restaura em transação
# única (--single-transaction: tudo ou nada). Confere o sha256 quando disponível.
# =============================================================================
set -eu
set -o pipefail
umask 077

RD_LOG_TAG="db-restore"
. /usr/local/lib/rd/rd-backup-lib.sh

FILE=""; TARGET=""; CREATE=0; REPLACE=0; ALLOW_LIVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --file) FILE="$2"; shift 2 ;;
    --target-db) TARGET="$2"; shift 2 ;;
    --create) CREATE=1; shift ;;
    --replace) REPLACE=1; shift ;;
    --allow-live-target) ALLOW_LIVE=1; shift ;;
    *) err "argumento desconhecido: $1"; exit 2 ;;
  esac
done
[ -n "$FILE" ] && [ -n "$TARGET" ] || { err "uso: rd-restore --file <arquivo> --target-db <banco> [--create] [--replace]"; exit 2; }
[ -r "$FILE" ] || { err "arquivo não encontrado: $FILE"; exit 2; }
case "$TARGET" in
  *[!A-Za-z0-9_]*) err "nome de banco inválido: $TARGET"; exit 2 ;;
esac
if [ "$TARGET" = "$RD_DB_NAME" ] && [ "$ALLOW_LIVE" -ne 1 ]; then
  err "destino é o banco vivo da instância ($RD_DB_NAME). Use --allow-live-target (procedimento de desastre)."
  exit 2
fi

SUPERUSER="${RD_DB_SUPERUSER:-${POSTGRES_USER:-postgres}}"
RD_PASSPHRASE_FILE="${RD_BACKUP_PASSPHRASE_FILE:-/run/secrets/backup_passphrase}"
RESTORE_TMP=""
restore_cleanup() {
  [ -n "$RESTORE_TMP" ] && rm -rf "$RESTORE_TMP"
  rd_cleanup_secrets
}
trap restore_cleanup EXIT
rd_setup_pgpass "$SUPERUSER" "${RD_DB_SUPERUSER_PASSWORD_FILE:-}"
rd_setup_gnupg

if [ -f "$FILE.sha256" ]; then
  expected="$(cut -d' ' -f1 < "$FILE.sha256")"
  actual="$(sha256sum "$FILE" | cut -d' ' -f1)"
  [ "$expected" = "$actual" ] || { err "sha256 divergente — arquivo corrompido"; exit 1; }
  log "sha256 conferido"
fi

# Decifra ANTES de tocar no banco destino (sem pipe — W-DB, janela da AÇÃO 002): o status do gpg
# (passphrase/integridade MDC, que num fluxo só é conhecida no FIM) decide antes de qualquer
# DROP/CREATE/restore; o pg_restore lê o arquivo. Diretório privado (umask 077), apagado no EXIT.
mkdir -p "$RD_BACKUP_DIR/.tmp"
RESTORE_TMP="$(mktemp -d "$RD_BACKUP_DIR/.tmp/restore.XXXXXX")"
PLAIN="$RESTORE_TMP/restore.dump"
rd_gpg_decrypt_to "$FILE" "$PLAIN" || { err "decifragem falhou (passphrase errada ou arquivo corrompido)"; exit 1; }
pg_restore --list "$PLAIN" > /dev/null || { err "arquivo decifrado não é um dump válido"; exit 1; }
log "decifragem e TOC conferidos"

exists="$(psql -X -w -At -d postgres -c "SELECT 1 FROM pg_database WHERE datname = '$TARGET'")"
if [ "$exists" = "1" ]; then
  if [ "$REPLACE" -eq 1 ]; then
    log "removendo banco destino existente: $TARGET"
    psql -X -w -q -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE \"$TARGET\" WITH (FORCE)"
    CREATE=1
  elif [ "$CREATE" -eq 1 ]; then
    err "banco $TARGET já existe (use --replace)"; exit 1
  fi
elif [ "$REPLACE" -eq 1 ]; then
  CREATE=1   # --replace num banco inexistente: apenas cria
fi
if [ "$exists" != "1" ] && [ "$CREATE" -ne 1 ]; then
  err "banco $TARGET não existe (use --create)"; exit 1
fi
if [ "$CREATE" -eq 1 ]; then
  owner="$SUPERUSER"
  [ "$(psql -X -w -At -d postgres -c "SELECT 1 FROM pg_roles WHERE rolname='rd_owner'")" = "1" ] && owner="rd_owner"
  psql -X -w -q -d postgres -v ON_ERROR_STOP=1 \
       -c "CREATE DATABASE \"$TARGET\" OWNER \"$owner\" TEMPLATE template0 ENCODING 'UTF8'"
  log "banco criado: $TARGET (dono $owner)"
fi

start="$(date +%s)"
pg_restore -w --single-transaction --exit-on-error -d "$TARGET" "$PLAIN"
log "restore concluído em $(( $(date +%s) - start ))s: $(basename "$FILE") → $TARGET"
