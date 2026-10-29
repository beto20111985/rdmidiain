"""
AÇÃO 003 / QA (agente G) — BYPASS POR ENV (pedido itens 27 e 41; grill-me #1) nas instâncias DESCARTÁVEIS.

Pré-condição (operador, ``stack/scripts/003/g/qa003_infra.sh``), depois dos fluxos 1–4:
  ``bootstrap <modo> <valor invertido>`` (2×) e ``env-restart <modo> <valor invertido>`` — o ``bootstrap-instance`` roda
  de novo com ``RD_BOOTSTRAP_DUAL_MASTER_APPROVAL`` invertido e a API reinicia com a variável invertida e com
  ``RD_DUAL_MASTER_APPROVAL`` presente.
    single (SINGLE v3 após as transições) → ENV ``true``; dual (DUAL v1) → ENV ``false``.

Prova: o modo NÃO muda (mesma versão, mesma trilha), a divergência é sinalizada (auditoria 1× por valor/versão,
``/security/approval-mode.env.divergence``, variável não suportada sinalizada, métrica ``rd_security_env_divergence``)
e o fluxo continua o do modo efetivo (no DUAL, ``/authorize`` ⇒ 409 ``APPROVAL_MODE_DUAL``).
"""
from __future__ import annotations

import re
from datetime import date

import httpx
import pytest

from tests.e2e import qa003_support as S

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

EXPECTED = {"single": ("SINGLE", 3, "true"), "dual": ("DUAL", 1, "false")}


@pytest.mark.parametrize("mode", ["single", "dual"])
async def test_env_flip_and_restart_do_not_change_mode(mode):
    st = S.load_state(mode)
    exp_mode, exp_version, flipped = EXPECTED[mode]
    if not st.get("A"):
        pytest.skip(f"fluxo {mode} não executado")
    ev = S.Evidence(f"QA003_5_env_bypass_{mode}")
    c = S.counts(mode)
    ev.check("e.1", f"modo efetivo INALTERADO após bootstrap + reinício com ENV={flipped}: {exp_mode} v{exp_version}",
             c["mode"] == exp_mode and c["mode_version"] == exp_version and c["mode_rows"] == exp_version, counts=c)
    div = S.q(mode, "SELECT after_data FROM audit_logs WHERE action = 'security.env_divergence_detected'"
                    " AND after_data->>'kind' = 'approval_mode'")
    ev.check("e.2", "auditoria security.env_divergence_detected exatamente 1× (dedupe por valor/versão)",
             len(div) == 1 and (div[0][0] or {}).get("env_value") == flipped
             and int((div[0][0] or {}).get("mode_version")) == exp_version, n=len(div), audit=div and div[0][0])
    a = S.Master.from_state(mode, st["A"])
    assert (await a.login()).status_code == 200
    am = (await a.get("/security/approval-mode")).json()
    ev.check("e.3", "/security/approval-mode (titular): env.divergence=true e unsupported_var_present=true; "
                    "integridade OK; modo inalterado",
             am.get("mode") == exp_mode and am.get("mode_version") == exp_version
             and (am.get("env") or {}).get("divergence") is True
             and (am.get("env") or {}).get("unsupported_var_present") is True
             and (am.get("integrity") or {}).get("ok") is True, mode=am.get("mode"), version=am.get("mode_version"),
             env=am.get("env"), integrity=am.get("integrity"))
    async with httpx.AsyncClient(base_url=S.INSTANCES[mode]["url"], timeout=30) as c_:
        met = (await c_.get("/metrics")).text
    vals = dict(re.findall(r"^(rd_security_[a-z_]+(?:\{[^}]*\})?) ([0-9.e+-]+)$", met, re.M))
    ev.check("e.4", "métricas: rd_security_env_divergence=1, versão do modo e modo atual corretos",
             float(vals.get("rd_security_env_divergence", -1)) == 1.0
             and float(vals.get("rd_security_approval_mode_version", -1)) == exp_version
             and float(vals.get(f'rd_security_approval_mode{{mode="{exp_mode}"}}', -1)) == 1.0, metrics=vals)
    notif = S.q1(mode, "SELECT count(*) FROM notifications WHERE event_type = 'security.env_divergence'")
    ev.check("e.5", "aviso IN_APP security.env_divergence aos titulares (tela)", notif is not None and notif[0] >= 1,
             n=notif and notif[0])
    r = await a.post("/financial/approvals", json={
        "action_type": "EXPENSE_CREATE", "reason": "QA AÇÃO 003 — operação após troca do ENV",
        "payload": {"description": "QA003 pós-ENV", "category": "qa003", "amount_cents": 321,
                    "competence_date": date.today().isoformat()}})
    req = r.json() if r.status_code == 201 else {}
    ev.check("e.6", f"novo pedido nasce no modo EFETIVO ({exp_mode}), não no do ENV",
             r.status_code == 201 and req.get("approval_mode") == exp_mode, http=r.status_code, mode=req.get("approval_mode"))
    if exp_mode == "DUAL":
        r = await a.post(f"/financial/approvals/{req.get('id')}/authorize",
                         json={"totp_code": await a.totp.code(), "financial_password": a.fin_password,
                               "payload_sha256": req.get("payload_sha256"), "email_code": "000000"})
        ev.check("e.7", "bypass: A tenta autorizar sozinho com ENV=false ⇒ 409 APPROVAL_MODE_DUAL",
                 r.status_code == 409 and S.err_code(r) == "APPROVAL_MODE_DUAL", http=r.status_code, code=S.err_code(r))
        r = await a.post(f"/financial/approvals/{req.get('id')}/approve",
                         json={"totp_code": await a.totp.code(), "financial_password": a.fin_password,
                               "payload_sha256": req.get("payload_sha256"), "email_code": "000000"})
        ev.check("e.8", "bypass: A tenta aprovar o próprio pedido ⇒ 403 SELF_APPROVAL_FORBIDDEN",
                 r.status_code == 403 and S.err_code(r) == "SELF_APPROVAL_FORBIDDEN", http=r.status_code,
                 code=S.err_code(r))
    else:
        ev.check("e.7", "SINGLE segue exigindo autorização com fatores (pedido PENDING, can_authorize)",
                 req.get("status") == "PENDING" and req.get("can_authorize") is True, status=req.get("status"))
    st["A"] = a.to_state()
    S.save_state(mode, st)
    await a.aclose()
    ev.assert_ok()
