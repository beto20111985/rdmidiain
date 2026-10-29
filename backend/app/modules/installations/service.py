"""
RD Mídia Indoor — Instalação pelo técnico e manutenção (W-ORG; ETAPA2 #1135–#1145,
#1336–#1341, #1108–#1116; P001 §111–§112).

Instalação: checklist, geolocalização do técnico (distância até o estabelecimento),
teste REAL de conexão (presença do player no Redis, fornecida pelo W-PLAY), fotos e
assinatura do responsável (imagens validadas por magic bytes + decodificação) e
conclusão só com os requisitos cumpridos (o teste de conexão pode ser dispensado com
justificativa registrada — nunca marcado como "online" sem prova).

Manutenção: ordem com status, técnico designado e histórico append-only; a tela vai
para MAINTENANCE sem sair do inventário (continua vendável); ao resolver a última ordem
aberta volta a ACTIVE.
"""
from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.core.timeutils import local_date, utcnow
from app.models.establishments import Establishment
from app.models.installations import (
    Installation,
    InstallationPhoto,
    InstallationStatus,
    MaintenanceEvent,
    MaintenanceOrder,
    MaintenanceStatus,
    PhotoKind,
)
from app.models.screens import Screen, ScreenHistoryEvent, ScreenStatus
from app.modules.establishments.geo import haversine_m
from app.modules.organizations import files
from app.modules.organizations import notifications as _notification_types  # noqa: F401  (registra tipos)

DEFAULT_CHECKLIST: list[dict] = [
    {"key": "tv_fixed", "label": "TV fixada com segurança (suporte/parede)", "required": True},
    {"key": "power", "label": "Alimentação elétrica estável", "required": True},
    {"key": "player_connected", "label": "Player conectado à TV (HDMI)", "required": True},
    {"key": "network", "label": "Rede/Wi-Fi configurada e com internet", "required": True},
    {"key": "paired", "label": "Dispositivo pareado à tela", "required": True},
    {"key": "orientation", "label": "Orientação da tela conferida", "required": True},
    {"key": "playback", "label": "Reprodução conferida na tela", "required": True},
    {"key": "cables_organized", "label": "Cabos organizados", "required": False},
    {"key": "responsible_trained", "label": "Responsável orientado (suporte por ticket com o ID do anúncio)",
     "required": False},
]
CONNECTION_TEST_MAX_AGE = timedelta(minutes=30)
PRESENCE_FRESH_MS = 120_000


async def user_can(db: AsyncSession, user_id: uuid.UUID, perm: str, est: Establishment) -> bool:
    from app.core.authz import AuthContext, load_grants
    from app.models.users import User

    u = await db.get(User, user_id)
    if u is None or not u.is_active:
        return False
    tmp = AuthContext(kind="user", user=u, grants=await load_grants(db, u.id))
    return tmp.can(perm, org_id=est.org_id, establishment_id=est.id, city=est.city_key)


def _actor_can_work(ctx: Any, inst: Installation) -> None:
    if ctx.user_id == inst.technician_user_id or ctx.can("equipment.manage", global_only=True):
        return
    raise Forbidden("NOT_ASSIGNED_TECHNICIAN", "Somente o técnico designado (ou a equipe RD) executa a instalação.")


def _require_status(inst: Installation, *allowed: str) -> None:
    if inst.status not in allowed:
        raise Conflict("INSTALLATION_INVALID_STATE", "Operação não permitida no status atual da instalação.",
                       details={"status": inst.status, "allowed": list(allowed)})


