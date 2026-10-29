"""
Helpers dos testes financeiros (W-FIN). PostgreSQL/Redis reais (harness do W1).

- ``fin_env``: configura a instância CloudPayPag de TESTE (token/segredo gerados por
  execução; subconta 2 como no exemplo da doc), captura o despacho de tasks e limpa os
  overrides no fim. Nenhuma chamada sai para a internet: o cliente real é testado com
  ``respx``; o restante usa o ``FakeCloudPayPag`` (somente test/development).
- ``scope``: sessão transacional separada (commit no fim) — mesma semântica do worker.
"""
from __future__ import annotations

import json
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.authz import AuthContext, load_grants
from app.core.database import session_factory
from app.core.passwords import hash_password
from app.models.financial import PaymentOrder
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import commission, common, payments
from tests.factories import make_establishment, make_org, make_user

TEST_TOKEN = "cpg_test_" + secrets.token_hex(12)
TEST_SECRET = "whsec_" + secrets.token_hex(16)
SUBCONTA = 2
DOC_EXAMPLES = json.loads((Path(__file__).resolve().parent / "fixtures" / "cloudpaypag_doc_examples.json").read_text("utf-8"))


def provider_config(*, mode: str = "fake", subconta_id: Optional[int] = SUBCONTA, secret: str = TEST_SECRET,
                token: str = TEST_TOKEN) -> cpp.ProviderConfig:
    return cpp.ProviderConfig(instance_slug="rd", base_url="https://cloudpaypag.top", token=token,
                              webhook_secret=secret, subconta_id=subconta_id, mode=mode)


@pytest.fixture
def fin_env(clean_state):
    dispatched: list = []
    common.set_task_dispatcher(lambda name, args: dispatched.append((name, args)))
    cfg = provider_config()
    cpp.set_config_override(cfg)
    commission.set_contracted_plan_provider(None)
    yield SimpleNamespace(cfg=cfg, dispatched=dispatched)
    cpp.set_config_override(None)
    cpp.set_provider_override(None)
    common.set_task_dispatcher(None)
    commission.set_contracted_plan_provider(None)
    commission.set_attribution_provider(None)


@pytest_asyncio.fixture
async def scope(db_engine):
    """Fábrica de sessões independentes com commit (como ``worker_session``)."""
    sf = session_factory(db_engine)

    @asynccontextmanager
    async def _scope():
        async with sf() as s:
            try:
                yield s
                await s.commit()
            except BaseException:
                await s.rollback()
                raise

    return _scope


async def ctx_for(db, user) -> AuthContext:
    """Contexto real (grants do banco); recarrega o usuário (seguro após rollback/expiração)."""
    from sqlalchemy import inspect as sa_inspect

    from app.models.users import User

    uid = user if isinstance(user, uuid.UUID) else sa_inspect(user).identity[0]
    u = await db.get(User, uid)
    return AuthContext(kind="user", user=u, grants=await load_grants(db, uid))


async def set_fin_password(db, user, pw: Optional[str] = None) -> str:
    from app.models.users import User

    pw = pw or ("Fin-" + secrets.token_hex(6))
    u = await db.get(User, user.id)
    u.financial_password_hash = hash_password(pw)
    await db.commit()
    user.fin_password = pw  # type: ignore[attr-defined]
    return pw


async def _seated_master(db, seat: int, name: str) -> Any:
    u = await make_user(db, role="super_admin", mfa=True, admin_master_seat=seat, name=name)
    if not getattr(u, "fin_password", None):  # fábrica antiga não definia a senha financeira
        await set_fin_password(db, u)
    if u in db:  # desanexado: ``db.expire_all()`` nos testes não expira o "cartão" do Master (id, e-mail…)
        db.expunge(u)
    return u


