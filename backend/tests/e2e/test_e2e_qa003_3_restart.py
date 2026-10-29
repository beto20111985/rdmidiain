"""
AÇÃO 003 / QA (agente G) — REINÍCIO e IDEMPOTÊNCIA do bootstrap nas instâncias DESCARTÁVEIS (pedido itens 31,
38.12–13, 39.18–19).

Pré-condição (operador, ``stack/scripts/003/g/qa003_infra.sh``): depois dos fluxos 1 e 2, ``bootstrap-seq <modo> 10``
(10 execuções sequenciais do ``bootstrap-instance``, cada uma num container one-shot como o serviço ``bootstrap``),
``bootstrap-conc <modo> 10`` (10 processos simultâneos) e ``restart <modo>`` (reinício do backend).

Prova: nada mudou — mesmo modo/versão, 1 linha de modo, mesmos assentos/eventos/Masters/usuários, 1 só auditoria de
bootstrap; login dos Masters segue funcionando; integridade OK; sem divergência (ENV igual ao modo).
"""
from __future__ import annotations

import pytest

from tests.e2e import qa003_support as S

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

KEYS = ("mode", "mode_rows", "mode_version", "seat_events", "seat_holders", "active_masters", "users", "super_admin",
        "invites")


@pytest.mark.parametrize("mode", ["single", "dual"])
async def test_restart_and_repeated_bootstrap_change_nothing(mode):
    st = S.load_state(mode)
    if not st.get("counts_after_flow"):
        pytest.skip(f"fluxo {mode} não executado")
    ev = S.Evidence(f"QA003_3_restart_{mode}")
    before = st["counts_after_flow"]
    now = S.counts(mode)
    ev.check("r.1", "após 10 bootstraps sequenciais + 10 concorrentes + reinício: contagens idênticas",
             all(now[k] == before[k] for k in KEYS), before=before, now=now)
    n_boot = S.audit_count(mode, "admin_master.bootstrapped")
    n_init = S.audit_count(mode, "security.approval_mode_initialized")
    n_div = S.audit_count(mode, "security.env_divergence_detected")
    ev.check("r.2", "auditoria: 1 admin_master.bootstrapped, 1 security.approval_mode_initialized, 0 divergência",
             n_boot == 1 and n_init == 1 and n_div == 0, bootstrapped=n_boot, initialized=n_init, divergence=n_div)
    expected_masters = 1 if mode == "single" else 2
    ev.check("r.3", f"continuam exatamente {expected_masters} Master(s) ativo(s)",
             now["active_masters"] == expected_masters and now["seat_holders"] == expected_masters,
             active=now["active_masters"])
    for label in ("A", "B"):
        if not st.get(label):
            continue
        m = S.Master.from_state(mode, st[label])
        r = await m.login()
        ok = r.status_code == 200 and m.access_token
        am = (await m.get("/security/approval-mode")).json() if ok else {}
        ev.check(f"r.4.{label}", f"login de {label} após o reinício + integridade OK + sem divergência de ENV",
                 bool(ok) and (am.get("integrity") or {}).get("ok") is True
                 and (am.get("env") or {}).get("divergence") is False and am.get("mode") == before["mode"],
                 http=r.status_code, mode=am.get("mode"), version=am.get("mode_version"), integrity=am.get("integrity"),
                 env=am.get("env"))
        st[label] = m.to_state()
        await m.aclose()
    S.save_state(mode, st)
    ev.assert_ok()
