"""Costo finale d'acquisto, riparazioni, tempi di rivendita e coefficiente di rischio."""
from __future__ import annotations

import re

from .config import IMPORT_DUTY, IMPORT_HANDLING_EUR, IMPORT_VAT
from .extract import WATCH_BRANDS
from .market import Market
from .models import Attributes, Category, CostBreakdown, Listing, RepairEstimate, SaleKind, Valuation

EU = {
    "IT", "EU", "DE", "FR", "ES", "AT", "BE", "NL", "LU", "PT", "IE", "FI", "SE", "DK", "PL", "CZ", "SK",
    "SI", "HR", "HU", "RO", "BG", "GR", "CY", "MT", "EE", "LV", "LT",
}
VAT_IT = 0.22


# --- costo finale -------------------------------------------------------------
def landed_cost(listing: Listing, attrs: Attributes, market: Market, profile: dict,
                price: float | None = None) -> CostBreakdown:
    """Tutto quello che paghi per avere l'oggetto in mano in Italia, in EUR."""
    p = listing.price if price is None else price
    item = market.to_eur(p, listing.currency) or 0.0
    premium = item * profile.get("premium", 0.0) + profile.get("fixed_fee", 0.0)
    vat_prem = premium * VAT_IT if profile.get("vat_on_premium") else 0.0
    if listing.shipping is not None:
        shipping = market.to_eur(listing.shipping, listing.currency) or 0.0
    else:
        shipping = float(profile.get("shipping", 0.0))
    duty = imp_vat = other = 0.0
    country = (listing.country or profile.get("country") or "IT").upper()
    if country not in EU:
        base = item + premium + shipping
        duty = base * IMPORT_DUTY.get(attrs.category.value, 0.03)
        if attrs.category == Category.BULLION_COIN and attrs.fine_gold_g:
            vat_rate = 0.0  # oro da investimento: esente IVA
        else:
            vat_rate = IMPORT_VAT.get(attrs.category.value, IMPORT_VAT["default"])
        imp_vat = (base + duty) * vat_rate
        other = IMPORT_HANDLING_EUR
    return CostBreakdown(
        item_eur=round(item, 2), buyer_premium=round(premium, 2), vat_on_premium=round(vat_prem, 2),
        shipping=round(shipping, 2), import_duty=round(duty, 2), import_vat=round(imp_vat, 2),
        other_fees=round(other, 2),
    )


def max_bid(listing: Listing, attrs: Attributes, market: Market, profile: dict,
            exit_net: float, repair_mid: float, margin: float) -> float | None:
    """Prezzo massimo (nella valuta dell'annuncio) che lascia il margine voluto.

    Il costo finale è lineare nel prezzo, quindi basta misurarne pendenza e intercetta.
    """
    c0 = landed_cost(listing, attrs, market, profile, price=0.0).total
    c1 = landed_cost(listing, attrs, market, profile, price=1000.0).total
    slope = (c1 - c0) / 1000.0
    if slope <= 0:
        return None
    budget = exit_net / (1 + margin) - repair_mid
    p = (budget - c0) / slope
    return round(p, 0) if p > 0 else 0.0


# --- riparazioni ----------------------------------------------------------------
WATCH_SERVICE = {1: (700, 1400), 2: (400, 850), 3: (150, 350), None: (120, 300)}
QUARTZ_RE = re.compile(r"quar[tz]|quartz", re.I)


def _watch_tier(brand: str | None) -> int | None:
    if brand and brand in WATCH_BRANDS:
        return WATCH_BRANDS[brand][1]
    return None


def estimate_repair(listing: Listing, attrs: Attributes) -> RepairEstimate:
    f = attrs.flags
    text = f"{listing.title} {listing.description}"
    lo = hi = 0.0
    items: list[str] = []
    cat = attrs.category
    if cat == Category.WATCH:
        tier = _watch_tier(attrs.brand)
        quartz = bool(QUARTZ_RE.search(text))
        if "broken" in f:
            if quartz:
                lo, hi = 60, 250
                items.append("movimento al quarzo da riparare/sostituire")
            else:
                s_lo, s_hi = WATCH_SERVICE[tier]
                lo, hi = s_lo * 1.2, s_hi * 1.8
                items.append("revisione completa + ricambi (non funzionante)")
        elif "service" in f:
            s_lo, s_hi = WATCH_SERVICE[tier]
            lo, hi = (20, 60) if quartz else (s_lo, s_hi)
            items.append("revisione" if not quartz else "pila e controllo")
        if "damaged" in f:
            lo, hi = lo + 80, hi + 300
            items.append("vetro/parti estetiche")
        if attrs.full_set is False and tier == 1:
            items.append("senza scatola e garanzia: rivendita ~10-15% più bassa (già nei comparabili se simili)")
    elif cat == Category.GOLD or cat == Category.BULLION_COIN:
        pass  # si rivende a peso: i difetti non contano
    elif cat == Category.JEWELRY:
        if "broken" in f or "damaged" in f:
            lo, hi = 40, 250
            items.append("riparazione orafo (chiusura, griffe, pietra)")
    elif cat == Category.BAG:
        if "damaged" in f or "scratches" in f:
            lo, hi = 80, 400
            items.append("pulizia/restauro pelle e minuteria")
    elif cat == Category.ART:
        if "damaged" in f:
            lo, hi = 200, 2000
            items.append("restauro: serve preventivo di un restauratore")
    elif cat in (Category.COIN, Category.CARD):
        if "damaged" in f:
            items.append("danno non riparabile: valore già ridotto")
    else:
        if "broken" in f:
            lo, hi = 30, 200
            items.append("riparazione generica")
    return RepairEstimate(low=round(lo), high=round(hi), items=items)


