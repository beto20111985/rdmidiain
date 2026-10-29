"""
RD Mídia Indoor — DDL extra do domínio de inventário (W-INV). Idempotente.

Consumido por:
- harness de testes (``after_create`` dos models de inventário + ``DOMAIN_DDL_MODULES``
  de ``app/db/ddl.py`` quando existir — ambos idempotentes);
- migration 0004 (W-DB): ``for s in statements(): op.execute(s)`` APÓS criar as
  tabelas (e ``CREATE EXTENSION btree_gist`` ANTES de ``rotation_policies``).

Conteúdo:
1. ``btree_gist`` (EXCLUDE de vigência sem sobreposição em ``rotation_policies``).
2. Âncora WRITE-ONCE: trigger ``trg_screen_inventory_state_anchor_write_once``
   bloqueia alterar ``anchor_utc`` já definido, exceto dentro de
   ``rd_reset_screen_anchor(...)`` (flag de transação ``rd.anchor_reset``).
3. ``rd_reset_screen_anchor(screen, nova, justificativa, ator)``: reset auditado
   (justificativa ≥ 10 caracteres) com linha em ``screen_anchor_history``.
4. ``screen_anchor_history`` append-only (``app.db.ddl.append_only_trigger``).
5. Seed idempotente da política GLOBAL 120/120 (vigente desde 2020-01-01).
"""
from __future__ import annotations

from typing import Iterable, Optional

from app.db import ddl as core_ddl

EXTENSION = "CREATE EXTENSION IF NOT EXISTS btree_gist"

ANCHOR_WRITE_ONCE_FN = """
CREATE OR REPLACE FUNCTION rd_anchor_write_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.anchor_utc IS NOT NULL
       AND NEW.anchor_utc IS DISTINCT FROM OLD.anchor_utc
       AND coalesce(current_setting('rd.anchor_reset', true), '') <> 'on' THEN
        RAISE EXCEPTION 'RD_ANCHOR_WRITE_ONCE: a âncora da tela % já foi definida', OLD.screen_id
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""

ANCHOR_WRITE_ONCE_TRIGGER = [
    "DROP TRIGGER IF EXISTS trg_screen_inventory_state_anchor_write_once ON screen_inventory_state",
    "CREATE TRIGGER trg_screen_inventory_state_anchor_write_once "
    "BEFORE UPDATE OF anchor_utc ON screen_inventory_state "
    "FOR EACH ROW EXECUTE FUNCTION rd_anchor_write_once()",
]

ANCHOR_RESET_FN = """
CREATE OR REPLACE FUNCTION rd_reset_screen_anchor(p_screen uuid, p_new timestamptz, p_reason text, p_actor uuid)
RETURNS timestamptz
LANGUAGE plpgsql AS $$
DECLARE
    v_old timestamptz;
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) < 10 THEN
        RAISE EXCEPTION 'RD_ANCHOR_RESET_REASON: justificativa obrigatória (mínimo 10 caracteres)'
            USING ERRCODE = 'P0001';
    END IF;
    SELECT s.anchor_utc INTO v_old FROM screen_inventory_state s WHERE s.screen_id = p_screen FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RD_ANCHOR_RESET_NOT_FOUND: tela % sem estado de inventário', p_screen
            USING ERRCODE = 'P0001';
    END IF;
    PERFORM set_config('rd.anchor_reset', 'on', true);
    UPDATE screen_inventory_state
       SET anchor_utc = p_new,
           anchor_source = CASE WHEN p_new IS NULL THEN NULL ELSE 'ADMIN_RESET' END,
           anchor_device_id = NULL,
           anchor_reported_at = now(),
           updated_at = now()
     WHERE screen_id = p_screen;
    PERFORM set_config('rd.anchor_reset', 'off', true);
    INSERT INTO screen_anchor_history (id, screen_id, old_anchor, new_anchor, source, reason, actor_user_id, created_at)
    VALUES (gen_random_uuid(), p_screen, v_old, p_new, 'ADMIN_RESET', p_reason, p_actor, now());
    RETURN p_new;
END;
$$
"""

GLOBAL_POLICY_SEED = """
INSERT INTO rotation_policies (id, scope, scope_id, internal_seconds, external_seconds,
                               allow_external_overflow, overflow_max_seconds, effective_from,
                               effective_to, version, reason, created_by, created_at)
SELECT '00000000-0000-4000-8000-000000000120'::uuid, 'GLOBAL', NULL, 120, 120, false, 0,
       '2020-01-01T00:00:00Z'::timestamptz, NULL, 1,
       'Padrão RD: 2 min interno + 2 min externo (ADR_006)', NULL, now()
