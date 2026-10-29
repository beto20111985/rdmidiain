"""Sentry opcional: no-op sem DSN e eventos sem credenciais/PII (REQ-OBS / REQ-SEC-002)."""
from __future__ import annotations

import app.core.error_tracking as et
from app.core.logging import MASK


def test_no_dsn_is_noop(monkeypatch):
    monkeypatch.setattr(et.settings, "SENTRY_DSN", "", raising=False)
    monkeypatch.setattr(et, "_initialized", None)
    assert et.init_error_tracking("api") is False
    assert et._initialized is None


def test_with_dsn_initializes_without_pii(monkeypatch):
    calls: dict = {}

    import sentry_sdk

    monkeypatch.setattr(et.settings, "SENTRY_DSN", "https://public@example.invalid/1", raising=False)
    monkeypatch.setattr(et, "_initialized", None)
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.update(kw))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *a, **k: None)
    assert et.init_error_tracking("worker") is True
    assert calls["send_default_pii"] is False
    assert calls["traces_sample_rate"] == 0.0
    assert calls["max_request_body_size"] == "never"
    assert calls["before_send"] is et.scrub_event
    # idempotente
    calls.clear()
    assert et.init_error_tracking("worker") is True
    assert calls == {}


def test_scrub_event_removes_credentials_and_pii():
    event = {
        "request": {
            "url": "http://localhost/ws/player?ticket=SEGREDO1&x=1",
            "query_string": "ticket=SEGREDO2&page=2",
            "cookies": {"rd_refresh": "SEGREDO3"},
            "headers": {"Authorization": "Bearer SEGREDO4", "X-RD-CSRF": "SEGREDO5", "Accept": "json"},
            "data": {"password": "SEGREDO6"},
        },
        "user": {"id": "u1", "email": "a@b.c", "ip_address": "10.0.0.1"},
        "breadcrumbs": {"values": [
            {"message": "GET /api?token=SEGREDO7", "data": {"url": "http://x/y?access_token=SEGREDO8"}},
        ]},
    }
    out = et.scrub_event(event)
    flat = repr(out)
    for n in range(1, 9):
        assert f"SEGREDO{n}" not in flat
    req = out["request"]
    assert "cookies" not in req
    assert req["headers"]["Authorization"] == MASK
    assert req["headers"]["X-RD-CSRF"] == MASK
    assert req["headers"]["Accept"] == "json"
    assert req["data"] == MASK
    assert req["query_string"] == f"ticket={MASK}&page=2"
    assert req["url"].endswith(f"?ticket={MASK}&x=1")
    assert out["user"] == {"id": "u1"}
