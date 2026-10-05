"""Aste di preziosi: Affide (pegno), Zoll-Auktion (Stato tedesco), Catawiki, LiveAuctioneers."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..models import Category, Comparable, Listing, SaleKind, SellerType
from .base import Query, Source, parse_price

log = logging.getLogger(__name__)


def _abs(base: str, href: str) -> str:
    return href if href.startswith("http") else base.rstrip("/") + "/" + href.lstrip("/")


# =============================================================================
# Affide - aste su pegno (ex UniCredit), lotti periziati: oro, orologi, gioielli
# =============================================================================
AFFIDE = "https://affide.it"


class Affide(Source):
    name = "affide"
    profile = "affide"
    catalog_mode = True

    async def catalog(self) -> list[Listing]:
        known = self.cfg.get("_known") or (lambda key: False)
        cal = await self.http.get_text(f"{AFFIDE}/c/prossime-aste/")
        auctions = sorted(set(re.findall(r'href="(/(?:en/)?c/auction-\d+/\d+/?)"', cal)))
        lot_paths: list[str] = []
        for a in auctions[: self.cfg.get("max_auctions", 12)]:
            try:
                html = await self.http.get_text(AFFIDE + a)
            except Exception as e:
                log.debug("affide %s: %s", a, e)
                continue
            for p in re.findall(r'href="(/(?:en/)?c/lot-\d+/\d+/?)"', html):
                if p not in lot_paths:
                    lot_paths.append(p)
        out = []
        for p in lot_paths[: self.cfg.get("max_lots", 400)]:
            sid = re.search(r"/(\d+)/?$", p).group(1)
            if known(f"affide:{sid}"):
                continue
            try:
                html = await self.http.get_text(AFFIDE + p)
            except Exception as e:
                log.debug("affide lot %s: %s", p, e)
                continue
            if l := parse_affide_lot(html, AFFIDE + p, sid):
                out.append(l)
        return out


def parse_affide_lot(html: str, url: str, sid: str) -> Listing | None:
    tree = HTMLParser(html)
    h = tree.css_first("h1") or tree.css_first("title")
    title = h.text(strip=True) if h else ""
    text = tree.body.text(separator=" ", strip=True) if tree.body else ""
    m = re.search(r"base\s+d.asta[^\d]{0,20}([\d.]+(?:,\d{2})?)", text + " " + title, re.I)
    if not m:
        return None
    price = parse_price(m.group(1))
    if not price:
        return None
    date = None
    dm = re.search(r"(\d{2})/(\d{2})/(\d{4})(?:[^\d]{1,12}(\d{1,2})[:.](\d{2}))?", title + " " + text)
    if dm:
        d, mo, y, hh, mm = dm.groups()
        try:
            date = datetime(int(y), int(mo), int(d), int(hh or 10), int(mm or 0))
        except ValueError:
            pass
    # la descrizione utile (peso, titolo dell'oro) è nel corpo del lotto
    desc_node = tree.css_first(".lot-description, .description, article, main")
    desc = desc_node.text(separator=" ", strip=True)[:2500] if desc_node else text[:2500]
    realized = None
    rm = re.search(r"prezzo\s+realizzato[^\d]{0,20}([\d.]+(?:,\d{2})?)", text, re.I)
    if rm:
        realized = parse_price(rm.group(1))
    img = tree.css_first("meta[property='og:image']")
    clean_title = re.sub(r"\s*[–-]\s*Base d.asta.*$", "", title, flags=re.I)
    first_line = desc.split(".")[0][:120]
    return Listing(
        source="affide", source_id=sid, url=url,
        title=clean_title if len(clean_title) > 25 else f"{clean_title} {first_line}".strip(),
        description=desc, price=price, kind=SaleKind.AUCTION, ends_at=date,
        seller_type=SellerType.INSTITUTION, country="IT",
        images=[_abs(AFFIDE, img.attributes["content"])] if img and img.attributes.get("content") else [],
        raw={"realized": realized},
    )


# =============================================================================
# Zoll-Auktion - aste dello Stato tedesco (orologi, gioielli, metalli preziosi, monete)
# =============================================================================
ZOLL = "https://www.zoll-auktion.de"
ZOLL_CATS = {"243": "Uhren", "242": "Schmuck", "1115": "Edelsteine & Edelmetalle", "240": "Münzen"}


class Zoll(Source):
    name = "zoll"
    profile = "zoll"
    lang = "de"
    catalog_mode = True

    async def catalog(self) -> list[Listing]:
        known = self.cfg.get("_known") or (lambda key: False)
        links: list[str] = []
        for cat in self.cfg.get("categories", list(ZOLL_CATS)):
            for page in range(1, self.cfg.get("pages", 3) + 1):
                html = await self.http.get_text(
                    f"{ZOLL}/auktion/auktionsuebersicht.php",
                    params={"n1[]": cat, "n0": "search", "pagination": str(page)},
                )
                found = re.findall(r'href="([^"]*/auktion/(?:produkt/[^"]+/\d+|auktion\.php\?id=\d+))"', html)
                new = [f for f in dict.fromkeys(found) if f not in links]
                if not new:
                    break
                links += new
        out = []
        for href in links[: self.cfg.get("max_lots", 300)]:
            sid = re.search(r"(\d+)$", href).group(1)
            if known(f"zoll:{sid}"):
                continue
            try:
                html = await self.http.get_text(_abs(ZOLL, href))
            except Exception as e:
                log.debug("zoll %s: %s", href, e)
                continue
            if l := parse_zoll_lot(html, _abs(ZOLL, href), sid):
                out.append(l)
        return out


def _label_value(tree: HTMLParser, label: str) -> str | None:
    for node in tree.css("span, td, dt, li, div"):
        t = node.text(strip=True)
        if t.rstrip(":").lower() == label.lower():
            nxt = node.next
            while nxt is not None and (nxt.tag == "-text" and not nxt.text(strip=True)):
                nxt = nxt.next
            if nxt is not None:
                return nxt.text(strip=True)
    return None


def parse_zoll_lot(html: str, url: str, sid: str) -> Listing | None:
    tree = HTMLParser(html)
    h = tree.css_first("h1")
    title = h.text(strip=True) if h else ""
    text = tree.body.text(separator=" ", strip=True) if tree.body else ""
    gebot = _label_value(tree, "Aktuelles Gebot") or _label_value(tree, "Gebot") or ""
    if not gebot:
        m = re.search(r"(?:aktuelles\s+gebot|mindestgebot|startpreis)[^\d]{0,15}([\d.]+,\d{2})", text, re.I)
        gebot = m.group(1) if m else ""
    price = parse_price(gebot)
    if not title or price is None:
        return None
    bids = None
    bm = re.search(r"(\d+)\s+Gebot", text)
    if bm:
        bids = int(bm.group(1))
    end = None
    em = re.search(r"(?:Auktionsende|Endzeit|endet am)[^\d]{0,15}(\d{2})\.(\d{2})\.(\d{4})[^\d]{1,10}(\d{2}):(\d{2})", text, re.I)
    if em:
        d, mo, y, hh, mm = map(int, em.groups())
        end = datetime(y, mo, d, hh, mm)
    ship = None
    sm = re.search(r"Versand(?:kosten)?[^\d]{0,30}([\d.]+,\d{2})\s*(?:EUR|€)", text)
    if sm:
        ship = parse_price(sm.group(1))
    pickup_only = bool(re.search(r"nur\s+Abholung|Selbstabholung|kein\s+Versand", text, re.I))
    desc_node = tree.css_first(".beschreibung, #beschreibung, .produktbeschreibung, .description")
    desc = desc_node.text(separator=" ", strip=True) if desc_node else text[:2500]
    if pickup_only:
        desc = "[SOLO RITIRO IN GERMANIA] " + desc
    img = tree.css_first("meta[property='og:image']")
    return Listing(
        source="zoll", source_id=sid, url=url, title=title, description=desc, price=price,
        kind=SaleKind.AUCTION, bids=bids, ends_at=end, shipping=ship, country="DE",
        seller_type=SellerType.INSTITUTION,
        images=[img.attributes["content"]] if img and img.attributes.get("content") else [],
        raw={"pickup_only": pickup_only},
    )


# =============================================================================
# Catawiki - aste settimanali curate (protezione PerimeterX: da casa con curl_cffi
# spesso passa, altrimenti serve il browser)
# =============================================================================
CATAWIKI = "https://www.catawiki.com"
CATAWIKI_CATS = {Category.WATCH: "333", Category.JEWELRY: "313", Category.GOLD: "313",
                 Category.BULLION_COIN: "913", Category.COIN: "718", Category.CARD: "725"}


class Catawiki(Source):
    name = "catawiki"
    profile = "catawiki"

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        # i filtri di categoria e budget nell'URL davano "nessuna corrispondenza" (scan del 5/10/2026):
        # si cerca solo per testo e il filtro sul prezzo lo fa il motore
        filters = ["bidding_end_days[]=1"] if self.cfg.get("ending_today_only") else []
        params = {"q": query.text("it"), "sort": "bidding_end_asc"}
        if filters:
            params["filters"] = "&".join(filters)
        html = await self.http.get_text(f"{CATAWIKI}/it/s", params=params)
        return await self._to_listings(parse_catawiki_search(html))

    async def scan(self) -> list[Listing]:
        """Tutti i lotti delle categorie scelte che chiudono entro 24 ore (oltre alle ricerche per modello)."""
        out: list[Listing] = []
        for cat in self.cfg.get("scan_categories", ["333"]):  # 333 = Orologi
            for page in range(1, self.cfg.get("scan_pages", 4) + 1):
                html = await self.http.get_text(
                    f"{CATAWIKI}/it/c/{cat}", params={"sort": "bidding_end_asc", "page": str(page),
                                                       "filters": "bidding_end_days[]=1"})
                lots = parse_catawiki_search(html)
                if not lots:
                    break
                out += await self._to_listings(lots, category_hint=Category.WATCH if cat == "333" else None)
        return out

    async def _bids(self, ids: list[str]) -> dict[str, dict]:
        bids: dict[str, dict] = {}
        for i in range(0, len(ids), 24):
            try:
                data = await self.http.get_json(
                    f"{CATAWIKI}/buyer/api/v3/bidding/lots", params={"ids": ",".join(ids[i:i + 24])},
                    headers={"Accept": "application/json"},
                )
                for b in data.get("lots", []):
                    bids[str(b.get("id"))] = b
            except Exception as e:
                log.info("catawiki bidding api: %s", e)
        return bids

    async def _to_listings(self, lots: list[dict], category_hint: Category | None = None) -> list[Listing]:
        if not lots:
            return []
        bids = await self._bids([str(l["id"]) for l in lots])
        out = []
        for lot in lots:
            b = bids.get(str(lot["id"]), {})
            if b.get("closed"):
                continue
            amount = (b.get("current_bid_amount") or {}).get("EUR")
            buy_now = (lot.get("buyNow") or {}).get("price_eur")
            price = amount if amount is not None else buy_now
            if price is None:
                price = 1.0  # nessuna offerta ancora: il valore utile è l'offerta massima consigliata
            out.append(Listing(
                source="catawiki", source_id=str(lot["id"]),
                url=lot.get("url") or f"{CATAWIKI}/it/l/{lot['id']}",
                title=" ".join(x for x in (lot.get("title"), lot.get("subtitle")) if x),
                price=float(price), kind=SaleKind.AUCTION if buy_now is None or amount is not None else SaleKind.BUY_NOW,
                ends_at=_ts(b.get("bidding_end_time")),
                reserve_met=None if lot.get("reservePriceSet") else True,
                shipping=0.0 if lot.get("hasFreeShipping") else None, country="EU",
                images=[u for u in (lot.get("originalImageUrl") or lot.get("thumbImageUrl"),) if u],
                category_hint=category_hint, raw={"reserve": lot.get("reservePriceSet")},
            ))
        return out

    async def result(self, listing_id: str) -> dict | None:
        """Esito di un lotto chiuso: aggiudicato o no, e a quanto. Catawiki non ha un archivio dei
        venduti, quindi il bot se lo costruisce rileggendo i lotti che aveva visto."""
        html = await self.http.get_text(f"{CATAWIKI}/it/l/{listing_id}")
        return parse_catawiki_result(html)


def _ts(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_catawiki_result(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError):
        return None
    bb = pp.get("biddingBlockResponse") or {}
    det = pp.get("lotDetailsData") or {}
    if not bb.get("closed") and not det.get("isClosed"):
        return {"closed": False}
    hammer = parse_price(str(bb.get("localizedCurrentBidAmount") or ""))
    return {
        "closed": True,
        "sold": bool(bb.get("sold")),
        "hammer_eur": hammer,
        # prezzo pagato dal compratore: martello + 9% + 3 € di Buyer Protection
        "paid_eur": round(hammer * 1.09 + 3, 2) if hammer else None,
        "title": " ".join(x for x in (det.get("lotTitle"), det.get("lotSubtitle")) if x),
        "estimate": [((det.get("expertsEstimate") or {}).get(k) or {}).get("EUR") for k in ("min", "max")],
    }


def parse_catawiki_search(html: str) -> list[dict]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        props = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError):
        return []
    lots = (props.get("searchLots") or props.get("categoryLots") or {}).get("lots") or []
    return [l for l in lots if not l.get("isVectorSearchResult")]


# =============================================================================
# LiveAuctioneers - migliaia di case d'asta; l'archivio dà i prezzi di aggiudicazione
# =============================================================================
LA_SEARCH = "https://search-party-prod.liveauctioneers.com/search/v4/web"


async def liveauctioneers_search(http, term: str, status: str = "online", page_size: int = 48) -> list[dict]:
    params = {"page": 1, "pageSize": page_size, "searchTerm": term,
              "sort": "-saleStart" if status == "archive" else "-publishDate", "status": status}
    data = await http.get_json(
        LA_SEARCH,
        params={"parameters": json.dumps(params, separators=(",", ":")), "useAuctionHouseSearchFiltering": "true"},
        headers={"Origin": "https://www.liveauctioneers.com", "Referer": "https://www.liveauctioneers.com/"},
    )
    return ((data.get("payload") or {}).get("items")) or []


class LiveAuctioneers(Source):
    name = "liveauctioneers"
    profile = "liveauctioneers"
    lang = "en"

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        q = query.foreign_text("en")
        if not q:
            return []
        items = await liveauctioneers_search(self.http, q)
        out = []
        for it in items:
            price = it.get("leadingBid") or it.get("startPrice") or it.get("lowBidEstimate")
            if not price:
                continue
            ts = it.get("saleStartTs")
            out.append(Listing(
                source="liveauctioneers", source_id=str(it.get("itemId")),
                url=f"https://www.liveauctioneers.com/item/{it.get('itemId')}_{it.get('slugWithLocation', '')}",
                title=it.get("title", ""), description=it.get("shortDescription") or "",
                price=float(price), currency=it.get("currency", "USD"), kind=SaleKind.AUCTION,
                ends_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
                estimate_low=it.get("lowBidEstimate"), estimate_high=it.get("highBidEstimate"),
                country="US", seller_type=SellerType.BUSINESS,
                images=[f"https://p1.liveauctioneers.com/{it.get('sellerId')}/{it.get('catalogId')}/{it.get('itemId')}_1_x.jpg"],
                raw={"house": it.get("sellerName")},
            ))
        return out


async def liveauctioneers_sold(http, market, term: str) -> list[Comparable]:
    """Prezzi di aggiudicazione (martello) dall'archivio LiveAuctioneers."""
    items = await liveauctioneers_search(http, term, status="archive")
    out = []
    for it in items:
        sp = it.get("salePrice")
        if not sp:
            continue
        eur = market.to_eur(float(sp), it.get("currency", "USD"))
        ts = it.get("saleStartTs")
        out.append(Comparable(
            price_eur=round(eur, 2), title=it.get("title", ""), source="liveauctioneers_sold", kind="sold",
            url=f"https://www.liveauctioneers.com/item/{it.get('itemId')}",
            sold_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
        ))
    return out


