"""
RD Mídia Indoor — Geografia do Brasil para estabelecimentos (W-ORG; AÇÃO 002).

- ``UF_REGION``: macro-região IBGE por UF.
- ``suggest_timezone(uf, city)``: sugestão de fuso IANA (o admin pode corrigir —
  ``timezone_source = MANUAL``). Fonte: Lei 12.876/2013 (Acre e oeste do Amazonas em
  UTC−5) e a base IANA (tzdata). A sugestão é só um ponto de partida: o valor gravado
  é sempre validado por ``validate_timezone``.
- ``derive_keys``: chaves normalizadas (``city_key`` = escopo CITY do RBAC).
- ``haversine_m``: distância em metros entre dois pontos (geolocalização do técnico).
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Optional

from app.core.authz import city_key as _city_key
from app.core.ids import BR_UFS

REGIONS = ("NORTE", "NORDESTE", "CENTRO_OESTE", "SUDESTE", "SUL")
REGION_LABELS = {
    "NORTE": "Norte",
    "NORDESTE": "Nordeste",
    "CENTRO_OESTE": "Centro-Oeste",
    "SUDESTE": "Sudeste",
    "SUL": "Sul",
}

UF_REGION: dict[str, str] = {
    **{uf: "NORTE" for uf in ("AC", "AM", "AP", "PA", "RO", "RR", "TO")},
    **{uf: "NORDESTE" for uf in ("AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE")},
    **{uf: "CENTRO_OESTE" for uf in ("DF", "GO", "MS", "MT")},
    **{uf: "SUDESTE" for uf in ("ES", "MG", "RJ", "SP")},
    **{uf: "SUL" for uf in ("PR", "RS", "SC")},
}
assert set(UF_REGION) == set(BR_UFS)

# Fuso padrão por UF (zona IANA canônica da capital/maioria do estado).
UF_TIMEZONE: dict[str, str] = {
    "AC": "America/Rio_Branco",
    "AM": "America/Manaus",
    "AP": "America/Belem",
    "PA": "America/Belem",
    "RO": "America/Porto_Velho",
    "RR": "America/Boa_Vista",
    "TO": "America/Araguaina",
    "MA": "America/Fortaleza",
    "PI": "America/Fortaleza",
    "CE": "America/Fortaleza",
    "RN": "America/Fortaleza",
    "PB": "America/Fortaleza",
    "PE": "America/Recife",
    "AL": "America/Maceio",
    "SE": "America/Maceio",
    "BA": "America/Bahia",
    "MT": "America/Cuiaba",
    "MS": "America/Campo_Grande",
    "GO": "America/Sao_Paulo",
    "DF": "America/Sao_Paulo",
    "MG": "America/Sao_Paulo",
    "ES": "America/Sao_Paulo",
    "RJ": "America/Sao_Paulo",
    "SP": "America/Sao_Paulo",
    "PR": "America/Sao_Paulo",
    "SC": "America/Sao_Paulo",
    "RS": "America/Sao_Paulo",
}
assert set(UF_TIMEZONE) == set(BR_UFS)


def slug(text: Optional[str]) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


# Exceções por município (chave = slug da cidade).
# Oeste do Amazonas em UTC−5 (America/Eirunepe) — Lei 12.876/2013.
_AM_EIRUNEPE = {
    slug(c)
    for c in (
        "Atalaia do Norte", "Benjamin Constant", "Boca do Acre", "Eirunepé", "Envira", "Guajará",
        "Ipixuna", "Itamarati", "Jutaí", "Lábrea", "Pauini", "São Paulo de Olivença", "Tabatinga",
    )
}
_CITY_TZ_OVERRIDES: dict[tuple[str, str], tuple[str, str]] = {
    ("PE", slug("Fernando de Noronha")): ("America/Noronha", "Fernando de Noronha usa UTC−2."),
}


def normalize_uf(uf: Optional[str]) -> str:
    v = (uf or "").strip().upper()
    if v not in BR_UFS:
        raise ValueError("UF inválida")
    return v


def suggest_timezone(uf: str, city: Optional[str] = None) -> dict:
    """Sugestão de fuso IANA para UF/cidade (o admin pode corrigir)."""
    u = normalize_uf(uf)
    cs = slug(city)
    if (u, cs) in _CITY_TZ_OVERRIDES:
        tz, why = _CITY_TZ_OVERRIDES[(u, cs)]
        return {"timezone": tz, "reason": why, "confidence": "CITY"}
    if u == "AM" and cs in _AM_EIRUNEPE:
        return {
            "timezone": "America/Eirunepe",
            "reason": "Município do oeste do Amazonas em UTC−5 (Lei 12.876/2013).",
            "confidence": "CITY",
        }
    reason = "Fuso padrão da UF."
    if u == "AM":
        reason = "Fuso da maior parte do Amazonas (UTC−4); municípios do oeste usam America/Eirunepe."
    return {"timezone": UF_TIMEZONE[u], "reason": reason, "confidence": "STATE"}


def region_of(uf: str) -> str:
    return UF_REGION[normalize_uf(uf)]


def derive_keys(state: Optional[str], city: Optional[str], neighborhood: Optional[str]) -> dict:
    u = normalize_uf(state)
    c = (city or "").strip()
    if not c:
        raise ValueError("Cidade obrigatória")
    nb = slug(neighborhood) if neighborhood else None
    return {
        "state": u,
        "city_key": _city_key(u, c),
        "region": UF_REGION[u],
        "neighborhood_key": nb or None,
    }


def normalize_postal_code(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    d = re.sub(r"\D", "", str(raw))
    if not d:
        return None
    if len(d) != 8:
        raise ValueError("CEP deve ter 8 dígitos")
    return d


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(round(2 * r * math.asin(math.sqrt(a))))
