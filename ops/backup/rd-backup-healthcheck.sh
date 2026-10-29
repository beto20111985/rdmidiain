#!/bin/sh
# =============================================================================
# Healthcheck do db-backup:
#   - unhealthy se a última execução de rotação falhou (.last_failure > .last_success);
#   - unhealthy se o último sucesso tem mais de RD_BACKUP_MAX_AGE_H (padrão 26 h);
#   - unhealthy se nunca houve sucesso e o agendador roda há mais de 26 h;
#   - healthy caso contrário (instalação nova aguardando o primeiro 03:00).
# =============================================================================
D="${RD_BACKUP_DIR:-/backups}"
MAX=$(( ${RD_BACKUP_MAX_AGE_H:-26} * 3600 ))
now="$(date +%s)"
ok=0; bad=0; started=0
[ -f "$D/.last_success" ]       && ok="$(cat "$D/.last_success")"
[ -f "$D/.last_failure" ]       && bad="$(cat "$D/.last_failure")"
[ -f "$D/.scheduler_started" ]  && started="$(cat "$D/.scheduler_started")"

if [ "$bad" -gt "$ok" ]; then
  echo "último backup FALHOU"; exit 1
fi
if [ "$ok" -gt 0 ]; then
  if [ $((now - ok)) -gt "$MAX" ]; then echo "último backup com mais de ${RD_BACKUP_MAX_AGE_H:-26}h"; exit 1; fi
  echo "ok: último backup há $(( (now - ok) / 60 )) min"; exit 0
fi
if [ "$started" -gt 0 ] && [ $((now - started)) -gt "$MAX" ]; then
  echo "nenhum backup desde o início do agendador (> ${RD_BACKUP_MAX_AGE_H:-26}h)"; exit 1
fi
echo "ok: aguardando primeiro backup agendado"
exit 0
