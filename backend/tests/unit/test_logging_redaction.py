"""Redação de segredos em query strings nos logs do uvicorn (REQ-SEC-002 / OBS-004)."""
import logging

import pytest

from app.core.logging import (
    MASK,
    QueryRedactionFilter,
    REDACTED_LOGGERS,
    configure_logging,
    redact_query_values,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/ws/player?ticket=AbC123", f"/ws/player?ticket={MASK}"),
        ("/ws/admin?ticket=t1&lang=pt", f"/ws/admin?ticket={MASK}&lang=pt"),
        ("/api/v1/x?page=2&token=s3cr3t&size=10", f"/api/v1/x?page=2&token={MASK}&size=10"),
        ("/cb?code=123456&state=ok", f"/cb?code={MASK}&state=ok"),
        ("/m?access_token=a&refresh_token=b&signature=c&key=d",
         f"/m?access_token={MASK}&refresh_token={MASK}&signature={MASK}&key={MASK}"),
        ("/m?TICKET=X&Device_Token=Y", f"/m?TICKET={MASK}&Device_Token={MASK}"),
        ("/m?creative_code=RD-A7M2P9&page=1", "/m?creative_code=RD-A7M2P9&page=1"),  # não sensível
        ("/m?ticket=", "/m?ticket="),
        ("/sem/query", "/sem/query"),
    ],
)
def test_redact_query_values(raw, expected):
    assert redact_query_values(raw) == expected


def _record(name, msg, args):
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, None)


def test_ws_handshake_line_is_redacted_keeping_route_and_status():
    rec = _record("uvicorn.error", '%s - "WebSocket %s" [accepted]', ("172.31.0.9:51234", "/ws/player?ticket=SEGREDO123"))
    assert QueryRedactionFilter().filter(rec) is True
    msg = rec.getMessage()
    assert "SEGREDO123" not in msg
    assert msg == f'172.31.0.9:51234 - "WebSocket /ws/player?ticket={MASK}" [accepted]'
    rec = _record("uvicorn.error", '%s - "WebSocket %s" %d', ("1.2.3.4:1", "/ws/admin?ticket=XYZ", 403))
    QueryRedactionFilter().filter(rec)
    assert rec.getMessage() == f'1.2.3.4:1 - "WebSocket /ws/admin?ticket={MASK}" 403'


def test_access_log_keeps_method_path_status():
    from uvicorn.logging import AccessFormatter

    rec = _record("uvicorn.access", '%s - "%s %s HTTP/%s" %d',
                  ("10.0.0.1:5555", "GET", "/api/v1/media/1/stream?token=abc&range=1", "1.1", 206))
    QueryRedactionFilter().filter(rec)
    out = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(rec)
    assert out == f'10.0.0.1:5555 - "GET /api/v1/media/1/stream?token={MASK}&range=1 HTTP/1.1" 206 Partial Content'


def test_preformatted_message_is_redacted():
    rec = _record("uvicorn.error", "falha em /ws/player?ticket=ZZZ", ())
    QueryRedactionFilter().filter(rec)
    assert rec.getMessage() == f"falha em /ws/player?ticket={MASK}"


def test_configure_logging_installs_filter_on_uvicorn_loggers_once():
    configure_logging()
    configure_logging()
    for name in REDACTED_LOGGERS:
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, QueryRedactionFilter)]
        assert len(filters) == 1, name


def test_end_to_end_through_uvicorn_error_logger():
    """O registro emitido no logger REAL do uvicorn chega aos handlers já redigido.

    Determinístico: handler próprio anexado ao ``uvicorn.error`` (não depende de propagação
    nem do caplog) e estado do logger restaurado — outros testes/ferramentas podem ter
    desativado ou reconfigurado loggers (ex.: ``fileConfig`` do Alembic).
    """
    import io

    configure_logging()
    lg = logging.getLogger("uvicorn.error")
    saved = (lg.disabled, lg.level)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg.disabled = False
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    try:
        lg.info('%s - "WebSocket %s" [accepted]', "127.0.0.1:9", "/ws/player?ticket=VALOR-SECRETO&lang=pt")
        handler.flush()
    finally:
        lg.removeHandler(handler)
        lg.disabled, lg.level = saved
    out = buf.getvalue()
    assert out.strip() == f'127.0.0.1:9 - "WebSocket /ws/player?ticket={MASK}&lang=pt" [accepted]'
    assert "VALOR-SECRETO" not in out
