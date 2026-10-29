"""
AÇÃO 003 / QA (agente G) — TRANSIÇÕES de modo (pedido itens 40–41) na instância DESCARTÁVEL ``rd-qa003-single``
(depois do fluxo 1 e do reinício). Partes EM ORDEM:

- t1: SINGLE→DUAL pelo fluxo real (``APPROVAL_MODE_CHANGE`` autorizado pelo próprio Master com TOTP + senha
      financeira + código por e-mail) → v2 DUAL; pedidos SINGLE pendentes cancelados; 2º Master passa a ser exigido
      (financeiro restrito até a ativação); convite do 2º Master; DUAL→SINGLE com o 2º pendente ⇒ 409.
- t2: ativação do 2º Master (API; pulada se o estado já tiver B — ativação feita pela UI ``/ativar-master``).
- t3: DUAL→SINGLE protegido: A solicita (sem/errado step-up ⇒ 403), A tenta aprovar/autorizar ⇒ recusado,
      B aprova com 3 fatores ⇒ v3 SINGLE com motivo e auditoria; pedidos DUAL pendentes cancelados.

Pulado sem ``RD_E2E_QA003`` (ver ``tests/e2e/qa003_support.py``).
"""
from __future__ import annotations

import time
from datetime import date

import httpx
import pytest

from tests.e2e import qa003_support as S

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

MODE = "single"
B_EMAIL = "master.b.single@rd-qa.localhost"
REASON_S2D = "QA AÇÃO 003: passar a instância para DUPLA aprovação (fluxo real)"
REASON_D2S = "QA AÇÃO 003: voltar a instância para aprovação ÚNICA (A solicita, B aprova)"


def _expense(tag: str) -> dict:
    return {"description": f"QA003 transição {tag}", "category": "qa003", "amount_cents": 999,
            "competence_date": date.today().isoformat()}


def _save(**kw) -> None:
    st = S.load_state(MODE)
    st.update(kw)
    S.save_state(MODE, st)


async def _authorize_single(m: S.Master, rid: str, sha: str) -> httpx.Response:
    t0 = time.time()
    r = await m.post(f"/financial/approvals/{rid}/email-code")
    assert r.status_code == 200, r.text
    code = await S.mail_code(m.email, t0)
    return await m.post(f"/financial/approvals/{rid}/authorize",
                        json={"totp_code": await m.totp.code(), "financial_password": m.fin_password,
                              "payload_sha256": sha, "email_code": code})


