# syntax=docker/dockerfile:1.7
# =============================================================================
# RD Mídia Indoor — Player Web (Vite) — multi-stage: development / production
#
# development: Vite dev server na porta 5174 com o código montado por bind mount
#   (./player-web:/app). Enquanto player-web/package.json NÃO existir, o container
#   serve uma página provisória claramente marcada "EM CONSTRUÇÃO" (sem nenhuma
#   funcionalidade de player) e, ao detectar o package.json, encerra para o Docker
#   reiniciá-lo já em modo Vite (restart policy).
# production: build estático (npm ci + npm run build → dist/) servido por nginx
#   NÃO-root (porta 8080) atrás do nginx de borda (origem player.<dominio>).
#   Requer player-web/package.json + package-lock.json (app do agente do player).
# Headers de segurança/CSP do player são aplicados pela BORDA (nginx/), não aqui.
# =============================================================================
ARG NODE_IMAGE=node:22-alpine

# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS development
WORKDIR /app
ENV PLAYER_DEV_PORT=5174 \
    NODE_ENV=development

COPY --chmod=0755 <<'EOF' /usr/local/bin/rd-player-dev
#!/bin/sh
set -eu
cd /app
PORT="${PLAYER_DEV_PORT:-5174}"
if [ -f package.json ]; then
  # node_modules vive num volume anônimo (Linux); reinstala só quando o
  # package.json / package-lock.json mudar.
  HASH="$(cat package.json package-lock.json 2>/dev/null | sha256sum | cut -d' ' -f1)"
  if [ ! -f node_modules/.rd-deps-hash ] || [ "$(cat node_modules/.rd-deps-hash)" != "$HASH" ]; then
    echo "[player-web] instalando dependências (npm install)"
    npm install --no-audit --no-fund
    echo "$HASH" > node_modules/.rd-deps-hash
  fi
  echo "[player-web] iniciando Vite dev em 0.0.0.0:$PORT"
  exec npm run dev -- --host 0.0.0.0 --port "$PORT" --strictPort
fi
echo "[player-web] player-web/package.json ausente — servindo página provisória EM CONSTRUÇÃO na porta $PORT"
exec node /opt/rd-placeholder/server.mjs
EOF

COPY <<'EOF' /opt/rd-placeholder/index.html
<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Player Web — EM CONSTRUÇÃO</title>
<style>
  html,body{margin:0;height:100%;background:#05070a;color:#e6e6e6;font-family:system-ui,sans-serif}
  main{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;text-align:center;padding:2rem;box-sizing:border-box}
  h1{font-size:2rem;margin:0 0 1rem}
  p{max-width:40rem;line-height:1.5;color:#b0b0b0}
  code{color:#f5c542}
</style>
</head>
<body>
<main>
  <h1>Player Web — EM CONSTRUÇÃO</h1>
  <p>Esta é uma página provisória da infraestrutura local. O aplicativo do Player Web
  ainda não foi implementado neste diretório (<code>player-web/package.json</code> ausente).</p>
  <p>Nenhuma função de player está ativa aqui: sem pareamento, sem download de mídia,
  sem reprodução e sem envio de eventos.</p>
</main>
</body>
</html>
EOF

COPY <<'EOF' /opt/rd-placeholder/server.mjs
// Servidor provisório (somente leitura) da página "EM CONSTRUÇÃO".
import { createServer } from 'node:http';
import { readFileSync, existsSync } from 'node:fs';

const port = Number(process.env.PLAYER_DEV_PORT || 5174);
const html = readFileSync('/opt/rd-placeholder/index.html');

createServer((req, res) => {
  if (req.url === '/healthz') {
    res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
    return res.end('placeholder\n');
  }
  res.writeHead(200, {
    'content-type': 'text/html; charset=utf-8',
    'cache-control': 'no-store',
    'x-rd-placeholder': 'player-web-em-construcao',
  });
  res.end(html);
}).listen(port, '0.0.0.0', () => console.log(`[player-web] placeholder em 0.0.0.0:${port}`));

// Quando o app real aparecer (bind mount), encerra para o Docker reiniciar em modo Vite.
setInterval(() => {
  if (existsSync('/app/package.json')) {
    console.log('[player-web] package.json detectado — reiniciando em modo Vite');
    process.exit(0);
  }
}, 15000);
EOF

EXPOSE 5174
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
  CMD wget -q -O /dev/null "http://127.0.0.1:${PLAYER_DEV_PORT:-5174}/" || exit 1
CMD ["/usr/local/bin/rd-player-dev"]

# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM deps AS builder
COPY . .
RUN npm run build

# ---------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:1.28-alpine AS production
COPY <<'EOF' /etc/nginx/conf.d/default.conf
server {
    listen 8080;
    server_name _;
    server_tokens off;
    root /usr/share/nginx/html;
    index index.html;

    # Service Worker e index nunca em cache longo (troca de versão controlada)
    location = /index.html { add_header Cache-Control "no-cache" always; try_files $uri =404; }
    location = /sw.js      { add_header Cache-Control "no-cache" always; try_files $uri =404; }
    location /assets/      { add_header Cache-Control "public, max-age=31536000, immutable" always; try_files $uri =404; }
    location = /healthz    { access_log off; default_type text/plain; return 200 "ok\n"; }
    location /             { try_files $uri $uri/ /index.html; }
}
EOF
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
