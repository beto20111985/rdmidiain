"""Fixtures do domínio W-ORG (PostgreSQL/Redis reais via harness do W1)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from tests.integration.org_api.helpers import staff_session


@pytest.fixture
def storage(tmp_path):
    from app.core.storage import LocalStorageProvider, set_storage

    p = LocalStorageProvider(tmp_path / "storage")
    set_storage(p)
    yield p
    set_storage(None)


@pytest_asyncio.fixture
async def admin(client, db):
    return await staff_session(client, db, "admin")
