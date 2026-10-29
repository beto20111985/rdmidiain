# Prometheus com configuração e regras embutidas (produção: sem bind mount).
FROM prom/prometheus:v3.5.0
COPY prometheus.yml /etc/prometheus/prometheus.yml
COPY rules/         /etc/prometheus/rules/
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:9090/-/ready || exit 1
