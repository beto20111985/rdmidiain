"""system_seeds — AÇÃO 002 / W-DB

Seeds OBRIGATÓRIOS de sistema (não são dados de demonstração), idempotentes:

- políticas de conteúdo padrão (``app.modules.taxonomy.service.ensure_default_content_policies``:
  apostas, conteúdo adulto e armas BLOQUEADOS; bebidas alcoólicas, tabaco, política e
  farmacêuticos PERMITIDOS — administráveis pela RD, auditado ``content_policy.defaults_seeded``);
- taxonomia de referência editável (``ensure_reference_taxonomy``: setores/categorias de
  estabelecimento e segmentos de anúncio de partida);
- política de rotação GLOBAL 120 s interno + 120 s externo (``app.modules.inventory.ddl
  .GLOBAL_POLICY_SEED``, id fixo ``00000000-0000-4000-8000-000000000120``).

Os serviços são assíncronos (AsyncSession); aqui rodam sobre a conexão síncrona da migration
por um adaptador que expõe a mesma interface (``execute/get/flush/add``) e executa a corrotina
sem event loop — se algum serviço tentar I/O assíncrono real, a migration FALHA (nunca
"pula" o seed). Tudo dentro da transação da migration (SAVEPOINT próprio da Session).

Downgrade: no-op documentado (os seeds são removidos junto com as tabelas no downgrade da
0004; reaplicar a 0005 é idempotente).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class _SyncSessionAsAsync:
    """Interface mínima de AsyncSession sobre uma Session síncrona (somente para seeds)."""

    def __init__(self, session):
        self._s = session

    async def execute(self, *args, **kwargs):
        return self._s.execute(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return self._s.scalar(*args, **kwargs)

    async def scalars(self, *args, **kwargs):
        return self._s.scalars(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return self._s.get(*args, **kwargs)

    async def flush(self, *args, **kwargs):
        self._s.flush(*args, **kwargs)

    def add(self, obj):
        self._s.add(obj)

    def add_all(self, objs):
        self._s.add_all(objs)


def _run(coro):
    """Executa uma corrotina que não faz I/O assíncrono real (o adaptador responde na hora)."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise RuntimeError("0005: o seed tentou I/O assíncrono real — não é seguro dentro da migration")


def upgrade() -> None:
    from sqlalchemy.orm import Session

    from app.modules.inventory import ddl as inventory_ddl
    from app.modules.taxonomy import service as taxonomy

    session = Session(bind=op.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        db = _SyncSessionAsAsync(session)
        _run(taxonomy.ensure_default_content_policies(db))
        _run(taxonomy.ensure_reference_taxonomy(db))
        session.commit()  # libera o SAVEPOINT; a transação da migration continua aberta
    finally:
        session.close()
    op.execute(inventory_ddl.GLOBAL_POLICY_SEED)


def downgrade() -> None:
    # Seeds de sistema não são apagados aqui (podem ter sido editados pela RD e referenciados);
    # o downgrade da 0004 remove as tabelas inteiras.
    pass
