"""
AÇÃO 003 / QA (agente G) — E2E MODO DUPLO (``RD_BOOTSTRAP_DUAL_MASTER_APPROVAL=true``), pedido itens 39 e 55.

Instância DESCARTÁVEL ``rd-qa003-dual`` (banco ``rdqa003_dual``) recém-criada pelo bootstrap. Quatro partes
EM ORDEM (estado de teste persistido entre elas; permite a checagem visual do modal/banner entre as partes):

- a: bootstrap com Master 1 → primeiro acesso (next_step=SECOND_MASTER, não bloqueante) → área não financeira
     liberada → operação de dupla aprovação ⇒ 409 SECOND_MASTER_REQUIRED (inclusive DUAL→SINGLE).
- b: convite do 2º Master (TOTP de A) → e-mail SÓ para B no Mailpit (link com fragmento) → INVITE_PENDING.
- c: ativação de B (senha, TOTP e senha financeira PRÓPRIOS; sem TOTP ⇒ recusada) → exatamente 2 Masters
     → setup de A sem pendência (banner some) → 3º Master impossível.
- d: A solicita → PENDING; A tenta aprovar/autorizar ⇒ recusado; B aprova com TOTP + senha financeira + código
     por e-mail ⇒ executa.

Pulado sem ``RD_E2E_QA003`` (ver ``tests/e2e/qa003_support.py``).
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date

import httpx
import pytest

from tests.e2e import qa003_support as S

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

MODE = "dual"
B_EMAIL = "master.b.dual@rd-qa.localhost"


def _a() -> S.Master:
    st = S.load_state(MODE)
    if not st.get("A"):
        pytest.fail("parte a não executada (estado ausente)")
    return S.Master.from_state(MODE, st["A"])


def _save(**kw) -> None:
    st = S.load_state(MODE)
    st.update(kw)
    S.save_state(MODE, st)


async def test_dual_a_bootstrap_first_access_and_blocked_dual_approval():
    ev = S.Evidence("QA003_2a_dual")
    inst = S.INSTANCES[MODE]
    if S.load_state(MODE).get("A"):
        pytest.skip("instância já usada (recrie com qa003_infra.sh down/up dual)")
    c = S.counts(MODE)
    hist = S.q(MODE, "SELECT mode_version, kind, to_mode, source, env_value FROM instance_approval_mode_history")
    ev.check("a.1", "bootstrap: modo DUAL v1 (BOOTSTRAP_ENV, env=true); Master 1 no assento 1; assento 2 vago",
             c["mode"] == "DUAL" and c["mode_rows"] == 1 and c["seat_holders"] == 1 and c["seat_events"] == 1
             and c["super_admin"] == 1 and c["invites"] == 0
             and hist == [(1, "INITIALIZED", "DUAL", "BOOTSTRAP_ENV", "true")], counts=c, history=hist)

    a = S.Master(mode=MODE, label="A", email=inst["email"], password=S.initial_password(MODE))
    await S.first_access(a, ev, "a.2", expect_mode="DUAL")
    _save(A=a.to_state())

    me = (await a.get("/me")).json()
    ss = me.get("security_setup") or {}
    ev.check("a.3", "SetupState: required=false (não bloqueante), next_step=SECOND_MASTER, second_master=NOT_INVITED, "
                    "dual_approval_ready=false",
             ss.get("required") is False and ss.get("next_step") == "SECOND_MASTER"
             and (ss.get("steps") or {}).get("second_master") == "NOT_INVITED"
             and ss.get("dual_approval_ready") is False and ss.get("approval_mode") == "DUAL", security_setup=ss)
    res = {}
    for meth, path in [("GET", "/organizations"), ("GET", "/users"), ("GET", "/settings"),
                       ("GET", "/financial/approvals"), ("POST", "/realtime/ticket"), ("GET", "/security/masters")]:
        r = await a.req(meth, path)
        res[f"{meth} {path}"] = r.status_code
    ev.check("a.4", "área NÃO financeira liberada com o 2º Master pendente (inclui ticket WS e leitura financeira)",
             all(v in (200, 201) for v in res.values()), results=res)
    am = (await a.get("/security/approval-mode")).json()
    ev.check("a.5", "/security/approval-mode: DUAL v1, dual_approval_ready=false, 1 Master ativo",
             am.get("mode") == "DUAL" and am.get("mode_version") == 1 and am.get("dual_approval_ready") is False
             and am.get("active_masters") == 1, mode=am.get("mode"), ready=am.get("dual_approval_ready"),
             active=am.get("active_masters"))

    payload = {"description": "QA003 despesa dual bloqueada", "category": "qa003", "amount_cents": 5000,
               "competence_date": date.today().isoformat()}
    r = await a.post("/financial/approvals", json={"action_type": "EXPENSE_CREATE", "payload": payload,
                                                    "reason": "QA AÇÃO 003 — despesa com 2º Master ausente"})
    ev.check("a.6", "operação de dupla aprovação com 2º Master inexistente ⇒ 409 SECOND_MASTER_REQUIRED "
                    "(details.second_master=NOT_INVITED, mensagem clara)",
             r.status_code == 409 and S.err_code(r) == "SECOND_MASTER_REQUIRED"
             and S.err_details(r).get("second_master") == "NOT_INVITED", http=r.status_code, code=S.err_code(r),
             details=S.err_details(r), message=(r.json().get("error") or {}).get("message"))
    r = await a.post("/financial/approvals", json={
        "action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "SINGLE"},
        "reason": "QA 003: tentativa de voltar ao modo único sem o segundo Master",
        "step_up": {"totp_code": await a.totp.code(), "financial_password": a.fin_password}})
    ev.check("a.7", "DUAL→SINGLE com o 2º Master ausente (bypass unilateral) ⇒ 409 SECOND_MASTER_REQUIRED",
             r.status_code == 409 and S.err_code(r) == "SECOND_MASTER_REQUIRED", http=r.status_code,
             code=S.err_code(r))
    n = S.q1(MODE, "SELECT count(*) FROM financial_approval_requests")[0]
    ev.check("a.8", "nenhum pedido foi gravado; modo segue DUAL v1", n == 0 and S.counts(MODE)["mode_version"] == 1,
             pedidos=n)
    _save(A=a.to_state())
    await a.aclose()
    ev.assert_ok()


async def test_dual_b_invite_second_master_email_only_to_b():
    ev = S.Evidence("QA003_2b_dual")
    a = _a()
    assert (await a.login()).status_code == 200
    r = await a.post("/security/masters/invites", json={"full_name": "Master QA B", "email": B_EMAIL,
                                                         "totp_code": a.totp.wrong()})
    ev.check("b.1", "convite com TOTP errado de A ⇒ 400 INVALID_MFA_CODE", r.status_code == 400
             and S.err_code(r) == "INVALID_MFA_CODE", http=r.status_code, code=S.err_code(r))
    t0 = time.time()
    r = await a.post("/security/masters/invites", json={"full_name": "Master QA B", "email": B_EMAIL,
                                                         "totp_code": await a.totp.code()})
    inv = r.json() if r.status_code == 201 else {}
    ev.check("b.2", "convite (nome + e-mail + TOTP de A) ⇒ 201 PENDING, e-mail mascarado, SEM token na resposta",
             r.status_code == 201 and inv.get("status") == "PENDING" and "token" not in inv
             and "@" in (inv.get("email_masked") or "") and inv.get("email_masked") != B_EMAIL,
             http=r.status_code, code=S.err_code(r), invite={k: v for k, v in inv.items() if k != "id"})
    r2 = await a.post("/security/masters/invites", json={"full_name": "Outro QA", "email": "outro@rd-qa.localhost",
                                                          "totp_code": await a.totp.code()})
    ev.check("b.3", "segundo convite com um pendente ⇒ 409 MASTER_INVITE_ALREADY_PENDING",
             r2.status_code == 409 and S.err_code(r2) == "MASTER_INVITE_ALREADY_PENDING", http=r2.status_code,
             code=S.err_code(r2))
    token, meta = await S.mail_invite(B_EMAIL, t0)
    await asyncio.sleep(2)
    rcpts = await S.mail_recipients_since(
        t0, re.compile(re.escape(S.INSTANCES[MODE]["panel"]) + r"/ativar-master#token="))
    ev.check("b.4", "link de ativação (fragmento #token=) entregue SOMENTE ao e-mail de B (Mailpit)",
             bool(token) and rcpts == [B_EMAIL], recipients=rcpts, subject=meta.get("subject"))
    me = (await a.get("/me")).json()
    ss = me.get("security_setup") or {}
    ms = (await a.get("/security/masters")).json()
    ev.check("b.5", "A: second_master=INVITE_PENDING; /security/masters mostra o convite sem token/e-mail completo",
             (ss.get("steps") or {}).get("second_master") == "INVITE_PENDING"
             and (ms.get("invite") or {}).get("status") == "PENDING" and B_EMAIL not in str(ms)
             and not S.leaked_secrets(ms, [token]), security_setup=ss, invite=ms.get("invite"))
    r = await a.post("/financial/approvals", json={
        "action_type": "EXPENSE_CREATE", "reason": "QA AÇÃO 003 — despesa com convite pendente",
        "payload": {"description": "QA003 bloqueada", "category": "qa003", "amount_cents": 100,
                    "competence_date": date.today().isoformat()}})
    ev.check("b.6", "com o convite pendente a dupla aprovação segue bloqueada (409, second_master=INVITE_PENDING)",
             r.status_code == 409 and S.err_code(r) == "SECOND_MASTER_REQUIRED"
             and S.err_details(r).get("second_master") == "INVITE_PENDING", http=r.status_code, code=S.err_code(r),
             details=S.err_details(r))
    aud = S.q(MODE, "SELECT after_data::text FROM audit_logs WHERE action LIKE 'admin_master.invite%'")
    ev.check("b.7", "auditoria do convite sem o token", aud and all(token not in a_[0] for a_ in aud), n_audit=len(aud))
    _save(A=a.to_state(), B_invite_token=token)
    await a.aclose()
    ev.assert_ok()


async def test_dual_c_activate_second_master():
    ev = S.Evidence("QA003_2c_dual")
    st = S.load_state(MODE)
    token = st.get("B_invite_token")
    assert token, "parte b não executada"
    inst = S.INSTANCES[MODE]
    async with httpx.AsyncClient(base_url=inst["url"], timeout=60) as cb:
        r = await cb.post("/api/v1/auth/master-activation/inspect", json={"token": token})
        ins = r.json() if r.status_code == 200 else {}
        ev.check("c.1", "inspect ⇒ 200 (nome, e-mail mascarado, passos PASSWORD/TOTP/FINANCIAL_PASSWORD)",
                 r.status_code == 200 and ins.get("steps") == ["PASSWORD", "TOTP", "FINANCIAL_PASSWORD"]
                 and B_EMAIL not in str(ins), http=r.status_code, body=ins)
        b_pw, b_fin = S.new_login_password(), S.new_financial_password()
        r = await cb.post("/api/v1/auth/master-activation/complete",
                          json={"token": token, "password": b_pw, "totp_code": "123456", "financial_password": b_fin})
        ev.check("c.2", "concluir SEM configurar TOTP ⇒ 400 MFA_SETUP_NOT_STARTED (não existe Master sem TOTP)",
                 r.status_code == 400 and S.err_code(r) == "MFA_SETUP_NOT_STARTED", http=r.status_code,
                 code=S.err_code(r))
        r = await cb.post("/api/v1/auth/master-activation/totp", json={"token": token})
        sec = r.json().get("secret") if r.status_code == 200 else None
        ev.check("c.3", "TOTP de B gerado na ativação (segredo pendente do convite)", bool(sec), http=r.status_code)
        tb = S.Totp(sec)
        r = await cb.post("/api/v1/auth/master-activation/complete",
                          json={"token": token, "password": b_pw, "totp_code": tb.wrong(), "financial_password": b_fin})
        ev.check("c.4", "concluir com TOTP errado ⇒ 400 INVALID_MFA_CODE (tentativa contada)",
                 r.status_code == 400 and S.err_code(r) == "INVALID_MFA_CODE", http=r.status_code, code=S.err_code(r))
        r = await cb.post("/api/v1/auth/master-activation/complete",
                          json={"token": token, "password": b_pw, "totp_code": await tb.code(), "financial_password": b_pw})
        ev.check("c.5", "senha financeira igual à de login ⇒ 400 FINANCIAL_PASSWORD_POLICY",
                 r.status_code == 400 and S.err_code(r) == "FINANCIAL_PASSWORD_POLICY", http=r.status_code,
                 code=S.err_code(r))
        n_masters_before = S.counts(MODE)["seat_holders"]
        r = await cb.post("/api/v1/auth/master-activation/complete",
                          json={"token": token, "password": b_pw, "totp_code": await tb.code(), "financial_password": b_fin})
        body = r.json() if r.status_code == 200 else {}
        ev.check("c.6", "ativação ATÔMICA de B ⇒ 200 + sessão + códigos de recuperação + cookie rd_refresh",
                 r.status_code == 200 and body.get("access_token") and len(body.get("recovery_codes") or []) >= 8
                 and "rd_refresh" in r.cookies and n_masters_before == 1, http=r.status_code, code=S.err_code(r),
                 n_recovery=len(body.get("recovery_codes") or []))
        b = S.Master(mode=MODE, label="B", email=B_EMAIL, password=b_pw, totp=tb, fin_password=b_fin,
                     access_token=body.get("access_token"), user_id=(body.get("user") or {}).get("id"))
        _save(B=b.to_state(), B_invite_token=None)
        r = await cb.post("/api/v1/auth/master-activation/inspect", json={"token": token})
        ev.check("c.7", "token reutilizado ⇒ 400 MASTER_INVITE_INVALID (uso único)",
                 r.status_code == 400 and S.err_code(r) == "MASTER_INVITE_INVALID", http=r.status_code,
                 code=S.err_code(r))
    c = S.counts(MODE)
    seats = S.q(MODE, "SELECT admin_master_seat, rd_is_active_master(id), totp_enabled, financial_password_hash IS NOT NULL,"
                      " must_change_password FROM users WHERE admin_master_seat IS NOT NULL ORDER BY 1")
    inv = S.q1(MODE, "SELECT status, accepted_user_id::text FROM admin_master_invites")
    ev.check("c.8", "banco: exatamente 2 Masters ATIVOS (assentos 1 e 2, ambos com TOTP + senha financeira), "
                    "2 eventos de assento, 2 super_admin, convite ACCEPTED por B",
             c["seat_holders"] == 2 and c["active_masters"] == 2 and c["seat_events"] == 2 and c["super_admin"] == 2
             and seats == [(1, True, True, True, False), (2, True, True, True, False)]
             and inv == ("ACCEPTED", b.user_id), counts=c, seats=seats, invite_status=inv and inv[0])
    a = _a()
    assert (await a.login()).status_code == 200
    me = (await a.get("/me")).json()
    ss = me.get("security_setup") or {}
    ev.check("c.9", "A: second_master=DONE, next_step=null, dual_approval_ready=true (banner some)",
             (ss.get("steps") or {}).get("second_master") == "DONE" and ss.get("next_step") is None
             and ss.get("dual_approval_ready") is True, security_setup=ss)
    meb = (await b.get("/me")).json()
    ssb = meb.get("security_setup") or {}
    ev.check("c.10", "B: Master ativo com setup completo (required=false, next_step=null)",
             ssb.get("required") is False and ssb.get("next_step") is None and ssb.get("is_admin_master") is True,
             security_setup=ssb)
    r = await a.post("/security/masters/invites", json={"full_name": "Terceiro QA", "email": "terceiro@rd-qa.localhost",
                                                         "totp_code": await a.totp.code()})
    ev.check("c.11", "3º Master por convite ⇒ 409 ADMIN_MASTER_SEATS_FULL", r.status_code == 409
             and S.err_code(r) == "ADMIN_MASTER_SEATS_FULL", http=r.status_code, code=S.err_code(r))
    aud = S.q1(MODE, "SELECT after_data FROM audit_logs WHERE action = 'admin_master.activated'")
    ev.check("c.12", "auditoria admin_master.activated (assento 2, convite, same_device_as_inviter)",
             aud is not None and (aud[0] or {}).get("seat") == 2, audit=aud and aud[0])
    _save(A=a.to_state(), B=b.to_state(), counts_after_activation=c)
    await a.aclose()
    await b.aclose()
    ev.assert_ok()


async def test_dual_d_a_requests_b_approves():
    ev = S.Evidence("QA003_2d_dual")
    st = S.load_state(MODE)
    a, b = _a(), S.Master.from_state(MODE, st["B"])
    assert (await a.login()).status_code == 200
    assert (await b.login()).status_code == 200
    tag = time.strftime("%H%M%S")
    payload = {"description": f"QA003 despesa dual {tag}", "category": "qa003", "amount_cents": 7777,
               "competence_date": date.today().isoformat()}
    r = await a.post("/financial/approvals", json={"action_type": "EXPENSE_CREATE", "payload": payload,
                                                    "reason": "QA AÇÃO 003 — despesa de teste (dupla aprovação)"})
    req = r.json() if r.status_code == 201 else {}
    rid, sha = req.get("id"), req.get("payload_sha256")
    ev.check("d.1", "A solicita ⇒ 201 PENDING, approval_mode=DUAL, can_approve=false para A",
             r.status_code == 201 and req.get("status") == "PENDING" and req.get("approval_mode") == "DUAL"
             and req.get("can_approve") is False and req.get("can_authorize") is False, http=r.status_code,
             code=S.err_code(r), status=req.get("status"))
    body = {"totp_code": await a.totp.code(), "financial_password": a.fin_password, "payload_sha256": sha,
            "email_code": "000000"}
    r = await a.post(f"/financial/approvals/{rid}/approve", json=body)
    ev.check("d.2", "A tenta aprovar o próprio pedido ⇒ 403 SELF_APPROVAL_FORBIDDEN",
             r.status_code == 403 and S.err_code(r) == "SELF_APPROVAL_FORBIDDEN", http=r.status_code,
             code=S.err_code(r))
    r = await a.post(f"/financial/approvals/{rid}/authorize", json={**body, "totp_code": await a.totp.code()})
    ev.check("d.3", "A tenta 'autorização única' no DUAL ⇒ 409 APPROVAL_MODE_DUAL",
             r.status_code == 409 and S.err_code(r) == "APPROVAL_MODE_DUAL", http=r.status_code, code=S.err_code(r))
    r = await a.post(f"/financial/approvals/{rid}/email-code")
    ev.check("d.4", "A pede o código de aprovação do próprio pedido ⇒ 403", r.status_code == 403,
             http=r.status_code, code=S.err_code(r))
    vb = (await b.get(f"/financial/approvals/{rid}")).json()
    ev.check("d.5", "B vê o pedido com can_approve=true", vb.get("can_approve") is True and vb.get("status") == "PENDING",
             can_approve=vb.get("can_approve"))
    r = await b.post(f"/financial/approvals/{rid}/authorize",
                     json={"totp_code": await b.totp.code(), "financial_password": b.fin_password,
                           "payload_sha256": sha, "email_code": "000000"})
    ev.check("d.6", "B tenta /authorize no DUAL ⇒ 409 APPROVAL_MODE_DUAL", r.status_code == 409
             and S.err_code(r) == "APPROVAL_MODE_DUAL", http=r.status_code, code=S.err_code(r))
    r = await b.post(f"/financial/approvals/{rid}/approve",
                     json={"totp_code": await b.totp.code(), "financial_password": b.fin_password,
                           "payload_sha256": sha})
    ev.check("d.7", "B aprova sem o código por e-mail ⇒ 403 FINANCIAL_EMAIL_CODE_REQUIRED",
             r.status_code == 403 and S.err_code(r) == "FINANCIAL_EMAIL_CODE_REQUIRED", http=r.status_code,
             code=S.err_code(r))
    t0 = time.time()
    r = await b.post(f"/financial/approvals/{rid}/email-code")
    code = await S.mail_code(b.email, t0)
    ev.check("d.8", "código de aprovação enviado ao e-mail de B", r.status_code == 200 and bool(code), http=r.status_code)
    r = await b.post(f"/financial/approvals/{rid}/approve",
                     json={"totp_code": await b.totp.code(), "financial_password": b.fin_password,
                           "payload_sha256": sha, "email_code": code})
    done = r.json() if r.status_code == 200 else {}
    ev.check("d.9", "B aprova com TOTP + senha financeira + código ⇒ APPROVED/EXECUTED (approved_by=B)",
             r.status_code == 200 and done.get("status") == "APPROVED" and done.get("execution_state") == "EXECUTED"
             and done.get("approved_by") == b.user_id, http=r.status_code, code=S.err_code(r),
             status=done.get("status"), execution_state=done.get("execution_state"))
    row = S.q1(MODE, "SELECT approval_mode, status, requested_by::text, approved_by::text, self_authorized_by IS NULL"
                     " FROM financial_approval_requests WHERE id = :i", i=rid)
    meta = S.q1(MODE, "SELECT metadata FROM ledger_transactions WHERE approval_request_id = :i", i=rid)
    ev.check("d.10", "banco: DUAL APPROVED requested_by=A, approved_by=B, self_authorized_by NULL; lançamento com meta DUAL",
             row == ("DUAL", "APPROVED", a.user_id, b.user_id, True) and meta
             and (meta[0] or {}).get("authorization_mode") == "DUAL" and (meta[0] or {}).get("authorized_by") == b.user_id,
             request=row, ledger_meta=meta and meta[0])
    _save(A=a.to_state(), B=b.to_state(), counts_after_flow=S.counts(MODE), dual_request=rid)
    await a.aclose()
    await b.aclose()
    ev.assert_ok()
