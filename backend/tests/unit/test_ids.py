"""IDs públicos (contrato §1 ID_PUBLICOS; P002 item 26) — teste massivo."""
import math
import re

import pytest

from app.core import ids

N = 200_000


def _assert_no_forbidden(codes):
    joined = "".join(codes)
    for ch in ids.FORBIDDEN_CHARS:
        assert ch not in joined, f"caractere proibido {ch!r} encontrado"


def test_alphabet_is_exactly_the_safe_one():
    assert ids.SAFE_ALPHABET == "ABCDEFGHJMNPQRSTUVYZ23456789"
    assert len(set(ids.SAFE_ALPHABET)) == 28
    assert not set(ids.SAFE_ALPHABET) & set("LIXWOK01")


def test_creative_code_200k_no_forbidden_chars_and_collisions_as_expected():
    codes = [ids.new_creative_code() for _ in range(N)]
    assert all(ids.is_valid_creative_code(c) for c in codes)
    body = [c[3:] for c in codes]
    _assert_no_forbidden(body)
    # todos os 28 símbolos aparecem (sanidade da distribuição)
    assert set("".join(body)) == set(ids.SAFE_ALPHABET)
    observed_dups = N - len(set(codes))
    expected = ids.expected_duplicates(N, 28**6)
    # Poisson: desvio-padrão ≈ sqrt(esperado); tolerância de 6σ
    assert abs(observed_dups - expected) <= 6 * math.sqrt(expected) + 1, (observed_dups, expected)


def test_screen_code_200k_format_and_collision_rate():
    codes = [ids.new_screen_code("GO") for _ in range(N)]
    assert all(re.fullmatch(r"TEL-GO-[ABCDEFGHJMNPQRSTUVYZ23456789]{4}", c) for c in codes)
    _assert_no_forbidden([c[7:] for c in codes])
    observed = N - len(set(codes))
    expected = ids.expected_duplicates(N, 28**4)  # ≈ 29 mil: espaço pequeno → UNIQUE + retry obrigatórios
    assert abs(observed - expected) / expected < 0.02, (observed, expected)


def test_pairing_code_200k():
    codes = [ids.new_pairing_code() for _ in range(N)]
    assert all(ids.PAIRING_CODE_RE.fullmatch(c) for c in codes)
    _assert_no_forbidden([c.replace("-", "") for c in codes])
    observed = N - len(set(codes))
    expected = ids.expected_duplicates(N, 28**8)
    assert observed <= expected + 6 * math.sqrt(expected) + 2


def test_letter_frequency_uniform_chi_square():
    """Qui-quadrado da frequência de símbolos (1,2 milhão de amostras)."""
    sample = "".join(ids.new_creative_code()[3:] for _ in range(N))
    n = len(sample)
    exp = n / 28
    chi2 = sum((sample.count(ch) - exp) ** 2 / exp for ch in ids.SAFE_ALPHABET)
    # 27 graus de liberdade: p=0,0001 → ~61.0
    assert chi2 < 61.0, chi2


def test_screen_code_rejects_invalid_uf():
    with pytest.raises(ValueError):
        ids.new_screen_code("XX")
    with pytest.raises(ValueError):
        ids.new_screen_code("")


def test_normalize_pairing_code():
    assert ids.normalize_pairing_code("abcd efgh") == "ABCD-EFGH"
    with pytest.raises(ValueError):
        ids.normalize_pairing_code("ABCD-EFG1")  # '1' proibido
    with pytest.raises(ValueError):
        ids.normalize_pairing_code("ABC")


async def test_generate_unique_retries_on_collision():
    seq = iter(["RD-AAAAAA", "RD-AAAAAA", "RD-BBBBBB"])
    taken = {"RD-AAAAAA"}
    calls = []

    async def exists(code):
        calls.append(code)
        return code in taken

    code = await ids.generate_unique(lambda: next(seq), exists)
    assert code == "RD-BBBBBB"
    assert calls == ["RD-AAAAAA", "RD-AAAAAA", "RD-BBBBBB"]


async def test_generate_unique_gives_up():
    with pytest.raises(ids.IdGenerationError):
        await ids.generate_unique(lambda: "RD-AAAAAA", lambda c: True, max_attempts=3)