async def test_t1_single_to_dual_real_flow():
    ev = S.Evidence("QA003_4t1_single_to_dual")
    st = S.load_state(MODE)
    if not st.get("A") or st.get("t1_done"):
        pytest.skip("pré-condição: fluxo 1 executado e t1 ainda não executado")
    a = S.Master.from_state(MODE, st["A"])
    assert (await a.login()).status_code == 200
    c0 = S.counts(MODE)
    ev.check("t1.0", "pré-condição: SINGLE v1, 1 Master", c0["mode"] == "SINGLE" and c0["mode_version"] == 1
             and c0["active_masters"] == 1, counts=c0)
    r = await a.post("/financial/approvals", json={"action_type": "EXPENSE_CREATE", "payload": _expense("pendente-single"),
                                                    "reason": "QA AÇÃO 003 — pendente SINGLE antes da troca"})
    p1 = r.json().get("id") if r.status_code == 201 else None
    ev.check("t1.1", "pedido SINGLE pendente criado (deve ser cancelado pela troca)", r.status_code == 201, http=r.status_code)

    bad = [
        ({"target_mode": "SINGLE"}, REASON_S2D, 400, "APPROVAL_MODE_CHANGE_INVALID", "alvo = modo atual"),
        ({"target_mode": "DUAL", "from_mode": "SINGLE"}, REASON_S2D, 400, "APPROVAL_MODE_CHANGE_INVALID", "chave extra"),
        ({"target_mode": "DUAL"}, "motivo curto!", 400, "REASON_REQUIRED", "motivo < 20"),
    ]
    for i, (payload, reason, http, code, what) in enumerate(bad):
        r = await a.post("/financial/approvals", json={"action_type": "APPROVAL_MODE_CHANGE", "payload": payload,
                                                        "reason": reason})
        ev.check(f"t1.2.{i}", f"mudança inválida ({what}) ⇒ {http} {code}", r.status_code == http
                 and S.err_code(r) == code, http=r.status_code, code=S.err_code(r))
    r = await a.post("/financial/approvals", json={"action_type": "APPROVAL_MODE_CHANGE",
                                                    "payload": {"target_mode": "DUAL"}, "reason": REASON_S2D})
    req = r.json() if r.status_code == 201 else {}
    ev.check("t1.3", "pedido SINGLE→DUAL ⇒ 201 PENDING (approval_mode SINGLE, is_mode_change, payload canônico do servidor)",
             r.status_code == 201 and req.get("is_mode_change") is True and req.get("approval_mode") == "SINGLE"
             and (req.get("payload") or {}).get("from_mode") == "SINGLE"
             and (req.get("payload") or {}).get("expected_version") == 1, http=r.status_code, code=S.err_code(r),
             payload=req.get("payload"))
    rid, sha = req.get("id"), req.get("payload_sha256")
    r = await a.post(f"/financial/approvals/{rid}/authorize",
                     json={"totp_code": await a.totp.code(), "financial_password": a.fin_password, "payload_sha256": sha})
    ev.check("t1.4", "autorizar a troca SEM o código por e-mail ⇒ 403 FINANCIAL_EMAIL_CODE_REQUIRED (modo inalterado)",
             r.status_code == 403 and S.err_code(r) == "FINANCIAL_EMAIL_CODE_REQUIRED"
             and S.counts(MODE)["mode_version"] == 1, http=r.status_code, code=S.err_code(r))
    r = await _authorize_single(a, rid, sha)
    done = r.json() if r.status_code == 200 else {}
    ev.check("t1.5", "autorização única com 3 fatores ⇒ APPROVED/EXECUTED, execution_ref=mode_version:2",
             r.status_code == 200 and done.get("execution_state") == "EXECUTED"
             and done.get("execution_ref") == "mode_version:2", http=r.status_code, code=S.err_code(r),
             execution_ref=done.get("execution_ref"))
    hist = S.q(MODE, "SELECT mode_version, kind, from_mode, to_mode, source, requested_by::text, authorized_by::text,"
                     " reason, approval_request_id::text FROM instance_approval_mode_history ORDER BY mode_version")
    ev.check("t1.6", "histórico selado: v2 SINGLE_TO_DUAL (APPROVAL_REQUEST, solicitante = autorizador = A, motivo)",
             len(hist) == 2 and hist[1][:5] == (2, "SINGLE_TO_DUAL", "SINGLE", "DUAL", "APPROVAL_REQUEST")
             and hist[1][5] == a.user_id and hist[1][6] == a.user_id and hist[1][7] == REASON_S2D and hist[1][8] == rid,
             history=[h[:5] for h in hist])
    p1row = S.q1(MODE, "SELECT status FROM financial_approval_requests WHERE id = :i", i=p1)
    p1ev = S.q1(MODE, "SELECT count(*) FROM financial_approval_events WHERE request_id = :i AND event = 'CANCELLED_MODE_CHANGE'",
                i=p1)
    ev.check("t1.7", "pedido SINGLE pendente ⇒ CANCELLED (evento CANCELLED_MODE_CHANGE)",
             p1row == ("CANCELLED",) and p1ev[0] == 1, status=p1row, events=p1ev[0])
    me = (await a.get("/me")).json()
    ss = me.get("security_setup") or {}
    ev.check("t1.8", "A: modo DUAL, second_master=NOT_INVITED, next_step=SECOND_MASTER, dual_approval_ready=false",
             ss.get("approval_mode") == "DUAL" and (ss.get("steps") or {}).get("second_master") == "NOT_INVITED"
             and ss.get("next_step") == "SECOND_MASTER" and ss.get("dual_approval_ready") is False, security_setup=ss)
    r = await a.post("/financial/approvals", json={"action_type": "EXPENSE_CREATE", "payload": _expense("pos-dual"),
                                                    "reason": "QA AÇÃO 003 — financeiro após SINGLE→DUAL"})
    ev.check("t1.9", "financeiro restrito até a ativação do 2º Master ⇒ 409 SECOND_MASTER_REQUIRED",
             r.status_code == 409 and S.err_code(r) == "SECOND_MASTER_REQUIRED", http=r.status_code, code=S.err_code(r))
    aud = S.audit_count(MODE, "security.approval_mode_changed")
    ev.check("t1.10", "auditoria security.approval_mode_changed registrada", aud >= 1, n=aud)
    t0 = time.time()
    r = await a.post("/security/masters/invites", json={"full_name": "Master QA B single", "email": B_EMAIL,
                                                         "totp_code": await a.totp.code()})
    ev.check("t1.11", "convite do 2º Master agora permitido (modo DUAL)", r.status_code == 201, http=r.status_code,
             code=S.err_code(r))
    token, _ = await S.mail_invite(B_EMAIL, t0)
    r = await a.post("/financial/approvals", json={
        "action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "SINGLE"}, "reason": REASON_D2S,
        "step_up": {"totp_code": await a.totp.code(), "financial_password": a.fin_password}})
    ev.check("t1.12", "DUAL→SINGLE com o 2º Master PENDENTE ⇒ 409 SECOND_MASTER_REQUIRED (impossível)",
             r.status_code == 409 and S.err_code(r) == "SECOND_MASTER_REQUIRED", http=r.status_code, code=S.err_code(r))
    _save(A=a.to_state(), t1_done=True, B_invite_token=token)
    await a.aclose()
    ev.assert_ok()