async def make_admin_masters(db, *, mode: str = "DUAL") -> tuple[Any, Any]:
    """Dois Masters ATIVOS (assento + TOTP + senha financeira + ``super_admin``) com assentos SELADOS
    (``LEGACY_ADOPTED``) e o modo da instância inicializado (padrão DUAL — fluxo A solicita → B aprova)."""
    from tests.factories import adopt_master_seats, init_approval_mode

    a = await _seated_master(db, 1, "Sócio A")
    b = await _seated_master(db, 2, "Sócio B")
    await init_approval_mode(db, mode)
    await adopt_master_seats(db, a, b)
    return a, b


async def make_single_master(db, *, mode: str = "SINGLE", seat: int = 1, name: str = "Sócio A") -> Any:
    """Um único Master ativo e selado; modo da instância ``mode`` (SINGLE por padrão; DUAL = 2º pendente)."""
    from tests.factories import adopt_master_seats, init_approval_mode

    a = await _seated_master(db, seat, name)
    await init_approval_mode(db, mode)
    await adopt_master_seats(db, a)
    return a


async def add_second_master(db, name: str = "Sócio B") -> Any:
    """Ativa o 2º Master (assento 2 selado) numa instância já inicializada — equivale, para o motor
    financeiro, à conclusão da ativação por convite (fluxo do agente B, testado por ele)."""
    from tests.factories import adopt_master_seats

    b = await _seated_master(db, 2, name)
    await adopt_master_seats(db, b)
    return b


async def payer_org(db, **kw):
    return await make_org(db, email=f"pagador{secrets.token_hex(3)}@example.com", phone="62999990001",
                          document=f"{secrets.randbelow(10**14):014d}", document_type="CNPJ", **kw)


async def make_screen(db, est, name: str = "Tela") -> Any:
    from app.models.screens import Screen

    s = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=f"TEL-GO-{secrets.token_hex(3).upper()}",
               name=name)
    db.add(s)
    await db.commit()
    return s


async def est_with_owner(db, *, tz: str = "America/Sao_Paulo", mfa: bool = True):
    org = await make_org(db, business_roles=["PARTNER"])
    est = await make_establishment(db, org, tz=tz)
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org.id), mfa=mfa)
    if mfa:
        await set_fin_password(db, owner)
    return org, est, owner


def media_item(campaign_id, amount: int, start: date, end: date) -> dict:
    return {"kind": "MEDIA", "campaign_id": campaign_id, "description": "mídia", "amount_cents": amount,
            "service_start": start, "service_end": end}


async def create_order(db, org, items: list[dict], amount: int, *, ref_type: str = "test_ref",
                       ref_id: Optional[str] = None) -> PaymentOrder:
    """Cobrança pelo caminho real (``create_payment_order``) com o provedor falso."""
    o = await payments.create_payment_order(db, AuthContext.system("system"), org.id, items, amount, None,
                                            (ref_type, ref_id or uuid.uuid4().hex))
    await db.commit()
    return o


def webhook_for(order: PaymentOrder, *, status: str = "paid", event_id: Optional[int] = None,
                amount_cents: Optional[int] = None, fee_cents: int = 299, subconta_id: Optional[int] = None,
                secret: str = TEST_SECRET) -> tuple[bytes, dict]:
    payload = cpp.build_charge_webhook_payload(
        status=status, external_ref=order.provider_external_ref, order_id=str(order.id),
        amount_cents=order.amount_cents if amount_cents is None else amount_cents, fee_cents=fee_cents,
        webhook_event_id=event_id or (secrets.randbelow(2**40) + 1), transaction_id="tx_" + secrets.token_hex(8),
        internal_transaction_id=secrets.randbelow(10**6) + 1, method="pix",
        subconta_id=SUBCONTA if subconta_id is None else subconta_id)
    body = cpp.dumps_exact(payload)
    return body.encode(), {"Content-Type": "application/json",
                           cpp.SIGNATURE_HEADER: cpp.signature_header_value(json.loads(body), secret)}


