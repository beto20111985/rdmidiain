"""
Apoio aos testes do W-CAMP (campanhas, conflitos, preços, checkout) — PostgreSQL REAL.

- ``Tax``: taxonomia mínima (setores/categorias/segmentos/política de conteúdo) gravada no banco.
- ``make_est``/``make_screen``/``make_media``/``make_price_table``: dados reais nas tabelas
  dos domínios (W-ORG/W-MEDIA) com os campos obrigatórios.
- ``FakeDeps``: substitui SOMENTE as dependências de outros domínios (W-INV ``hold`` /
  ``convert_hold`` / ``release_hold`` / ``invalidate_screens`` e W-FIN ``create_payment_order``)
  registrando as chamadas — o núcleo testado (W-CAMP) não é mockado. O E2E integrado é do QA.
"""
from __future__ import annotations

import hashlib
import secrets
import sys
import types
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_screen_code
from tests.factories import make_establishment, make_org

UTC = timezone.utc


def _hex(u: uuid.UUID) -> str:
    return u.hex


class Tax:
    """ALIMENTAÇÃO > {Supermercado, Distribuidora de bebidas}; SAÚDE > Academia.
    Segmentos: bebidas > alcoólicas > cerveja; alimentos > snacks; apostas (BLOCK)."""

    async def create(self, db: AsyncSession) -> "Tax":
        from app.models.taxonomy import ContentPolicy, Segment, TaxonomyNode

        sfx = secrets.token_hex(3)

        def node(kind, name, parent=None):
            i = uuid.uuid4()
            path = (parent.path if parent else "/") + f"{_hex(i)}/"
            n = TaxonomyNode(id=i, kind=kind, parent_id=parent.id if parent else None, name=name,
                             slug=f"{name.lower().replace(' ', '-')}-{sfx}", path=path,
                             depth=0 if parent is None else parent.depth + 1, is_active=True)
            db.add(n)
            return n

        def seg(name, parent=None):
            i = uuid.uuid4()
            path = (parent.path if parent else "/") + f"{_hex(i)}/"
            s = Segment(id=i, parent_id=parent.id if parent else None, name=name,
                        slug=f"{name.lower().replace(' ', '-')}-{sfx}", path=path,
                        depth=0 if parent is None else parent.depth + 1, is_active=True)
            db.add(s)
            return s

        self.alimentacao = node("SECTOR", "Alimentacao")
        self.supermercado = node("CATEGORY", "Supermercado", self.alimentacao)
        self.distribuidora = node("CATEGORY", "Distribuidora de bebidas", self.alimentacao)
        self.saude = node("SECTOR", "Saude")
        self.academia = node("CATEGORY", "Academia", self.saude)
        self.bebidas = seg("bebidas")
        self.alcoolicas = seg("alcoolicas", self.bebidas)
        self.cerveja = seg("cerveja", self.alcoolicas)
        self.alimentos = seg("alimentos")
        self.snacks = seg("snacks", self.alimentos)
        self.apostas = seg("apostas")
        await db.flush()
        db.add(ContentPolicy(segment_id=self.apostas.id, effect="BLOCK", reason="Política padrão: apostas bloqueadas",
                             is_default=True))
        await db.commit()
        return self


async def set_advertiser(db: AsyncSession, org, *, category=None, segments=()) -> None:
    from app.models.taxonomy import AdvertiserProfile, AdvertiserSegment

    db.add(AdvertiserProfile(org_id=org.id, category_id=category.id if category else None))
    for s in segments:
        db.add(AdvertiserSegment(org_id=org.id, segment_id=s.id))
    await db.commit()


async def make_est(db: AsyncSession, org=None, *, city="Goiânia", state="GO", tz="America/Sao_Paulo", category=None,
                   segments=(), neighborhood: Optional[str] = None, postal_code: Optional[str] = None,
                   venue_class: str = "STANDARD"):
    from app.models.taxonomy import EstablishmentSegment

    org = org or await make_org(db)
    e = await make_establishment(db, org, city=city, state=state, tz=tz)
    e.category_id = category.id if category else None
    e.neighborhood = neighborhood
    e.postal_code = postal_code
    e.venue_class = venue_class
    for s in segments:
        db.add(EstablishmentSegment(establishment_id=e.id, segment_id=s.id))
    await db.commit()
    await db.refresh(e)
    return e


