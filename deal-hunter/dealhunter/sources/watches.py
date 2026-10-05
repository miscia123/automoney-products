"""Portali specializzati o forti sugli orologi usati, oltre a Chrono24 e Catawiki.

- Kleinanzeigen.de: il più grande mercato di privati in Germania (categoria Uhren & Schmuck, c157)
- Marktplaats.nl / 2dehands.be: API JSON pubblica, privati olandesi e belgi
- Willhaben.at: privati austriaci (dati __NEXT_DATA__)
- Ricardo.ch: aste svizzere (API JSON; molti spediscono solo in Svizzera)
- Reddit r/Watchexchange: post "[WTS]" di collezionisti, via feed RSS (il JSON è chiuso dal 2026)
- Watch Collecting (watchcollecting.com): aste spesso senza riserva + archivio dei VENDUTI
- Orologi & Passioni (orologi.forumfree.it): mercatino del forum italiano più grande

Endpoint e formati verificati su scraper open source 2025-2026; i dettagli non confermati sono
gestiti in modo tollerante (regex sul testo) e segnalati in docs/SELEZIONE_SITI.md.
"""
from __future__ import annotations

import html as htmlmod
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..models import Category, Comparable, Listing, SaleKind, SellerType
from .base import Query, Source, parse_price

log = logging.getLogger(__name__)

# categorie che si cercano anche sui mercati esteri (serve una query senza parole italiane o una traduzione)
FOREIGN_OK = (None, Category.WATCH, Category.JEWELRY, Category.GOLD, Category.BULLION_COIN, Category.BAG,
              Category.CARD)


def _iso(v) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


# =============================================================================
# Kleinanzeigen.de
# =============================================================================
KA = "https://www.kleinanzeigen.de"


class Kleinanzeigen(Source):
    name = "kleinanzeigen"
    profile = "kleinanzeigen"
    lang = "de"
    paged = True
    page_size = 25

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        q = query.foreign_text("de")
        if query.category not in FOREIGN_OK or not q:
            return []
        # 157 = Uhren & Schmuck; per borse, monete e carte si cerca in tutto il sito
        cat = "157" if query.category in (None, Category.WATCH, Category.JEWELRY, Category.GOLD) else ""
        params = {"keywords": q, "categoryId": cat, "sortingField": "SORTING_DATE",
                  "adType": "OFFER", "posterType": "", "pageNum": str(page), "action": "find", "radius": "0",
                  "minPrice": str(int(query.min_price)) if query.min_price else "",
                  "maxPrice": str(int(query.max_price)) if query.max_price else ""}
        page = await self.http.get_text(f"{KA}/s-suchanfrage.html", params=params,
                                        headers={"Accept-Language": "de-DE,de;q=0.9"})
        return parse_kleinanzeigen(page)


