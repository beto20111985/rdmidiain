"""
RD Mídia Indoor — Control plane (AÇÃO 002 / W-BIZ).

- Instâncias (com token próprio de heartbeat; hash SHA-256 no banco, valor exibido 1×).
- Releases: RASCUNHO → VALIDADA (a versão precisa estar RODANDO na instância DEV,
  comprovado pelo heartbeat) → PUBLICADA → DESCONTINUADA (ETAPA2 #39: LOCAL → DEV VPS →
  validada → publicada → rollout seletivo).
- Rollouts: UMA / GRUPO / TODAS, versão escolhida, fila com ``max_concurrency``
  (``dispatch`` sob ``SELECT … FOR UPDATE`` do rollout + índice único parcial que
  impede dois deploys simultâneos na mesma instância), política de falha (PARAR/
  CONTINUAR), pausa/retomada/cancelamento, rollback (novo rollout inverso) e
  histórico append-only (``cp_events``). Produção RD vai primeiro, depois as demais.
- Execução: ``DeploymentExecutor`` (NULO = dependência externa; SIMULACAO_LOCAL rotulada).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any, Optional

import structlog
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.crypto import constant_time_equals
from app.core.errors import DomainError, NotFound
from app.core.timeutils import utcnow
from app.models.control_plane import CpEvent, CpInstance, CpInstanceGroup, CpRelease, CpRollout, CpRolloutTarget
from app.modules.control_plane.executor import SIMULATION_DETAIL, executor_for

log = structlog.get_logger("rd.control_plane")

SEMVER_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}(?:-[0-9A-Za-z.-]{1,20})?$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")
HEARTBEAT_STALE = timedelta(minutes=10)
ACTIVE_ROLLOUT = ("PENDENTE", "EM_ANDAMENTO", "PAUSADO")
TERMINAL_TARGET = ("CONCLUIDO", "SIMULADO", "BLOQUEADO_DEPENDENCIA", "FALHOU", "CANCELADO")
KIND_ORDER = {"RD_PRODUCAO": 0, "WHITE_LABEL": 1, "FRANQUIA": 1, "LICENCA": 1, "DEV": 2}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _ev(db: AsyncSession, type_: str, *, actor: Any = None, instance_id=None, release_id=None, rollout_id=None,
              data: Optional[dict] = None) -> None:
    from app.core.audit import sanitize

    db.add(CpEvent(type=type_, instance_id=instance_id, release_id=release_id, rollout_id=rollout_id,
                   data=sanitize(data or {}), actor_id=getattr(actor, "user_id", None)))


def effective_health(i: CpInstance) -> str:
    if i.last_heartbeat_at is None:
        return "SEM_SINAL"
    if utcnow() - i.last_heartbeat_at > HEARTBEAT_STALE:
        return "SEM_SINAL"
    return i.health_status


# ─── Grupos / instâncias ─────────────────────────────────────────────────────

async def create_group(db: AsyncSession, ctx: Any, name: str, description: Optional[str]) -> CpInstanceGroup:
    g = CpInstanceGroup(name=name.strip(), description=description)
    db.add(g)
    await db.flush()
    await audit(db, ctx, "control_plane.group_created", "cp_group", g.id, after={"name": g.name})
    return g


async def create_instance(db: AsyncSession, ctx: Any, data: dict) -> tuple[CpInstance, str]:
    slug = data["slug"]
    if not SLUG_RE.match(slug):
        raise DomainError("INSTANCE_SLUG_INVALID", "Identificador inválido (minúsculas, números e hífen).", status=400)
    if data.get("group_id") and await db.get(CpInstanceGroup, data["group_id"]) is None:
        raise NotFound()
    token = "rdi_" + secrets.token_urlsafe(32)
    i = CpInstance(slug=slug, name=data["name"].strip(), kind=data["kind"], base_url=data.get("base_url"),
                   group_id=data.get("group_id"), status="ATIVA", health_status="DESCONHECIDO", token_hash=_hash(token),
                   license=data.get("license"), notes=data.get("notes"))
    db.add(i)
    await db.flush()
    await _ev(db, "instance_registered", actor=ctx, instance_id=i.id, data={"slug": slug, "kind": i.kind})
    await audit(db, ctx, "control_plane.instance_created", "cp_instance", i.id, after={"slug": slug, "kind": i.kind})
    return i, token


async def rotate_token(db: AsyncSession, ctx: Any, instance_id: uuid.UUID) -> str:
    i = await db.get(CpInstance, instance_id, with_for_update=True)
    if i is None:
        raise NotFound()
    token = "rdi_" + secrets.token_urlsafe(32)
    i.token_hash = _hash(token)
    await _ev(db, "instance_token_rotated", actor=ctx, instance_id=i.id)
    await audit(db, ctx, "control_plane.instance_token_rotated", "cp_instance", i.id)
    return token


async def heartbeat(db: AsyncSession, slug: str, token: str, version: str, health: dict) -> CpInstance:
    """Heartbeat da instância (autenticado pelo token da instância). Fonte da versão atual."""
    i = (await db.execute(select(CpInstance).where(CpInstance.slug == slug).with_for_update())).scalar_one_or_none()
    if i is None or not i.token_hash or not constant_time_equals(i.token_hash, _hash(token or "")):
        from app.core.errors import Unauthorized

        raise Unauthorized("INSTANCE_UNAUTHORIZED", "Instância não autenticada.")
    if not SEMVER_RE.match(version or ""):
        raise DomainError("VERSION_INVALID", "Versão inválida.", status=400)
    previous = i.current_version
    i.current_version = version
    i.last_heartbeat_at = utcnow()
    status = str((health or {}).get("status", "OK")).upper()
    i.health_status = status if status in ("OK", "DEGRADADO", "FORA_DO_AR") else "DEGRADADO"
    i.health = {k: v for k, v in (health or {}).items() if isinstance(k, str)}
    if previous != version:
        await _ev(db, "instance_version_reported", instance_id=i.id, data={"from": previous, "to": version})
    # confirmação de deploy real: alvo em execução para esta versão é concluído pelo heartbeat
    running = (await db.execute(select(CpRolloutTarget).where(
        CpRolloutTarget.instance_id == i.id, CpRolloutTarget.status == "EM_EXECUCAO",
        CpRolloutTarget.to_version == version))).scalars().all()
    for t in running:
        rollout = await db.get(CpRollout, t.rollout_id)
        if rollout is not None and rollout.executor != "SIMULACAO_LOCAL":
            await _finish_target(db, None, t, "CONCLUIDO", "Confirmado pelo heartbeat da instância")
            i.last_deployed_version = version
            await _maybe_finish(db, None, rollout)
    return i


# ─── Releases ────────────────────────────────────────────────────────────────

async def create_release(db: AsyncSession, ctx: Any, version: str, image_ref: Optional[str], changelog: Optional[str]) -> CpRelease:
    if not SEMVER_RE.match(version or ""):
        raise DomainError("VERSION_INVALID", "Versão inválida (use semver, ex.: 1.4.0).", status=400)
    if (await db.execute(select(CpRelease.id).where(CpRelease.version == version))).first():
        raise DomainError("RELEASE_EXISTS", "Versão já cadastrada.", status=409)
    r = CpRelease(version=version, status="RASCUNHO", image_ref=image_ref, changelog=changelog,
                  created_by=getattr(ctx, "user_id", None))
    db.add(r)
    await db.flush()
    await _ev(db, "release_created", actor=ctx, release_id=r.id, data={"version": version})
    await audit(db, ctx, "control_plane.release_created", "cp_release", r.id, after={"version": version})
    return r


async def validate_release(db: AsyncSession, ctx: Any, release_id: uuid.UUID, dev_instance_id: uuid.UUID) -> CpRelease:
    r = await db.get(CpRelease, release_id, with_for_update=True)
    if r is None:
        raise NotFound()
    if r.status != "RASCUNHO":
        raise DomainError("RELEASE_INVALID_TRANSITION", "Somente rascunhos podem ser validados.", status=409)
    dev = await db.get(CpInstance, dev_instance_id)
    if dev is None:
        raise NotFound()
    if dev.kind != "DEV":
        raise DomainError("RELEASE_VALIDATION_NOT_DEV", "A validação deve ocorrer na instância DEV.", status=409)
    if dev.current_version != r.version or effective_health(dev) != "OK":
        raise DomainError(
            "RELEASE_NOT_RUNNING_ON_DEV",
            "A instância DEV precisa estar rodando esta versão e saudável (comprovado pelo heartbeat).",
            status=409, details={"dev_version": dev.current_version, "dev_health": effective_health(dev)},
        )
    r.status, r.validated_on_instance_id, r.validated_by, r.validated_at = "VALIDADA", dev.id, ctx.user_id, utcnow()
    await _ev(db, "release_validated", actor=ctx, release_id=r.id, instance_id=dev.id, data={"version": r.version})
    await audit(db, ctx, "control_plane.release_validated", "cp_release", r.id, after={"dev_instance": dev.slug})
    return r


async def publish_release(db: AsyncSession, ctx: Any, release_id: uuid.UUID) -> CpRelease:
    r = await db.get(CpRelease, release_id, with_for_update=True)
    if r is None:
        raise NotFound()
    if r.status != "VALIDADA":
        raise DomainError("RELEASE_INVALID_TRANSITION", "Somente versões validadas na DEV podem ser publicadas.",
                          status=409, details={"status": r.status})
    r.status, r.published_by, r.published_at = "PUBLICADA", ctx.user_id, utcnow()
    await _ev(db, "release_published", actor=ctx, release_id=r.id, data={"version": r.version})
    await audit(db, ctx, "control_plane.release_published", "cp_release", r.id, after={"version": r.version})
    return r


# ─── Rollouts ────────────────────────────────────────────────────────────────

async def _targets_for(db: AsyncSession, target_kind: str, target_ref: Optional[uuid.UUID], release: CpRelease) -> list[CpInstance]:
    q = select(CpInstance).where(CpInstance.status == "ATIVA")
    if target_kind == "UMA":
        q = q.where(CpInstance.id == target_ref)
    elif target_kind == "GRUPO":
        if target_ref is None or await db.get(CpInstanceGroup, target_ref) is None:
            raise NotFound()
        q = q.where(CpInstance.group_id == target_ref)
    else:  # TODAS — a DEV recebe versões pelo fluxo próprio, não pelo "todas"
        q = q.where(CpInstance.kind != "DEV")
    rows = list((await db.execute(q)).scalars())
    if target_kind == "UMA" and not rows:
        raise NotFound()
    if release.status != "PUBLICADA":
        # rascunho/validada só pode ir para a DEV (validação), nunca para produção/clientes
        bad = [i.slug for i in rows if i.kind != "DEV"]
        if bad or release.status == "DESCONTINUADA":
            raise DomainError("RELEASE_NOT_PUBLISHED",
                              "Somente versões publicadas podem ser aplicadas fora da instância DEV.", status=409,
                              details={"instances": bad[:20]})
    rows.sort(key=lambda i: (KIND_ORDER.get(i.kind, 9), i.slug))
    return rows


async def create_rollout(db: AsyncSession, ctx: Any, *, release_id: uuid.UUID, target_kind: str,
                         target_ref: Optional[uuid.UUID], max_concurrency: int, failure_policy: str = "PARAR",
                         executor: str = "NULO", reason: Optional[str] = None) -> CpRollout:
    release = await db.get(CpRelease, release_id)
    if release is None:
        raise NotFound()
    executor_for(executor)  # valida (simulação proibida em produção)
    # serializa a criação de rollouts (verificação de conflito consistente)
    from sqlalchemy import text

    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('rd.cp.rollout_create'))"))
    instances = await _targets_for(db, target_kind, target_ref, release)
    if not instances:
        raise DomainError("ROLLOUT_EMPTY", "Nenhuma instância ativa no alvo.", status=400)
    busy = (await db.execute(
        select(CpRolloutTarget.instance_id).join(CpRollout, CpRollout.id == CpRolloutTarget.rollout_id)
        .where(CpRollout.status.in_(ACTIVE_ROLLOUT), CpRolloutTarget.instance_id.in_([i.id for i in instances]),
               CpRolloutTarget.status.in_(("NA_FILA", "EM_EXECUCAO")))
    )).scalars().all()
    if busy:
        raise DomainError("ROLLOUT_CONFLICT", "Há instâncias deste alvo em outro rollout ativo.", status=409,
                          details={"instance_ids": [str(x) for x in set(busy)]})
    ro = CpRollout(release_id=release.id, target_kind=target_kind, target_ref=target_ref, max_concurrency=max_concurrency,
                   failure_policy=failure_policy, executor=executor, status="PENDENTE", reason=reason,
                   created_by=getattr(ctx, "user_id", None))
    db.add(ro)
    await db.flush()
    for pos, i in enumerate(instances):
        if i.current_version == release.version:
            continue  # já está na versão (reportada pelo heartbeat)
        db.add(CpRolloutTarget(rollout_id=ro.id, instance_id=i.id, position=pos, from_version=i.current_version,
                               to_version=release.version, status="NA_FILA"))
    await db.flush()
    await _ev(db, "rollout_created", actor=ctx, rollout_id=ro.id, release_id=release.id,
              data={"target_kind": target_kind, "max_concurrency": max_concurrency, "executor": executor,
                    "instances": len(instances)})
    await audit(db, ctx, "control_plane.rollout_created", "cp_rollout", ro.id,
                after={"version": release.version, "target_kind": target_kind, "executor": executor})
    await _maybe_finish(db, ctx, ro)
    return ro


async def dispatch(db: AsyncSession, ctx: Any, rollout_id: uuid.UUID) -> list[CpRolloutTarget]:
    """Inicia os próximos alvos respeitando ``max_concurrency`` (EM_EXECUCAO ≤ limite)."""
    ro = (await db.execute(select(CpRollout).where(CpRollout.id == rollout_id).with_for_update())).scalar_one_or_none()
    if ro is None:
        raise NotFound()
    if ro.status not in ("PENDENTE", "EM_ANDAMENTO"):
        return []
    if ro.failure_policy == "PARAR" and await _count(db, ro.id, ("FALHOU",)):
        ro.status, ro.finished_at = "FALHOU", utcnow()
        return []
    if ro.status == "PENDENTE":
        ro.status, ro.started_at = "EM_ANDAMENTO", utcnow()
        await _ev(db, "rollout_started", actor=ctx, rollout_id=ro.id)
    release = await db.get(CpRelease, ro.release_id)
    ex = executor_for(ro.executor)
    started: list[CpRolloutTarget] = []
    while True:
        inflight = await _count(db, ro.id, ("EM_EXECUCAO",))
        slots = ro.max_concurrency - inflight
        if slots <= 0:
            break
        nxt = list((await db.execute(
            select(CpRolloutTarget).where(CpRolloutTarget.rollout_id == ro.id, CpRolloutTarget.status == "NA_FILA")
            .order_by(CpRolloutTarget.position).limit(slots).with_for_update(skip_locked=True)
        )).scalars())
        if not nxt:
            break
        progressed = False
        for t in nxt:
            other = (await db.execute(select(CpRolloutTarget.id).where(
                CpRolloutTarget.instance_id == t.instance_id, CpRolloutTarget.status == "EM_EXECUCAO"))).first()
            if other:
                continue  # instância ocupada por outro deploy — fica na fila
            inst = await db.get(CpInstance, t.instance_id)
            t.status, t.attempt, t.started_at = "EM_EXECUCAO", t.attempt + 1, utcnow()
            if t.from_version is None and inst is not None:
                t.from_version = inst.current_version
            await db.flush()
            res = await ex.start(inst, release, t)
            t.detail = res.detail
            if res.status in ("BLOQUEADO_DEPENDENCIA", "FALHOU"):
                await _finish_target(db, ctx, t, res.status, res.detail)
            else:
                await _ev(db, "deploy_started", actor=ctx, rollout_id=ro.id, instance_id=t.instance_id,
                          data={"to_version": t.to_version, "executor": ex.name, "simulated": ex.simulated})
            started.append(t)
            progressed = True
        await db.flush()
        if not progressed:
            break
        if ro.failure_policy == "PARAR" and await _count(db, ro.id, ("FALHOU",)):
            break
    await _maybe_finish(db, ctx, ro)
    return started


async def _count(db: AsyncSession, rollout_id: uuid.UUID, statuses: tuple) -> int:
    return int((await db.execute(select(func.count()).select_from(CpRolloutTarget).where(
        CpRolloutTarget.rollout_id == rollout_id, CpRolloutTarget.status.in_(statuses)))).scalar_one())


async def _finish_target(db: AsyncSession, ctx: Any, t: CpRolloutTarget, status: str, detail: Optional[str]) -> None:
    t.status, t.detail, t.finished_at = status, (detail or t.detail), utcnow()
    await _ev(db, f"deploy_{status.lower()}", actor=ctx, rollout_id=t.rollout_id, instance_id=t.instance_id,
              data={"from_version": t.from_version, "to_version": t.to_version, "detail": t.detail})


async def _maybe_finish(db: AsyncSession, ctx: Any, ro: CpRollout) -> None:
    await db.flush()
    pending = await _count(db, ro.id, ("NA_FILA", "EM_EXECUCAO"))
    if pending and ro.status != "CANCELADO":
        if ro.failure_policy == "PARAR" and await _count(db, ro.id, ("FALHOU",)) and not await _count(db, ro.id, ("EM_EXECUCAO",)):
            ro.status, ro.finished_at = "FALHOU", utcnow()
        else:
            return
    rows = dict((await db.execute(select(CpRolloutTarget.status, func.count()).where(
        CpRolloutTarget.rollout_id == ro.id).group_by(CpRolloutTarget.status))).all())
    if ro.status not in ("CANCELADO", "FALHOU"):
        if rows.get("FALHOU"):
            final = "FALHOU"
        elif rows.get("BLOQUEADO_DEPENDENCIA"):
            final = "BLOQUEADO_DEPENDENCIA"
        elif rows.get("SIMULADO"):
            final = "SIMULADO"
        else:
            final = "CONCLUIDO"
        ro.status = final
    if ro.finished_at is None:
        ro.finished_at = utcnow()
    await _ev(db, "rollout_finished", actor=ctx, rollout_id=ro.id,
              data={"status": ro.status, "targets": {k: int(v) for k, v in rows.items()}})
    try:
        from app.modules.notifications.service import StaffRecipients, notify

        release = await db.get(CpRelease, ro.release_id)
        await notify(db, [StaffRecipients("control_plane.manage")], "control_plane.rollout_finished",
                     {"version": release.version if release else "?", "status": ro.status, "rollout_id": str(ro.id)},
                     dedupe_key=f"cp.rollout_finished:{ro.id}", channels=["IN_APP"])
    except Exception as exc:  # notificação não pode desfazer o fechamento do rollout
        log.warning("cp_notify_failed", error_type=type(exc).__name__)


async def simulate_step(db: AsyncSession, ctx: Any, rollout_id: uuid.UUID, *, fail_instance_ids: tuple = ()) -> dict:
    """SIMULAÇÃO LOCAL: conclui os alvos em execução (ou falha os indicados) e despacha os próximos."""
    ro = (await db.execute(select(CpRollout).where(CpRollout.id == rollout_id).with_for_update())).scalar_one_or_none()
    if ro is None:
        raise NotFound()
    if ro.executor != "SIMULACAO_LOCAL":
        raise DomainError("ROLLOUT_NOT_SIMULATED", "Este rollout não é uma simulação local.", status=409)
    running = list((await db.execute(select(CpRolloutTarget).where(
        CpRolloutTarget.rollout_id == ro.id, CpRolloutTarget.status == "EM_EXECUCAO").with_for_update())).scalars())
    done = []
    for t in running:
        if t.instance_id in fail_instance_ids:
            await _finish_target(db, ctx, t, "FALHOU", "SIMULAÇÃO LOCAL — falha simulada")
        else:
            await _finish_target(db, ctx, t, "SIMULADO", SIMULATION_DETAIL)
        done.append(t)
    started = await dispatch(db, ctx, ro.id)
    return {"completed": len(done), "started": len(started), "status": ro.status}


async def set_rollout_state(db: AsyncSession, ctx: Any, rollout_id: uuid.UUID, action: str) -> CpRollout:
    ro = (await db.execute(select(CpRollout).where(CpRollout.id == rollout_id).with_for_update())).scalar_one_or_none()
    if ro is None:
        raise NotFound()
    before = ro.status
    if action == "pause" and ro.status in ("PENDENTE", "EM_ANDAMENTO"):
        ro.status = "PAUSADO"
    elif action == "resume" and ro.status == "PAUSADO":
        ro.status = "EM_ANDAMENTO"
    elif action == "cancel" and ro.status in ACTIVE_ROLLOUT:
        await db.execute(update(CpRolloutTarget).where(CpRolloutTarget.rollout_id == ro.id,
                                                       CpRolloutTarget.status == "NA_FILA")
                         .values(status="CANCELADO", finished_at=utcnow(), detail="Cancelado antes da execução"))
        ro.status = "CANCELADO"
        ro.finished_at = utcnow()
    else:
        raise DomainError("ROLLOUT_INVALID_TRANSITION", "Ação inválida para o estado atual.", status=409,
                          details={"status": ro.status, "action": action})
    await _ev(db, f"rollout_{action}", actor=ctx, rollout_id=ro.id, data={"from": before, "to": ro.status})
    await audit(db, ctx, f"control_plane.rollout_{action}", "cp_rollout", ro.id, before={"status": before},
                after={"status": ro.status})
    return ro


async def rollback(db: AsyncSession, ctx: Any, rollout_id: uuid.UUID, *, max_concurrency: Optional[int] = None,
                   reason: Optional[str] = None) -> CpRollout:
    """Cria um rollout INVERSO para as instâncias alteradas pelo rollout informado."""
    orig = (await db.execute(select(CpRollout).where(CpRollout.id == rollout_id).with_for_update())).scalar_one_or_none()
    if orig is None:
        raise NotFound()
    if orig.status in ("PENDENTE", "EM_ANDAMENTO"):
        raise DomainError("ROLLOUT_STILL_RUNNING", "Pause ou aguarde o rollout antes do rollback.", status=409)
    changed = list((await db.execute(select(CpRolloutTarget).where(
        CpRolloutTarget.rollout_id == orig.id, CpRolloutTarget.status.in_(("CONCLUIDO", "SIMULADO", "EM_EXECUCAO")),
        CpRolloutTarget.from_version.is_not(None)).order_by(CpRolloutTarget.position))).scalars())
    if not changed:
        raise DomainError("ROLLBACK_NOTHING", "Nenhuma instância foi alterada por este rollout.", status=409)
    busy = (await db.execute(
        select(CpRolloutTarget.instance_id).join(CpRollout, CpRollout.id == CpRolloutTarget.rollout_id)
        .where(CpRollout.status.in_(ACTIVE_ROLLOUT), CpRollout.id != orig.id,
               CpRolloutTarget.instance_id.in_([t.instance_id for t in changed]),
               CpRolloutTarget.status.in_(("NA_FILA", "EM_EXECUCAO"))))).scalars().all()
    if busy:
        raise DomainError("ROLLOUT_CONFLICT", "Há instâncias em outro rollout ativo.", status=409)
    rb = CpRollout(release_id=orig.release_id, target_kind=orig.target_kind, target_ref=orig.target_ref,
                   max_concurrency=max_concurrency or orig.max_concurrency, failure_policy="PARAR",
                   executor=orig.executor, status="PENDENTE", is_rollback=True, rollback_of_id=orig.id,
                   reason=reason, created_by=getattr(ctx, "user_id", None))
    db.add(rb)
    await db.flush()
    for pos, t in enumerate(changed):
        db.add(CpRolloutTarget(rollout_id=rb.id, instance_id=t.instance_id, position=pos, from_version=t.to_version,
                               to_version=t.from_version, status="NA_FILA"))
    await db.flush()
    await _ev(db, "rollback_requested", actor=ctx, rollout_id=orig.id, data={"rollback_rollout_id": str(rb.id),
                                                                          "instances": len(changed), "reason": reason})
    await _ev(db, "rollout_created", actor=ctx, rollout_id=rb.id, release_id=orig.release_id,
              data={"rollback_of": str(orig.id), "instances": len(changed)})
    await audit(db, ctx, "control_plane.rollback_created", "cp_rollout", rb.id, after={"rollback_of": orig.id},
                reason=reason)
    return rb


async def overview(db: AsyncSession) -> dict:
    inst = list((await db.execute(select(CpInstance))).scalars())
    by_version: dict = {}
    by_health: dict = {}
    for i in inst:
        by_version[i.current_version or "desconhecida"] = by_version.get(i.current_version or "desconhecida", 0) + 1
        h = effective_health(i)
        by_health[h] = by_health.get(h, 0) + 1
    active = int((await db.execute(select(func.count()).select_from(CpRollout).where(
        CpRollout.status.in_(ACTIVE_ROLLOUT)))).scalar_one())
    latest = (await db.execute(select(CpRelease).where(CpRelease.status == "PUBLICADA")
                               .order_by(CpRelease.published_at.desc()).limit(1))).scalar_one_or_none()
    return {
        "instances": len(inst), "by_version": by_version, "by_health": by_health, "active_rollouts": active,
        "latest_published_version": latest.version if latest else None,
        "outdated": sum(1 for i in inst if latest and i.current_version != latest.version and i.kind != "DEV"),
        "execution": {"real_deploy": False,
                      "detail": "Execução real (Portainer) é dependência externa; rollouts usam executor NULO "
                                "ou SIMULACAO_LOCAL rotulada."},
    }
