#!/bin/sh
# =============================================================================
# RD Mídia Indoor — agendador do db-backup (processo principal do container).
# Executa rd-backup diariamente em RD_BACKUP_AT (padrão 03:00) no fuso TZ
# (padrão America/Sao_Paulo). Laço próprio (sem cron/root): dorme em fatias de
# até 5 min e recalcula o alvo (robusto a ajuste de relógio).
# =============================================================================
set -eu

. /usr/local/lib/rd/rd-backup-lib.sh

AT="${RD_BACKUP_AT:-03:00}"
mkdir -p "$RD_BACKUP_DIR"
date +%s > "$RD_BACKUP_DIR/.scheduler_started"
trap 'log "encerrando agendador"; exit 0' INT TERM

log "agendador ativo: diário às $AT (TZ=${TZ:-UTC}); destino=$RD_BACKUP_DIR; retenção ${RD_BACKUP_KEEP_DAILY:-7}d/${RD_BACKUP_KEEP_WEEKLY:-4}s/${RD_BACKUP_KEEP_MONTHLY:-3}m"

if [ "${RD_BACKUP_ON_START:-false}" = "true" ]; then
  rd-backup || err "backup inicial falhou"
fi

while true; do
  now="$(date +%s)"
  target="$(date -d "$(date +%Y-%m-%d) $AT:00" +%s)"
  [ "$target" -le "$now" ] && target=$((target + 86400))
  log "próximo backup: $(date -d "@$target" '+%Y-%m-%d %H:%M %Z')"
  while :; do
    rem=$((target - $(date +%s)))
    [ "$rem" -le 0 ] && break
    [ "$rem" -gt 300 ] && rem=300
    sleep "$rem" &
    wait $! || true
  done
  rd-backup || err "backup agendado FALHOU (ver status.json)"
done
