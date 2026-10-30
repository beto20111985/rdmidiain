# RD Mídia Indoor — Operação (ops)

Guia operacional do produto: ambiente local em Docker, borda nginx, segredos, banco,
backup/restore, observabilidade, continuidade dos players e implantação via Portainer.

> Regra permanente: `stack/` é memória privada do proprietário. Nada aqui lê, monta ou
> empacota `stack/`. O produto funciona sem ele.

---

## 1. Serviços e portas (local)

| Serviço | Container | Acesso no host | Observação |
|---|---|---|---|
| nginx (borda) | `rd-nginx` | `http://localhost` (painel) · `http://localhost:8088` (Player Web, só loopback) | config em `nginx/` |
| backend (FastAPI) | `rd-backend` | — (via nginx `/api`, `/ws`, `/health`) | |
| worker / worker-beat (Celery) | `rd-worker`, `rd-worker-beat` | — | |
| frontend (Vite dev) | `rd-frontend` | — (via nginx `/`) | |
| player-web (Vite dev) | `rd-player-web` | — (via nginx `:8088`) | sem `player-web/package.json` serve página "EM CONSTRUÇÃO" |
| PostgreSQL 16 | `rd-postgres` | `5441` (compose integrado: só `127.0.0.1`) | |
| Redis 7 | `rd-redis` | `6384` (compose integrado: só `127.0.0.1`) | |
| db-init (one-shot) | `rd-db-init` | — | papéis `rd_owner`/`rd_app`/`rd_backup` |
| bootstrap (one-shot, AÇÃO 003) | — | — | `bootstrap-instance` após `migrate`: modo de aprovação + Master primário; backend/worker/beat dependem dele (§4.3) |
| db-backup | `rd-db-backup` | — | diário 03:00 America/Sao_Paulo |
| db-restore (profile `ops`) | one-off | — | exercício/restore real |
| backup-metrics | `rd-backup-metrics` | — (`backup-metrics:9101/metrics.prom`, rede interna) | métricas de backup/drill para o Prometheus |
| Mailpit | `rd-mailpit` | `http://127.0.0.1:8025` | SMTP interno `mailpit:1025` |
| Prometheus | `rd-prometheus` | `http://127.0.0.1:9095` | |
| Alertmanager | `rd-alertmanager` | `http://127.0.0.1:9093` | e-mail → Mailpit |
| Grafana | `rd-grafana` | `http://127.0.0.1:3005` | usuário `admin`, senha em `secrets/grafana_admin_password` |
| Loki / Promtail | `rd-loki`, `rd-promtail` | — | só containers do projeto |

Portas escolhidas para não colidir com outros projetos do host (3000, 3001, 3010, 3011,
3043, 3100, 5432, 5433, 5439, 6379, 8010, 8043, 8080, 8090, 9090, 9099 já em uso).

## 2. Subir o ambiente local

Pré-requisitos: Docker Desktop (Windows) com Compose v2, `.env` na raiz (a partir de
`.env.example`, nunca versionado).

```powershell
# 1) segredos locais (idempotente; não imprime valores)
powershell -ExecutionPolicy Bypass -File ops\dev\generate-secrets.ps1
#    (Linux/Git Bash: bash ops/dev/generate-secrets.sh)

# 1b) configuração LOCAL não secreta (uma vez; revisar valores) — §3.1 e §4.3
Copy-Item ops\dev\dev-bootstrap.env.example ops\dev\dev-bootstrap.env   # modo + Master primário (1ª inicialização)
Copy-Item ops\dev\dev-origins.env.example ops\dev\dev-origins.env       # opcional: túnel ngrok

# 2) subir tudo (o one-shot bootstrap roda após migrate; backend/worker/beat esperam por ele)
docker compose up -d --build

# 3) conferir saúde
docker compose ps
curl http://localhost/health
```

Reconstruir UM serviço sem efeito colateral nas dependências:

```powershell
docker compose up -d --no-deps --build <serviço>
```

> Sem `--no-deps`, o Compose também "converge" as dependências (ex.: `up -d backend`
> recria `postgres`/`redis` se a configuração deles divergir do container em execução).

## 3. Borda nginx

Arquivos:

| Arquivo | Uso |
|---|---|
| `nginx/nginx.conf` | comum: `server_tokens off`, log JSON **sem query string**, zonas de rate limit (429), timeouts |
| `nginx/snippets/headers-*.conf` | headers de segurança por app/ambiente (incluídos em **todo** server/location que usa `add_header`) |
| `nginx/snippets/loc-*.conf` | rotas compartilhadas local/produção (`/health`, API do painel, API/WS dos players) |
| `nginx/conf.d/local.conf` | **local** (HTTP, sem HSTS, Vite dev) — não entra na imagem de produção |
| `nginx/prod/templates/rd.conf.template` | **produção** (TLS, HSTS, CSP estrita, domínios por variável) |
| `nginx/Dockerfile` | imagem da borda de produção |

Rotas:

| Rota | Destino | Regras |
|---|---|---|
| `/health`, `/health/ready` | backend | públicas, sem log de acesso |
| `/health/*` (ex.: `/health/full`), `/metrics` | **negado (403)** | Prometheus raspa `backend:8000/metrics` pela rede interna |
| `/api/v1/auth/login`, `mfa/verify`, `password/*`, `invites/accept` | backend | `rl_login` 10 req/min por IP, rajada 5 |
| `/api/v1/auth/refresh|logout|sessions`, `/me`, `/realtime/ticket` | backend | `rl_session` 120 req/min por IP (NAT) |
| `/api/v1/**/upload(s)` | backend | 210 MB, `proxy_request_buffering off`, 600 s |
| `/api/v1/player/**` | backend | `rl_device` (Bearer; sem header → IP) + teto por IP; mídia com `Range`/`If-Range`, sem buffer, 300 s |
| `/ws/player*` e `/ws/*` | backend | upgrade WebSocket, 3600 s, limite de handshake por IP |
| `/` | frontend (local: Vite dev + HMR) | |
| `:8088/` (local) / `player.<dominio>` (prod) | player-web | **só** `/api/v1/player/**`, `/ws/player*`, `/health` nesta origem |

