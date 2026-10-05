"""Dati di ESEMPIO per provare la dashboard senza rete (`dealhunter dashboard --demo`).

Gli annunci sono inventati ma passano dal vero motore di valutazione: costi, rischio,
offerta massima e livelli sono calcolati esattamente come nei giri reali.
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .comps import CompsResult
from .engine import Engine
from .market import Market
from .models import Comparable, Listing, SaleKind, SellerType

NOW = datetime.now(timezone.utc)


def _l(source, sid, title, price, **kw) -> Listing:
    kw.setdefault("url", f"https://example.com/{source}/{sid}")
    kw.setdefault("images", [])
    return Listing(source=source, source_id=sid, title=title, price=price, **kw)


# (annuncio, prezzo centrale dei venduti simili, n. comparabili)
SAMPLES: list[tuple[Listing, float | None, int]] = [
    (_l("catawiki", "91000001", "Omega Speedmaster Professional Moonwatch 3570.50", 1500, kind=SaleKind.AUCTION,
        bids=12, ends_at=NOW + timedelta(minutes=14), country="EU"), 4200, 14),
    (_l("subito", "612345678", "Bracciale oro giallo 18kt", 2000, location="Milano, Lombardia",
        description="Bracciale in oro giallo 18 kt, peso 40 grammi, punzonato 750. Visionabile a Milano.",
        seller_type=SellerType.PRIVATE, shipping=0.0), None, 0),
    (_l("subito", "612399001", "Lotto 10 sterline oro Elisabetta II", 5250, location="Torino, Piemonte",
        description="Dieci sterline oro, conservazione SPL. Consegna a mano.", seller_type=SellerType.PRIVATE,
        shipping=0.0), None, 0),
    (_l("affide", "448812", "Anello oro 18kt con brillanti", 900, kind=SaleKind.AUCTION,
        ends_at=NOW + timedelta(hours=20), description="Anello in oro 750, gr 20, brillanti ct 0,30 circa.",
        seller_type=SellerType.INSTITUTION), None, 0),
    (_l("judicial", "fc-1661132", "Orologio Rolex Explorer II ref. 216570", 4500, kind=SaleKind.AUCTION,
        ends_at=NOW + timedelta(hours=9), seller_type=SellerType.INSTITUTION, location="Como",
        description="Tribunale di Como, lotto 3. Funzionante, senza scatola."), 8600, 11),
    (_l("kleinanzeigen", "2911111111", "Omega Speedmaster Professional 3570.50 Moonwatch", 2950,
        country="DE", location="80331 Altstadt", description="[trattabile] Uhr läuft einwandfrei, mit Box und Papieren.",
        seller_type=SellerType.PRIVATE), 4200, 14),
    (_l("marktplaats", "m2100000001", "Tudor Black Bay 58 79030N", 2650, country="NL", location="Utrecht",
        description="Compleet met doos en papieren", seller_type=SellerType.PRIVATE), 3350, 16),
    (_l("vinted", "5550001", "Cartier Tank Solo quarzo", 1100, country="EU", seller_rating=0.98,
        description="Cartier · Ottime"), 1850, 9),
    (_l("zoll", "771234", "Herrenarmbanduhr Rolex Datejust 16234", 3100, kind=SaleKind.AUCTION, bids=19,
        ends_at=NOW + timedelta(hours=5), country="DE", seller_type=SellerType.INSTITUTION), 6500, 12),
    (_l("watchexchange", "1abcdef", "Tudor Black Bay 58 Navy 79030B", 3050, currency="USD", country="US",
        description="Full set, 2022. Ships worldwide."), 3300, 10),
    (_l("chrono24", "38123456", "Rolex Submariner Date 116610LN Acciaio 2016 full set", 8950, location="Italia, Milano",
        shipping=45, seller_type=SellerType.PRIVATE), 10200, 18),
    (_l("orologipassioni", "123456", "Rolex Submariner 124060 ITA", 9800, description="full set garanzia italiana"),
     10400, 12),
    (_l("catawiki", "91000077", "Omega Seamaster 300 165.024 vintage", 900, kind=SaleKind.AUCTION, bids=7,
        ends_at=NOW + timedelta(days=3), country="EU"), 4800, 6),
    (_l("subito", "612377777", "Rolex Submariner 116610LN", 2500, description="regalo, vendo urgente",
        seller_type=SellerType.PRIVATE), 10000, 12),
    (_l("subito", "612388888", "Orologio Universal Genève Polerouter oro", 1300,
        description="Automatico, anni 60, cassa oro 18kt.", seller_type=SellerType.PRIVATE), None, 0),
    (_l("judicial", "ag-90311", "Lotto di 35 monete in argento Regno d'Italia", 600, kind=SaleKind.AUCTION,
        ends_at=NOW + timedelta(days=2), seller_type=SellerType.INSTITUTION), None, 0),
]


class _DemoComps:
    def __init__(self, centers: dict[str, tuple[float | None, int]]):
        self.centers = centers

    async def get(self, listing, attrs):
        center, n = self.centers.get(listing.key, (None, 0))
        if not center:
            return CompsResult()
        rnd = random.Random(listing.key)
        return CompsResult([
            Comparable(price_eur=round(center * rnd.uniform(0.93, 1.07), -1), title=f"{listing.title} #{i + 1}",
                       source=rnd.choice(["ebay_it_sold", "ebay_de_sold", "watchcollecting_sold", "storico_catawiki"]),
                       url=f"https://www.ebay.it/itm/2965{rnd.randint(10_000_000, 99_999_999)}",
                       sold_at=NOW - timedelta(days=rnd.randint(2, 80)))
            for i in range(n)])


def build_demo_db(cfg: dict) -> str:
    return asyncio.run(build_demo_db_async(cfg))


async def build_demo_db_async(cfg: dict) -> str:
    path = Path(cfg["db_path"])
    if path.exists():
        path.unlink()
    eng = Engine(cfg)
    eng.market = Market(None, eng.db, {"gold_eur_g": 100.0, "silver_eur_g": 1.15})
    eng.db.kv_set("metals", {"gold": 100.0, "silver": 1.15})
    eng.db.kv_set("fx", {"EUR": 1.0, "USD": 1.08, "GBP": 0.85, "CHF": 0.94, "JPY": 165.0})
    eng.comps = _DemoComps({l.key: (c, n) for l, c, n in SAMPLES})

    for l, _c, _n in SAMPLES:
        eng.db.needs_eval(l, 0)
        d = await eng.evaluate(l)
        if d:
            eng.db.save_eval(d)
    eng.db.set_status("subito:612399001", "visto", "chiamato il venditore, appuntamento sabato")

    now = time.time()
    rnd = random.Random(7)
    for name, scfg in cfg["sources"].items():
        if not scfg.get("enabled"):
            continue
        iv = scfg.get("interval", 60) * 60
        if name == "chrono24":
            continue  # resta "mai eseguita": nella demo manca Playwright
        for k in range(8, 0, -1):
            t = now - k * iv + rnd.uniform(-60, 60)
            ok = not (name == "vinted" and k <= 3)
            eng.db.conn.execute(
                "INSERT INTO coverage(source, run_at, duration_s, queries, query_errors, listings, new_listings,"
                " capped_queries, gap_s, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, t, round(rnd.uniform(4, 90), 1), 58 if name not in ("affide", "zoll", "judicial",
                 "watchexchange", "orologipassioni") else 0, 0, rnd.randint(40, 900) if ok else 0,
                 rnd.randint(0, 40) if ok else 0, 1 if (name == "subito" and k == 2) else 0, iv,
                 int(ok), None if ok else "BlockedError 403: pagina anti-bot (DataDome)"))
        if name == "vinted":
            eng.db.conn.execute(
                "INSERT INTO source_health(source, last_ok, last_error, last_error_msg, last_count, consecutive_errors)"
                " VALUES (?,?,?,?,?,?)", (name, now - 4 * iv, now - 300, "BlockedError 403: pagina anti-bot (DataDome)",
                                          0, 3))
        else:
            eng.db.conn.execute(
                "INSERT INTO source_health(source, last_ok, last_count, consecutive_errors) VALUES (?,?,?,0)",
                (name, now - rnd.uniform(60, iv * 0.9), rnd.randint(40, 900)))
        eng.db.kv_set(f"lastrun:{name}", now - rnd.uniform(60, iv * 0.9))
    eng.db.conn.commit()
    for msg in ("catawiki: 412 annunci, 37 nuovi/cambiati, 37 valutati, 1 alert-worthy in 41.2s",
                "subito 'rolex submariner': pagina 1 tutta nuova, leggo la pagina 2",
                "sorgente vinted in errore (3 di fila): BlockedError 403: pagina anti-bot (DataDome)",
                "chrono24 richiede il browser (Playwright) e verrà saltata",
                "storico venduti: +6 esiti d'asta"):
        eng.events.append({"t": now - rnd.uniform(30, 1800), "level": "warning" if "errore" in msg else "info",
                           "msg": msg})
    eng.db.kv_set("demo_events", sorted(eng.events, key=lambda e: e["t"]))
    return str(path)
