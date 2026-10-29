"""Inventário — fronteira de autenticação/autorização (reescrito pelo W-INV na AÇÃO 002).

As rotas legadas ``/inventory/reservations*`` (slots de relógio; ``/book`` sem pagamento;
``DELETE`` que liberava inventário pago; listagem global) foram REMOVIDAS. A intenção dos
testes antigos é mantida sobre as rotas novas: sem credencial → 401; usuário de
organização não lê inventário/reservas de terceiros (403 sem permissão global, 404 fora
do escopo); rotas legadas não existem mais (404).
"""
from __future__ import annotations

import uuid
from datetime import date

from tests.factories import login_as, make_user
from tests.inv_support import mk_org


async def test_inventory_routes_require_auth(client):
    sid = uuid.uuid4()
    for method, url, body in [
        ("GET", "/api/v1/inventory/holds", None),
        ("POST", "/api/v1/inventory/quote", {"screen_ids": [str(sid)], "patterns": [{}]}),
        ("GET", f"/api/v1/inventory/screens/{sid}/state", None),
        ("GET", f"/api/v1/scheduler/screens/{sid}/manifests", None),
        ("GET", "/api/v1/internal-content?establishment_id=" + str(sid), None),
    ]:
        r = await client.request(method, url, json=body)
        assert r.status_code == 401, (url, r.status_code, r.text)
        assert r.json()["error"]["code"] in ("UNAUTHENTICATED", "INVALID_TOKEN")


async def test_legacy_reservation_and_unauthenticated_manifest_routes_are_gone(client, db):
    admin = await make_user(db, role="super_admin", mfa=True)
    s = await login_as(client, admin)
    rid = uuid.uuid4()
    assert (await client.post("/api/v1/inventory/reservations", headers=s.headers, json={
        "screen_id": str(uuid.uuid4()), "slot_date": date.today().isoformat(), "hour": 14}))\
        .status_code in (404, 405)
    assert (await client.post(f"/api/v1/inventory/reservations/{rid}/book", headers=s.headers)).status_code == 404
    assert (await client.delete(f"/api/v1/inventory/reservations/{rid}", headers=s.headers)).status_code == 404
    # o GET de manifesto SEM autenticação (A6 E20/R01 — 500) não existe mais
    assert (await client.get(f"/api/v1/scheduler/manifest/{rid}")).status_code == 404


async def test_org_scoped_user_cannot_use_global_inventory_routes(client, db):
    org = await mk_org(db)
    adv = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id))
    s = await login_as(client, adv)
    # políticas de rotação e relatório de redistribuição: só equipe RD (GLOBAL)
    assert (await client.get("/api/v1/inventory/policies", headers=s.headers)).status_code == 403
    assert (await client.get("/api/v1/inventory/retirements", headers=s.headers)).status_code == 403
    # listagem de reservas é filtrada pelo escopo (não vaza reservas de outras organizações)
    r = await client.get("/api/v1/inventory/holds", headers=s.headers)
    assert r.status_code == 200 and r.json()["total"] == 0
