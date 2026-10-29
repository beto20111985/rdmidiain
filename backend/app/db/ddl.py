"""
RD Mídia Indoor — DDL extra centralizado (funções, sequências, triggers).

Usado por:
- migrations (``op.execute(stmt)`` para cada statement), e
- harness de testes em modo rápido (``Base.metadata.create_all`` + ``apply_all``).

Todos os statements são IDEMPOTENTES (``CREATE OR REPLACE`` / ``DROP ... IF EXISTS``
+ ``CREATE``) e não contêm ``:nome`` (seguros para ``sqlalchemy.text``/``op.execute``).

Para proteger uma tabela nova como append-only (ex.: ledger), o agente de domínio:
1. adiciona o nome em ``APPEND_ONLY_TABLES`` (o harness de testes passa a aplicar);
2. registra no relatório que a próxima migration deve executar
   ``for s in ddl.append_only_trigger("<tabela>"): op.execute(s)``.

Coluna imutável (ex.: ``screens.friendly_id``, código RD do criativo): acrescente em
``IMMUTABLE_COLUMNS`` e a próxima migration executa
``ddl.immutable_columns_trigger("<tabela>", ["<coluna>"])`` (a função
``rd_forbid_column_change`` ainda NÃO está na 0003 — a próxima migration deve executar
``ddl.FORBID_COLUMN_CHANGE_FN`` antes).

Mudança no corpo de uma função exige nova migration chamando o statement de novo
(``CREATE OR REPLACE``).

AÇÃO 003 / 0008: os ``*_FN`` abaixo (e nos módulos de domínio) são HISTÓRICOS (replay das migrations
antigas); o corpo VIGENTE é o endurecido de ``hardened_function_statements()`` (``search_path`` seguro
+ referências qualificadas) — ver a seção "ENDURECIMENTO" no fim deste arquivo.
"""
from __future__ import annotations

from typing import Iterable

# Chave do advisory lock que serializa a cadeia de hash da auditoria.
AUDIT_CHAIN_LOCK_KEY = 7240010001
GENESIS_HASH = "0" * 64

FORBID_MUTATION_FN = """
CREATE OR REPLACE FUNCTION rd_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'RD_APPEND_ONLY: tabela % é somente-inserção (% proibido)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'P0001';
END;
$$
"""

# Coluna imutável: o trigger recebe os nomes das colunas protegidas em TG_ARGV.
FORBID_COLUMN_CHANGE_FN = """
CREATE OR REPLACE FUNCTION rd_forbid_column_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    col text;
    o jsonb;
    n jsonb;
BEGIN
    o := to_jsonb(OLD);
    n := to_jsonb(NEW);
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF (o -> col) IS DISTINCT FROM (n -> col) THEN
            RAISE EXCEPTION 'RD_IMMUTABLE_COLUMN: %.% não pode ser alterada', TG_TABLE_NAME, col
                USING ERRCODE = 'P0001';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$
"""

AUDIT_SEQUENCE = "CREATE SEQUENCE IF NOT EXISTS rd_audit_seq AS BIGINT"

# Material canônico do hash (independe do TimeZone da sessão).
AUDIT_HASH_MATERIAL_FN = """
CREATE OR REPLACE FUNCTION rd_audit_hash_material(r audit_logs, prev text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_array(
        prev,
        r.seq,
        (extract(epoch FROM r.created_at) * 1000000)::bigint,
        r.actor_user_id, r.real_user_id, r.actor_device_id, r.org_id,
        r.action, r.entity_type, r.entity_id,
        r.before_data, r.after_data, r.reason,
        r.ip_address, r.user_agent, r.origin, r.request_id
    )::text
$$
"""

AUDIT_CHAIN_FN = f"""
CREATE OR REPLACE FUNCTION rd_audit_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    last_hash text;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'RD_AUDIT_ISOLATION: auditoria exige READ COMMITTED (atual: %)',
            current_setting('transaction_isolation');
    END IF;
    -- serializa a cadeia: a linha anterior já está confirmada quando o lock é obtido
    PERFORM pg_advisory_xact_lock({AUDIT_CHAIN_LOCK_KEY});
    NEW.seq := nextval('rd_audit_seq');
    IF NEW.created_at IS NULL THEN
        NEW.created_at := now();
    END IF;
    SELECT a.row_hash INTO last_hash FROM audit_logs a ORDER BY a.seq DESC LIMIT 1;
    NEW.prev_hash := COALESCE(last_hash, '{GENESIS_HASH}');
    NEW.row_hash := encode(sha256(convert_to(rd_audit_hash_material(NEW, NEW.prev_hash), 'UTF8')), 'hex');
    RETURN NEW;
END;
$$
"""

