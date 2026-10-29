"""
RD Mídia Indoor — IDs públicos legíveis (contrato §1 ID_PUBLICOS).

Alfabeto seguro: ``ABCDEFGHJMNPQRSTUVYZ23456789`` (28 símbolos; sem L, I, X, W, O,
K, 0, 1). Geração exclusivamente com ``secrets`` (CSPRNG).

- ``new_creative_code()``  → ``RD-XXXXXX``  (28^6 ≈ 481,9 milhões)
- ``new_screen_code(uf)``  → ``TEL-<UF>-XXXX`` (28^4 = 614.656 por UF)
- ``new_pairing_code()``   → ``XXXX-XXXX`` (28^8 ≈ 3,78×10^11)
- ``generate_unique(gen, exists, max_attempts)``: tenta de novo em colisão
  (``exists`` pode ser síncrono ou assíncrono). Mesmo com o retry, a coluna no banco
  DEVE ter UNIQUE — o retry evita o erro na prática, o UNIQUE garante a regra.
"""
from __future__ import annotations

import inspect
import re
import secrets
from typing import Awaitable, Callable, Union

SAFE_ALPHABET = "ABCDEFGHJMNPQRSTUVYZ23456789"
FORBIDDEN_CHARS = frozenset("LIXWOK01")
assert len(SAFE_ALPHABET) == 28 and not (set(SAFE_ALPHABET) & FORBIDDEN_CHARS)

BR_UFS = frozenset(
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()
)

CREATIVE_CODE_RE = re.compile(rf"^RD-[{SAFE_ALPHABET}]{{6}}$")
SCREEN_CODE_RE = re.compile(rf"^TEL-[A-Z]{{2}}-[{SAFE_ALPHABET}]{{4}}$")
PAIRING_CODE_RE = re.compile(rf"^[{SAFE_ALPHABET}]{{4}}-[{SAFE_ALPHABET}]{{4}}$")


class IdGenerationError(RuntimeError):
    pass


def random_safe(n: int) -> str:
    if n <= 0:
        raise ValueError("n deve ser positivo")
    return "".join(secrets.choice(SAFE_ALPHABET) for _ in range(n))


def new_creative_code() -> str:
    return f"RD-{random_safe(6)}"


def new_screen_code(uf: str) -> str:
    uf = (uf or "").strip().upper()
    if uf not in BR_UFS:
        raise ValueError("UF inválida")
    return f"TEL-{uf}-{random_safe(4)}"


def new_pairing_code() -> str:
    return f"{random_safe(4)}-{random_safe(4)}"


def normalize_pairing_code(raw: str) -> str:
    """Aceita entrada do usuário com minúsculas/espaços/sem hífen."""
    s = re.sub(r"[\s\-]", "", (raw or "").upper())
    if len(s) != 8 or any(c not in SAFE_ALPHABET for c in s):
        raise ValueError("Código de pareamento inválido")
    return f"{s[:4]}-{s[4:]}"


def is_valid_creative_code(code: str) -> bool:
    return bool(CREATIVE_CODE_RE.match(code or ""))


def is_valid_screen_code(code: str) -> bool:
    return bool(SCREEN_CODE_RE.match(code or "")) and code.split("-")[1] in BR_UFS


ExistsFn = Callable[[str], Union[bool, Awaitable[bool]]]


async def generate_unique(gen: Callable[[], str], exists: ExistsFn, max_attempts: int = 10) -> str:
    """Gera um código e verifica unicidade; tenta novamente em colisão."""
    for _ in range(max_attempts):
        code = gen()
        res = exists(code)
        if inspect.isawaitable(res):
            res = await res
        if not res:
            return code
    raise IdGenerationError(f"Não foi possível gerar código único após {max_attempts} tentativas")


def expected_collisions(n: int, space: int) -> float:
    """Número esperado de pares colidentes (aprox. aniversário): n(n-1)/(2·space)."""
    return n * (n - 1) / (2 * space)


def expected_duplicates(n: int, space: int) -> float:
    """Esperança de (n − distintos) ao sortear n valores uniformes num espaço ``space``."""
    expected_distinct = space * (1.0 - (1.0 - 1.0 / space) ** n)
    return n - expected_distinct
