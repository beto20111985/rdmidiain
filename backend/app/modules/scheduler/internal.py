"""
RD Mídia Indoor — Conteúdo interno: padrão de blocos (#423, #1063–#1064; A6 §10.6 item 6). PURO.

- ``build_internal_pattern(items, block_ms)``: a partir da lista ORDENADA do
  estabelecimento (peças com duração), preenche blocos de ``I`` ms consumindo um
  fluxo cíclico da lista, em ordem, sem cortar: uma peça só entra se couber inteira;
  quando a próxima não cabe, o bloco termina (o restante vira fallback no player).
  O processo para quando o estado (índice inicial do bloco) se repete; a sequência
  inteira é o padrão e o player toca ``pattern[k mod P]`` em laço. Consequências: 1 peça de 60 s num bloco de 120 s ⇒ ``[[p, p]]``
  (repete dentro do bloco); 10 min de peças de 60 s ⇒ 5 blocos distintos que rotacionam.
- ``InternalStream``: o MESMO fluxo cíclico preenche as unidades não vendidas dos
  blocos externos ("sobra volta ao estabelecimento", #413), em ordem cronológica.
"""
from __future__ import annotations

from typing import Optional, Sequence


def build_internal_pattern(items: Sequence[tuple[str, int]], block_ms: int) -> list[list[dict]]:
    """``items`` = [(chave_criativo, duração_ms)] na ordem definida. Peças > bloco são ignoradas."""
    seq = [(k, d) for k, d in items if 0 < d <= block_ms]
    if not seq:
        return []
    m = len(seq)
    blocks: list[list[dict]] = []
    starts: dict[int, int] = {}
    state = 0
    while state not in starts:
        starts[state] = len(blocks)
        rem = block_ms
        j = state
        block: list[dict] = []
        while seq[j][1] <= rem:
            block.append({"c": seq[j][0], "d": seq[j][1]})
            rem -= seq[j][1]
            j = (j + 1) % m
        blocks.append(block)
        state = j
    # Sequência completa até o estado inicial de algum bloco se repetir. O player toca
    # ``pattern[k mod P]`` em laço: começa SEMPRE pela 1ª peça da lista definida pelo
    # estabelecimento/RD e todas as peças aparecem a cada volta.
    return blocks


class InternalStream:
    """Fluxo cíclico de peças internas para preencher lacunas de blocos externos."""

    def __init__(self, items: Sequence[tuple[str, int]]):
        self.items = [(k, d) for k, d in items if d > 0]
        self.cursor = 0

    def fill(self, gap_ms: int) -> list[tuple[str, int]]:
        """Preenche ``gap_ms`` sem cortar: próxima peça do fluxo que caiba (first-fit
        cíclico a partir do cursor — mantém a rotação e aproveita lacunas curtas).
        Devolve [(chave, d)]; o que não couber fica para o fallback do player."""
        out: list[tuple[str, int]] = []
        if not self.items:
            return out
        rem = gap_ms
        m = len(self.items)
        while rem > 0:
            pick = None
            for step in range(m):
                j = (self.cursor + step) % m
                if self.items[j][1] <= rem:
                    pick = j
                    break
            if pick is None:
                break
            k, d = self.items[pick]
            out.append((k, d))
            rem -= d
            self.cursor = (pick + 1) % m
        return out


def total_internal_ms(pattern: Sequence[Sequence[dict]]) -> Optional[int]:
    return sum(it["d"] for blk in pattern for it in blk) if pattern else None