CSP:

- Painel local: relaxada **só** para o Vite dev (`'unsafe-inline'` do preâmbulo React Fast
  Refresh e dos estilos injetados; `ws://localhost:*` do HMR; `wss://$host` para HMR/`/ws/admin` por
  túnel https de desenvolvimento — §3.1). Produção: `script-src 'self'`.
- Player (A4 §9.2 + bundle do player): `default-src 'self'; script-src 'self'; style-src 'self'; …;
  require-trusted-types-for 'script'; trusted-types rd-player`, `Referrer-Policy: no-referrer`.
  Local relaxa só `style-src 'unsafe-inline'` (CSS injetado pelo Vite) e `connect-src ws://localhost:8088 wss://$host` (HMR; túnel).
- HSTS (`max-age=31536000; includeSubDomains`) **somente** no server TLS de produção.
- `Permissions-Policy`: painel (local e prod) `geolocation=(self)` — o botão "Usar GPS" do
  cadastro usa `navigator.geolocation`; player, docs e redirecionamento HTTP mantêm
  `geolocation=()`. Câmera, microfone, pagamento e USB desligados em todas as origens.

Rate limit responde **429** com corpo JSON no envelope do contrato e `Retry-After`.
Rejeições de rate limit não vão ao error log (evita registrar a linha da requisição com
query string); ficam no access log JSON (`status=429`, `limit_req_status=REJECTED`).

Validar configuração:

```powershell
docker exec rd-nginx nginx -t
docker exec rd-nginx nginx -s reload     # aplica alterações em nginx/ sem derrubar conexões
```

### 3.1 Acesso externo de DESENVOLVIMENTO (túnel ngrok) — contrato de origens

Destinos do túnel (o agente do ngrok roda no Windows, fora do Docker):

| App | Porta no host | Destino do túnel |
|---|---|---|
| Painel + API + `/ws` | `80` | `http://localhost:80` |
| Player Web | `8088` (só loopback) | `http://localhost:8088` — túnel **próprio** (outro domínio) |

Nada de domínio de túnel em código: ele fica em `ops/dev/dev-origins.env` (arquivo LOCAL, não
secreto, fora do git e de todas as imagens; modelo comentado em `ops/dev/dev-origins.env.example`),
lido pelo compose como `env_file` **opcional** (`required: false`) em `backend`, `frontend` e
`player-web`.

| Variável | Ambientes | Consumida por |
|---|---|---|
| `CORS_ORIGINS` (base do painel) | todos (produção: só `https://` oficiais) | `settings.panel_origins` |
| `RD_DEV_ALLOWED_ORIGINS` | **só development/test** | backend: `panel_origins` = `CORS_ORIGINS` ∪ esta → CORS, CSRF (`/auth/refresh`, `/auth/logout`…), `Origin` do WS `/ws/admin` (e aceito no WS do player); Vite do painel: `server.allowedHosts` (host de cada origem) |
| `PLAYER_ALLOWED_ORIGINS` (base do player) | todos (produção: `RD_PLAYER_ALLOWED_ORIGINS`, https) | `settings.player_origins` |
| `RD_DEV_PLAYER_ALLOWED_ORIGINS` | **só development/test** | backend: `player_origins` → CORS e `Origin` de `/ws/player`, `/ws/player/pairing`; Vite do player: `server.allowedHosts` |
| `VITE_ALLOWED_HOSTS` (legado, player) | dev | hosts extras do Vite do player (mesmo parser) |
| `VITE_HMR_CLIENT_PORT` (player) | dev | `server.hmr.clientPort` só se definido |

Regras: origens EXATAS `http(s)://host[:porta]` — sem `*`, caminho, query ou credenciais;
`server.allowedHosts` é sempre um array (nunca `true`); entrada inválida ⇒ backend/Vite não sobem
(erro explícito). Em `staging`/`production` qualquer `RD_DEV_*` presente ⇒ o backend RECUSA subir, e
`CORS_ORIGINS`/`PLAYER_ALLOWED_ORIGINS` com curinga, loopback ou domínio de túnel público (ngrok,
trycloudflare…) também ⇒ recusa. Cookie `rd_refresh` inalterado (HttpOnly, `SameSite=Strict`,
`Path=/api/v1/auth`, `Secure`=`COOKIE_SECURE`): pelo túnel o painel é *same-origin* com a API.
HMR e `/ws/admin` usam o esquema da página (`https://<túnel>` → `wss://<túnel>`), sem mixed content.

nginx LOCAL (`nginx/conf.d/local.conf`): `X-Forwarded-For`/`-Proto` do cliente só são aceitos de
pares na rede Docker (gateway do Docker Desktop, por onde chegam navegador local, agente ngrok e LAN);
o ngrok ACRESCENTA ao fim das listas, então vale só o último elemento (IP real via `real_ip_recursive`;
esquema `https`). `X-Forwarded-Host`/`-Port` do cliente nunca são aceitos (o ngrok os repassa sem
alterar). Produção não muda: lá o nginx é a borda TLS (`$scheme`/`$remote_addr`). Risco residual
SÓ em dev: processos locais/LAN que alcançam a porta 80 podem forjar XFF/XFP (afeta rate limit por IP
do nginx e IP de auditoria em dev; não afeta autorização).

**Como trocar o domínio do ngrok** (sem mudar código):

```powershell
# 1) editar a linha RD_DEV_ALLOWED_ORIGINS (origem https EXATA do novo túnel)
notepad ops\dev\dev-origins.env
# 2) recriar SÓ quem lê o arquivo (env_file só é relido na recriação; dados intactos)
docker compose up -d --no-deps backend frontend player-web
# 3) conferir (troque <dominio>):
curl.exe -s -o NUL -w "%{http_code}" -H "Host: <dominio>" http://localhost/          # 200 (não 403 "Blocked request")
curl.exe -s -i -X OPTIONS http://localhost/api/v1/auth/login -H "Origin: https://<dominio>" -H "Access-Control-Request-Method: POST" | findstr /i access-control-allow-origin
```

