"""Stima del valore di mercato dai comparabili venduti e dal valore del metallo."""
from __future__ import annotations

import statistics

from .extract import FAKE_RE, FLAG_RE, KARAT_FINENESS, PLATED_RE, similarity
from .market import Market
from .models import Attributes, Category, Comparable, Listing, Valuation

ASK_DISCOUNT = 0.88  # i prezzi richiesti sono più alti dei venduti: li riportiamo a "venduto"
BULLION_PREMIUM = 0.03  # premio tipico di sterline/marenghi sul metallo


def _weighted_median(values: list[tuple[float, float]]) -> float:
    values = sorted(values)
    total = sum(w for _, w in values)
    acc = 0.0
    for v, w in values:
        acc += w
        if acc >= total / 2:
            return v
    return values[-1][0]


def _quartiles(xs: list[float]) -> tuple[float, float]:
    if len(xs) < 4:
        return min(xs), max(xs)
    q = statistics.quantiles(xs, n=4)
    return q[0], q[2]


def filter_comps(listing: Listing, attrs: Attributes, comps: list[Comparable],
                 min_sim: float) -> list[Comparable]:
    """Tiene solo i comparabili davvero simili e nelle stesse condizioni."""
    ref = attrs.query_text or listing.title
    listing_broken = "broken" in attrs.flags
    kept = []
    for c in comps:
        if c.price_eur <= 0:
            continue
        if FAKE_RE.search(c.title) or PLATED_RE.search(c.title):
            continue
        c_broken = bool(FLAG_RE["broken"].search(c.title))
        if c_broken != listing_broken:
            continue
        if c.kind != "index":
            sim = max(similarity(ref, c.title), similarity(listing.title, c.title))
            if attrs.reference and attrs.reference.lower() in c.title.lower():
                sim = max(sim, 0.9)
            if sim < min_sim:
                continue
            c.similarity = sim
        kept.append(c)
    if len(kept) >= 5:  # via gli outlier (IQR)
        prices = [c.price_eur for c in kept]
        q1, q3 = _quartiles(prices)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        kept = [c for c in kept if lo <= c.price_eur <= hi]
    return kept


def melt_value(attrs: Attributes, market: Market) -> float | None:
    if attrs.category == Category.BULLION_COIN:
        return market.melt_value(attrs.fine_gold_g, attrs.fine_silver_g)
    if attrs.karat and attrs.grams:
        fine = attrs.grams * KARAT_FINENESS[attrs.karat]
        if attrs.category == Category.WATCH:
            fine *= 0.5  # cassa+bracciale: in media metà del peso totale è oro
        return market.melt_value(fine_gold_g=fine)
    if attrs.silver_fineness and attrs.grams:
        return market.melt_value(fine_silver_g=attrs.grams * attrs.silver_fineness / 1000)
    return None


def value(listing: Listing, attrs: Attributes, comps: list[Comparable], market: Market,
          min_sim: float = 0.3) -> Valuation:
    comps = filter_comps(listing, attrs, comps, min_sim)
    melt = melt_value(attrs, market)
    notes: list[str] = []

    pts: list[tuple[float, float]] = []
    for c in comps:
        if c.kind == "sold":
            pts.append((c.price_eur, 1.0 * c.similarity))
        elif c.kind == "index":
            pts.append((c.price_eur, 3.0))  # indici di prezzo (es. Cardmarket trend) pesano di più
        else:
            pts.append((c.price_eur * ASK_DISCOUNT, 0.5 * c.similarity))

    fair = low = high = None
    conf = 0.0
    method = "nessun comparabile"
    if pts:
        fair = _weighted_median(pts)
        prices = [p for p, _ in pts]
        low, high = _quartiles(prices)
        n_eff = sum(w for _, w in pts)
        disp = (high - low) / fair if fair else 1.0
        conf = min(1.0, n_eff / 10) * (0.4 + 0.6 * max(0.0, 1 - disp))
        n_sold = sum(1 for c in comps if c.kind == "sold")
        method = f"mediana di {len(comps)} comparabili ({n_sold} venduti)"

    if melt:
        if attrs.category == Category.GOLD:
            fair_melt = melt
            if fair is None or fair < fair_melt or attrs.brand is None:
                fair, low, high = fair_melt, melt * 0.95, max(melt, high or 0)
                conf = max(conf, 0.85 if attrs.grams and attrs.karat else 0.5)
                method = "valore dell'oro fino (peso x titolo x spot)"
        elif attrs.category == Category.BULLION_COIN:
            fair_melt = melt * (1 + BULLION_PREMIUM)
            if fair is None or abs(fair - fair_melt) / fair_melt > 0.5:
                fair, low, high = fair_melt, melt, melt * 1.08
                method = "valore del metallo fino + premio di mercato"
            conf = max(conf, 0.9)
        else:
            if fair is None or fair < melt:
                notes.append("il valore del metallo supera i comparabili: uso il metallo come minimo")
                fair = melt
                low = min(low or melt, melt)
                high = max(high or melt, melt)
                conf = max(conf, 0.6)

    return Valuation(
        fair_value=round(fair, 2) if fair else None,
        low=round(low, 2) if low else None,
        high=round(high, 2) if high else None,
        confidence=round(conf, 3),
        method=method,
        comps=sorted(comps, key=lambda c: -c.similarity),
        melt_value=melt,
        notes=notes,
    )
