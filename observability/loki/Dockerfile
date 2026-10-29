# =============================================================================
# Loki com healthcheck: a imagem oficial é distroless (sem shell/wget); copiamos
# só o binário estático do busybox para o HEALTHCHECK. Também cria /loki com dono
# 10001 (usuário da imagem) para o volume nomeado herdar a permissão correta.
# Config embutida (produção); em local o compose sobrepõe por bind mount.
# =============================================================================
FROM busybox:1.37-musl AS bb
RUN mkdir -p /out/loki/chunks /out/loki/rules /out/loki/compactor

FROM grafana/loki:3.6.0
COPY --from=bb /bin/busybox /usr/local/bin/busybox
COPY --from=bb --chown=10001:10001 /out/ /
COPY loki-config.yml /etc/loki/rd-config.yml
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
  CMD ["/usr/local/bin/busybox", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:3100/ready"]
CMD ["-config.file=/etc/loki/rd-config.yml"]
