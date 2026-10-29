"""
RD Mídia Indoor — Webhook CloudPayPag (W-FIN; ADR_009 item 6; doc pública "Webhook & HMAC").

``POST /api/v1/webhooks/cloudpaypag`` (instância padrão) e
``POST /api/v1/webhooks/cloudpaypag/{instance_slug}`` (rota por instância/white-label).

- Header ``X-Webhook-Signature: sha256=<hex>`` OBRIGATÓRIO; HMAC-SHA256 com o
  ``webhook_secret`` da instância sobre ``json.dumps(json.loads(body), sort_keys=True,
  separators=(",", ":"))``; ``compare_digest``. Sem header/assinatura errada/corpo
  alterado ⇒ 401. Instância sem segredo ⇒ 503 (nunca aceita sem assinatura).
- ``cloudpaypag.subconta_id`` diferente da subconta da instância ⇒ 403 (registrado).
- Idempotência: ``webhook_events`` UNIQUE (provider, instância, webhook_event_id) com
  ``ON CONFLICT DO NOTHING``; repetição ⇒ 200 ``duplicate``.
- Responde em < 5 s; o processamento (ledger, campanha, comissão) é no worker.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.financial import payments

router = APIRouter()


async def _ingest(request: Request, db: AsyncSession, slug: Optional[str]) -> JSONResponse:
    body = await request.body()
    res = await payments.ingest_webhook(db, body, dict(request.headers), slug)
    return JSONResponse(status_code=res.status_code, content=res.body)


@router.post("/cloudpaypag")
async def cloudpaypag_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    return await _ingest(request, db, None)


@router.post("/cloudpaypag/{instance_slug}")
async def cloudpaypag_webhook_instance(instance_slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    return await _ingest(request, db, instance_slug[:64])


@router.get("/cloudpaypag/health")
async def webhook_health():
    return {"status": "ok", "endpoint": "cloudpaypag"}
