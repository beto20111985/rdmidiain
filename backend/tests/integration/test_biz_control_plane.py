"""W-BIZ — Control plane: releases (DEV → validada → publicada), rollout em grupo com
LIMITE DE CONCORRÊNCIA (inclusive com despachos concorrentes), executor nulo
(dependência externa registrada, nunca sucesso falso), simulação local rotulada,
rollback com histórico, heartbeat autenticado por token da instância e permissões."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import session_factory
from app.models.control_plane import CpEvent, CpRolloutTarget
from app.modules.control_plane import service as cp
from tests.factories import login_as, make_org, make_user


@pytest.fixture(autouse=True)
async def _drain_after_test(clean_state):
    yield
    from app.modules.notifications.service import drain

    await drain()


async def _admin(client, db):
    u = await make_user(db, role="super_admin", mfa=True)
    return await login_as(client, u)


async def _instances(client, s, n, group_id=None, kind="WHITE_LABEL", prefix="wl"):
    out = []
    for k in range(n):
        r = await client.post("/api/v1/control-plane/instances", headers=s.headers, json={
            "slug": f"{prefix}-{k}-{uuid.uuid4().hex[:4]}", "name": f"Instância {k}", "kind": kind,
            "group_id": str(group_id) if group_id else None})
        assert r.status_code == 201, r.text
        out.append(r.json())
    return out


async def _heartbeat(client, inst, version, status="OK"):
    return await client.post("/api/v1/control-plane/heartbeat", json={"version": version, "health": {"status": status}},
                             headers={"X-RD-Instance": inst["slug"], "X-RD-Instance-Token": inst["instance_token"]})


async def _published_release(client, s, version="2.0.0"):
    dev = (await _instances(client, s, 1, kind="DEV", prefix="dev"))[0]
    r = await client.post("/api/v1/control-plane/releases", headers=s.headers, json={"version": version})
    rel = r.json()
    # sem rodar na DEV não valida
    r = await client.post(f"/api/v1/control-plane/releases/{rel['id']}/validate", headers=s.headers,
                          json={"dev_instance_id": dev["id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RELEASE_NOT_RUNNING_ON_DEV"
    # publicar sem validar é proibido
    r = await client.post(f"/api/v1/control-plane/releases/{rel['id']}/publish", headers=s.headers)
    assert r.status_code == 409
    assert (await _heartbeat(client, dev, version)).status_code == 200
    r = await client.post(f"/api/v1/control-plane/releases/{rel['id']}/validate", headers=s.headers,
                          json={"dev_instance_id": dev["id"]})
    assert r.status_code == 200 and r.json()["status"] == "VALIDADA", r.text
    r = await client.post(f"/api/v1/control-plane/releases/{rel['id']}/publish", headers=s.headers)
    assert r.status_code == 200 and r.json()["status"] == "PUBLICADA"
    return rel


async def test_group_rollout_respects_concurrency_limit_and_rollback_history(client, db, mail):
    s = await _admin(client, db)
    g = (await client.post("/api/v1/control-plane/groups", headers=s.headers, json={"name": "Franquias GO"})).json()
    insts = await _instances(client, s, 5, group_id=g["id"])
    for i in insts:
        assert (await _heartbeat(client, i, "1.9.0")).status_code == 200
    rel = await _published_release(client, s, "2.0.0")
    r = await client.post("/api/v1/control-plane/rollouts", headers=s.headers, json={
        "release_id": rel["id"], "target_kind": "GRUPO", "target_ref": g["id"], "max_concurrency": 2,
        "executor": "SIMULACAO_LOCAL", "reason": "Teste de fila"})
    assert r.status_code == 201, r.text
    ro = r.json()
    assert ro["simulated"] is True and ro["status"] == "EM_ANDAMENTO"
    states = [t["status"] for t in ro["targets"]]
    assert states.count("EM_EXECUCAO") == 2 and states.count("NA_FILA") == 3
    assert all(t["from_version"] == "1.9.0" and t["to_version"] == "2.0.0" for t in ro["targets"])
    assert all("SIMULAÇÃO LOCAL" in (t["detail"] or "") for t in ro["targets"] if t["status"] == "EM_EXECUCAO")

    # despachos repetidos e CONCORRENTES nunca passam do limite
    sf = session_factory()

    async def disp():
        async with sf() as sess:
            await cp.dispatch(sess, None, uuid.UUID(ro["id"]))
            await sess.commit()

    await asyncio.gather(*[disp() for _ in range(5)])
    running = (await db.execute(select(func.count()).select_from(CpRolloutTarget).where(
        CpRolloutTarget.rollout_id == uuid.UUID(ro["id"]), CpRolloutTarget.status == "EM_EXECUCAO"))).scalar_one()
    assert running == 2

    max_seen = 2
    for _ in range(5):
        r = await client.post(f"/api/v1/control-plane/rollouts/{ro['id']}/simulate-step", headers=s.headers, json={})
        assert r.status_code == 200, r.text
        cur = [t["status"] for t in r.json()["targets"]].count("EM_EXECUCAO")
        max_seen = max(max_seen, cur)
        if r.json()["status"] != "EM_ANDAMENTO":
            break
    final = r.json()
    assert max_seen <= 2
    assert final["status"] == "SIMULADO"  # rotulado: nenhum deploy real
    assert all(t["status"] == "SIMULADO" for t in final["targets"])
    # versão reportada NÃO muda por simulação (só o heartbeat muda)
    lst = (await client.get(f"/api/v1/control-plane/instances?group_id={g['id']}", headers=s.headers)).json()["items"]
    assert {i["current_version"] for i in lst} == {"1.9.0"}

    # rollback: rollout inverso + histórico
    r = await client.post(f"/api/v1/control-plane/rollouts/{ro['id']}/rollback", headers=s.headers,
                          json={"reason": "Erro na versão 2.0.0", "max_concurrency": 3})
    assert r.status_code == 201, r.text
    rb = r.json()
    assert rb["is_rollback"] is True and rb["rollback_of_id"] == ro["id"]
    assert all(t["from_version"] == "2.0.0" and t["to_version"] == "1.9.0" for t in rb["targets"])
    assert [t["status"] for t in rb["targets"]].count("EM_EXECUCAO") == 3
    ev = (await client.get(f"/api/v1/control-plane/events?rollout_id={ro['id']}", headers=s.headers)).json()
    types = [e["type"] for e in ev["items"]]
    assert "rollback_requested" in types and "rollout_finished" in types and types.count("deploy_simulado") == 5
    # histórico é append-only
    with pytest.raises(DBAPIError):
        await db.execute(text("DELETE FROM cp_events"))
    await db.rollback()


async def test_null_executor_never_reports_success(client, db, mail):
    s = await _admin(client, db)
    insts = await _instances(client, s, 2, kind="RD_PRODUCAO", prefix="rd")
    for i in insts:
        await _heartbeat(client, i, "1.0.0")
    rel = await _published_release(client, s, "1.1.0")
    r = await client.post("/api/v1/control-plane/rollouts", headers=s.headers, json={
        "release_id": rel["id"], "target_kind": "UMA", "target_ref": insts[0]["id"], "max_concurrency": 1})
    assert r.status_code == 201, r.text
    ro = r.json()
    assert ro["executor"] == "NULO" and ro["simulated"] is False
    assert ro["status"] == "BLOQUEADO_DEPENDENCIA"
    t = ro["targets"][0]
    assert t["status"] == "BLOQUEADO_DEPENDENCIA" and "dependência externa" in t["detail"]
    ov = (await client.get("/api/v1/control-plane/overview", headers=s.headers)).json()
    assert ov["execution"]["real_deploy"] is False


async def test_release_rules_conflicts_and_heartbeat_auth(client, db, mail):
    s = await _admin(client, db)
    insts = await _instances(client, s, 1, kind="WHITE_LABEL")
    r = await client.post("/api/v1/control-plane/releases", headers=s.headers, json={"version": "3.0.0"})
    draft = r.json()
    # rascunho não pode ir para instância de cliente
    r = await client.post("/api/v1/control-plane/rollouts", headers=s.headers, json={
        "release_id": draft["id"], "target_kind": "UMA", "target_ref": insts[0]["id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RELEASE_NOT_PUBLISHED"
    # versão inválida
    r = await client.post("/api/v1/control-plane/releases", headers=s.headers, json={"version": "latest"})
    assert r.status_code == 400
    # heartbeat com token errado → 401; correto → versão atualizada
    bad = await client.post("/api/v1/control-plane/heartbeat", json={"version": "1.0.0"},
                            headers={"X-RD-Instance": insts[0]["slug"], "X-RD-Instance-Token": "rdi_falso"})
    assert bad.status_code == 401
    assert (await _heartbeat(client, insts[0], "1.0.0")).status_code == 200
    # dois rollouts ativos na mesma instância → conflito
    rel = await _published_release(client, s, "3.1.0")
    r1 = await client.post("/api/v1/control-plane/rollouts", headers=s.headers, json={
        "release_id": rel["id"], "target_kind": "UMA", "target_ref": insts[0]["id"], "executor": "SIMULACAO_LOCAL"})
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/control-plane/rollouts", headers=s.headers, json={
        "release_id": rel["id"], "target_kind": "UMA", "target_ref": insts[0]["id"], "executor": "SIMULACAO_LOCAL"})
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "ROLLOUT_CONFLICT"
    # rollback de rollout em andamento é recusado
    r = await client.post(f"/api/v1/control-plane/rollouts/{r1.json()['id']}/rollback", headers=s.headers,
                          json={"reason": "cedo demais"})
    assert r.status_code == 409


async def test_permissions(client, db, mail):
    admin = await make_user(db, role="admin", mfa=True)  # admin tem view, não manage
    s = await login_as(client, admin)
    assert (await client.get("/api/v1/control-plane/overview", headers=s.headers)).status_code == 200
    r = await client.post("/api/v1/control-plane/releases", headers=s.headers, json={"version": "9.9.9"})
    assert r.status_code == 403
    org = await make_org(db)
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    sa = await login_as(client, adv)
    assert (await client.get("/api/v1/control-plane/instances", headers=sa.headers)).status_code == 403
    assert (await client.get("/api/v1/control-plane/overview")).status_code == 401
