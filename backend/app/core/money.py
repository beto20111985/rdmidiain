"""
RD Mídia Indoor — Dinheiro (contrato §1).

Regras:
- Valores monetários são SEMPRE ``int`` em centavos (``*_cents``). ``float`` é
  rejeitado em qualquer entrada (``TypeError``). ``bool`` também é rejeitado.
- Percentuais em basis points (``*_bps``: 500 = 5,00%), inteiros 0..10000 por padrão.
- Arredondamento: ``apply_bps`` usa floor (ROUND_DOWN para valores positivos) —
  o resíduo é explícito e quem decide para onde vai é o domínio (ex.: comissão:
  resíduo pertence à RD).
- ``allocate``: rateio determinístico por pesos inteiros com floor + resíduo
  explícito (nunca distribui mais que o total).
- Conversão para gateways: ``cents_to_reais`` → ``Decimal`` com 2 casas;
  ``reais_to_cents`` aceita ``Decimal``/``str`` (nunca float) e exige ≤ 2 casas.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Mapping, Sequence, TypeVar, Union

K = TypeVar("K")

BPS_DENOMINATOR = 10_000


class MoneyError(ValueError):
    pass


def ensure_cents(value: object, *, allow_negative: bool = False, name: str = "amount_cents") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} deve ser int (centavos); recebido {type(value).__name__}")
    if not allow_negative and value < 0:
        raise MoneyError(f"{name} não pode ser negativo")
    return value


def ensure_bps(value: object, *, max_bps: int = BPS_DENOMINATOR, name: str = "bps") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} deve ser int (basis points); recebido {type(value).__name__}")
    if value < 0 or value > max_bps:
        raise MoneyError(f"{name} fora do intervalo 0..{max_bps}")
    return value


def apply_bps(amount_cents: int, bps: int) -> int:
    """floor(amount × bps / 10000) para amount ≥ 0."""
    ensure_cents(amount_cents)
    ensure_bps(bps)
    return (amount_cents * bps) // BPS_DENOMINATOR


def split_bps(amount_cents: int, bps: int) -> tuple[int, int]:
    """Retorna (parte, restante) com parte = floor(amount × bps/10000) e restante = amount − parte."""
    part = apply_bps(amount_cents, bps)
    return part, amount_cents - part


@dataclass(frozen=True)
class Allocation:
    shares: dict  # chave → centavos
    residual_cents: int  # resíduo explícito (total − soma das partes)
    total_cents: int


def allocate(total_cents: int, weights: Union[Mapping[K, int], Sequence[tuple[K, int]]]) -> Allocation:
    """Rateio determinístico: cada parte = floor(total × w / Σw); resíduo explícito.

    - Pesos devem ser inteiros ≥ 0 (use ``*_milli`` para frações: 1000 = 1,0).
    - Σw = 0 → nada é distribuído; resíduo = total.
    - Ordem/chaves preservadas; o resultado não depende da ordem de entrada.
    """
    ensure_cents(total_cents)
    items = list(weights.items()) if isinstance(weights, Mapping) else list(weights)
    for _, w in items:
        if isinstance(w, bool) or not isinstance(w, int):
            raise TypeError("pesos devem ser int")
        if w < 0:
            raise MoneyError("pesos não podem ser negativos")
    total_w = sum(w for _, w in items)
    if total_w == 0:
        return Allocation(shares={k: 0 for k, _ in items}, residual_cents=total_cents, total_cents=total_cents)
    shares = {k: (total_cents * w) // total_w for k, w in items}
    distributed = sum(shares.values())
    return Allocation(shares=shares, residual_cents=total_cents - distributed, total_cents=total_cents)


_TWO_PLACES = Decimal("0.01")


def cents_to_reais(amount_cents: int) -> Decimal:
    ensure_cents(amount_cents, allow_negative=True)
    return (Decimal(amount_cents) / Decimal(100)).quantize(_TWO_PLACES)


def reais_to_cents(value: Union[Decimal, str, int]) -> int:
    """Converte reais (Decimal/str/int) para centavos. Rejeita float e > 2 casas decimais."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("float não é aceito para dinheiro; use Decimal ou str")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise MoneyError("valor monetário inválido") from None
    if not d.is_finite():
        raise MoneyError("valor monetário inválido")
    if d.as_tuple().exponent < -2 and d != d.quantize(_TWO_PLACES):
        raise MoneyError("valor monetário com mais de 2 casas decimais")
    return int((d * 100).to_integral_value(rounding=ROUND_HALF_UP))


def format_brl(amount_cents: int) -> str:
    """Formatação pt-BR simples: 123456 → 'R$ 1.234,56'."""
    ensure_cents(amount_cents, allow_negative=True)
    sign = "-" if amount_cents < 0 else ""
    v = abs(amount_cents)
    reais, cents = divmod(v, 100)
    reais_s = f"{reais:,}".replace(",", ".")
    return f"{sign}R$ {reais_s},{cents:02d}"
