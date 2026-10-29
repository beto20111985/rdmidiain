"""
Push — interface ``PushProvider`` + implementação NULA.

Nenhum provedor de push (FCM/APNs/Web Push) está configurado nesta instalação:
é dependência externa (credenciais do provedor). ``NullPushProvider`` NUNCA
declara envio: o ``notify`` registra a entrega como ``UNAVAILABLE`` com o detalhe
``push: indisponível (dependência externa)``. Para ativar no futuro, implemente
``PushProvider`` e registre com ``set_push_provider``.
"""
from __future__ import annotations

from typing import Optional, Protocol

PUSH_UNAVAILABLE_DETAIL = "push: indisponível (dependência externa — provedor de push não configurado)"


class PushProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    async def send(self, user_id: str, title: str, body: str, data: Optional[dict] = None) -> None: ...


class NullPushProvider:
    name = "null"

    @property
    def available(self) -> bool:
        return False

    async def send(self, user_id: str, title: str, body: str, data: Optional[dict] = None) -> None:
        raise RuntimeError(PUSH_UNAVAILABLE_DETAIL)


_provider: PushProvider = NullPushProvider()


def get_push_provider() -> PushProvider:
    return _provider


def set_push_provider(provider: Optional[PushProvider]) -> None:
    global _provider
    _provider = provider or NullPushProvider()