AUDIT_CHAIN_TRIGGER = [
    "DROP TRIGGER IF EXISTS trg_audit_logs_chain ON audit_logs",
    "CREATE TRIGGER trg_audit_logs_chain BEFORE INSERT ON audit_logs "
    "FOR EACH ROW EXECUTE FUNCTION rd_audit_chain()",
]

# Tabelas protegidas como append-only (UPDATE/DELETE/TRUNCATE lançam exceção).
# Agentes de domínio: acrescentem aqui (ex.: "ledger_entries").
APPEND_ONLY_TABLES: list[str] = [
    "audit_logs",
    "system_setting_history",
    "consent_records",
    # W-ORG (estoque, histórico de tela, aceites de contrato, manutenção, audiência estimada)
    "equipment_movements",
    "screen_history",
    "contract_acceptances",
    "maintenance_events",
    "audience_estimates",
    # W-FIN (ledger em partidas dobradas, trilha da dupla aprovação, fechamentos)
    "ledger_transactions",
    "ledger_postings",
    "financial_approval_events",
    "financial_period_closings",
    "commission_day_plans",  # W-FIN 0006: plano diário congelado do termo de comissão
    # W-BIZ (aprovação formal de arte, históricos, pisos de preço, control plane, comprovantes)
    "design_approvals",
    "design_events",
    "lead_stage_events",
    "ticket_events",
    "rd_price_floor_history",
    "cp_events",
    "delivery_proofs",
    # W-CAMP (histórico de status de campanha, decisões de conflito explicáveis)
    "campaign_status_history",
    "conflict_decisions",
    # AÇÃO 003 (ADR_011): trilhas seladas do modo de aprovação e dos assentos de Admin Master.
    # Além do trigger, o rd_app NÃO tem nem INSERT (só as funções SECURITY DEFINER gravam).
    "instance_approval_mode_history",
    "admin_master_seat_events",
]

# Append-only em que o papel de runtime também não tem INSERT (escrita só por função definer).
DEFINER_ONLY_TABLES: frozenset[str] = frozenset({"instance_approval_mode_history", "admin_master_seat_events"})


# Extensões necessárias ao DDL de domínio (ex.: EXCLUDE com btree_gist).
EXTENSIONS: list[str] = ["btree_gist"]

# Módulos de DDL de DOMÍNIO. Cada módulo expõe ``statements(tables: set[str] | None) -> list[str]``
# com statements IDEMPOTENTES (constraint triggers DEFERRABLE, imutabilidade parcial,
# EXCLUDE btree_gist...). Aplicados pelo harness de testes (``all_statements``) e pelas
# migrations (``domain_statements``). Módulo ainda inexistente é ignorado (o domínio cria).
DOMAIN_DDL_MODULES: list[str] = [
    "app.modules.financial.ddl",
    "app.modules.inventory.ddl",
    "app.modules.organizations.ddl",
    "app.modules.designer.ddl",  # W-BIZ: versão aprovada da arte imutável
    "app.modules.security.ddl",  # AÇÃO 003: modo de aprovação, assentos de Admin Master, convites
]

# Módulos registrados DEPOIS da migration 0004 → revisão que os introduziu. A 0004 chama
# ``domain_statements(strict=True)`` SEM lista de tabelas (replay histórico a partir do zero):
# esses módulos ainda não existiam e são omitidos nessa chamada — a própria revisão os aplica.
# (Pela mesma regra, ``statements(None)`` do financeiro devolve o estado da época da 0004.)
DOMAIN_DDL_MODULES_SINCE: dict[str, str] = {
    "app.modules.security.ddl": "0007",
}

# Colunas imutáveis após o INSERT (ex.: código público RD-XXXXXX do criativo).
# Agentes de domínio: acrescentem {"tabela": ["coluna", ...]} por edição cirúrgica.
IMMUTABLE_COLUMNS: dict[str, list[str]] = {
    # W-ORG: ID público da tela TEL-UF-XXXX nunca muda; tipo/rastreio do item de estoque fixos
    "screens": ["friendly_id"],
    "equipment_items": ["type", "tracking"],
    # W-CAMP: código público RD-XXXXXX do criativo nunca muda (nova versão = novo criativo)
    "creatives": ["code"],
}


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"nome de tabela inválido: {name!r}")
    return name


