# RD Mídia Indoor — observador de deploy (AÇÃO 007): eventos do Docker → alertas no Alertmanager.
FROM docker:29.1-cli
COPY --chmod=0755 rd-deploy-watcher.sh /usr/local/bin/rd-deploy-watcher
# o processo só lê eventos/estado do Docker (socket montado somente leitura)
CMD ["rd-deploy-watcher"]
