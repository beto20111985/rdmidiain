"""IP real atrás de proxy confiável (não confiar em X-Forwarded-For arbitrário)."""
from app.core.net import resolve_client_ip

TRUSTED = ["127.0.0.1/32", "172.16.0.0/12"]


def test_untrusted_peer_ignores_headers():
    assert resolve_client_ip("203.0.113.9", "1.2.3.4", "5.6.7.8", TRUSTED) == "203.0.113.9"


def test_trusted_proxy_uses_rightmost_untrusted():
    # cliente forja "1.1.1.1" à esquerda; nginx acrescenta o IP real à direita
    assert resolve_client_ip("172.18.0.5", "1.1.1.1, 198.51.100.7", None, TRUSTED) == "198.51.100.7"


def test_trusted_chain_skips_internal_proxies():
    assert resolve_client_ip("172.18.0.5", "198.51.100.7, 172.18.0.9", None, TRUSTED) == "198.51.100.7"


def test_x_real_ip_when_no_xff():
    assert resolve_client_ip("127.0.0.1", None, "198.51.100.8", TRUSTED) == "198.51.100.8"


def test_garbage_header():
    assert resolve_client_ip("172.18.0.5", "not-an-ip", None, TRUSTED) == "172.18.0.5"
