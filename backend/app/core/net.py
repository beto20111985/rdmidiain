"""
RD Mídia Indoor — IP real do cliente atrás de proxy.

Somente quando o par TCP (``request.client.host``) pertence a ``TRUSTED_PROXIES``
os cabeçalhos ``X-Forwarded-For``/``X-Real-IP`` são considerados. Em
``X-Forwarded-For`` percorre-se da DIREITA para a esquerda, pulando proxies
confiáveis: o primeiro IP não confiável é o cliente (o lado esquerdo, controlado
pelo cliente, nunca é aceito cegamente — corrige A7 M1).
"""
from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Iterable, Optional

from app.core.config import settings


@lru_cache(maxsize=1)
def _trusted_networks(cidrs: tuple[str, ...]) -> tuple:
    nets = []
    for c in cidrs:
        try:
            nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def _parse_ip(value: str) -> Optional[ipaddress._BaseAddress]:
    value = (value or "").strip().strip('"')
    if value.startswith("[") and "]" in value:  # [ipv6]:port
        value = value[1 : value.index("]")]
    elif value.count(":") == 1 and "." in value:  # ipv4:port
        value = value.split(":", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def is_trusted(ip: Optional[ipaddress._BaseAddress], cidrs: Iterable[str] | None = None) -> bool:
    if ip is None:
        return False
    nets = _trusted_networks(tuple(cidrs if cidrs is not None else settings.TRUSTED_PROXIES))
    return any(ip in n for n in nets)


def resolve_client_ip(
    peer: Optional[str],
    x_forwarded_for: Optional[str],
    x_real_ip: Optional[str],
    trusted: Iterable[str] | None = None,
) -> str:
    peer_ip = _parse_ip(peer or "")
    if peer_ip is None:
        return peer or "unknown"
    if not is_trusted(peer_ip, trusted):
        return str(peer_ip)
    if x_forwarded_for:
        chain = [p for p in (x.strip() for x in x_forwarded_for.split(",")) if p]
        for raw in reversed(chain):
            ip = _parse_ip(raw)
            if ip is None:
                # entrada inválida: não confiar em nada à esquerda dela
                break
            if not is_trusted(ip, trusted):
                return str(ip)
        else:
            if chain:
                first = _parse_ip(chain[0])
                if first is not None:
                    return str(first)
    if x_real_ip:
        ip = _parse_ip(x_real_ip)
        if ip is not None:
            return str(ip)
    return str(peer_ip)


def client_ip(request) -> str:
    peer = request.client.host if getattr(request, "client", None) else None
    return resolve_client_ip(
        peer,
        request.headers.get("x-forwarded-for"),
        request.headers.get("x-real-ip"),
    )