async def create_installation(db: AsyncSession, ctx: Any, est: Establishment, screen: Optional[Screen],
                              technician_user_id: uuid.UUID, scheduled_date, extra_items: list[dict]) -> Installation:
    if not await user_can(db, technician_user_id, "equipment.manage", est):
        raise DomainError("TECHNICIAN_OUT_OF_SCOPE",
                          "O técnico informado não tem permissão de equipamentos neste estabelecimento.", status=400)
    checklist = [dict(i, done=False, note=None) for i in DEFAULT_CHECKLIST]
    keys = {i["key"] for i in checklist}
    for it in extra_items or []:
        k = str(it.get("key") or "").strip()
        if not k or k in keys:
            raise DomainError("CHECKLIST_ITEM_INVALID", "Item de checklist inválido ou repetido.", status=400)
        checklist.append({"key": k[:40], "label": str(it.get("label") or k)[:200], "required": bool(it.get("required")),
                          "done": False, "note": None})
        keys.add(k)
    inst = Installation(id=uuid.uuid4(), establishment_id=est.id, screen_id=screen.id if screen else None,
                        technician_user_id=technician_user_id, status=InstallationStatus.SCHEDULED.value,
                        scheduled_date=scheduled_date, checklist=checklist, created_by=getattr(ctx, "user_id", None))
    db.add(inst)
    await db.flush()
    await audit(db, ctx, "installation.scheduled", "installation", inst.id,
                after={"establishment_id": str(est.id), "screen_id": str(screen.id) if screen else None,
                       "technician_user_id": str(technician_user_id), "scheduled_date": scheduled_date},
                org_id=est.org_id)
    from app.modules.notifications.service import notify

    await notify(db, [technician_user_id], "installation.assigned",
                 {"establishment_name": est.name, "installation_id": str(inst.id),
                  "scheduled_date": scheduled_date.isoformat() if scheduled_date else "a definir"},
                 dedupe_key=f"installation.assigned:{inst.id}:{technician_user_id}", entity=("installation", inst.id),
                 org_id=est.org_id)
    return inst


async def update_installation(db: AsyncSession, ctx: Any, inst: Installation, est: Establishment,
                              changes: dict) -> dict:
    """Ajusta a instalação ANTES da conclusão: tela (vincular/trocar/desvincular), técnico, data, observações.

    A tela precisa ser do MESMO estabelecimento (senão 404) e não estar retirada definitivamente. Trocar a
    tela invalida o teste de conexão anterior (era de outra tela). Trocar o técnico exige que ele atenda o
    estabelecimento e o notifica. Auditado (``installation.updated``)."""
    _require_status(inst, InstallationStatus.SCHEDULED.value, InstallationStatus.IN_PROGRESS.value)
    before: dict = {}
    after: dict = {}
    if "technician_user_id" in changes and changes["technician_user_id"] != inst.technician_user_id:
        new_tech = changes["technician_user_id"]
        if new_tech is None or not await user_can(db, new_tech, "equipment.manage", est):
            raise DomainError("TECHNICIAN_OUT_OF_SCOPE",
                              "O técnico informado não tem permissão de equipamentos neste estabelecimento.",
                              status=400)
        before["technician_user_id"], after["technician_user_id"] = inst.technician_user_id, new_tech
        inst.technician_user_id = new_tech
    other = {k: v for k, v in changes.items() if k != "technician_user_id"}
    # planejamento (PATCH) = equipment.manage no escopo do estabelecimento (checado na rota);
    # execução (checklist/fotos/assinatura/conclusão) continua restrita ao técnico designado ou à RD GLOBAL
    if "screen_id" in other and other["screen_id"] != inst.screen_id:
        sid = other["screen_id"]
        if sid is not None:
            scr = await db.get(Screen, sid)
            if scr is None or scr.establishment_id != est.id:
                raise NotFound()
            if scr.status == ScreenStatus.DECOMMISSIONED.value:
                raise Conflict("SCREEN_DECOMMISSIONED", "Tela retirada definitivamente.")
        before["screen_id"], after["screen_id"] = inst.screen_id, sid
        inst.screen_id = sid
        if inst.connection_test is not None or inst.connection_test_waiver:
            before["connection_test"] = (inst.connection_test or {}).get("status")
            after["connection_test"] = None
            inst.connection_test = None
            inst.connection_test_waiver = None
    if "scheduled_date" in other and other["scheduled_date"] != inst.scheduled_date:
        before["scheduled_date"], after["scheduled_date"] = inst.scheduled_date, other["scheduled_date"]
        inst.scheduled_date = other["scheduled_date"]
    if "notes" in other and other["notes"] != inst.notes:
        before["notes"], after["notes"] = inst.notes, other["notes"]
        inst.notes = other["notes"]
    if after:
        await db.flush()
        await audit(db, ctx, "installation.updated", "installation", inst.id, before=before, after=after,
                    org_id=est.org_id)
        if "technician_user_id" in after:
            from app.modules.notifications.service import notify

            tech_id = after["technician_user_id"]
            await notify(db, [tech_id], "installation.assigned",
                         {"establishment_name": est.name, "installation_id": str(inst.id),
                          "scheduled_date": inst.scheduled_date.isoformat() if inst.scheduled_date else "a definir"},
                         dedupe_key=f"installation.assigned:{inst.id}:{tech_id}",
                         entity=("installation", inst.id), org_id=est.org_id)
    return after


