"""
RD Mídia Indoor — Instância / white-label (AÇÃO 002 / W-BIZ).

Interfaces para outros domínios:
- ``assert_price_not_below_floor(db, floor_key, price_cents)`` → ``DomainError``
  ``PRICE_BELOW_RD_MINIMUM`` (400) se o preço da instância for menor que o piso RD.
  Usado pelo W-CAMP ao gravar tabela de preços/planos (ETAPA2 #7).
- ``brand_name(db)`` / ``get_profile(db)`` — marca da instância (PDFs, e-mails).
- ``sign_payload(db, data)`` / ``verify_signature(db, kid, data, sig)`` — ES256 com a
  chave da INSTÂNCIA (comprovantes de veiculação). A chave privada fica cifrada em
  repouso (AES-256-GCM, chave derivada de DATA_ENCRYPTION_KEY/SECRET_KEY).
- ``resolve_payment_secret(db)`` — valor do segredo CloudPayPag desta instância a
  partir da REFERÊNCIA configurada (``env:NOME`` ou ``file:/run/secrets/...``); uso
  interno do W-FIN; nunca exposto em API/log.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, NotFound
from app.core.timeutils import utcnow

log = structlog.get_logger("rd.instance")

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SECRET_REF_RE = re.compile(r"^(env:[A-Z_][A-Z0-9_]{1,80}|file:/run/secrets/[A-Za-z0-9_.-]{1,100})$")
FLOOR_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,79}$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")

# Tokens do design system (stack/RD_MIDIA_INDOOR_DESIGN_SYSTEM.md): a marca da
# instância só substitui ``action`` (botão com texto branco) e ``brand-blue``
# (destaque sobre o fundo escuro ``canvas``). Contraste mínimo WCAG AA.
CANVAS_DARK = "#05070A"
TEXT_ON_ACTION = "#FFFFFF"
DEFAULT_PRIMARY = "#0056B3"
DEFAULT_ACCENT = "#0B84FF"
MIN_CONTRAST_TEXT = 4.5
MIN_CONTRAST_UI = 3.0

DEFAULTS = {
    "instance_slug": "rd",
    "display_name": "RD Mídia Indoor",
    "is_white_label": False,
    "primary_color": DEFAULT_PRIMARY,
    "accent_color": DEFAULT_ACCENT,
}


# ─── Cores ───────────────────────────────────────────────────────────────────

def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def validate_brand_colors(primary: Optional[str], accent: Optional[str]) -> None:
    for name, val in (("primary_color", primary), ("accent_color", accent)):
        if val is not None and not HEX_RE.match(val):
            raise DomainError("BRAND_COLOR_INVALID", "Cor inválida (use #RRGGBB).", status=400, details={"field": name})
    if primary is not None and contrast_ratio(primary, TEXT_ON_ACTION) < MIN_CONTRAST_TEXT:
        raise DomainError(
            "BRAND_COLOR_CONTRAST",
            "A cor principal precisa ter contraste mínimo de 4,5:1 com texto branco (token action).",
            status=400, details={"field": "primary_color", "ratio": round(contrast_ratio(primary, TEXT_ON_ACTION), 2)},
        )
    if accent is not None and contrast_ratio(accent, CANVAS_DARK) < MIN_CONTRAST_UI:
        raise DomainError(
            "BRAND_COLOR_CONTRAST",
            "A cor de destaque precisa ter contraste mínimo de 3:1 com o fundo escuro (token canvas).",
            status=400, details={"field": "accent_color", "ratio": round(contrast_ratio(accent, CANVAS_DARK), 2)},
        )


# ─── Perfil ──────────────────────────────────────────────────────────────────

async def get_profile(db: AsyncSession, *, for_update: bool = False):
    from app.models.instance import InstanceProfile

    q = select(InstanceProfile).where(InstanceProfile.id == 1)
    if for_update:
        q = q.with_for_update()
    return (await db.execute(q)).scalar_one_or_none()


async def get_or_create_profile(db: AsyncSession):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.instance import InstanceProfile

    await db.execute(
        pg_insert(InstanceProfile.__table__)
        .values(id=1, instance_slug=DEFAULTS["instance_slug"], display_name=DEFAULTS["display_name"],
                is_white_label=False, payment_provider="CLOUDPAYPAG", version=1)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return await get_profile(db, for_update=True)


async def brand_name(db: AsyncSession) -> str:
    p = await get_profile(db)
    return (p.display_name if p else None) or DEFAULTS["display_name"]


@dataclass
class Brand:
    """Marca da instância para documentos gerados no servidor (PDF/XLSX/e-mail). Fallback = RD."""

    name: str = "RD Mídia Indoor"
    primary_color: str = DEFAULT_PRIMARY
    accent_color: str = DEFAULT_ACCENT
    is_white_label: bool = False
    logo: Optional[bytes] = None
    logo_content_type: Optional[str] = None
    support_email: Optional[str] = None
    support_phone: Optional[str] = None

    @property
    def staff_label(self) -> str:
        """Rótulo da equipe da instância exibido a clientes (sem nome de funcionário)."""
        return f"Equipe {self.name}" if self.is_white_label else "Equipe RD"

    def pdf_kwargs(self) -> dict:
        return {"brand_name": self.name, "brand_color": self.primary_color, "logo": self.logo}


MAX_LOGO_BYTES = 1024 * 1024


async def get_brand(db: AsyncSession, *, with_logo: bool = True) -> Brand:
    """Marca vigente (nome, cores, logo). Logo ausente/ilegível → sem logo (nunca falha a geração)."""
    p = await get_profile(db)
    b = Brand(
        name=(getattr(p, "display_name", None) or DEFAULTS["display_name"]),
        primary_color=(getattr(p, "primary_color", None) or DEFAULT_PRIMARY),
        accent_color=(getattr(p, "accent_color", None) or DEFAULT_ACCENT),
        is_white_label=bool(getattr(p, "is_white_label", False)),
        support_email=getattr(p, "support_email", None),
        support_phone=getattr(p, "support_phone", None),
    )
    if with_logo and p is not None and p.logo_key:
        try:
            from app.core.storage import get_storage

            b.logo = await get_storage().read_bytes(p.logo_key, max_bytes=MAX_LOGO_BYTES)
            b.logo_content_type = p.logo_content_type
        except Exception as exc:  # logo é opcional: registra e segue sem ela
            log.warning("instance_logo_unavailable", error_type=type(exc).__name__)
    return b


def email_html(brand: Brand, title: str, body_text: str, *, link: Optional[str] = None,
               footer: Optional[str] = None) -> str:
    """HTML simples e seguro (tudo escapado) com a marca da instância. Logo por URL pública do painel
    (``/api/v1/instance/logo``) — host vem da configuração (``PANEL_BASE_URL``/``FRONTEND_URL``)."""
    from html import escape

    from app.core.config import settings

    base = settings.panel_base_url
    logo = (f'<img src="{escape(base)}/api/v1/instance/logo" alt="{escape(brand.name)}" height="32" '
            f'style="display:block;border:0;margin-bottom:8px">') if brand.logo else ""
    paragraphs = "".join(f"<p style=\"margin:0 0 12px\">{escape(par).replace(chr(10), '<br>')}</p>"
                         for par in (body_text or "").split("\n\n") if par.strip())
    button = (f'<p><a href="{escape(link)}" style="display:inline-block;padding:10px 16px;background:{brand.primary_color};'
              f'color:#FFFFFF;text-decoration:none;border-radius:6px">Abrir no painel</a></p>') if link else ""
    foot = escape(footer or brand.name)
    return (
        "<!doctype html><html><body style=\"margin:0;background:#F5F7FA;font-family:Arial,Helvetica,sans-serif;color:#0F172A\">"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\"><tr><td align=\"center\" style=\"padding:24px\">"
        "<table role=\"presentation\" width=\"600\" cellpadding=\"0\" cellspacing=\"0\" style=\"background:#FFFFFF;border-radius:8px\">"
        f"<tr><td style=\"background:{brand.primary_color};color:#FFFFFF;padding:16px 24px;border-radius:8px 8px 0 0\">"
        f"{logo}<strong style=\"font-size:16px\">{escape(brand.name)}</strong></td></tr>"
        f"<tr><td style=\"padding:24px\"><h1 style=\"font-size:18px;margin:0 0 16px\">{escape(title)}</h1>{paragraphs}{button}</td></tr>"
        f"<tr><td style=\"padding:12px 24px;color:#475569;font-size:12px;border-top:1px solid #E2E8F0\">{foot}</td></tr>"
        "</table></td></tr></table></body></html>"
    )


def branding_etag(payload: dict) -> str:
    import hashlib
    import json

    return '"' + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32] + '"'


def normalize_document(doc: Optional[str]) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", doc or "").upper()


async def instance_org_ids(db: AsyncSession) -> set:
    """Organizações que SÃO a própria instância (mesmo documento de ``instance_profile.document``).
    Nunca aparecem como cliente (proposta, lead). Sem documento configurado → conjunto vazio."""
    from app.models.organizations import Organization

    p = await get_profile(db)
    doc = normalize_document(getattr(p, "document", None))
    if not doc:
        return set()
    rows = await db.execute(select(Organization.id).where(
        func.upper(func.regexp_replace(Organization.document, "[^0-9A-Za-z]", "", "g")) == doc))
    return set(rows.scalars())


def payment_credentials_status(ref: Optional[str]) -> dict:
    """Estado da credencial SEM revelar o valor (apenas se a referência resolve)."""
    if not ref:
        return {"configured": False, "ref_kind": None, "detail": "Referência ao segredo CloudPayPag não configurada."}
    kind, _, target = ref.partition(":")
    ok = False
    if kind == "env":
        ok = bool(os.environ.get(target))
    elif kind == "file":
        try:
            ok = os.path.isfile(target) and os.path.getsize(target) > 0
        except OSError:
            ok = False
    return {
        "configured": ok,
        "ref_kind": kind,
        "detail": None if ok else "Referência configurada, mas o segredo não está presente neste ambiente "
                                  "(dependência externa: conta CloudPayPag da instância).",
    }


async def resolve_payment_secret(db: AsyncSession) -> Optional[str]:
    """USO INTERNO (W-FIN). Nunca devolva este valor em resposta HTTP ou log."""
    p = await get_profile(db)
    ref = p.payment_credentials_ref if p else None
    if not ref:
        return None
    kind, _, target = ref.partition(":")
    if kind == "env":
        return os.environ.get(target) or None
    if kind == "file":
        try:
            with open(target, "r", encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None
    return None


def profile_public(p) -> dict:
    return {
        "instance_slug": getattr(p, "instance_slug", None) or DEFAULTS["instance_slug"],
        "display_name": getattr(p, "display_name", None) or DEFAULTS["display_name"],
        "is_white_label": bool(getattr(p, "is_white_label", False)),
        "primary_color": getattr(p, "primary_color", None) or DEFAULT_PRIMARY,
        "accent_color": getattr(p, "accent_color", None) or DEFAULT_ACCENT,
        "has_logo": bool(getattr(p, "logo_key", None)),
        "support_email": getattr(p, "support_email", None),
        "support_phone": getattr(p, "support_phone", None),
    }


def profile_admin(p) -> dict:
    out = profile_public(p)
    out.update(
        {
            "legal_name": getattr(p, "legal_name", None),
            "document": getattr(p, "document", None),
            "subdomain": getattr(p, "subdomain", None),
            "subdomain_note": "Roteamento por subdomínio é configurado no proxy de borda da VPS (fase futura).",
            "payment_provider": "CLOUDPAYPAG",
            "payment_credentials_ref": getattr(p, "payment_credentials_ref", None),
            "payment_account_label": getattr(p, "payment_account_label", None),
            "payment_credentials": payment_credentials_status(getattr(p, "payment_credentials_ref", None)),
            "version": getattr(p, "version", 0),
            "isolation": {
                "model": "STACK_POR_INSTANCIA",
                "detail": "Cada instância roda em stack Docker própria (PostgreSQL, Redis, volumes e configuração "
                          "independentes). Não há banco compartilhado nem acesso cruzado; código-fonte não é "
                          "distribuído (imagens fechadas, gerenciadas pela RD via control plane).",
            },
        }
    )
    return out


ALLOWED_PROFILE_FIELDS = {
    "instance_slug", "display_name", "legal_name", "document", "is_white_label", "support_email", "support_phone",
    "primary_color", "accent_color", "subdomain", "payment_credentials_ref", "payment_account_label",
}


async def update_profile(db: AsyncSession, ctx: Any, changes: dict, *, expected_version: Optional[int] = None) -> Any:
    from app.core.audit import audit

    bad = set(changes) - ALLOWED_PROFILE_FIELDS
    if bad:
        raise DomainError("FIELD_NOT_ALLOWED", "Campo não editável.", status=400, details={"fields": sorted(bad)})
    p = await get_or_create_profile(db)
    if expected_version is not None and expected_version != p.version:
        raise DomainError("INSTANCE_VERSION_CONFLICT", "A configuração foi alterada por outra pessoa. Recarregue.",
                          status=409, details={"current_version": p.version})
    validate_brand_colors(changes.get("primary_color"), changes.get("accent_color"))
    if changes.get("subdomain") is not None and not SUBDOMAIN_RE.match(changes["subdomain"]):
        raise DomainError("SUBDOMAIN_INVALID", "Subdomínio inválido.", status=400)
    if changes.get("instance_slug") is not None and not SLUG_RE.match(changes["instance_slug"]):
        raise DomainError("INSTANCE_SLUG_INVALID", "Identificador da instância inválido.", status=400)
    if changes.get("payment_credentials_ref") is not None and not SECRET_REF_RE.match(changes["payment_credentials_ref"]):
        raise DomainError(
            "SECRET_REF_INVALID",
            "Informe somente a REFERÊNCIA ao segredo (env:NOME_DA_VARIAVEL ou file:/run/secrets/arquivo), nunca o valor.",
            status=400,
        )
    if "display_name" in changes and not (changes["display_name"] or "").strip():
        raise DomainError("DISPLAY_NAME_REQUIRED", "Nome da instância é obrigatório.", status=400)
    before = {k: getattr(p, k) for k in changes}
    for k, v in changes.items():
        setattr(p, k, v)
    p.version += 1
    p.updated_by = getattr(ctx, "user_id", None)
    p.updated_at = utcnow()
    await db.flush()
    await audit(db, ctx, "instance.profile_updated", "instance_profile", "1", before=before,
                after={k: getattr(p, k) for k in changes} | {"version": p.version})
    return p


# ─── Pisos de preço RD ───────────────────────────────────────────────────────

async def get_floor(db: AsyncSession, floor_key: str):
    from app.models.instance import RdPriceFloor

    return await db.get(RdPriceFloor, floor_key)


async def floors_for(db: AsyncSession, floor_keys: Iterable[str]) -> dict[str, int]:
    """Pisos cadastrados para as chaves pedidas (uma consulta): ``{chave: min_price_cents}``.
    Chave sem piso não aparece (preço livre). Para o motor de preços avaliar vários itens de uma vez."""
    from app.models.instance import RdPriceFloor

    keys = sorted({k for k in floor_keys if k})
    if not keys:
        return {}
    rows = await db.execute(select(RdPriceFloor.key, RdPriceFloor.min_price_cents).where(RdPriceFloor.key.in_(keys)))
    return {k: int(v) for k, v in rows.all()}


async def assert_price_not_below_floor(db: AsyncSession, floor_key: str, price_cents: int) -> None:
    """Rejeita preço abaixo do mínimo RD. Sem piso cadastrado para a chave → aceita
    (o piso é opcional por produto); valor inválido → TypeError (centavos int)."""
    from app.core.money import ensure_cents, format_brl

    ensure_cents(price_cents, name="price_cents")
    floor = await get_floor(db, floor_key)
    if floor is None:
        return
    if price_cents < floor.min_price_cents:
        raise DomainError(
            "PRICE_BELOW_RD_MINIMUM",
            f"Preço abaixo do mínimo da RD para {floor.label} ({format_brl(floor.min_price_cents)} por {floor.unit}).",
            status=400,
            details={"floor_key": floor_key, "min_price_cents": floor.min_price_cents, "price_cents": price_cents},
        )


async def set_floor(db: AsyncSession, ctx: Any, floor_key: str, *, min_price_cents: int, label: str, unit: str,
                    note: Optional[str] = None, reason: Optional[str] = None,
                    expected_version: Optional[int] = None):
    from app.core.audit import audit
    from app.core.money import ensure_cents
    from app.models.instance import RdPriceFloor, RdPriceFloorHistory

    if not FLOOR_KEY_RE.match(floor_key or ""):
        raise DomainError("FLOOR_KEY_INVALID", "Chave de piso inválida.", status=400)
    ensure_cents(min_price_cents, name="min_price_cents")
    row = (await db.execute(select(RdPriceFloor).where(RdPriceFloor.key == floor_key).with_for_update())).scalar_one_or_none()
    if expected_version is not None and expected_version != (row.version if row else 0):
        raise DomainError("FLOOR_VERSION_CONFLICT", "O piso foi alterado por outra pessoa. Recarregue.", status=409)
    old = row.min_price_cents if row else None
    actor = getattr(ctx, "user_id", None)
    if row is None:
        row = RdPriceFloor(key=floor_key, label=label, unit=unit, min_price_cents=min_price_cents, note=note,
                           version=1, updated_by=actor, updated_at=utcnow())
        db.add(row)
    else:
        row.label, row.unit, row.min_price_cents, row.note = label, unit, min_price_cents, note
        row.version += 1
        row.updated_by, row.updated_at = actor, utcnow()
    await db.flush()
    db.add(RdPriceFloorHistory(key=floor_key, old_min_price_cents=old, new_min_price_cents=min_price_cents,
                               version=row.version, changed_by=actor, reason=reason))
    await audit(db, ctx, "instance.price_floor_set", "rd_price_floor", floor_key,
                before={"min_price_cents": old}, after={"min_price_cents": min_price_cents, "version": row.version},
                reason=reason)
    return row


# ─── Chave de assinatura da instância (ES256) ────────────────────────────────

_SIGNING_PURPOSE = "instance-signing-key"


async def get_active_signing_key(db: AsyncSession):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.core.crypto import encrypt_str
    from app.models.instance import InstanceSigningKey

    key = (await db.execute(select(InstanceSigningKey).where(InstanceSigningKey.is_active.is_(True)))).scalar_one_or_none()
    if key is not None:
        return key
    # serializa a criação (duas requisições simultâneas não geram duas chaves ativas)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('rd.instance_signing_key'))"))
    key = (await db.execute(select(InstanceSigningKey).where(InstanceSigningKey.is_active.is_(True)))).scalar_one_or_none()
    if key is not None:
        return key
    priv = ec.generate_private_key(ec.SECP256R1())
    pem_priv = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode("ascii")
    pem_pub = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    kid = "ik-" + secrets.token_hex(8)
    key = InstanceSigningKey(kid=kid, algorithm="ES256", public_pem=pem_pub,
                             private_enc=encrypt_str(pem_priv, purpose=_SIGNING_PURPOSE, aad=kid), is_active=True)
    db.add(key)
    await db.flush()
    log.info("instance_signing_key_created", kid=kid)
    return key


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


async def sign_payload(db: AsyncSession, data: bytes) -> tuple[str, str]:
    """Assina ``data`` (ES256/ECDSA P-256 + SHA-256). → (kid, assinatura base64url DER)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.core.crypto import decrypt_str

    key = await get_active_signing_key(db)
    priv = serialization.load_pem_private_key(
        decrypt_str(key.private_enc, purpose=_SIGNING_PURPOSE, aad=key.kid).encode("ascii"), password=None
    )
    sig = priv.sign(data, ec.ECDSA(hashes.SHA256()))
    return key.kid, _b64(sig)


async def verify_signature(db: AsyncSession, kid: str, data: bytes, signature_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.models.instance import InstanceSigningKey

    key = await db.get(InstanceSigningKey, kid)
    if key is None:
        return False
    pub = serialization.load_pem_public_key(key.public_pem.encode("ascii"))
    try:
        pub.verify(_unb64(signature_b64), data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


async def public_keys(db: AsyncSession) -> list[dict]:
    from app.models.instance import InstanceSigningKey

    rows = (await db.execute(select(InstanceSigningKey).order_by(InstanceSigningKey.created_at))).scalars()
    return [
        {"kid": k.kid, "alg": k.algorithm, "public_key_pem": k.public_pem, "active": k.is_active,
         "created_at": k.created_at, "retired_at": k.retired_at}
        for k in rows
    ]


async def require_profile(db: AsyncSession):
    p = await get_profile(db)
    if p is None:
        raise NotFound()
    return p
