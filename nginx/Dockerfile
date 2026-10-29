# =============================================================================
# RD Mídia Indoor — imagem do nginx de PRODUÇÃO (borda: TLS, headers, rate limit)
# Contém SOMENTE: nginx.conf + snippets + templates de produção + recarga periódica
# de certificados. A configuração LOCAL (conf.d/local.conf) não entra (.dockerignore).
# Build:  docker build -t <registro>/rd-midiaindoor-nginx:<versão> ./nginx
# =============================================================================
FROM nginx:1.28-alpine

RUN rm -f /etc/nginx/conf.d/default.conf \
 && mkdir -p /var/www/certbot /etc/letsencrypt

COPY nginx.conf               /etc/nginx/nginx.conf
COPY snippets/                /etc/nginx/snippets/
COPY prod/templates/          /etc/nginx/templates/
# Variante atrás de borda TLS compartilhada (várias instâncias por VPS — AÇÃO 005);
# ativada por NGINX_ENVSUBST_TEMPLATE_DIR=/etc/nginx/templates-proxied no compose.
COPY prod/templates-proxied/  /etc/nginx/templates-proxied/
COPY --chmod=0755 prod/40-rd-reload-certs.sh /docker-entrypoint.d/40-rd-reload-certs.sh

# envsubst do entrypoint oficial: só variáveis RD_* (variáveis do nginx intactas)
ENV NGINX_ENVSUBST_FILTER=^RD_ \
    RD_TLS_CERT_DIR=/etc/letsencrypt/live

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD nginx -t -q && wget -q -O /dev/null http://127.0.0.1:8081/nginx-health || exit 1