async def start(db: AsyncSession, ctx: Any, inst: Installation, est: Establishment) -> None:
    _actor_can_work(ctx, inst)
    _require_status(inst, InstallationStatus.SCHEDULED.value)
    inst.status = InstallationStatus.IN_PROGRESS.value
    inst.started_at = utcnow()
    await db.flush()
    await audit(db, ctx, "installation.started", "installation", inst.id, before={"status": "SCHEDULED"},
                after={"status": inst.status}, org_id=est.org_id)


async def update_checklist(db: AsyncSession, ctx: Any, inst: Installation, est: Establishment, items: list[dict]) -> None:
    _actor_can_work(ctx, inst)
    _require_status(inst, InstallationStatus.IN_PROGRESS.value)
    by_key = {i["key"]: dict(i) for i in inst.checklist}
    changes = {}
    for it in items:
        k = it.get("key")
        if k not in by_key:
            raise DomainError("CHECKLIST_ITEM_INVALID", "Item de checklist inexistente.", status=400, details={"key": k})
        by_key[k]["done"] = bool(it.get("done"))
        if it.get("note") is not None:
            by_key[k]["note"] = str(it["note"])[:500]
        changes[k] = by_key[k]["done"]
    inst.checklist = [by_key[i["key"]] for i in inst.checklist]
    await db.flush()
    await audit(db, ctx, "installation.checklist_updated", "installation", inst.id, after={"items": changes},
                org_id=est.org_id)


async def set_geolocation(db: AsyncSession, ctx: Any, inst: Installation, est: Establishment, lat: float, lng: float,
                          accuracy_m: Optional[int]) -> None:
    _actor_can_work(ctx, inst)
    _require_status(inst, InstallationStatus.IN_PROGRESS.value)
    inst.tech_lat, inst.tech_lng, inst.tech_accuracy_m, inst.tech_geo_at = lat, lng, accuracy_m, utcnow()
    inst.distance_to_establishment_m = haversine_m(lat, lng, est.lat, est.lng) if est.lat is not None else None
    await db.flush()
    await audit(db, ctx, "installation.geolocated", "installation", inst.id,
                after={"lat": lat, "lng": lng, "accuracy_m": accuracy_m,
                       "distance_to_establishment_m": inst.distance_to_establishment_m}, org_id=est.org_id)


async def probe_screen_connection(db: AsyncSession, screen_id: uuid.UUID) -> dict:
    """Teste real: há dispositivo ATIVO pareado e com presença recente no Redis?"""
    now = utcnow()
    try:
        from app.models.devices import Device
        from app.modules.presence import service as presence
    except ImportError:  # pragma: no cover
        return {"status": "UNAVAILABLE", "checked_at": now.isoformat(), "source": "none",
                "detail": "Serviço de presença indisponível."}
    dev = (await db.execute(select(Device).where(Device.screen_id == screen_id, Device.status == "ACTIVE"))
           ).scalars().first()
    if dev is None:
        return {"status": "NOT_PAIRED", "checked_at": now.isoformat(), "source": "devices",
                "detail": "Nenhum dispositivo ativo pareado a esta tela."}
    try:
        snap = await presence.get_snapshot(dev.id)
    except Exception as exc:  # noqa: BLE001 — Redis indisponível: resultado honesto
        return {"status": "UNAVAILABLE", "checked_at": now.isoformat(), "source": "redis",
                "device_id": str(dev.id), "detail": f"Presença indisponível ({type(exc).__name__})."}
    last_ms = int(snap.get("last_seen_ms") or 0)
    age = int(time.time() * 1000) - last_ms if last_ms else None
    online = snap.get("connected") == "1" and age is not None and age <= PRESENCE_FRESH_MS
    return {"status": "ONLINE" if online else "OFFLINE", "checked_at": now.isoformat(), "source": "presence",
            "device_id": str(dev.id), "last_seen_age_ms": age, "player_status": snap.get("status"),
            "app_version": snap.get("app_version")}


