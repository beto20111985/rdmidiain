"""
RD Mídia Indoor — Troca de hardware da tela preservando histórico (ETAPA2 #161–#162, #210;
P001 §21; REQ-TEL-002/006).

A tela (``screens.id`` e ``TEL-UF-XXXX``) NÃO muda. A troca:
1. grava o snapshot anterior e o novo em ``screen_history`` (TV_REPLACED /
   PLAYER_REPLACED / HARDWARE_CHANGED) — append-only;
2. se havia item de estoque da RD instalado (TV/TV Box), registra a saída dele
   (devolução ao estoque ou envio para manutenção) e a instalação do novo item, com o
   MESMO ``group_id`` no ledger (``equipment_movements``) e ``replaced_item_id``;
3. atualiza o snapshot de hardware da tela a partir do item novo (marca, modelo, série,
   patrimônio, polegadas) + campos informados.
A identidade criptográfica do player (pareamento) é do W-PLAY: trocar o TV Box exige
novo pareamento lá; o histórico de reprodução continua ligado à mesma tela.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import DomainError
from app.models.equipment import EquipmentType, LocationKind, MovementType, TrackingMode
from app.models.establishments import Establishment
from app.models.screens import Screen, ScreenHistoryEvent, ScreenStatus
from app.modules.assets import service as assets
from app.modules.screens.service import HARDWARE_FIELDS, PLAYER_FIELDS, TV_FIELDS, add_history, snap

COMPONENT_ITEM_TYPE = {"TV": EquipmentType.TV.value, "PLAYER": EquipmentType.TVBOX.value}


async def change_hardware(db: AsyncSession, ctx: Any, screen: Screen, est: Establishment, fields: dict,
                          reason: Optional[str]) -> dict:
    """Atualização de dados de hardware sem troca física de componente."""
    changes = {k: v for k, v in fields.items() if k in HARDWARE_FIELDS and getattr(screen, k) != v}
    if not changes:
        return {}
    before = snap(screen, changes.keys())
    for k, v in changes.items():
        setattr(screen, k, v)
    after = snap(screen, changes.keys())
    await add_history(db, screen, ScreenHistoryEvent.HARDWARE_CHANGED, actor_user_id=getattr(ctx, "user_id", None),
                      before=before, after=after, reason=reason)
    await audit(db, ctx, "screen.hardware_changed", "screen", screen.id, before=before, after=after, reason=reason,
                org_id=est.org_id)
    return after


async def replace_component(
    db: AsyncSession,
    ctx: Any,
    screen: Screen,
    est: Establishment,
    *,
    component: str,
    new_item_id: Optional[uuid.UUID],
    old_item_destination: Optional[str],
    hardware: dict,
    reason: str,
    comodato_agreement_id: Optional[uuid.UUID] = None,
) -> dict:
    if component not in COMPONENT_ITEM_TYPE:
        raise DomainError("COMPONENT_INVALID", "Componente deve ser TV ou PLAYER.", status=400)
    if screen.status == ScreenStatus.DECOMMISSIONED.value:
        raise DomainError("SCREEN_DECOMMISSIONED", "Tela retirada definitivamente.", status=409)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    item_type = COMPONENT_ITEM_TYPE[component]
    fields = TV_FIELDS if component == "TV" else PLAYER_FIELDS
    group_id = uuid.uuid4()
    before = snap(screen, fields)
    movements = []

    # item antigo da RD instalado nesta tela (mesmo tipo)
    olds = [i for i in await assets.installed_items(db, screen.id) if i.type == item_type]
    new_item = None
    lock_ids = sorted({i.id for i in olds} | ({new_item_id} if new_item_id else set()), key=str)
    locked = {iid: await assets.lock_item(db, iid) for iid in lock_ids}  # ordem determinística (evita deadlock)
    if new_item_id:
        new_item = locked[new_item_id]
        if new_item.type != item_type or new_item.tracking != TrackingMode.SERIAL.value:
            raise DomainError("EQUIPMENT_TYPE_MISMATCH", "O item novo não é do tipo do componente trocado.",
                              status=400, details={"expected_type": item_type})
    for old in olds:
        if new_item is not None and old.id == new_item.id:
            raise DomainError("EQUIPMENT_ALREADY_INSTALLED", "Este item já está instalado nesta tela.", status=400)
        dest = (old_item_destination or "WAREHOUSE").upper()
        if dest == "WAREHOUSE":
            mt, to = MovementType.RETURN.value, assets.Location(LocationKind.WAREHOUSE.value, None,
                                                                old.location_city_key)
        elif dest == "MAINTENANCE":
            mt, to = MovementType.MAINTENANCE_IN.value, assets.Location(LocationKind.MAINTENANCE.value, None,
                                                                        old.location_city_key)
        else:
            raise DomainError("DESTINATION_INVALID", "Destino do item retirado: WAREHOUSE ou MAINTENANCE.", status=400)
        movements.append(await assets.move(db, ctx, locked[old.id], mt, to, reason=reason, group_id=group_id,
                                           audit_it=True))
    if new_item is not None:
        mt = MovementType.COMODATO.value if comodato_agreement_id else MovementType.INSTALL.value
        movements.append(await assets.move(
            db, ctx, new_item, mt, assets.Location(LocationKind.SCREEN.value, screen.id), reason=reason,
            group_id=group_id, replaced_item_id=olds[0].id if olds else None,
            comodato_agreement_id=comodato_agreement_id,
        ))
        if component == "TV":
            screen.hardware_brand = new_item.brand
            screen.hardware_model = new_item.model
            screen.serial_number = new_item.serial_number
            screen.asset_tag = new_item.asset_tag
            if new_item.inches:
                screen.inches = new_item.inches
            screen.tv_owner = "RD"
        else:
            screen.player_brand = new_item.brand
            screen.player_model = new_item.model
            screen.player_serial_number = new_item.serial_number
            screen.player_type = "TVBOX"
    for k, v in (hardware or {}).items():
        if k in fields:
            setattr(screen, k, v)
    after = snap(screen, fields)
    event = ScreenHistoryEvent.TV_REPLACED if component == "TV" else ScreenHistoryEvent.PLAYER_REPLACED
    await add_history(db, screen, event, actor_user_id=getattr(ctx, "user_id", None),
                      before={**before, "equipment_item_ids": [str(o.id) for o in olds]},
                      after={**after, "equipment_item_id": str(new_item.id) if new_item else None,
                             "movement_group_id": str(group_id)},
                      reason=reason)
    await db.flush()
    await audit(db, ctx, f"screen.{event.value.lower()}", "screen", screen.id, before=before, after=after,
                reason=reason, org_id=est.org_id)
    return {"before": before, "after": after, "movement_group_id": str(group_id),
            "movements": [assets.movement_out(m) for m in movements]}