async def test_t2_activate_second_master():
    st = S.load_state(MODE)
    if not st.get("t1_done"):
        pytest.skip("t1 não executado")
    if st.get("B"):
        pytest.skip("B já ativado (UI /ativar-master)")
    ev = S.Evidence("QA003_4t2_activation")
    token = st["B_invite_token"]
    async with httpx.AsyncClient(base_url=S.INSTANCES[MODE]["url"], timeout=60) as cb:
        r = await cb.post("/api/v1/auth/master-activation/totp", json={"token": token})
        tb = S.Totp(r.json()["secret"])
        b_pw, b_fin = S.new_login_password(), S.new_financial_password()
        r = await cb.post("/api/v1/auth/master-activation/complete",
                          json={"token": token, "password": b_pw, "totp_code": await tb.code(), "financial_password": b_fin})
        body = r.json() if r.status_code == 200 else {}
    ev.check("t2.1", "ativação de B ⇒ 200", r.status_code == 200, http=r.status_code, code=S.err_code(r))
    b = S.Master(mode=MODE, label="B", email=B_EMAIL, password=b_pw, totp=tb, fin_password=b_fin,
                 user_id=(body.get("user") or {}).get("id"))
    _save(B=b.to_state(), B_invite_token=None)
    c = S.counts(MODE)
    ev.check("t2.2", "exatamente 2 Masters ativos", c["active_masters"] == 2 and c["seat_holders"] == 2, counts=c)
    ev.assert_ok()


