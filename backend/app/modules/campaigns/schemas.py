"""Schemas Pydantic (contrato HTTP) de campanhas/criativos/checkout — ``extra="forbid"``:
campos desconhecidos (ex.: ``amount_cents``/``price``) são REJEITADOS (422), nunca aceitos como preço."""
from __future__ import annotations

import uuid
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetIn(_Strict):
    type: Literal["COUNTRY", "REGION", "STATE", "CITY", "NEIGHBORHOOD", "POSTAL_CODE", "CATEGORY", "ESTABLISHMENT"]
    value: str = Field(min_length=1, max_length=255)
    exclude: bool = False


class CampaignFields(_Strict):
    name: Optional[str] = Field(default=None, min_length=3, max_length=255)
    description: Optional[str] = Field(default=None, max_length=5000)
    period_type: Optional[Literal["AVULSO", "MENSAL", "SEMESTRAL", "ANUAL"]] = None
    period_days: Optional[int] = Field(default=None, ge=1, le=366)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    duration_s: Optional[int] = None
    freq_kind: Optional[Literal["PER_HOUR", "PER_DAY", "SPECIFIC_TIME"]] = None
    freq_value: Optional[int] = Field(default=None, ge=1, le=1440)
    specific_times: Optional[list[str]] = Field(default=None, max_length=48)
    min_interval_s: Optional[int] = Field(default=None, ge=0, le=86400)
    days_mask: Optional[int] = Field(default=None, ge=1, le=127)
    time_bands: Optional[list[list[int]]] = Field(default=None, max_length=24)
    priority: Optional[Literal["STANDARD", "PREMIUM"]] = None
    exclusivity: Optional[Literal["NONE", "COMPETITOR_EXCLUSIVE"]] = None
    allow_same_advertiser_consecutive: Optional[bool] = None
    request_art_service: Optional[bool] = None
    auto_renew: Optional[bool] = None
    plan_id: Optional[uuid.UUID] = None
    product_id: Optional[uuid.UUID] = None
    brand_id: Optional[uuid.UUID] = None
    sales_mode: Optional[Literal["SELF_SERVICE", "COMMERCIAL"]] = None
    seller_user_id: Optional[uuid.UUID] = None


class CampaignCreate(CampaignFields):
    name: str = Field(min_length=3, max_length=255)
    org_id: Optional[uuid.UUID] = None
    targets: Optional[list[TargetIn]] = Field(default=None, max_length=500)
    segment_ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=50)


class CampaignPatch(CampaignFields):
    expected_version: Optional[int] = Field(default=None, ge=1)


class TargetsPut(_Strict):
    targets: list[TargetIn] = Field(max_length=500)


class SegmentsPut(_Strict):
    segment_ids: list[uuid.UUID] = Field(max_length=50)


class CreativeLink(_Strict):
    creative_id: uuid.UUID
    order: int = Field(default=0, ge=0, le=1000)


class Reason(_Strict):
    reason: str = Field(min_length=1, max_length=2000)


class OptionalReason(_Strict):
    reason: Optional[str] = Field(default=None, max_length=2000)


class RejectIn(_Strict):
    reason: str = Field(min_length=1, max_length=2000)
    creative_ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=50)


class ApproveIn(_Strict):
    note: Optional[str] = Field(default=None, max_length=2000)


class BulkReviewIn(_Strict):
    campaign_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    action: Literal["approve", "reject"]
    reason: Optional[str] = Field(default=None, max_length=2000)


class ApprovalPolicyIn(_Strict):
    mode: Literal["MANUAL", "AUTO"]
    reason: str = Field(min_length=10, max_length=2000)


class AvailabilityIn(_Strict):
    org_id: Optional[uuid.UUID] = None
    targets: list[TargetIn] = Field(min_length=1, max_length=500)
    duration_s: int = 30
    # campanha existente (rascunho ou alteração): aplica as MESMAS exclusões do checkout/aprovação
    campaign_id: Optional[uuid.UUID] = None


