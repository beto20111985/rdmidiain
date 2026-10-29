#!/bin/sh
# =============================================================================
# RD Mídia Indoor — executa UM backup do PostgreSQL.
#   pg_dump (formato custom) → gpg AES256 simétrico → /backups/<tag>/
#   + verificação (decifra e lista o TOC com pg_restore --list)
#   + sha256 + manifesto JSON + contagem exata de linhas no MESMO snapshot do dump
#   + promoção semanal/mensal (hardlink) e retenção 7 diários / 4 semanais / 3 mensais
#   + gancho opcional de cópia externa (fora da VPS)
#
# Variáveis:
#   RD_DB_NAME, PGHOST, PGPORT
#   RD_BACKUP_DB_USER (padrão rd_backup), RD_BACKUP_DB_PASSWORD_FILE
#   RD_BACKUP_PASSPHRASE_FILE (padrão /run/secrets/backup_passphrase)
#   RD_BACKUP_DIR (padrão /backups), RD_BACKUP_TAG (daily|drill|manual; padrão daily)
#   RD_BACKUP_KEEP_DAILY=7 RD_BACKUP_KEEP_WEEKLY=4 RD_BACKUP_KEEP_MONTHLY=3
#   RD_BACKUP_COUNT_ROWS=1   (0 desliga a contagem exata — bancos muito grandes)
#   RD_BACKUP_OFFSITE_CMD    comando executado com o arquivo cifrado como $1
#                            (ex.: rclone/scp para destino FORA da VPS; dependência externa)
# =============================================================================
set -eu
set -o pipefail
umask 077

. /usr/local/lib/rd/rd-backup-lib.sh

TAG="${RD_BACKUP_TAG:-daily}"
DB_USER="${RD_BACKUP_DB_USER:-rd_backup}"
DB_PWFILE="${RD_BACKUP_DB_PASSWORD_FILE:-/run/secrets/db_backup_password}"
RD_PASSPHRASE_FILE="${RD_BACKUP_PASSPHRASE_FILE:-/run/secrets/backup_passphrase}"
KEEP_DAILY="${RD_BACKUP_KEEP_DAILY:-7}"
KEEP_WEEKLY="${RD_BACKUP_KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${RD_BACKUP_KEEP_MONTHLY:-3}"
COUNT_ROWS="${RD_BACKUP_COUNT_ROWS:-1}"

[ -r "$RD_PASSPHRASE_FILE" ] || { err "passphrase ausente: $RD_PASSPHRASE_FILE"; exit 1; }

STARTED_EPOCH="$(date +%s)"
TS="$(date +%Y%m%dT%H%M%S)"
NAME="rd_${RD_DB_NAME}_${TS}"
WORK="$RD_BACKUP_DIR/.tmp/$NAME"
DEST="$RD_BACKUP_DIR/$TAG"
mkdir -p "$WORK" "$DEST" "$RD_BACKUP_DIR/weekly" "$RD_BACKUP_DIR/monthly"

# Marcadores de saúde: só backups de rotação (daily/manual) alimentam o healthcheck;
# o exercício de restore (drill) tem status próprio.
if [ "$TAG" = "drill" ]; then STATUS_DIR="$DEST"; else STATUS_DIR="$RD_BACKUP_DIR"; fi

PSQL_PID=""
cleanup() {
  [ -n "$PSQL_PID" ] && kill "$PSQL_PID" 2>/dev/null || true
  { exec 3>&-; } 2>/dev/null || true
  rm -rf "$WORK"
  rd_cleanup_secrets
}
fail() {
  err "$*"
  date +%s > "$STATUS_DIR/.last_failure"
  printf '{"status":"failed","tag":"%s","at":"%s","error":"%s"}\n' "$TAG" "$(date -Iseconds)" "$(printf '%s' "$*" | tr -d '"\\' | head -c 300)" > "$STATUS_DIR/status.json"
  exit 1
}
trap cleanup EXIT
trap 'fail "interrompido"' INT TERM

rd_setup_pgpass "$DB_USER" "$DB_PWFILE" || fail "credencial do banco"
rd_setup_gnupg

OUT="$WORK/$NAME.dump.gpg"
log "início: banco=$RD_DB_NAME host=$PGHOST tag=$TAG usuário=$DB_USER contagem=$COUNT_ROWS"

