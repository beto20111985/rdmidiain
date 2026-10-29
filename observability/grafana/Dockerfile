# Grafana com datasources e dashboards provisionados (produção: sem bind mount).
# Senha do admin via Docker secret: GF_SECURITY_ADMIN_PASSWORD__FILE.
FROM grafana/grafana:12.2.0
COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/   /var/lib/grafana/dashboards/
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:3000/api/health || exit 1
