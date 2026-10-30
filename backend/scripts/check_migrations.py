"""
RD Mídia Indoor — verificador de migrations compatíveis com a versão anterior (AÇÃO 007).

Deploy sem queda = as duas versões do backend convivem por alguns segundos com o MESMO
banco. Uma migration só pode EXPANDIR (adicionar tabela, coluna anulável ou com default,
índice, constraint nova) — a versão anterior continua funcionando. Operações que quebram a
versão anterior só na fase CONTRAIR, depois que nenhum código no ar usa o que vai sair:

    RD_MIGRATION_PHASE = "contract"   # no módulo da migration, com o motivo no docstring

Uso (CI, só migrations novas/alteradas):  python backend/scripts/check_migrations.py <arquivos…>
Saída 0 = ok; 1 = migration incompatível sem a marcação; 2 = uso/erro de leitura.
Análise estática (ast): não importa a aplicação nem conecta ao banco.
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DESTRUCTIVE_OPS = {
    "drop_table": "remove tabela (a versão anterior ainda a usa)",
    "drop_column": "remove coluna (a versão anterior ainda a lê/grava)",
    "rename_table": "renomeia tabela (a versão anterior usa o nome antigo)",
}
SQL_PATTERNS = [
    (re.compile(r"\bDROP\s+(TABLE|COLUMN)\b", re.I), "SQL remove tabela/coluna"),
    (re.compile(r"\bRENAME\s+(TO|COLUMN)\b", re.I), "SQL renomeia tabela/coluna"),
    (re.compile(r"\bALTER\s+COLUMN\b[^;]*\b(TYPE|SET\s+NOT\s+NULL)\b", re.I), "SQL troca tipo / impõe NOT NULL"),
]


@dataclass
class Finding:
    file: str
    line: int
    reason: str


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _is_false(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _op_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "op":
        return f.attr
    return None


def _literal_sql(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and node.args:  # sa.text("…")
        return _literal_sql(node.args[0])
    return None


def _phase(tree: ast.Module) -> str | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RD_MIGRATION_PHASE" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def check_source(source: str, filename: str = "<migration>") -> tuple[list[Finding], str | None]:
    tree = ast.parse(source, filename=filename)
    upgrade = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None)
    findings: list[Finding] = []
    if upgrade is None:
        return findings, _phase(tree)
    for call in (n for n in ast.walk(upgrade) if isinstance(n, ast.Call)):
        name = _op_name(call)
        if name in DESTRUCTIVE_OPS:
            findings.append(Finding(filename, call.lineno, f"op.{name}: {DESTRUCTIVE_OPS[name]}"))
        elif name == "alter_column":
            if _kw(call, "new_column_name") is not None:
                findings.append(Finding(filename, call.lineno, "op.alter_column(new_column_name=…): renomeia coluna"))
            if _kw(call, "type_") is not None:
                findings.append(Finding(filename, call.lineno, "op.alter_column(type_=…): troca o tipo da coluna"))
            if _is_false(_kw(call, "nullable")):
                findings.append(Finding(filename, call.lineno, "op.alter_column(nullable=False): impõe NOT NULL em coluna existente"))
        elif name == "add_column":
            col = call.args[1] if len(call.args) > 1 else _kw(call, "column")
            if isinstance(col, ast.Call) and _is_false(_kw(col, "nullable")) and _kw(col, "server_default") is None:
                findings.append(Finding(filename, call.lineno,
                                        "op.add_column NOT NULL sem server_default: INSERT da versão anterior falha"))
        elif name == "execute":
            sql = _literal_sql(call.args[0]) if call.args else None
            if sql is None:
                findings.append(Finding(filename, call.lineno, "op.execute com SQL dinâmico: não verificável"))
            else:
                findings += [Finding(filename, call.lineno, f"op.execute: {why}") for rx, why in SQL_PATTERNS if rx.search(sql)]
    return findings, _phase(tree)


def main(argv: list[str]) -> int:
    if not argv:
        print("uso: check_migrations.py <arquivo de migration>…", file=sys.stderr)
        return 2
    blocked = 0
    for path in argv:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERRO: não foi possível ler {path}: {exc}", file=sys.stderr)
            return 2
        findings, phase = check_source(source, path)
        if not findings:
            print(f"ok        {path} (só expande)")
        elif phase == "contract":
            print(f"contract  {path}: {len(findings)} operação(ões) de contração declaradas (RD_MIGRATION_PHASE)")
            for f in findings:
                print(f"            linha {f.line}: {f.reason}")
        else:
            blocked += 1
            print(f"BLOQUEADA {path}: incompatível com a versão anterior (deploy sem queda)")
            for f in findings:
                print(f"            linha {f.line}: {f.reason}")
    if blocked:
        print("\nExpandir → implantar → contrair: faça a mudança em duas migrations/deploys. Se esta já é a "
              "fase de contração (nenhum código no ar usa o que sai), declare RD_MIGRATION_PHASE = \"contract\" "
              "com o motivo no docstring.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
