"""Máquina de estados de campanha (P001 §39; A6 §10.5.1) — tabela de transições.

Reescrito pelo W-CAMP (AÇÃO 002): status canônicos em PT; enums de playback/inventário
saíram daqui (cobertos pelos testes dos donos W-PLAY/W-INV).
"""
import itertools

from app.models.campaigns import CampaignStatus
from app.modules.campaigns import states as ST

PT = {"RASCUNHO", "AGUARDANDO_PAGAMENTO", "AGUARDANDO_MIDIA", "EM_ANALISE", "APROVADA", "AGENDADA",
      "SINCRONIZANDO", "ATIVA", "PAUSADA", "FINALIZADA", "REJEITADA", "CANCELADA"}


def test_status_canonicos_em_portugues_sem_reproduzindo():
    assert {s.value for s in CampaignStatus} == PT
    assert "REPRODUZINDO" not in {s.value for s in CampaignStatus}  # derivado de PoP, não armazenado
    assert not {"DRAFT", "APPROVED", "ACTIVE", "IN_REVIEW"} & {s.value for s in CampaignStatus}


def test_todas_as_arestas_usam_status_validos():
    for f, t in ST.ALL_EDGES:
        assert f in PT and t in PT


def test_terminais_nao_tem_saida():
    for f, _ in ST.ALL_EDGES:
        assert f not in ST.TERMINAL


def test_rejeicao_e_aprovacao_somente_de_em_analise():
    for s in PT:
        assert ST.is_allowed("reject", s, "REJEITADA") == (s == "EM_ANALISE")
        assert ST.is_allowed("approve", s, "APROVADA") == (s == "EM_ANALISE")


def test_checkout_somente_de_rascunho_ou_rejeitada():
    froms = {f for f, _ in ST.TRANSITIONS["checkout"]}
    assert froms == {"RASCUNHO", "REJEITADA"}
    assert {t for _, t in ST.TRANSITIONS["checkout"]} == {"AGUARDANDO_PAGAMENTO"}


def test_pagamento_nunca_sai_de_estado_operacional():
    for f, t in ST.TRANSITIONS["payment_confirmed"]:
        assert f in {"AGUARDANDO_PAGAMENTO", "RASCUNHO"}  # RASCUNHO = pagamento tardio após expirar a reserva
        assert t in {"AGUARDANDO_MIDIA", "EM_ANALISE", "APROVADA"}


def test_ativa_so_por_ack_ou_retomada():
    into_active = {trig for trig, edges in ST.TRANSITIONS.items() if any(t == "ATIVA" for _, t in edges)}
    assert into_active == {"ack_active", "resume", "change_rejected"}


def test_finalizada_somente_de_estados_operacionais():
    assert {f for f, _ in ST.TRANSITIONS["expired"]} == {"APROVADA", "AGENDADA", "SINCRONIZANDO", "ATIVA", "PAUSADA"}


def test_alteracao_pos_aprovacao_volta_para_analise():
    for s in ("APROVADA", "AGENDADA", "SINCRONIZANDO", "ATIVA", "PAUSADA"):
        assert ST.is_allowed("change_requested", s, "EM_ANALISE")
    assert not ST.is_allowed("change_requested", "RASCUNHO", "EM_ANALISE")


def test_transicoes_invalidas_exemplos():
    invalid = [("approve", "RASCUNHO", "APROVADA"), ("approve", "AGUARDANDO_PAGAMENTO", "APROVADA"),
               ("reject", "ATIVA", "REJEITADA"), ("pause", "RASCUNHO", "PAUSADA"), ("resume", "ATIVA", "ATIVA"),
               ("cancel", "FINALIZADA", "CANCELADA"), ("cancel", "CANCELADA", "CANCELADA"),
               ("mark_syncing", "EM_ANALISE", "SINCRONIZANDO"), ("ack_active", "PAUSADA", "ATIVA")]
    for trig, f, t in invalid:
        assert not ST.is_allowed(trig, f, t), (trig, f, t)


def test_matriz_completa_somente_arestas_declaradas():
    declared = ST.ALL_EDGES
    for f, t in itertools.product(PT, PT):
        allowed_any = any(ST.is_allowed(trig, f, t) for trig in ST.TRANSITIONS)
        assert allowed_any == ((f, t) in declared)
