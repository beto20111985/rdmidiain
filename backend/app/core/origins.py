"""
RD Mídia Indoor — Origens permitidas (CORS, CSRF e ``Origin`` dos WebSockets).

Contrato de origens (AÇÃO 003 — ver ops/README.md §3.1):

- ``CORS_ORIGINS`` / ``PLAYER_ALLOWED_ORIGINS``: listas BASE (produção: só ``https://`` oficiais).
- ``RD_DEV_ALLOWED_ORIGINS`` / ``RD_DEV_PLAYER_ALLOWED_ORIGINS``: acréscimos de DESENVOLVIMENTO
  (ex.: túnel ngrok), aceitos SOMENTE com ``ENVIRONMENT`` ∈ {development, test}. Ficam em
  ``ops/dev/dev-origins.env`` (configuração local, nunca em código).
- Todos os consumidores usam as listas efetivas de ``Settings`` (``panel_origins``,
  ``player_origins``, ``all_cors_origins``) — nenhum consumidor monta lista própria.

Formato aceito: origem EXATA ``http(s)://host[:porta]`` (normalizada em minúsculas, sem ``/`` final e
sem porta padrão). Rejeitado: curinga (``*``), caminho/query/fragmento, credenciais, outro esquema,
host inválido. Este módulo não importa ``app.core.config`` (é usado por ela).
"""
from __future__ import annotations

import ipaddress
import re
from typing import Iterable, Optional
from urllib.parse import urlsplit

# Sufixos de serviços PÚBLICOS de túnel de desenvolvimento: nunca são origem oficial de produção.
# (Somente sufixos genéricos de provedores — nenhum domínio de instância/conta aqui.)
PUBLIC_TUNNEL_SUFFIXES: tuple[str, ...] = (
    "ngrok-free.dev",
    "ngrok-free.app",
    "ngrok.dev",
    "ngrok.app",
    "ngrok.io",
    "ngrok.pizza",
    "trycloudflare.com",
    "loca.lt",
    "localtunnel.me",
    "serveo.net",
    "serveousercontent.com",
    "localhost.run",
    "lhr.life",
    "devtunnels.ms",
    "pinggy.link",
    "pinggy.online",
)

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class OriginError(ValueError):
    """Entrada de origem inválida. ``reason`` é seguro para log (não contém o valor)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _valid_hostname(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if not host or len(host) > 253:
        return False
    return all(_LABEL.match(label) for label in host.split("."))


def normalize_origin(value: str) -> str:
    """Valida e normaliza UMA origem exata. Levanta ``OriginError`` se inválida."""
    v = (value or "").strip()
    if not v:
        raise OriginError("entrada vazia")
    if "*" in v:
        raise OriginError("curinga (*) não é permitido — use a origem exata")
    if any(c.isspace() for c in v):
        raise OriginError("espaço dentro da origem")
    parts = urlsplit(v)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not v.lower().startswith(scheme + "://"):
        raise OriginError("esquema deve ser http:// ou https://")
    if "@" in parts.netloc:
        raise OriginError("credenciais na origem não são permitidas")
    if parts.path not in ("", "/") or parts.query or parts.fragment or "?" in v or "#" in v:
        raise OriginError("somente esquema://host[:porta] (sem caminho, query ou fragmento)")
    host = (parts.hostname or "").lower()
    if not _valid_hostname(host):
        raise OriginError("host inválido")
    try:
        port = parts.port
    except ValueError:
        raise OriginError("porta inválida") from None
    if port == 0:
        raise OriginError("porta inválida")
    default_port = 443 if scheme == "https" else 80
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != default_port:
        netloc += f":{port}"
    return f"{scheme}://{netloc}"


def parse_origin_list(values: Iterable[str]) -> list[str]:
    """Normaliza uma lista; entradas vazias são ignoradas, inválidas levantam ``OriginError``
    com a POSIÇÃO (1-based) e o motivo (sem o valor). Remove duplicatas preservando a ordem."""
    out: list[str] = []
    for i, raw in enumerate(values or (), start=1):
        if not str(raw or "").strip():
            continue
        try:
            o = normalize_origin(str(raw))
        except OriginError as exc:
            raise OriginError(f"item {i}: {exc.reason}") from None
        if o not in out:
            out.append(o)
    return out


def merge_origins(*lists: Iterable[str]) -> list[str]:
    """União sem duplicatas (ordem preservada). Comparação por origem sem ``/`` final.
    Entradas com curinga são DESCARTADAS (o CORSMiddleware trataria ``*`` como "qualquer
    origem" e, com credenciais, ecoaria a origem do atacante)."""
    out: list[str] = []
    for lst in lists:
        for o in lst or ():
            v = str(o or "").strip().rstrip("/")
            if v and "*" not in v and v not in out:
                out.append(v)
    return out


def origin_in(origin: Optional[str], allowed: Iterable[str]) -> bool:
    """``True`` se ``origin`` (cabeçalho do navegador) está EXATAMENTE na lista permitida."""
    if not origin:
        return False
    return origin.strip().rstrip("/") in set(allowed)


def host_of(origin: str) -> str:
    return (urlsplit(origin).hostname or "").lower()


def is_loopback_host(host: str) -> bool:
    h = (host or "").lower().strip("[]")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def is_public_tunnel_host(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return any(h == s or h.endswith("." + s) for s in PUBLIC_TUNNEL_SUFFIXES)


def official_origin_problems(name: str, origins: Iterable[str]) -> list[str]:
    """Guardas de origem para produção/staging (mensagens só com NOMES, nunca valores):
    origem exata e bem-formada, sem curinga, sem loopback/localhost e sem túnel público.
    (A exigência de ``https://`` é verificada à parte pelos chamadores.)"""
    problems: list[str] = []

    def add(msg: str) -> None:
        if msg not in problems:
            problems.append(msg)

    for raw in origins or ():
        v = str(raw or "").strip()
        if not v:
            continue
        if "*" in v:
            add(f"{name} contém curinga (*) — somente origens exatas")
            continue
        try:
            o = normalize_origin(v)
        except OriginError:
            add(f"{name} contém origem malformada (use https://host[:porta] exato)")
            continue
        host = host_of(o)
        if is_loopback_host(host):
            add(f"{name} contém origem de loopback/localhost")
        if is_public_tunnel_host(host):
            add(f"{name} contém domínio de túnel público de desenvolvimento (ex.: ngrok)")
    return problems
