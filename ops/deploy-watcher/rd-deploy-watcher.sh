#!/bin/sh
# =============================================================================
# RD Mídia Indoor — observador de deploy (AÇÃO 007). Sem polling: segue os EVENTOS do
# Docker e publica alertas no Alertmanager (API v2) da instância.
#
#   RdDeployRollback  (critical) — troca de um serviço da stack falhou e o Swarm voltou à
#                                  versão anterior (updatestate rollback_started/completed);
#                                  resolvido no próximo update "completed" do mesmo serviço.
#   RdDeployPausado   (critical) — troca pausada (paused / rollback_paused).
#   RdMigracaoFalhou  (critical) — job "migrate" falhou DEFINITIVAMENTE (o Swarm esgotou as
#                                  tentativas — falha transitória, ex. lock_timeout, não alerta);
#                                  resolvido quando um "migrate" terminar com 0.
#
# Sem perda de evento: retoma o fluxo do instante do último evento visto (--since) a cada
# reconexão, e na partida/reconexão concilia o estado atual dos serviços (dispara OU resolve).
# Acesso ao Docker só pelo socket-proxy somente leitura (DOCKER_HOST). Variáveis:
#   RD_SWARM_STACK      nome da stack Swarm observada (ex.: rd-dev-app)
#   RD_INSTANCE         instância (rótulo dos alertas)
#   RD_ALERTMANAGER_URL ex.: http://alertmanager:9093
# =============================================================================
set -u
STACK="${RD_SWARM_STACK:?defina RD_SWARM_STACK}"
INST="${RD_INSTANCE:?defina RD_INSTANCE}"
AM="${RD_ALERTMANAGER_URL:-http://alertmanager:9093}"
HOLD_S="${RD_ALERT_HOLD_S:-86400}"     # alerta disparado vale 24 h (ou até ser resolvido)
FINAL_CHECK_S="${RD_MIGRATE_FINAL_CHECK_S:-20}"  # > restart_policy.delay do job migrate (10 s)

log() { echo "[deploy-watcher] $*"; }
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ; }
# texto seguro para JSON: sem caracteres de controle, aspas e barras escapadas
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# alert <alertname> <service> <firing|resolved> <summary> <description>
alert() {
  now=$(date -u +%s); start=$(iso "$now")
  if [ "$3" = firing ]; then ends=$(iso $((now + HOLD_S))); else ends=$start; fi
  body="[{\"labels\":{\"alertname\":\"$1\",\"severity\":\"critical\",\"rd_instance\":\"$(esc "$INST")\",\"service\":\"$(esc "$2")\"},\"annotations\":{\"summary\":\"$(esc "$4")\",\"description\":\"$(esc "$5")\"},\"startsAt\":\"$start\",\"endsAt\":\"$ends\"}]"
  for wait_s in 1 3 9; do
    if wget -q -O /dev/null -T 10 --header 'Content-Type: application/json' --post-data "$body" "$AM/api/v2/alerts"; then
      log "alerta $1 $3: $2"; return 0
    fi
    sleep "$wait_s"
  done
  log "ERRO ao publicar alerta $1 ($3) para $2 no Alertmanager (3 tentativas)"
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
    completed|"")
      alert RdDeployRollback "$svc" resolved "Deploy de $svc concluído" "Atualização concluída com sucesso."
      alert RdDeployPausado "$svc" resolved "Deploy de $svc concluído" "Atualização concluída com sucesso." ;;
  esac
}

on_die() {  # on_die <serviço completo> <exitCode>
  [ "$(short "$1")" = migrate ] || return 0
  if [ "$2" = 0 ]; then
    alert RdMigracaoFalhou migrate resolved "Migração concluída" "Job migrate terminou com sucesso."
    return 0
  fi
  # Falha só é DEFINITIVA se, passado o intervalo de nova tentativa do Swarm, a tentativa MAIS
  # RECENTE do job também falhou (tentativa nova em andamento ou concluída = sem alerta).
  (
    sleep "$FINAL_CHECK_S"
    latest=$(for t in $(docker service ps -q "$1" 2>/dev/null); do
               docker inspect --type task --format '{{.CreatedAt}} {{.Status.State}}' "$t" 2>/dev/null
             done | sort -r | head -1 | awk '{print $NF}')
    case "$latest" in
      failed|rejected)
        alert RdMigracaoFalhou migrate firing "Migração do banco falhou (código $2, sem nova tentativa)" \
          "Stack $STACK: o job migrate falhou e não há nova tentativa. As tarefas novas não ficam saudáveis e a versão anterior segue atendendo; ver logs do serviço ${STACK}_migrate." ;;
      *) log "migrate saiu com $2; tentativa mais recente: ${latest:-?} (sem alerta)" ;;
    esac
  ) &
}

reconcile() {  # estado atual de cada serviço: dispara ou resolve
  for s in $(docker service ls --filter "label=com.docker.stack.namespace=$STACK" --format '{{.Name}}' 2>/dev/null); do
    case "$(short "$s")" in migrate|bootstrap) continue ;; esac
    on_update "$s" "$(docker service inspect "$s" --format '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}' 2>/dev/null)"
  done
}

log "observando a stack $STACK (instância $INST) → $AM"
since=$(date -u +%s)
reconcile
while :; do
  docker events --since "$since" \
    --filter type=service --filter type=container --filter event=update --filter event=die \
    --format '{{.TimeNano}}|{{.Type}}|{{.Action}}|{{if eq .Type "service"}}{{.Actor.Attributes.name}}|{{index .Actor.Attributes "updatestate.new"}}{{else}}{{index .Actor.Attributes "com.docker.swarm.service.name"}}|{{index .Actor.Attributes "exitCode"}}{{end}}' |
  while IFS='|' read -r tn typ action name val; do
    echo "${tn%?????????}" > /tmp/rd-last-event   # segundos do último evento visto
    case "$name" in "${STACK}"_*) ;; *) continue ;; esac
    if [ "$typ" = service ] && [ -n "$val" ]; then on_update "$name" "$val"; fi
    if [ "$typ" = container ] && [ "$action" = die ]; then on_die "$name" "$val"; fi
  done
  [ -s /tmp/rd-last-event ] && since=$(cat /tmp/rd-last-event)
  log "fluxo de eventos encerrado; retomando desde $since"
  sleep 2
  reconcile
done
