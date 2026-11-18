"""Typed results returned by the client, the REST API and the MCP tools."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Sort(StrEnum):
    newest = "newest"
    relevance = "relevance"
    price_asc = "price_asc"
    price_desc = "price_desc"


class SellerType(StrEnum):
    private = "private"
    commercial = "commercial"


class AdType(StrEnum):
    offer = "offer"
    wanted = "wanted"


class Price(BaseModel):
    amount_eur: float | None = Field(None, description="Numeric price, None if not given (e.g. only 'VB').")
    negotiable: bool = Field(False, description="'VB' – Verhandlungsbasis")
    free: bool = Field(False, description="'Zu verschenken'")
    old_amount_eur: float | None = Field(None, description="Struck-through previous price, if reduced.")
    text: str = ""


class AdSummary(BaseModel):
    id: str
    url: str
    category_id: str | None = Field(None, description="Leaf category of the ad (from its URL).")
    title: str
    description: str = Field("", description="Shortened teaser text from the result list.")
    price: Price
    location: str = ""
    zip_code: str | None = None
    distance_km: float | None = None
    posted_at: datetime | None = Field(None, description="Europe/Berlin; None for top ads without date.")
    posted_text: str = ""
    image_url: str | None = None
    image_count: int = 0
    tags: list[str] = Field(default_factory=list, description="e.g. 'Direkt kaufen', '50.785 km', 'EZ 11/2021'")
    shipping: bool = False
    buy_now: bool = Field(False, description="'Direkt kaufen' (Kleinanzeigen payment) available")
    is_promoted: bool = Field(False, description="Paid TOP/priority placement – not necessarily new.")
    ad_type: AdType = AdType.offer
    seller_id: str | None = None
    seller_type: SellerType | None = None
    shop_name: str | None = None
    shop_url: str | None = None


class SearchResult(BaseModel):
    url: str
    location_name: str | None = Field(None, description="How the location filter was resolved.")
    location_alternatives: list[str] = Field(default_factory=list)
    complete: bool | None = Field(None, description="With a time window: all ads in the window were read.")
    total: int | None = Field(None, description="Total hits reported by Kleinanzeigen.")
    page: int
    has_next_page: bool
    ads: list[AdSummary]
    notes: list[str] = Field(default_factory=list)
    cached: bool = False
    fetched_at: datetime


class SellerSummary(BaseModel):
    user_id: str | None = None
    name: str | None = None
    seller_type: SellerType | None = None
    active_since: str | None = None
    rating: str | None = Field(None, description="Satisfaction badge, e.g. TOP / OK / NA JA")
    badges: list[str] = Field(default_factory=list)
    response_time: str | None = None
    ads_online: int | None = None
    shop_url: str | None = None
    profile_url: str | None = None


class AdDetail(BaseModel):
    id: str
    url: str
    title: str
    description: str
    price: Price
    location: str = ""
    zip_code: str | None = None
    region: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    posted_at: datetime | None = None
    categories: list[str] = Field(default_factory=list)
    category_id: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    features: list[str] = Field(default_factory=list)
    shipping: str | None = None
    images: list[str] = Field(default_factory=list)
    views: int | None = None
    seller: SellerSummary = Field(default_factory=SellerSummary)
    cached: bool = False
    fetched_at: datetime


class Seller(BaseModel):
    profile: SellerSummary
    ads: list[AdSummary]
    cached: bool = False
    fetched_at: datetime


class MarketStats(BaseModel):
    basis: str = Field(description="Which search the statistics are based on.")
    category_id: str | None = Field(None, description="Category the comparison was restricted to.")
    category_breakdown: list[dict] = Field(
        default_factory=list,
        description="Matches per category (count, median, title_position). If the auto-picked category "
                    "is wrong, call again with `category` set.",
    )
    sample_size: int
    low_confidence: bool = Field(False, description="True if fewer than 15 comparable offers.")
    outliers_removed: int = 0
    median_eur: float | None = None
    mean_eur: float | None = None
    p25_eur: float | None = None
    p75_eur: float | None = None
    min_eur: float | None = None
    max_eur: float | None = None


class RiskAssessment(BaseModel):
    score: int = Field(description="0 (unauffällig) – 100 (sehr verdächtig). Heuristic, not a verdict.")
    level: str = Field(description="low | medium | high")
    price_vs_median: float | None = Field(None, description="price / market median, e.g. 0.8 = 20 % cheaper")
    account_age_days: int | None = None
    reasons: list[str] = Field(default_factory=list, description="Risk signals with their points.")
    positives: list[str] = Field(default_factory=list, description="Trust signals with their points.")
    hints: list[str] = Field(default_factory=list, description="Deal-quality notes, not scam signals.")


class Deal(BaseModel):
    ad: AdSummary
    price_vs_median: float | None = None
    savings_eur: float | None = Field(None, description="Market median minus price.")
    risk: RiskAssessment | None = Field(None, description="Only for the top candidates (check_top).")
    condition: str | None = Field(None, description="'Zustand' from the ad details, if checked.")
    seller: SellerSummary | None = None


class DealsResult(BaseModel):
    query: str
    market: MarketStats
    deals: list[Deal]
    checked: int = Field(description="How many deals got a full risk check.")
    excluded: dict[str, int] = Field(default_factory=dict)
    best_low_risk_id: str | None = Field(None, description="Cheapest checked deal with risk level 'low'.")
    disclaimer: str = (
        "Heuristische Einschätzung aus öffentlichen Signalen (Preis, Kontoalter, Bewertungen, Text). "
        "Kein Beweis – im Zweifel nur Abholung mit Prüfung oder 'Sicher bezahlen' nutzen."
    )
    fetched_at: datetime


class Location(BaseModel):
    id: str
    name: str


class Category(BaseModel):
    id: str
    name: str
    slug: str
    parent_id: str | None = None
    count: int | None = None
