"""Redação de query sensível no log REAL do uvicorn (REQ-SEC-002 / OBS-004).

Sobe um uvicorn de verdade (subprocesso; com e sem ``--reload``), faz handshakes WebSocket
com ``?ticket=`` — rejeitado (403) e aceito (``[accepted]``) — e captura stdout/stderr:
o valor do ticket nunca aparece; a linha mantém rota, demais parâmetros e status.

Determinismo (rodando junto da suíte completa, máquina carregada):
- porta atribuída pelo SO (``--port 0``) e lida da linha "Uvicorn running on" — sem corrida
  entre escolher uma porta livre e o uvicorn ocupá-la;
- prontidão = ``GET /health`` respondendo 200 (não sleep fixo);
- espera ativa pelas 3 linhas de handshake no log (não sleep fixo);
- timeouts folgados; as asserções só rodam após o processo terminar e o stdout/stderr ser
  lido até o EOF.
"""
import asyncio
import os
import re
import secrets
import subprocess
import sys
import threading
import time

import httpx
import pytest

from app.core.config import settings

pytestmark = pytest.mark.slow

STARTUP_TIMEOUT_S = 180
LINES_TIMEOUT_S = 60
HANDSHAKE_TIMEOUT_S = 30

PROBE_APP = "\n".join([
    "from app.main import app",
    "from fastapi import WebSocket",
    "@app.websocket('/ws/w1probe')",
    "async def _probe(ws: WebSocket):",
    "    await ws.accept()",
    "    await ws.close()",
    "",
])
RUNNING_RE = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")


async def _handshake(port: int, path: str) -> str:
    import websockets

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}{path}", open_timeout=HANDSHAKE_TIMEOUT_S,
                                      close_timeout=10) as ws:
            await ws.close()
            return "accepted"
    except Exception as exc:
        return type(exc).__name__


class _Output:
    def __init__(self, proc: subprocess.Popen) -> None:
        self.lines: list[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._read, args=(proc,), daemon=True)
        self._thread.start()

    def _read(self, proc: subprocess.Popen) -> None:
        for ln in proc.stdout:  # até EOF (todos os processos que herdaram o pipe terminaram)
            with self._lock:
                self.lines.append(ln.rstrip("\n"))

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)

    def wait_for(self, pred, timeout: float, proc: subprocess.Popen):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for ln in self.snapshot():
                m = pred(ln)
                if m:
                    return m
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        return None

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


def _wait_http_ready(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
            if r.status_code == 200:
                return
            last = r.status_code
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.2)
    raise AssertionError(f"uvicorn não ficou pronto em {timeout}s (último: {last})")


def _run(tmp_path, extra_args: list[str], secret: str) -> tuple[list[str], list[str]]:
    (tmp_path / "w1probe_app.py").write_text(PROBE_APP, encoding="utf-8")
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        # banco/Redis de TESTE (nunca o principal)
        "DATABASE_URL": settings.DATABASE_URL,
        "REDIS_URL": settings.REDIS_URL,
        "REDIS_KEY_PREFIX": settings.REDIS_KEY_PREFIX,
    }
    cmd = [sys.executable, "-m", "uvicorn", "w1probe_app:app", "--app-dir", str(tmp_path), "--host", "127.0.0.1",
           "--port", "0", "--log-level", "info", *extra_args]
    proc = subprocess.Popen(cmd, cwd=backend_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=env)
    out = _Output(proc)
    try:
        m = out.wait_for(RUNNING_RE.search, STARTUP_TIMEOUT_S, proc)
        assert m, "porta não anunciada:\n" + "\n".join(out.snapshot()[-40:])
        port = int(m.group(1))
        assert out.wait_for(lambda ln: "Application startup complete" in ln, STARTUP_TIMEOUT_S, proc), \
            "startup não concluiu:\n" + "\n".join(out.snapshot()[-40:])
        _wait_http_ready(port, STARTUP_TIMEOUT_S)
        results = [
            asyncio.run(_handshake(port, f"/ws/{p}?ticket={secret}&lang=pt"))
            for p in ("player", "admin", "w1probe")
        ]
        done = out.wait_for(
            lambda _ln: sum('"WebSocket /ws/' in x for x in out.snapshot()) >= 3 or None, LINES_TIMEOUT_S, proc
        )
        assert done, "linhas de handshake não apareceram:\n" + "\n".join(out.snapshot()[-40:])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
    assert out.join(60), "stdout/stderr não foi lido até o fim"
    return results, out.snapshot()


@pytest.mark.parametrize("extra_args", [[], ["--reload", "--reload-dir", "app"]], ids=["sem-reload", "reload"])
def test_ws_handshake_ticket_is_masked_in_real_uvicorn_output(clean_state, tmp_path, extra_args):
    secret = "W1PROBE" + secrets.token_hex(12)
    results, lines = _run(tmp_path, extra_args, secret)
    assert results[2] == "accepted" and results[0] != "accepted" and results[1] != "accepted", results
    out = "\n".join(lines)
    assert secret not in out, "valor do ticket vazou no log do uvicorn"
    ws_lines = [ln for ln in lines if '"WebSocket /ws/' in ln]
    assert len(ws_lines) == 3, ws_lines
    assert all("ticket=[MASCARADO]&lang=pt" in ln for ln in ws_lines)
    assert any('"WebSocket /ws/w1probe?ticket=[MASCARADO]&lang=pt" [accepted]' in ln for ln in ws_lines)
    assert any('"WebSocket /ws/player?ticket=[MASCARADO]&lang=pt" 403' in ln for ln in ws_lines)
