#!/bin/sh
# =============================================================================
# RD Mídia Indoor — exportador de métricas de BACKUP / RESTORE DRILL (sidecar).
#
# Lê SOMENTE metadados do volume de backups (montado read-only): marcadores
# .last_success/.last_failure/.scheduler_started, status.json, contagem de arquivos
# por camada e o status do último exercício de restore (drill/). Nunca lê/serve os
# dumps nem segredos. Gera um arquivo de texto no formato Prometheus em tmpfs e o
# serve com o httpd do busybox (porta interna RD_METRICS_PORT, padrão 9101,
# caminho /metrics.prom). Sem consultas ao banco; custo desprezível.
#
# Variáveis: RD_BACKUP_DIR (/backups), RD_METRICS_PORT (9101), RD_METRICS_INTERVAL_S (30),
#   RD_BACKUP_MAX_AGE_H (26), RD_DRILL_MAX_AGE_DAYS (35), RD_BACKUP_OFFSITE_REQUIRED (false)
# =============================================================================
set -eu

D="${RD_BACKUP_DIR:-/backups}"
PORT="${RD_METRICS_PORT:-9101}"
INTERVAL="${RD_METRICS_INTERVAL_S:-30}"
MAX_AGE_S=$(( ${RD_BACKUP_MAX_AGE_H:-26} * 3600 ))
DRILL_MAX_AGE_S=$(( ${RD_DRILL_MAX_AGE_DAYS:-35} * 86400 ))
case "${RD_BACKUP_OFFSITE_REQUIRED:-false}" in true|1|sim|yes) OFF_REQ=1 ;; *) OFF_REQ=0 ;; esac

WEB="${RD_METRICS_WEB_DIR:-/tmp/rd-metrics}"   # tmpfs; só este diretório é servido
CONF=/tmp/rd-httpd.conf
mkdir -p "$WEB"
printf '.prom:text/plain; version=0.0.4; charset=utf-8\n' > "$CONF"