def parse_kleinanzeigen(page: str) -> list[Listing]:
    """Layout 2026: <article data-adid data-href> con titolo in h3, prezzo in p.text-title3,
    luogo e data in span, e un ld+json con titolo, descrizione e foto."""
    tree = HTMLParser(page)
    out = []
    stop_at = page.find("Alternative Anzeigen")
    for art in tree.css("article[data-adid]"):
        sid = art.attributes.get("data-adid")
        href = art.attributes.get("data-href") or ""
        if not sid:
            continue
        if stop_at > 0 and page.find(f'data-adid="{sid}"') > stop_at:
            break  # sotto questa riga ci sono annunci di altre ricerche
        ld = {}
        if s := art.css_first('script[type="application/ld+json"]'):
            try:
                ld = json.loads(s.text())
            except ValueError:
                ld = {}
        t = art.css_first("h3 a") or art.css_first("h2 a") or art.css_first("a.ellipsis")
        title = (t.text(strip=True) if t else "") or ld.get("title") or ""
        if not href and t:
            href = t.attributes.get("href") or ""
        pnode = art.css_first("p.text-title3") or art.css_first("p.aditem-main--middle--price-shipping--price")
        if pnode:
            for old in pnode.css("s, del, [class*=strike], [class*=line-through]"):
                old.decompose()
        ptext = pnode.text(strip=True) if pnode else ""
        price = parse_price(ptext.replace("VB", ""))
        if not title or not price:
            continue
        spans = [x.text(strip=True) for x in art.css("span")]
        text = art.text(separator=" ", strip=True)
        desc = ld.get("description") or ""
        if not desc:
            d = art.css_first("p.text-onSurfaceSubdued") or art.css_first(".aditem-main--middle--description")
            desc = d.text(strip=True) if d else ""
        pickup = "Nur Abholung" in text
        if pickup:
            desc = "[SOLO RITIRO IN GERMANIA] " + desc
        if "VB" in ptext:
            desc = "[trattabile] " + desc
        loc = next((x for x in spans if re.match(r"\d{5}\s+\S", x)), None)
        img = ld.get("contentUrl")
        if not img and (im := art.css_first("img[src]")):
            img = im.attributes.get("src")
        out.append(Listing(
            source="kleinanzeigen", source_id=sid, url=KA + href if href.startswith("/") else href,
            title=title, description=desc, price=price, kind=SaleKind.OFFER, country="DE",
            location=loc, seller_type=SellerType.PRIVATE, images=[img] if img else [],
            raw={"pickup_only": pickup, "negotiable": "VB" in ptext, "shipping": "Versand möglich" in text},
        ))
    return out


# =============================================================================
# Marktplaats.nl / 2dehands.be (stessa piattaforma)
# =============================================================================
class Marktplaats(Source):
    name = "marktplaats"
    profile = "marktplaats"
    lang = "en"
    paged = True
    page_size = 100

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        q = query.foreign_text("en")
        if query.category not in FOREIGN_OK or not q:
            return []
        out = []
        errors = []
        domains = self.cfg.get("domains", ["https://www.marktplaats.nl", "https://www.2dehands.be"])
        for base in domains:
            params = {"query": q, "limit": "100", "offset": str((page - 1) * 100), "sortBy": "SORT_INDEX",
                      "sortOrder": "DECREASING", "searchInTitleAndDescription": "true", "viewOptions": "list-view"}
            if query.category == Category.WATCH and "marktplaats" in base:
                params["l1CategoryId"] = "1826"
            if query.min_price or query.max_price:
                params["attributeRanges[]"] = (f"PriceCents:{int((query.min_price or 0) * 100)}:"
                                               f"{int(query.max_price * 100) if query.max_price else ''}")
            try:
                data = await self.http.get_json(f"{base}/lrp/api/search", params=params)
            except Exception as e:
                log.info("marktplaats %s: %s", base, e)
                errors.append(e)
                continue
            out += parse_marktplaats(data, base)
        if errors and len(errors) == len(domains):
            raise errors[0]
        return out


def parse_marktplaats(data: dict, base: str = "https://www.marktplaats.nl") -> list[Listing]:
    country = "BE" if "2dehands" in base else "NL"
    src = "marktplaats"
    out = []
    for it in data.get("listings", []):
        cents = (it.get("priceInfo") or {}).get("priceCents") or 0
        if cents <= 0:
            continue
        attrs = {a.get("key"): a.get("value") for a in it.get("attributes") or [] if isinstance(a, dict)}
        loc = it.get("location") or {}
        pickup = str(attrs.get("delivery", "")).lower().startswith("ophalen")
        seller = it.get("sellerInformation") or {}
        imgs = it.get("imageUrls") or []
        out.append(Listing(
            source=src, source_id=str(it.get("itemId")), url=base + it.get("vipUrl", ""),
            title=it.get("title", ""), description=("[SOLO RITIRO] " if pickup else "") + (it.get("description") or ""),
            price=cents / 100, kind=SaleKind.OFFER, country=country,
            location=loc.get("cityName"),
            seller_type=SellerType.BUSINESS if seller.get("isVerified") or seller.get("showWebsiteUrl") else SellerType.PRIVATE,
            images=["https:" + u if u.startswith("//") else u for u in imgs[:3]],
            raw={"price_type": (it.get("priceInfo") or {}).get("priceType"), "date": it.get("date")},
        ))
    return out


