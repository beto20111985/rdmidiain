# RD Mídia Indoor — Backend Dockerfile
# Python 3.12 + FastAPI + Celery

FROM python:3.12-slim AS base

# Dependências de sistema (FFmpeg para transcoding)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─── DEPS LAYER ─────────────────────────────────────────────────────────────
FROM base AS deps
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ─── DEVELOPMENT ─────────────────────────────────────────────────────────────
FROM deps AS development
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY . .
# --reload-dir app: observa só o código da app (observar /app inteiro — tests/, media/,
# caches — esgotou memória do watchfiles: "Cannot allocate memory (os error 12)").
# ATENÇÃO (uvicorn 0.32.1): o WatchFilesReload SEMPRE observa o diretório de trabalho (/app) e
# DESCARTA --reload-dir que seja subdiretório dele — por isso tests/, migrations/... recarregavam
# o servidor. As exclusões precisam ser caminhos ABSOLUTOS (FileFilter compara com path.parents).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "app", "--reload-exclude", "/app/tests", "--reload-exclude", "/app/migrations", "--reload-exclude", "/app/scripts", "--reload-exclude", "/app/media", "--log-level", "info"]

# ─── PRODUCTION ──────────────────────────────────────────────────────────────
FROM deps AS production
COPY . .
# - Código pertence ao root e é SOMENTE LEITURA para o usuário da aplicação
#   (sem chown -R /app: o processo não consegue alterar o próprio código).
# - Testes, artefatos de teste e credenciais de desenvolvimento NÃO entram na imagem.
# - Apenas /app/media (volume) é gravável pelo usuário da aplicação.
RUN rm -rf tests .devtest .pytest_cache htmlcov .coverage requirements-dev.txt && \
    find /app -name "__pycache__" -type d -prune -exec rm -rf {} + && \
    useradd --no-create-home --shell /bin/false rdapp && \
    mkdir -p /app/media && chown -R rdapp:rdapp /app/media && \
    chmod -R a-w /app/app /app/migrations /app/scripts
USER rdapp
# --workers 4: só é seguro com o gateway WebSocket via Redis Pub/Sub (agente de playback,
# AÇÃO 002). Enquanto houver estado WS em memória, presença/comandos dependem de cair no
# mesmo processo — confirmar com o relatório do W-PLAY antes de usar em produção.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--log-level", "info"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
