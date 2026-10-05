"""Chrono24: il più grande mercato di orologi. Serve due volte al bot:

1. come sorgente: gli ultimi annunci inseriti (anche da privati) per referenza o modello;
2. come prezzo di riferimento: la mediana dei prezzi RICHIESTI, scontata in valutazione
   (i prezzi chiesti sono più alti dei venduti).

Chrono24 è dietro Cloudflare: i client HTTP ricevono 403, serve un browser vero
(Playwright con profilo persistente, ~3,5 s tra le pagine). La valuta dipende dalla
geolocalizzazione: da un IP italiano su chrono24.it i prezzi sono in euro.
Selettori verificati su scraper open source del 2026 (chrono24-mcp, watch-tracker).
"""
from __future__ import annotations

import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..models import Category, Comparable, Listing, SaleKind, SellerType
from .base import Query, Source, parse_price

BASE = "https://www.chrono24.it"
SORT_NEWEST = "5"

COUNTRIES = {
    "italia": "IT", "italy": "IT", "germania": "DE", "germany": "DE", "deutschland": "DE", "francia": "FR",
    "france": "FR", "spagna": "ES", "spain": "ES", "austria": "AT", "belgio": "BE", "belgium": "BE",
    "paesi bassi": "NL", "olanda": "NL", "netherlands": "NL", "portogallo": "PT", "polonia": "PL", "grecia": "GR",
    "lussemburgo": "LU", "irlanda": "IE", "slovenia": "SI", "croazia": "HR", "repubblica ceca": "CZ",
    "svezia": "SE", "danimarca": "DK", "finlandia": "FI", "romania": "RO", "ungheria": "HU",
    "svizzera": "CH", "switzerland": "CH", "regno unito": "GB", "united kingdom": "GB", "stati uniti": "US",
    "united states": "US", "usa": "US", "giappone": "JP", "japan": "JP", "hong kong": "HK", "singapore": "SG",
    "emirati arabi uniti": "AE", "united arab emirates": "AE", "turchia": "TR", "cina": "CN", "taiwan": "TW",
    "thailandia": "TH", "canada": "CA", "australia": "AU", "monaco": "MC", "san marino": "SM",
}


def country_code(location: str | None) -> str:
    if not location:
        return "EU"
    loc = location.lower()
    for name, code in COUNTRIES.items():
        if name in loc:
            return code
    return "EU"


def search_url(q: str, sort: str = SORT_NEWEST, min_price=None, max_price=None, page: int = 1) -> str:
    from urllib.parse import urlencode

    params = {"dosearch": "true", "query": q, "pageSize": "60", "sortorder": sort, "usedOrNew": "used"}
    if min_price:
        params["priceFrom"] = str(int(min_price))
    if max_price:
        params["priceTo"] = str(int(max_price))
    if page > 1:
        params["showpage"] = str(page)  # minuscolo: "showPage" viene ignorato
    return f"{BASE}/search/index.htm?{urlencode(params)}"


class Chrono24(Source):
    name = "chrono24"
    profile = "chrono24"
    needs_browser = True
    paged = True
    page_size = 60

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        if query.category not in (None, Category.WATCH):
            return []
        html = await self.http.get_text(search_url(query.text("it"), SORT_NEWEST, query.min_price, query.max_price,
                                                   page=page))
        return parse_chrono24(html)


def parse_chrono24(html: str) -> list[Listing]:
    out = _parse_cards(html)
    if not out:
        out = _parse_jsonld(html)
    return out


def _parse_cards(html: str) -> list[Listing]:
    tree = HTMLParser(html)
    cards = tree.css("div.js-listing-item-container") or tree.css("a.js-article-item")
    out = []
    for c in cards:
        a = c if c.tag == "a" else (c.css_first("a.js-listing-item-link") or c.css_first("a[href*='--id']"))
        if not a:
            continue
        href = a.attributes.get("href") or ""
        m = re.search(r"--id(\d+)\.htm", href)
        if not m:
            continue
        texts = [p.text(strip=True) for p in c.css("p.text-ellipsis")]
        title = " ".join(t for t in texts[:2] if t) or (a.attributes.get("title") or "")
        pnode = c.css_first("p.wt-listing-item-price, .wt-listing-item-price")
        ptext = pnode.text(strip=True) if pnode else ""
        price = parse_price(ptext)
        if not title or price is None:  # "Prezzo su richiesta"
            continue
        currency = "USD" if "$" in ptext else "GBP" if "£" in ptext else "CHF" if "CHF" in ptext else "EUR"
        lnode = c.css_first("button.wt-listing-item-location, .wt-listing-item-location")
        location = (lnode.attributes.get("data-title") or lnode.text(strip=True)) if lnode else None
        img = c.css_first("img[data-lazy-sweet-spot-master-src]") or c.css_first("img")
        image = None
        if img:
            image = (img.attributes.get("data-lazy-sweet-spot-master-src") or img.attributes.get("src") or "")
            image = image.replace("_SIZE_", "480") or None
        alltext = c.text(separator=" ", strip=True).lower()
        ship = None
        sm = re.search(r"\+\s*([\d.,]+)\s*€?\s*(?:di\s+)?spedizione", alltext)
        if sm:
            ship = parse_price(sm.group(1))
        out.append(Listing(
            source="chrono24", source_id=m.group(1), url=href if href.startswith("http") else BASE + href,
            title=title, price=price, currency=currency, kind=SaleKind.BUY_NOW, shipping=ship,
            location=location, country=country_code(location),
            seller_type=SellerType.PRIVATE if "venditore privato" in alltext or "private seller" in alltext
            else SellerType.BUSINESS,
            images=[image] if image else [], category_hint=Category.WATCH,
        ))
    return out


def _parse_jsonld(html: str) -> list[Listing]:
    out = []
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes:
            if not isinstance(node, dict) or node.get("@type") != "AggregateOffer":
                continue
            for off in node.get("offers", []):
                m = re.search(r"--id(\d+)\.htm", off.get("url", ""))
                try:
                    price = float(off.get("price"))
                except (TypeError, ValueError):
                    continue
                if not m:
                    continue
                out.append(Listing(
                    source="chrono24", source_id=m.group(1), url=off["url"], title=off.get("name", ""),
                    price=price, currency=off.get("priceCurrency", node.get("priceCurrency", "EUR")),
                    kind=SaleKind.BUY_NOW, country="EU", category_hint=Category.WATCH,
                    images=[off["image"]] if isinstance(off.get("image"), str) else [],
                    raw={"seller": (off.get("seller") or {}).get("name")},
                ))
    return out


async def chrono24_asks(http, market, query: str) -> list[Comparable]:
    """Prezzi richiesti su Chrono24 per lo stesso modello: riferimento, non venduto."""
    html = await http.get_text(search_url(query, sort="1"))  # dal più economico
    out = []
    for l in parse_chrono24(html):
        eur = market.to_eur(l.price, l.currency)
        if eur:
            out.append(Comparable(price_eur=round(eur, 2), title=l.title, source="chrono24_ask", kind="ask",
                                  url=l.url))
    return out
