#!/usr/bin/env bash
# =============================================================================
# RD Mídia Indoor — prepara UMA instância no host Swarm (AÇÃO 007). Idempotente.
#
#   RD_INSTANCE=rd-dev RD_SECRETS_DIR=/opt/.../secrets RD_CONFIG_DIR=/opt/.../config \
#     bash ops/swarm/rd-instance-init.sh
#
# Cria (se não existirem):
#   - rede overlay <inst>-internal (attachable, sem saída) — usada pela stack de dados
#     (compose) e pela stack da aplicação (Swarm);
#   - segredos Swarm <inst>_<nome> a partir dos arquivos de RD_SECRETS_DIR;
#   - config Swarm <inst>_player_release_public_keys.
# Por que externos: o Portainer CE 2.45.2, ao implantar stack SWARM, junta o caminho do
# projeto com o caminho ABSOLUTO dos arquivos de secrets:/configs: ("/data/compose/5//opt/…:
# no such file") — o deploy falha. Segredos externos são o padrão do Swarm e não dependem
# de o Portainer ler arquivos.
#
# Segredos Swarm são IMUTÁVEIS: para trocar um valor, crie <inst>_<nome>_v2 (docker secret
# create … < arquivo), aponte a stack para o novo nome e remova o antigo depois do deploy.
# Nunca imprime valores.
# =============================================================================
set -euo pipefail

INST="${RD_INSTANCE:?defina RD_INSTANCE}"
SECRETS_DIR="${RD_SECRETS_DIR:?defina RD_SECRETS_DIR}"
CONFIG_DIR="${RD_CONFIG_DIR:?defina RD_CONFIG_DIR}"
SECRETS=(db_owner_password db_app_password redis_password app_secret_key jwt_secret_key
         data_encryption_key cloudpaypag_webhook_secret manifest_signing_key bootstrap_master_password)

[ "$(docker info --format '{{.Swarm.LocalNodeState}}')" = active ] || { echo "host não está em modo Swarm (docker swarm init)" >&2; exit 2; }

net="$INST-internal"
if docker network inspect "$net" >/dev/null 2>&1; then
  echo "mantida: rede $net ($(docker network inspect "$net" --format '{{.Driver}} attachable={{.Attachable}} internal={{.Internal}}'))"
else
  docker network create --driver overlay --attachable --internal "$net" >/dev/null
  echo "criada:  rede $net"
fi

for s in "${SECRETS[@]}"; do
  name="${INST}_${s}"
  if docker secret inspect "$name" >/dev/null 2>&1; then
    echo "mantido: segredo $name"
  else
    [ -s "$SECRETS_DIR/$s" ] || { echo "arquivo ausente: $SECRETS_DIR/$s" >&2; exit 3; }
    docker secret create --label "rd.instance=$INST" "$name" "$SECRETS_DIR/$s" >/dev/null
    echo "criado:  segredo $name"
  fi
done

cfg="${INST}_player_release_public_keys"
if docker config inspect "$cfg" >/dev/null 2>&1; then
  echo "mantida: config $cfg"
else
  docker config create --label "rd.instance=$INST" "$cfg" "$CONFIG_DIR/player_release_public_keys.json" >/dev/null
  echo "criada:  config $cfg"
fi