if [ "$COUNT_ROWS" = "1" ]; then
  # Sessão psql mantida aberta com um snapshot exportado: o pg_dump e a contagem de
  # linhas enxergam exatamente o mesmo estado do banco (comparação exata no restore).
  FIFO="$WORK/psql.fifo"
  mkfifo "$FIFO"
  psql -X -w -q -At -v ON_ERROR_STOP=1 -d "$RD_DB_NAME" < "$FIFO" > "$WORK/psql.out" 2> "$WORK/psql.err" &
  PSQL_PID=$!
  exec 3> "$FIFO"
  printf '%s\n' "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;" \
                "SELECT pg_export_snapshot() \\g $WORK/snapshot.txt" >&3
  i=0
  while [ ! -s "$WORK/snapshot.txt" ]; do
    i=$((i + 1))
    if [ "$i" -gt 300 ] || ! kill -0 "$PSQL_PID" 2>/dev/null; then
      fail "não foi possível exportar snapshot ($(head -c 300 "$WORK/psql.err" 2>/dev/null))"
    fi
    sleep 0.1
  done
  SNAP="$(tr -d '\r\n' < "$WORK/snapshot.txt")"
  pg_dump -w -Fc -Z 6 --snapshot="$SNAP" -d "$RD_DB_NAME" 2> "$WORK/pg_dump.err" | rd_gpg_encrypt "$OUT" \
    || fail "pg_dump/gpg falhou ($(head -c 500 "$WORK/pg_dump.err"))"
  printf '%s\n' "$RD_COUNT_SQL \\g $WORK/counts.tsv" "COMMIT;" "\\q" >&3
  exec 3>&-
  wait "$PSQL_PID" || fail "sessão de snapshot falhou ($(head -c 300 "$WORK/psql.err"))"
  PSQL_PID=""
  [ -s "$WORK/counts.tsv" ] || fail "contagem de linhas vazia"
else
  pg_dump -w -Fc -Z 6 -d "$RD_DB_NAME" 2> "$WORK/pg_dump.err" | rd_gpg_encrypt "$OUT" \
    || fail "pg_dump/gpg falhou ($(head -c 500 "$WORK/pg_dump.err"))"
fi

# Verificação (detecta arquivo truncado/corrompido/passphrase errada).
# SEM pipe: decifra para arquivo no workspace privado ($WORK, umask 077, apagado no EXIT) e
# confere o status do gpg (integridade MDC), depois o pg_restore lê o ARQUIVO: TOC completo
# (--list) e leitura de TODAS as entradas (-f /dev/null). Antes era `gpg | pg_restore --list`:
# o pg_restore encerra após o TOC e, com dump maior que o buffer do pipe, o gpg recebia EPIPE
# e o backup falhava (janela integrada da AÇÃO 002, dump ~8 MB — correção W-DB). Custo: espaço
# temporário igual ao tamanho do dump no volume de backups.
VERIFY="$WORK/verify.dump"
rd_gpg_decrypt_to "$OUT" "$VERIFY" 2> "$WORK/verify.err" \
  || fail "verificação: decifragem falhou ($(head -c 300 "$WORK/verify.err"))"
pg_restore --list "$VERIFY" > "$WORK/toc.txt" 2>> "$WORK/verify.err" \
  || fail "verificação: TOC ilegível ($(head -c 300 "$WORK/verify.err"))"
pg_restore -f /dev/null "$VERIFY" 2>> "$WORK/verify.err" \
  || fail "verificação: conteúdo ilegível ($(head -c 300 "$WORK/verify.err"))"
rm -f "$VERIFY"
TOC_ENTRIES="$(grep -c -v '^;' "$WORK/toc.txt" || true)"
TABLE_DATA="$(grep -c ' TABLE DATA ' "$WORK/toc.txt" || true)"

SIZE="$(wc -c < "$OUT" | tr -d ' ')"
SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
printf '%s  %s\n' "$SHA" "$NAME.dump.gpg" > "$WORK/$NAME.dump.gpg.sha256"
SERVER_VER="$(psql -X -w -At -d "$RD_DB_NAME" -c 'SHOW server_version' 2>/dev/null || echo desconhecida)"
DUMP_VER="$(pg_dump --version | awk '{print $NF}')"
ROWS_TOTAL="null"
if [ -s "$WORK/counts.tsv" ]; then
  cp "$WORK/counts.tsv" "$WORK/$NAME.counts.tsv"
  ROWS_TOTAL="$(awk -F'|' '{s+=$2} END {print s+0}' "$WORK/counts.tsv")"
