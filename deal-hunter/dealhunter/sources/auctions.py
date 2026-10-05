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
        cal = await self.http.get_text(f"{AFFIDE}/c/prossime-aste/")
        auctions = list(dict.fromkeys(re.findall(r'href="(?:https://affide\.it)?(/c/auction-\d+/\d+/?)"', cal)))
        out: list[Listing] = []
        errors = []
        for a in auctions[: self.cfg.get("max_auctions", 20)]:
            try:
                html = await self.http.get_text(AFFIDE + a)
            except Exception as e:
                log.info("affide %s: %s", a, e)
                errors.append(e)
                continue
            out += parse_affide_auction(html)
        if auctions and len(errors) == len(auctions[: self.cfg.get("max_auctions", 20)]):
            raise errors[0]
        return out


def parse_affide_auction(html: str) -> list[Listing]:
    """La pagina di un'asta contiene tutti i lotti in JSON (const lots = {...}): una richiesta per asta."""
    i = html.find("const lots = ")
    if i < 0:
        return []
    try:
        lots, _ = json.JSONDecoder().raw_decode(html[i + len("const lots = "):])
    except ValueError:
        return []
    out = []
    for lot in (lots.values() if isinstance(lots, dict) else lots):
        if not isinstance(lot, dict) or lot.get("isClosed") or lot.get("verkauft"):
            continue
        price = lot.get("sortingPrice") or parse_price(lot.get("callPrice"))
        if not price:
            continue
        title = (lot.get("titel") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", lot.get("beschreibung") or "").strip()
        end = lot.get("ablaufzeit") or lot.get("datum")
        imgs = lot.get("bilderPublicURL") or lot.get("images400x400") or []
        sid = str(lot.get("uid"))
        out.append(Listing(
            source="affide", source_id=sid, url=_abs(AFFIDE, lot.get("detailURL") or f"/c/lot-229/{sid}/"),
            # il titolo è generico ("Anello"): la descrizione ha oro, carati e grammi, serve all'estrazione
            title=(f"{title} - {desc}" if title and desc.lower() != title.lower() else title or desc)[:200],
            description=desc, price=float(price), kind=SaleKind.AUCTION,
            ends_at=datetime.fromtimestamp(end, tz=timezone.utc) if end else None,
            seller_type=SellerType.INSTITUTION, country="IT",
            images=[_abs(AFFIDE, u) for u in imgs[:3]],
            raw={"bids": lot.get("bidCounter"), "watchers": lot.get("watcherCount"),
                 "auction": lot.get("auktion"), "group": lot.get("warengruppeElternTitel"),
                 "lot_number": lot.get("publicNummer")},
        ))
    return out


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
        lots: dict[str, Listing] = {}
        for cat in self.cfg.get("categories", list(ZOLL_CATS)):
            pages = self.cfg.get("pages", 15)
            page = 1
            while page <= pages:
                html = await self.http.get_text(
                    f"{ZOLL}/auktion/auktionsuebersicht.php",
                    params={"n1[]": cat, "n0": "search", "t": "t1", "s": "12", "pagination": str(page)},
                )
                found = parse_zoll_list(html)
                new = [l for l in found if l.source_id not in lots]
                if not new:
                    break
                for l in new:
                    lots[l.source_id] = l
                if page == 1 and (m := re.search(r"([\d.]+)\s*Treffer", html)):
                    pages = min(pages, -(-int(m.group(1).replace(".", "")) // max(len(found), 1)))
                page += 1
        out = []
        # la scheda ha la perizia (titolo dell'oro, peso, referenza, spedizione): solo per i lotti nuovi
        for sid, l in list(lots.items())[: self.cfg.get("max_lots", 400)]:
            if not known(f"zoll:{sid}"):
                try:
                    l = parse_zoll_lot(await self.http.get_text(l.url), l)
                except Exception as e:
                    log.debug("zoll %s: %s", sid, e)
            out.append(l)
        return out


def parse_zoll_list(html: str) -> list[Listing]:
    """Riquadri dell'elenco aste: titolo, prezzo attuale, offerte, luogo, tempo residuo."""
    tree = HTMLParser(html)
    out: list[Listing] = []
    seen: set[str] = set()
    for art in tree.css("ul.auktionen_kachel_list article, article.row"):
        a = art.css_first(".kachel_auktion_link a") or art.css_first('a[href*="/auktion/produkt/"]')
        if not a:
            continue
        href = a.attributes.get("href") or ""
        m = re.search(r"/(\d+)/?$", href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        title = (a.attributes.get("title") or a.text(strip=True)).strip().rstrip(",")
        pnode = art.css_first("p.text-right span") or art.css_first(".font-weight-bold")
        price = parse_price((pnode.text(strip=True) if pnode else "").replace("EUR", ""))
        if not title or price is None:
            continue
        loc = left = None
        bids = None
        for li in art.css("ul.fa-ul li"):
            lab = li.css_first("[aria-label]")
            kind = lab.attributes.get("aria-label") if lab else ""
            t = li.text(strip=True).replace("\xa0", " ")
            if kind == "Artikelstandort":
                loc = t
            elif kind == "Restlaufzeit":
                left = t
            elif kind == "Gebotsstatus" and (bm := re.search(r"(\d+)", t)):
                bids = int(bm.group(1))
        img = art.css_first("img")
        out.append(Listing(
            source="zoll", source_id=m.group(1), url=_abs(ZOLL, href), title=title, price=price,
            kind=SaleKind.AUCTION, bids=bids, ends_at=_zoll_left(left), country="DE", location=loc,
            seller_type=SellerType.INSTITUTION,
            images=[_abs(ZOLL, img.attributes["src"])] if img and img.attributes.get("src") else [],
        ))
    return out


def _zoll_left(text: str | None) -> datetime | None:
    """'noch 2 Tage 3 Std. 28 Min.' -> fine asta in UTC."""
    if not text:
        return None
    from datetime import timedelta

    d = re.search(r"(\d+)\s*Tag", text)
    h = re.search(r"(\d+)\s*Std", text)
    m = re.search(r"(\d+)\s*Min", text)
    if not (d or h or m):
        return None
    return datetime.now(timezone.utc) + timedelta(days=int(d.group(1)) if d else 0,
                                                  hours=int(h.group(1)) if h else 0,
                                                  minutes=int(m.group(1)) if m else 0)


def parse_zoll_lot(html: str, base: Listing) -> Listing:
    """Arricchisce un lotto dell'elenco con la scheda: descrizione periziata, fine asta, spedizione."""
    tree = HTMLParser(html)
    for x in tree.css("script, style, nav, header, footer"):
        x.decompose()
    text = tree.body.text(separator=" | ", strip=True) if tree.body else ""
    flat = re.sub(r"[\s|]+", " ", text)
    em = re.search(r"Auktionsende:\s*\w*\.?,?\s*(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{2}):(\d{2})", flat)
    if em:
        from zoneinfo import ZoneInfo

        d, mo, y, hh, mm = map(int, em.groups())
        base.ends_at = datetime(y, mo, d, hh, mm, tzinfo=ZoneInfo("Europe/Berlin"))
    sm = re.search(r"Versand:\s*Deutschland\s*\(([\d.]+,\d{2})\s*EUR\)", flat)
    if sm:
        base.shipping = parse_price(sm.group(1))
    pickup_only = bool(re.search(r"Versand:\s*(?:Nein|nicht möglich)|nur\s+Abholung|kein\s+Versand", flat, re.I))
    dm = re.search(r"Gegenstandsbeschreibung\s*(.*?)(?:Besichtigung, Abholung|Versandoptionen:|$)", flat, re.S)
    desc = (dm.group(1) if dm else "").strip()[:3000]
    if pickup_only:
        desc = "[SOLO RITIRO IN GERMANIA] " + desc
    if desc:
        base.description = desc
    seller = re.search(r"Anbieter:\s*(.*?)\s*(?:\(\d+ weitere|Ort:)", flat)
    base.raw = {**(base.raw or {}), "pickup_only": pickup_only,
                "seller": seller.group(1).strip() if seller else None}
    img = tree.css_first("meta[property='og:image']")
    if img and img.attributes.get("content") and not base.images:
        base.images = [img.attributes["content"]]
    return base


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


# varianti dei parametri per l'archivio: nel 2026 "status: archive" con sort -saleStart cade sull'indice LIVE.
# Si provano in ordine e si tiene la prima che risponde dall'archivio (usedIndex diverso da LIVE o venduti > 0).
LA_ARCHIVE_VARIANTS = [
    {"status": "archive"},
    {"status": "archive", "sort": "-relevance"},
    {"status": "sold"},
    {"status": "archive", "pastOnly": True},
    {"status": "past"},
]
_la_archive = {"variant": None, "tried": False}


async def _la_get(http, params: dict) -> dict:
    data = await http.get_json(
        LA_SEARCH,
        params={"parameters": json.dumps(params, separators=(",", ":")), "useAuctionHouseSearchFiltering": "true"},
        headers={"Origin": "https://www.liveauctioneers.com", "Referer": "https://www.liveauctioneers.com/"},
    )
    return data.get("payload") or {}


async def liveauctioneers_search(http, term: str, status: str = "online", page_size: int = 48) -> list[dict]:
    base = {"page": 1, "pageSize": page_size, "searchTerm": term}
    if status != "archive":
        return (await _la_get(http, {**base, "sort": "-publishDate", "status": status})).get("items") or []
    variants = [_la_archive["variant"]] if _la_archive["variant"] else \
        ([] if _la_archive["tried"] else LA_ARCHIVE_VARIANTS)
    for v in variants:
        p = await _la_get(http, {**base, **v})
        items = p.get("items") or []
        sold = [it for it in items if it.get("isSold") or it.get("salePrice")]
        if p.get("usedIndex") not in (None, "LIVE") or p.get("totalSold") or sold:
            if _la_archive["variant"] != v:
                log.info("LiveAuctioneers: archivio dei venduti raggiunto con %s", v)
            _la_archive["variant"] = v
            return sold
        if _la_archive["variant"]:
            return []  # variante buona, semplicemente nessun venduto per questa ricerca
    if not _la_archive["tried"]:
        _la_archive["tried"] = True
        log.warning("LiveAuctioneers: nessuna variante raggiunge l'archivio dei venduti (risponde solo l'indice LIVE)")
    return []


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