Player Web por túnel: possível com um SEGUNDO túnel para `http://localhost:8088` (domínio próprio,
nunca o do painel), declarado em `RD_DEV_PLAYER_ALLOWED_ORIGINS`. O plano gratuito do ngrok tem um
único domínio estático — o segundo túnel recebe domínio aleatório a cada início, então a linha
precisa ser atualizada (e os 3 serviços recriados) a cada novo domínio. A página de aviso do ngrok
gratuito aparece no primeiro acesso de cada navegador (para chamadas de API/teste use o cabeçalho
`ngrok-skip-browser-warning: 1`).

## 4. Segredos (Docker secrets)

Gerados por `ops/dev/generate-secrets.(ps1|sh)` em `./secrets/` (ignorado por git e por todos os
`.dockerignore`). A aplicação lê `<NOME>_FILE` (ex.: `POSTGRES_PASSWORD_FILE=/run/secrets/db_app_password`).

| Arquivo | Uso |
|---|---|
| `postgres_superuser_password` | superusuário do PostgreSQL (db-init, restore, testes) |
| `db_owner_password` | `rd_owner` — migrations |
| `db_app_password` | `rd_app` — runtime API/workers |
| `db_backup_password` | `rd_backup` — pg_dump |
| `redis_password` | Redis |
| `app_secret_key`, `jwt_secret_key`, `data_encryption_key` | app (produção; em dev seguem no `.env`) |
| `backup_passphrase` | criptografia dos backups (**guardar cópia fora da VPS** — sem ela não há restore) |
| `grafana_admin_password` | Grafana |
| `cloudpaypag_api_key`, `cloudpaypag_webhook_secret`, `smtp_password` | produção — fornecidos pelo proprietário (dependência externa) |
| `manifest_signing_key` | PEM PKCS8 EC P-256 que assina os manifestos dos players (`MANIFEST_SIGNING_KEY_FILE`) — **obrigatória fora de dev**; ver §4.1 |
| `bootstrap_master_password` | senha INICIAL do Master primário (32 alfanuméricos, letras + dígitos) — montada **só** no serviço `bootstrap`, usada uma vez na 1ª inicialização; ver §4.3 |

Configuração não secreta da instância (Docker config, em `RD_CONFIG_DIR`):
`player_release_public_keys.json` (`PLAYER_RELEASE_PUBLIC_KEYS_FILE`) — JSON
`{"<key_id>": "<SPKI DER em base64url>"}` com as chaves **públicas** de release dos players
(verificação de `/api/v1/player/app-update`); `{}` = nenhuma atualização publicada.

Rotação: `ops/dev/generate-secrets.ps1 -Rotate <nome>` e aplicar no serviço:
`db_*` → `docker compose up db-init` (reaplica senhas) e reiniciar quem usa;
`postgres_superuser_password` → `ops/db/set-superuser-password.sql` (ver §9);
`redis_password` → recriar `redis` e os serviços de backend; `grafana_admin_password` só vale
no primeiro start do volume (depois: `grafana cli admin reset-admin-password`).

### 4.1 Chave de assinatura de manifesto (ES256)

- **Onde é usada**: backend e worker (gerador de manifesto). `key_id` = `srv-` + 12 hex do
  SHA-256 da chave pública. Os players recebem a chave pública **somente no pareamento**
  (`server_keys` do resultado do pareamento, RDPP §2.5) e passam a verificar todo manifesto
  (RDPP §5) com ela.
- **Dev local**: sem `MANIFEST_SIGNING_KEY_FILE`, o backend cria e reutiliza
  `/app/media/.rd-keys/manifest_es256.pem` no volume `media-storage` (compartilhado por backend e
  worker — mesmo `key_id`). `docker compose down -v` apaga o volume ⇒ nova chave ⇒ re-parear os
  players de teste. O arquivo `secrets/manifest_signing_key` gerado localmente NÃO é usado em dev
  (existe para paridade com produção).
- **Produção**: `ops/dev/generate-secrets.sh` gera `manifest_signing_key` em `RD_SECRETS_DIR`
  (OpenSSL; no Windows, `.ps1` via CNG efêmera). Sem ela o backend lança
  `RuntimeError: MANIFEST_SIGNING_KEY_FILE é obrigatório fora de desenvolvimento` ao gerar o
  primeiro manifesto e nenhum player sincroniza.
- **Backup (obrigatório)**: a chave NÃO está no dump do banco. Guardar cópia cifrada FORA da VPS,
  junto com `backup_passphrase` e `data_encryption_key` (esta decifra segredos TOTP e a chave de
  assinatura da instância guardados no banco — sem ela o restore do banco fica inutilizável para
  esses dados).
- **Perda da chave**: os players NÃO param — continuam reproduzindo o último manifesto válido
  (offline-first) até o `valid_until_ms` dos itens — mas rejeitam todo manifesto novo
  (assinatura com `key_id` desconhecido; RDPP §5). Procedimento: 1) restaurar a chave a partir da
  cópia externa (mesmo `key_id`, nenhum impacto); 2) só se não houver cópia: gerar nova chave
  (`RD_CONFIRM_MANIFEST_KEY_ROTATION=sim ops/dev/generate-secrets.sh --rotate manifest_signing_key`),
  recriar backend/worker e **re-parear todas as telas** (o novo pareamento entrega o novo
  `server_keys`); priorizar telas com campanhas vigentes.
- **Rotação planejada**: o código atual publica UMA chave (`server_public_keys`) e o protocolo
  não tem renovação de `server_keys` fora do pareamento ⇒ hoje rotação = re-pareamento geral.
  Por isso o script recusa `--rotate manifest_signing_key` sem confirmação explícita.
  Pendência registrada (W-INV/W-PLAY): lista de chaves (atual + próxima) e entrega da próxima
  chave assinada pela atual, para rotação sem re-pareamento.

### 4.2 Chaves de release dos players (app-update)

A chave **privada** de release fica OFFLINE com o proprietário (nunca na VPS/repositório); o
servidor só verifica. Gerar (máquina de release):

```bash
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out release-2026a.pem
openssl pkey -in release-2026a.pem -pubout -outform DER | base64 -w0 | tr '+/' '-_' | tr -d '='
# → colocar em ${RD_CONFIG_DIR}/player_release_public_keys.json: {"release-2026a": "<saída>"}
```

