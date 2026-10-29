"""Health e métricas (reescrito na AÇÃO 002 — mesmo objetivo do arquivo original)."""


async def test_health_check_returns_healthy(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy" and "version" in r.json()


async def test_full_health_checks_db_and_redis_without_leaking_errors(client):
    r = await client.get("/health/full")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy", "services": {"app": "healthy", "db": "healthy", "redis": "healthy"}}


async def test_metrics_endpoint_returns_prometheus_format(client):
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "rd_ws_player_connections" in r.text


async def test_request_id_is_propagated(client):
    r = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    r = await client.get("/health", headers={"X-Request-ID": "invalido com espaco;drop"})
    assert r.headers["x-request-id"] != "invalido com espaco;drop" and len(r.headers["x-request-id"]) == 32
