# =============================================================================
# RD Mídia Indoor — imagem de ferramentas de banco (db-init, db-backup, db-restore)
# Contexto de build: ./ops   (docker build -f ops/dbtools/Dockerfile ops)
# Mesma versão major do servidor (pg_dump 16 ↔ PostgreSQL 16). Base alpine só com o
# CLIENTE do PostgreSQL (sem servidor e sem o VOLUME de dados herdado da imagem postgres).
# Roda como usuário não-root `postgres` (uid 70); sem cron/root.
# =============================================================================
FROM alpine:3.22

# rclone: ferramenta para o gancho de cópia externa (SFTP/S3/etc.; destino = dependência externa)
# busybox-extras: httpd do sidecar backup-metrics; jq: leitura de status.json
RUN apk add --no-cache postgresql16-client gnupg tzdata rclone busybox-extras jq \
 && (getent group postgres >/dev/null || addgroup -g 70 -S postgres) \
 && (id postgres >/dev/null 2>&1 || adduser -u 70 -S -D -G postgres -H -h /backups -s /sbin/nologin postgres) \
 && mkdir -p /backups /opt/rd/db /usr/local/lib/rd \
 && chown postgres:postgres /backups \
 && chmod 0750 /backups

COPY db/roles.sql                   /opt/rd/db/roles.sql
COPY db/db-init.sh                  /usr/local/bin/rd-db-init
COPY backup/rd-backup-lib.sh        /usr/local/lib/rd/rd-backup-lib.sh
COPY backup/rd-backup.sh            /usr/local/bin/rd-backup
COPY backup/rd-backup-scheduler.sh  /usr/local/bin/rd-backup-scheduler
COPY backup/rd-backup-healthcheck.sh /usr/local/bin/rd-backup-healthcheck
COPY backup/rd-restore.sh           /usr/local/bin/rd-restore
COPY backup/rd-restore-drill.sh     /usr/local/bin/rd-restore-drill
COPY backup/rd-backup-metrics.sh    /usr/local/bin/rd-backup-metrics

RUN chmod 0755 /usr/local/bin/rd-* \
 && chmod 0644 /usr/local/lib/rd/rd-backup-lib.sh /opt/rd/db/roles.sql \
 && sed -i 's/\r$//' /usr/local/bin/rd-* /usr/local/lib/rd/rd-backup-lib.sh /opt/rd/db/roles.sql

ENV TZ=America/Sao_Paulo \
    RD_BACKUP_DIR=/backups \
    PGHOST=postgres \
    PGPORT=5432

USER postgres
WORKDIR /backups

CMD ["rd-backup-scheduler"]
