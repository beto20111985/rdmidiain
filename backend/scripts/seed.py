"""
RD Mídia Indoor — Seed (AÇÃO 002 / W1).

NÃO existe mais senha padrão: a senha hardcoded anterior foi removida (A7 C3).

    docker exec rd-backend python scripts/seed.py            # sincroniza o catálogo RBAC
    docker exec -it rd-backend python -m app.cli bootstrap-masters   # cria os 2 Admin Master
    docker exec rd-backend python -m app.cli seed-dev        # dados DEMO (sem senhas conhecidas)

Ver ``app/cli.py``.
"""
import sys

sys.path.insert(0, "/app")

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    main(["sync-permissions"])