async def make_screen(db: AsyncSession, est, *, orientation="HORIZONTAL", sellable=True, status="ACTIVE",
                      screen_class="STANDARD"):
    from app.models.screens import Screen

    s = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=new_screen_code(est.state),
               name=f"Tela {secrets.token_hex(2)}", orientation=orientation, sellable=sellable, status=status,
               screen_class=screen_class)
    if status == "DECOMMISSIONED":
        s.decommissioned_at = datetime.now(UTC)
        s.decommission_reason = "teste"
        s.sellable = False
    db.add(s)
    await db.commit()
    return s


async def make_media(db: AsyncSession, org, *, kind="VIDEO", duration_ms: Optional[int] = 30000,
                     orientation="HORIZONTAL", status="READY"):
    from app.models.media import MediaFile

    h = hashlib.sha256(secrets.token_bytes(16)).hexdigest()
    w, hgt = (1920, 1080) if orientation == "HORIZONTAL" else (1080, 1920)
    m = MediaFile(
        id=uuid.uuid4(), org_id=org.id, name="midia", original_filename="midia.mp4", kind=kind,
        source_format="mp4" if kind == "VIDEO" else "png", source_mime="video/mp4" if kind == "VIDEO" else "image/png",
        source_size_bytes=1000, source_sha256=h, status=status,
        storage_key=f"test/{h}" if status == "READY" else None, sha256=h if status == "READY" else None,
        size_bytes=1000 if status == "READY" else None, width=w if status == "READY" else None,
        height=hgt if status == "READY" else None, duration_ms=duration_ms if kind == "VIDEO" else None,
        orientation=orientation, mime_type="video/mp4" if kind == "VIDEO" else "image/png",
    )
    db.add(m)
    await db.commit()
    return m


BASE_ITEMS = [
    ("DURATION_BASE", "30", "BASE_CENTS", None, None, 10),
    ("DURATION_BASE", "60", "BASE_CENTS", None, None, 18),
    ("DURATION_BASE", "120", "BASE_CENTS", None, None, 32),
    ("EXCLUSIVITY", "COMPETITOR_EXCLUSIVE", "MULTIPLIER_BPS", None, None, 15000),
    ("FEE", "ART", "FIXED_CENTS", None, None, 15000),
    ("COST_GATEWAY", "*", "PERCENT_BPS", None, None, 199),
    ("TAX", "ISS", "PERCENT_BPS", None, None, 500),
]


async def make_price_table(db: AsyncSession, extra=(), *, activate=True, base=BASE_ITEMS):
    from app.models.pricing import PriceItem, PriceTable

    from sqlalchemy import func, select

    v = int((await db.execute(select(func.max(PriceTable.version)))).scalar() or 0) + 1
    t = PriceTable(id=uuid.uuid4(), name=f"Tabela {v}", version=v, status="ATIVA" if activate else "RASCUNHO",
                   activated_at=datetime.now(UTC) if activate else None)
    db.add(t)
    await db.flush()
    for kind, key, mode, lo, hi, value in list(base) + list(extra):
        db.add(PriceItem(id=uuid.uuid4(), table_id=t.id, kind=kind, key=key, mode=mode, min_value=lo, max_value=hi,
                         value=value))
    await db.commit()
    return t


def future_monday(weeks: int = 1) -> date:
    d = date.today() + timedelta(days=7 * weeks)
    return d - timedelta(days=d.weekday())


