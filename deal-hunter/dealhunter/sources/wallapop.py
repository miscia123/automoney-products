"""Wallapop (it.wallapop.com) tramite l'API di ricerca pubblica, senza login."""
from __future__ import annotations

import uuid

from ..models import Listing, SaleKind
from .base import Query, Source

API = "https://api.wallapop.com/api/v3/search"


class Wallapop(Source):
    name = "wallapop"
    profile = "wallapop"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.device_id = str(uuid.uuid4())

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        lat, lon = self.cfg.get("latitude", 41.9028), self.cfg.get("longitude", 12.4964)
        params = {
            "keywords": query.text("it"),
            "source": "search_box",
            "search_id": str(uuid.uuid4()),
            "latitude": str(lat),
            "longitude": str(lon),
            "order_by": "newest",
            "section_type": "organic_search_results",
        }
        if self.cfg.get("distance_km"):
            params["distance_in_km"] = str(self.cfg["distance_km"])
        if query.min_price:
            params["min_sale_price"] = str(int(query.min_price))
        if query.max_price:
            params["max_sale_price"] = str(int(query.max_price))
        headers = {
            "deviceos": "0", "x-deviceos": "0", "x-appversion": "818810", "x-deviceid": self.device_id,
            "Accept-Language": "it-IT", "Referer": "https://it.wallapop.com/", "Origin": "https://it.wallapop.com",
        }
        data = await self.http.get_json(API, params=params, headers=headers)
        section = (data.get("data") or {}).get("section") or {}
        items = section.get("items") or (section.get("payload") or {}).get("items") or []
        return [l for it in items if (l := parse_item(it))]


def parse_item(it: dict) -> Listing | None:
    price = (it.get("price") or {}).get("amount")
    if price is None:
        return None
    loc = it.get("location") or {}
    imgs = [((im.get("urls") or {}).get("big")) for im in it.get("images") or []]
    return Listing(
        source="wallapop", source_id=str(it.get("id")),
        url=f"https://it.wallapop.com/item/{it.get('web_slug')}",
        title=it.get("title", ""), description=it.get("description", ""),
        price=float(price), currency=(it.get("price") or {}).get("currency", "EUR"),
        kind=SaleKind.OFFER, country=(loc.get("country_code") or "IT").upper(),
        location=loc.get("city"), images=[u for u in imgs if u],
        raw={"shippable": (it.get("shipping") or {}).get("user_allows_shipping")},
    )
