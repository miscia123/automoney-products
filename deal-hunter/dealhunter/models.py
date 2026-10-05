"""Strutture dati condivise da sorgenti, valutazione e alert."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Category(str, Enum):
    WATCH = "watch"
    GOLD = "gold"  # gioielli/oggetti d'oro venduti a peso
    JEWELRY = "jewelry"  # gioielli firmati o con pietre
    BULLION_COIN = "bullion_coin"  # sterline, marenghi, krugerrand, lingotti
    COIN = "coin"  # numismatica
    CARD = "card"
    BAG = "bag"
    ART = "art"
    WINE = "wine"
    COLLECTIBLE = "collectible"
    OTHER = "other"


class SaleKind(str, Enum):
    AUCTION = "auction"
    BUY_NOW = "buy_now"
    OFFER = "offer"  # prezzo trattabile / offerta libera


class SellerType(str, Enum):
    PRIVATE = "private"
    BUSINESS = "business"
    INSTITUTION = "institution"  # tribunale, dogana, banca su pegno


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    price: float  # prezzo richiesto o offerta corrente, nella valuta indicata
    currency: str = "EUR"
    kind: SaleKind = SaleKind.BUY_NOW
    description: str = ""
    ends_at: datetime | None = None
    bids: int | None = None
    shipping: float | None = None  # in valuta della listing
    country: str = "IT"
    location: str | None = None
    seller_type: SellerType | None = None
    seller_rating: float | None = None  # 0..1
    seller_reviews: int | None = None
    images: list[str] = field(default_factory=list)
    category_hint: Category | None = None
    estimate_low: float | None = None  # stima della casa d'asta
    estimate_high: float | None = None
    reserve_met: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"


@dataclass
class Attributes:
    category: Category = Category.OTHER
    brand: str | None = None
    model: str | None = None
    reference: str | None = None
    karat: int | None = None  # 9, 14, 18, 22, 24
    silver_fineness: int | None = None  # 800, 835, 925, 999
    grams: float | None = None
    fine_gold_g: float | None = None  # oro fino (monete da investimento)
    fine_silver_g: float | None = None
    bullion_name: str | None = None
    grade: str | None = None  # PSA 10, BGS 9.5, ...
    full_set: bool | None = None
    flags: set[str] = field(default_factory=set)  # broken, service, fake_risk, no_box, ...
    query_text: str = ""  # testo pulito per cercare i comparabili


@dataclass
class Comparable:
    price_eur: float
    title: str
    source: str  # ebay_it_sold, catawiki_sold, cardmarket, metals, ...
    kind: str = "sold"  # sold | ask | index
    url: str | None = None
    sold_at: datetime | None = None
    similarity: float = 1.0


@dataclass
class Valuation:
    fair_value: float | None  # valore di mercato stimato (prezzo venduto tipico) in EUR
    low: float | None
    high: float | None
    confidence: float  # 0..1
    method: str
    comps: list[Comparable] = field(default_factory=list)
    melt_value: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class CostBreakdown:
    item_eur: float
    buyer_premium: float
    vat_on_premium: float
    shipping: float
    import_duty: float
    import_vat: float
    other_fees: float

    @property
    def total(self) -> float:
        return round(
            self.item_eur
            + self.buyer_premium
            + self.vat_on_premium
            + self.shipping
            + self.import_duty
            + self.import_vat
            + self.other_fees,
            2,
        )


@dataclass
class RepairEstimate:
    low: float
    high: float
    items: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2


@dataclass
class Deal:
    listing: Listing
    attrs: Attributes
    valuation: Valuation
    cost: CostBreakdown
    repair: RepairEstimate
    exit_channel: str
    exit_net: float  # incasso netto alla rivendita, dopo commissioni
    profit: float
    roi: float
    risk: int  # 0..100, più alto = più rischioso
    risk_reasons: list[str]
    resale_days: tuple[int, int]
    max_bid: float | None  # offerta massima consigliata (in valuta della listing)
    score: float
    level: str  # hot | good | watch | none
    llm_notes: dict[str, Any] | None = None

    @property
    def reliability(self) -> int:
        return 100 - self.risk