def append_only_trigger(table: str) -> list[str]:
    t = _ident(table)
    return [
        f"DROP TRIGGER IF EXISTS trg_{t}_append_only ON {t}",
        f"CREATE TRIGGER trg_{t}_append_only BEFORE UPDATE OR DELETE ON {t} "
        "FOR EACH ROW EXECUTE FUNCTION rd_forbid_mutation()",
        f"DROP TRIGGER IF EXISTS trg_{t}_no_truncate ON {t}",
        f"CREATE TRIGGER trg_{t}_no_truncate BEFORE TRUNCATE ON {t} "
        "FOR EACH STATEMENT EXECUTE FUNCTION rd_forbid_mutation()",
    ]


def immutable_columns_trigger(table: str, columns: list[str]) -> list[str]:
    t = _ident(table)
    if not columns:
        raise ValueError("informe ao menos uma coluna")
    args = ", ".join("'" + _ident(c) + "'" for c in columns)
    return [
        f"DROP TRIGGER IF EXISTS trg_{t}_immutable_columns ON {t}",
        f"CREATE TRIGGER trg_{t}_immutable_columns BEFORE UPDATE ON {t} "
        f"FOR EACH ROW EXECUTE FUNCTION rd_forbid_column_change({args})",
    ]


def drop_immutable_columns_trigger(table: str) -> list[str]:
    return [f"DROP TRIGGER IF EXISTS trg_{_ident(table)}_immutable_columns ON {_ident(table)}"]


def drop_append_only_trigger(table: str) -> list[str]:
    t = _ident(table)
    return [
        f"DROP TRIGGER IF EXISTS trg_{t}_append_only ON {t}",
        f"DROP TRIGGER IF EXISTS trg_{t}_no_truncate ON {t}",
    ]


def base_functions() -> list[str]:
    """Funções que não dependem de tabelas."""
    return [FORBID_MUTATION_FN, FORBID_COLUMN_CHANGE_FN, AUDIT_SEQUENCE]


def audit_chain() -> list[str]:
    """Requer a tabela audit_logs criada."""
    return [AUDIT_HASH_MATERIAL_FN, AUDIT_CHAIN_FN, *AUDIT_CHAIN_TRIGGER]


def drop_audit_chain() -> list[str]:
    return [
        "DROP TRIGGER IF EXISTS trg_audit_logs_chain ON audit_logs",
        "DROP FUNCTION IF EXISTS rd_audit_chain()",
        "DROP FUNCTION IF EXISTS rd_audit_hash_material(audit_logs, text)",
    ]


def extension_statements() -> list[str]:
    return [f'CREATE EXTENSION IF NOT EXISTS "{_ident(e)}"' for e in EXTENSIONS]


def domain_statements(existing_tables: Iterable[str] | None = None, *, strict: bool = False) -> list[str]:
    """Statements dos módulos de DDL de domínio (``DOMAIN_DDL_MODULES``).

    ``strict=True`` (migrations): módulo ausente é erro. ``False`` (harness): ignorado.
    ``existing_tables=None`` (só a 0004): módulos de ``DOMAIN_DDL_MODULES_SINCE`` são omitidos.
    """
    import importlib

    tables = set(existing_tables) if existing_tables is not None else None
    out: list[str] = []
    for mod_name in DOMAIN_DDL_MODULES:
        if tables is None and mod_name in DOMAIN_DDL_MODULES_SINCE:
            continue
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError as exc:
            missing_self = exc.name is not None and (mod_name == exc.name or mod_name.startswith(exc.name + "."))
            if strict or not missing_self:
                raise  # erro de import DENTRO do módulo de domínio nunca é engolido
            continue
        fn = getattr(mod, "statements", None)
        if fn is None:
            raise AttributeError(f"{mod_name} não expõe statements(tables)")
        out += list(fn(tables))
    return out


