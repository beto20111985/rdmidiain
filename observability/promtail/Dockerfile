# Promtail com config embutida (produção). Em local o compose sobrepõe por bind mount.
# Healthcheck via bash /dev/tcp (a imagem não tem curl/wget).
FROM grafana/promtail:3.6.0
COPY promtail-config.yml /etc/promtail/rd-config.yml
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/bin/bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/9080 && printf 'GET /ready HTTP/1.0\\r\\n\\r\\n' >&3 && grep -q '200 OK' <&3"]
CMD ["-config.file=/etc/promtail/rd-config.yml", "-config.expand-env=true"]
