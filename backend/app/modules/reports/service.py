"""
RD Mídia Indoor — Exportação de relatórios, envio por e-mail, relatório MENSAL automático
e COMPROVANTE DE VEICULAÇÃO assinado (AÇÃO 002 / W-BIZ).

Comprovante (ETAPA2 #807–#809; P001 §104):
- conta SOMENTE exibições VÁLIDAS de proof-of-play (``playback_events.valid = true``;
  para dias fora da retenção de 90 dias, ``playback_daily_stats.plays_valid``, que é o
  agregado preservado desses mesmos eventos válidos). A programação planejada NUNCA entra.
- anunciante vê telas por CÓDIGO + REGIÃO (UF), nunca nome/endereço do estabelecimento.
- assinatura: payload canônico (JSON ordenado, sem espaços) assinado com ES256 pela
  chave da INSTÂNCIA (``instance.service.sign_payload``) e impresso numa página de
  verificação do PDF; o PDF final tem SHA-256 assinado (assinatura destacada guardada) e
  pode ser verificado em ``POST /api/v1/reports/proofs/verify``. NÃO é assinatura digital
  certificada ICP-Brasil (não há certificado A1/A3 da RD) — documentado no próprio PDF.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, timedelta
from typing import Any, Optional

import structlog
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.errors import DomainError, NotFound
from app.core.ids import generate_unique, random_safe
from app.core.timeutils import DEFAULT_TIMEZONE, isoformat_z, local_date, utcnow
from app.models.reports import DeliveryProof, ReportExport, ReportRun
from app.modules.reports import data as rdata
from app.modules.reports.exporters import Column, Dataset, export

log = structlog.get_logger("rd.reports")

EXPORT_TTL_DAYS = 30
MAX_EMAIL_ATTACHMENT = 10 * 1024 * 1024
MAX_EXPORT_BYTES = 50 * 1024 * 1024


# ─── Exportação ──────────────────────────────────────────────────────────────

async def create_export(db: AsyncSession, ctx: AuthContext, *, key: str, scope: rdata.Scope, period: rdata.Period,
                        params: dict, fmt: str, email_to_me: bool) -> tuple[ReportExport, bytes]:
    from app.core.storage import build_key, get_storage
    from app.modules.instance.service import get_brand

    if not ctx.has_any("reports.export") and not scope.audience in (rdata.ADVERTISER, rdata.ESTABLISHMENT):
        raise DomainError("FORBIDDEN", "Exportar relatórios exige reports.export.", status=403,
                          details={"permission": "reports.export"})
    ds = await rdata.build(db, ctx, key, scope, period, params)
    b = await get_brand(db)
    brand = b.name
    data, ctype, ext = export(ds, fmt, brand=b, footer=f"{brand} · {ds.title}")
    if len(data) > MAX_EXPORT_BYTES:
        raise DomainError("EXPORT_TOO_LARGE", "Relatório grande demais; reduza o período ou os filtros.", status=413)
    eid = uuid.uuid4()
    obj = await get_storage().put_stream(build_key("reports", "exports", eid, f"relatorio.{ext}"), data, ctype)
    fname = f"{key}_{period.start.isoformat()}_{period.end.isoformat()}.{ext}"
    e = ReportExport(id=eid, report_key=key, params={**{k: str(v) for k, v in params.items() if v is not None},
                                                      "start": period.start.isoformat(), "end": period.end.isoformat(),
                                                      "audience": scope.audience},
                     format=fmt, status="CONCLUIDO", storage_key=obj.key, filename=fname, content_type=ctype,
                     size_bytes=obj.size, sha256=obj.sha256, row_count=len(ds.rows), requested_by=ctx.user_id,
                     org_id=scope.org_id, expires_at=utcnow() + timedelta(days=EXPORT_TTL_DAYS))
    db.add(e)
    await db.flush()
    await audit(db, ctx, "report.exported", "report_export", e.id,
                after={"report": key, "format": fmt, "rows": e.row_count, "sha256": e.sha256}, org_id=scope.org_id)
    if email_to_me:
        e.emailed_to = await email_file(db, [ctx.user], subject=f"{brand} — {ds.title}",
                                        text=f"Segue o relatório \"{ds.title}\" ({period.start.strftime('%d/%m/%Y')} a "
                                             f"{period.end.strftime('%d/%m/%Y')}).\n\n{brand}",
                                        filename=fname, content_type=ctype, data=data)
    return e, data


async def email_file(db: AsyncSession, users: list, *, subject: str, text: str, filename: str, content_type: str,
                     data: bytes) -> int:
    """Envia o arquivo anexado (≤ 10 MB) a cada usuário ativo com e-mail, com HTML na marca da instância.
    Falha → erro (sem sucesso falso)."""
    from app.core.email import OutgoingEmail, get_email_sender
    from app.modules.instance.service import email_html, get_brand

    brand = await get_brand(db)
    html = email_html(brand, subject, text)

    if len(data) > MAX_EMAIL_ATTACHMENT:
        raise DomainError("ATTACHMENT_TOO_LARGE", "Arquivo grande demais para e-mail; faça o download pelo painel.",
                          status=413)
    sent = 0
    for u in users:
        if u is None or not getattr(u, "email", None) or not getattr(u, "is_active", True):
            continue
        await get_email_sender().send(OutgoingEmail(to=u.email, subject=subject, text=text, html=html,
                                                    attachments=[(filename, content_type.split(";")[0], data)]))
        sent += 1
    return sent


# ─── Comprovante de veiculação ───────────────────────────────────────────────

def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


async def _proof_rows(db: AsyncSession, campaign_id: uuid.UUID, p: rdata.Period) -> tuple[list[dict], str]:
    """Por tela × criativo: exibições VÁLIDAS e ms válidos. Brutos (valid=true) na retenção;
    agregados (plays_valid) para dias anteriores."""
    from app.models.establishments import Establishment
    from app.models.playback import PlaybackDailyStat as PDS, PlaybackEvent as PE
    from app.models.screens import Screen

    oldest_raw = local_date(utcnow(), DEFAULT_TIMEZONE) - timedelta(days=rdata.RAW_RETENTION_DAYS - 1)
    acc: dict = {}
    sources = []
    if p.end >= oldest_raw:
        raw_start = max(p.start, oldest_raw)
        q = (select(Screen.friendly_id, Establishment.state, Establishment.region, PE.creative_code,
                    func.count(), func.coalesce(func.sum(PE.actual_ms), 0))
             .select_from(PE).join(Screen, Screen.id == PE.screen_id)
             .outerjoin(Establishment, Establishment.id == PE.establishment_id)
             .where(PE.campaign_id == campaign_id, PE.valid.is_(True), PE.status == "ACCEPTED",
                    PE.local_day >= raw_start, PE.local_day <= p.end)
             .group_by(Screen.friendly_id, Establishment.state, Establishment.region, PE.creative_code))
        for code, uf, region, ccode, n, ms in (await db.execute(q)).all():
            k = (code, ccode)
            a = acc.setdefault(k, {"screen": code, "region": rdata._region(uf, region), "creative_code": ccode,
                                   "valid_plays": 0, "valid_ms": 0})
            a["valid_plays"] += int(n)
            a["valid_ms"] += int(ms)
        sources.append("playback_events.valid")
    if p.start < oldest_raw:
        agg_end = min(p.end, oldest_raw - timedelta(days=1))
        q = (select(Screen.friendly_id, Establishment.state, Establishment.region, PDS.creative_code,
                    func.coalesce(func.sum(PDS.plays_valid), 0), func.coalesce(func.sum(PDS.valid_ms), 0))
             .select_from(PDS).join(Screen, Screen.id == PDS.screen_id)
             .outerjoin(Establishment, Establishment.id == PDS.establishment_id)
             .where(PDS.campaign_id == campaign_id, PDS.local_day >= p.start, PDS.local_day <= agg_end)
             .group_by(Screen.friendly_id, Establishment.state, Establishment.region, PDS.creative_code))
        for code, uf, region, ccode, n, ms in (await db.execute(q)).all():
            if not n:
                continue
            k = (code, ccode)
            a = acc.setdefault(k, {"screen": code, "region": rdata._region(uf, region), "creative_code": ccode,
                                   "valid_plays": 0, "valid_ms": 0})
            a["valid_plays"] += int(n)
            a["valid_ms"] += int(ms)
        sources.append("playback_daily_stats.plays_valid")
    rows = sorted(acc.values(), key=lambda r: (r["screen"], r["creative_code"]))
    return rows, "+".join(sources)


async def _estimated_audience(db: AsyncSession, screen_codes: list[str], days: int) -> Optional[int]:
    from app.models.screens import Screen

    if not screen_codes:
        return None
    total = (await db.execute(select(func.sum(Screen.estimated_daily_audience)).where(
        Screen.friendly_id.in_(screen_codes), Screen.estimated_daily_audience.is_not(None)))).scalar()
    return int(total) * days if total else None


async def create_proof(db: AsyncSession, ctx: AuthContext, campaign_id: uuid.UUID, start: Optional[date],
                       end: Optional[date]) -> tuple[DeliveryProof, bytes]:
    from app.core.storage import build_key, get_storage
    from app.models.campaigns import Campaign, CampaignCreative, Creative
    from app.models.organizations import Organization
    from app.modules.instance.service import get_brand, sign_payload

    c = await db.get(Campaign, campaign_id)
    if c is None or not (ctx.can("reports.view", org_id=c.org_id) or ctx.can("campaigns.view", org_id=c.org_id)):
        raise NotFound()
    camp_start = getattr(c, "start_date", None)
    camp_end = getattr(c, "end_date", None)
    today = local_date(utcnow(), DEFAULT_TIMEZONE)
    s = start or camp_start or today.replace(day=1)
    e = end or min(camp_end or today, today)
    p = rdata.parse_period(s, e)
    rows, source = await _proof_rows(db, c.id, p)
    total = sum(r["valid_plays"] for r in rows)
    creatives = list((await db.execute(
        select(Creative.code, Creative.name).join(CampaignCreative, CampaignCreative.creative_id == Creative.id)
        .where(CampaignCreative.campaign_id == c.id).order_by(Creative.code))).all())
    org = await db.get(Organization, c.org_id)
    screens = sorted({r["screen"] for r in rows})
    audience = await _estimated_audience(db, screens, p.days)

    async def exists(code: str) -> bool:
        return (await db.execute(select(DeliveryProof.id).where(DeliveryProof.code == code))).first() is not None

    code = await generate_unique(lambda: f"CV-{random_safe(8)}", exists)
    issued_at = utcnow()
    payload = {
        "type": "rd.delivery_proof", "v": 1, "code": code, "issued_at": isoformat_z(issued_at),
        "campaign": {"id": str(c.id), "name": c.name}, "advertiser": {"org_id": str(c.org_id),
                                                                     "name": org.name if org else None},
        "period": {"start": p.start.isoformat(), "end": p.end.isoformat(), "timezone_basis": "dia local da tela"},
        "creatives": [{"code": cc, "name": nm} for cc, nm in creatives],
        "screens": [{"code": r["screen"], "region": r["region"], "creative_code": r["creative_code"],
                     "valid_plays": r["valid_plays"], "valid_ms": r["valid_ms"]} for r in rows],
        "total_valid_plays": total, "source": source,
        "rule": "Somente proof-of-play válido (100% da duração); programação planejada não conta como execução.",
    }
    pbytes = canonical(payload)
    payload_sha = hashlib.sha256(pbytes).hexdigest()
    kid, payload_sig = await sign_payload(db, pbytes)
    pdf = _proof_pdf(payload, payload_sha, kid, payload_sig, await get_brand(db), audience)
    pdf_sha = hashlib.sha256(pdf).hexdigest()
    _kid2, pdf_sig = await sign_payload(db, pdf_sha.encode("ascii"))
    obj = await get_storage().put_stream(build_key("reports", "proofs", c.id, f"{code.lower()}.pdf"), pdf,
                                         "application/pdf")
    proof = DeliveryProof(code=code, campaign_id=c.id, org_id=c.org_id, period_start=p.start, period_end=p.end,
                          source=source, total_valid_plays=total, payload=payload, payload_sha256=payload_sha,
                          payload_signature=payload_sig, kid=kid, pdf_key=obj.key, pdf_sha256=pdf_sha,
                          pdf_signature=pdf_sig, created_by=ctx.user_id)
    db.add(proof)
    await db.flush()
    await audit(db, ctx, "report.delivery_proof_issued", "delivery_proof", proof.id,
                after={"code": code, "campaign_id": c.id, "total_valid_plays": total, "pdf_sha256": pdf_sha},
                org_id=c.org_id)
    return proof, pdf


def _proof_pdf(payload: dict, payload_sha: str, kid: str, sig: str, b, audience: Optional[int]) -> bytes:
    from app.modules.reports.pdf import Heading, KeyValues, PageBreak, Para, PdfDoc, Table, render_pdf

    per = payload["period"]
    fmt = lambda d: date.fromisoformat(d).strftime("%d/%m/%Y")  # noqa: E731
    total = payload["total_valid_plays"]
    blocks: list = [
        KeyValues([
            ("Comprovante", payload["code"]),
            ("Campanha", payload["campaign"]["name"]),
            ("Anunciante", payload["advertiser"]["name"] or "—"),
            ("Período", f"{fmt(per['start'])} a {fmt(per['end'])} (dia local de cada tela)"),
            ("Criativos (IDs)", ", ".join(f"{c['code']} — {c['name']}" for c in payload["creatives"]) or "—"),
            ("Telas com exibição válida", str(len({s['code'] for s in payload['screens']}))),
            ("Exibições válidas (total)", f"{total:,}".replace(",", ".")),
            ("Emitido em", payload["issued_at"].replace("T", " ").replace("Z", " UTC")),
        ]),
        Heading("Exibições válidas por tela e criativo"),
        Table(["Tela (código)", "Região", "Criativo (ID)", "Exibições válidas", "Tempo válido (h)"],
              [[s["code"], s["region"], s["creative_code"], s["valid_plays"],
                f"{s['valid_ms'] / 3_600_000:.2f}".replace(".", ",")] for s in payload["screens"]],
              col_widths=[38, 40, 32, 35, 35], align_right=[3, 4]),
        Para(rdata.EXHIBITION_NOTE, small=True),
        Para("Telas identificadas apenas por código e região (UF); nome e endereço dos estabelecimentos não são "
             "divulgados.", small=True, muted=True),
    ]
    if audience is not None:
        blocks.append(Para(f"Audiência ESTIMADA (não medida): até {audience:,} pessoas no período, pela soma das "
                           "estimativas diárias cadastradas para as telas. Não é derivada das exibições."
                           .replace(",", "."), small=True, muted=True))
    blocks += [
        PageBreak(),
        Heading("Página de verificação"),
        Para("Este comprovante é assinado digitalmente pela instância com ECDSA P-256/SHA-256 (ES256). O conteúdo "
             "acima é derivado do documento canônico abaixo identificado. Para verificar: envie este PDF em "
             "POST /api/v1/reports/proofs/verify, ou consulte GET /api/v1/reports/proofs/{código}/verification e "
             "verifique a assinatura com a chave pública publicada em GET /api/v1/instance/signing-keys.", small=True),
        KeyValues([
            ("Código", payload["code"]),
            ("SHA-256 do conteúdo canônico", payload_sha),
            ("Chave (kid)", kid),
            ("Assinatura (base64url, DER)", sig),
            ("Fonte das contagens", payload["source"]),
        ]),
        Para("Aviso legal: assinatura eletrônica da plataforma, NÃO é assinatura digital qualificada com "
             "certificado ICP-Brasil.", small=True, muted=True),
    ]
    return render_pdf(PdfDoc(title=f"Comprovante de veiculação {payload['code']}", subtitle=payload["campaign"]["name"],
                             blocks=blocks, footer=f"{b.name} · Comprovante {payload['code']}", **b.pdf_kwargs(),
                             keywords=f"comprovante {payload['code']}"))


async def verify_pdf(db: AsyncSession, data: bytes) -> dict:
    from app.modules.instance.service import verify_signature

    sha = hashlib.sha256(data).hexdigest()
    proof = (await db.execute(select(DeliveryProof).where(DeliveryProof.pdf_sha256 == sha))).scalar_one_or_none()
    if proof is None:
        return {"valid": False, "reason": "Documento não reconhecido ou alterado (SHA-256 não confere).",
                "pdf_sha256": sha}
    ok_pdf = await verify_signature(db, proof.kid, sha.encode("ascii"), proof.pdf_signature)
    pbytes = canonical(proof.payload)
    ok_payload = (hashlib.sha256(pbytes).hexdigest() == proof.payload_sha256
                  and await verify_signature(db, proof.kid, pbytes, proof.payload_signature))
    return {"valid": bool(ok_pdf and ok_payload), "code": proof.code, "issued_at": proof.payload.get("issued_at"),
            "period": proof.payload.get("period"), "total_valid_plays": proof.total_valid_plays,
            "kid": proof.kid, "pdf_sha256": sha}


# ─── Relatório mensal automático ─────────────────────────────────────────────

def previous_month(today: date) -> rdata.Period:
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return rdata.Period(last_prev.replace(day=1), last_prev)


async def run_monthly(db: AsyncSession, period: rdata.Period) -> dict:
    """Gera e envia (e-mail com PDF) o relatório mensal a anunciantes e estabelecimentos com
    exibições válidas no período. Idempotente por (tipo, organização, período)."""
    from app.models.campaigns import Campaign
    from app.models.establishments import Establishment
    from app.models.playback import PlaybackDailyStat as PDS
    from app.modules.instance.service import get_brand
    from app.modules.notifications.service import OrgRecipients, notify, resolve_recipients
    from app.models.users import User

    b = await get_brand(db)
    brand = b.name
    adv_orgs = set((await db.execute(
        select(distinct(Campaign.org_id)).select_from(PDS).join(Campaign, Campaign.id == PDS.campaign_id)
        .where(PDS.local_day >= period.start, PDS.local_day <= period.end, PDS.plays_valid > 0))).scalars())
    est_orgs = set((await db.execute(
        select(distinct(Establishment.org_id)).select_from(PDS).join(Establishment, Establishment.id == PDS.establishment_id)
        .where(PDS.local_day >= period.start, PDS.local_day <= period.end, PDS.plays_valid > 0))).scalars())
    stats = {"advertisers": 0, "establishments": 0, "emails": 0, "skipped_existing": 0}
    label = period.start.strftime("%m/%Y")
    for kind, orgs, audience, groups in (
        ("MENSAL_ANUNCIANTE", adv_orgs, rdata.ADVERTISER, ("campanha", "anuncio", "regiao")),
        ("MENSAL_ESTABELECIMENTO", est_orgs, rdata.ESTABLISHMENT, ("tela", "tipo", "dia")),
    ):
        for org_id in sorted(orgs, key=str):
            exists = (await db.execute(select(ReportRun.id).where(
                ReportRun.kind == kind, ReportRun.org_id == org_id, ReportRun.period_start == period.start))).first()
            if exists:
                stats["skipped_existing"] += 1
                continue
            scope = rdata.Scope(audience, org_id=org_id, label="Anunciante" if audience == rdata.ADVERTISER else "Estabelecimento")
            pdf = await _monthly_pdf(db, scope, period, groups, b, label)
            user_ids = await resolve_recipients(db, [OrgRecipients(org_id, "reports.view")])
            users = list((await db.execute(select(User).where(User.id.in_(user_ids)))).scalars()) if user_ids else []
            sent = 0
            if users:
                sent = await email_file(db, users, subject=f"{brand} — Relatório mensal {label}",
                                        text=f"Segue o relatório mensal de {label} com as exibições VÁLIDAS (proof-of-play).\n\n{brand}",
                                        filename=f"relatorio-mensal-{period.start.strftime('%Y-%m')}.pdf",
                                        content_type="application/pdf", data=pdf)
                await notify(db, user_ids, "report.monthly", {"period": label},
                             dedupe_key=f"report.monthly:{kind}:{org_id}:{period.start.isoformat()}", org_id=org_id)
            db.add(ReportRun(kind=kind, org_id=org_id, period_start=period.start, period_end=period.end,
                             status="ENVIADO" if sent else "SEM_DESTINATARIOS", recipients=sent,
                             pdf_sha256=hashlib.sha256(pdf).hexdigest()))
            await db.flush()
            stats["emails"] += sent
            stats["advertisers" if audience == rdata.ADVERTISER else "establishments"] += 1
    return stats


async def _monthly_pdf(db: AsyncSession, scope: rdata.Scope, period: rdata.Period, groups: tuple, b,
                       label: str) -> bytes:
    from app.modules.reports.exporters import _display
    from app.modules.reports.pdf import Heading, KeyValues, Para, PdfDoc, Table, render_pdf

    blocks: list = [KeyValues([("Período", f"{period.start.strftime('%d/%m/%Y')} a {period.end.strftime('%d/%m/%Y')}"),
                               ("Destinatário", scope.label)])]
    for g in groups:
        ds = await rdata.exhibitions(db, scope, period, g)
        blocks.append(Heading(ds.title))
        num = [i for i, c in enumerate(ds.columns) if c.kind in ("int", "money", "bps")]
        blocks.append(Table([c.label for c in ds.columns], [[_display(c, r.get(c.key)) for c in ds.columns] for r in ds.rows],
                            align_right=num))
    blocks.append(Para(rdata.EXHIBITION_NOTE, small=True, muted=True))
    return render_pdf(PdfDoc(title=f"Relatório mensal — {label}", subtitle=scope.label, blocks=blocks,
                             footer=f"{b.name} · Relatório mensal {label}", **b.pdf_kwargs()))