fi
FINISHED_EPOCH="$(date +%s)"
cat > "$WORK/$NAME.manifest.json" <<EOF
{"file":"$NAME.dump.gpg","database":"$RD_DB_NAME","tag":"$TAG","created_at":"$(date -Iseconds)","duration_s":$((FINISHED_EPOCH - STARTED_EPOCH)),"size_bytes":$SIZE,"sha256":"$SHA","format":"pg_dump custom (-Fc -Z6)","encryption":"gpg symmetric AES256 (s2k SHA512)","pg_dump_version":"$DUMP_VER","server_version":"$SERVER_VER","toc_entries":$TOC_ENTRIES,"table_data_entries":$TABLE_DATA,"rows_total":$ROWS_TOTAL,"snapshot_consistent_counts":$( [ "$COUNT_ROWS" = "1" ] && echo true || echo false )}
EOF

# Publica no destino (mv atômico dentro do mesmo volume)
for f in "$NAME.dump.gpg" "$NAME.dump.gpg.sha256" "$NAME.manifest.json" "$NAME.counts.tsv"; do
  if [ -f "$WORK/$f" ]; then mv "$WORK/$f" "$DEST/$f"; fi
done
FINAL="$DEST/$NAME.dump.gpg"
log "ok: $FINAL ($SIZE bytes, sha256=$SHA, toc=$TOC_ENTRIES, linhas=$ROWS_TOTAL)"

# Retenção (somente backups diários agendados/manuais entram na rotação)
promote() {  # promote <dir> → hardlink dos arquivos do backup atual
  for f in "$NAME.dump.gpg" "$NAME.dump.gpg.sha256" "$NAME.manifest.json" "$NAME.counts.tsv"; do
    if [ -f "$DEST/$f" ]; then
      ln "$DEST/$f" "$1/$f" 2>/dev/null || cp -p "$DEST/$f" "$1/$f"
    fi
  done
  return 0
}
prune() {  # prune <dir> <manter>
  ls -1t "$1"/*.dump.gpg 2>/dev/null | tail -n +"$(( $2 + 1 ))" | while read -r old; do
    base="${old%.dump.gpg}"
    rm -f "$base.dump.gpg" "$base.dump.gpg.sha256" "$base.manifest.json" "$base.counts.tsv"
    log "retenção: removido $(basename "$old")"
  done
  return 0
}
if [ "$TAG" = "daily" ]; then
  if [ -z "$(find "$RD_BACKUP_DIR/weekly" -name '*.dump.gpg' -mtime -6 2>/dev/null)" ]; then
    promote "$RD_BACKUP_DIR/weekly";  log "promovido a semanal"
  fi
  if [ -z "$(find "$RD_BACKUP_DIR/monthly" -name '*.dump.gpg' -mtime -27 2>/dev/null)" ]; then
    promote "$RD_BACKUP_DIR/monthly"; log "promovido a mensal"
  fi
  prune "$RD_BACKUP_DIR/daily"   "$KEEP_DAILY"
  prune "$RD_BACKUP_DIR/weekly"  "$KEEP_WEEKLY"
  prune "$RD_BACKUP_DIR/monthly" "$KEEP_MONTHLY"
elif [ "$TAG" = "drill" ]; then
  prune "$DEST" 2
fi

# Cópia externa (fora da VPS) — dependência externa; ausência é registrada, não mascarada
OFFSITE="not_configured"
if [ -n "${RD_BACKUP_OFFSITE_CMD:-}" ]; then
  if sh -c "$RD_BACKUP_OFFSITE_CMD" rd-offsite "$FINAL" > "$RD_BACKUP_DIR/.offsite.log" 2>&1; then
    OFFSITE="ok"; log "cópia externa: ok"
  else
    OFFSITE="failed"; err "cópia externa FALHOU (ver $RD_BACKUP_DIR/.offsite.log)"
  fi
else
  log "cópia externa: NÃO CONFIGURADA (RD_BACKUP_OFFSITE_CMD vazio — dependência externa)"
fi

date +%s > "$STATUS_DIR/.last_success"
printf '{"status":"ok","tag":"%s","at":"%s","file":"%s","size_bytes":%s,"sha256":"%s","rows_total":%s,"offsite":"%s"}\n' \
  "$TAG" "$(date -Iseconds)" "$TAG/$NAME.dump.gpg" "$SIZE" "$SHA" "$ROWS_TOTAL" "$OFFSITE" > "$STATUS_DIR/status.json"
echo "$FINAL"