# =============================================================================
# Willhaben.at
# =============================================================================
class Willhaben(Source):
    name = "willhaben"
    profile = "willhaben"
    lang = "de"
    paged = True
    page_size = 60

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        q = query.foreign_text("de")
        if query.category not in FOREIGN_OK or not q:
            return []
        params = {"keyword": q, "rows": "60", "page": str(page), "sort": "1"}
        if query.min_price:
            params["PRICE_FROM"] = str(int(query.min_price))
        if query.max_price:
            params["PRICE_TO"] = str(int(query.max_price))
        page = await self.http.get_text("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz", params=params)
        return parse_willhaben(page)


def parse_willhaben(page: str) -> list[Listing]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return []
    try:
        ads = (json.loads(m.group(1))["props"]["pageProps"]["searchResult"]["advertSummaryList"]["advertSummary"])
    except (ValueError, KeyError, TypeError):
        return []
    out = []
    for ad in ads:
        attrs = {}
        for a in (ad.get("attributes") or {}).get("attribute", []):
            vals = a.get("values") or []
            attrs[a.get("name")] = vals[0] if vals else None
        price = parse_price(attrs.get("PRICE") or attrs.get("PRICE_FOR_DISPLAY"))
        if not price:
            continue
        seo = attrs.get("SEO_URL") or ""
        img = attrs.get("MMO")
        out.append(Listing(
            source="willhaben", source_id=str(ad.get("id")),
            url=seo if seo.startswith("http") else f"https://www.willhaben.at/iad/{seo.lstrip('/')}",
            title=attrs.get("HEADING") or ad.get("description", ""), description=attrs.get("BODY_DYN") or "",
            price=price, kind=SaleKind.OFFER, country="AT", location=attrs.get("LOCATION"),
            seller_type=SellerType.PRIVATE if attrs.get("ISPRIVATE") in ("1", "true", None) else SellerType.BUSINESS,
            images=[f"https://cache.willhaben.at/mmo/{img}"] if img else [],
        ))
    return out


# =============================================================================
# Ricardo.ch
# =============================================================================
class Ricardo(Source):
    name = "ricardo"
    profile = "ricardo"
    lang = "de"

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        q = query.foreign_text("de")
        if query.category not in FOREIGN_OK or not q:
            return []
        from urllib.parse import quote

        data = await self.http.get_json(f"https://www.ricardo.ch/api/mfa/search/{quote(q)}",
                                        headers={"Accept": "application/json", "Referer": "https://www.ricardo.ch/"})
        return parse_ricardo(data)


def parse_ricardo(data: dict) -> list[Listing]:
    out = []
    for a in data.get("articles", []):
        auction = bool(a.get("hasAuction"))
        price = a.get("bidPrice") if auction and a.get("bidPrice") else a.get("buyNowPrice")
        if not price:
            continue
        ship = (a.get("shipping") or [{}])[0] if a.get("shipping") else {}
        out.append(Listing(
            source="ricardo", source_id=str(a.get("id")), url=f"https://www.ricardo.ch/de/a/{a.get('id')}",
            title=a.get("title", ""), price=float(price), currency="CHF",
            kind=SaleKind.AUCTION if auction else SaleKind.BUY_NOW, ends_at=_iso(a.get("endDate")),
            country="CH", location=ship.get("city"),
            images=[a["image"]] if isinstance(a.get("image"), str) else [],
            raw={"buy_now": a.get("buyNowPrice"), "seller": a.get("sellerId")},
        ))
    return out


# =============================================================================
# Reddit r/Watchexchange (feed RSS)
# =============================================================================
RSS_NS = {"a": "http://www.w3.org/2005/Atom"}
PRICE_PATTERNS = [
    re.compile(r"\$\s?(\d[\d,]*(?:\.\d{2})?)"),
    re.compile(r"(?<![A-Za-z\d])(\d[\d,]*)\s?\$"),
    re.compile(r"(\d[\d,.]*)\s?(?:€|eur\b)", re.I),
    re.compile(r"\b(?:asking|price)\b[^\d\n]{0,20}(\d[\d,]{2,})", re.I),
]
LOC_RE = re.compile(r"-\s*([A-Z]{2,5}(?:/[A-Z]{2,5})?)\s*$")


