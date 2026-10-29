"""Execução de deploy das instâncias — interface ``DeploymentExecutor``.

A execução REAL (Portainer: atualizar a stack da instância para a imagem da versão) é
DEPENDÊNCIA EXTERNA nesta ação: não há endpoint/credencial do Portainer configurados e
é proibido executar deploy. Implementações:

- ``NullDeploymentExecutor`` (padrão): não executa nada; o alvo fica
  ``BLOQUEADO_DEPENDENCIA`` com o motivo registrado no histórico.
- ``LocalSimulationExecutor``: SIMULAÇÃO LOCAL claramente rotulada para exercitar a fila,
  o limite de concorrência e o rollback. O alvo fica ``EM_EXECUCAO`` até um passo de
  simulação concluí-lo como ``SIMULADO``. Nunca altera a versão reportada pela instância
  (que só muda pelo heartbeat da própria instância). Proibida em produção.

Para o futuro: ``PortainerDeploymentExecutor`` implementa ``start`` disparando o
update da stack e a conclusão vem pelo heartbeat da instância reportando ``to_version``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

NULL_DETAIL = ("Execução real indisponível: integração com Portainer não configurada "
               "(dependência externa). Nenhum deploy foi executado.")
SIMULATION_DETAIL = "SIMULAÇÃO LOCAL — nenhum deploy real foi executado."


@dataclass
class ExecResult:
    status: str  # EM_EXECUCAO | BLOQUEADO_DEPENDENCIA | FALHOU
    detail: str


class DeploymentExecutor(Protocol):
    name: str
    simulated: bool

    async def start(self, instance: Any, release: Any, target: Any) -> ExecResult: ...


class NullDeploymentExecutor:
    name = "NULO"
    simulated = False

    async def start(self, instance: Any, release: Any, target: Any) -> ExecResult:
        return ExecResult("BLOQUEADO_DEPENDENCIA", NULL_DETAIL)


class LocalSimulationExecutor:
    name = "SIMULACAO_LOCAL"
    simulated = True

    async def start(self, instance: Any, release: Any, target: Any) -> ExecResult:
        return ExecResult("EM_EXECUCAO", SIMULATION_DETAIL)


def executor_for(name: str) -> DeploymentExecutor:
    from app.core.config import settings

    if name == "SIMULACAO_LOCAL":
        if settings.is_production:
            raise ValueError("simulação local proibida em produção")
        return LocalSimulationExecutor()
    return NullDeploymentExecutor()