async def pay(db, order: PaymentOrder, **kw) -> str:
    """Entrega um webhook assinado pela rota de ingestão e processa (caminho real)."""
    raw, headers = webhook_for(order, **kw)
    res = await payments.ingest_webhook(db, raw, headers)
    await db.commit()
    assert res.status_code == 200, res.body
    if res.event_id is None:
        return "DUPLICATE"
    st = await payments.process_webhook_event(db, res.event_id)
    await db.commit()
    return st


def pop(campaign_id, screen_id, day: date, n: int = 1) -> list:
    return [SimpleNamespace(playback_uuid=uuid.uuid4(), campaign_id=campaign_id, screen_id=screen_id,
                            kind="EXTERNAL", local_day=day) for _ in range(n)]


def plan_provider(plans: dict):
    """Plano contratado (entrada do W-INV) para os testes do algoritmo de comissão."""

    async def _p(db, campaign_id):
        return plans.get(campaign_id, {})

    commission.set_contracted_plan_provider(_p)


def days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


async def balance(db, code: str, owner_id=None) -> int:
    from app.modules.financial import ledger

    return await ledger.balance_of(db, code, owner_id)


async def get(db, model, pk):
    db.expire_all()
    return (await db.execute(select(model).where(model.__mapper__.primary_key[0] == pk))).scalar_one()


# ─── Código por e-mail (3º fator: toda aprovação financeira e todo saque) ─────
import re as _re  # noqa: E402

_ECODE_RE = _re.compile(r"é (\d{6})\.")


def last_code(mail, user) -> str:
    """Código de 6 dígitos do último e-mail de CÓDIGO financeiro enviado ao usuário (caixa de saída em
    memória). Ignora notificações que cheguem depois (são entregues em segundo plano, após o commit)."""
    msgs = [m for m in getattr(mail, "outbox", []) if m.to.lower() == user.email.lower()]
    assert msgs, "nenhum e-mail enviado"
    for m in reversed(msgs):
        found = _ECODE_RE.search(m.text or "")
        if found and (m.tags or {}).get("kind", "fin_email_code") == "fin_email_code":
            return found.group(1)
    raise AssertionError(msgs[-1].text)


async def http_approval_code(client, session, req_id, mail, user) -> str:
    r = await client.post(f"/api/v1/financial/approvals/{req_id}/email-code", headers=session.headers)
    assert r.status_code == 200, r.text
    return last_code(mail, user)


async def http_withdrawal_code(client, session, establishment_id, amount_cents, mail, user) -> str:
    r = await client.post("/api/v1/financial/withdrawals/email-code",
                          json={"establishment_id": str(establishment_id), "amount_cents": amount_cents},
                          headers=session.headers)
    assert r.status_code == 202, r.text
    return last_code(mail, user)


async def authorize_svc(db, requester, req, mail):
    """Autorização ÚNICA (SINGLE) pelo serviço com os três fatores do próprio solicitante."""
    from app.modules.financial import approvals
    from tests.factories import totp_code

    await approvals.request_email_code(db, await ctx_for(db, requester), req.id)
    await db.commit()
    out = await approvals.authorize(db, await ctx_for(db, requester), req.id, totp_code=totp_code(requester),
                                    financial_password=requester.fin_password, payload_sha256=req.payload_sha256,
                                    email_code=last_code(mail, requester))
    await db.commit()
    return out


async def approve_svc(db, approver, req, mail):
    """Aprovação pelo serviço com os três fatores (TOTP + senha financeira + código por e-mail)."""
    from app.modules.financial import approvals
    from tests.factories import totp_code

    await approvals.request_email_code(db, await ctx_for(db, approver), req.id)
    await db.commit()
    out = await approvals.approve(db, await ctx_for(db, approver), req.id, totp_code=totp_code(approver),
                                  financial_password=approver.fin_password, payload_sha256=req.payload_sha256,
                                  email_code=last_code(mail, approver))
    await db.commit()
    return out