async def connection_test(db: AsyncSession, ctx: Any, inst: Installation, est: Establishment) -> dict:
    _actor_can_work(ctx, inst)
    _require_status(inst, InstallationStatus.IN_PROGRESS.value)
    if inst.screen_id is None:
        raise DomainError("INSTALLATION_NO_SCREEN", "Vincule a tela à instalação antes do teste.", status=400)
    res = await probe_screen_connection(db, inst.screen_id)
    inst.connection_test = res
    await db.flush()
    await audit(db, ctx, "installation.connection_tested", "installation", inst.id, after=res, org_id=est.org_id)
    return res


async def add_photo(db: AsyncSession, ctx: Any, inst: Installation, est: Establishment, kind: str,
                    data: bytes) -> InstallationPhoto:
    _actor_can_work(ctx, inst)
    _require_status(inst, InstallationStatus.IN_PROGRESS.value)
    if kind not in {k.value for k in PhotoKind}:
        raise DomainError("PHOTO_KIND_INVALID", "Tipo de foto inválido.", status=400)
    count = (await db.execute(select(func.count()).select_from(InstallationPhoto).where(
        InstallationPhoto.installation_id == inst.id))).scalar_one()
    if count >= 30:
        raise DomainError("PHOTO_LIMIT", "Limite de fotos por instalação atingido.", status=400)
    info = files.sniff_image(data)
    pid = uuid.uuid4()
    ext = "jpg" if info.content_type == "image/jpeg" else "png"
    stored = await files.store(db, files.key_for("installations", est.org_id, inst.id, f"{pid}.{ext}"), data,
                               info.content_type, max_bytes=files.MAX_IMAGE_BYTES)
    ph = InstallationPhoto(id=pid, installation_id=inst.id, kind=kind, storage_key=stored.key, sha256=stored.sha256,
                           content_type=info.content_type, size_bytes=stored.size, width=info.width,
                           height=info.height, uploaded_by=getattr(ctx, "user_id", None))
    db.add(ph)
    await db.flush()
    await audit(db, ctx, "installation.photo_added", "installation", inst.id,
                after={"photo_id": str(pid), "kind": kind, "sha256": stored.sha256, "size": stored.size},
                org_id=est.org_id)
    return ph


async def set_signer(db: AsyncSession, ctx: Any, inst: Installation, est: Establishment, signer_name: str,
                     signer_role: Optional[str], data: bytes) -> None:
    _actor_can_work(ctx, inst)
    _require_status(inst, InstallationStatus.IN_PROGRESS.value)
    signer_name = (signer_name or "").strip()
    if len(signer_name) < 3:
        raise DomainError("SIGNER_NAME_REQUIRED", "Informe o nome de quem assinou.", status=400)
    if len(data) > files.MAX_SIGNATURE_BYTES:
        raise DomainError("FILE_TOO_LARGE", "Imagem de assinatura maior que o permitido.", status=413)
    info = files.sniff_image(data)
    ext = "jpg" if info.content_type == "image/jpeg" else "png"
    stored = await files.store(db, files.key_for("installations", est.org_id, inst.id, f"assinatura-{uuid.uuid4()}.{ext}"),
                               data, info.content_type, max_bytes=files.MAX_SIGNATURE_BYTES)
    inst.signer_name, inst.signer_role = signer_name, (signer_role or None)
    inst.signer_image_key, inst.signer_image_sha256, inst.signed_at = stored.key, stored.sha256, utcnow()
    await db.flush()
    await audit(db, ctx, "installation.signed", "installation", inst.id,
                after={"signer_name": signer_name, "signer_role": signer_role, "image_sha256": stored.sha256},
                org_id=est.org_id)