WHERE NOT EXISTS (SELECT 1 FROM rotation_policies WHERE scope = 'GLOBAL')
"""

TABLES = ("rotation_policies", "screen_inventory_state", "screen_anchor_history")


def anchor_statements() -> list[str]:
    return [ANCHOR_WRITE_ONCE_FN, *ANCHOR_WRITE_ONCE_TRIGGER, ANCHOR_RESET_FN]


def history_statements() -> list[str]:
    return core_ddl.append_only_trigger("screen_anchor_history")


def statements(tables: Optional[Iterable[str]] = None, *, include_seed: bool = False) -> list[str]:
    """Statements idempotentes. ``tables``: tabelas existentes (None = todas)."""
    have = set(tables) if tables is not None else set(TABLES)
    out: list[str] = [EXTENSION]
    if "screen_inventory_state" in have:
        out += anchor_statements()
    if "screen_anchor_history" in have:
        out += history_statements()
    if include_seed and "rotation_policies" in have:
        out.append(GLOBAL_POLICY_SEED)
    return out


# ─── 0008 (AÇÃO 003): corpos VIGENTES da âncora ──────────────────────────────
# ``ANCHOR_WRITE_ONCE_FN``/``ANCHOR_RESET_FN`` acima são HISTÓRICOS (replay da 0004 — NÃO editar).
# Achado: o write-once confiava só no GUC ``rd.anchor_reset``, que QUALQUER sessão define com
# ``SET LOCAL`` ⇒ o papel de runtime trocava a âncora sem justificativa nem histórico. Agora vale o desenho
# documentado ("reset só por rd_reset_screen_anchor com justificativa"):
#   - ``rd_reset_screen_anchor`` é SECURITY DEFINER (EXECUTE só para o papel de runtime) e
#   - o write-once só libera com o GUC **e** ``current_user`` = dono dessa função (isto é: dentro dela).
# O dono é lido do catálogo (não fixa ``rd_owner``: no harness o dono é o superusuário dos testes).
# Função ausente ⇒ subconsulta NULL ⇒ recusa (falha fechada).

ANCHOR_RESET_SIGNATURE = "rd_reset_screen_anchor(uuid, timestamptz, text, uuid)"

ANCHOR_WRITE_ONCE_FN_V2 = """
CREATE OR REPLACE FUNCTION rd_anchor_write_once() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF OLD.anchor_utc IS NOT NULL
       AND NEW.anchor_utc IS DISTINCT FROM OLD.anchor_utc
       AND NOT (coalesce(current_setting('rd.anchor_reset', true), '') = 'on'
                AND current_user::text IS NOT DISTINCT FROM (
                    SELECT pg_catalog.pg_get_userbyid(p.proowner)::text FROM pg_catalog.pg_proc p
                     WHERE p.oid = pg_catalog.to_regprocedure('public.rd_reset_screen_anchor(uuid,timestamptz,text,uuid)'))) THEN
        RAISE EXCEPTION 'RD_ANCHOR_WRITE_ONCE: a âncora da tela % já foi definida', OLD.screen_id
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""

ANCHOR_RESET_FN_V2 = """
CREATE OR REPLACE FUNCTION rd_reset_screen_anchor(p_screen uuid, p_new timestamptz, p_reason text, p_actor uuid)
RETURNS timestamptz
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_old timestamptz;
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) < 10 THEN
        RAISE EXCEPTION 'RD_ANCHOR_RESET_REASON: justificativa obrigatória (mínimo 10 caracteres)'
            USING ERRCODE = 'P0001';
    END IF;
    SELECT s.anchor_utc INTO v_old FROM public.screen_inventory_state s WHERE s.screen_id = p_screen FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RD_ANCHOR_RESET_NOT_FOUND: tela % sem estado de inventário', p_screen
            USING ERRCODE = 'P0001';
    END IF;
    PERFORM set_config('rd.anchor_reset', 'on', true);
    UPDATE public.screen_inventory_state
       SET anchor_utc = p_new,
           anchor_source = CASE WHEN p_new IS NULL THEN NULL ELSE 'ADMIN_RESET' END,
           anchor_device_id = NULL,
           anchor_reported_at = now(),
           updated_at = now()
     WHERE screen_id = p_screen;
    PERFORM set_config('rd.anchor_reset', 'off', true);
    INSERT INTO public.screen_anchor_history (id, screen_id, old_anchor, new_anchor, source, reason, actor_user_id, created_at)
    VALUES (gen_random_uuid(), p_screen, v_old, p_new, 'ADMIN_RESET', p_reason, p_actor, now());
    RETURN p_new;
END;
$$
"""


def hardened_statements(tables: Optional[Iterable[str]] = None) -> list[str]:
    """0008 (AÇÃO 003): corpos VIGENTES (``search_path`` seguro + tabelas qualificadas + reset SECURITY DEFINER);
    os constantes ``ANCHOR_*_FN`` são HISTÓRICOS (replay da 0004). O GRANT ao papel de runtime está em
    ``runtime_grant_statements`` (a migration aplica se o papel existir)."""
    have = set(tables) if tables is not None else set(TABLES)
    if "screen_inventory_state" not in have or "screen_anchor_history" not in have:
        return []
    return [ANCHOR_WRITE_ONCE_FN_V2, ANCHOR_RESET_FN_V2, f"REVOKE ALL ON FUNCTION {ANCHOR_RESET_SIGNATURE} FROM PUBLIC"]


def runtime_grant_statements(runtime_role: str) -> list[str]:
    """EXECUTE do reset de âncora só para o papel de runtime (``rd_app``)."""
    if not runtime_role.replace("_", "").isalnum():
        raise ValueError("papel inválido")
    return [f"GRANT EXECUTE ON FUNCTION {ANCHOR_RESET_SIGNATURE} TO {runtime_role}"]


def drop_statements() -> list[str]:
    return [
        "DROP TRIGGER IF EXISTS trg_screen_inventory_state_anchor_write_once ON screen_inventory_state",
        "DROP FUNCTION IF EXISTS rd_reset_screen_anchor(uuid, timestamptz, text, uuid)",
        "DROP FUNCTION IF EXISTS rd_anchor_write_once()",
        *core_ddl.drop_append_only_trigger("screen_anchor_history"),
    ]