# --- tempi di rivendita ---------------------------------------------------------
def resale_days(attrs: Attributes, valuation: Valuation) -> tuple[int, int]:
    cat = attrs.category
    tier = _watch_tier(attrs.brand)
    if cat in (Category.GOLD, Category.BULLION_COIN):
        base = (1, 3)
    elif cat == Category.WATCH:
        base = {1: (5, 21), 2: (10, 40), 3: (15, 60)}.get(tier, (20, 90))
    elif cat == Category.BAG:
        base = (5, 21) if attrs.brand == "Hermès" else (14, 60)
    elif cat == Category.JEWELRY:
        base = (14, 60) if attrs.brand else (30, 120)
    elif cat == Category.CARD:
        base = (7, 30) if attrs.grade else (7, 45)
    elif cat == Category.COIN:
        base = (14, 60)
    elif cat == Category.ART:
        base = (60, 180)
    elif cat == Category.WINE:
        base = (30, 90)
    else:
        base = (14, 75)
    if cat not in (Category.GOLD, Category.BULLION_COIN):
        sold = sum(1 for c in valuation.comps if c.kind == "sold")
        if sold >= 25:
            base = (max(1, int(base[0] * 0.6)), max(2, int(base[1] * 0.6)))
        elif sold <= 3:
            base = (int(base[0] * 1.5), int(base[1] * 1.5))
    return base


# --- rischio --------------------------------------------------------------------
PRIVATE_SOURCES = {"subito", "vinted", "wallapop", "facebook"}
HIGH_FAKE_CATEGORIES = {Category.WATCH, Category.BAG, Category.JEWELRY, Category.CARD}


def risk_score(listing: Listing, attrs: Attributes, valuation: Valuation, profile: dict,
               discount: float) -> tuple[int, list[str]]:
    """Coefficiente di rischio 0..100 (affidabilità = 100 - rischio) con le motivazioni."""
    r = float(profile.get("base_risk", 30))
    why = [f"piattaforma {listing.source}: rischio base {int(r)}"]
    private = listing.source in PRIVATE_SOURCES

    if attrs.category in HIGH_FAKE_CATEGORIES and private:
        r += 15
        why.append("categoria spesso contraffatta venduta da privato")
    if discount >= 0.75:
        add = 35 if private else 15
        r += add
        why.append(f"sconto del {discount:.0%} sul valore: troppo bello per essere vero?")
    elif discount >= 0.55:
        add = 18 if private else 6
        r += add
        why.append(f"sconto del {discount:.0%}: verifica bene")
    if listing.seller_reviews is not None:
        if listing.seller_reviews == 0:
            r += 12
            why.append("venditore senza recensioni")
        elif listing.seller_reviews >= 50 and (listing.seller_rating or 0) >= 0.97:
            r -= 8
            why.append("venditore con molte recensioni positive")
    if listing.seller_rating is not None and listing.seller_rating < 0.9:
        r += 10
        why.append(f"feedback venditore basso ({listing.seller_rating:.0%})")
    conf = valuation.confidence
    r += (1 - conf) * 25
    if conf < 0.4:
        why.append(f"prezzo di riferimento incerto (affidabilità stima {conf:.0%})")
    if not listing.images:
        r += 8
        why.append("annuncio senza foto")
    if len(listing.description or "") < 40 and listing.source in PRIVATE_SOURCES:
        r += 4
        why.append("descrizione scarna")
    flags = attrs.flags
    if "fake_risk" in flags:
        r += 50
        why.append("parole sospette (replica/copia/tipo/stile)")
    if "plated" in flags:
        r += 25
        why.append("placcato/dorato: non è oro massiccio")
    if "broken" in flags:
        r += 10
        why.append("non funzionante: costo riparazione incerto")
    if "unverified" in flags:
        r += 10
        why.append("il venditore non garantisce l'autenticità")
    if "lot" in flags:
        r += 5
        why.append("lotto multiplo: valore per pezzo da verificare")
    if attrs.category == Category.GOLD and listing.source in PRIVATE_SOURCES:
        r += 8
        why.append("peso e titolo dichiarati dal privato: pesare e saggiare prima di pagare")
    if listing.kind == SaleKind.AUCTION and listing.estimate_low and listing.price < listing.estimate_low * 0.3:
        why.append("asta ancora lontana dalla stima: il prezzo salirà")
    if (listing.country or "IT").upper() not in EU:
        r += 5
        why.append("importazione extra UE: dazi, IVA e tempi")
    if listing.seller_type and listing.seller_type.value == "institution":
        r -= 5
    return int(max(0, min(100, round(r)))), why
