"""eBay: annunci attivi (Browse API ufficiale se hai le chiavi, altrimenti pagina di ricerca)
e prezzi VENDUTI dalla pagina di ricerca con LH_Sold=1 (l'unica fonte di venduti accessibile:
la Finding API è stata spenta a febbraio 2025, Marketplace Insights è riservata)."""
from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timezone

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..http import BlockedError
from ..models import Comparable, Listing, SaleKind, SellerType
from .base import Query, Source, parse_price

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

IT_MONTHS = {"gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6, "lug": 7, "ago": 8,
             "set": 9, "ott": 10, "nov": 11, "dic": 12,
             "jan": 1, "mär": 3, "mai": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "dez": 12,
             "may": 5, "oct": 10, "dec": 12}


class Ebay(Source):
    name = "ebay"
    profile = "ebay"

    _token: str | None = None
    _token_exp: float = 0

    async def search(self, query: Query) -> list[Listing]:
        if self.secrets.get("ebay_client_id") and self.secrets.get("ebay_client_secret"):
            out = []
            for sort in ("newlyListed", "endingSoonest"):
                out += await self._browse(query, sort)
            return out
        return await self._html_active(query)

    # --- API ufficiale -----------------------------------------------------------
    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        cred = f"{self.secrets['ebay_client_id']}:{self.secrets['ebay_client_secret']}"
        resp = await self.http.request(
            "POST", TOKEN_URL,
            headers={"Authorization": "Basic " + base64.b64encode(cred.encode()).decode(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
        )
        d = resp.json()
        self._token = d["access_token"]
        self._token_exp = time.time() + int(d.get("expires_in", 7200))
        return self._token

    async def _browse(self, query: Query, sort: str) -> list[Listing]:
        token = await self._ensure_token()
        filters = ["priceCurrency:EUR", "itemLocationCountry:IT" if self.cfg.get("italy_only") else None]
        if query.min_price or query.max_price:
            filters.append(f"price:[{int(query.min_price or 0)}..{int(query.max_price) if query.max_price else ''}]")
        if sort == "endingSoonest":
            filters.append("buyingOptions:{AUCTION}")
        params = {"q": query.text("it"), "sort": sort, "limit": "50",
                  "filter": ",".join(f for f in filters if f)}
        data = await self.http.get_json(
            BROWSE_URL, params=params,
            headers={"Authorization": f"Bearer {token}",
                     "X-EBAY-C-MARKETPLACE-ID": self.cfg.get("marketplace", "EBAY_IT")},
        )
        return [l for it in data.get("itemSummaries", []) if (l := _parse_browse(it))]

    # --- pagina di ricerca (annunci attivi) ------------------------------------------
    async def _html_active(self, query: Query) -> list[Listing]:
        params = {"_nkw": query.text("it"), "_sop": "10", "_ipg": "120"}
        if query.min_price:
            params["_udlo"] = str(int(query.min_price))
        if query.max_price:
            params["_udhi"] = str(int(query.max_price))
        html = await self.http.get_text("https://www.ebay.it/sch/i.html", params=params)
        _check_challenge(html)
        out = []
        for card in iter_cards(html):
            out.append(Listing(
                source="ebay", source_id=card["id"], url=card["url"], title=card["title"],
                price=card["price"], currency=card["currency"],
                kind=SaleKind.AUCTION if card.get("bids") is not None else SaleKind.BUY_NOW,
                bids=card.get("bids"), shipping=card.get("shipping"), country="EU",
                images=[card["image"]] if card.get("image") else [],
            ))
        return out


def _parse_browse(it: dict) -> Listing | None:
    price = it.get("currentBidPrice") or it.get("price") or {}
    if not price.get("value"):
        return None
    seller = it.get("seller") or {}
    ship = None
    for so in it.get("shippingOptions") or []:
        cost = (so.get("shippingCost") or {}).get("value")
        if cost is not None:
            ship = float(cost)
            break
    ends = it.get("itemEndDate")
    auction = "AUCTION" in (it.get("buyingOptions") or [])
    fb = seller.get("feedbackPercentage")
    loc = it.get("itemLocation") or {}
    return Listing(
        source="ebay", source_id=str(it.get("itemId")), url=it.get("itemWebUrl", ""),
        title=it.get("title", ""), description=it.get("condition") or "",
        price=float(price["value"]), currency=price.get("currency", "EUR"),
        kind=SaleKind.AUCTION if auction else SaleKind.BUY_NOW,
        bids=it.get("bidCount"), shipping=ship,
        ends_at=datetime.fromisoformat(ends.replace("Z", "+00:00")) if ends else None,
        country=loc.get("country") or "EU",
        seller_type=SellerType.BUSINESS if (it.get("sellerAccountType") == "BUSINESS") else None,
        seller_rating=float(fb) / 100 if fb else None,
        seller_reviews=seller.get("feedbackScore"),
        images=[(it.get("image") or {}).get("imageUrl")] if it.get("image") else [],
    )


def _check_challenge(html: str) -> None:
    if "/splashui/challenge" in html or "/splashui/captcha" in html or "Pardon Our Interruption" in html:
        raise BlockedError("ebay", 200, "challenge Akamai")


def iter_cards(html: str):
    """Itera le schede risultato, sia layout vecchio (s-item) sia nuovo (s-card)."""
    tree = HTMLParser(html)
    nodes = tree.css("ul.srp-results > li") or tree.css("li.s-item, li.s-card")
    for node in nodes:
        cls = node.attributes.get("class") or ""
        if "srp-river-answer" in cls or "srp-river-results-null" in cls:
            # "Risultati che corrispondono a meno parole": da qui in poi è rumore
            if node.css_first("h3, .section-notice__main") or "corrispond" in node.text().lower():
                break
            continue
        if "s-item" not in cls and "s-card" not in cls:
            continue
        link = node.css_first("a.s-card__link, a.s-item__link, a[href*='/itm/']")
        if not link:
            continue
        href = link.attributes.get("href") or ""
        m = re.search(r"/itm/(?:[^/]+/)?(\d{9,})", href)
        if not m:
            continue
        tnode = node.css_first(".s-card__title, .s-item__title")
        title = tnode.text(strip=True) if tnode else ""
        if not title:
            img = node.css_first("img")
            title = (img.attributes.get("alt") if img else "") or ""
        title = re.sub(r"^(Nuova inserzione|Nuovo annuncio|New listing)\s*", "", title, flags=re.I)
        title = re.sub(r"\s*Si apre in una nuova finestra.*$", "", title)
        if not title or title.lower().startswith("shop on ebay"):
            continue
        pnode = node.css_first(".s-card__price, .s-item__price")
        ptext = pnode.text(strip=True) if pnode else ""
        if re.search(r"\ba\b|\bto\b|\bbis\b", ptext):  # intervalli di prezzo: scarta
            continue
        price = parse_price(ptext)
        if price is None:
            continue
        currency = "GBP" if "£" in ptext else "USD" if "$" in ptext else "EUR"
        text = node.text(separator=" ", strip=True)
        ship = None
        sm = re.search(r"\+?\s*(?:EUR\s*)?([\d.,]+)\s*(?:EUR)?\s*(?:di\s+)?spedizione", text, re.I)
        if re.search(r"spedizione gratuita|free shipping|kostenloser versand", text, re.I):
            ship = 0.0
        elif sm:
            ship = parse_price(sm.group(1))
        bids = None
        bm = re.search(r"(\d+)\s+(?:offert[ae]|bids?|gebote?)\b", text, re.I)
        if bm:
            bids = int(bm.group(1))
        sold_at = _parse_sold_date(text)
        img = node.css_first("img")
        image = None
        if img:
            image = img.attributes.get("src") or img.attributes.get("data-src")
        yield {
            "id": m.group(1), "url": href.split("?")[0], "title": title, "price": price,
            "currency": currency, "shipping": ship, "bids": bids, "sold_at": sold_at, "image": image,
        }


def _parse_sold_date(text: str) -> datetime | None:
    m = re.search(r"(?:vendut[oi]|sold|verkauft)\s+(?:il\s+|am\s+)?(\d{1,2})\s+([a-zä]{3})[a-z.]*\s+(\d{4})",
                  text, re.I)
    if not m:
        return None
    mon = IT_MONTHS.get(m.group(2).lower()[:3])
    if not mon:
        return None
    try:
        return datetime(int(m.group(3)), mon, int(m.group(1)), tzinfo=timezone.utc)
    except ValueError:
        return None


async def ebay_sold(http, query: str, domain: str = "ebay.it", limit: int = 60) -> list[Comparable]:
    """Comparabili VENDUTI negli ultimi ~90 giorni (prezzo + spedizione non inclusa)."""
    params = {"_nkw": query, "LH_Sold": "1", "LH_Complete": "1", "_sop": "13", "_ipg": "120"}
    html = await http.get_text(f"https://www.{domain}/sch/i.html", params=params)
    _check_challenge(html)
    out = []
    for card in iter_cards(html):
        out.append(Comparable(
            price_eur=card["price"] if card["currency"] == "EUR" else 0.0,
            title=card["title"], source=f"{domain.replace('.', '_')}_sold", kind="sold",
            url=card["url"], sold_at=card["sold_at"],
        ))
        if len(out) >= limit:
            break
    return [c for c in out if c.price_eur > 0]
