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

HEAD = "from alembic import op\nimport sqlalchemy as sa\n"


def reasons(body: str, prelude: str = HEAD) -> list[str]:
    src = prelude + "\ndef upgrade():\n" + body + "\n\ndef downgrade():\n    op.drop_table('x')\n"
    return [f.reason for f in cm.check_source(src)[0]]


def sql(statement: str) -> list[str]:
    return reasons(f"    op.execute({statement!r})")


def test_expandir_passa():
    body = (
        "    op.create_table('t', sa.Column('id', sa.Integer, primary_key=True))\n"
        "    op.add_column('u', sa.Column('apelido', sa.String(), nullable=True))\n"
        "    op.add_column('u', sa.Column('ativo', sa.Boolean(), nullable=False, server_default=sa.true()))\n"
        "    op.create_index('ix_u_apelido', 'u', ['apelido'])"
    )
    assert reasons(body) == []


@pytest.mark.parametrize("statement", [
    "CREATE INDEX ix2 ON u (id)",
    "CREATE TABLE z (id int NOT NULL)",
    "ALTER TABLE t ADD COLUMN y int NOT NULL DEFAULT 0",
    "ALTER TABLE t ALTER COLUMN x DROP NOT NULL",
    "ALTER TABLE t DROP CONSTRAINT ck_x",
    "DROP INDEX ix_old",
    "CREATE OR REPLACE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql",
])
def test_sql_que_so_expande_passa(statement):
    assert sql(statement) == []


@pytest.mark.parametrize("body, trecho", [
    ("    op.drop_column('u', 'nome')", "remove coluna"),
    ("    op.drop_table('u')", "remove tabela"),
    ("    op.rename_table('u', 'usuarios')", "renomeia tabela"),
    ("    op.alter_column('u', 'nome', new_column_name='nome_completo')", "renomeia coluna"),
    ("    op.alter_column('u', 'idade', type_=sa.BigInteger())", "troca o tipo"),
    ("    op.alter_column('u', 'email', nullable=False)", "NOT NULL"),
    ("    op.alter_column('u', 'ativo', server_default=None)", "remove o default"),
    ("    op.add_column('u', sa.Column('cpf', sa.String(), nullable=False))", "sem server_default"),
    ("    op.add_column('u', sa.Column('cpf', sa.String(), nullable=False, server_default=None))", "sem server_default"),
    ("    op.add_column('u', sa.Column('id2', sa.Integer(), primary_key=True))", "sem server_default"),
    # desvios apontados pela revisão adversarial
    ("    with op.batch_alter_table('u') as b:\n        b.drop_column('nome')", "remove coluna"),
    ("    op.get_bind().execute(sa.text('DROP TABLE u'))", "não verificável"),
    ("    sql = 'DROP TABLE u'\n    op.execute(sql)", "não verificável"),
    ("    op.execute(f'DROP TABLE {nome}')", "não verificável"),
])
def test_operacoes_que_quebram_a_versao_anterior_sao_detectadas(body, trecho):
    found = reasons(body)
    assert any(trecho in r for r in found), found


@pytest.mark.parametrize("statement", [
    "ALTER TABLE u DROP COLUMN nome",
    "ALTER TABLE u DROP nome",
    "ALTER TABLE u RENAME a TO b",
    "ALTER TABLE u RENAME TO usuarios",
    "ALTER TABLE u ALTER x TYPE bigint",
    "ALTER TABLE u ALTER COLUMN x SET NOT NULL",
    "ALTER TABLE u ALTER COLUMN x DROP DEFAULT",
    "ALTER TABLE u ADD COLUMN y int NOT NULL",
    "TRUNCATE u",
    "DROP VIEW v",
    "DROP FUNCTION f()",
    "CREATE INDEX i ON u (x); DROP TABLE u",
])
def test_sql_destrutivo_e_detectado(statement):
    assert sql(statement), statement


def test_alias_de_op_e_helper_fora_de_upgrade_sao_detectados():
    src = ("from alembic import op as o\n\ndef _limpa():\n    o.drop_table('u')\n\n"
           "def upgrade():\n    _limpa()\n\ndef downgrade():\n    pass\n")
    assert any("remove tabela" in f.reason for f in cm.check_source(src)[0])


def test_downgrade_nao_conta():
    assert reasons("    op.add_column('u', sa.Column('x', sa.String(), nullable=True))") == []


def test_marcacao_anotada_e_reconhecida():
    src = HEAD + 'RD_MIGRATION_PHASE: str = "contract"\n\ndef upgrade():\n    op.drop_column("u", "x")\n'
    assert cm.check_source(src)[1] == "contract"


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


def test_all_aceita_o_baseline_e_verifica_o_resto(tmp_path, monkeypatch, capsys):
    # migrations reais do projeto: todas no baseline, portão passa
    assert cm.main(["--all"]) == 0
    capsys.readouterr()
    # cópia de uma migration do baseline EDITADA deixa de ser baseline e é verificada
    real = cm.VERSIONS_DIR / "0003_auth_rbac_audit_foundation.py"
    (tmp_path / real.name).write_text(cm._normalized(real) + "\n# editada\n", encoding="utf-8")
    (tmp_path / "0010_nova.py").write_text(HEAD + "def upgrade():\n    op.drop_table('u')\n", encoding="utf-8")
    monkeypatch.setattr(cm, "VERSIONS_DIR", tmp_path)
    assert cm.main(["--all"]) == 1
    out = capsys.readouterr().out
    assert "BLOQUEADA 0010_nova.py" in out and "base      0003" not in out
