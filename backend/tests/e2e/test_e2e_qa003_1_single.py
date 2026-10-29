"""
AÇÃO 003 / QA (agente G) — E2E MODO ÚNICO (``RD_BOOTSTRAP_DUAL_MASTER_APPROVAL=false``), pedido itens 38 e 54.

Instância DESCARTÁVEL ``rd-qa003-single`` (banco ``rdqa003_single``) recém-criada pelo bootstrap. Prova:
bootstrap (1 modo SINGLE v1, exatamente 1 Master, troca de senha obrigatória) → primeiro acesso no backend
(ordem e guard) → painel liberado → nenhum vestígio de 2º Master → operação financeira sensível
(despesa) autorizada pelo PRÓPRIO Master com TOTP + senha financeira + código por e-mail ⇒ executada;
sem fatores ⇒ recusada; ``/approve``/``/reject`` ⇒ 409.

Pulado sem ``RD_E2E_QA003`` (ver ``tests/e2e/qa003_support.py``). Rodar só numa instância recém-criada.
"""
from __future__ import annotations

import time
from datetime import date

import pytest

from tests.e2e import qa003_support as S

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

MODE = "single"


async def test_single_master_bootstrap_first_access_and_self_authorization():  # noqa: C901
    ev = S.Evidence("QA003_1_single")
    inst = S.INSTANCES[MODE]

    # 1. bootstrap
    c = S.counts(MODE)
    hist = S.q(MODE, "SELECT mode_version, kind, to_mode, source, env_value FROM instance_approval_mode_history")
    holder = S.q1(MODE, "SELECT admin_master_seat, must_change_password, totp_enabled, financial_password_hash IS NULL,"
                        " is_active, lower(email) FROM users WHERE admin_master_seat IS NOT NULL")
    ev.check("1.1", "bootstrap: modo SINGLE v1 (BOOTSTRAP_ENV, env=false), exatamente 1 Master no assento 1",
             c["mode"] == "SINGLE" and c["mode_rows"] == 1 and c["seat_holders"] == 1 and c["seat_events"] == 1
             and c["super_admin"] == 1 and c["invites"] == 0 and hist == [(1, "INITIALIZED", "SINGLE", "BOOTSTRAP_ENV", "false")],
             counts=c, history=hist)
    ev.check("1.2", "Master primário nasce com troca obrigatória, sem TOTP e sem senha financeira",
             holder is not None and holder[0] == 1 and holder[1] is True and holder[2] is False and holder[3] is True
             and holder[4] is True and holder[5] == inst["email"], seat=holder and holder[0],
             must_change=holder and holder[1], totp=holder and holder[2])

    a = S.Master(mode=MODE, label="A", email=inst["email"], password=S.initial_password(MODE))
    st = S.load_state(MODE)
    if st.get("A"):
        pytest.skip("instância já usada por uma execução anterior (recrie com qa003_infra.sh down/up)")

    # 2. primeiro acesso (guard no backend)
    await S.first_access(a, ev, "2", expect_mode="SINGLE")
    S.save_state(MODE, {"A": a.to_state()})

    # 3. painel liberado
    res = {}
    for meth, path in [("GET", "/organizations"), ("GET", "/users"), ("GET", "/settings"),
                       ("GET", "/financial/approvals"), ("POST", "/realtime/ticket")]:
        r = await a.req(meth, path)
        res[f"{meth} {path}"] = r.status_code
    ev.check("3.1", "painel liberado após o setup (rotas gerais, financeiro em leitura, ticket WS)",
             all(v in (200, 201) for v in res.values()), results=res)
    me = (await a.get("/me")).json()
    ss = me.get("security_setup") or {}
    ev.check("3.2", "/me.security_setup: required=false, next_step=null, second_master=NOT_REQUIRED, "
                    "dual_approval_ready=null, approval_mode=SINGLE",
             ss.get("required") is False and ss.get("next_step") is None
             and (ss.get("steps") or {}).get("second_master") == "NOT_REQUIRED"
             and ss.get("dual_approval_ready") is None and ss.get("approval_mode") == "SINGLE"
             and ss.get("is_admin_master") is True, security_setup=ss)
    r = await a.refresh()
    ev.check("3.3", "refresh pelo cookie (CSRF + Origin do painel) ⇒ 200", r.status_code == 200, http=r.status_code)

    # 4. nenhum Master 2
    am = await a.get("/security/approval-mode")
    amb = am.json() if am.status_code == 200 else {}
    ev.check("4.1", "/security/approval-mode: SINGLE v1, 1 Master ativo, integridade OK, sem divergência",
             am.status_code == 200 and amb.get("mode") == "SINGLE" and amb.get("mode_version") == 1
             and amb.get("active_masters") == 1 and amb.get("dual_approval_ready") is None
             and (amb.get("integrity") or {}).get("ok") is True and (amb.get("env") or {}).get("divergence") is False,
             http=am.status_code, mode=amb.get("mode"), version=amb.get("mode_version"),
             active=amb.get("active_masters"), integrity=amb.get("integrity"), env=amb.get("env"))
    ms = await a.get("/security/masters")
    msb = ms.json() if ms.status_code == 200 else {}
    ev.check("4.2", "/security/masters: SINGLE, can_invite=false, nenhum convite, exatamente 1 assento ATIVO",
             ms.status_code == 200 and msb.get("approval_mode") == "SINGLE" and msb.get("can_invite") is False
             and msb.get("invite") is None
             and [s.get("status") for s in msb.get("seats") or [] if s.get("status") == "ACTIVE"] == ["ACTIVE"],
             http=ms.status_code, body=msb)
    r = await a.post("/security/masters/invites", json={"full_name": "Segundo QA", "email": "segundo@rd-qa.localhost",
                                                         "totp_code": "000000"})
    ev.check("4.3", "convite de 2º Master no SINGLE ⇒ 409 APPROVAL_MODE_SINGLE",
             r.status_code == 409 and S.err_code(r) == "APPROVAL_MODE_SINGLE", http=r.status_code, code=S.err_code(r))
    acts = (await a.get("/financial/approvals/actions")).json()
    ev.check("4.4", "/financial/approvals/actions informa approval_mode=SINGLE", acts.get("approval_mode") == "SINGLE",
             approval_mode=acts.get("approval_mode"))

    # 5. operação financeira sensível (despesa) — autorização única
    tag = time.strftime("%H%M%S")
    payload = {"description": f"QA003 despesa single {tag}", "category": "qa003", "amount_cents": 12345,
               "competence_date": date.today().isoformat()}
    r = await a.post("/financial/approvals", json={"action_type": "EXPENSE_CREATE", "payload": payload,
                                                    "reason": "QA AÇÃO 003 — despesa de teste (modo único)"},
                     headers={"Idempotency-Key": f"qa003-single-{tag}"})
    req = r.json() if r.status_code == 201 else {}
    rid = req.get("id")
    ev.check("5.1", "criação do pedido ⇒ 201 PENDING, approval_mode=SINGLE, can_authorize=true, can_approve=false",
             r.status_code == 201 and req.get("status") == "PENDING" and req.get("approval_mode") == "SINGLE"
             and req.get("can_authorize") is True and req.get("can_approve") is False,
             http=r.status_code, code=S.err_code(r), status=req.get("status"), mode=req.get("approval_mode"))
    sha = req.get("payload_sha256")
    for verb in ("approve", "reject"):
        body = ({"totp_code": "000000", "financial_password": "x", "payload_sha256": sha, "email_code": "000000"}
                if verb == "approve" else {"reason": "QA rejeição indevida"})
        r = await a.post(f"/financial/approvals/{rid}/{verb}", json=body)
        ev.check(f"5.2.{verb}", f"/{verb} no SINGLE ⇒ 409 APPROVAL_MODE_SINGLE",
                 r.status_code == 409 and S.err_code(r) == "APPROVAL_MODE_SINGLE", http=r.status_code,
                 code=S.err_code(r))
    r = await a.post(f"/financial/approvals/{rid}/authorize",
                     json={"totp_code": await a.totp.code(), "financial_password": "senha-financeira-errada",
                           "payload_sha256": sha, "email_code": "000000"})
    ev.check("5.3", "autorizar com senha financeira errada ⇒ 403 FINANCIAL_STEP_UP_INVALID",
             r.status_code == 403 and S.err_code(r) == "FINANCIAL_STEP_UP_INVALID", http=r.status_code,
             code=S.err_code(r))
    r = await a.post(f"/financial/approvals/{rid}/authorize",
                     json={"totp_code": await a.totp.code(), "financial_password": a.fin_password,
                           "payload_sha256": sha})
    ev.check("5.4", "autorizar sem código por e-mail ⇒ 403 FINANCIAL_EMAIL_CODE_REQUIRED",
             r.status_code == 403 and S.err_code(r) == "FINANCIAL_EMAIL_CODE_REQUIRED", http=r.status_code,
             code=S.err_code(r))
    cur = (await a.get(f"/financial/approvals/{rid}")).json()
    ev.check("5.5", "após as recusas o pedido segue PENDING e nada foi executado",
             cur.get("status") == "PENDING" and cur.get("execution_state") in (None, "NOT_STARTED")
             and S.q1(MODE, "SELECT count(*) FROM payables WHERE approval_request_id = :i", i=rid)[0] == 0,
             status=cur.get("status"), execution_state=cur.get("execution_state"))
    t0 = time.time()
    r = await a.post(f"/financial/approvals/{rid}/email-code")
    code = await S.mail_code(a.email, t0)
    ev.check("5.6", "código por e-mail (finalidade self_authorization) enviado ao PRÓPRIO Master (Mailpit)",
             r.status_code == 200 and bool(code), http=r.status_code)
    r = await a.post(f"/financial/approvals/{rid}/authorize",
                     json={"totp_code": await a.totp.code(), "financial_password": a.fin_password,
                           "payload_sha256": sha, "email_code": code})
    done = r.json() if r.status_code == 200 else {}
    ev.check("5.7", "autorizar com TOTP + senha financeira + código ⇒ APPROVED/EXECUTED; self_authorized_by=A; "
                    "approved_by=NULL",
             r.status_code == 200 and done.get("status") == "APPROVED" and done.get("execution_state") == "EXECUTED"
             and done.get("self_authorized_by") == a.user_id and done.get("approved_by") in (None, ""),
             http=r.status_code, code=S.err_code(r), status=done.get("status"),
             execution_state=done.get("execution_state"))
    row = S.q1(MODE, "SELECT approval_mode, status, approved_by IS NULL, self_authorized_by::text, execution_state"
                     " FROM financial_approval_requests WHERE id = :i", i=rid)
    pay = S.q1(MODE, "SELECT count(*) FROM payables WHERE approval_request_id = :i", i=rid)[0]
    meta = S.q1(MODE, "SELECT metadata FROM ledger_transactions WHERE approval_request_id = :i", i=rid)
    aud = S.q1(MODE, "SELECT after_data FROM audit_logs WHERE action = 'finance.self_authorized' AND entity_id = :i",
               i=rid)
    ev.check("5.8", "banco: pedido SINGLE APPROVED (approved_by NULL), 1 conta a pagar, lançamento com meta do modo, "
                    "auditoria finance.self_authorized com os 3 fatores",
             row == ("SINGLE", "APPROVED", True, a.user_id, "EXECUTED") and pay == 1 and meta
             and (meta[0] or {}).get("authorization_mode") == "SINGLE" and (meta[0] or {}).get("authorized_by") == a.user_id
             and aud and sorted((aud[0] or {}).get("factors") or []) == ["email", "financial_password", "totp"],
             request=row, payables=pay, ledger_meta=meta and meta[0], audit=aud and aud[0])
    r = await a.post(f"/financial/approvals/{rid}/authorize",
                     json={"totp_code": await a.totp.code(), "financial_password": a.fin_password,
                           "payload_sha256": sha, "email_code": code})
    ev.check("5.9", "reautorizar o mesmo pedido ⇒ 409 APPROVAL_NOT_PENDING (executa uma vez)",
             r.status_code == 409 and S.err_code(r) == "APPROVAL_NOT_PENDING", http=r.status_code, code=S.err_code(r))

    final = S.counts(MODE)
    ev.check("6.1", "ao final: ainda exatamente 1 Master (ativo), modo SINGLE v1, nenhum convite",
             final["seat_holders"] == 1 and final["active_masters"] == 1 and final["mode"] == "SINGLE"
             and final["mode_version"] == 1 and final["invites"] == 0, counts=final)
    S.save_state(MODE, {"A": a.to_state(), "counts_after_flow": final, "expense_request": rid})
    await a.aclose()
    ev.assert_ok()