class FakeDeps:
    """Dependências de outros domínios (W-INV/W-FIN) — registra chamadas; o núcleo W-CAMP é real."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.holds: dict = {}
        self.payments: dict = {}
        self.convert_status = "CONVERTED"
        self.payment_status = "PENDING"
        self.refund_errors: dict = {}
        self.last_payment_kwargs: dict = {}
        self.fail_hold: Optional[Exception] = None
        inv = types.ModuleType("app.modules.inventory.service")
        fin = types.ModuleType("app.modules.financial.service")

        async def hold(db, ctx, campaign_id, screen_ids, patterns, idempotency_key, ttl, replaces_hold_id=None):
            if self.fail_hold:
                raise self.fail_hold
            hid = uuid.uuid4()
            self.holds[hid] = {"campaign_id": campaign_id, "screens": list(screen_ids), "patterns": patterns,
                               "key": idempotency_key, "ttl": ttl, "replaces": replaces_hold_id, "status": "HELD"}
            self.calls.append(("hold", campaign_id, idempotency_key))
            return types.SimpleNamespace(id=hid, status="HELD")

        async def convert_hold(db, hold_id):
            self.calls.append(("convert_hold", hold_id))
            self.holds.setdefault(hold_id, {})["status"] = self.convert_status
            return {"status": self.convert_status, "hold_id": hold_id}

        async def release_hold(db, hold_id, reason):
            self.calls.append(("release_hold", hold_id, reason))
            if hold_id in self.holds:
                self.holds[hold_id]["status"] = "RELEASED:" + reason

        async def invalidate_screens(db, screen_ids, reason):
            self.calls.append(("invalidate_screens", len(screen_ids), reason))

        async def create_payment_order(db, ctx, org_id, items, amount_cents, expires_at, reference, *,
                                       method="pix", payment_source_token=None, installments=1,
                                       seller_user_id=None, **_):
            self.last_payment_kwargs = {"method": method, "installments": installments,
                                        "has_token": payment_source_token is not None,
                                        "token": payment_source_token, "seller_user_id": seller_user_id}
            assert isinstance(amount_cents, int) and amount_cents == sum(
                i["amount_cents"] if i["kind"] in ("MEDIA", "SERVICE") else -i["amount_cents"] for i in items)
            assert all(i["kind"] in ("MEDIA", "SERVICE", "DISCOUNT") and i["amount_cents"] > 0 for i in items)
            if self.payment_status == "CREATION_FAILED":
                pid = uuid.uuid4()
                self.calls.append(("create_payment_order_failed", amount_cents))
                return types.SimpleNamespace(id=pid, status="CREATION_FAILED", amount_cents=amount_cents)
            pid = uuid.uuid4()
            self.payments[pid] = {"org_id": org_id, "items": items, "amount_cents": amount_cents,
                                  "expires_at": expires_at, "reference": reference}
            self.calls.append(("create_payment_order", amount_cents, reference["type"]))
            return types.SimpleNamespace(id=pid, status="PENDING", amount_cents=amount_cents,
                                         pix_copy_paste="000201-TESTE-NAO-E-REAL", expires_at=expires_at)

        inv.hold, inv.convert_hold, inv.release_hold, inv.invalidate_screens = (hold, convert_hold, release_hold,
                                                                                invalidate_screens)
        async def request_refund(db, ctx, payment_order_id, campaign_id, reason):
            self.calls.append(("request_refund", payment_order_id, campaign_id))
            code = self.refund_errors.get(payment_order_id)
            if code:
                from app.core.errors import Conflict

                raise Conflict(code, "teste")
            return types.SimpleNamespace(id=uuid.uuid4(), status="REQUESTED")

        async def quote_refund(db, payment_order_id, campaign_id=None, as_of=None):
            self.calls.append(("quote_refund", payment_order_id, campaign_id))
            amount = self.payments.get(payment_order_id, {}).get("amount_cents", 0)
            return {"payment_order_id": str(payment_order_id), "campaign_id": str(campaign_id),
                    "policy": "FULL_BEFORE_START", "allowed": True, "refund_cents": amount}

        fin.create_payment_order = create_payment_order
        fin.request_refund = request_refund
        fin.quote_refund = quote_refund
        self.inv, self.fin = inv, fin

    def install(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "app.modules.inventory.service", self.inv)
        monkeypatch.setitem(sys.modules, "app.modules.financial.service", self.fin)
        return self

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]