A mesma chave pública precisa estar fixada na configuração dos players (`update.public_keys`).
Rotação: publicar a nova pública ao lado da antiga (o JSON aceita várias), atualizar os players
com uma release assinada pela antiga que já contenha a nova, depois retirar a antiga.

### 4.3 Bootstrap da instância — modo de aprovação e Master primário (AÇÃO 003)

Serviço one-shot `bootstrap` (`python -m app.cli bootstrap-instance`, idempotente; saída 0 = ok,
2 = configuração, 1 = erro inesperado) roda após `migrate`; `backend`, `worker` e `worker-beat`
só sobem depois que ele termina com 0. Ele:
1. na **1ª inicialização** grava o modo de aprovação financeira no banco (histórico selado) a partir
   de `RD_BOOTSTRAP_DUAL_MASTER_APPROVAL` (`true` = DUPLA, `false` = ÚNICA);
2. cria o **Master primário** (assento 1) com `RD_BOOTSTRAP_MASTER_EMAIL`/`_NAME` e a senha do
   segredo `bootstrap_master_password`, com troca de senha obrigatória; TOTP e senha financeira são
   configurados no primeiro acesso (wizard). Nenhuma senha/credencial universal existe no código.

| Onde ficam os valores | Desenvolvimento | Produção (Portainer) |
|---|---|---|
| `RD_BOOTSTRAP_DUAL_MASTER_APPROVAL`, `RD_BOOTSTRAP_MASTER_EMAIL`, `RD_BOOTSTRAP_MASTER_NAME` (NÃO secretos) | `ops/dev/dev-bootstrap.env` (local, fora do git e das imagens; modelo `ops/dev/dev-bootstrap.env.example`; env_file opcional **só** do `bootstrap`) | variáveis da stack (`ops/portainer/instance.env.example`); ausentes ficam ausentes no container |
| Senha inicial | `./secrets/bootstrap_master_password` (`generate-secrets`) | `${RD_SECRETS_DIR}/bootstrap_master_password` |
| `RD_BOOTSTRAP_MASTER_PASSWORD` em texto | **recusado** (código 2) | **recusado** |

**O ENV não muda nada depois da 1ª inicialização**: com o modo já gravado, `RD_BOOTSTRAP_*` é
ignorado; divergência (ENV × modo efetivo, e-mail × titular) só é sinalizada (auditoria + métrica
`rd_security_env_divergence` → alerta `RDSecurityEnvDivergence`). Trocar `true`→`false` e reiniciar
**não** desliga a dupla aprovação: mudança de modo só pelo fluxo protegido do painel
(Configurações → Segurança da instância; DUAL→ÚNICA exige pedido de um Master e aprovação do outro
com TOTP + senha financeira + código por e-mail).

1ª inicialização em DESENVOLVIMENTO (coordenada; depois de aplicar todas as migrations até o head com `docker compose up --no-deps migrate`):

```powershell
powershell -ExecutionPolicy Bypass -File ops\dev\generate-secrets.ps1   # cria bootstrap_master_password (não exibe)
Copy-Item ops\dev\dev-bootstrap.env.example ops\dev\dev-bootstrap.env   # se ainda não existir; revisar valores
docker compose up --no-deps bootstrap                                     # deve terminar com código 0
docker compose up -d --no-deps backend worker worker-beat
docker compose run --rm --no-deps backend python -m app.cli security-verify   # OK = código 0
```

A senha inicial do Master é lida do arquivo do segredo **pelo proprietário**, uma única vez, no host
(nunca colada em chat/log); o primeiro login obriga a troca. Rotacionar `bootstrap_master_password`
depois da 1ª inicialização não altera a senha do Master (o segredo não é mais lido).

Recuperação (Master perdeu acesso antes/depois do setup — sem backdoor):
`docker compose run --rm --no-deps backend python -m app.cli recover-master --seat <1|2> --reason "<motivo ≥ 10>"`
(senha temporária via `RD_RECOVERY_PASSWORD_FILE` ou prompt sem eco; zera fatores e sessões do
titular, exige novo setup, audita e notifica todos os Masters; nunca troca o titular). Verificação
de integridade a qualquer momento: `python -m app.cli security-verify` (alertas
`RDSecurityIntegrityFailed`, `RDSecurityApprovalModeChanged`, `RDSecurityEnvDivergence` — §7).
A CLI `bootstrap-masters` passa a ser só recuperação/automação headless (sem `--force-reassign`).

**Atenção ao reiniciar serviços em DESENVOLVIMENTO** (incidente registrado na AÇÃO 003): `docker compose start backend` e
`docker compose up -d backend` (sem `--no-deps`) resolvem `depends_on` e rodam `migrate` e `bootstrap` antes. Com o
`./backend` montado no container, uma migration ainda em edição seria aplicada ao banco de dev. Para reiniciar só
o serviço, use `docker restart rd-backend` (idem `rd-worker`, `rd-worker-beat`) ou `docker compose up -d --no-deps <serviço>`.
Migrations novas: aplicar de propósito, com backup antes (`docker compose exec db-backup rd-backup`), via
`docker compose up --no-deps migrate`.

## 5. Banco de dados — papéis

`ops/db/roles.sql` (idempotente, executado pelo one-shot `db-init`):

- `rd_owner`: dono do schema/objetos; usado **apenas** pelo serviço `migrate` (`alembic upgrade head`).
- `rd_app`: runtime; só `SELECT/INSERT/UPDATE/DELETE`; sem DDL, sem `TRUNCATE`, sem superuser;
  `idle_in_transaction_session_timeout=5min`.
- `rd_backup`: `pg_read_all_data` + `BYPASSRLS`, transação somente leitura.
- `REVOKE CREATE ON SCHEMA public FROM PUBLIC`; privilégios padrão: objetos criados por
  `rd_owner` concedem DML a `rd_app` automaticamente.
- O db-init **não** re-concede privilégios a tabelas em que `rd_app` já tenha ACL explícita
  (preserva REVOKEs de append-only feitos pelas migrations).
