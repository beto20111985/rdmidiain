"""
RD Mídia Indoor — Política de rotação (ADR_006 §1–§2; A6 §10.0, §10.9). Módulo PURO.

Ciclo ``C = I + E_s`` (segundos). Regras:
- ``I`` e ``E_s`` múltiplos de 30 s (unidade) e > 0;
- ``C`` divide 3600 ⇒ toda hora (e qualquer janela de 3600 s) tem exatamente
  ``K = 3600 / C`` inícios de bloco externo, para QUALQUER âncora;
- capacidade por tela-hora = ``K × E`` unidades de 30 s (``E = E_s / 30``);
- criativo de ``du`` unidades só é elegível se ``du ≤ E`` (nunca cortar/dividir);
- intervalo mínimo ``iv`` vira distância mínima em blocos
  ``gap = ceil((iv + (E − 1)·30) / C)`` (pior caso: 1ª ocorrência na última unidade
  do bloco e a seguinte na primeira); sem intervalo, ``gap = 1`` (a mesma linha
  nunca aparece duas vezes no mesmo bloco).

``allow_external_overflow``/``overflow_max_s`` ficam MODELADOS e validados, mas o
empacotador não invade o bloco interno: o RDPP/1 publica ``internal_ms`` e
``external_ms`` fixos por manifesto (invasão por ciclo exigiria nova versão do
protocolo) — pendência documentada no relatório W-INV.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

UNIT_S = 30
UNIT_MS = UNIT_S * 1000
HOUR_S = 3600
ALLOWED_DURATION_UNITS = (1, 2, 4)  # 30 / 60 / 120 s


class PolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def validate_rotation(internal_s: int, external_s: int, allow_external_overflow: bool = False,
                      overflow_max_s: int = 0) -> None:
    for name, v in (("internal_seconds", internal_s), ("external_seconds", external_s),
                    ("overflow_max_seconds", overflow_max_s)):
        if isinstance(v, bool) or not isinstance(v, int):
            raise PolicyError("ROTATION_INVALID", f"{name} deve ser inteiro (segundos)")
    if internal_s <= 0 or internal_s % UNIT_S:
        raise PolicyError("ROTATION_INVALID", "Tempo interno deve ser múltiplo positivo de 30 s.")
    if external_s <= 0 or external_s % UNIT_S:
        raise PolicyError("ROTATION_INVALID", "Tempo externo deve ser múltiplo positivo de 30 s.")
    cycle = internal_s + external_s
    if HOUR_S % cycle:
        raise PolicyError("ROTATION_CYCLE_NOT_DIVISOR",
                          f"O ciclo ({cycle} s) precisa dividir 3600 s exatamente.")
    if overflow_max_s < 0 or overflow_max_s % UNIT_S or overflow_max_s > internal_s:
        raise PolicyError("ROTATION_INVALID", "Limite de invasão deve ser múltiplo de 30 s e ≤ tempo interno.")
    if not allow_external_overflow and overflow_max_s:
        raise PolicyError("ROTATION_INVALID", "Limite de invasão exige a flag allow_external_overflow.")


@dataclass(frozen=True)
class RotationSpec:
    internal_s: int
    external_s: int
    allow_external_overflow: bool = False
    overflow_max_s: int = 0
    policy_id: Optional[str] = None

    def __post_init__(self) -> None:
        validate_rotation(self.internal_s, self.external_s, self.allow_external_overflow, self.overflow_max_s)

    @property
    def cycle_s(self) -> int:
        return self.internal_s + self.external_s

    @property
    def cycle_ms(self) -> int:
        return self.cycle_s * 1000

    @property
    def cycles_per_hour(self) -> int:  # K
        return HOUR_S // self.cycle_s

    @property
    def units_per_block(self) -> int:  # E
        return self.external_s // UNIT_S

    @property
    def units_per_hour(self) -> int:
        return self.cycles_per_hour * self.units_per_block

    def eligible(self, duration_units: int) -> bool:
        return duration_units in ALLOWED_DURATION_UNITS and duration_units <= self.units_per_block

    def gap_blocks(self, min_interval_s: int) -> int:
        if min_interval_s <= 0:
            return 1
        num = min_interval_s + (self.units_per_block - 1) * UNIT_S
        return max(1, -(-num // self.cycle_s))

    def max_per_hour(self, min_interval_s: int) -> int:
        return self.cycles_per_hour // self.gap_blocks(min_interval_s)

    def key(self) -> tuple:
        return (self.internal_s, self.external_s)

    def to_manifest(self) -> dict:
        return {
            "internal_ms": self.internal_s * 1000,
            "external_ms": self.external_s * 1000,
            "cycle_ms": self.cycle_ms,
            "unit_ms": UNIT_MS,
            "policy_id": self.policy_id,
        }

    def summary(self) -> dict:
        return {
            "internal_seconds": self.internal_s,
            "external_seconds": self.external_s,
            "cycle_seconds": self.cycle_s,
            "cycles_per_hour": self.cycles_per_hour,
            "units_per_block": self.units_per_block,
            "units_per_hour": self.units_per_hour,
            "eligible_120s": self.eligible(4),
            "allow_external_overflow": self.allow_external_overflow,
            "overflow_max_seconds": self.overflow_max_s,
        }


DEFAULT_ROTATION = RotationSpec(120, 120)