num() {  # num <arquivo> → inteiro do arquivo ou 0
  if [ -r "$1" ]; then v="$(tr -cd '0-9' < "$1")"; echo "${v:-0}"; else echo 0; fi
}
jget() {  # jget <arquivo.json> <campo> → valor (string sem aspas) ou vazio
  [ -r "$1" ] && jq -r --arg k "$2" '.[$k] // empty' "$1" 2>/dev/null || true
}
count_tier() { ls -1 "$D/$1"/*.dump.gpg 2>/dev/null | wc -l | tr -d ' '; }

render() {
  ok_ts="$(num "$D/.last_success")"
  fail_ts="$(num "$D/.last_failure")"
  started="$(num "$D/.scheduler_started")"
  st="$(jget "$D/status.json" status)"
  size="$(jget "$D/status.json" size_bytes)"; size="${size:-0}"
  rows="$(jget "$D/status.json" rows_total)"; case "$rows" in ''|null) rows=0 ;; esac
  off="$(jget "$D/status.json" offsite)"
  last_ok=1
  if [ "$fail_ts" -gt "$ok_ts" ] || [ "$st" = "failed" ]; then last_ok=0; fi
  [ "$ok_ts" -eq 0 ] && [ "$fail_ts" -eq 0 ] && last_ok=1   # instalação nova: nada falhou ainda
  off_conf=1; off_ok=0
  case "$off" in ok) off_ok=1 ;; not_configured|'') off_conf=0 ;; esac

  # restore drill: status.json (scripts novos) ou relatório legado
  dr_ts=0; dr_ok=0; dr_div=-1
  if [ -r "$D/drill/status.json" ]; then
    dr_ts="$(jget "$D/drill/status.json" at_epoch)"; dr_ts="${dr_ts:-0}"
    [ "$(jget "$D/drill/status.json" status)" = "ok" ] && dr_ok=1
    dr_div="$(jget "$D/drill/status.json" divergences)"; dr_div="${dr_div:--1}"
  elif [ -r "$D/drill/last_drill_report.txt" ]; then
    dr_ts="$(date -r "$D/drill/last_drill_report.txt" +%s)"
    dr_div="$(sed -n 's/.*divergências: \([0-9][0-9]*\).*/\1/p' "$D/drill/last_drill_report.txt" | tail -n 1)"
    dr_div="${dr_div:--1}"
    [ "$dr_div" = "0" ] && dr_ok=1
  fi

  set -- $(df -Pk "$D" 2>/dev/null | awk 'NR==2 {print $2, $4}')
  vol_size=$(( ${1:-0} * 1024 )); vol_avail=$(( ${2:-0} * 1024 ))

  tmp="$WEB/.metrics.prom.tmp"
  {
    echo "# HELP rd_backup_last_success_timestamp_seconds Ultimo backup concluido com sucesso (unix s; 0 = nunca)"
    echo "# TYPE rd_backup_last_success_timestamp_seconds gauge"
    echo "rd_backup_last_success_timestamp_seconds $ok_ts"
    echo "# HELP rd_backup_last_failure_timestamp_seconds Ultima falha de backup (unix s; 0 = nunca)"
    echo "# TYPE rd_backup_last_failure_timestamp_seconds gauge"
    echo "rd_backup_last_failure_timestamp_seconds $fail_ts"
    echo "# HELP rd_backup_last_run_success 1 se a ultima execucao de backup teve sucesso"
    echo "# TYPE rd_backup_last_run_success gauge"
    echo "rd_backup_last_run_success $last_ok"
    echo "# HELP rd_backup_last_size_bytes Tamanho do ultimo backup cifrado"
    echo "# TYPE rd_backup_last_size_bytes gauge"
    echo "rd_backup_last_size_bytes $size"
    echo "# HELP rd_backup_last_rows_total Linhas no snapshot do ultimo backup"
    echo "# TYPE rd_backup_last_rows_total gauge"
    echo "rd_backup_last_rows_total $rows"
    echo "# HELP rd_backup_files Backups retidos por camada"
    echo "# TYPE rd_backup_files gauge"
    for t in daily weekly monthly; do echo "rd_backup_files{tier=\"$t\"} $(count_tier $t)"; done
    echo "# HELP rd_backup_scheduler_started_timestamp_seconds Inicio do agendador de backup (unix s)"
    echo "# TYPE rd_backup_scheduler_started_timestamp_seconds gauge"
    echo "rd_backup_scheduler_started_timestamp_seconds $started"
    echo "# HELP rd_backup_max_age_seconds Idade maxima aceita do ultimo backup (configuracao)"
    echo "# TYPE rd_backup_max_age_seconds gauge"
    echo "rd_backup_max_age_seconds $MAX_AGE_S"
    echo "# HELP rd_backup_offsite_required 1 se a copia fora da VPS e obrigatoria nesta instancia"
    echo "# TYPE rd_backup_offsite_required gauge"
    echo "rd_backup_offsite_required $OFF_REQ"
    echo "# HELP rd_backup_offsite_configured 1 se o gancho de copia externa esta configurado"
    echo "# TYPE rd_backup_offsite_configured gauge"
    echo "rd_backup_offsite_configured $off_conf"
    echo "# HELP rd_backup_offsite_last_success 1 se a copia externa do ultimo backup teve sucesso"
    echo "# TYPE rd_backup_offsite_last_success gauge"
    echo "rd_backup_offsite_last_success $off_ok"
    echo "# HELP rd_backup_restore_drill_last_timestamp_seconds Ultimo exercicio de restore (unix s; 0 = nunca)"
    echo "# TYPE rd_backup_restore_drill_last_timestamp_seconds gauge"
    echo "rd_backup_restore_drill_last_timestamp_seconds $dr_ts"
    echo "# HELP rd_backup_restore_drill_last_success 1 se o ultimo exercicio de restore conferiu sem divergencias"
    echo "# TYPE rd_backup_restore_drill_last_success gauge"
    echo "rd_backup_restore_drill_last_success $dr_ok"
    echo "# HELP rd_backup_restore_drill_divergences Tabelas divergentes no ultimo exercicio (-1 = desconhecido)"
    echo "# TYPE rd_backup_restore_drill_divergences gauge"
    echo "rd_backup_restore_drill_divergences $dr_div"
    echo "# HELP rd_backup_restore_drill_max_age_seconds Periodo maximo entre exercicios de restore (configuracao)"
    echo "# TYPE rd_backup_restore_drill_max_age_seconds gauge"
    echo "rd_backup_restore_drill_max_age_seconds $DRILL_MAX_AGE_S"
    echo "# HELP rd_backup_volume_size_bytes Tamanho do volume de backups"
    echo "# TYPE rd_backup_volume_size_bytes gauge"
    echo "rd_backup_volume_size_bytes $vol_size"
    echo "# HELP rd_backup_volume_avail_bytes Espaco livre no volume de backups"
    echo "# TYPE rd_backup_volume_avail_bytes gauge"
    echo "rd_backup_volume_avail_bytes $vol_avail"
    echo "# HELP rd_backup_exporter_last_render_timestamp_seconds Ultima geracao deste arquivo de metricas"
    echo "# TYPE rd_backup_exporter_last_render_timestamp_seconds gauge"
    echo "rd_backup_exporter_last_render_timestamp_seconds $(date +%s)"
  } > "$tmp"
  mv -f "$tmp" "$WEB/metrics.prom"
}

if [ "${1:-}" = "--once" ]; then
  render
  cat "$WEB/metrics.prom"
  exit 0
fi

render
busybox-extras httpd -f -p "$PORT" -h "$WEB" -c "$CONF" &
HTTPD_PID=$!
trap 'kill "$HTTPD_PID" 2>/dev/null; exit 0' INT TERM
echo "[backup-metrics] servindo http://0.0.0.0:$PORT/metrics.prom (intervalo ${INTERVAL}s; volume $D somente leitura)"
while kill -0 "$HTTPD_PID" 2>/dev/null; do
  sleep "$INTERVAL" &
  wait $! || true
  render || echo "[backup-metrics] falha ao gerar métricas" >&2
done
echo "[backup-metrics] httpd encerrou" >&2
exit 1
