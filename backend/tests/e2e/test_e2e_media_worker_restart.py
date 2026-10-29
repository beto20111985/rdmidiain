"""
C (P002 item 50, parte "worker reinicia com tarefas em andamento") — reprocessamento de mídia após restart do
worker DURANTE o processamento (reteste do BUG-QA-01). SÓ roda com ``RD_E2E_FAILURE=1``; o restart é feito pelo
orquestrador (``stack/scripts/002/qa/c_media_restart.sh``) quando este teste grava o marcador MEDIA_PROCESSING.
Esperado: a mídia volta a ser processada e fica READY em poucos minutos (sem esperar 1 h), uma única mídia/arquivo final.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from tests.e2e import live_support as L
from tests.e2e.test_e2e_full_flow import _make_video

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

ENABLED = os.environ.get("RD_E2E_FAILURE") == "1"
READY_LIMIT_S = int(os.environ.get("RD_E2E_MEDIA_READY_LIMIT_S", "600"))


@pytest.mark.skipif(not ENABLED, reason="seção C: defina RD_E2E_FAILURE=1 (somente com SLOT DE FALHA autorizado)")
async def test_media_reprocessed_after_worker_restart():
    tag = os.environ.get("RD_E2E_FAILURE_TAG") or L.run_tag()
    d = Path(L.REPORT_DIR or "/tmp")
    log = L.StepLog("C_media_worker_restart")
    adm = await L.login("admin")
    adv = None
    try:
        t0 = time.monotonic()
        org, creds = await L.new_advertiser(adm, tag, city="Macapá", uf="AP")
        adv = await L.login("advertiser", creds=creds)
        with tempfile.TemporaryDirectory() as td:
            vp = os.path.join(td, "qa_c_video.mp4")
            _make_video(vp, tag)
            data = open(vp, "rb").read()
        r = await adv.post("/api/v1/media/upload", data={"org_id": org, "name": f"{L.PREFIX}videoC_{tag}"},
                           files={"file": ("qa_c_video.mp4", data, "video/mp4")}, timeout=300)
        assert r.status_code in (200, 202), r.text
        mid = r.json()["id"]
        log.created.update(advertiser_org=org, media=mid)

        async def _processing():
            row = L.q1("select status, progress_pct from media_files where id=:i", i=mid)
            return row if row[0] == "PROCESSING" and (row[1] or 0) > 0 else None
        row = await L.wait_until(_processing, timeout=180, interval=0.5, what="mídia em PROCESSING com progresso")
        (d / f"C_{tag}_MEDIA_PROCESSING").write_text("{}", encoding="utf-8")
        log.record("M.0", "vídeo em processamento no worker (marcador para o restart)", True, t0,
                   {"media": mid, "status": row[0], "progress": row[1]})

        t1 = time.monotonic()

        async def _final():
            st = L.q1("select status, status_code, attempts, progress_pct, lease_until from media_files where id=:i", i=mid)
            return st if st[0] in ("READY", "REJECTED", "FAILED") else None
        try:
            st = await L.wait_until(_final, timeout=READY_LIMIT_S, interval=3, what="mídia READY após o restart")
            ok = st[0] == "READY"
        except L.E2EError:
            st = L.q1("select status, status_code, attempts, progress_pct, lease_until from media_files where id=:i", i=mid)
            ok = False
        aud = L.q("select action, origin from audit_logs where entity_id=:i order by created_at", i=mid)
        log.record("M.1", "mídia reprocessada e READY após restart do worker (sem esperar 1 h)", ok, t1,
                   {"final": [str(x) for x in st], "wait_s": round(time.monotonic() - t1, 1), "audit": aud})
    finally:
        print(f"[C-media] relatório: {log.dump()}", flush=True)
        (d / f"C_{tag}_MEDIA_FINISHED").write_text("{}", encoding="utf-8")
        for c in (adm, adv):
            if c is not None:
                await c.aclose()
    failed = [x for x in log.steps if x["result"] != "PASS"]
    assert not failed, failed
