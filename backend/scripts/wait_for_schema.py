"""
RD Mídia Indoor — portão de schema para deploy sem queda (AÇÃO 007).

Uso (entrypoint dos serviços Swarm):
    python scripts/wait_for_schema.py -- uvicorn app.main:app ...

Espera o banco chegar ao head das migrations DESTA imagem e então substitui o
processo pelo comando (``os.execvp``) — o serviço novo só fica saudável depois da
migração, enquanto a versão anterior continua atendendo (update start-first).

Decisão (função ``decide``):
- revisão do banco == head da imagem                 → segue;
- revisão do banco DESCONHECIDA pela imagem          → segue: o banco está À FRENTE
  (rollback/versão anterior), coberto pela regra expandir → implantar → contrair;
- revisão conhecida mas anterior ao head, ou vazia   → espera o job "migrate".

Nunca imprime a URL do banco. SIGTERM durante a espera encerra na hora (PID 1).
"""
from __future__ import annotations

import os
import signal
import sys
import time

# Executado como "python scripts/wait_for_schema.py": o Python põe scripts/ no path, não a
# raiz da aplicação — sem isto "import app…" falha (mesma solução de migrations/env.py).
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

READY, AHEAD, WAIT = "ready", "ahead", "wait"


def decide(current: set[str], heads: set[str], known: set[str]) -> str:
    """``current``: revisões no banco; ``heads``: heads da imagem; ``known``: revisões da imagem."""
    if current and current == heads:
        return READY
    if current and not current <= known:
        return AHEAD
    return WAIT


def _state() -> tuple[set[str], set[str], set[str]]:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, pool

    from app.core.config import settings

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = set(script.get_heads())
    known = {rev.revision for rev in script.walk_revisions()}
    engine = create_engine(settings.database_url_sync, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            current = set(MigrationContext.configure(conn).get_current_heads())
    finally:
        engine.dispose()
    return current, heads, known


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    cmd = argv[argv.index("--") + 1:] if "--" in argv else []
    if not cmd:
        print("uso: wait_for_schema.py -- <comando> [args]", file=sys.stderr)
        return 2
    timeout = int(os.environ.get("RD_SCHEMA_WAIT_TIMEOUT", "600"))
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            current, heads, known = _state()
            outcome = decide(current, heads, known)
            detail = f"banco={sorted(current) or '-'} imagem={sorted(heads)}"
        except Exception as exc:  # banco ainda indisponível: tenta de novo
            outcome, detail = WAIT, f"banco indisponível ({exc.__class__.__name__})"
        if outcome != WAIT:
            msg = "schema no head" if outcome == READY else "banco à frente da imagem (versão anterior)"
            print(f"[schema] {msg}: {detail}", file=sys.stderr, flush=True)
            os.execvp(cmd[0], cmd)
        if detail != last:
            print(f"[schema] aguardando migração: {detail}", file=sys.stderr, flush=True)
            last = detail
        if time.monotonic() >= deadline:
            print(f"[schema] tempo esgotado ({timeout} s): {detail}", file=sys.stderr, flush=True)
            return 1
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