async def test_t3_dual_to_single_protected_flow():
    st = S.load_state(MODE)
    if not st.get("B") or st.get("t3_done"):
        pytest.skip("pré-condição: t1+t2 executados e t3 ainda não executado")
    ev = S.Evidence("QA003_4t3_dual_to_single")
    a, b = S.Master.from_state(MODE, st["A"]), S.Master.from_state(MODE, st["B"])
    assert (await a.login()).status_code == 200
    assert (await b.login()).status_code == 200
    c0 = S.counts(MODE)
    ev.check("t3.0", "pré-condição: DUAL v2 com 2 Masters ativos", c0["mode"] == "DUAL" and c0["mode_version"] == 2
             and c0["active_masters"] == 2, counts=c0)
    r = await a.post("/financial/approvals", json={"action_type": "EXPENSE_CREATE", "payload": _expense("pendente-dual"),
                                                    "reason": "QA AÇÃO 003 — pendente DUAL antes da troca"})
    p2 = r.json().get("id") if r.status_code == 201 else None
    ev.check("t3.1", "pedido DUAL pendente criado (deve ser cancelado pela troca)", r.status_code == 201, http=r.status_code)
    r = await a.post("/financial/approvals", json={"action_type": "APPROVAL_MODE_CHANGE",
                                                    "payload": {"target_mode": "SINGLE"}, "reason": REASON_D2S})
    ev.check("t3.2", "DUAL→SINGLE sem step-up ⇒ 403 FINANCIAL_STEP_UP_INVALID", r.status_code == 403
             and S.err_code(r) == "FINANCIAL_STEP_UP_INVALID", http=r.status_code, code=S.err_code(r))
    r = await a.post("/financial/approvals", json={
        "action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "SINGLE"}, "reason": REASON_D2S,
        "step_up": {"totp_code": await a.totp.code(), "financial_password": "senha-financeira-errada"}})
    ev.check("t3.3", "DUAL→SINGLE com senha financeira errada ⇒ 403 FINANCIAL_STEP_UP_INVALID", r.status_code == 403
             and S.err_code(r) == "FINANCIAL_STEP_UP_INVALID", http=r.status_code, code=S.err_code(r))
    r = await a.post("/financial/approvals", json={
        "action_type": "APPROVAL_MODE_CHANGE", "payload": {"target_mode": "SINGLE"}, "reason": REASON_D2S,
        "step_up": {"totp_code": await a.totp.code(), "financial_password": a.fin_password}})
    req = r.json() if r.status_code == 201 else {}
    rid, sha = req.get("id"), req.get("payload_sha256")
    ev.check("t3.4", "A solicita DUAL→SINGLE com step-up ⇒ 201 PENDING (approval_mode DUAL); modo ainda DUAL v2",
             r.status_code == 201 and req.get("status") == "PENDING" and req.get("approval_mode") == "DUAL"
             and S.counts(MODE)["mode_version"] == 2, http=r.status_code, code=S.err_code(r))
    am = (await a.get("/security/approval-mode")).json()
    ev.check("t3.5", "/security/approval-mode mostra o pedido pendente de mudança",
             (am.get("pending_change_request") or {}).get("id") == rid
             and (am.get("pending_change_request") or {}).get("target_mode") == "SINGLE",
             pending=am.get("pending_change_request"))
    r = await a.post(f"/financial/approvals/{rid}/approve",
                     json={"totp_code": await a.totp.code(), "financial_password": a.fin_password,
                           "payload_sha256": sha, "email_code": "000000"})
    ev.check("t3.6", "A tenta aprovar a própria troca (unilateral) ⇒ 403 SELF_APPROVAL_FORBIDDEN",
             r.status_code == 403 and S.err_code(r) == "SELF_APPROVAL_FORBIDDEN", http=r.status_code, code=S.err_code(r))
    r = await a.post(f"/financial/approvals/{rid}/authorize",
                     json={"totp_code": await a.totp.code(), "financial_password": a.fin_password,
                           "payload_sha256": sha, "email_code": "000000"})
    ev.check("t3.7", "A tenta 'autorização única' da troca ⇒ 409 APPROVAL_MODE_DUAL", r.status_code == 409
             and S.err_code(r) == "APPROVAL_MODE_DUAL", http=r.status_code, code=S.err_code(r))
    ev.check("t3.8", "após as tentativas unilaterais o modo segue DUAL v2", S.counts(MODE)["mode_version"] == 2)
    t0 = time.time()
    r = await b.post(f"/financial/approvals/{rid}/email-code")
    code = await S.mail_code(b.email, t0)
    r = await b.post(f"/financial/approvals/{rid}/approve",
                     json={"totp_code": await b.totp.code(), "financial_password": b.fin_password,
                           "payload_sha256": sha, "email_code": code})
    done = r.json() if r.status_code == 200 else {}
    ev.check("t3.9", "B aprova com TOTP + senha financeira + código ⇒ EXECUTED, mode_version:3",
             r.status_code == 200 and done.get("execution_state") == "EXECUTED"
             and done.get("execution_ref") == "mode_version:3", http=r.status_code, code=S.err_code(r),
             execution_ref=done.get("execution_ref"))
    hist = S.q(MODE, "SELECT mode_version, kind, from_mode, to_mode, requested_by::text, authorized_by::text, reason"
                     " FROM instance_approval_mode_history ORDER BY mode_version")
    ev.check("t3.10", "histórico: v3 DUAL_TO_SINGLE solicitado por A, autorizado por B, com motivo",
             len(hist) == 3 and hist[2][:4] == (3, "DUAL_TO_SINGLE", "DUAL", "SINGLE") and hist[2][4] == a.user_id
             and hist[2][5] == b.user_id and hist[2][6] == REASON_D2S, history=[h[:4] for h in hist])
    p2row = S.q1(MODE, "SELECT status FROM financial_approval_requests WHERE id = :i", i=p2)
    ev.check("t3.11", "pedido DUAL pendente ⇒ CANCELLED pela troca", p2row == ("CANCELLED",), status=p2row)
    am = (await a.get("/security/approval-mode")).json()
    names = [(h.get("mode_version"), h.get("kind"), bool(h.get("requested_by_name")), bool(h.get("authorized_by_name")))
             for h in am.get("history") or []]
    ev.check("t3.12", "/security/approval-mode: SINGLE v3, integridade OK, histórico com nomes (A/B) e motivo",
             am.get("mode") == "SINGLE" and am.get("mode_version") == 3 and (am.get("integrity") or {}).get("ok") is True
             and len(names) == 3, mode=am.get("mode"), version=am.get("mode_version"), history=names)
    aud = S.q(MODE, "SELECT after_data FROM audit_logs WHERE action = 'security.approval_mode_changed' ORDER BY created_at")
    ev.check("t3.13", "auditoria security.approval_mode_changed para v2 e v3", len(aud) >= 2, n=len(aud),
             last=aud and aud[-1][0])
    r = await a.post("/financial/approvals", json={"action_type": "EXPENSE_CREATE", "payload": _expense("pos-single"),
                                                    "reason": "QA AÇÃO 003 — financeiro após DUAL→SINGLE"})
    req = r.json() if r.status_code == 201 else {}
    ev.check("t3.14", "após voltar a SINGLE: novo pedido nasce SINGLE (autorização única)",
             r.status_code == 201 and req.get("approval_mode") == "SINGLE" and req.get("can_authorize") is True,
             http=r.status_code, mode=req.get("approval_mode"))
    _save(A=a.to_state(), B=b.to_state(), t3_done=True, counts_after_transitions=S.counts(MODE))
    await a.aclose()
    await b.aclose()
    ev.assert_ok()
