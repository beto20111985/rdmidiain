#!/usr/bin/env bash
# =============================================================================
# RD Mídia Indoor — gera os Docker secrets locais (um arquivo por segredo).
#
#   ops/dev/generate-secrets.sh                 # cria SÓ os que faltam (idempotente)
#   ops/dev/generate-secrets.sh --rotate NOME   # regenera um segredo específico
#
# - Destino: ./secrets/ (ou $RD_SECRETS_DIR). Diretório ignorado por .gitignore e
#   .dockerignore — nunca entra em git, imagem ou contexto de build.
# - NUNCA imprime valores; só o nome e a ação (gerado/mantido).
# - Valores alfanuméricos (seguros em URLs de conexão), sem quebra de linha final.
# - Permissões (Linux/VPS): diretório 0700 (só o dono atravessa) e arquivos 0444
#   (o container recebe o arquivo por bind mount e precisa lê-lo com usuário não-root).
# - Rotação de senhas de banco/Redis exige aplicar a nova senha no serviço
#   (ver ops/README.md — "Rotação de segredos").
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIR="${RD_SECRETS_DIR:-$ROOT/secrets}"

# nome:tamanho
SECRETS=(
  "postgres_superuser_password:48"
  "db_owner_password:48"
  "db_app_password:48"
  "db_backup_password:48"
  "redis_password:48"
  "app_secret_key:64"
  "jwt_secret_key:64"
  "data_encryption_key:64"
  "backup_passphrase:64"
  "grafana_admin_password:32"
  # AÇÃO 003: senha INICIAL do Master primário (só o serviço bootstrap a monta; usada uma
  # única vez na 1ª inicialização — rotacionar depois NÃO muda a senha do Master).
  "bootstrap_master_password:32"
)

ROTATE=""
if [ "${1:-}" = "--rotate" ]; then
  ROTATE="${2:?uso: $0 --rotate NOME}"
fi

gen() {
  # Gera $1 caracteres [A-Za-z0-9] a partir de /dev/urandom (sem pipe para head,
  # evitando SIGPIPE com pipefail).
  local want="$1" out="" chunk
  while [ "${#out}" -lt "$want" ]; do
    chunk="$(LC_ALL=C tr -dc 'A-Za-z0-9' < <(head -c 512 /dev/urandom))"
    out="$out$chunk"
  done
  printf '%s' "${out:0:$want}"
}

gen_for() {
  # $1 = nome, $2 = tamanho. Senhas de LOGIN precisam passar na política da aplicação
  # (letras E dígitos): regenera até conter os dois (nunca exibe o valor).
  local name="$1" len="$2" v
  while :; do
    v="$(gen "$len")"
    case "$name" in
      bootstrap_master_password)
        [[ "$v" =~ [A-Za-z] && "$v" =~ [0-9] ]] || continue ;;
    esac
    printf '%s' "$v"
    return 0
  done
}

umask 077
mkdir -p "$DIR"
chmod 700 "$DIR" 2>/dev/null || true

found_rotate=0
for entry in "${SECRETS[@]}"; do
  name="${entry%%:*}"
  len="${entry##*:}"
  file="$DIR/$name"
  if [ -n "$ROTATE" ] && [ "$ROTATE" != "$name" ]; then
    continue
  fi
  [ -n "$ROTATE" ] && found_rotate=1
  if [ -s "$file" ] && [ -z "$ROTATE" ]; then
    echo "mantido: $name"
    continue
  fi
  tmp="$file.tmp.$$"
  gen_for "$name" "$len" > "$tmp"
  chmod 444 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$file"
  echo "gerado:  $name"
done

# ─── Chave ES256 de assinatura de MANIFESTO (PEM PKCS8, P-256) ────────────────
# Usada em produção (MANIFEST_SIGNING_KEY_FILE). Em dev local o backend usa a chave
# própria persistida no volume media-storage. ROTACIONAR = re-parear TODAS as telas
# (ver ops/README.md §4.1); por isso --rotate exige confirmação explícita.
MK="$DIR/manifest_signing_key"
if [ -z "$ROTATE" ] || [ "$ROTATE" = "manifest_signing_key" ]; then
  [ "$ROTATE" = "manifest_signing_key" ] && found_rotate=1
  if [ -s "$MK" ] && [ -z "$ROTATE" ]; then
    echo "mantido: manifest_signing_key"
  elif [ "$ROTATE" = "manifest_signing_key" ] && [ "${RD_CONFIRM_MANIFEST_KEY_ROTATION:-}" != "sim" ]; then
    echo "recusado: rotacionar manifest_signing_key exige re-parear todas as telas;" >&2
    echo "          defina RD_CONFIRM_MANIFEST_KEY_ROTATION=sim para confirmar" >&2
    exit 3
  else
    command -v openssl >/dev/null 2>&1 || { echo "openssl não encontrado (necessário para manifest_signing_key)" >&2; exit 4; }
    tmp="$MK.tmp.$$"
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$tmp" 2>/dev/null
    chmod 444 "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$MK"
    echo "gerado:  manifest_signing_key (PEM PKCS8 P-256)"
  fi
fi

if [ -n "$ROTATE" ] && [ "$found_rotate" -eq 0 ]; then
  echo "segredo desconhecido: $ROTATE" >&2
  exit 2
fi
echo "secrets em: $DIR (valores não exibidos)"
