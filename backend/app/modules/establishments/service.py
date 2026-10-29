"""
RD Mídia Indoor — Serviço de estabelecimentos (W-ORG; ETAPA2 #74–#100, #127–#158).

- Timezone IANA obrigatório e validado; se omitido, SUGERIDO por UF/cidade
  (``timezone_source = AUTO``); informado pelo admin ⇒ ``MANUAL``. Mudança auditada.
- Coordenadas manuais ou por GPS (com precisão), sempre auditadas.
- Segmentos/produtos vendidos (múltiplos) — usados pelo motor de conflitos (W-CAMP).
- Escopo: ORGANIZATION (dona), ESTABLISHMENT, CITY (``city_key``) → 404 fora do escopo.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.errors import DomainError, Forbidden, NotFound
from app.core.timeutils import utcnow, validate_timezone
from app.models.contracts import PartnerPlan
from app.models.establishments import (
    AudienceMethod,
    Establishment,
    GeoSource,
    OnboardingStatus,
    TimezoneSource,
    VenueClass,
)
from app.models.organizations import BusinessRole, Organization
from app.models.screens import AudienceEstimate
from app.models.taxonomy import EstablishmentProduct, EstablishmentSegment, Product
from app.modules.establishments.geo import (
    REGION_LABELS,
    derive_keys,
    normalize_postal_code,
    normalize_uf,
    suggest_timezone,
)
from app.modules.organizations.documents import DocumentError, normalize_document


def est_scope(est: Establishment) -> dict:
    return {"org_id": est.org_id, "establishment_id": est.id, "city": est.city_key}


def ensure_est(ctx: AuthContext, perm: str, est: Establishment) -> None:
    ctx.ensure(perm, org_id=est.org_id, establishment_id=est.id, city=est.city_key)


def est_filter(ctx: AuthContext, perm: str):
    return ctx.scope_filter(perm, org_col=Establishment.org_id, establishment_col=Establishment.id,
                            city_col=Establishment.city_key)


async def get_est(db: AsyncSession, est_id: uuid.UUID, *, lock: bool = False) -> Establishment:
    q = select(Establishment).where(Establishment.id == est_id, Establishment.deleted_at.is_(None))
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    est = (await db.execute(q)).scalar_one_or_none()
    if est is None:
        raise NotFound()
    return est


def _tz(value: Optional[str]) -> str:
    try:
        return validate_timezone(value)  # type: ignore[arg-type]
    except ValueError:
        raise DomainError("TIMEZONE_INVALID", "Fuso horário IANA inválido (ex.: America/Sao_Paulo).", status=400,
                          details={"timezone": value}) from None


def _doc(raw: Optional[str], dtype: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not raw:
        return None, None
    try:
        return normalize_document(raw, dtype)
    except DocumentError as e:
        raise DomainError("DOCUMENT_INVALID", str(e), status=400) from None


def _geo(lat: Optional[float], lng: Optional[float], source: Optional[str]) -> tuple:
    if (lat is None) != (lng is None):
        raise DomainError("COORDINATES_INCOMPLETE", "Informe latitude e longitude.", status=400)
    if lat is None:
        return None, None, None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise DomainError("COORDINATES_INVALID", "Coordenadas fora do intervalo válido.", status=400)
    src = (source or GeoSource.MANUAL.value).upper()
    if src not in (GeoSource.MANUAL.value, GeoSource.GPS.value):
        raise DomainError("GEO_SOURCE_INVALID", "Origem da coordenada deve ser MANUAL ou GPS.", status=400)
    return float(lat), float(lng), src


async def _check_plan(db: AsyncSession, plan_id: Optional[uuid.UUID]) -> Optional[uuid.UUID]:
    if plan_id is None:
        return None
    p = await db.get(PartnerPlan, plan_id)
    if p is None or not p.is_active:
        raise DomainError("PARTNER_PLAN_INVALID", "Plano do parceiro inexistente ou inativo.", status=400)
    return p.id


async def create_establishment(db: AsyncSession, ctx: AuthContext, data: dict, *,
                               onboarding_status: str = OnboardingStatus.AWAITING_CONTRACT.value) -> Establishment:
    from app.modules.taxonomy.service import validate_category_id

    org = await db.get(Organization, data["org_id"])
    if org is None:
        raise NotFound()
    try:
        uf = normalize_uf(data.get("state"))
        keys = derive_keys(uf, data.get("city"), data.get("neighborhood"))
        postal = normalize_postal_code(data.get("postal_code"))
    except ValueError as e:
        raise DomainError("ADDRESS_INVALID", str(e), status=400) from None
    # escopo: o ator precisa poder gerenciar estabelecimentos dessa org OU dessa cidade
    if not ctx.can("establishments.manage", org_id=org.id, city=keys["city_key"]):
        raise NotFound()
    if not org.is_active:
        raise DomainError("ORG_INACTIVE", "Organização inativa.", status=400)
    if data.get("timezone"):
        tz, tz_src = _tz(data["timezone"]), TimezoneSource.MANUAL.value
    else:
        tz, tz_src = suggest_timezone(uf, data.get("city"))["timezone"], TimezoneSource.AUTO.value
    doc, dtype = _doc(data.get("document"), data.get("document_type"))
    lat, lng, gsrc = _geo(data.get("lat"), data.get("lng"), data.get("geo_source"))
    venue = (data.get("venue_class") or VenueClass.STANDARD.value).upper()
    if venue not in (VenueClass.STANDARD.value, VenueClass.PREMIUM.value):
        raise DomainError("VENUE_CLASS_INVALID", "Classe do local inválida.", status=400)
    est = Establishment(
        id=uuid.uuid4(),
        org_id=org.id,
        name=data["name"].strip(),
        trade_name=data.get("trade_name"),
        legal_name=data.get("legal_name"),
        document=doc,
        document_type=dtype,
        responsible_name=data.get("responsible_name"),
        phone=data.get("phone"),
        email=(str(data["email"]).lower() if data.get("email") else None),
        address=data.get("address"),
        number=data.get("number"),
        complement=data.get("complement"),
        neighborhood=data.get("neighborhood"),
        city=data["city"].strip(),
        state=uf,
        city_ibge_code=data.get("city_ibge_code"),
        country="BRA",
        postal_code=postal,
        lat=lat,
        lng=lng,
        geo_source=gsrc,
        geo_accuracy_m=data.get("geo_accuracy_m") if lat is not None else None,
        geo_captured_at=utcnow() if lat is not None else None,
        timezone=tz,
        timezone_source=tz_src,
        category_id=await validate_category_id(db, data.get("category_id")),
        venue_class=venue,
        weight_milli=int(data.get("weight_milli") or 1000),
        partner_plan_id=await _check_plan(db, data.get("partner_plan_id")),
        receives_commission=bool(data.get("receives_commission", True)),
        pays_monthly_fee=bool(data.get("pays_monthly_fee", False)),
        monthly_fee_cents=data.get("monthly_fee_cents"),
        onboarding_status=onboarding_status,
        responsible_technician_id=data.get("responsible_technician_id"),
        installation_date=data.get("installation_date"),
        operational_config=data.get("operational_config"),
        notes=data.get("notes"),
        is_active=True,
        created_by=ctx.user_id,
    )
    if est.pays_monthly_fee and not est.monthly_fee_cents:
        raise DomainError("MONTHLY_FEE_REQUIRED", "Informe o valor da mensalidade (centavos).", status=400)
    db.add(est)
    await db.flush()
    if not org.has_role(BusinessRole.PARTNER):
        before_roles = list(org.business_roles or [])
        org.business_roles = before_roles + [BusinessRole.PARTNER.value]
        await audit(db, ctx, "organization.role_added", "organization", org.id, before={"business_roles": before_roles},
                    after={"business_roles": org.business_roles}, reason="Estabelecimento cadastrado", org_id=org.id)
    if data.get("segment_ids") is not None:
        await set_segments(db, ctx, est, data["segment_ids"], audit_it=False)
    await audit(db, ctx, "establishment.created", "establishment", est.id, after=establishment_out(est),
                org_id=org.id)
    return est


EDITABLE = (
    "name", "trade_name", "legal_name", "responsible_name", "phone", "email", "address", "number", "complement",
    "notes", "operational_config", "receives_commission", "pays_monthly_fee", "monthly_fee_cents",
    "responsible_technician_id", "installation_date", "city_ibge_code", "weight_milli",
)


async def update_establishment(db: AsyncSession, ctx: AuthContext, est: Establishment, data: dict) -> dict:
    from app.modules.taxonomy.service import validate_category_id

    before_all = establishment_out(est)
    for f in EDITABLE:
        if f in data:
            v = data[f]
            if f == "email" and v:
                v = str(v).lower()
            setattr(est, f, v)
    if "document" in data:
        est.document, est.document_type = _doc(data.get("document"), data.get("document_type"))
    if "venue_class" in data and data["venue_class"] is not None:
        v = str(data["venue_class"]).upper()
        if v not in (VenueClass.STANDARD.value, VenueClass.PREMIUM.value):
            raise DomainError("VENUE_CLASS_INVALID", "Classe do local inválida.", status=400)
        est.venue_class = v
    if "category_id" in data:
        est.category_id = await validate_category_id(db, data["category_id"])
    if "partner_plan_id" in data:
        est.partner_plan_id = await _check_plan(db, data["partner_plan_id"])
    addr_changed = False
    if any(k in data for k in ("state", "city", "neighborhood", "postal_code")):
        try:
            uf = normalize_uf(data.get("state", est.state))
            city = data.get("city", est.city)
            keys = derive_keys(uf, city, data.get("neighborhood", est.neighborhood))
            postal = normalize_postal_code(data["postal_code"]) if "postal_code" in data else est.postal_code
        except ValueError as e:
            raise DomainError("ADDRESS_INVALID", str(e), status=400) from None
        if keys["city_key"] != est.city_key and not ctx.can("establishments.manage", org_id=est.org_id,
                                                            city=keys["city_key"]):
            raise Forbidden("ESTABLISHMENT_OUT_OF_SCOPE", "Nova cidade fora da sua área de atuação.")
        addr_changed = keys["city_key"] != est.city_key
        est.state, est.city = uf, city.strip()
        est.neighborhood = data.get("neighborhood", est.neighborhood)
        est.postal_code = postal
    if "timezone" in data and data["timezone"] is not None:
        est.timezone = _tz(data["timezone"])
        est.timezone_source = TimezoneSource.MANUAL.value
    elif addr_changed and est.timezone_source == TimezoneSource.AUTO.value:
        est.timezone = suggest_timezone(est.state, est.city)["timezone"]
    if "lat" in data or "lng" in data:
        lat, lng, src = _geo(data.get("lat"), data.get("lng"), data.get("geo_source"))
        est.lat, est.lng, est.geo_source = lat, lng, src
        est.geo_accuracy_m = data.get("geo_accuracy_m") if lat is not None else None
        est.geo_captured_at = utcnow() if lat is not None else None
    if est.pays_monthly_fee and not est.monthly_fee_cents:
        raise DomainError("MONTHLY_FEE_REQUIRED", "Informe o valor da mensalidade (centavos).", status=400)
    await db.flush()
    await db.refresh(est)
    after_all = establishment_out(est)
    changes = {k: (before_all.get(k), after_all[k]) for k in after_all if before_all.get(k) != after_all[k]
               and k not in ("updated_at",)}
    if changes:
        reason = data.get("reason")
        await audit(db, ctx, "establishment.updated", "establishment", est.id,
                    before={k: v[0] for k, v in changes.items()}, after={k: v[1] for k, v in changes.items()},
                    reason=reason, org_id=est.org_id)
    return changes


async def set_segments(db: AsyncSession, ctx: AuthContext, est: Establishment, segment_ids: list,
                       *, audit_it: bool = True) -> list[uuid.UUID]:
    from app.modules.taxonomy.service import validate_segment_ids

    ids = await validate_segment_ids(db, segment_ids)
    before = sorted(str(s) for s in (await db.execute(
        select(EstablishmentSegment.segment_id).where(EstablishmentSegment.establishment_id == est.id))).scalars())
    await db.execute(delete(EstablishmentSegment).where(EstablishmentSegment.establishment_id == est.id))
    for sid in ids:
        db.add(EstablishmentSegment(establishment_id=est.id, segment_id=sid))
    await db.flush()
    if audit_it:
        await audit(db, ctx, "establishment.segments_set", "establishment", est.id, before={"segment_ids": before},
                    after={"segment_ids": sorted(str(s) for s in ids)}, org_id=est.org_id)
    return ids


async def set_products(db: AsyncSession, ctx: AuthContext, est: Establishment, product_ids: list) -> list:
    ids = list(dict.fromkeys(uuid.UUID(str(p)) for p in product_ids or []))
    if ids:
        found = set((await db.execute(select(Product.id).where(Product.id.in_(ids), Product.is_active.is_(True))))
                    .scalars())
        missing = [str(i) for i in ids if i not in found]
        if missing:
            raise DomainError("PRODUCT_NOT_FOUND", "Produto inexistente ou inativo.", status=400,
                              details={"ids": missing})
    before = sorted(str(s) for s in (await db.execute(
        select(EstablishmentProduct.product_id).where(EstablishmentProduct.establishment_id == est.id))).scalars())
    await db.execute(delete(EstablishmentProduct).where(EstablishmentProduct.establishment_id == est.id))
    for pid in ids:
        db.add(EstablishmentProduct(establishment_id=est.id, product_id=pid))
    await db.flush()
    await audit(db, ctx, "establishment.products_set", "establishment", est.id, before={"product_ids": before},
                after={"product_ids": sorted(str(i) for i in ids)}, org_id=est.org_id)
    return ids


async def segment_ids_of(db: AsyncSession, est_id: uuid.UUID) -> list[str]:
    return sorted(str(s) for s in (await db.execute(
        select(EstablishmentSegment.segment_id).where(EstablishmentSegment.establishment_id == est_id))).scalars())


async def product_ids_of(db: AsyncSession, est_id: uuid.UUID) -> list[str]:
    return sorted(str(s) for s in (await db.execute(
        select(EstablishmentProduct.product_id).where(EstablishmentProduct.establishment_id == est_id))).scalars())


async def record_audience(
    db: AsyncSession,
    ctx: AuthContext,
    est: Establishment,
    *,
    method: str,
    daily_people: Optional[int],
    daily_visitors: Optional[int],
    visibility_bps: Optional[int],
    note: Optional[str],
    screen: Any = None,
) -> AudienceEstimate:
    """Audiência ESTIMADA (manual ou por fluxo). Nunca derivada de exibições (P002 #28)."""
    method = (method or "").upper()
    if method == AudienceMethod.MANUAL.value:
        if daily_people is None or daily_people < 0:
            raise DomainError("AUDIENCE_VALUE_REQUIRED", "Informe a estimativa de pessoas/dia.", status=400)
        value, basis = int(daily_people), {"source": (note or "informado manualmente")[:200]}
    elif method == AudienceMethod.FLOW_ESTIMATE.value:
        if daily_visitors is None or daily_visitors < 0 or visibility_bps is None or not (0 < visibility_bps <= 10000):
            raise DomainError("AUDIENCE_FLOW_REQUIRED",
                              "Informe o fluxo diário estimado e o fator de visibilidade (bps, 1–10000).", status=400)
        value = int(daily_visitors) * int(visibility_bps) // 10000
        basis = {"daily_visitors": int(daily_visitors), "visibility_bps": int(visibility_bps),
                 "formula": "pessoas_dia = fluxo_diario × visibilidade_bps ÷ 10000 (arredondado para baixo)"}
    else:
        raise DomainError("AUDIENCE_METHOD_INVALID", "Método deve ser MANUAL ou FLOW_ESTIMATE.", status=400)
    ae = AudienceEstimate(
        id=uuid.uuid4(), subject_type="SCREEN" if screen is not None else "ESTABLISHMENT",
        screen_id=screen.id if screen is not None else None, establishment_id=est.id, daily_people=value,
        method=method, basis=basis, note=note, created_by=ctx.user_id,
    )
    db.add(ae)
    now = utcnow()
    target = screen if screen is not None else est
    before = {"value": getattr(target, "estimated_daily_audience", None) if screen is not None
              else est.estimated_daily_visitors, "method": target.audience_method}
    if screen is not None:
        screen.estimated_daily_audience = value
        screen.audience_method = method
        screen.audience_updated_at = now
        from app.models.screens import ScreenHistoryEvent
        from app.modules.screens.service import add_history

        await add_history(db, screen, ScreenHistoryEvent.AUDIENCE_UPDATED, actor_user_id=ctx.user_id, before=before,
                          after={"value": value, "method": method}, reason=note)
    else:
        est.estimated_daily_visitors = value
        est.audience_method = method
        est.audience_updated_at = now
    await db.flush()
    await audit(db, ctx, "audience_estimate.recorded", "screen" if screen is not None else "establishment",
                target.id, before=before, after={"value": value, "method": method, "basis": basis},
                reason=note, org_id=est.org_id)
    return ae


def establishment_out(est: Establishment, *, segment_ids: Optional[list] = None,
                      product_ids: Optional[list] = None) -> dict:
    from app.modules.organizations.common import iso
    from app.modules.screens.service import audience_label

    d = {
        "id": str(est.id),
        "org_id": str(est.org_id),
        "name": est.name,
        "trade_name": est.trade_name,
        "legal_name": est.legal_name,
        "document": est.document,
        "document_type": est.document_type,
        "responsible_name": est.responsible_name,
        "phone": est.phone,
        "email": est.email,
        "address": est.address,
        "number": est.number,
        "complement": est.complement,
        "neighborhood": est.neighborhood,
        "city": est.city,
        "city_key": est.city_key,
        "city_ibge_code": est.city_ibge_code,
        "state": est.state,
        "region": est.region,
        "region_label": REGION_LABELS.get(est.region),
        "country": est.country,
        "postal_code": est.postal_code,
        "lat": est.lat,
        "lng": est.lng,
        "geo_source": est.geo_source,
        "geo_accuracy_m": est.geo_accuracy_m,
        "geo_captured_at": iso(est.geo_captured_at),
        "timezone": est.timezone,
        "timezone_source": est.timezone_source,
        "category_id": str(est.category_id) if est.category_id else None,
        "venue_class": est.venue_class,
        "weight_milli": est.weight_milli,
        "estimated_daily_visitors": audience_label(est.estimated_daily_visitors, est.audience_method),
        "partner_plan_id": str(est.partner_plan_id) if est.partner_plan_id else None,
        "receives_commission": est.receives_commission,
        "pays_monthly_fee": est.pays_monthly_fee,
        "monthly_fee_cents": est.monthly_fee_cents,
        "onboarding_status": est.onboarding_status,
        "responsible_technician_id": str(est.responsible_technician_id) if est.responsible_technician_id else None,
        "installation_date": iso(est.installation_date),
        "is_active": est.is_active,
        "deactivated_at": iso(est.deactivated_at),
        "deactivation_reason": est.deactivation_reason,
        "operational_config": est.operational_config,
        "notes": est.notes,
        "created_at": iso(est.created_at),
        "updated_at": iso(est.updated_at),
    }
    if segment_ids is not None:
        d["segment_ids"] = segment_ids
    if product_ids is not None:
        d["product_ids"] = product_ids
    return d
