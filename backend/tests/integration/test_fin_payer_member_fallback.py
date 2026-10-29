"""AÇÃO 004 — contato do pagador NÃO vem da equipe RD.

Achado do coordenador: ``payments.payer_from_org`` fazia ``email = org.email or user.email`` (idem telefone).
Com o BRITAS BAR sem e-mail e a cobrança PIX FAKE criada pela conta de TESTE admin (GLOBAL), o pagador saiu
com o e-mail do admin. Agora o fallback só usa o usuário que é MEMBRO da organização pagadora (atribuição com
escopo ORGANIZATION dela — o próprio anunciante/agência que opera a conta). Equipe RD (GLOBAL/CIDADE) e
contexto de sistema não emprestam contato.

Só vale para cobranças NOVAS: ``payer_from_org`` roda na criação da cobrança (``create_payment_order`` /
``ensure_payer_supported``); campanha já paga não passa por aqui.
"""
import pytest

from app.core.authz import AuthContext, load_grants
from app.core.errors import DomainError
from app.modules.financial import payments
from tests.factories import make_user
from tests.integration.org_api.helpers import mk_org


async def _ctx(db, user):
    return AuthContext(kind="user", user=user, grants=await load_grants(db, user.id))


async def test_equipe_rd_nao_empresta_email_nem_telefone_ao_pagador(db):
    org = await mk_org(db, roles=("ADVERTISER",))  # empresa antiga: sem e-mail e sem telefone
    org.phone = "(61) 99999-0000"
    await db.commit()
    admin = await make_user(db, role="admin", mfa=True, phone="5562988887777")
    with pytest.raises(DomainError) as e:
        await payments.payer_from_org(db, org.id, await _ctx(db, admin))
    # antes: devolvia o pagador com o e-mail do admin de teste
    assert e.value.code == "PAYER_DATA_INCOMPLETE" and e.value.details["missing"] == ["email"]

    org2 = await mk_org(db, roles=("ADVERTISER",))
    org2.email = "financeiro@org2.com.br"
    await db.commit()
    with pytest.raises(DomainError) as e:
        await payments.payer_from_org(db, org2.id, await _ctx(db, admin))
    assert e.value.details["missing"] == ["phone"]  # o telefone do admin também não serve


async def test_membro_da_organizacao_continua_completando_o_pagador(db):
    org = await mk_org(db, roles=("ADVERTISER",))
    member = await make_user(db, role="advertiser", scope=("ORGANIZATION", org.id), phone="5562988887777")
    payer = await payments.payer_from_org(db, org.id, await _ctx(db, member))
    assert payer.email == member.email and payer.phone == "62988887777"
    # membro de OUTRA organização não serve
    other = await mk_org(db, roles=("ADVERTISER",))
    stranger = await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id), phone="5562977776666")
    with pytest.raises(DomainError) as e:
        await payments.payer_from_org(db, org.id, await _ctx(db, stranger))
    assert e.value.details["missing"] == ["email", "phone"]


async def test_cadastro_completo_nao_depende_de_quem_cria_a_cobranca(db):
    org = await mk_org(db, roles=("ADVERTISER",))
    org.email, org.phone = "financeiro@completo.com.br", "(62) 3333-4444"
    await db.commit()
    admin = await make_user(db, role="admin", mfa=True)
    for ctx in (await _ctx(db, admin), AuthContext.system("system"), None):
        payer = await payments.payer_from_org(db, org.id, ctx)
        assert payer.email == "financeiro@completo.com.br" and payer.phone == "6233334444"