async def complete(db: AsyncSession, ctx: Any, inst: Installation, est: Establishment,
                   waiver: Optional[str]) -> list[str]:
    _actor_can_work(ctx, inst)
    _require_status(inst, InstallationStatus.IN_PROGRESS.value)
    missing: list[str] = []
    pend = [i["key"] for i in inst.checklist if i.get("required") and not i.get("done")]
    if pend:
        missing.append("checklist:" + ",".join(pend))
    photos = (await db.execute(select(func.count()).select_from(InstallationPhoto).where(
        InstallationPhoto.installation_id == inst.id, InstallationPhoto.kind == PhotoKind.TV_WORKING.value))).scalar_one()
    if not photos:
        missing.append("photo:TV_WORKING")
    if not inst.signer_image_key:
        missing.append("signature")
    if inst.tech_lat is None:
        missing.append("geolocation")
    if inst.screen_id is None:
        missing.append("screen")
    ct = inst.connection_test or {}
    fresh_online = False
    if ct.get("status") == "ONLINE" and ct.get("checked_at"):
        from datetime import datetime

        fresh_online = utcnow() - datetime.fromisoformat(ct["checked_at"]) <= CONNECTION_TEST_MAX_AGE
    waiver = (waiver or "").strip()
    if not fresh_online:
        if len(waiver) < 20:
            missing.append("connection_test")
        else:
            inst.connection_test_waiver = waiver
    if missing:
        raise DomainError("INSTALLATION_INCOMPLETE", "Instalação incompleta.", status=400, details={"missing": missing})
    now = utcnow()
    inst.status = InstallationStatus.COMPLETED.value
    inst.completed_at = now
    screen = await db.get(Screen, inst.screen_id, with_for_update=True)
    if screen is not None:
        from app.modules.screens.service import add_history

        before = {"installed_at": screen.installed_at.isoformat() if screen.installed_at else None,
                  "installed_by": str(screen.installed_by) if screen.installed_by else None}
        screen.installed_at = now
        screen.installed_by = inst.technician_user_id
        await add_history(db, screen, ScreenHistoryEvent.HARDWARE_CHANGED, actor_user_id=getattr(ctx, "user_id", None),
                          before=before, after={"installed_at": now.isoformat(),
                                                "installed_by": str(inst.technician_user_id),
                                                "installation_id": str(inst.id)},
                          reason="Instalação concluída")
    if est.installation_date is None:
        est.installation_date = local_date(now, est.timezone)
    if est.responsible_technician_id is None:
        est.responsible_technician_id = inst.technician_user_id
    await db.flush()
    await audit(db, ctx, "installation.completed", "installation", inst.id,
                after={"status": inst.status, "connection_test": ct.get("status"),
                       "connection_test_waived": bool(inst.connection_test_waiver)},
                reason=inst.connection_test_waiver, org_id=est.org_id)
    return []


async def cancel(db: AsyncSession, ctx: Any, inst: Installation, est: Establishment, reason: str) -> None:
    _require_status(inst, InstallationStatus.SCHEDULED.value, InstallationStatus.IN_PROGRESS.value)
    before = inst.status
    inst.status = InstallationStatus.CANCELLED.value
    await db.flush()
    await audit(db, ctx, "installation.cancelled", "installation", inst.id, before={"status": before},
                after={"status": inst.status}, reason=reason, org_id=est.org_id)


def installation_out(inst: Installation, photos: Optional[list] = None) -> dict:
    from app.modules.organizations.common import iso

    d = {
        "id": str(inst.id), "establishment_id": str(inst.establishment_id),
        "screen_id": str(inst.screen_id) if inst.screen_id else None,
        "technician_user_id": str(inst.technician_user_id), "status": inst.status,
        "scheduled_date": iso(inst.scheduled_date), "started_at": iso(inst.started_at),
        "completed_at": iso(inst.completed_at), "checklist": inst.checklist,
        "geolocation": None if inst.tech_lat is None else {
            "lat": inst.tech_lat, "lng": inst.tech_lng, "accuracy_m": inst.tech_accuracy_m,
            "captured_at": iso(inst.tech_geo_at), "distance_to_establishment_m": inst.distance_to_establishment_m},
        "connection_test": inst.connection_test, "connection_test_waiver": inst.connection_test_waiver,
        "signer": None if not inst.signer_image_key else {
            "name": inst.signer_name, "role": inst.signer_role, "image_sha256": inst.signer_image_sha256,
            "signed_at": iso(inst.signed_at)},
        "notes": inst.notes, "created_at": iso(inst.created_at),
    }
    if photos is not None:
        d["photos"] = [{"id": str(p.id), "kind": p.kind, "content_type": p.content_type, "size_bytes": p.size_bytes,
                        "sha256": p.sha256, "width": p.width, "height": p.height, "created_at": iso(p.created_at),
                        "uploaded_by": str(p.uploaded_by) if p.uploaded_by else None}
                       for p in photos]
    d["created_by"] = str(inst.created_by) if inst.created_by else None
    return d


