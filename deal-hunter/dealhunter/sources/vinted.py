"""Vinted.it tramite la nuova API di catalogo (api.vinted.it/svc-catalogue, da settembre 2026).

La vecchia /api/v2/catalog/items è stata ritirata. Serve prima una visita alla home
per ottenere i cookie di sessione (access_token_web, anon_id); il token dura ~24 ore.
Vinted usa DataDome: dagli IP di datacenter spesso blocca, da casa di solito no.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from ..http import BlockedError, FetchError
from ..models import Listing, SaleKind, SellerType
from .base import Query, Source

HOME = "https://www.vinted.it/"
API = "https://api.vinted.it/svc-catalogue/items"


class Vinted(Source):
    name = "vinted"
    profile = "vinted"
    paged = True
    page_size = 48

    _token: str | None = None
    _anon: str | None = None
    _token_at: float = 0.0

    async def _bootstrap(self) -> None:
        resp = await self.http.request("GET", HOME, headers={"Accept": "text/html"})
        jar = self.http.cookies()
        self._token = jar.get("access_token_web")
        self._anon = jar.get("anon_id") or resp.headers.get("x-anon-id")
        self._token_at = time.time()
        if not self._token:
            raise BlockedError(HOME, resp.status_code, "nessun access_token_web: probabile blocco DataDome")

    def _headers(self) -> dict:
        h = {
            "Accept": "application/json, text/plain, */*",
            "Locale": "it-IT",
            "Platform": "web",
            "X-Next-App": "marketplace-web",
            "Origin": "https://www.vinted.it",
            "Referer": "https://www.vinted.it/",
            "Sec-Fetch-Site": "same-site",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if self._anon:
            h["X-Anon-Id"] = self._anon
        return h

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        if not self._token or time.time() - self._token_at > 20 * 3600:
            await self._bootstrap()
        params = {
            "search_text": query.text("it"),
            "order": "newest_first",
            "per_page": str(self.cfg.get("page_size", 48)),
            "page": str(page),
            "currency": "EUR",
        }
        if query.min_price:
            params["price_from"] = str(int(query.min_price))
        if query.max_price:
            params["price_to"] = str(int(query.max_price))
        for k, v in (query.extra.get("vinted") or {}).items():
            params[k] = v  # es. "attribute_ids[catalog]": "..."
        try:
            data = await self.http.get_json(API, params=params, headers=self._headers(), retries=1)
        except (BlockedError, FetchError) as e:
            if getattr(e, "status", None) in (401, 403):
                await self._bootstrap()  # token scaduto: un solo nuovo tentativo
                data = await self.http.get_json(API, params=params, headers=self._headers(), retries=1)
            else:
                raise
        return [l for it in data.get("items", []) if (l := parse_item(it))]


def _amount(v) -> float | None:
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_item(it: dict) -> Listing | None:
    price = _amount(it.get("price"))
    if price is None:
        return None
    url = it.get("url") or it.get("path") or ""
    if url.startswith("/"):
        url = "https://www.vinted.it" + url
    photo = it.get("photo") or {}
    user = it.get("user") or {}
    rep = user.get("feedback_reputation")
    posted = None
    ts = ((photo.get("high_resolution") or {}).get("timestamp"))
    if ts:
        posted = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    box = it.get("item_box") or {}
    desc = " · ".join(x for x in (it.get("brand_title"), it.get("status"), box.get("second_line")) if x)
    return Listing(
        source="vinted",
        source_id=str(it.get("id")),
        url=url,
        title=it.get("title", ""),
        description=desc,
        price=price,
        currency=(it.get("price") or {}).get("currency_code", "EUR") if isinstance(it.get("price"), dict) else "EUR",
        kind=SaleKind.OFFER,
        country="EU",
        seller_type=SellerType.BUSINESS if user.get("business") else SellerType.PRIVATE,
        seller_rating=float(rep) if rep is not None else None,
        images=[u for u in (photo.get("full_size_url") or photo.get("url"),) if u],
        raw={"posted": posted, "favourites": it.get("favourite_count"), "user": user.get("login")},
    )
