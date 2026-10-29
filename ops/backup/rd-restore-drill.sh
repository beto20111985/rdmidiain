#!/bin/sh
# =============================================================================
# RD Mídia Indoor — exercício de restore (restore testável e periódico).
#
#   rd-restore-drill [--file <arquivo.dump.gpg>]      (padrão: backup diário mais recente)
#
# 1) restaura o backup num banco DESCARTÁVEL (padrão rd_restore_test);
# 2) compara a contagem exata de linhas de cada tabela com a contagem gravada no
#    momento do backup (mesmo snapshot do pg_dump — arquivo .counts.tsv);
# 3) mostra também a contagem atual do banco vivo (informativo: pode divergir por
#    escritas posteriores ao backup);
# 4) remove o banco descartável ao final (sempre), salvo RD_DRILL_KEEP=1.
# Sai com código != 0 se qualquer tabela divergir do backup.
# =============================================================================
set -eu
umask 077

RD_LOG_TAG="restore-drill"
. /usr/local/lib/rd/rd-backup-lib.sh

DRILL_DB="${RD_DRILL_DB:-rd_restore_test}"
case "$DRILL_DB" in
  rd_restore_test*) : ;;
  *) err "o banco do exercício deve começar com rd_restore_test (recebido: $DRILL_DB)"; exit 2 ;;
esac
[ "$DRILL_DB" != "$RD_DB_NAME" ] || { err "banco do exercício não pode ser o banco vivo"; exit 2; }

FILE=""
[ "${1:-}" = "--file" ] && FILE="${2:-}"
if [ -z "$FILE" ]; then
  FILE="$(ls -1t "$RD_BACKUP_DIR"/daily/*.dump.gpg 2>/dev/null | head -n 1 || true)"
fi
[ -n "$FILE" ] && [ -r "$FILE" ] || { err "nenhum backup encontrado"; exit 1; }
COUNTS="${FILE%.dump.gpg}.counts.tsv"

SUPERUSER="${RD_DB_SUPERUSER:-${POSTGRES_USER:-postgres}}"
REPORT_DIR="$RD_BACKUP_DIR/drill"
mkdir -p "$REPORT_DIR"
REPORT="$REPORT_DIR/last_drill_report.txt"

drop_drill() {
  if [ "${RD_DRILL_KEEP:-0}" != "1" ]; then
    [ -n "${PGPASSFILE:-}" ] || rd_setup_pgpass "$SUPERUSER" "${RD_DB_SUPERUSER_PASSWORD_FILE:-}" || true
    psql -X -w -q -d postgres -c "DROP DATABASE IF EXISTS \"$DRILL_DB\" WITH (FORCE)" >/dev/null 2>&1       && log "banco descartável removido: $DRILL_DB"
  fi
  rd_cleanup_secrets
}
trap drop_drill EXIT

log "arquivo: $FILE"
log "destino descartável: $DRILL_DB"

# restore em processo filho (usa as mesmas credenciais de superusuário do ambiente)
write_status() {  # write_status <ok|failed> <divergencias> <tabelas> — lido pelo backup-metrics
  printf '{"status":"%s","at_epoch":%s,"at":"%s","backup_file":"%s","tables":%s,"divergences":%s}\n' \
    "$1" "$(date +%s)" "$(date -Iseconds)" "$(basename "$FILE")" "${3:-0}" "${2:--1}" > "$REPORT_DIR/.status.json.tmp" \
    && mv -f "$REPORT_DIR/.status.json.tmp" "$REPORT_DIR/status.json"
}

( rd-restore --file "$FILE" --target-db "$DRILL_DB" --replace ) || { err "restore falhou"; write_status failed -1 0; exit 1; }

rd_setup_pgpass "$SUPERUSER" "${RD_DB_SUPERUSER_PASSWORD_FILE:-}"

psql -X -w -At -d "$DRILL_DB" -c "$RD_COUNT_SQL" > "$REPORT_DIR/.restored.tsv"
psql -X -w -At -d "$RD_DB_NAME" -c "$RD_COUNT_SQL" > "$REPORT_DIR/.live.tsv" 2>/dev/null || : > "$REPORT_DIR/.live.tsv"

{
  echo "EXERCÍCIO DE RESTORE — $(date -Iseconds)"
  echo "backup:   $(basename "$FILE")"
  echo "sha256:   $(cut -d' ' -f1 < "$FILE.sha256" 2>/dev/null || echo n/d)"
  echo "destino:  $DRILL_DB (descartável)"
  echo
  printf '%-40s %14s %14s %14s  %s\n' "tabela" "no_backup" "restaurado" "vivo_agora" "resultado"
} > "$REPORT"

if [ -s "$COUNTS" ]; then
  if awk -F'|' -v R="$REPORT_DIR/.restored.tsv" -v L="$REPORT_DIR/.live.tsv" '
    BEGIN {
      while ((getline line < R) > 0) { split(line, a, "|"); rest[a[1]] = a[2] }
      while ((getline line < L) > 0) { split(line, a, "|"); live[a[1]] = a[2] }
    }
    {
      b = $2; r = (($1 in rest) ? rest[$1] : "AUSENTE"); l = (($1 in live) ? live[$1] : "-")
      ok = (b == r) ? "OK" : "DIVERGENTE"
      if (ok != "OK") bad++
      n++; tb += b
      printf "%-40s %14s %14s %14s  %s\n", $1, b, r, l, ok
    }
    END {
      printf "\ntabelas: %d | linhas no backup: %d | divergências: %d\n", n, tb, bad + 0
      exit (bad > 0) ? 1 : 0
    }' "$COUNTS" >> "$REPORT"; then rc=0; else rc=1; fi
else
  echo "AVISO: backup sem .counts.tsv (RD_BACKUP_COUNT_ROWS=0) — comparando restaurado × vivo" >> "$REPORT"
  if cmp -s "$REPORT_DIR/.restored.tsv" "$REPORT_DIR/.live.tsv"; then rc=0; else rc=1; fi
fi
rm -f "$REPORT_DIR/.restored.tsv" "$REPORT_DIR/.live.tsv"

DIV="$(sed -n 's/.*divergências: \([0-9][0-9]*\).*/\1/p' "$REPORT" | tail -n 1)"
TABS="$(sed -n 's/^tabelas: \([0-9][0-9]*\).*/\1/p' "$REPORT" | tail -n 1)"
if [ "$rc" -eq 0 ]; then write_status ok "${DIV:-0}" "${TABS:-0}"; else write_status failed "${DIV:--1}" "${TABS:-0}"; fi

cat "$REPORT"
if [ "$rc" -eq 0 ]; then
  log "RESULTADO: OK — contagens idênticas ao snapshot do backup"
else
  err "RESULTADO: DIVERGÊNCIA — ver $REPORT"
fi
exit "$rc"