class RedditWatchexchange(Source):
    name = "watchexchange"
    profile = "watchexchange"
    lang = "en"
    catalog_mode = True

    async def catalog(self) -> list[Listing]:
        ua = self.cfg.get("user_agent", "dealhunter/0.1 (personal watch alerts)")
        xml = await self.http.get_text("https://www.reddit.com/r/Watchexchange/new/.rss",
                                       params={"limit": "100"}, headers={"User-Agent": ua}, retries=0)
        return parse_watchexchange_rss(xml)


def _price_from(text: str) -> tuple[float | None, str]:
    for i, rx in enumerate(PRICE_PATTERNS):
        m = rx.search(text)
        if m:
            v = parse_price(m.group(1))
            if v and v >= 50:
                return v, "EUR" if i == 2 else "USD"
    return None, "USD"


def parse_watchexchange_rss(xml: str) -> list[Listing]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall("a:entry", RSS_NS):
        title = (e.findtext("a:title", "", RSS_NS) or "").strip()
        if not re.match(r"^\s*\[WTS\]", title, re.I):
            continue
        link = e.find("a:link", RSS_NS)
        url = link.get("href") if link is not None else ""
        body_html = e.findtext("a:content", "", RSS_NS) or ""
        body = re.sub(r"<[^>]+>", " ", htmlmod.unescape(body_html))
        body = re.sub(r"\s+", " ", body).strip()
        price, cur = _price_from(title)
        if price is None:
            price, cur = _price_from(body)
        if price is None:
            continue
        lm = LOC_RE.search(title)
        loc = lm.group(1) if lm else "US"
        if "CONUS" in loc:
            continue  # spedisce solo negli USA continentali
        country = "GB" if loc.startswith("UK") else "US" if loc in ("US", "USA") else loc[:2]
        clean = re.sub(r"^\s*\[WTS\]\s*", "", title, flags=re.I)
        clean = re.sub(r"\s*-\s*[$€]?\s?\d[\d,.]*\s*[$€]?\s*(?:-\s*[A-Z/]{2,9})?\s*$", "", clean)
        sid = (e.findtext("a:id", "", RSS_NS) or url).rsplit("/", 1)[-1].replace("t3_", "")
        out.append(Listing(
            source="watchexchange", source_id=sid, url=url, title=clean, description=body[:2500],
            price=price, currency=cur, kind=SaleKind.OFFER, country=country, location=loc,
            seller_type=SellerType.PRIVATE, category_hint=Category.WATCH,
            images=re.findall(r'href="(https://(?:i\.redd\.it|i\.imgur\.com)/[^"]+)"', body_html)[:3],
            raw={"posted": e.findtext("a:updated", "", RSS_NS)},
        ))
    return out


# =============================================================================
# Watch Collecting (watchcollecting.com): aste e archivio dei venduti via Typesense
# =============================================================================
WC_API = "https://dora.production.collecting.com/multi_search"
WC_SITE = "https://watchcollecting.com"


async def _wc_key(http, cfg: dict, cache: dict) -> str:
    """La chiave di ricerca è pubblica (la usa il sito dal browser) ma cambia: la rileggiamo dal sito."""
    if cfg.get("typesense_key"):
        return cfg["typesense_key"]
    if cache.get("key"):
        return cache["key"]
    import time
    if time.time() - cache.get("fail_at", 0) < 3600:  # non riprovare la scoperta a ogni annuncio
        raise RuntimeError("chiave Typesense di Watch Collecting non disponibile (nuovo tentativo tra poco)")
    page = await http.get_text(f"{WC_SITE}/buy")
    srcs = [page]
    for js in re.findall(r'src="(/_next/static/[^"]+\.js)"', page)[:15]:
        try:
            srcs.append(await http.get_text(WC_SITE + js, retries=0))
        except Exception:
            continue
    for text in srcs:
        m = (re.search(r'(?:typesense|TYPESENSE)[^"\']{0,80}["\']([A-Za-z0-9]{24,64})["\']', text)
             or re.search(r'apiKey["\']?\s*[:=]\s*["\']([A-Za-z0-9]{24,64})["\']', text))
        if m:
            cache["key"] = m.group(1)
            return cache["key"]
    cache["fail_at"] = time.time()
    raise RuntimeError("chiave Typesense di Watch Collecting non trovata: impostala in config (typesense_key)")


