"""Verificador de migrations compatíveis com a versão anterior (AÇÃO 007, deploy sem queda)."""
import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_migrations", Path(__file__).resolve().parents[2] / "scripts" / "check_migrations.py"
)
cm = importlib.util.module_from_spec(_spec)
sys.modules["check_migrations"] = cm  # @dataclass resolve o módulo por nome
_spec.loader.exec_module(cm)

HEAD = 'from alembic import op\nimport sqlalchemy as sa\n'


def reasons(body: str, extra: str = "") -> list[str]:
    findings, _ = cm.check_source(HEAD + extra + "\ndef upgrade():\n" + body + "\n\ndef downgrade():\n    op.drop_table('x')\n")
    return [f.reason for f in findings]


def test_expandir_passa():
    body = (
        "    op.create_table('t', sa.Column('id', sa.Integer, primary_key=True))\n"
        "    op.add_column('u', sa.Column('apelido', sa.String(), nullable=True))\n"
        "    op.add_column('u', sa.Column('ativo', sa.Boolean(), nullable=False, server_default=sa.true()))\n"
        "    op.create_index('ix_u_apelido', 'u', ['apelido'])\n"
        "    op.execute(\"CREATE INDEX ix2 ON u (id)\")"
    )
    assert reasons(body) == []


@pytest.mark.parametrize("body, trecho", [
    ("    op.drop_column('u', 'nome')", "remove coluna"),
    ("    op.drop_table('u')", "remove tabela"),
    ("    op.rename_table('u', 'usuarios')", "renomeia tabela"),
    ("    op.alter_column('u', 'nome', new_column_name='nome_completo')", "renomeia coluna"),
    ("    op.alter_column('u', 'idade', type_=sa.BigInteger())", "troca o tipo"),
    ("    op.alter_column('u', 'email', nullable=False)", "NOT NULL"),
    ("    op.add_column('u', sa.Column('cpf', sa.String(), nullable=False))", "sem server_default"),
    ("    op.execute('ALTER TABLE u DROP COLUMN nome')", "remove tabela/coluna"),
    ("    op.execute(sa.text('ALTER TABLE u RENAME COLUMN a TO b'))", "renomeia"),
    ("    op.execute('ALTER TABLE u ALTER COLUMN x SET NOT NULL')", "NOT NULL"),
    ("    sql = 'DROP TABLE u'\n    op.execute(sql)", "não verificável"),
])
def test_operacoes_que_quebram_a_versao_anterior_sao_detectadas(body, trecho):
    assert any(trecho in r for r in reasons(body)), reasons(body)


def test_downgrade_nao_conta():
    assert reasons("    op.add_column('u', sa.Column('x', sa.String(), nullable=True))") == []


def test_cli_bloqueia_sem_marcacao_e_libera_contract(tmp_path, capsys):
    ruim = tmp_path / "0010_drop.py"
    ruim.write_text(HEAD + "def upgrade():\n    op.drop_column('u', 'nome')\n", encoding="utf-8")
    assert cm.main([str(ruim)]) == 1
    assert "BLOQUEADA" in capsys.readouterr().out
    contrato = tmp_path / "0011_contract.py"
    contrato.write_text('"""Contrair: coluna nome sem uso desde a 1.4."""\n' + HEAD
                        + 'RD_MIGRATION_PHASE = "contract"\n\ndef upgrade():\n    op.drop_column("u", "nome")\n',
                        encoding="utf-8")
    assert cm.main([str(contrato)]) == 0
    assert "contract" in capsys.readouterr().out


def test_migrations_do_projeto_sao_analisaveis():
    # o verificador precisa ler todas as migrations reais sem erro de sintaxe/estrutura
    versions = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    for f in sorted(versions.glob("*.py")):
        cm.check_source(f.read_text(encoding="utf-8"), str(f))
