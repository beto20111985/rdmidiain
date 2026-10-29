#!/usr/bin/env bash
# =============================================================================
# RD Mídia Indoor — constrói as imagens VERSIONADAS de produção (sem publicar).
#
#   ops/release/build-images.sh <versão> [registro]      ex.: ops/release/build-images.sh 1.0.0
#
# Gera <registro>/<imagem>:<versão> para: backend, frontend, player-web, nginx,
# dbtools, prometheus, alertmanager, loki, promtail, grafana.
# Push/deploy NÃO são feitos aqui (decisão do proprietário; ver ops/README.md).
# Os contextos de build são subdiretórios com .dockerignore próprio: stack/,
# secrets/ e .env nunca entram nas imagens.
# =============================================================================
set -euo pipefail

VERSION="${1:?uso: $0 <versão> [registro]}"
REGISTRY="${2:-rd-midiaindoor}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

build() {  # build <nome> <contexto> [dockerfile] [target]
  local name="$1" ctx="$2" df="${3:-}" target="${4:-}"
  local args=(build -t "$REGISTRY/$name:$VERSION" --label "org.opencontainers.image.version=$VERSION"
              --label "org.opencontainers.image.title=rd-midiaindoor-$name")
  [ -n "$df" ] && args+=(-f "$df")
  [ -n "$target" ] && args+=(--target "$target")
  echo "==> $REGISTRY/$name:$VERSION"
  docker "${args[@]}" "$ctx"
}

build backend      backend      ""                         production
build frontend     frontend     ""                         production
build player-web   player-web   ""                         production
build nginx        nginx
build dbtools      ops          ops/dbtools/Dockerfile
build prometheus   observability/prometheus
build alertmanager observability/alertmanager
build loki         observability/loki
build promtail     observability/promtail
build grafana      observability/grafana

echo "ok: imagens $REGISTRY/*:$VERSION construídas (não publicadas)"
