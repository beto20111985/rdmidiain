"""
E2E contra o STACK VIVO (AÇÃO 002 / QA). Pulados por padrão.

Rodar (raiz do projeto, git-bash):
    MSYS_NO_PATHCONV=1 docker compose --profile test run --rm \
        -e RD_E2E_BASE_URL=http://nginx backend-tests \
        python -m pytest -q -s -m e2e tests/e2e

AÇÃO 003 (QA): ``test_e2e_qa003_*`` rodam contra INSTÂNCIAS DESCARTÁVEIS e só com ``RD_E2E_QA003=1``
(orquestração: ``stack/scripts/003/g/run_qa003_e2e.sh``; apoio: ``tests/e2e/qa003_support.py``).

Sem ``RD_E2E_BASE_URL`` todos os testes deste diretório são PULADOS (a suíte normal não
depende do stack vivo). Aqui NÃO se cria banco descartável: o fixture de sessão do
conftest raiz é sobrescrito por um no-op (as asserções leem o banco vivo, só SELECT).
"""
from __future__ import annotations

import os

import pytest

E2E_ENABLED = bool(os.environ.get("RD_E2E_BASE_URL"))
# AÇÃO 003 (QA): E2E contra INSTÂNCIAS DESCARTÁVEIS (modo único/dupla, 2º Master, transições) —
# habilitados SOMENTE por RD_E2E_QA003 (ver tests/e2e/qa003_support.py e stack/scripts/003/g/run_qa003_e2e.sh).
QA003_ENABLED = bool(os.environ.get("RD_E2E_QA003"))


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="E2E contra o stack vivo: defina RD_E2E_BASE_URL (ver tests/e2e/conftest.py)")
    skip_qa003 = pytest.mark.skip(reason="E2E AÇÃO 003 em instâncias descartáveis: defina RD_E2E_QA003=1 "
                                         "(ver tests/e2e/qa003_support.py)")
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/tests/e2e/" not in path:
            continue
        if "/tests/e2e/test_e2e_qa003_" in path:
            if not QA003_ENABLED:
                item.add_marker(skip_qa003)
        elif not E2E_ENABLED:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Sobrescreve o fixture do conftest raiz: E2E não usa banco descartável."""
    yield None
