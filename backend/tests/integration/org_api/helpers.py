"""Helpers de teste do domínio W-ORG."""
from __future__ import annotations

import secrets
import uuid
from typing import Optional

from app.models.establishments import Establishment
from app.models.organizations import Organization
from app.modules.organizations.documents import make_cnpj, make_cpf
from tests.factories import login_as, make_user


def new_cnpj() -> str:
    return make_cnpj("".join(secrets.choice("0123456789") for _ in range(12)))


def new_cpf() -> str:
    return make_cpf("".join(secrets.choice("0123456789") for _ in range(9)))


async def mk_org(db, name: Optional[str] = None, roles=("PARTNER",), city=None, state=None) -> Organization:
    from app.core.authz import city_key

    o = Organization(id=uuid.uuid4(), name=name or f"Org {secrets.token_hex(3)}", document=new_cnpj(),
                     document_type="CNPJ", business_roles=list(roles), is_active=True, city=city, state=state,
                     city_key=city_key(state, city) if city and state else None)
    db.add(o)
    await db.commit()
    return o


async def mk_est(db, org: Organization, city: str = "Goiânia", state: str = "GO", *, name: Optional[str] = None,
                 onboarding_status: str = "ACTIVE", tz: str = "America/Sao_Paulo", **kw) -> Establishment:
    e = Establishment(id=uuid.uuid4(), org_id=org.id, name=name or f"Est {secrets.token_hex(3)}", city=city,
                      state=state, country="BRA", timezone=tz, is_active=True, onboarding_status=onboarding_status,
                      **kw)
    db.add(e)
    await db.commit()
    return e


async def staff_session(client, db, role: str = "admin", scope: Optional[tuple] = None):
    u = await make_user(db, role=role, scope=scope, mfa=role in ("admin", "super_admin", "financial"))
    return await login_as(client, u)


async def audit_actions(db, entity_id=None) -> list[str]:
    from sqlalchemy import select

    from app.models.audit import AuditLog

    q = select(AuditLog.action).order_by(AuditLog.seq)
    if entity_id is not None:
        q = q.where(AuditLog.entity_id == str(entity_id))
    return list((await db.execute(q)).scalars().all())
