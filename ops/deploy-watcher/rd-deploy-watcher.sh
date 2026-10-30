#!/bin/sh
# =============================================================================
# RD Mídia Indoor — observador de deploy (AÇÃO 007). Sem polling: segue os EVENTOS do
# Docker e publica alertas no Alertmanager (API v2) da instância.
#
#   RdDeployRollback  (critical) — troca de um serviço da stack falhou e o Swarm voltou à
#                                  versão anterior (updatestate rollback_started/completed);
#                                  resolvido no próximo update "completed" do mesmo serviço.
#   RdDeployPausado   (critical) — troca pausada (paused / rollback_paused).
#   RdMigracaoFalhou  (critical) — job "migrate" terminou com código ≠ 0; resolvido quando
#                                  um "migrate" terminar com 0.
#
# Na partida, confere UMA vez o estado atual dos serviços (rollback ocorrido enquanto este
# observador estava fora do ar não passa em branco). Variáveis:
#   RD_SWARM_STACK      nome da stack Swarm observada (ex.: rd-dev-app)
#   RD_INSTANCE         instância (rótulo dos alertas)
#   RD_ALERTMANAGER_URL ex.: http://alertmanager:9093
# =============================================================================
set -u
STACK="${RD_SWARM_STACK:?defina RD_SWARM_STACK}"
INST="${RD_INSTANCE:?defina RD_INSTANCE}"
AM="${RD_ALERTMANAGER_URL:-http://alertmanager:9093}"
HOLD_S="${RD_ALERT_HOLD_S:-86400}"   # alerta disparado vale 24 h (ou até ser resolvido)

log() { echo "[deploy-watcher] $*"; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ; }
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r'; }

# alert <alertname> <service> <firing|resolved> <summary> <description>
alert() {
  now=$(date -u +%s); start=$(iso "$now")
  if [ "$3" = firing ]; then ends=$(iso $((now + HOLD_S))); else ends=$start; fi
  body="[{\"labels\":{\"alertname\":\"$1\",\"severity\":\"critical\",\"rd_instance\":\"$(esc "$INST")\",\"service\":\"$(esc "$2")\"},\"annotations\":{\"summary\":\"$(esc "$4")\",\"description\":\"$(esc "$5")\"},\"startsAt\":\"$start\",\"endsAt\":\"$ends\"}]"
  if wget -q -O /dev/null -T 10 --header 'Content-Type: application/json' --post-data "$body" "$AM/api/v2/alerts"; then
    log "alerta $1 $3: $2"
  else
    log "ERRO ao publicar alerta $1 ($3) para $2 no Alertmanager"
  fi
}

short() { echo "${1#"${STACK}"_}"; }

on_update() {  # on_update <serviço completo> <estado novo>
  svc=$(short "$1")
  case "$2" in
    rollback_started|rollback_completed)
      msg=$(docker service inspect "$1" --format '{{if .UpdateStatus}}{{.UpdateStatus.Message}}{{end}}' 2>/dev/null)
      alert RdDeployRollback "$svc" firing "Deploy de $svc falhou: voltou à versão anterior ($2)" \
        "Stack $STACK, serviço $svc: a nova versão não ficou saudável e o Swarm fez rollback. Usuários seguem na versão anterior. Detalhe: ${msg:-sem mensagem}" ;;
    paused|rollback_paused)
      alert RdDeployPausado "$svc" firing "Deploy de $svc pausado ($2)" \
        "Stack $STACK, serviço $svc: atualização pausada; verificar no Portainer (serviço → Update status)." ;;
    completed)
      alert RdDeployRollback "$svc" resolved "Deploy de $svc concluído" "Atualização concluída com sucesso."
      alert RdDeployPausado "$svc" resolved "Deploy de $svc concluído" "Atualização concluída com sucesso." ;;
  esac
}

on_die() {  # on_die <serviço completo> <exitCode>
  [ "$(short "$1")" = migrate ] || return 0
  if [ "$2" = 0 ]; then
    alert RdMigracaoFalhou migrate resolved "Migração concluída" "Job migrate terminou com sucesso."
  else
    alert RdMigracaoFalhou migrate firing "Migração do banco falhou (código $2)" \
      "Stack $STACK: o job migrate saiu com $2. As tarefas novas não ficam saudáveis e a versão anterior segue atendendo; ver logs do serviço ${STACK}_migrate."
  fi
}

reconcile() {  # uma vez, na partida
  for s in $(docker service ls --filter "label=com.docker.stack.namespace=$STACK" --format '{{.Name}}' 2>/dev/null); do
    st=$(docker service inspect "$s" --format '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}' 2>/dev/null)
    case "$st" in rollback_started|rollback_completed|paused|rollback_paused) on_update "$s" "$st" ;; esac
  done
}

log "observando a stack $STACK (instância $INST) → $AM"
reconcile
while :; do
  docker events \
    --filter type=service --filter type=container --filter event=update --filter event=die \
    --format '{{.Type}}|{{.Action}}|{{if eq .Type "service"}}{{.Actor.Attributes.name}}|{{index .Actor.Attributes "updatestate.new"}}{{else}}{{index .Actor.Attributes "com.docker.swarm.service.name"}}|{{index .Actor.Attributes "exitCode"}}{{end}}' |
  while IFS='|' read -r typ action name val; do
    case "$name" in "${STACK}"_*) ;; *) continue ;; esac
    if [ "$typ" = service ] && [ -n "$val" ]; then on_update "$name" "$val"; fi
    if [ "$typ" = container ] && [ "$action" = die ]; then on_die "$name" "$val"; fi
  done
  log "fluxo de eventos encerrado (daemon reiniciado?); reconectando"
  sleep 2
  reconcile
done