# ═════════════════════════════════════════════════════════════════════════════
# ENDURECIMENTO DAS FUNÇÕES (AÇÃO 003, migration 0008)
#
# Ameaça: o papel de runtime (``rd_app``) tem TEMPORARY. Quando ``pg_temp`` não está listado no
# ``search_path`` ele é pesquisado PRIMEIRO para tabelas/sequências/TIPOS (nunca para funções);
# uma tabela temporária ``financial_period_closings`` vazia, por exemplo, fazia um guard
# SECURITY INVOKER com nome não qualificado aceitar lançamento em período fechado.
#
# Regra (vale para TODA função em ``public``; teste de inventário em
# ``tests/adversarial/test_sec_db_temp_shadow.py`` falha se surgir função fora dela):
#   1. ``SET search_path = pg_catalog, public, pg_temp`` (pg_temp por ÚLTIMO);
#   2. toda referência a tabela/sequência qualificada (``public.``), inclusive ``nextval('public.…')``.
#
# Os constantes de função acima e nos módulos de domínio são HISTÓRICOS: as migrations 0003/0004/0007
# os referenciam por nome (replay a partir do zero) — NÃO editar. O corpo VIGENTE de cada função é o
# devolvido por ``hardened_function_statements()`` (aplicado pela 0008 e, no fim, pelo harness).
# Função nova deve nascer já no padrão (e então pode entrar diretamente no ``statements`` do módulo).
# ═════════════════════════════════════════════════════════════════════════════

SAFE_SEARCH_PATH = "pg_catalog, public, pg_temp"


def harden_function(sql: str, replacements: Iterable[tuple[str, str]] = ()) -> str:
    """Versão endurecida de um ``CREATE OR REPLACE FUNCTION``: acrescenta o ``SET search_path`` seguro
    antes de ``AS $$`` e aplica as qualificações explícitas ``(trecho, trecho qualificado)``.

    Cada trecho precisa existir no corpo (sentinela: se o corpo histórico mudar, falha alto em vez de
    deixar uma referência sem qualificação)."""
    head, sep, rest = sql.partition(" AS $$")
    if not sep or "CREATE OR REPLACE FUNCTION" not in head:
        raise ValueError("statement não é um CREATE OR REPLACE FUNCTION ... AS $$")
    if "SET search_path" in head:
        raise ValueError("função já define search_path")
    body = rest
    for old, new in replacements:
        if old not in body:
            raise ValueError(f"trecho a qualificar ausente: {old!r}")
        body = body.replace(old, new)
    return f"{head}\nSET search_path = {SAFE_SEARCH_PATH} AS $${body}"


def hardened_statements(existing_tables: Iterable[str] | None = None) -> list[str]:
    """Corpos VIGENTES (0008) das funções de base: append-only, coluna imutável e cadeia de auditoria."""
    tables = set(existing_tables) if existing_tables is not None else None
    out = [harden_function(FORBID_MUTATION_FN), harden_function(FORBID_COLUMN_CHANGE_FN)]
    if tables is None or "audit_logs" in tables:
        out += [
            harden_function(AUDIT_HASH_MATERIAL_FN),
            harden_function(AUDIT_CHAIN_FN, [
                ("nextval('rd_audit_seq')", "nextval('public.rd_audit_seq')"),
                ("FROM audit_logs a", "FROM public.audit_logs a"),
                ("encode(sha256(convert_to(rd_audit_hash_material(", "encode(sha256(convert_to(public.rd_audit_hash_material("),
            ]),
        ]
    return out


def hardened_function_statements(existing_tables: Iterable[str] | None = None) -> list[str]:
    """Todos os corpos VIGENTES endurecidos: base + cada módulo de ``DOMAIN_DDL_MODULES`` que expõe
    ``hardened_statements(tables)``. Só ``CREATE OR REPLACE FUNCTION`` (triggers e ACLs intactos)."""
    import importlib

    tables = set(existing_tables) if existing_tables is not None else None
    out = hardened_statements(tables)
    for mod_name in DOMAIN_DDL_MODULES:
        fn = getattr(importlib.import_module(mod_name), "hardened_statements", None)
        if fn is not None:
            out += list(fn(tables))
    return out


def all_statements(existing_tables: Iterable[str] | None = None) -> list[str]:
    """Tudo que o harness de testes aplica após ``create_all`` (estado do HEAD)."""
    tables = set(existing_tables) if existing_tables is not None else None
    stmts = extension_statements() + base_functions() + audit_chain()
    for t in APPEND_ONLY_TABLES:
        if tables is None or t in tables:
            stmts += append_only_trigger(t)
    for t, cols in IMMUTABLE_COLUMNS.items():
        if cols and (tables is None or t in tables):
            stmts += immutable_columns_trigger(t, cols)
    stmts += domain_statements(tables)
    stmts += hardened_function_statements(tables)  # 0008: corpos vigentes por último
    return stmts


def apply_all_sync(connection) -> None:
    """Aplica ``all_statements`` numa Connection síncrona (SQLAlchemy)."""
    from sqlalchemy import inspect, text

    tables = set(inspect(connection).get_table_names())
    for stmt in all_statements(tables):
        connection.execute(text(stmt))
