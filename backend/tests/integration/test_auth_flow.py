"""Fluxo de autenticação básico (reescrito na AÇÃO 002: asserções exatas, sem 'in (401, 422, 404)')."""


async def test_login_with_invalid_credentials_returns_401(client):
    r = await client.post("/api/v1/auth/login", json={"login": "nonexistent@example.com", "password": "wrongpassword"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_protected_endpoint_without_token_returns_401(client):
    r = await client.get("/api/v1/campaigns")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_protected_endpoint_with_invalid_token_returns_401(client):
    r = await client.get("/api/v1/campaigns", headers={"Authorization": "Bearer not.a.valid.jwt.token"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"
