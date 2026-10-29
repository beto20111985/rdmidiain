#!/usr/bin/env bash
# =============================================================================
# RD Mídia Indoor — GitOps da VPS: reimplanta stacks Git do Portainer quando o branch muda
# (AÇÃO 005). Roda no HOST por timer do systemd (ops/README.md §12).
#
# Por que existe: o polling nativo do Portainer CE 2.45.2 não disparou nas stacks criadas
# pela API (AutoUpdate gravado, JobID vazio, nenhum redeploy em 19 min — AÇÃO 005). O deploy
# continua sendo do Portainer: este script só chama PUT /api/stacks/{id}/git/redeploy.
#
# Regras:
#   - stacks de INSTÂNCIA (RD_GITOPS_STACKS) são reimplantadas a cada commit novo do branch;
#   - a BORDA (RD_GITOPS_EDGE_STACK) só quando algo em RD_GITOPS_EDGE_PATHS mudou entre o commit
#     implantado e o novo (evita derrubar o TLS de todas as instâncias a cada push);
#   - flock: uma execução por vez; estado por stack evita disparo duplo durante o redeploy
#     assíncrono. Nunca imprime senha/token.
#
# Configuração: /etc/rd-gitops.conf (variáveis abaixo). Requer: git, curl, jq.
# =============================================================================
set -euo pipefail

CONF="${RD_GITOPS_CONF:-/etc/rd-gitops.conf}"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

: "${RD_GITOPS_REPO_DIR:=/opt/rd-midiaindoor/repo}"
: "${RD_GITOPS_BRANCH:=dev}"
: "${RD_GITOPS_STACKS:=rd-dev}"
: "${RD_GITOPS_EDGE_STACK:=rd-edge}"
: "${RD_GITOPS_EDGE_PATHS:=ops/edge/}"
: "${RD_GITOPS_PORTAINER_URL:=https://127.0.0.1:9443}"
: "${RD_GITOPS_PORTAINER_USER:=admin}"
: "${RD_GITOPS_PORTAINER_PASSWORD_FILE:=/opt/rd-midiaindoor/portainer/admin_password}"
: "${RD_GITOPS_STATE_DIR:=/var/lib/rd-gitops}"

log() { echo "[rd-gitops] $*"; }

exec 9>"/run/rd-gitops.lock"
flock -n 9 || { log "outra execução em andamento"; exit 0; }
install -d -m 700 "$RD_GITOPS_STATE_DIR"

git -C "$RD_GITOPS_REPO_DIR" fetch -q origin "$RD_GITOPS_BRANCH"
NEW="$(git -C "$RD_GITOPS_REPO_DIR" rev-parse "origin/$RD_GITOPS_BRANCH")"

API="$RD_GITOPS_PORTAINER_URL/api"
TOKEN="$(jq -n --arg u "$RD_GITOPS_PORTAINER_USER" --rawfile p "$RD_GITOPS_PORTAINER_PASSWORD_FILE" \
  '{Username:$u, Password:($p|rtrimstr("\n"))}' \
  | curl -sk -X POST "$API/auth" -H 'Content-Type: application/json' -d @- | jq -r '.jwt // empty')"
[ -n "$TOKEN" ] || { log "ERRO: login no Portainer falhou"; exit 1; }
pt() { local m="$1" p="$2"; shift 2; curl -sk -X "$m" "$API$p" -H "Authorization: Bearer $TOKEN" "$@"; }

STACKS_JSON="$(pt GET /stacks)"

redeploy() {  # redeploy <nome> <id> <endpointId> <commit_atual>
  local name="$1" id="$2" eid="$3" cur="$4" stamp="$RD_GITOPS_STATE_DIR/$1.triggered"
  if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$NEW" ]; then
    log "$name: redeploy para ${NEW:0:7} já disparado (aguardando o Portainer)"; return 0
  fi
  log "$name: ${cur:0:7} -> ${NEW:0:7} — redeploy"
  local env; env="$(pt GET "/stacks/$id" | jq -c '.Env // []')"
  jq -n --argjson env "$env" --arg ref "refs/heads/$RD_GITOPS_BRANCH" \
     '{env:$env, prune:true, pullImage:false, repositoryReferenceName:$ref, repositoryAuthentication:false}' \
   | pt PUT "/stacks/$id/git/redeploy?endpointId=$eid" -H 'Content-Type: application/json' -d @- --max-time 60 \
   | jq -c '{Name, msg: .message, det: .details}' || true
  printf '%s' "$NEW" > "$stamp"
}

for name in $RD_GITOPS_STACKS $RD_GITOPS_EDGE_STACK; do
  row="$(jq -c --arg n "$name" '.[] | select(.Name==$n and .GitConfig != null)' <<<"$STACKS_JSON")"
  [ -n "$row" ] || { log "$name: stack Git não encontrada"; continue; }
  id="$(jq -r .Id <<<"$row")"; eid="$(jq -r .EndpointId <<<"$row")"; cur="$(jq -r .GitConfig.ConfigHash <<<"$row")"
  [ "$cur" = "$NEW" ] && continue
  if [ "$name" = "$RD_GITOPS_EDGE_STACK" ]; then
    # shellcheck disable=SC2086
    if git -C "$RD_GITOPS_REPO_DIR" cat-file -e "$cur^{commit}" 2>/dev/null \
       && git -C "$RD_GITOPS_REPO_DIR" diff --quiet "$cur" "$NEW" -- $RD_GITOPS_EDGE_PATHS; then
      continue   # nada da borda mudou: não derruba o TLS das instâncias
    fi
  fi
  redeploy "$name" "$id" "$eid" "$cur"
done
