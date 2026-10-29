"""RD Mídia Indoor — Control plane RD (AÇÃO 002 / W-BIZ). Prefixo ``/api/v1/control-plane``.

Instâncias, grupos, releases (DEV → validada → publicada), rollouts com fila e limite de
concorrência, rollback, histórico e heartbeat das instâncias. A execução REAL do deploy
(Portainer) é dependência externa: executor NULO ou SIMULACAO_LOCAL rotulada.

Rotas legadas removidas: ``/health/full``, ``/screens/overview``,
``/screens/{id}/kill-creative`` (→ W-PLAY ``/playback/kills``), ``/stats`` (→ ``/dashboards/admin``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import NotFound
from app.core.timeutils import isoformat_z
from app.models.control_plane import CpEvent, CpInstance, CpInstanceGroup, CpRelease, CpRollout, CpRolloutTarget
from app.modules.control_plane import service as svc

router = APIRouter()

VIEW = require("control_plane.view", global_only=True)
MANAGE = require("control_plane.manage", global_only=True, allow_impersonation=False)


def _z(dt: Optional[datetime]) -> Optional[str]:
    return isoformat_z(dt) if dt else None


def _s(v) -> Optional[str]:
    return str(v) if v is not None else None


def _inst(i: CpInstance) -> dict:
    return {"id": str(i.id), "slug": i.slug, "name": i.name, "kind": i.kind, "base_url": i.base_url,
            "group_id": _s(i.group_id), "status": i.status, "current_version": i.current_version,
            "last_deployed_version": i.last_deployed_version, "health_status": svc.effective_health(i),
            "health": i.health or {}, "last_heartbeat_at": _z(i.last_heartbeat_at), "license": i.license or {},
            "notes": i.notes, "created_at": _z(i.created_at)}


def _rel(r: CpRelease) -> dict:
    return {"id": str(r.id), "version": r.version, "status": r.status, "image_ref": r.image_ref,
            "changelog": r.changelog, "validated_on_instance_id": _s(r.validated_on_instance_id),
            "validated_at": _z(r.validated_at), "published_at": _z(r.published_at), "created_at": _z(r.created_at)}


def _ro(r: CpRollout, targets=None) -> dict:
    out = {"id": str(r.id), "release_id": str(r.release_id), "target_kind": r.target_kind, "target_ref": _s(r.target_ref),
           "max_concurrency": r.max_concurrency, "failure_policy": r.failure_policy, "executor": r.executor,
           "simulated": r.executor == "SIMULACAO_LOCAL", "status": r.status, "is_rollback": r.is_rollback,
           "rollback_of_id": _s(r.rollback_of_id), "reason": r.reason, "started_at": _z(r.started_at),
           "finished_at": _z(r.finished_at), "created_at": _z(r.created_at)}
    if targets is not None:
        out["targets"] = [{"id": str(t.id), "instance_id": str(t.instance_id), "position": t.position,
                           "from_version": t.from_version, "to_version": t.to_version, "status": t.status,
                           "attempt": t.attempt, "detail": t.detail, "started_at": _z(t.started_at),
                           "finished_at": _z(t.finished_at)} for t in targets]
    return out


@router.get("/overview")
async def overview(ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    return await svc.overview(db)


# ─── Grupos ──────────────────────────────────────────────────────────────────

class GroupIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: Optional[str] = Field(None, max_length=500)


@router.get("/groups")
async def list_groups(ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CpInstanceGroup).order_by(CpInstanceGroup.name))).scalars()
    return {"items": [{"id": str(g.id), "name": g.name, "description": g.description} for g in rows]}


@router.post("/groups", status_code=201)
async def create_group(body: GroupIn, ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    g = await svc.create_group(db, ctx, body.name, body.description)
    return {"id": str(g.id), "name": g.name, "description": g.description}


# ─── Instâncias ──────────────────────────────────────────────────────────────

class InstanceIn(BaseModel):
    slug: str = Field(min_length=2, max_length=60)
    name: str = Field(min_length=2, max_length=120)
    kind: Literal["RD_PRODUCAO", "DEV", "WHITE_LABEL", "FRANQUIA", "LICENCA"]
    base_url: Optional[str] = Field(None, max_length=255)
    group_id: Optional[UUID] = None
    license: Optional[dict] = None
    notes: Optional[str] = Field(None, max_length=2000)


class InstancePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    base_url: Optional[str] = Field(None, max_length=255)
    group_id: Optional[UUID] = None
    status: Optional[Literal["ATIVA", "INATIVA", "MANUTENCAO"]] = None
    license: Optional[dict] = None
    notes: Optional[str] = Field(None, max_length=2000)


@router.get("/instances")
async def list_instances(group_id: Optional[UUID] = None, ctx: AuthContext = Depends(VIEW),
                         db: AsyncSession = Depends(get_db)):
    q = select(CpInstance).order_by(CpInstance.slug)
    if group_id:
        q = q.where(CpInstance.group_id == group_id)
    return {"items": [_inst(i) for i in (await db.execute(q)).scalars()]}


@router.post("/instances", status_code=201)
async def create_instance(body: InstanceIn, ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    i, token = await svc.create_instance(db, ctx, body.model_dump())
    out = _inst(i)
    out["instance_token"] = token  # exibido UMA vez; guardado apenas o hash
    return out


@router.get("/instances/{instance_id}")
async def get_instance(instance_id: UUID, ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    i = await db.get(CpInstance, instance_id)
    if i is None:
        raise NotFound()
    out = _inst(i)
    out["events"] = [{"type": e.type, "data": e.data, "created_at": _z(e.created_at)} for e in (await db.execute(
        select(CpEvent).where(CpEvent.instance_id == i.id).order_by(CpEvent.created_at.desc()).limit(50))).scalars()]
    return out


@router.patch("/instances/{instance_id}")
async def patch_instance(instance_id: UUID, body: InstancePatch, ctx: AuthContext = Depends(MANAGE),
                         db: AsyncSession = Depends(get_db)):
    i = await db.get(CpInstance, instance_id, with_for_update=True)
    if i is None:
        raise NotFound()
    changes = body.model_dump(exclude_unset=True)
    before = {k: getattr(i, k) for k in changes}
    for k, v in changes.items():
        setattr(i, k, v)
    await db.flush()
    await audit(db, ctx, "control_plane.instance_updated", "cp_instance", i.id, before=before, after=changes)
    return _inst(i)


@router.post("/instances/{instance_id}/rotate-token")
async def rotate_token(instance_id: UUID, ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    return {"instance_token": await svc.rotate_token(db, ctx, instance_id)}


class HeartbeatIn(BaseModel):
    version: str = Field(min_length=5, max_length=40)
    health: dict = Field(default_factory=dict)


@router.post("/heartbeat")
async def instance_heartbeat(body: HeartbeatIn, x_rd_instance: str = Header(..., max_length=60),
                             x_rd_instance_token: str = Header(..., max_length=200), db: AsyncSession = Depends(get_db)):
    """Chamado pela INSTÂNCIA (token da instância, não usuário). Fonte da versão atual."""
    i = await svc.heartbeat(db, x_rd_instance, x_rd_instance_token, body.version, body.health)
    return {"ok": True, "instance": i.slug, "current_version": i.current_version}


# ─── Releases ────────────────────────────────────────────────────────────────

class ReleaseIn(BaseModel):
    version: str = Field(min_length=5, max_length=40)
    image_ref: Optional[str] = Field(None, max_length=255)
    changelog: Optional[str] = Field(None, max_length=10000)


class ValidateIn(BaseModel):
    dev_instance_id: UUID


@router.get("/releases")
async def list_releases(ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CpRelease).order_by(CpRelease.created_at.desc()))).scalars()
    return {"items": [_rel(r) for r in rows]}


@router.post("/releases", status_code=201)
async def create_release(body: ReleaseIn, ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    return _rel(await svc.create_release(db, ctx, body.version, body.image_ref, body.changelog))


@router.post("/releases/{release_id}/validate")
async def validate_release(release_id: UUID, body: ValidateIn, ctx: AuthContext = Depends(MANAGE),
                           db: AsyncSession = Depends(get_db)):
    return _rel(await svc.validate_release(db, ctx, release_id, body.dev_instance_id))


@router.post("/releases/{release_id}/publish")
async def publish_release(release_id: UUID, ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    return _rel(await svc.publish_release(db, ctx, release_id))


# ─── Rollouts ────────────────────────────────────────────────────────────────

class RolloutIn(BaseModel):
    release_id: UUID
    target_kind: Literal["UMA", "GRUPO", "TODAS"]
    target_ref: Optional[UUID] = None
    max_concurrency: int = Field(1, ge=1, le=20)
    failure_policy: Literal["PARAR", "CONTINUAR"] = "PARAR"
    executor: Literal["NULO", "SIMULACAO_LOCAL"] = "NULO"
    reason: Optional[str] = Field(None, max_length=500)
    start: bool = True


@router.get("/rollouts")
async def list_rollouts(status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                        ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    cond = [CpRollout.status == status] if status else []
    total = (await db.execute(select(func.count()).select_from(CpRollout).where(*cond))).scalar_one()
    rows = (await db.execute(select(CpRollout).where(*cond).order_by(CpRollout.created_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars()
    return {"items": [_ro(r) for r in rows], "total": total, "page": page, "page_size": page_size}


async def _ro_full(db: AsyncSession, rollout_id: UUID) -> dict:
    r = await db.get(CpRollout, rollout_id)
    if r is None:
        raise NotFound()
    await db.refresh(r)
    targets = list((await db.execute(select(CpRolloutTarget).where(CpRolloutTarget.rollout_id == r.id)
                                     .order_by(CpRolloutTarget.position))).scalars())
    return _ro(r, targets)


@router.post("/rollouts", status_code=201)
async def create_rollout(body: RolloutIn, ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    ro = await svc.create_rollout(db, ctx, release_id=body.release_id, target_kind=body.target_kind,
                                  target_ref=body.target_ref, max_concurrency=body.max_concurrency,
                                  failure_policy=body.failure_policy, executor=body.executor, reason=body.reason)
    if body.start:
        await svc.dispatch(db, ctx, ro.id)
    return await _ro_full(db, ro.id)


@router.get("/rollouts/{rollout_id}")
async def get_rollout(rollout_id: UUID, ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    return await _ro_full(db, rollout_id)


@router.post("/rollouts/{rollout_id}/dispatch")
async def dispatch_rollout(rollout_id: UUID, ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    await svc.dispatch(db, ctx, rollout_id)
    return await _ro_full(db, rollout_id)


class SimStepIn(BaseModel):
    fail_instance_ids: list[UUID] = Field(default_factory=list, max_length=100)


@router.post("/rollouts/{rollout_id}/simulate-step")
async def simulate_step(rollout_id: UUID, body: SimStepIn, ctx: AuthContext = Depends(MANAGE),
                        db: AsyncSession = Depends(get_db)):
    """SIMULAÇÃO LOCAL (rotulada): conclui os alvos em execução e despacha os próximos."""
    await svc.simulate_step(db, ctx, rollout_id, fail_instance_ids=tuple(body.fail_instance_ids))
    return await _ro_full(db, rollout_id)


class RollbackIn(BaseModel):
    max_concurrency: Optional[int] = Field(None, ge=1, le=20)
    reason: str = Field(min_length=3, max_length=500)
    start: bool = True


@router.post("/rollouts/{rollout_id}/rollback", status_code=201)
async def rollback(rollout_id: UUID, body: RollbackIn, ctx: AuthContext = Depends(MANAGE),
                   db: AsyncSession = Depends(get_db)):
    rb = await svc.rollback(db, ctx, rollout_id, max_concurrency=body.max_concurrency, reason=body.reason)
    if body.start:
        await svc.dispatch(db, ctx, rb.id)
    return await _ro_full(db, rb.id)


@router.post("/rollouts/{rollout_id}/{action}")
async def rollout_action(rollout_id: UUID, action: Literal["pause", "resume", "cancel"],
                         ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    ro = await svc.set_rollout_state(db, ctx, rollout_id, action)
    if action == "resume":
        await svc.dispatch(db, ctx, ro.id)
    return await _ro_full(db, rollout_id)


@router.get("/events")
async def list_events(instance_id: Optional[UUID] = None, rollout_id: Optional[UUID] = None,
                      page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                      ctx: AuthContext = Depends(VIEW), db: AsyncSession = Depends(get_db)):
    cond = []
    if instance_id:
        cond.append(CpEvent.instance_id == instance_id)
    if rollout_id:
        cond.append(CpEvent.rollout_id == rollout_id)
    total = (await db.execute(select(func.count()).select_from(CpEvent).where(*cond))).scalar_one()
    rows = (await db.execute(select(CpEvent).where(*cond).order_by(CpEvent.created_at.desc(), CpEvent.id)
                             .offset((page - 1) * page_size).limit(page_size))).scalars()
    return {"items": [{"id": str(e.id), "type": e.type, "instance_id": _s(e.instance_id), "rollout_id": _s(e.rollout_id),
                       "release_id": _s(e.release_id), "data": e.data, "actor_id": _s(e.actor_id),
                       "created_at": _z(e.created_at)} for e in rows],
            "total": total, "page": page, "page_size": page_size}
