#!/bin/sh
# =============================================================================
# Executado pelo entrypoint oficial do nginx (/docker-entrypoint.d/) na imagem de
# PRODUÇÃO: recarrega o nginx periodicamente para aplicar certificados renovados
# pelo certbot (volume compartilhado). Recarga é graciosa (sem derrubar conexões).
# Intervalo: RD_NGINX_RELOAD_INTERVAL_S (padrão 21600 s = 6 h).
# =============================================================================
INTERVAL="${RD_NGINX_RELOAD_INTERVAL_S:-21600}"
(
  while sleep "$INTERVAL"; do
    if nginx -t -q 2>/dev/null; then
      nginx -s reload 2>/dev/null && echo "[rd-reload] nginx recarregado (certificados)"
    else
      echo "[rd-reload] nginx -t falhou; recarga ignorada" >&2
    fi
  done
) &
