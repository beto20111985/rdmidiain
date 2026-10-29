"""DDL extra do domínio de design (aplicado pelo harness via ``app.db.ddl.DOMAIN_DDL_MODULES``
e, na migration 0004, por ``ddl.domain_statements``).

``design_versions``: o CONTEÚDO de uma versão (arquivo, sha256, número, pedido) nunca
muda; uma versão APROVADA (``locked``) não pode ser alterada nem apagada — qualquer
mudança exige NOVA versão e NOVA aprovação (RESPOSTAS RODRIGO #386–#387).
"""
from __future__ import annotations

from typing import Optional

DESIGN_VERSION_GUARD_FN = """
CREATE OR REPLACE FUNCTION rd_design_version_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.locked THEN
            RAISE EXCEPTION 'RD_DESIGN_VERSION_LOCKED: versão % aprovada é imutável', OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.locked THEN
        RAISE EXCEPTION 'RD_DESIGN_VERSION_LOCKED: versão % aprovada é imutável', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.storage_key IS DISTINCT FROM OLD.storage_key OR NEW.sha256 IS DISTINCT FROM OLD.sha256
       OR NEW.number IS DISTINCT FROM OLD.number OR NEW.request_id IS DISTINCT FROM OLD.request_id
       OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes OR NEW.content_type IS DISTINCT FROM OLD.content_type
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'RD_DESIGN_VERSION_IMMUTABLE: conteúdo da versão % não pode mudar', OLD.id USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""


def hardened_statements(tables: Optional[set] = None) -> list[str]:
    """0008 (AÇÃO 003): corpo VIGENTE com ``search_path`` seguro; ``DESIGN_VERSION_GUARD_FN`` é HISTÓRICO
    (replay da 0004). Ver ``app.db.ddl.harden_function``."""
    from app.db.ddl import harden_function

    if tables is not None and "design_versions" not in tables:
        return []
    return [harden_function(DESIGN_VERSION_GUARD_FN)]


def statements(tables: Optional[set] = None) -> list[str]:
    if tables is not None and "design_versions" not in tables:
        return []
    return [
        DESIGN_VERSION_GUARD_FN,
        "DROP TRIGGER IF EXISTS trg_design_versions_guard ON design_versions",
        "CREATE TRIGGER trg_design_versions_guard BEFORE UPDATE OR DELETE ON design_versions "
        "FOR EACH ROW EXECUTE FUNCTION rd_design_version_guard()",
    ]
