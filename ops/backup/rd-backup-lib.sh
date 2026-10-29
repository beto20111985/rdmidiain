#!/bin/sh
# =============================================================================
# RD Mídia Indoor — funções comuns de backup/restore (sourced; POSIX sh/busybox).
# Nunca imprime senhas/passphrase.
# =============================================================================

RD_BACKUP_DIR="${RD_BACKUP_DIR:-/backups}"
RD_DB_NAME="${RD_DB_NAME:-${POSTGRES_DB:-rdmidiaindoor}}"
PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"
export PGHOST PGPORT

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "${RD_LOG_TAG:-db-backup}" "$*"; }
err() { log "ERRO: $*" >&2; }

rd_read_secret() {
  if [ ! -r "$1" ]; then
    err "segredo ausente/ilegível: $1"
    return 1
  fi
  tr -d '\r\n' < "$1"
}

# rd_setup_pgpass <usuario> <arquivo_senha|""> — cria PGPASSFILE temporário.
# Sem arquivo de senha usa $POSTGRES_PASSWORD (fallback transitório do dev).
rd_setup_pgpass() {
  _user="$1"
  _pwfile="${2:-}"
  if [ -n "$_pwfile" ] && [ -r "$_pwfile" ]; then
    _pw="$(rd_read_secret "$_pwfile")"
  elif [ -n "${POSTGRES_PASSWORD:-}" ]; then
    _pw="$POSTGRES_PASSWORD"
  else
    err "senha do usuário $_user não fornecida"
    return 1
  fi
  unset POSTGRES_PASSWORD
  RD_PGPASSFILE="$(mktemp)"
  chmod 600 "$RD_PGPASSFILE"
  _esc_user="$(printf '%s' "$_user" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g')"
  _esc_pw="$(printf '%s' "$_pw" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g')"
  printf '%s:%s:*:%s:%s\n' "$PGHOST" "$PGPORT" "$_esc_user" "$_esc_pw" > "$RD_PGPASSFILE"
  unset _pw _esc_pw
  PGPASSFILE="$RD_PGPASSFILE"
  PGUSER="$_user"
  export PGPASSFILE PGUSER
}

rd_setup_gnupg() {
  GNUPGHOME="$(mktemp -d)"
  chmod 700 "$GNUPGHOME"
  export GNUPGHOME
}

rd_cleanup_secrets() {
  [ -n "${RD_PGPASSFILE:-}" ] && rm -f "$RD_PGPASSFILE"
  [ -n "${GNUPGHOME:-}" ] && rm -rf "$GNUPGHOME"
  return 0
}

# Consulta de contagem EXATA de linhas por tabela do schema public
# (saída: tabela|linhas). Usada no backup (mesmo snapshot do pg_dump) e no restore.
RD_COUNT_SQL="SELECT c.relname, (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p') ORDER BY 1"

rd_gpg_encrypt() {
  # stdin → $1 (AES256 simétrico, passphrase por arquivo; sem compressão: o dump já é comprimido)
  gpg --batch --yes --quiet --pinentry-mode loopback \
      --passphrase-file "$RD_PASSPHRASE_FILE" \
      --symmetric --cipher-algo AES256 \
      --s2k-mode 3 --s2k-digest-algo SHA512 --s2k-count 65011712 \
      --compress-algo none \
      -o "$1"
}

rd_gpg_decrypt() {
  # $1 → stdout
  gpg --batch --quiet --pinentry-mode loopback \
      --passphrase-file "$RD_PASSPHRASE_FILE" \
      --decrypt "$1"
}

rd_gpg_decrypt_to() {
  # $1 → arquivo $2 (sem pipe). O código de saída do gpg é o veredito da decifragem:
  # passphrase errada, MDC/integridade inválida ou arquivo truncado ⇒ ≠ 0 (e $2 é apagado).
  # Use um diretório PRIVADO (umask 077) e apague $2 depois (texto claro em disco).
  rm -f "$2"
  if ! gpg --batch --quiet --yes --pinentry-mode loopback \
         --passphrase-file "$RD_PASSPHRASE_FILE" \
         --decrypt -o "$2" "$1"; then
    rm -f "$2"
    return 1
  fi
  [ -s "$2" ] || { rm -f "$2"; return 1; }
}
