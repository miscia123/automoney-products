"""Subito.it tramite l'API JSON usata dal sito (hades.subito.it).

Verificato su scraper open source aggiornati a settembre 2026: basta l'header
`x-subito-channel: web` e un client con impronta TLS da browser (curl_cffi).
"""
from __future__ import annotations

from datetime import datetime

from ..models import Category, Listing, SaleKind, SellerType
from .base import Query, Source

API = "https://hades.subito.it/v1/search/items"
HEADERS = {
    "x-subito-channel": "web",
    "Accept": "application/json",
    "Origin": "https://www.subito.it",
    "Referer": "https://www.subito.it/",
}
# 16 = Abbigliamento e Accessori (include Orologi e Gioielli), 21 = Collezionismo
CATEGORY_IDS = {
    Category.WATCH: "16", Category.JEWELRY: "16", Category.GOLD: "16", Category.BAG: "16",
    Category.COIN: "21", Category.BULLION_COIN: "21", Category.CARD: "21", Category.COLLECTIBLE: "21",
    Category.ART: "21",
}


class Subito(Source):
    name = "subito"
    profile = "subito"

    async def search(self, query: Query) -> list[Listing]:
        params = {
            "q": query.text("it"),
            "t": "s",
            "lim": str(self.cfg.get("page_size", 50)),
            "start": "0",
            "sort": "datedesc",
        }
        if query.category in CATEGORY_IDS:
            params["c"] = CATEGORY_IDS[query.category]
        if query.min_price:
            params["ps"] = str(int(query.min_price))
        if query.max_price:
            params["pe"] = str(int(query.max_price))
        if self.cfg.get("titles_only", True):
            params["qso"] = "true"
        data = await self.http.get_json(API, params=params, headers=HEADERS)
        return [l for ad in data.get("ads", []) if (l := parse_ad(ad))]


def _feature(ad: dict, uri: str) -> dict | None:
    for f in ad.get("features") or []:
        if f.get("uri") == uri and f.get("values"):
            return f["values"][0]
    return None


def parse_ad(ad: dict) -> Listing | None:
    price_v = _feature(ad, "/price")
    if not price_v:
        return None
    try:
        price = float(str(price_v.get("key")).replace(",", "."))
    except (TypeError, ValueError):
        return None
    urn = ad.get("urn", "")
    sid = urn.rsplit(":", 1)[-1] if urn else str(ad.get("id"))
    adv = ad.get("advertiser") or {}
    geo = ad.get("geo") or {}
    city = (geo.get("city") or {}).get("value")
    region = (geo.get("region") or {}).get("value")
    images = []
    for im in ad.get("images") or []:
        base = im.get("cdn_base_url")
        if base:
            images.append(f"{base}?rule=gallery-desktop-2x-jpeg")
    shippable = _feature(ad, "/item_shippable")
    cond = _feature(ad, "/item_condition")
    desc = ad.get("body") or ""
    if cond and cond.get("value"):
        desc = f"[{cond['value']}] {desc}"
    posted = None
    if (d := (ad.get("dates") or {}).get("display_iso8601")):
        try:
            posted = datetime.fromisoformat(d.replace("Z", "+00:00"))
        except ValueError:
            pass
    return Listing(
        source="subito",
        source_id=sid,
        url=(ad.get("urls") or {}).get("default", ""),
        title=ad.get("subject", ""),
        description=desc,
        price=price,
        currency="EUR",
        kind=SaleKind.OFFER,
        country="IT",
        location=", ".join(x for x in (city, region) if x) or None,
        seller_type=SellerType.BUSINESS if adv.get("company") else SellerType.PRIVATE,
        images=images,
        shipping=None if (shippable and shippable.get("key") in ("1", "true", True)) else 0.0,
        raw={"posted": posted.isoformat() if posted else None, "shippable": bool(shippable)},
    )
