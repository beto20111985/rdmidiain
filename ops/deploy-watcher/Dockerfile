# RD Mídia Indoor — observador de deploy (AÇÃO 007): eventos do Docker → alertas no Alertmanager.
FROM docker:29.1-cli
COPY --chmod=0755 rd-deploy-watcher.sh /usr/local/bin/rd-deploy-watcher
# lê eventos/estado do Docker pelo socket-proxy somente leitura (DOCKER_HOST na stack)
CMD ["rd-deploy-watcher"]
