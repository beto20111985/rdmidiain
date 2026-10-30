"""
RD Mídia Indoor — verificador de migrations compatíveis com a versão anterior (AÇÃO 007).

Deploy sem queda = as duas versões do backend convivem por alguns segundos com o MESMO
banco. Uma migration só pode EXPANDIR (tabela nova, coluna anulável ou com default, índice,
constraint nova) — a versão anterior continua funcionando. O que quebra a versão anterior só
na fase CONTRAIR, depois que nenhum código no ar usa o que vai sair:

    RD_MIGRATION_PHASE = "contract"   # no módulo da migration, com o motivo no docstring

Uso:
    python backend/scripts/check_migrations.py --all            # CI: TODAS as migrations
    python backend/scripts/check_migrations.py <arquivos…>      # arquivos específicos

--all verifica todo backend/migrations/versions/*.py; as migrations ANTERIORES ao deploy sem
queda (BASELINE) são aceitas só enquanto o conteúdo for idêntico ao registrado (sha256) —
editar uma delas também passa pela verificação. Assim o portão não depende do diff do push
(workflow_dispatch, force-push, push seguinte não "escondem" uma migration).

Saída 0 = ok; 1 = migration incompatível sem a marcação; 2 = uso/erro de leitura.
Análise estática (ast): não importa a aplicação nem conecta ao banco. Na dúvida, bloqueia
(SQL dinâmico ou executado por outro caminho = "não verificável").
"""
from __future__ import annotations

import ast
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"

# Migrations aplicadas ANTES do deploy sem queda (AÇÃO 007): aceitas como estão (sha256 do
# conteúdo com fim de linha LF). Não acrescentar migrations novas aqui.
BASELINE = {
    "001_initial_schema.py": "61f8dd03105e0d99bc2e595c54767d9ce0aacbb502d6fd199aceac55bab188d8",
    "d058863a802f_add_is_super_admin_to_users.py": "1a7477bcfc7c51e87fccf4d1f840e699e5b9d246000d1d3e55b656df21950d2f",
    "0003_auth_rbac_audit_foundation.py": "87ed57f2adbac9a3f022db0bfdaa0b75a3df23d03b9640ee1d09be60375fc745",
    "0004_domain_consolidation.py": "3a8827c77319fa21bc91668543929d54506206b29539d3dd613253ecca3b4be7",
    "0005_system_seeds.py": "845e6fe0d71570555873f4a71780031483a70806bbe41d1e22675677b26a932f",
    "0006_commission_terms_per_payment.py": "9178586453c96583785302c8f6f4f01c19986d246810eff86be819d4417598a9",
    "0007_master_bootstrap_approval_mode.py": "68b82e5353ca4dfcf4a13d5e8c60ac1b1fd5e1d295c6c10cfe7fc8353a83d5d7",
    "0008_function_search_path_hardening.py": "4ac3a6a0c9e345909bf5243791c3bf00343d3e5da3c50bd50979f9e2093332b9",
    "0009_master_credential_seal.py": "363b45e95997481c83fa411e20fe1dd07d4683de6610f1672763d642c8832e15",
}

# Métodos destrutivos em QUALQUER receptor: op.*, batch_op.* (batch_alter_table), alias de op…
DESTRUCTIVE_METHODS = {
    "drop_table": "remove tabela (a versão anterior ainda a usa)",
    "drop_column": "remove coluna (a versão anterior ainda a lê/grava)",
    "rename_table": "renomeia tabela (a versão anterior usa o nome antigo)",
}
# Execução de SQL por caminhos que não dá para analisar com segurança.
UNVERIFIABLE_METHODS = {"get_bind", "exec_driver_sql", "get_context", "bulk_insert_raw"}