# ─── Manutenção ──────────────────────────────────────────────────────────────

TRANSITIONS = {
    MaintenanceStatus.OPEN.value: {"IN_PROGRESS", "WAITING_PARTS", "RESOLVED", "CANCELLED"},
    MaintenanceStatus.IN_PROGRESS.value: {"WAITING_PARTS", "RESOLVED", "CANCELLED"},
    MaintenanceStatus.WAITING_PARTS.value: {"IN_PROGRESS", "RESOLVED", "CANCELLED"},
    MaintenanceStatus.RESOLVED.value: set(),
    MaintenanceStatus.CANCELLED.value: set(),
}
OPEN_STATES = (MaintenanceStatus.OPEN.value, MaintenanceStatus.IN_PROGRESS.value, MaintenanceStatus.WAITING_PARTS.value)


def _event(db: AsyncSession, ctx: Any, order: MaintenanceOrder, event: str, *, from_status=None, to_status=None,
           note=None, technician=None) -> None:
    db.add(MaintenanceEvent(id=uuid.uuid4(), order_id=order.id, event=event, from_status=from_status,
                            to_status=to_status, note=note, technician_user_id=technician,
                            actor_user_id=getattr(ctx, "user_id", None)))


async def _notify_technician(db: AsyncSession, order: MaintenanceOrder, est: Establishment, user_id: uuid.UUID) -> None:
    from app.modules.notifications.service import notify

    await notify(db, [user_id], "maintenance.assigned",
                 {"establishment_name": est.name, "order_id": str(order.id), "component": order.component,
                  "priority": order.priority},
                 dedupe_key=f"maintenance.assigned:{order.id}:{user_id}", entity=("maintenance_order", order.id),
                 org_id=est.org_id)


async def open_order(db: AsyncSession, ctx: Any, est: Establishment, screen: Optional[Screen],
                     item_id: Optional[uuid.UUID], component: str, priority: str, description: str,
                     technician_user_id: Optional[uuid.UUID], put_screen_in_maintenance: bool) -> MaintenanceOrder:
    if technician_user_id is not None and not await user_can(db, technician_user_id, "maintenance.manage", est):
        raise DomainError("TECHNICIAN_OUT_OF_SCOPE", "O técnico não atende este estabelecimento.", status=400)
    order = MaintenanceOrder(id=uuid.uuid4(), establishment_id=est.id, screen_id=screen.id if screen else None,
                             equipment_item_id=item_id, component=component, priority=priority,
                             description=description.strip(), technician_user_id=technician_user_id,
                             status=MaintenanceStatus.OPEN.value, opened_by=getattr(ctx, "user_id", None))
    db.add(order)
    await db.flush()
    _event(db, ctx, order, "OPENED", to_status=order.status, note=description)
    if technician_user_id:
        _event(db, ctx, order, "ASSIGNED", technician=technician_user_id)
    if screen is not None and put_screen_in_maintenance and screen.status == ScreenStatus.ACTIVE.value:
        from app.modules.screens.service import set_status

        await set_status(db, ctx, screen, est, ScreenStatus.MAINTENANCE.value,
                         f"Ordem de manutenção {order.id} aberta")
    await db.flush()
    await audit(db, ctx, "maintenance.opened", "maintenance_order", order.id,
                after={"establishment_id": str(est.id), "screen_id": str(screen.id) if screen else None,
                       "component": component, "priority": priority,
                       "technician_user_id": str(technician_user_id) if technician_user_id else None},
                org_id=est.org_id)
    if technician_user_id:
        await _notify_technician(db, order, est, technician_user_id)
    return order