class QuotePreviewIn(_Strict):
    org_id: Optional[uuid.UUID] = None
    # campanha existente (rascunho ou estimativa de alteração): mesmas exclusões do checkout/aprovação
    campaign_id: Optional[uuid.UUID] = None
    targets: list[TargetIn] = Field(min_length=1, max_length=500)
    duration_s: int = 30
    freq_kind: Literal["PER_HOUR", "PER_DAY", "SPECIFIC_TIME"] = "PER_HOUR"
    freq_value: int = Field(default=1, ge=1, le=1440)
    specific_times: Optional[list[str]] = Field(default=None, max_length=48)
    days_mask: int = Field(default=127, ge=1, le=127)
    time_bands: list[list[int]] = Field(default_factory=lambda: [[0, 1440]], max_length=24)
    start_date: date
    period_type: Literal["AVULSO", "MENSAL", "SEMESTRAL", "ANUAL"] = "AVULSO"
    period_days: Optional[int] = Field(default=None, ge=1, le=366)
    end_date: Optional[date] = None
    priority: Literal["STANDARD", "PREMIUM"] = "STANDARD"
    exclusivity: Literal["NONE", "COMPETITOR_EXCLUSIVE"] = "NONE"
    request_art_service: bool = False
    plan_id: Optional[uuid.UUID] = None
    coupon_code: Optional[str] = Field(default=None, max_length=40)


class CreativeReplacement(_Strict):
    old_creative_id: uuid.UUID
    new_creative_id: uuid.UUID


class ChangeIn(_Strict):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    period_type: Optional[Literal["AVULSO", "MENSAL", "SEMESTRAL", "ANUAL"]] = None
    duration_s: Optional[int] = None
    freq_kind: Optional[Literal["PER_HOUR", "PER_DAY", "SPECIFIC_TIME"]] = None
    freq_value: Optional[int] = Field(default=None, ge=1, le=1440)
    specific_times: Optional[list[str]] = Field(default=None, max_length=48)
    min_interval_s: Optional[int] = Field(default=None, ge=0, le=86400)
    days_mask: Optional[int] = Field(default=None, ge=1, le=127)
    time_bands: Optional[list[list[int]]] = Field(default=None, max_length=24)
    priority: Optional[Literal["STANDARD", "PREMIUM"]] = None
    exclusivity: Optional[Literal["NONE", "COMPETITOR_EXCLUSIVE"]] = None
    allow_same_advertiser_consecutive: Optional[bool] = None
    targets: Optional[list[TargetIn]] = Field(default=None, max_length=500)
    creative_replacements: Optional[list[CreativeReplacement]] = Field(default=None, max_length=50)
    add_creative_ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=50)
    remove_creative_ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=50)


class _PaymentMethod(_Strict):
    """Forma de pagamento repassada ao W-FIN. ``payment_source_token`` (cartão) vem do SDK da
    CloudPayPag no navegador — nunca é persistido, logado ou ecoado."""
    coupon_code: Optional[str] = Field(default=None, max_length=40)
    method: Literal["pix", "credit_card"] = "pix"
    payment_source_token: Optional[str] = Field(default=None, min_length=8, max_length=4096, repr=False)
    installments: int = Field(default=1, ge=1, le=12)


class CheckoutIn(_PaymentMethod):
    campaign_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class SingleCheckoutIn(_PaymentMethod):
    pass


class CreativeCreate(_Strict):
    org_id: Optional[uuid.UUID] = None
    name: str = Field(min_length=2, max_length=255)
    media_file_id: uuid.UUID
    duration_seconds: int
    kind: Literal["EXTERNAL", "INTERNAL", "INSTITUTIONAL", "FALLBACK"] = "EXTERNAL"
    ad_text: Optional[str] = Field(default=None, max_length=2000)
    product_id: Optional[uuid.UUID] = None
    brand_id: Optional[uuid.UUID] = None
    segment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class CreativePatch(_Strict):
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    ad_text: Optional[str] = Field(default=None, max_length=2000)
    product_id: Optional[uuid.UUID] = None
    brand_id: Optional[uuid.UUID] = None


class CreativeVersionIn(_Strict):
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    media_file_id: Optional[uuid.UUID] = None
    duration_seconds: Optional[int] = None
    ad_text: Optional[str] = Field(default=None, max_length=2000)
    product_id: Optional[uuid.UUID] = None
    brand_id: Optional[uuid.UUID] = None
    segment_ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=50)


class RenewIn(_Strict):
    periods: int = Field(default=1, ge=1, le=12)


class AutoRenewIn(_Strict):
    auto_renew: bool


class CreativeReviewIn(_Strict):
    decision: Literal["APPROVE", "REJECT"]
    reason: Optional[str] = Field(default=None, max_length=2000)
