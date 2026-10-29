> **Revisado na AÇÃO 002 (2026-10-08): EVOLUÍDO.** O padrão vigente é `run_async(...)` + `worker_session` (engine com NullPool por execução), definido nas tasks (`backend/app/workers/tasks/*.py`) e no `docs/backend/GUIA_FUNDACAO.md`; o worker não depende de memória do processo FastAPI (eventos em tempo real via Redis Pub/Sub).

# ADR-004: Padrão asyncio.run() em Tasks Celery

**Status:** Aceito  
**Data:** 2026-10-07  
**Contexto:** Celery workers são síncronos; codebase usa SQLAlchemy async

## Decisão

Tasks Celery usam o padrão `asyncio.run(_async_helper())` para executar código assíncrono. A função helper usa `get_db_context()` (context manager async) para sessões DB.

```python
@celery_app.task(bind=True)
def my_task(self, arg: str):
    return asyncio.run(_my_task_async(arg))

async def _my_task_async(arg: str) -> dict:
    async with get_db_context() as db:
        # ... SQLAlchemy async queries
```

## Justificativa

- Celery 5.x não tem suporte nativo a async workers sem gevent/eventlet.
- `asyncio.run()` cria e fecha um event loop limpo por task — sem state compartilhado.
- Alternativas (celery-gevent, celery-aiohttp) introduzem complexidade de deploy desnecessária.
- Pattern é testável: a função `_async_helper` pode ser testada diretamente.

## Consequências

- Cada task cria um novo event loop e connection pool — overhead aceitável para tarefas background.
- Nunca chamar `asyncio.run()` dentro de um context assíncrono (causa erro "nested event loop").
- Workers não devem chamar `ws_manager` diretamente — a notificação WebSocket deve ser feita pelo endpoint FastAPI, não pelo worker.