async def assign(db: AsyncSession, ctx: Any, order: MaintenanceOrder, est: Establishment,
                 technician_user_id: uuid.UUID) -> None:
    if order.status not in OPEN_STATES:
        raise Conflict("MAINTENANCE_CLOSED", "Ordem encerrada.")
    if not await user_can(db, technician_user_id, "maintenance.manage", est):
        raise DomainError("TECHNICIAN_OUT_OF_SCOPE", "O técnico não atende este estabelecimento.", status=400)
    before = order.technician_user_id
    order.technician_user_id = technician_user_id
    _event(db, ctx, order, "ASSIGNED", technician=technician_user_id)
    await db.flush()
    await audit(db, ctx, "maintenance.assigned", "maintenance_order", order.id,
                before={"technician_user_id": str(before) if before else None},
                after={"technician_user_id": str(technician_user_id)}, org_id=est.org_id)
    await _notify_technician(db, order, est, technician_user_id)


async def change_status(db: AsyncSession, ctx: Any, order: MaintenanceOrder, est: Establishment, status: str,
                        note: Optional[str]) -> None:
    if status not in TRANSITIONS.get(order.status, set()):
        raise Conflict("MAINTENANCE_INVALID_TRANSITION", "Transição de status inválida.",
                       details={"from": order.status, "to": status})
    if status in (MaintenanceStatus.RESOLVED.value, MaintenanceStatus.CANCELLED.value) and len((note or "").strip()) < 10:
        raise DomainError("RESOLUTION_REQUIRED", "Descreva a solução/motivo (mínimo 10 caracteres).", status=400)
    before = order.status
    order.status = status
    if status == MaintenanceStatus.RESOLVED.value:
        order.resolution = note
    if status in (MaintenanceStatus.RESOLVED.value, MaintenanceStatus.CANCELLED.value):
        order.closed_at = utcnow()
    _event(db, ctx, order, "RESOLVED" if status == "RESOLVED" else "CANCELLED" if status == "CANCELLED"
           else "STATUS_CHANGED", from_status=before, to_status=status, note=note)
    await db.flush()
    if order.closed_at is not None and order.screen_id is not None:
        others = (await db.execute(select(func.count()).select_from(MaintenanceOrder).where(
            MaintenanceOrder.screen_id == order.screen_id, MaintenanceOrder.status.in_(OPEN_STATES)))).scalar_one()
        screen = await db.get(Screen, order.screen_id, with_for_update=True)
        if others == 0 and screen is not None and screen.status == ScreenStatus.MAINTENANCE.value:
            from app.modules.screens.service import set_status

            await set_status(db, ctx, screen, est, ScreenStatus.ACTIVE.value, f"Manutenção {order.id} encerrada")
    await audit(db, ctx, "maintenance.status_changed", "maintenance_order", order.id, before={"status": before},
                after={"status": status}, reason=note, org_id=est.org_id)


async def add_note(db: AsyncSession, ctx: Any, order: MaintenanceOrder, est: Establishment, note: str) -> None:
    _event(db, ctx, order, "NOTE", note=note)
    await db.flush()
    await audit(db, ctx, "maintenance.note_added", "maintenance_order", order.id, after={"note": note},
                org_id=est.org_id)


def order_out(o: MaintenanceOrder, events: Optional[list] = None) -> dict:
    from app.modules.organizations.common import iso

    d = {
        "id": str(o.id), "establishment_id": str(o.establishment_id),
        "screen_id": str(o.screen_id) if o.screen_id else None,
        "equipment_item_id": str(o.equipment_item_id) if o.equipment_item_id else None,
        "component": o.component, "status": o.status, "priority": o.priority, "description": o.description,
        "resolution": o.resolution, "technician_user_id": str(o.technician_user_id) if o.technician_user_id else None,
        "opened_by": str(o.opened_by) if o.opened_by else None, "opened_at": iso(o.opened_at),
        "closed_at": iso(o.closed_at),
    }
    if events is not None:
        d["history"] = [{"id": str(e.id), "event": e.event, "from_status": e.from_status, "to_status": e.to_status,
                         "technician_user_id": str(e.technician_user_id) if e.technician_user_id else None,
                         "note": e.note, "actor_user_id": str(e.actor_user_id) if e.actor_user_id else None,
                         "created_at": iso(e.created_at)} for e in events]
    return d


async def get_installation(db: AsyncSession, inst_id: uuid.UUID, *, lock: bool = False) -> Installation:
    q = select(Installation).where(Installation.id == inst_id)
    if lock:
        q = q.with_for_update()
    inst = (await db.execute(q)).scalar_one_or_none()
    if inst is None:
        raise NotFound()
    return inst
