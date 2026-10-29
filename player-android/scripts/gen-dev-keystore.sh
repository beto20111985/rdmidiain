#!/usr/bin/env bash
# Gera a keystore de DESENVOLVIMENTO (player-android/keystore/dev.jks) DENTRO do container (keytool do JDK).
# Senha aleatória gravada apenas em keystore/dev.properties (diretório ignorado pelo .gitignore). Nunca imprime a senha.
# Release real: keystore do PROPRIETÁRIO, fora do repositório (mesmas chaves em dev.properties ou variáveis de CI).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${RD_ANDROID_IMAGE:-ghcr.io/cirruslabs/android-sdk:35}"
to_docker_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
export MSYS_NO_PATHCONV=1
if [ -f "$HERE/keystore/dev.jks" ]; then
  echo "keystore/dev.jks já existe — nada a fazer"
  exit 0
fi
docker run --rm -v "$(to_docker_path "$HERE"):/work/player-android" -w /work/player-android "$IMAGE" bash -c '
  set -euo pipefail
  umask 077
  mkdir -p keystore
  PASS=$(head -c 48 /dev/urandom | base64 | tr -dc "A-Za-z0-9" | head -c 32)
  keytool -genkeypair -v -keystore keystore/dev.jks -storetype PKCS12 -alias rd-dev \
    -keyalg RSA -keysize 3072 -validity 3650 \
    -dname "CN=RD Midia Indoor DEV (nao usar em producao), O=RD Midia Indoor, C=BR" \
    -storepass "$PASS" -keypass "$PASS" >/dev/null 2>&1
  printf "storeFile=keystore/dev.jks\nstorePassword=%s\nkeyAlias=rd-dev\nkeyPassword=%s\n" "$PASS" "$PASS" > keystore/dev.properties
  keytool -list -v -keystore keystore/dev.jks -storepass "$PASS" 2>/dev/null | grep -E "SHA256:|Alias name|Valid from" | head -3
'
echo "keystore de desenvolvimento gerada em keystore/dev.jks (senha em keystore/dev.properties — não versionar)"
