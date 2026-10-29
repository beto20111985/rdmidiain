"""
RD Mídia Indoor — DDL de domínio do W-ORG (aplicado pelo harness e pelas migrations
via ``app.db.ddl.DOMAIN_DDL_MODULES``). Statements IDEMPOTENTES.

- ``contract_templates``: depois de publicado, o texto (title/body/body_sha256), o
  tipo e a versão nunca mudam; não volta a DRAFT; só DRAFT pode ser apagado.
- ``screens``: DECOMMISSIONED é terminal (retirada definitiva não é desfeita — uma
  tela reinstalada é um novo cadastro com novo TEL-UF-XXXX).
"""
from __future__ import annotations

from typing import Optional

CONTRACT_TEMPLATE_GUARD_FN = """
CREATE OR REPLACE FUNCTION rd_contract_template_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION 'RD_CONTRACT_TEMPLATE_IMMUTABLE: modelo publicado não pode ser apagado'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> 'DRAFT' THEN
        IF NEW.body IS DISTINCT FROM OLD.body OR NEW.body_sha256 IS DISTINCT FROM OLD.body_sha256
           OR NEW.title IS DISTINCT FROM OLD.title OR NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.version IS DISTINCT FROM OLD.version THEN
            RAISE EXCEPTION 'RD_CONTRACT_TEMPLATE_IMMUTABLE: texto de modelo publicado não pode mudar'
                USING ERRCODE = 'P0001';
        END IF;
        IF NEW.status = 'DRAFT' THEN
            RAISE EXCEPTION 'RD_CONTRACT_TEMPLATE_IMMUTABLE: modelo publicado não volta a rascunho'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

SCREEN_DECOMMISSION_GUARD_FN = """
CREATE OR REPLACE FUNCTION rd_screen_decommission_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'DECOMMISSIONED' AND (NEW.status <> 'DECOMMISSIONED' OR NEW.sellable) THEN
        RAISE EXCEPTION 'RD_SCREEN_DECOMMISSIONED: tela retirada definitivamente não pode ser reativada'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""


def statements(tables: Optional[set] = None) -> list[str]:
    out: list[str] = [CONTRACT_TEMPLATE_GUARD_FN, SCREEN_DECOMMISSION_GUARD_FN]
    if tables is None or "contract_templates" in tables:
        out += [
            "DROP TRIGGER IF EXISTS trg_contract_templates_guard ON contract_templates",
            "CREATE TRIGGER trg_contract_templates_guard BEFORE UPDATE OR DELETE ON contract_templates "
            "FOR EACH ROW EXECUTE FUNCTION rd_contract_template_guard()",
        ]
    if tables is None or "screens" in tables:
        out += [
            "DROP TRIGGER IF EXISTS trg_screens_decommission_guard ON screens",
            "CREATE TRIGGER trg_screens_decommission_guard BEFORE UPDATE ON screens "
            "FOR EACH ROW EXECUTE FUNCTION rd_screen_decommission_guard()",
        ]
    return out


def hardened_statements(tables: Optional[set] = None) -> list[str]:
    """0008 (AÇÃO 003): corpos VIGENTES com ``search_path`` seguro (sem tabelas a qualificar); os constantes
    acima são HISTÓRICOS (replay da 0004). Ver ``app.db.ddl.harden_function``."""
    from app.db.ddl import harden_function

    return [harden_function(CONTRACT_TEMPLATE_GUARD_FN), harden_function(SCREEN_DECOMMISSION_GUARD_FN)]


def drop_statements() -> list[str]:
    return [
        "DROP TRIGGER IF EXISTS trg_contract_templates_guard ON contract_templates",
        "DROP TRIGGER IF EXISTS trg_screens_decommission_guard ON screens",
        "DROP FUNCTION IF EXISTS rd_contract_template_guard()",
        "DROP FUNCTION IF EXISTS rd_screen_decommission_guard()",
    ]
