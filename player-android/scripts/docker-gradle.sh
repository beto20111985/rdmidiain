#!/usr/bin/env bash
# Executa o Gradle do player Android dentro do container com Android SDK (nenhuma toolchain no host).
#
# Uso (a partir de qualquer diretório):
#   player-android/scripts/docker-gradle.sh testDebugUnitTest assembleDebug
#   player-android/scripts/docker-gradle.sh lintDebug
#   player-android/scripts/docker-gradle.sh assembleRelease      # assinado com keystore/dev.properties se existir
#
# Variáveis:
#   RD_ANDROID_IMAGE   imagem (padrão ghcr.io/cirruslabs/android-sdk:35 — JDK + SDK 35 + build-tools 35.0.0)
#   RD_GRADLE_VOLUME   volume nomeado para o cache do Gradle (padrão rd-gradle-cache)
#
# O diretório protocol/ do monorepo é montado somente-leitura para os testes de conformidade (protocol/vectors).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
IMAGE="${RD_ANDROID_IMAGE:-ghcr.io/cirruslabs/android-sdk:35}"
VOLUME="${RD_GRADLE_VOLUME:-rd-gradle-cache}"

# Git Bash (Windows): caminhos no formato D:/... e sem conversão automática de /work.
to_docker_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi
}
export MSYS_NO_PATHCONV=1

PROJECT_MOUNT="$(to_docker_path "$HERE")"
PROTOCOL_MOUNT="$(to_docker_path "$ROOT/protocol")"

exec docker run --rm \
  -v "$PROJECT_MOUNT:/work/player-android" \
  -v "$PROTOCOL_MOUNT:/work/protocol:ro" \
  -v "$VOLUME:/root/.gradle" \
  -w /work/player-android \
  "$IMAGE" \
  bash -lc "chmod +x ./gradlew && ./gradlew --no-daemon --console=plain $*"