_STATEMENT_RULES = [
    (re.compile(r"^\s*TRUNCATE\b", re.I), "SQL TRUNCATE apaga dados"),
    (re.compile(r"^\s*DROP\s+(?!INDEX\b)", re.I), "SQL DROP remove objeto que a versão anterior pode usar"),
    (re.compile(r"^\s*ALTER\s+\w+.*\bRENAME\b", re.I | re.S), "SQL renomeia objeto/coluna"),
    (re.compile(r"^\s*ALTER\s+TABLE\b.*\bDROP\b(?!\s+CONSTRAINT\b|\s+INDEX\b|\s+NOT\s+NULL\b)", re.I | re.S),
     "SQL ALTER TABLE … DROP (coluna/default)"),
    (re.compile(r"^\s*ALTER\s+TABLE\b.*\bTYPE\b", re.I | re.S), "SQL troca o tipo de coluna"),
    (re.compile(r"^\s*ALTER\s+TABLE\b.*\bSET\s+NOT\s+NULL\b", re.I | re.S), "SQL impõe NOT NULL em coluna existente"),
    (re.compile(r"^\s*ALTER\s+TABLE\b.*\bADD\b(?:\s+COLUMN)?\b(?:(?!\bDEFAULT\b).)*\bNOT\s+NULL\b(?:(?!\bDEFAULT\b).)*$", re.I | re.S),
     "SQL adiciona coluna NOT NULL sem DEFAULT (INSERT da versão anterior falha)"),
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


def _is_none(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _has_value(node) -> bool:  # keyword presente e diferente de None literal
    return node is not None and not _is_none(node)


def _method(call: ast.Call) -> str | None:
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def _literal_sql(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and node.args:  # sa.text("…")
        return _literal_sql(node.args[0])
    return None


def _sql_findings(sql: str) -> list[str]:
    reasons = []
    for stmt in (s for s in sql.split(";") if s.strip()):
        # remove comentários de linha antes de analisar
        clean = "\n".join(l.split("--", 1)[0] for l in stmt.splitlines())
        reasons += [why for rx, why in _STATEMENT_RULES if rx.search(clean)]
    return reasons


def _phase(tree: ast.Module) -> str | None:
    for node in tree.body:
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if any(isinstance(t, ast.Name) and t.id == "RD_MIGRATION_PHASE" for t in targets):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return None


def _nodes_outside_downgrade(tree: ast.Module):
    """Todo o módulo (upgrade, helpers, nível de módulo) EXCETO o corpo de downgrade()."""
    skip = {id(n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "downgrade"}
    stack = [n for n in tree.body if id(n) not in skip]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(ast.iter_child_nodes(node))


def check_source(source: str, filename: str = "<migration>") -> tuple[list[Finding], str | None]:
    tree = ast.parse(source, filename=filename)
    findings: list[Finding] = []
    for call in (n for n in _nodes_outside_downgrade(tree) if isinstance(n, ast.Call)):
        name = _method(call)
        add = lambda why: findings.append(Finding(filename, call.lineno, why))  # noqa: E731
        if name in DESTRUCTIVE_METHODS:
            add(f"{name}: {DESTRUCTIVE_METHODS[name]}")
        elif name in UNVERIFIABLE_METHODS:
            add(f"{name}: SQL executado por caminho não verificável")
        elif name == "alter_column":
            if _kw(call, "new_column_name") is not None:
                add("alter_column(new_column_name=…): renomeia coluna")
            if _kw(call, "type_") is not None:
                add("alter_column(type_=…): troca o tipo da coluna")
            if _is_false(_kw(call, "nullable")):
                add("alter_column(nullable=False): impõe NOT NULL em coluna existente")
            if _is_none(_kw(call, "server_default")):
                add("alter_column(server_default=None): remove o default (INSERT da versão anterior pode falhar)")
        elif name == "add_column":
            col = call.args[1] if len(call.args) > 1 else _kw(call, "column")
            if isinstance(col, ast.Call):
                not_null = _is_false(_kw(col, "nullable")) or (
                    isinstance(_kw(col, "primary_key"), ast.Constant) and _kw(col, "primary_key").value is True)
                if not_null and not _has_value(_kw(col, "server_default")):
                    add("add_column NOT NULL sem server_default: INSERT da versão anterior falha")
        elif name == "execute":
            sql = _literal_sql(call.args[0]) if call.args else None
            if sql is None:
                add("execute com SQL dinâmico: não verificável")
            else:
                for why in _sql_findings(sql):
                    add(f"execute: {why}")
    return findings, _phase(tree)


def _normalized(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def main(argv: list[str]) -> int:
    if not argv:
        print("uso: check_migrations.py --all | <arquivo de migration>…", file=sys.stderr)
        return 2
    if argv == ["--all"]:
        paths = sorted(VERSIONS_DIR.glob("*.py"))
    else:
        paths = [Path(a) for a in argv]
    blocked = 0
    for path in paths:
        try:
            source = _normalized(path)
        except OSError as exc:
            print(f"ERRO: não foi possível ler {path}: {exc}", file=sys.stderr)
            return 2
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if BASELINE.get(path.name) == digest:
            print(f"base      {path.name} (anterior ao deploy sem queda, conteúdo inalterado)")
            continue
        findings, phase = check_source(source, str(path))
        if not findings:
            print(f"ok        {path.name} (só expande)")
        elif phase == "contract":
            print(f"contract  {path.name}: {len(findings)} operação(ões) de contração declaradas (RD_MIGRATION_PHASE)")
            for f in findings:
                print(f"            linha {f.line}: {f.reason}")
        else:
            blocked += 1
            print(f"BLOQUEADA {path.name}: incompatível com a versão anterior (deploy sem queda)")
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