- `RD_DB_TRANSFER_OWNERSHIP=on` transfere tabelas/sequências/funções/tipos do schema para
  `rd_owner` (com `lock_timeout`; pendências são reportadas e resolvidas re-executando).

Tabelas **append-only** (REVOKE `UPDATE, DELETE, TRUNCATE` de `rd_app` aplicado pelas
**migrations do backend**, não pelo db-init) — lista esperada: `audit_log`/`audit_logs`,
`ledger_*` (ex.: `ledger_entries`), `webhook_events`, eventos brutos de playback após a
ingestão (`playback_events` ou tabela bruta equivalente). Proteção adicional recomendada:
trigger que bloqueia `UPDATE/DELETE` mesmo para o dono.

Filas do worker: `-Q default,media,notifications,financial,reports` via `RD_CELERY_QUEUES`
(ponto único de ajuste no compose; deve coincidir com `task_queues` em
`backend/app/workers/celery_app.py`).

Testes do backend criam bancos descartáveis (`CREATE DATABASE`): `rd_app` não pode. No compose
integrado use o serviço `backend-tests` (profile `test`), que conecta como superusuário:
`docker compose --profile test run --rm backend-tests pytest -m integration`.

## 6. Backup e restore

- `db-backup` (imagem `ops/dbtools`): `pg_dump -Fc` → `gpg --symmetric --cipher-algo AES256`
  (passphrase via secret) → volume `db-backups`. Agendado **03:00 America/Sao_Paulo**.
- Cada backup: verificação (decifra e lê o TOC inteiro com `pg_restore --list`), `sha256`,
  manifesto JSON e **contagem exata de linhas no mesmo snapshot do dump** (`*.counts.tsv`).
- Retenção: **7 diários / 4 semanais (~30 d) / 3 mensais (~90 d)** (hardlinks no mesmo volume).
- **Cópia fora da VPS** (obrigatória, dependência externa): `RD_BACKUP_OFFSITE_CMD` recebe o
  arquivo cifrado em `$1`. A imagem inclui `rclone` (SFTP, S3, etc.). Ex.:
  `rclone --config /run/secrets/rclone_conf copy "$1" offsite:rd-backups/<instancia>/`.
  Sem ela o `status.json` registra `"offsite":"not_configured"` (não é mascarado).
- Healthcheck: falha se o último backup falhou ou tem mais de 26 h.
- Métricas: o sidecar `backup-metrics` (mesma imagem `dbtools`, volume **read-only**,
  root FS read-only, tmpfs) lê só metadados (`.last_success`, `.last_failure`, `status.json`,
  `drill/status.json`, contagem por camada, espaço livre) e serve um arquivo de texto
  Prometheus via `httpd` do busybox em `:9101/metrics.prom` (só rede interna). Nunca lê
  dumps nem segredos. Limites configuráveis: `RD_BACKUP_MAX_AGE_H` (26),
  `RD_DRILL_MAX_AGE_DAYS` (35), `RD_BACKUP_OFFSITE_REQUIRED` (dev `false`, prod `true`).

Comandos:

```powershell
docker compose exec db-backup rd-backup                      # backup manual agora
docker compose exec db-backup cat /backups/status.json        # último resultado
docker compose --profile ops run --rm db-restore              # EXERCÍCIO de restore
```

O exercício restaura o backup diário mais recente no banco descartável `rd_restore_test`,
compara a contagem de linhas de cada tabela com a do backup, imprime o relatório
(`/backups/drill/last_drill_report.txt`) e **apaga** o banco descartável. Recomendado mensalmente.

Restore real (desastre) — parar a escrita antes:

```powershell
docker compose stop backend worker worker-beat
docker compose --profile ops run --rm db-restore rd-restore `
  --file /backups/daily/<arquivo>.dump.gpg --target-db rdmidiaindoor --replace --allow-live-target
docker compose up -d backend worker worker-beat
```

## 7. Observabilidade

- **Prometheus** raspa `backend:8000/metrics` (rede interna), Loki, Promtail, Grafana, Alertmanager.
  Regras: `RdBackendDown` (crítico, 2 min), `RdScrapeTargetDown`, `RdBackendMetricsSlow`,
  `RdPlayersSemConexao` (30 min) e `RdWatchdog` (sentinela do pipeline, não envia e-mail).
- **Workers Celery** (sem exporter de terceiros; tudo via Redis, O(1) por scrape no
  `/metrics` da API):
  - `rd_worker_last_heartbeat_timestamp_seconds` — task `worker_heartbeat` agendada pelo beat
    a cada 60 s (fila `default`, `expires` 55 s);
  - `rd_beat_last_heartbeat_publish_timestamp_seconds` — sinal `before_task_publish` no beat;
  - `rd_celery_queue_length{queue}` — `LLEN` das 5 filas (com as listas de prioridade do kombu);
    `rd_celery_broker_up`;
  - `rd_celery_task_failures_total{task}` / `rd_celery_task_retries_total{task}` — sinais
    `task_failure`/`task_retry` → `HINCRBY` (seguro com prefork); zerados no `worker_ready`
    para que a 1ª falha apareça no `increase()`.
  - Código: `backend/app/workers/observability.py`, `backend/app/workers/tasks/observability.py`.
- **Alertas de worker/backup**: `RdBeatSemPublicarHeartbeat` e `RdWorkerSemHeartbeat` (> 3 min;
  o do beat inibe o do worker), `RdFilaCeleryAcumulada` (> 100 msgs por 10 min),
  `RdBrokerIlegivel`, `RdFalhasDeTask` (> 3 em 15 min), `RdFalhaDeTaskFinanceira` (qualquer),
  `RdRetriesExcessivos` (> 20 em 15 min), `RdBackupAtrasado` (> 26 h; instalação nova conta do
  início do agendador), `RdBackupFalhou`, `RdRestoreDrillAtrasado` (> 35 d),
  `RdRestoreDrillFalhou`, `RdBackupSemCopiaExterna` (só quando obrigatória),
  `RdBackupVolumeQuaseCheio` (< 10 %), `RdMetricasDeBackupAusentes`.
- **Alertas de segurança da instância (AÇÃO 003, §4.3)**: `RDSecurityIntegrityFailed` (`rd_security_integrity_ok == 0`
  por 5 min, crítico — selos/titulares de Master não conferem; dinheiro bloqueado com 503),
  `RDSecurityApprovalModeChanged` (mudança da versão do modo em 30 min, aviso) e `RDSecurityEnvDivergence`
  (`RD_BOOTSTRAP_*` diverge do modo efetivo por 30 min, aviso). Gauges publicados pela API em `/metrics`.
- **Beat/RedBeat (BUG-QA-03)**: `beat_max_loop_interval=30 s` e `redbeat_lock_timeout=90 s` (lock renovado
  a cada loop). Antes: lock de 1500 s persistido pelo AOF do Redis ⇒ após queda do Redis o beat novo
  ficava até 25 min sem agendar nada. Healthcheck do beat = heartbeat publicado há < 3 min
  (`python -m app.workers.observability beat-healthcheck --max-age 180`), nunca a existência do lock.
- **`/metrics` sem Redis**: PING com timeout de 0,5 s; Redis fora ⇒ `rd_metrics_redis_up 0`, séries do
  Redis omitidas e resposta imediata (sem falso `RdBackendDown`).
- **Testes das regras** (simulação, sem parar serviços):
  `docker run --rm --entrypoint promtool -v <repo>/observability/prometheus:/p:ro prom/prometheus:v3.5.0 test rules /p/tests/rd-alerts.test.yml`.
- Aplicar mudanças de regras/config sem recriar: `docker kill -s HUP rd-prometheus` e
  `docker exec rd-alertmanager wget -q -O- --post-data='' http://127.0.0.1:9093/-/reload`.
