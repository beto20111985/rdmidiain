"""
Apoio aos testes do W-INV (inventário/manifesto): fábricas mínimas de dados REAIS no
PostgreSQL de teste (organização, estabelecimento, tela, mídia READY, criativo,
campanha com revisão vigente) e padrões de contratação.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

ALPHA = "ABCDEFGHJMNPQRSTUVYZ23456789"


def ref(obj):
    """Cópia simples das colunas (imune à expiração do ORM após rollback/commit)."""
    from types import SimpleNamespace

    return SimpleNamespace(**{c.key: getattr(obj, c.key) for c in obj.__table__.columns})


def code6() -> str:
    return "".join(secrets.choice(ALPHA) for _ in range(6))


async def db_today(db: AsyncSession, tz: str = "America/Sao_Paulo") -> date:
    return (await db.execute(text("SELECT (now() AT TIME ZONE :tz)::date"), {"tz": tz})).scalar_one()


async def mk_org(db: AsyncSession, name: Optional[str] = None):
    from app.models.organizations import Organization

    o = Organization(id=uuid.uuid4(), name=name or f"Org {secrets.token_hex(3)}",
                     document=str(10**13 + secrets.randbelow(10**13)), document_type="CNPJ")
    db.add(o)
    await db.commit()
    return ref(o)


async def mk_establishment(db: AsyncSession, org, tz: str = "America/Sao_Paulo", state: str = "GO",
                           city: str = "Goiânia"):
    from app.core.authz import city_key
    from app.models.establishments import Establishment

    region = {"GO": "CENTRO_OESTE", "SP": "SUDESTE", "AM": "NORTE", "AC": "NORTE"}.get(state, "SUDESTE")
    e = Establishment(id=uuid.uuid4(), org_id=org.id, name=f"Est {secrets.token_hex(3)}", city=city,
                      city_key=city_key(state, city), state=state, region=region, timezone=tz, is_active=True)
    db.add(e)
    await db.commit()
    return ref(e)


async def mk_screen(db: AsyncSession, est, *, state: str = "GO", status: str = "ACTIVE", sellable: bool = True):
    from app.models.screens import Screen

    s = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=f"TEL-{state}-{code6()[:4]}",
               name=f"Tela {secrets.token_hex(2)}", status=status, sellable=sellable)
    db.add(s)
    await db.commit()
    return ref(s)


async def mk_media(db: AsyncSession, org, *, ready: bool = True, kind: str = "VIDEO"):
    from app.models.media import MediaFile

    sha = hashlib.sha256(secrets.token_bytes(16)).hexdigest()
    m = MediaFile(id=uuid.uuid4(), org_id=org.id, name="m", original_filename="m.mp4", kind=kind,
                  source_format="mp4", source_mime="video/mp4", source_size_bytes=1000, source_sha256=sha,
                  status="READY" if ready else "PROCESSING", storage_key=f"test/{sha}" if ready else None,
                  mime_type="video/mp4", size_bytes=12345 if ready else None, sha256=sha if ready else None,
                  width=1920 if ready else None, height=1080 if ready else None, duration_ms=30000)
    db.add(m)
    await db.commit()
    return ref(m)


async def mk_creative(db: AsyncSession, org, *, kind: str = "EXTERNAL", duration: int = 30, status: str = "APROVADO",
                      media=None, ready: bool = True):
    from app.models.campaigns import Creative

    media = media or await mk_media(db, org, ready=ready)
    c = Creative(id=uuid.uuid4(), org_id=org.id, code=f"RD-{code6()}", name="Peça", kind=kind,
                 media_file_id=media.id, duration_seconds=duration, status=status,
                 technical_status="VALIDO" if ready else "PENDENTE")
    db.add(c)
    await db.commit()
    return ref(c)


async def mk_campaign(db: AsyncSession, org, *, status: str = "RASCUNHO", name: Optional[str] = None):
    from app.models.campaigns import Campaign

    c = Campaign(id=uuid.uuid4(), org_id=org.id, name=name or f"Campanha {secrets.token_hex(3)}", status=status)
    db.add(c)
    await db.commit()
    return ref(c)


async def link(db: AsyncSession, campaign, creative, order: int = 0):
    from app.models.campaigns import CampaignCreative

    db.add(CampaignCreative(id=uuid.uuid4(), campaign_id=campaign.id, creative_id=creative.id, order=order,
                            is_active=True))
    await db.commit()


def pattern(d_from: date, d_to: date, **kw) -> dict:
    p = {"date_from": d_from.isoformat(), "date_to": d_to.isoformat(), "days_mask": 127,
         "time_bands": [[0, 1440]], "duration_units": 1, "freq_kind": "PER_HOUR", "freq_value": 1,
         "min_interval_seconds": 0, "priority": "STANDARD", "allow_same_advertiser_consecutive": False,
         "exclusivity": "NONE"}
    p.update(kw)
    return p


async def make_live(db: AsyncSession, campaign, hold_id, screen_ids, creative_ids, pat: dict,
                    status: str = "APROVADA", inherited_hold_ids=(), start_date=None):
    """Torna a campanha tocável como o W-CAMP faria após aprovar: revisão vigente com o hold."""
    from app.models.campaigns import Campaign, CampaignRevision

    spec = {"start_date": pat["date_from"], "end_date": pat["date_to"], "days_mask": pat["days_mask"],
            "time_bands": pat["time_bands"], "duration_s": pat["duration_units"] * 30, "freq_kind": pat["freq_kind"],
            "freq_value": pat["freq_value"], "specific_times": pat.get("specific_times") or [],
            "min_interval_s": pat["min_interval_seconds"], "priority": pat["priority"],
            "allow_same_advertiser_consecutive": pat["allow_same_advertiser_consecutive"],
            "exclusivity": pat["exclusivity"], "period_type": "AVULSO", "targets": []}
    rev = CampaignRevision(id=uuid.uuid4(), campaign_id=campaign.id, number=1, status="APROVADA", spec=spec,
                           screen_ids=[str(s) for s in screen_ids], creative_ids=[str(c) for c in creative_ids],
                           hold_id=hold_id,
                           change_summary={"hold_ids": [str(h) for h in inherited_hold_ids]} if inherited_hold_ids
                           else None)
    db.add(rev)
    await db.flush()
    c = await db.get(Campaign, campaign.id)
    c.live_revision_id = rev.id
    c.status = status
    c.start_date = start_date or date.fromisoformat(pat["date_from"])
    c.end_date = date.fromisoformat(pat["date_to"])
    await db.commit()
    return ref(rev)


async def competitor_link(db: AsyncSession, org_a, org_b):
    from app.models.taxonomy import CompetitorLink

    a, b = sorted([org_a.id, org_b.id])  # CHECK org_a_id < org_b_id (ordem de bytes do uuid = ordem do int)
    db.add(CompetitorLink(id=uuid.uuid4(), org_a_id=a, org_b_id=b, reason="concorrentes (teste)"))
    await db.commit()


async def backdate_hold(db: AsyncSession, hold_id: Any, seconds: int) -> None:
    """Simula a passagem do RELÓGIO DO BANCO (fonte da verdade do TTL) recuando os instantes."""
    await db.execute(text("""
        UPDATE inventory_holds SET created_at = created_at - make_interval(secs => :s),
                                   hold_expires_at = hold_expires_at - make_interval(secs => :s)
         WHERE id = CAST(:h AS uuid)"""), {"s": seconds, "h": str(hold_id)})
    await db.execute(text("""
        UPDATE booking_screens SET hold_expires_at = hold_expires_at - make_interval(secs => :s)
         WHERE hold_id = CAST(:h AS uuid) AND hold_expires_at IS NOT NULL"""), {"s": seconds, "h": str(hold_id)})
    await db.commit()


class SysCtx:
    """Contexto de sistema (como as tasks/serviços chamam o inventário)."""

    kind = "system"
    origin = "test"
    user_id = None
    impersonation = None
