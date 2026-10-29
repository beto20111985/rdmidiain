"""
RD Mídia Indoor — Validação de CPF e CNPJ (REQ-ORG-012, REQ-CTR-003).

- CPF: 11 dígitos, dois dígitos verificadores (módulo 11); sequências repetidas
  (``11111111111``) são rejeitadas.
- CNPJ: 14 posições. Suporta o CNPJ ALFANUMÉRICO (Receita Federal, IN RFB
  2.229/2024, vigente a partir de julho/2026): as 12 primeiras posições podem ter
  ``0-9A-Z``; os 2 dígitos verificadores são numéricos e calculados com o valor
  ``ord(c) − 48`` de cada caractere (para dígitos, igual ao algoritmo clássico).
- Entrada aceita com máscara (``.``, ``/``, ``-``, espaços); armazenada normalizada
  (maiúsculas, sem máscara).
"""
from __future__ import annotations

import re
from typing import Optional

_MASK_RE = re.compile(r"[\s.\-/]")
_CNPJ_RE = re.compile(r"^[0-9A-Z]{12}[0-9]{2}$")
_CPF_RE = re.compile(r"^[0-9]{11}$")


class DocumentError(ValueError):
    pass


def strip_mask(raw: Optional[str]) -> str:
    return _MASK_RE.sub("", (raw or "").strip()).upper()


def _cpf_dv(digits: str) -> str:
    def dv(base: str) -> int:
        w = len(base) + 1
        s = sum(int(c) * (w - i) for i, c in enumerate(base))
        r = (s * 10) % 11
        return 0 if r == 10 else r

    d1 = dv(digits[:9])
    d2 = dv(digits[:9] + str(d1))
    return f"{d1}{d2}"


def is_valid_cpf(raw: Optional[str]) -> bool:
    d = strip_mask(raw)
    if not _CPF_RE.match(d) or d == d[0] * 11:
        return False
    return _cpf_dv(d) == d[9:]


_W1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
_W2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]


def _cnpj_dv(base12: str) -> str:
    vals = [ord(c) - 48 for c in base12]

    def dv(v: list[int], w: list[int]) -> int:
        r = sum(a * b for a, b in zip(v, w)) % 11
        return 0 if r < 2 else 11 - r

    d1 = dv(vals, _W1)
    d2 = dv(vals + [d1], _W2)
    return f"{d1}{d2}"


def is_valid_cnpj(raw: Optional[str]) -> bool:
    d = strip_mask(raw)
    if not _CNPJ_RE.match(d) or d == d[0] * 14:
        return False
    return _cnpj_dv(d[:12]) == d[12:]


def normalize_document(raw: Optional[str], document_type: Optional[str] = None) -> tuple[str, str]:
    """Valida e normaliza. Retorna ``(documento, tipo)``; tipo inferido pelo tamanho se omitido."""
    d = strip_mask(raw)
    t = (document_type or "").strip().upper() or None
    if t is None:
        t = "CPF" if len(d) == 11 else "CNPJ" if len(d) == 14 else None
    if t == "CPF":
        if not is_valid_cpf(d):
            raise DocumentError("CPF inválido.")
        return d, "CPF"
    if t == "CNPJ":
        if not is_valid_cnpj(d):
            raise DocumentError("CNPJ inválido.")
        return d, "CNPJ"
    raise DocumentError("Documento deve ser CPF (11 dígitos) ou CNPJ (14 posições).")


def format_document(doc: str, document_type: str) -> str:
    if document_type == "CPF" and len(doc) == 11:
        return f"{doc[:3]}.{doc[3:6]}.{doc[6:9]}-{doc[9:]}"
    if document_type == "CNPJ" and len(doc) == 14:
        return f"{doc[:2]}.{doc[2:5]}.{doc[5:8]}/{doc[8:12]}-{doc[12:]}"
    return doc


def mask_document(doc: Optional[str], document_type: Optional[str]) -> Optional[str]:
    """Versão mascarada para exibição a terceiros (LGPD): mantém só os 2 últimos dígitos do CPF."""
    if not doc:
        return doc
    if document_type == "CPF" and len(doc) == 11:
        return f"***.***.***-{doc[9:]}"
    return format_document(doc, document_type or "")


def make_cpf(base9: str) -> str:
    """Gera CPF válido a partir de 9 dígitos (uso em testes/demonstração)."""
    return base9 + _cpf_dv(base9)


def make_cnpj(base12: str) -> str:
    """Gera CNPJ válido (numérico ou alfanumérico) a partir de 12 posições."""
    b = base12.upper()
    return b + _cnpj_dv(b)