- **Alertmanager** → e-mail (local: Mailpit). Produção: config por instância
  (`ops/portainer/alertmanager.prod.example.yml`).
- **Loki + Promtail**: coleta só containers com label `com.docker.compose.project=<projeto>`;
  redige `token=`, `ticket=`, `password=`, `code=`, `Bearer …`, campos JSON sensíveis e
  senhas em URLs **antes** de enviar ao Loki. Retenção 14 dias. Os logs locais do Docker
  (json-file, 3×10 MB) não são redigidos — por isso a regra do contrato: nada secreto em URL.
- **Grafana**: datasources Prometheus/Loki/Alertmanager e dashboard
  "RD Mídia Indoor — Visão geral" (API pela borda nginx, players, workers — heartbeat,
  filas, falhas —, backup e restore drill, coleta).
- **Decisão — profile**: observabilidade **sempre ativa** (sem profile). Motivos: requisito do
  proprietário de logs/alertas robustos desde o início, paridade local × VPS e custo modesto
  (~1 GB de RAM somados, limites por container). Para economizar recursos localmente:
  `docker compose stop prometheus alertmanager grafana loki promtail`.
- **Promtail está em fim de vida** (EOL 2026-03-02). Sucessor oficial: Grafana Alloy
  (`alloy convert --source-format=promtail`). Migração registrada como pendência.
- Sentry: aceito pelo backend via `SENTRY_DSN` (dependência externa, vazio localmente).

## 8. Disponibilidade do painel × continuidade da reprodução

| | Painel / API / workers | Reprodução nos players |
|---|---|---|
| Depende do servidor? | Sim | **Não** (offline-first: manifesto, mídia e outbox de eventos locais) |
| Em queda do servidor | painel, pagamentos, aprovações e relatórios indisponíveis | telas continuam exibindo a última programação válida |
| Ao voltar | — | players reenviam eventos (outbox) e sincronizam o manifesto |
| RPO (perda de dados) | até ~1 dia (backup diário 03:00) — perda máxima aceita pelo proprietário | eventos ainda no player não se perdem (reenvio) |
| RTO | tempo de restore + subida da stack (exercício local: segundos para o volume atual) | 0 para a exibição |

Riscos: um defeito que corrompa o manifesto **publicado** se propaga aos players na próxima
sincronização — por isso o player mantém a geração anterior para rollback.

## 9. Janela de reinício integrado (dev) — aplicar o compose endurecido

`ops/dev/docker-compose.integrated.yml` substitui o `docker-compose.yml` e muda
postgres/redis/backend/worker/beat/frontend (ver cabeçalho do arquivo). Procedimento
(PowerShell, na raiz):

```powershell
# 0) validar e salvar o compose atual para rollback
docker compose --project-directory . -f ops/dev/docker-compose.integrated.yml config -q
Copy-Item docker-compose.yml stack\bk\002_snapshot\docker-compose.pre-janela.yml
powershell -ExecutionPolicy Bypass -File ops\dev\generate-secrets.ps1
docker compose exec db-backup rd-backup

# 1) parar escrita
docker compose stop backend worker worker-beat

# 2) aplicar o compose integrado
Copy-Item ops\dev\docker-compose.integrated.yml docker-compose.yml

# 3) recriar dados (porta só em loopback, secrets) e alinhar a senha do superusuário
docker compose up -d --no-deps postgres redis
Get-Content ops\db\set-superuser-password.sql | docker compose exec -T postgres sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres'

# 4) papéis + transferência de propriedade, depois migrations como rd_owner
docker compose up --no-deps db-init          # conferir "0 pendente(s) por lock"
docker compose up --no-deps migrate

# 5) aplicação e demais serviços
docker compose up -d --build
docker compose ps                            # todos healthy
```

Rollback: `Copy-Item stack\bk\002_snapshot\docker-compose.pre-janela.yml docker-compose.yml`,
restaurar a senha antiga do superusuário a partir do `.env`
(`docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres'` com
`\getenv pw POSTGRES_PASSWORD` + `ALTER ROLE CURRENT_USER PASSWORD :'pw';`) e
`docker compose up -d`. A transferência de propriedade para `rd_owner` não precisa ser desfeita.

## 10. Produção (Portainer) — procedimento (não executado)

1. VPS com Docker + Portainer protegido (HTTPS, 2FA, sem exposição pública da API do Docker).
2. Imagens: `bash ops/release/build-images.sh <versão> <registro>` e publicar no registro
   privado (decisão do proprietário).