async def wc_search(http, cfg: dict, cache: dict, q: str, stage: str, per_page: int = 100) -> list[dict]:
    key = await _wc_key(http, cfg, cache)
    site = cfg.get("site_filter", "watches")
    body = {"searches": [{
        "collection": "production_listings", "q": q, "query_by": "title",
        # nel 2026 i lotti chiusi non hanno più dtSoldUTC: si ordina per fine asta
        "filter_by": f"sites:={site} && listingStage:={stage}" if stage == "live"
                     else f"sites:={site} && listingStage:=[sold,ended,past,completed]",
        "sort_by": "dtStageEndsUTC:asc" if stage == "live" else "dtStageEndsUTC:desc",
        "exclude_fields": "embedding,embeddingPriceBand",
        "per_page": per_page, "page": 1}]}
    data = await http.post_json(WC_API, body, headers={"X-TYPESENSE-API-KEY": key, "Origin": WC_SITE,
                                                       "Referer": WC_SITE + "/"})
    res = (data.get("results") or [{}])[0]
    if res.get("error"):
        raise RuntimeError(f"Watch Collecting: {res.get('error')}")
    return [h.get("document", {}) for h in res.get("hits", [])]


class WatchCollecting(Source):
    name = "watchcollecting"
    profile = "watchcollecting"
    lang = "en"
    _cache: dict = {}

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        q = query.foreign_text("en")
        if query.category not in (None, Category.WATCH) or not q:
            return []
        docs = await wc_search(self.http, self.cfg, self._cache, q, "live")
        return parse_watchcollecting(docs)


def parse_watchcollecting(docs: list[dict]) -> list[Listing]:
    out = []
    for d in docs:
        price = d.get("currentBid") or d.get("priceBuyNow")
        if not price:
            continue
        nr = d.get("noReserve")
        out.append(Listing(
            source="watchcollecting", source_id=str(d.get("id") or d.get("slug")),
            url=f"{WC_SITE}/for-sale/{d.get('slug')}", title=d.get("title", ""),
            description="[senza riserva] " if nr else "",
            price=float(price), currency=d.get("currencyCode", "GBP"),
            kind=SaleKind.AUCTION if d.get("currentBid") is not None else SaleKind.BUY_NOW,
            ends_at=_iso(d.get("dtStageEndsUTC")), country=(d.get("countryCode") or "GB").upper(),
            location=d.get("location"), reserve_met=True if nr else d.get("reserveMet"),
            images=[d["mainImageUrl"]] if d.get("mainImageUrl") else [], category_hint=Category.WATCH,
        ))
    return out


async def watchcollecting_sold(http, market, cfg: dict, q: str) -> list[Comparable]:
    docs = await wc_search(http, cfg, WatchCollecting._cache, q, "sold")
    out = []
    for d in docs:
        if d.get("isSoldPriceHidden") or not d.get("priceSold"):
            continue
        eur = d.get("priceNormalisedEUR") if d.get("currentBid") == d.get("priceSold") else None
        eur = eur or market.to_eur(float(d["priceSold"]), d.get("currencyCode", "GBP"))
        out.append(Comparable(price_eur=round(eur * 1.10, 2), title=d.get("title", ""),  # + 10% commissione acquirente
                              source="watchcollecting_sold", kind="sold",
                              url=f"{WC_SITE}/for-sale/{d.get('slug')}", sold_at=_iso(d.get("dtSoldUTC") or d.get("dtStageEndsUTC"))))
    return out


# =============================================================================
# Orologi & Passioni (orologi.forumfree.it): mercatino "Compro & Vendo"
# =============================================================================
OP = "https://orologi.forumfree.it"


