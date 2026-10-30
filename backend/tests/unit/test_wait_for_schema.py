"""Portão de schema do deploy sem queda (AÇÃO 007): decisão pura, sem banco."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "wait_for_schema", Path(__file__).resolve().parents[2] / "scripts" / "wait_for_schema.py"
)
wfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wfs)

KNOWN = {"0007", "0008", "0009"}
HEADS = {"0009"}


def test_banco_no_head_segue():
    assert wfs.decide({"0009"}, HEADS, KNOWN) == wfs.READY


def test_banco_atras_espera_migracao():
    assert wfs.decide({"0008"}, HEADS, KNOWN) == wfs.WAIT


def test_banco_vazio_espera():
    assert wfs.decide(set(), HEADS, KNOWN) == wfs.WAIT


def test_banco_a_frente_da_imagem_segue_para_permitir_rollback():
    # imagem antiga (head 0009) com o banco já em 0010: não pode travar o rollback
    assert wfs.decide({"0010"}, HEADS, KNOWN) == wfs.AHEAD


def test_sem_comando_retorna_erro_de_uso(capsys):
    assert wfs.main([]) == 2
    assert "uso:" in capsys.readouterr().err