3. Por instância (white-label), no host:
   ```bash
   install -d -m 700 /opt/rd-midiaindoor/instancias/<inst>/{secrets,config}
   RD_SECRETS_DIR=/opt/rd-midiaindoor/instancias/<inst>/secrets bash ops/dev/generate-secrets.sh
   # ↑ inclui manifest_signing_key (PEM P-256) — copiar para fora da VPS (§4.1)
   # + criar cloudpaypag_api_key, cloudpaypag_webhook_secret, smtp_password (valores do proprietário)
   cp ops/portainer/alertmanager.prod.example.yml /opt/rd-midiaindoor/instancias/<inst>/config/alertmanager.yml
   cp ops/portainer/player_release_public_keys.example.json /opt/rd-midiaindoor/instancias/<inst>/config/player_release_public_keys.json
   # variáveis obrigatórias fora de dev (o backend recusa subir/assinar sem elas):
   #   RD_PLAYER_ALLOWED_ORIGINS (https apenas), RD_INSTANCE_SLUG, domínios, SMTP — ver instance.env.example
   # 1ª inicialização (AÇÃO 003, §4.3): RD_BOOTSTRAP_DUAL_MASTER_APPROVAL, RD_BOOTSTRAP_MASTER_EMAIL
   #   (e-mail real), RD_BOOTSTRAP_MASTER_NAME na stack; senha inicial = bootstrap_master_password
   #   (gerado acima). Sem eles o serviço bootstrap sai com código 2 e a API não sobe.
   ```
4. DNS: `painel.<dominio>` e `player.<dominio>` → IP da VPS.
5. Certificados (1ª emissão, antes do nginx subir — porta 80 livre):
   ```bash
   docker run --rm -p 80:80 -v <inst>_letsencrypt:/etc/letsencrypt certbot/certbot:v5.0.0 \
     certonly --standalone --cert-name painel.<dominio> -d painel.<dominio> -m <email> --agree-tos -n
   # repetir para player.<dominio>
   ```
   Renovação: serviço `certbot` (webroot) + recarga automática do nginx a cada 6 h.
6. Portainer → Stacks → Add stack (nome = `RD_INSTANCE`) → colar
   `ops/portainer/docker-compose.prod.yml` → variáveis de `ops/portainer/instance.env.example`
   preenchidas → Deploy.
7. Conferir: `bootstrap` terminou com código 0 (logs sem segredos: modo, versão e e-mail mascarado);
   todos healthy; `https://painel.<dominio>/health`; Grafana via túnel SSH
   (`ssh -L 3005:127.0.0.1:3005 <vps>`); `docker compose --profile ops run --rm db-restore`.
8. Várias instâncias na mesma VPS: cada stack publica a borda em portas internas
   (`RD_HTTP_BIND=127.0.0.1:18080`, `RD_HTTPS_BIND=127.0.0.1:18443`) e um roteador SNI
   compartilhado (nginx `stream` com `ssl_preread`, passthrough TLS por `server_name`;
   HTTP:80 roteado por `Host` para o desafio ACME) encaminha para cada instância.

Produção nunca expõe PostgreSQL/Redis/Prometheus/Loki; a rede `internal` não tem saída.

## 11. Higiene de publicação

- `.gitignore` (raiz): ignora `stack/`, `.env*` (exceto `.env.example`), `secrets/`, chaves,
  dependências, builds, caches, cobertura (`htmlcov/`, `.coverage`), artefatos de players
  (`*.apk`, `*.exe`, …), credenciais de contas de TESTE do CLI dev-only (`backend/.devtest/`)
  e ferramentas locais do proprietário (`.agents/`, `.claude/`, `skills-lock.json`).
- `.dockerignore` na raiz e em cada contexto (`backend/`, `frontend/`, `player-web/`,
  `nginx/`, `ops/`): `.env`, `secrets/`, caches e dependências do host nunca entram nas imagens.
- **Memória privada preservada**: ignorar não apaga. `stack/` continua no disco local,
  com logs, backups de referência e scripts do proprietário; apenas não é publicado nem
  empacotado. Nenhum contexto de build inclui a raiz, portanto `stack/` é inalcançável
  por `COPY`.
- `.gitattributes`: scripts/configs com LF (CRLF quebra `sh` e nginx nos containers).

## 12. DEV online na VPS de testes — Swarm, deploy sem queda e gatilho por evento (AÇÕES 005/007)

Topologia de uma VPS com várias instâncias (dev agora; dev-teste e outras depois). Host em
**Docker Swarm de um nó** (`docker swarm init`; portas do cluster fechadas no firewall; sem
`live-restore` no daemon — incompatível com Swarm).

| Peça | Arquivo | Tipo | Papel |
|---|---|---|---|
| Borda (1 por VPS) | `ops/edge/docker-compose.edge.yml` (stack `rd-edge`) | compose | Traefik 80/443 + Let's Encrypt; provider **docker por eventos** via socket-proxy somente leitura; rede `rd-edge` overlay attachable, Traefik com IP fixo `10.250.0.2` (fora do `ip_range` dinâmico) |
| Dados da instância | `docker-compose.dev-data.yml` (stack `<inst>`, ex.: `rd-dev`) | compose | PostgreSQL, Redis, papéis, backup, Mailpit, observabilidade; cria a overlay `<inst>-internal`. **Não muda a cada deploy** |
| Aplicação da instância | `docker-compose.dev-app.yml` (stack `<inst>-app`) | **Swarm** | nginx da instância, painel, Player Web, backend, worker, beat, jobs `migrate`/`bootstrap`; imagens do GHCR; troca **start-first** com rollback |
| Imagens + gatilho | `.github/workflows/dev-images.yml` | GitHub Actions | push em `dev` → build de backend/frontend/player-web/nginx → GHCR (`:dev`, `:sha-<commit>`) → POST no **webhook da stack** `<inst>-app` |
| Variáveis | `ops/portainer/dev-online.env.example` | — | as mesmas nas duas stacks da instância |

