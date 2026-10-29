"""
RD Mídia Indoor — Piso de preço da RD imposto NO SERVIDOR (REQ-NEG-006; ETAPA2 #7; P002 #40).

A regra (comparação preço × piso) e o ERRO são do W-BIZ: ``app.modules.instance.service.floors_for`` (lote) e
``assert_price_not_below_floor``. Este módulo só deriva, para cada decisão de preço, a CHAVE de piso e o valor
comparável, e chama a interface do W-BIZ; a exceção dela PROPAGA SEM REEMBRULHO (contrato único para o FE):

    PRICE_BELOW_RD_MINIMUM (400) — details exatamente {floor_key, min_price_cents, price_cents}

CONVENÇÃO DE CHAVES (determinística; cadastradas pelo Admin Master em ``PUT /instance/price-floors/{key}``):

- ``slot_{D}s_{P}_tela``   preço de MÍDIA (após TODOS os descontos — plano, promoção, desconto comercial, cupom —
                           e o ajuste de preço mínimo) por TELA, normalizado a 30 dias corridos:
                           ``⌊media_cents × 30 / (telas × dias_corridos)⌋`` (unidade sugerida: "tela/mês");
                           D ∈ {30, 60, 120} = duração do anúncio; P ∈ {avulso, mensal, semestral, anual} = período.
                           Ex.: ``slot_30s_mensal_tela``, ``slot_120s_anual_tela``.
- ``slot_{D}s_insercao``   preço de MÍDIA médio por INSERÇÃO por tela: ``⌊media_cents / inserções⌋``
                           (unidade: "inserção/tela"); na ativação da tabela, também o preço-base
                           (DURATION_BASE, centavos por inserção) da duração D.
- ``campanha_minimo``      preço de MÍDIA mínimo por campanha (unidade: "campanha"); na ativação da tabela,
                           também o item MIN_PRICE da tabela.

Chave sem piso cadastrado ⇒ preço livre. Só vale em instância WHITE-LABEL (``instance_profile.is_white_label``);
a instância RD define os próprios planos base (sem piso para si). Divisões inteiras por baixo são exatas
para pisos inteiros (⌊x⌋ < F ⇔ x < F).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

CAMPAIGN_MIN_KEY = "campanha_minimo"
ERROR_CODE = "PRICE_BELOW_RD_MINIMUM"  # levantado pelo W-BIZ (400); aqui só para documentação/testes


def insertion_key(duration_s: int) -> str:
    return f"slot_{int(duration_s)}s_insercao"


def monthly_key(duration_s: int, period_type: str = "MENSAL") -> str:
    return f"slot_{int(duration_s)}s_{(period_type or 'MENSAL').lower()}_tela"


async def floors_apply(db: AsyncSession) -> bool:
    try:
        from app.modules.instance.service import get_profile
    except Exception:  # pragma: no cover - domínio W-BIZ ausente
        return False
    p = await get_profile(db)
    return bool(p is not None and getattr(p, "is_white_label", False))


async def _assert(db: AsyncSession, key: str, price_cents: int) -> None:
    from app.modules.instance.service import assert_price_not_below_floor

    await assert_price_not_below_floor(db, key, int(price_cents))  # PRICE_BELOW_RD_MINIMUM propaga intacto


async def enforce_values(db: AsyncSession, *, duration_s: int, media_cents: int, insertions: int, screens: int,
                         calendar_days: int, period_type: str = "MENSAL") -> None:
    """Valida o preço de MÍDIA (após descontos) contra todos os pisos aplicáveis."""
    if not await floors_apply(db):
        return
    from app.modules.instance.service import floors_for

    ki, km = insertion_key(duration_s), monthly_key(duration_s, period_type)
    floors = await floors_for(db, [ki, km, CAMPAIGN_MIN_KEY])
    if not floors:
        return
    if ki in floors and insertions > 0:
        await _assert(db, ki, media_cents // insertions)
    if km in floors and screens > 0 and calendar_days > 0:
        await _assert(db, km, (media_cents * 30) // (screens * calendar_days))
    if CAMPAIGN_MIN_KEY in floors:
        await _assert(db, CAMPAIGN_MIN_KEY, media_cents)


async def enforce_quote(db: AsyncSession, quote: dict) -> None:
    inp = quote.get("inputs") or {}
    await enforce_values(
        db, duration_s=int(inp.get("duration_s", 30)), media_cents=int(quote["media_cents"]),
        insertions=int((quote.get("insertions") or {}).get("total", 0)), screens=int(inp.get("screens_count", 0)),
        calendar_days=int(inp.get("calendar_days", 0)), period_type=str(inp.get("period_type") or "MENSAL"))


async def enforce_table_items(db: AsyncSession, items) -> None:
    """Ativação de tabela: preço-base por inserção (DURATION_BASE) e preço mínimo (MIN_PRICE) da própria tabela."""
    if not await floors_apply(db):
        return
    from app.modules.instance.service import floors_for

    keys = [insertion_key(int(i.key)) for i in items if i.kind == "DURATION_BASE" and str(i.key).isdigit()]
    keys.append(CAMPAIGN_MIN_KEY)
    floors = await floors_for(db, keys)
    for i in items:
        if i.kind == "DURATION_BASE" and str(i.key).isdigit() and insertion_key(int(i.key)) in floors:
            await _assert(db, insertion_key(int(i.key)), int(i.value))
        if i.kind == "MIN_PRICE" and CAMPAIGN_MIN_KEY in floors:
            await _assert(db, CAMPAIGN_MIN_KEY, int(i.value))