# =============================================================================
# Buyee (Yahoo Auctions Japan) - richiede il browser (challenge HTTP 202)
# =============================================================================
BUYEE = "https://buyee.jp"


class Buyee(Source):
    name = "buyee"
    profile = "buyee"
    lang = "ja"
    needs_browser = True

    async def search(self, query: Query, page: int = 1) -> list[Listing]:
        if not query.foreign_text("ja"):
            return []
        q = quote(query.foreign_text("ja"), safe="")
        params = "sort=end&order=d&translationType=98"  # ultimi inseriti
        if query.min_price:
            params += f"&aucminprice={int(query.min_price * 160)}"
        url = f"{BUYEE}/item/search/query/{q}?{params}"
        html = await self.http.get_text(url)
        return parse_buyee(html)


def parse_buyee(html: str) -> list[Listing]:
    tree = HTMLParser(html)
    out = []
    for card in tree.css("li.itemCard"):
        a = card.css_first(".itemCard__itemName a")
        if not a:
            continue
        href = a.attributes.get("href") or ""
        idm = re.search(r"/auction/([a-z]?\d+)", href)
        wb = card.css_first("[data-auction-id]")
        sid = (wb.attributes.get("data-auction-id") if wb else None) or (idm.group(1) if idm else None)
        if not sid:
            continue
        price = None
        buyout = None
        for pd in card.css("li.g-priceDetails__item"):
            label = (pd.css_first(".g-title").text(strip=True) if pd.css_first(".g-title") else "").lower()
            val = pd.css_first(".g-price")
            v = parse_price(val.text(strip=True)) if val else None
            if "buyout" in label or "即決" in label:
                buyout = v
            elif v is not None and price is None:
                price = v
        if price is None:
            price = buyout
        if price is None:
            continue
        bids = None
        for info in card.css("li.itemCard__infoItem"):
            t = info.text(separator=" ", strip=True)
            if "Bids" in t or "入札" in t:
                n = re.search(r"(\d+)", t.split(" ", 1)[-1])
                bids = int(n.group(1)) if n else None
        img = card.css_first("img.g-thumbnail__image")
        out.append(Listing(
            source="buyee", source_id=sid, url=_abs(BUYEE, href.split("?")[0]),
            title=a.text(strip=True), price=price, currency="JPY", kind=SaleKind.AUCTION, bids=bids,
            country="JP", images=[img.attributes.get("data-src")] if img and img.attributes.get("data-src") else [],
            raw={"buyout": buyout},
        ))
    return out