**Por que não cai (medido na AÇÃO 006/007):** o redeploy Git do Portainer em stack *compose* recria
TODOS os containers (inclusive o banco) — ~40 s fora do ar. Em *Swarm*, o deploy só atualiza
serviços cuja imagem/spec mudou; cada um sobe a tarefa nova, espera o healthcheck (`start_interval`
de 1–2 s) e só então derruba a antiga:
- backend/worker/bootstrap só sobem depois que o job `migrate` levou o schema ao head da imagem
  (`backend/scripts/wait_for_schema.py`; banco à FRENTE da imagem — rollback — também libera);
- backend, painel e player são alcançados pelo nginx via VIP do Swarm (só tarefas saudáveis);
- o nginx da instância recebe tráfego do Traefik (provider docker, por eventos) com **health check
  ativo** em `/__rd_ready` (1 s): no SIGTERM o nginx antigo responde 503 ali, sai do balanceamento e
  só encerra 4 s depois (SIGQUIT); borda com `providersThrottleDuration=0s`, `dialTimeout=500ms` e retry;
- o painel recarrega sozinho uma vez se um chunk da versão anterior sumiu (`frontend/src/lib/chunkReload.ts`).

Medição de aceitação (AÇÃO 007): 3 trocas do nginx + 1 troca simultânea de nginx, painel, player,
backend e worker — sonda na VPS a cada 50 ms (3 URLs): **7 626 requisições, 0 falhas**; log de acesso
do Traefik: **9 210 requisições roteadas, 0 erros 5xx/conexão**. Reboot da VPS: tudo volta sozinho.

**Regras obrigatórias para manter "sem queda":**
- migrations **compatíveis com a versão anterior** (expandir → implantar → contrair): as duas
  versões convivem alguns segundos;
- **nunca polling** para deploy (decisão do proprietário): gatilho só por evento (webhook do CI);
- mudanças na stack de **dados** (`docker-compose.dev-data.yml`, `ops/`, `observability/`) NÃO
  passam pelo CI: reimplantar de propósito, em janela (o Portainer recria o banco);
- segredos/config da aplicação são **externos** (`ops/swarm/rd-instance-init.sh`): o Portainer CE
  2.45.2 quebra caminhos absolutos de `secrets: file:` em stack Swarm ("/data/compose/<id>//opt/…")
  e o deploy falha em silêncio (o webhook responde 204 sem implantar). Segredos do Swarm são
  imutáveis: para trocar um valor, crie `<inst>_<nome>_v2`, aponte a stack para ele e remova o antigo;
- **labels do Traefik no nginx** (domínios, routers, middlewares) não mudam num deploy comum: durante
  o start-first os dois containers convivem e, com labels diferentes, o Traefik descarta o router
  ("defined multiple times with different configurations") ⇒ mudança de label = janela;
- o webhook da stack é criado com **"Re-pull image" e "Force redeployment"** ligados (sem o force, o
  Portainer ignora o webhook quando o commit já foi implantado e a imagem nova nunca entraria);
- o CI publica `:sha-<commit>` e só move `:dev` depois de **todas** as imagens construídas; imagem
  sem mudança mantém o mesmo digest e o Swarm não toca no serviço;
- o webhook responde antes do fim do rollout: falha de rollout (rollback automático) aparece no
  Portainer (serviço → *Update status*) e nos logs — conferir após mudanças grandes.

Ajustes da borda medidos (AÇÃO 007, serviço de teste com 3 trocas e requisição a cada 50 ms): Traefik
padrão = 1 falha por troca (o throttle de 2 s mantém o IP do container que saiu e a conexão espera
o timeout); `providersThrottleDuration=0s` + `dialTimeout=500ms` + retry = 0 falhas (pior latência ~1 s).

Preparação do host (uma vez, root): Docker CE + compose; firewall 22/80/443/9443; `docker swarm init
--advertise-addr <ip>`; Portainer CE montando `/opt/rd-midiaindoor/instancias` somente leitura no
mesmo caminho (o Portainer lê dali os arquivos de `secrets:`/`configs:`).

Por instância (ex.: `rd-dev`):

```bash
I=/opt/rd-midiaindoor/instancias/rd-dev
install -d -m 700 $I/secrets $I/config
RD_SECRETS_DIR=$I/secrets bash ops/dev/generate-secrets.sh       # + manifest_signing_key
# segredo do provedor FALSO (assina/valida webhooks simulados) — 64 alfanuméricos
( umask 077; openssl rand -base64 96 | tr -dc 'A-Za-z0-9' | head -c 64 > $I/secrets/cloudpaypag_webhook_secret )
# 0444 como os demais: o backend roda como "rdapp" e lê o arquivo montado
chmod 444 $I/secrets/cloudpaypag_webhook_secret
echo '{}' > $I/config/player_release_public_keys.json
# alertmanager.yml: smarthost mailpit:1025 sem TLS (alertas no Mailpit da instância)
```

No host, uma vez por instância (cria a overlay `<inst>-internal`, os segredos e o config Swarm):

```bash
RD_INSTANCE=rd-dev RD_SECRETS_DIR=$I/secrets RD_CONFIG_DIR=$I/config bash ops/swarm/rd-instance-init.sh
```

Ordem no Portainer (todas "Repository", referência `refs/heads/dev`, variáveis do `.env.example`):
1. `rd-edge` → `ops/edge/docker-compose.edge.yml` (cria a overlay `rd-edge`);
2. `rd-dev` → `docker-compose.dev-data.yml` (banco, Redis… na rede `rd-dev-internal`);
3. `rd-dev-app` → `docker-compose.dev-app.yml` como stack **Swarm**, com **webhook** ligado e
   "re-pull image"; a URL do webhook vai para o segredo `PORTAINER_WEBHOOK_DEV_APP` do repositório.

Acesso administrativo (somente loopback da VPS, via túnel SSH):
`ssh -L 8025:127.0.0.1:8025 -L 3005:127.0.0.1:3005 root@<vps>` → Mailpit `http://localhost:8025`,
Grafana `http://localhost:3005`. Portainer: `https://<vps>:9443`.

Fluxo de versões planejado (dev-teste): tag `vX.Y.Z-beta.N` → as mesmas imagens com tag de versão
→ stacks `rd-dev-teste` / `rd-dev-teste-app` com `RD_IMAGE_TAG=<versão>` atrás da mesma borda.
