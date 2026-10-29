"""P002 item 5: nenhuma ocorrência de comissão de 30 % (0.30 / 30% / commission_rate /
commission_pool fixos) no backend; nenhum float/Decimal armazenado em colunas financeiras."""
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

PATTERNS = [
    re.compile(r"commission_rate\s*=\s*0?\.3\b"),
    re.compile(r"\b0\.30\b"),
    re.compile(r"30\s*%\s*(de\s+)?comiss", re.IGNORECASE),
    re.compile(r"comiss\w*\s+de\s+30\s*%", re.IGNORECASE),
    re.compile(r"partner_commission_percent"),
    re.compile(r"commission_pool\s*=\s*.*0\.3"),
    re.compile(r"DEFAULT_COMMISSION_POOL_PERCENT\s*=\s*30"),
]


def test_no_thirty_percent_commission_anywhere_in_backend():
    hits = []
    for p in APP.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for pat in PATTERNS:
            for m in pat.finditer(text):
                hits.append(f"{p.relative_to(APP.parent)}:{text[:m.start()].count(chr(10)) + 1}: {m.group(0)}")
    assert hits == [], hits


def test_financial_models_have_no_float_or_numeric_columns():
    from sqlalchemy import Float, Numeric

    import app.models.financial as fin

    bad = []
    for name in dir(fin):
        cls = getattr(fin, name)
        table = getattr(cls, "__table__", None)
        if table is None or getattr(cls, "__module__", "") != fin.__name__:
            continue
        for col in table.columns:
            if isinstance(col.type, (Float, Numeric)):
                bad.append(f"{table.name}.{col.name}")
    assert bad == []


def test_default_pool_is_five_percent():
    from app.core.settings_service import REGISTRY

    assert REGISTRY["commission.pool_bps"].default == 500
    assert REGISTRY["commission.model"].default == "A_POOL"
    assert REGISTRY["commission.pool_bps"].financial is True
