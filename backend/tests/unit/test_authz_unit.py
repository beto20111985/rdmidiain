"""AuthContext: resolução de escopos sem banco."""
import uuid

from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects import postgresql

from app.core.authz import AuthContext, Grant, ImpersonationInfo, city_key, normalize_city_key
from app.core.permissions import ROLE_TEMPLATES


def g(role, stype="GLOBAL", sid=None, sval=None):
    return Grant(uuid.uuid4(), uuid.uuid4(), role, ROLE_TEMPLATES[role].mfa_required, stype, sid, sval,
                 ROLE_TEMPLATES[role].permissions)


class _U:
    def __init__(self, seat=None):
        self.id = uuid.uuid4()
        self.admin_master_seat = seat
        self.totp_enabled = True

    @property
    def is_admin_master(self):
        return self.admin_master_seat in (1, 2)


def test_org_scope():
    a, b = uuid.uuid4(), uuid.uuid4()
    ctx = AuthContext(user=_U(), grants=(g("advertiser", "ORGANIZATION", a),))
    assert ctx.can("campaigns.view", org_id=a)
    assert not ctx.can("campaigns.view", org_id=b)
    assert not ctx.can("campaigns.view")  # sem escopo do recurso
    assert not ctx.can("campaigns.view", org_id=a, global_only=True)
    assert ctx.has_any("campaigns.view") and not ctx.has_any("finance.view")
    assert ctx.org_ids("campaigns.view") == {a}


def test_global_and_city_scope():
    ctx = AuthContext(user=_U(), grants=(g("technician", "CITY", sval=city_key("GO", "Goiânia")),))
    assert ctx.can("screens.view", city="GO:goiania")
    assert ctx.can("screens.view", city="go:Goiânia")
    assert not ctx.can("screens.view", city="SP:sao-paulo")
    adm = AuthContext(user=_U(), grants=(g("admin"),))
    assert adm.can("screens.view", org_id=uuid.uuid4()) and adm.org_ids("screens.view") is None


def test_finance_approve_only_admin_master():
    sa = AuthContext(user=_U(), grants=(g("super_admin"),))
    assert not sa.can("finance.approve")
    master = AuthContext(user=_U(seat=1), grants=(g("super_admin"),))
    assert master.can("finance.approve") and master.is_admin_master


def test_impersonation_blocks_sensitive():
    ctx = AuthContext(user=_U(), grants=(g("admin"),),
                      impersonation=ImpersonationInfo(uuid.uuid4(), "Real", uuid.uuid4(), "motivo"))
    assert not ctx.can("users.manage")
    assert not ctx.can("pix_keys.manage")
    assert ctx.can("campaigns.view")


def test_scope_filter_sql():
    t = Table("x", MetaData(), Column("org_id", postgresql.UUID), Column("city", String))
    a = uuid.uuid4()
    ctx = AuthContext(user=_U(), grants=(g("advertiser", "ORGANIZATION", a),))
    sql = str(ctx.scope_filter("campaigns.view", org_col=t.c.org_id).compile(dialect=postgresql.dialect()))
    assert "org_id IN" in sql
    assert str(ctx.scope_filter("finance.view", org_col=t.c.org_id).compile(dialect=postgresql.dialect())) == "false"
    adm = AuthContext(user=_U(), grants=(g("admin"),))
    assert str(adm.scope_filter("campaigns.view", org_col=t.c.org_id).compile(dialect=postgresql.dialect())) == "true"


def test_city_key_normalization():
    assert city_key("go", "Goiânia") == "GO:goiania"
    assert normalize_city_key("5208707") == "5208707"
    assert normalize_city_key("SP:São Paulo") == "SP:sao-paulo"