class OrologiPassioni(Source):
    name = "orologipassioni"
    profile = "forum"
    catalog_mode = True

    async def catalog(self) -> list[Listing]:
        known = self.cfg.get("_known") or (lambda key: False)
        sections = self.cfg.get("section_ids") or OP_SECTIONS
        topics: dict[str, str] = {}
        errors = []
        for f in sections:
            try:
                page = await self.http.get_text(f"{OP}/", params={"f": f})
            except Exception as e:
                log.info("forum sezione %s: %s", f, e)
                errors.append(e)
                continue
            for tid, title in parse_forum_section(page).items():
                topics.setdefault(tid, title)
        if errors and len(errors) == len(sections):
            raise errors[0]
        out = []
        for tid, title in list(topics.items())[: self.cfg.get("max_topics", 120)]:
            if known(f"orologipassioni:{tid}"):
                continue
            try:
                page = await self.http.get_text(f"{OP}/", params={"t": tid})
            except Exception as e:
                log.debug("forum %s: %s", tid, e)
                continue
            if l := parse_forum_topic(page, tid, title):
                out.append(l)
        return out


# sezioni "Compro & Vendo" del forum (11413706 = accessori e ricambi: escluso)
OP_SECTIONS = ["190401", "640855", "552884", "7202007", "190402"]
_NOT_FOR_SALE = re.compile(r"^\s*[\[(]?\s*(compro|cerco|wtb|scambio|permuta|c)\b[\])]?|regolamento|"
                           r"\bvendut[oa]\b|\bsold\b|\bchiuso\b|feedback", re.I)


def parse_forum_section(page: str) -> dict[str, str]:
    """Discussioni di una sezione: link ?t=ID con il titolo (non i link 'ultimo messaggio' o di pagina)."""
    tree = HTMLParser(page)
    out: dict[str, str] = {}
    for a in tree.css('a[href*="?t="]'):
        href = a.attributes.get("href") or ""
        m = re.search(r"[?&]t=(\d+)", href)
        if not m or "help.forumfree" in href or re.search(r"[?&]st=|#lastpost", href):
            continue
        title = re.sub(r"\s+", " ", a.text(separator=" ", strip=True)).strip()
        if len(title) < 6 or title.startswith("Re:") or _NOT_FOR_SALE.search(title):
            continue
        out.setdefault(m.group(1), title)
    return out


def parse_forum_topic(page: str, tid: str, title: str) -> Listing | None:
    tree = HTMLParser(page)
    post = tree.css_first(".post .color, .post, td.Post, .postcolor") or tree.body
    text = post.text(separator=" ", strip=True)[:3000] if post else ""
    if re.search(r"\bvendut[oa]\b|\bsold\b|\bchiuso\b", title, re.I):
        return None
    if re.search(r"^\s*[\[(]?\s*(compro|cerco|wtb)\b", title, re.I):
        return None
    m = (re.search(r"(?:prezzo|richiesta|chiedo|price)[^\d€]{0,25}(?:€\s*)?(\d[\d.,]{2,})", text, re.I)
         or re.search(r"(?:€\s*)(\d[\d.,]{2,})|(\d[\d.,]{2,})\s*(?:€|euro)", title + " " + text, re.I))
    if not m:
        return None
    price = parse_price(next(g for g in m.groups() if g))
    if not price or price < 50:
        return None
    imgs = [i.attributes.get("src") for i in (post.css("img") if post else []) if
            (i.attributes.get("src") or "").startswith("https://") and "emoticon" not in (i.attributes.get("src") or "")]
    return Listing(
        source="orologipassioni", source_id=tid, url=f"{OP}/?t={tid}",
        title=re.sub(r"^\s*[\[(]?\s*(vendo|vendesi|wts|cedo|v)\s*[\])]?\s*[:\-]?\s*", "", title, flags=re.I),
        description=text,
        price=price, kind=SaleKind.OFFER, country="IT", seller_type=SellerType.PRIVATE,
        category_hint=Category.WATCH, images=imgs[:3],
    )
