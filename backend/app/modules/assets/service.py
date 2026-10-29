"""
RD Mídia Indoor — Serviço de estoque/patrimônio (W-ORG; ETAPA2 #204–#210, #1146–#1151).

Ledger de estoque: cada movimentação é ``from (kind, ref, city) → to (kind, ref, city)``
com quantidade > 0, gravada em ``equipment_movements`` (append-only por trigger).

- Saldo DERIVADO: ``balances(db, item_id)`` = Σ entradas − Σ saídas por local.
- Itens SERIAL: o local atual é o único com saldo 1; ``equipment_items.status`` e
  ``location_*`` são projeção atualizada sob ``SELECT … FOR UPDATE`` do item, na mesma
  transação da movimentação — duas saídas simultâneas do mesmo item: a segunda espera o
  lock, encontra o item fora do estoque e falha (409).
- Itens BULK: saída exige saldo suficiente no local de origem (calculado sob o lock).
- Transições válidas em ``ALLOWED`` (tipo de movimentação × origem × destino).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from sqlalchemy import case, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.core.timeutils import utcnow
from app.models.contracts import ComodatoAgreement
from app.models.equipment import (
    EquipmentItem,
    EquipmentMovement,
    EquipmentStatus,
    EquipmentType,
    LocationKind as LK,
    MovementType as MT,
    TrackingMode,
)
from app.models.establishments import Establishment
from app.models.screens import Screen

_K = LK
ALLOWED: dict[str, tuple[set[str], set[str]]] = {
    MT.ENTRY.value: ({_K.EXTERNAL.value}, {_K.WAREHOUSE.value}),
    MT.TRANSFER.value: ({_K.WAREHOUSE.value, _K.TECHNICIAN.value}, {_K.WAREHOUSE.value, _K.TECHNICIAN.value}),
    MT.INSTALL.value: ({_K.WAREHOUSE.value, _K.TECHNICIAN.value}, {_K.SCREEN.value}),
    MT.LOAN.value: ({_K.WAREHOUSE.value, _K.TECHNICIAN.value}, {_K.ESTABLISHMENT.value}),
    MT.COMODATO.value: ({_K.WAREHOUSE.value, _K.TECHNICIAN.value}, {_K.ESTABLISHMENT.value, _K.SCREEN.value}),
    MT.RETURN.value: ({_K.ESTABLISHMENT.value, _K.SCREEN.value, _K.TECHNICIAN.value}, {_K.WAREHOUSE.value}),
    MT.MAINTENANCE_IN.value: (
        {_K.WAREHOUSE.value, _K.TECHNICIAN.value, _K.ESTABLISHMENT.value, _K.SCREEN.value},
        {_K.MAINTENANCE.value},
    ),
    MT.MAINTENANCE_OUT.value: ({_K.MAINTENANCE.value}, {_K.WAREHOUSE.value, _K.SCREEN.value}),
    MT.RMA_OUT.value: ({_K.WAREHOUSE.value, _K.MAINTENANCE.value}, {_K.RMA.value}),
    MT.RMA_RETURN.value: ({_K.RMA.value}, {_K.WAREHOUSE.value}),
    MT.WRITE_OFF.value: (
        {_K.WAREHOUSE.value, _K.TECHNICIAN.value, _K.ESTABLISHMENT.value, _K.SCREEN.value, _K.MAINTENANCE.value,
         _K.RMA.value},
        {_K.DISPOSED.value},
    ),
    MT.ADJUSTMENT.value: ({_K.EXTERNAL.value, _K.WAREHOUSE.value}, {_K.WAREHOUSE.value, _K.DISPOSED.value}),
}
BULK_ONLY = {MT.ADJUSTMENT.value}


@dataclass
class Location:
    kind: str
    ref_id: Optional[uuid.UUID] = None
    city_key: Optional[str] = None
    # preenchidos por ``resolve``
    establishment_id: Optional[uuid.UUID] = None
    screen_id: Optional[uuid.UUID] = None
    org_id: Optional[uuid.UUID] = None
    user_id: Optional[uuid.UUID] = None

    def key(self) -> tuple:
        return (self.kind, self.ref_id, self.city_key)

    def out(self) -> dict:
        return {
            "kind": self.kind,
            "ref_id": str(self.ref_id) if self.ref_id else None,
            "city_key": self.city_key,
        }


async def resolve(db: AsyncSession, loc: Location) -> Location:
    """Valida o local e preenche estabelecimento/organização/cidade."""
    k = loc.kind
    if k not in {x.value for x in LK}:
        raise DomainError("LOCATION_INVALID", "Local inválido.", status=400)
    if k == LK.ESTABLISHMENT.value:
        est = await db.get(Establishment, loc.ref_id) if loc.ref_id else None
        if est is None:
            raise NotFound()
        loc.establishment_id, loc.org_id, loc.city_key = est.id, est.org_id, est.city_key
    elif k == LK.SCREEN.value:
        scr = await db.get(Screen, loc.ref_id) if loc.ref_id else None
        if scr is None:
            raise NotFound()
        est = await db.get(Establishment, scr.establishment_id)
        loc.screen_id, loc.establishment_id, loc.org_id, loc.city_key = scr.id, est.id, est.org_id, est.city_key
    elif k == LK.TECHNICIAN.value:
        from app.models.users import User

        u = await db.get(User, loc.ref_id) if loc.ref_id else None
        if u is None or not u.is_active:
            raise DomainError("TECHNICIAN_NOT_FOUND", "Técnico inexistente ou inativo.", status=400)
        loc.user_id = u.id
    elif k in (LK.WAREHOUSE.value, LK.MAINTENANCE.value):
        if loc.ref_id is not None:
            raise DomainError("LOCATION_INVALID", "Estoque/manutenção não usam referência.", status=400)
        if loc.city_key is not None:
            from app.core.authz import normalize_city_key

            loc.city_key = normalize_city_key(loc.city_key)
    else:  # EXTERNAL, RMA, DISPOSED
        loc.ref_id = None
    return loc


def _status_after(movement_type: str, to: Location) -> str:
    k = to.kind
    if k == LK.WAREHOUSE.value:
        return EquipmentStatus.IN_STOCK.value
    if k == LK.TECHNICIAN.value:
        return EquipmentStatus.WITH_TECHNICIAN.value
    if k == LK.SCREEN.value:
        return EquipmentStatus.INSTALLED.value
    if k == LK.ESTABLISHMENT.value:
        return EquipmentStatus.COMODATO.value if movement_type == MT.COMODATO.value else EquipmentStatus.LOANED.value
    if k == LK.MAINTENANCE.value:
        return EquipmentStatus.IN_MAINTENANCE.value
    if k == LK.RMA.value:
        return EquipmentStatus.IN_RMA.value
    if k == LK.DISPOSED.value:
        return EquipmentStatus.WRITTEN_OFF.value
    return EquipmentStatus.IN_STOCK.value


def current_location(item: EquipmentItem) -> Location:
    ref = None
    k = item.location_kind or LK.EXTERNAL.value
    if k == LK.SCREEN.value:
        ref = item.location_screen_id
    elif k == LK.ESTABLISHMENT.value:
        ref = item.location_establishment_id
    elif k == LK.TECHNICIAN.value:
        ref = item.location_user_id
    city = item.location_city_key if k in (LK.WAREHOUSE.value, LK.MAINTENANCE.value) else None
    return Location(kind=k, ref_id=ref, city_key=city, establishment_id=item.location_establishment_id,
                    screen_id=item.location_screen_id, org_id=item.scope_org_id, user_id=item.location_user_id)


async def lock_item(db: AsyncSession, item_id: uuid.UUID) -> EquipmentItem:
    it = (await db.execute(select(EquipmentItem).where(EquipmentItem.id == item_id).with_for_update()
                       .execution_options(populate_existing=True))).scalar_one_or_none()
    if it is None:
        raise NotFound()
    return it


def _ledger_key(kind: str, ref: Optional[uuid.UUID], city: Optional[str]) -> tuple:
    # o ledger guarda city_key só para WAREHOUSE/MAINTENANCE (estoques por cidade)
    return (kind, ref, city if kind in (LK.WAREHOUSE.value, LK.MAINTENANCE.value) else None)


async def balances(db: AsyncSession, item_id: uuid.UUID) -> list[dict]:
    """Saldo derivado por local (Σ entradas − Σ saídas). Exclui EXTERNAL/DISPOSED zerados."""
    M = EquipmentMovement
    ins = select(M.to_kind.label("kind"), M.to_ref_id.label("ref"),
                 case((M.to_kind.in_([LK.WAREHOUSE.value, LK.MAINTENANCE.value]), M.to_city_key), else_=None).label("city"),
                 M.quantity.label("q")).where(M.item_id == item_id)
    outs = select(M.from_kind, M.from_ref_id,
                  case((M.from_kind.in_([LK.WAREHOUSE.value, LK.MAINTENANCE.value]), M.from_city_key), else_=None),
                  (-M.quantity)).where(M.item_id == item_id)
    u = union_all(ins, outs).subquery()
    rows = (
        await db.execute(
            select(u.c.kind, u.c.ref, u.c.city, func.sum(u.c.q)).group_by(u.c.kind, u.c.ref, u.c.city)
        )
    ).all()
    out = []
    for kind, ref, city, q in rows:
        q = int(q or 0)
        if kind == LK.EXTERNAL.value or q == 0:
            continue
        out.append({"kind": kind, "ref_id": str(ref) if ref else None, "city_key": city, "quantity": q})
    out.sort(key=lambda r: (r["kind"], r["ref_id"] or "", r["city_key"] or ""))
    return out


async def balance_at(db: AsyncSession, item_id: uuid.UUID, loc: Location) -> int:
    kind, ref, city = _ledger_key(loc.kind, loc.ref_id, loc.city_key)
    M = EquipmentMovement

    def _match(kcol, rcol, ccol):
        conds = [kcol == kind]
        conds.append(rcol.is_(None) if ref is None else rcol == ref)
        if kind in (LK.WAREHOUSE.value, LK.MAINTENANCE.value):
            conds.append(ccol.is_(None) if city is None else ccol == city)
        return conds

    inq = (await db.execute(select(func.coalesce(func.sum(M.quantity), 0)).where(
        M.item_id == item_id, *_match(M.to_kind, M.to_ref_id, M.to_city_key)))).scalar_one()
    outq = (await db.execute(select(func.coalesce(func.sum(M.quantity), 0)).where(
        M.item_id == item_id, *_match(M.from_kind, M.from_ref_id, M.from_city_key)))).scalar_one()
    return int(inq) - int(outq)


async def derive_serial_location(db: AsyncSession, item_id: uuid.UUID) -> Optional[dict]:
    """Local atual de um item SERIAL derivado SOMENTE do ledger (para conferência)."""
    pos = [b for b in await balances(db, item_id) if b["quantity"] != 0]
    if not pos:
        return None
    if len(pos) != 1 or pos[0]["quantity"] != 1:
        raise RuntimeError(f"ledger inconsistente para item serial {item_id}: {pos}")
    return pos[0]


async def _check_agreement(db: AsyncSession, agreement_id: Optional[uuid.UUID], to: Location) -> Optional[ComodatoAgreement]:
    if agreement_id is None:
        raise DomainError("COMODATO_AGREEMENT_REQUIRED", "Comodato exige acordo de comodato ativo.", status=400)
    ag = await db.get(ComodatoAgreement, agreement_id)
    if ag is None or ag.status != "ACTIVE":
        raise DomainError("COMODATO_AGREEMENT_INVALID", "Acordo de comodato inexistente ou encerrado.", status=400)
    if to.establishment_id != ag.establishment_id:
        raise DomainError("COMODATO_AGREEMENT_MISMATCH", "O acordo de comodato é de outro estabelecimento.", status=400)
    return ag


async def move(
    db: AsyncSession,
    ctx: Any,
    item: EquipmentItem,
    movement_type: str,
    to: Location,
    *,
    source: Optional[Location] = None,
    quantity: int = 1,
    reason: Optional[str] = None,
    reference: Optional[str] = None,
    comodato_agreement_id: Optional[uuid.UUID] = None,
    expected_return_at: Optional[date] = None,
    group_id: Optional[uuid.UUID] = None,
    replaced_item_id: Optional[uuid.UUID] = None,
    audit_it: bool = True,
) -> EquipmentMovement:
    """Registra UMA movimentação. ``item`` DEVE ter sido obtido com ``lock_item``."""
    if movement_type not in ALLOWED:
        raise DomainError("MOVEMENT_TYPE_INVALID", "Tipo de movimentação inválido.", status=400)
    serial = item.tracking == TrackingMode.SERIAL.value
    if movement_type in BULK_ONLY and serial:
        raise DomainError("MOVEMENT_TYPE_INVALID", "Ajuste de inventário só para itens de quantidade.", status=400)
    if quantity is None or int(quantity) <= 0:
        raise DomainError("QUANTITY_INVALID", "Quantidade deve ser positiva.", status=400)
    quantity = int(quantity)
    if serial and quantity != 1:
        raise DomainError("QUANTITY_INVALID", "Item com número de série movimenta 1 unidade.", status=400)

    if serial:
        if item.status == EquipmentStatus.WRITTEN_OFF.value:
            raise Conflict("EQUIPMENT_WRITTEN_OFF", "Item baixado não pode ser movimentado.")
        src = current_location(item)
        if source is not None and source.key() != src.key():
            raise Conflict("EQUIPMENT_NOT_AT_SOURCE", "O item não está no local de origem informado.",
                           details={"current": src.out()})
    else:
        if source is None:
            raise DomainError("SOURCE_REQUIRED", "Informe o local de origem.", status=400)
        src = await resolve(db, source)

    to = await resolve(db, to)
    allowed_from, allowed_to = ALLOWED[movement_type]
    if src.kind not in allowed_from or to.kind not in allowed_to:
        raise Conflict(
            "EQUIPMENT_INVALID_MOVEMENT",
            "Movimentação não permitida a partir da situação atual do item.",
            details={"movement_type": movement_type, "from": src.kind, "to": to.kind, "status": item.status},
        )
    if src.key() == to.key():
        raise DomainError("EQUIPMENT_SAME_LOCATION", "Origem e destino iguais.", status=400)
    if movement_type in (MT.WRITE_OFF.value, MT.ADJUSTMENT.value, MT.RMA_OUT.value) and not (reason or "").strip():
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória para esta movimentação.", status=400)
    if movement_type == MT.RMA_OUT.value and not (reference or "").strip():
        raise DomainError("RMA_REFERENCE_REQUIRED", "Informe o número/protocolo do RMA.", status=400)
    if movement_type == MT.LOAN.value and expected_return_at is None:
        raise DomainError("RETURN_DATE_REQUIRED", "Empréstimo exige data prevista de devolução.", status=400)
    agreement = None
    if movement_type == MT.COMODATO.value:
        agreement = await _check_agreement(db, comodato_agreement_id, to)

    if src.kind != LK.EXTERNAL.value:
        available = await balance_at(db, item.id, src)
        if available < quantity:
            raise Conflict("EQUIPMENT_INSUFFICIENT_BALANCE", "Saldo insuficiente no local de origem.",
                           details={"available": available, "requested": quantity})

    from_status = item.status if serial else None
    to_status = _status_after(movement_type, to) if serial else None
    fk, fr, fc = _ledger_key(src.kind, src.ref_id, src.city_key)
    tk, tr, tc = _ledger_key(to.kind, to.ref_id, to.city_key)
    mv = EquipmentMovement(
        id=uuid.uuid4(),
        item_id=item.id,
        group_id=group_id,
        movement_type=movement_type,
        quantity=quantity,
        from_kind=fk, from_ref_id=fr, from_city_key=fc,
        to_kind=tk, to_ref_id=tr, to_city_key=tc,
        from_status=from_status,
        to_status=to_status,
        establishment_id=to.establishment_id or src.establishment_id,
        screen_id=to.screen_id or src.screen_id,
        org_id=to.org_id or src.org_id,
        comodato_agreement_id=agreement.id if agreement else None,
        replaced_item_id=replaced_item_id,
        reference=(reference or None),
        expected_return_at=expected_return_at,
        reason=(reason or None),
        performed_by=getattr(ctx, "user_id", None),
    )
    db.add(mv)
    item.movement_count = (item.movement_count or 0) + 1
    item.last_movement_at = utcnow()
    if serial:
        item.status = to_status
        item.location_kind = to.kind
        item.location_screen_id = to.screen_id
        item.location_establishment_id = to.establishment_id
        item.location_user_id = to.user_id
        item.scope_org_id = to.org_id
        if to.kind in (LK.WAREHOUSE.value, LK.MAINTENANCE.value):
            item.location_city_key = to.city_key if to.city_key is not None else (
                src.city_key if to.kind == LK.MAINTENANCE.value else None)
        elif to.kind in (LK.SCREEN.value, LK.ESTABLISHMENT.value):
            item.location_city_key = to.city_key
        elif to.kind == LK.TECHNICIAN.value:
            item.location_city_key = None
        else:
            item.location_city_key = None
        item.comodato_agreement_id = agreement.id if agreement else (
            item.comodato_agreement_id if to.kind == LK.SCREEN.value and movement_type == MT.MAINTENANCE_OUT.value
            else None)
        item.expected_return_at = expected_return_at if movement_type == MT.LOAN.value else None
    await db.flush()
    if audit_it:
        await audit(
            db, ctx, f"equipment.movement.{movement_type.lower()}", "equipment_item", item.id,
            before={"location": src.out(), "status": from_status},
            after={"location": to.out(), "status": to_status, "quantity": quantity, "movement_id": str(mv.id)},
            reason=reason, org_id=mv.org_id,
        )
    return mv


async def create_item(
    db: AsyncSession,
    ctx: Any,
    *,
    type: str,
    tracking: str,
    name: str,
    quantity: int,
    warehouse_city_key: Optional[str],
    reference: Optional[str],
    **fields: Any,
) -> tuple[EquipmentItem, Optional[EquipmentMovement]]:
    if type not in {t.value for t in EquipmentType}:
        raise DomainError("EQUIPMENT_TYPE_INVALID", "Tipo de equipamento inválido.", status=400)
    if tracking not in {t.value for t in TrackingMode}:
        raise DomainError("TRACKING_INVALID", "Rastreio deve ser SERIAL ou BULK.", status=400)
    serial = tracking == TrackingMode.SERIAL.value
    if serial and quantity != 1:
        raise DomainError("QUANTITY_INVALID", "Item com número de série tem quantidade 1.", status=400)
    if not serial and (fields.get("serial_number") or fields.get("asset_tag")):
        raise DomainError("BULK_NO_SERIAL", "Item de quantidade não tem série/patrimônio individual.", status=400)
    if quantity < 0:
        raise DomainError("QUANTITY_INVALID", "Quantidade inválida.", status=400)
    for f in ("serial_number", "asset_tag"):
        v = fields.get(f)
        if v:
            col = getattr(EquipmentItem, f)
            q = select(EquipmentItem.id).where(col == v)
            if f == "serial_number":
                q = q.where(EquipmentItem.type == type)
            if (await db.execute(q)).scalar_one_or_none() is not None:
                raise Conflict("EQUIPMENT_DUPLICATE", "Já existe item com este número de série/patrimônio.",
                               details={"field": f})
    item = EquipmentItem(
        id=uuid.uuid4(), type=type, tracking=tracking, name=name,
        status=EquipmentStatus.IN_STOCK.value if serial else EquipmentStatus.BULK.value,
        location_kind=LK.EXTERNAL.value if serial else None,
        created_by=getattr(ctx, "user_id", None),
        **fields,
    )
    db.add(item)
    await db.flush()
    mv = None
    if quantity > 0:
        mv = await move(db, ctx, item, MT.ENTRY.value, Location(LK.WAREHOUSE.value, None, warehouse_city_key),
                        source=Location(LK.EXTERNAL.value), quantity=quantity, reference=reference,
                        reason="Entrada no estoque", audit_it=False)
    await audit(db, ctx, "equipment.created", "equipment_item", item.id,
                after={"type": type, "tracking": tracking, "name": name, "quantity": quantity,
                       "serial_number": fields.get("serial_number"), "asset_tag": fields.get("asset_tag")})
    return item, mv


async def follow_screen_move(db: AsyncSession, screen: Screen, new_est: Establishment) -> int:
    """Itens instalados numa tela que mudou de estabelecimento acompanham a tela (projeção)."""
    items = (
        await db.execute(
            select(EquipmentItem).where(EquipmentItem.location_screen_id == screen.id).with_for_update()
        )
    ).scalars().all()
    for it in items:
        it.location_establishment_id = new_est.id
        it.scope_org_id = new_est.org_id
        it.location_city_key = new_est.city_key
    return len(items)


async def installed_items(db: AsyncSession, screen_id: uuid.UUID) -> list[EquipmentItem]:
    return list(
        (
            await db.execute(
                select(EquipmentItem).where(EquipmentItem.location_screen_id == screen_id).order_by(EquipmentItem.id)
            )
        ).scalars().all()
    )


def item_out(it: EquipmentItem, balances_: Optional[list] = None) -> dict:
    from app.modules.organizations.common import iso

    d = {
        "id": str(it.id),
        "type": it.type,
        "tracking": it.tracking,
        "name": it.name,
        "brand": it.brand,
        "model": it.model,
        "serial_number": it.serial_number,
        "asset_tag": it.asset_tag,
        "inches": it.inches,
        "unit": it.unit,
        "condition": it.condition,
        "supplier": it.supplier,
        "purchase_date": iso(it.purchase_date),
        "purchase_cost_cents": it.purchase_cost_cents,
        "warranty_until": iso(it.warranty_until),
        "notes": it.notes,
        "status": it.status,
        "location": None if it.tracking == TrackingMode.BULK.value else {
            "kind": it.location_kind,
            "city_key": it.location_city_key,
            "establishment_id": str(it.location_establishment_id) if it.location_establishment_id else None,
            "screen_id": str(it.location_screen_id) if it.location_screen_id else None,
            "user_id": str(it.location_user_id) if it.location_user_id else None,
        },
        "comodato_agreement_id": str(it.comodato_agreement_id) if it.comodato_agreement_id else None,
        "expected_return_at": iso(it.expected_return_at),
        "movement_count": it.movement_count,
        "last_movement_at": iso(it.last_movement_at),
        "created_at": iso(it.created_at),
    }
    if balances_ is not None:
        d["balances"] = balances_
        d["total_quantity"] = sum(b["quantity"] for b in balances_ if b["kind"] != LK.DISPOSED.value)
    return d


def movement_out(m: EquipmentMovement) -> dict:
    from app.modules.organizations.common import iso

    return {
        "id": str(m.id),
        "seq": m.seq,
        "item_id": str(m.item_id),
        "group_id": str(m.group_id) if m.group_id else None,
        "movement_type": m.movement_type,
        "quantity": m.quantity,
        "from": {"kind": m.from_kind, "ref_id": str(m.from_ref_id) if m.from_ref_id else None,
                 "city_key": m.from_city_key},
        "to": {"kind": m.to_kind, "ref_id": str(m.to_ref_id) if m.to_ref_id else None, "city_key": m.to_city_key},
        "from_status": m.from_status,
        "to_status": m.to_status,
        "establishment_id": str(m.establishment_id) if m.establishment_id else None,
        "screen_id": str(m.screen_id) if m.screen_id else None,
        "comodato_agreement_id": str(m.comodato_agreement_id) if m.comodato_agreement_id else None,
        "replaced_item_id": str(m.replaced_item_id) if m.replaced_item_id else None,
        "reference": m.reference,
        "expected_return_at": iso(m.expected_return_at),
        "reason": m.reason,
        "performed_by": str(m.performed_by) if m.performed_by else None,
        "created_at": iso(m.created_at),
    }

