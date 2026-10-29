# Alertmanager com configuração embutida (produção: substituir por config da instância).
FROM prom/alertmanager:v0.28.1
COPY alertmanager.yml /etc/alertmanager/alertmanager.yml
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:9093/-/ready || exit 1
