"""Motore dei comparabili: per ogni oggetto cerca a quanto si è VENDUTO davvero.

Fonti per categoria:
- eBay.it / eBay.de venduti (tutte le categorie)
- archivio LiveAuctioneers, prezzi di aggiudicazione (orologi, gioielli, monete, arte)
- Cardmarket price guide, media venduti a 7/30 giorni (carte collezionabili)
- valore del metallo (oro/argento) calcolato in valuation.py
I risultati restano in cache 24 ore per query, così migliaia di annunci simili
costano poche richieste.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from .db import DB
from .extract import similarity
from .http import Http
from .market import Market
from .models import Attributes, Category, Comparable, Listing
from .sources.auctions import liveauctioneers_sold
from .sources.chrono24 import chrono24_asks
from .sources.ebay import ebay_sold
from .sources.watches import watchcollecting_sold

log = logging.getLogger(__name__)

CARDMARKET_BASE = "https://downloads.s3.cardmarket.com/productCatalog"
CARDMARKET_GAMES = {"pokemon": 6, "magic": 1, "yugioh": 3, "onepiece": 18, "lorcana": 19}
LA_CATEGORIES = {Category.WATCH, Category.JEWELRY, Category.COIN, Category.ART, Category.BAG}


class CompsResult(list):
    """Lista di comparabili che ricorda anche quali fonti di prezzo hanno fallito."""

    def __init__(self, items=(), errors=None):
        super().__init__(items)
        self.errors: list[str] = errors or []


class CompsEngine:
    def __init__(self, http: Http, db: DB, market: Market, cfg: dict):
        self.http = http
        self.db = db
        self.market = market
        self.cfg = cfg
        self.ttl = cfg.get("ttl_hours", 24) * 3600
        self._locks: dict[str, asyncio.Lock] = {}
        self._cardmarket: dict[int, tuple[list[dict], dict[int, dict]]] = {}
        self.errors: dict[str, str] = {}
        self.browser = None  # impostato dal motore se Chrono24 è attivo
        self.stats: dict[str, dict[str, int]] = {}  # per fonte di prezzi: risposte piene, vuote, errori
        self._zero_logged: set[str] = set()

    async def get(self, listing: Listing, attrs: Attributes) -> list[Comparable]:
        if attrs.category == Category.GOLD and not attrs.brand:
            return CompsResult()  # oro generico: decide il peso, i comparabili sarebbero rumore
        q = attrs.query_text.strip()
        if len(q.split()) < 2 and attrs.category not in (Category.BULLION_COIN,):
            return CompsResult()
        tasks, labels = [], []
        for dom in self.cfg.get("ebay_domains", ["ebay.it"]):
            tasks.append(self._cached(f"{dom}:{q}", lambda d=dom: ebay_sold(self.http, q, d)))
            labels.append(dom)
        if attrs.category in LA_CATEGORIES and self.cfg.get("liveauctioneers", True):
            tasks.append(self._cached(f"la:{q}", lambda: liveauctioneers_sold(self.http, self.market, q)))
            labels.append("liveauctioneers")
        if attrs.category == Category.WATCH and self.browser is not None:
            tasks.append(self._cached(f"c24:{q}", lambda: chrono24_asks(self.browser, self.market, q)))
            labels.append("chrono24")
        if attrs.category == Category.WATCH and self.cfg.get("watchcollecting", True):
            tasks.append(self._cached(f"wc:{q}", lambda: watchcollecting_sold(
                self.http, self.market, self.cfg.get("watchcollecting_cfg") or {}, q)))
            labels.append("collecting")
        if attrs.category == Category.WATCH and self.cfg.get("own_history", True):
            tasks.append(self._own_history(listing, attrs))
            labels.append("storico")
        if attrs.category == Category.CARD and self.cfg.get("cardmarket", True):
            tasks.append(self._cardmarket_comps(listing, attrs))
            labels.append("cardmarket")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        comps: list[Comparable] = []
        errors: list[str] = []
        for label, r in zip(labels, results):
            self.stats.setdefault(label, {"ok": 0, "empty": 0, "error": 0})
            if isinstance(r, Exception):
                self.stats[label]["error"] += 1
            else:
                self.stats[label]["ok" if r else "empty"] += 1
                if not r and label not in self._zero_logged and label != "storico":
                    self._zero_logged.add(label)
                    d = next((v for h, v in (getattr(self.http, "last", None) or {}).items() if label.split(".")[0] in h), None)
                    if d:
                        log.warning("venduti %s per %r: 0 risultati | %s %sB | titolo=%r | segni=%s | inizio=%r",
                                    label, q, d["status"], d["bytes"], d["title"], d["markers"], d["head"][:400])
        for r in results:
            if isinstance(r, Exception):
                log.info("comparabili non disponibili per %r: %s", q, r)
                self.errors[type(r).__name__] = str(r)[:200]
                errors.append(f"{type(r).__name__}: {str(r)[:120]}")
                continue
            comps += r
        return CompsResult(comps[: self.cfg.get("max_per_query", 40) * 3], errors)

    async def _cached(self, key: str, fetch) -> list[Comparable]:
        hit = self.db.get_comps(key, self.ttl)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # due annunci uguali in parallelo: una sola richiesta
            hit = self.db.get_comps(key, self.ttl)
            if hit is not None:
                return hit
            comps = await fetch()
            self.db.put_comps(key, comps)
            return comps

    # --- storico proprio (aste chiuse rilette dal bot) ---------------------------------
    async def _own_history(self, listing: Listing, attrs: Attributes) -> list[Comparable]:
        from datetime import datetime, timezone

        ref = attrs.query_text or listing.title
        out = []
        for r in self.db.sold_history(attrs.category.value):
            sim = max(similarity(ref, r["title"]), similarity(listing.title, r["title"]))
            if attrs.reference and attrs.reference.lower() in r["title"].lower():
                sim = max(sim, 0.9)
            if sim >= 0.45:
                out.append(Comparable(price_eur=r["price_eur"], title=r["title"], source=f"storico_{r['source']}",
                                      kind="sold", url=r["url"], similarity=sim,
                                      sold_at=datetime.fromtimestamp(r["sold_at"], tz=timezone.utc)))
        return out[:40]

    # --- Cardmarket -------------------------------------------------------------
    async def _load_cardmarket(self, game: int):
        if game in self._cardmarket:
            return self._cardmarket[game]
        cached = self.db.kv_get(f"cardmarket:{game}", ttl_s=24 * 3600)
        if cached is None:
            prods = await self.http.get_json(f"{CARDMARKET_BASE}/productList/products_singles_{game}.json")
            prices = await self.http.get_json(f"{CARDMARKET_BASE}/priceGuide/price_guide_{game}.json")
            cached = {
                "products": [{"id": p["idProduct"], "name": p.get("name", "")} for p in prods.get("products", [])],
                "prices": {str(p["idProduct"]): p for p in prices.get("priceGuides", [])},
            }
            self.db.kv_set(f"cardmarket:{game}", cached)
        prices = {int(k): v for k, v in cached["prices"].items()}
        self._cardmarket[game] = (cached["products"], prices)
        return self._cardmarket[game]

    async def _cardmarket_comps(self, listing: Listing, attrs: Attributes) -> list[Comparable]:
        t = listing.title.lower()
        game = (6 if re.search(r"pok[eé]mon|pikachu|charizard", t) else
                1 if re.search(r"magic|mtg", t) else 3 if re.search(r"yu-?gi-?oh", t) else
                18 if "one piece" in t else 19 if "lorcana" in t else None)
        if game is None:
            return []
        products, prices = await self._load_cardmarket(game)
        best = sorted(products, key=lambda p: similarity(listing.title, p["name"]), reverse=True)[:3]
        out = []
        for p in best:
            sim = similarity(listing.title, p["name"])
            pg = prices.get(p["id"])
            if sim < 0.3 or not pg:
                continue
            val = pg.get("avg30") or pg.get("trend") or pg.get("avg")
            if not val:
                continue
            if attrs.grade:
                # le carte gradate valgono un multiplo del raw: Cardmarket non basta
                continue
            out.append(Comparable(price_eur=float(val), title=p["name"], source="cardmarket", kind="index",
                                  url=f"https://www.cardmarket.com/it/Products/Search?idProduct={p['id']}",
                                  similarity=sim))
        return out
