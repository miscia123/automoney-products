"""Il cuore del bot: raccoglie annunci, li valuta in parallelo e manda gli alert.

Flusso per ogni annuncio:
  estrazione a regole -> comparabili venduti (con cache) -> valore di mercato
  -> costo finale -> riparazioni -> canale di rivendita -> guadagno e ROI
  -> rischio -> tempi di rivendita -> offerta massima -> livello
  -> (solo i migliori) perito AI -> alert
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone

from .alerts import Notifier
from .analysis import estimate_repair, landed_cost, max_bid, resale_days, risk_score
from .comps import CompsEngine
from .config import EXIT_CHANNELS, MELT_EXIT, SOURCE_PROFILES, load_watchlist
from .db import DB, LEVEL_RANK
from .extract import extract
from .http import Http, HostPolicy
from .market import Market
from .models import Attributes, Category, Deal, Listing, RepairEstimate, SaleKind, Valuation
from .sources import REGISTRY, Query, Source
from .valuation import value

log = logging.getLogger(__name__)

# quanto della distanza tra offerta attuale e valore si "mangia" la concorrenza prima della fine
# (prudente: sulle aste online molto seguite il prezzo finale arriva vicino al valore)
AUCTION_COMPETITION = {"catawiki": 0.8, "ebay": 0.75, "zoll": 0.7, "affide": 0.7, "judicial": 0.3,
                       "buyee": 0.7, "liveauctioneers": 0.7, "watchcollecting": 0.8, "ricardo": 0.7}


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.db = DB(cfg["db_path"])
        self.watchlist = load_watchlist(cfg)
        self.llm_calls = 0
        self._stack = AsyncExitStack()
        self.reviewer = None

    # --- ciclo di vita -------------------------------------------------------------
    async def __aenter__(self):
        hcfg = self.cfg["http"]
        self.http = await self._stack.enter_async_context(Http(
            policies={h: HostPolicy(**p) for h, p in hcfg.get("hosts", {}).items()},
            default_policy=HostPolicy(**hcfg.get("default", {})),
            proxy=self.cfg.get("proxy"),
        ))
        self.market = Market(self.http, self.db, self.cfg.get("market"))
        self.comps = CompsEngine(self.http, self.db, self.market, self.cfg["comps"])
        self.comps.cfg["watchcollecting_cfg"] = self.cfg["sources"].get("watchcollecting", {})
        self.notifier = Notifier(self.http, self.cfg["alerts"], self.cfg["secrets"])
        self.browser = None
        self.sources: dict[str, Source] = {}
        for name, scfg in self.cfg["sources"].items():
            if not scfg.get("enabled") or name not in REGISTRY:
                continue
            cls = REGISTRY[name]
            http = self.http
            if cls.needs_browser or scfg.get("use_browser"):
                http = await self._get_browser()
                if http is None:
                    log.warning("%s richiede il browser (Playwright) e verrà saltata", name)
                    continue
            scfg = {**scfg, "_known": self._known}
            self.sources[name] = cls(http, scfg, self.cfg["secrets"])
        if self.cfg["comps"].get("chrono24") and self.cfg["sources"].get("chrono24", {}).get("enabled"):
            self.comps.browser = await self._get_browser()
        llm_cfg = self.cfg["llm"]
        if llm_cfg.get("enabled") is True or (llm_cfg.get("enabled") == "auto" and self.cfg["secrets"]["anthropic"]):
            try:
                from .llm import Reviewer
                self.reviewer = Reviewer(llm_cfg)
            except Exception as e:
                log.warning("perito AI non disponibile: %s", e)
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def _get_browser(self):
        if self.browser is None:
            try:
                from .browser import BrowserHttp
                self.browser = await self._stack.enter_async_context(
                    BrowserHttp(warmup={"buyee.jp": "https://buyee.jp/", "www.chrono24.it": "https://www.chrono24.it/"},
                            proxy=self.cfg.get("proxy")))
            except Exception as e:
                log.warning("browser non disponibile: %s", e)
                self.browser = False
        return self.browser or None

    def _known(self, key: str) -> bool:
        """Lotto letto da poco? Le sorgenti a catalogo saltano la scheda e la rileggono una volta al giorno,
        così le aste lunghe vengono rivalutate anche a ridosso della chiusura."""
        hours = self.cfg["evaluation"].get("catalog_refetch_hours", 20)
        row = self.db.conn.execute("SELECT last_seen FROM listings WHERE key=?", (key,)).fetchone()
        return row is not None and time.time() - row["last_seen"] < hours * 3600

    # --- raccolta ------------------------------------------------------------------
    def queries_for(self, source: str) -> list[Query]:
        out = []
        for item in self.watchlist.get("queries", []):
            srcs = item.get("sources")
            if srcs and source not in srcs:
                continue
            if source in (item.get("exclude_sources") or []):
                continue
            cat = item.get("category")
            out.append(Query(
                q=item["q"], category=Category(cat) if cat else None,
                min_price=item.get("min_price"), max_price=item.get("max_price"),
                translations={k: item[k] for k in ("ja", "de", "en", "fr") if k in item},
                extra=item.get("extra") or {},
            ))
        return out

    async def collect(self, name: str) -> list[Listing]:
        src = self.sources[name]
        listings: list[Listing] = []
        if src.catalog_mode:
            listings = await src.catalog()
        else:
            queries = self.queries_for(name)
            errors: list[Exception] = []
            for q in queries:
                try:
                    got = await src.search(q)
                except Exception as e:
                    log.info("%s %r: %s", name, q.q, e)
                    errors.append(e)
                    if len(errors) >= 3 and not listings:
                        raise  # tre errori di fila e nessun risultato: la sorgente è giù
                    continue
                for l in got:
                    if q.category and l.category_hint is None:
                        l.category_hint = q.category
                    if q.max_price and l.kind != SaleKind.AUCTION and l.price > q.max_price * 1.05:
                        continue
                    if q.min_price and l.kind != SaleKind.AUCTION and l.price < q.min_price:
                        continue
                    listings.append(l)
            if queries and len(errors) == len(queries):
                raise errors[0]
            if hasattr(src, "scan") and self.cfg["sources"][name].get("scan", True):
                try:
                    listings += await src.scan()
                except Exception as e:
                    log.info("%s scansione categorie: %s", name, e)
        uniq = {l.key: l for l in listings}
        return list(uniq.values())

    # --- valutazione -----------------------------------------------------------------
    async def evaluate(self, listing: Listing) -> Deal | None:
        attrs = extract(listing)
        if "fake_risk" in attrs.flags:
            return None
        if "plated" in attrs.flags and attrs.category in (Category.GOLD, Category.BULLION_COIN):
            return None
        if attrs.category == Category.OTHER:
            return None
        if listing.price < self.cfg["evaluation"].get("min_price_eur", 20) and listing.kind != SaleKind.AUCTION:
            return None
        comps = await self.comps.get(listing, attrs)
        val = value(listing, attrs, comps, self.market, self.cfg["comps"].get("min_similarity", 0.3))
        if not val.fair_value:
            return None
        repair = estimate_repair(listing, attrs)
        deal = self.build_deal(listing, attrs, val, repair)
        if self.reviewer and LEVEL_RANK[deal.level] >= LEVEL_RANK[self.cfg["llm"].get("min_level", "good")] \
                and self.llm_calls < self.cfg["llm"].get("max_calls_per_run", 25):
            self.llm_calls += 1
            notes = await self.reviewer.review(deal)
            if notes:
                deal = self.apply_review(deal, notes)
        return deal

    def build_deal(self, listing: Listing, attrs: Attributes, val: Valuation, repair: RepairEstimate,
                   extra_risk: int = 0, llm_notes: dict | None = None) -> Deal:
        ev = self.cfg["evaluation"]
        profile = SOURCE_PROFILES.get(REGISTRY[listing.source].profile if listing.source in REGISTRY else listing.source,
                                      SOURCE_PROFILES["ebay"])
        cat = attrs.category.value
        name, share, fixed = EXIT_CHANNELS.get(cat, EXIT_CHANNELS["other"])["main"]
        fair = val.fair_value or 0.0
        exit_net = fair * share - fixed
        channel = name
        if val.melt_value and attrs.category in (Category.GOLD, Category.BULLION_COIN):
            melt_exit = val.melt_value * MELT_EXIT[cat]
            exit_net = max(melt_exit, fair * 0.92 - 10) if attrs.category == Category.BULLION_COIN else melt_exit
            if attrs.category == Category.GOLD and attrs.brand:
                exit_net = max(melt_exit, fair * 0.85)
        elif val.melt_value and val.melt_value * 0.92 > exit_net:
            exit_net, channel = val.melt_value * 0.92, "fusione (compro oro)"

        # aste: stima del prezzo finale, non solo l'offerta attuale
        price_for_profit = listing.price
        if listing.kind == SaleKind.AUCTION:
            hours = _hours_left(listing)
            current_eur = self.market.to_eur(listing.price, listing.currency) or 0
            comp = AUCTION_COMPETITION.get(listing.source, 0.7)
            if hours is not None and hours <= 0.25:
                comp *= 0.4  # ultimi minuti: resta solo l'effetto dei cecchini
            target = max(current_eur, current_eur + (fair * 0.9 - current_eur) * comp)
            price_for_profit = self.market.from_eur(target, listing.currency)
            listing.raw["predicted_final"] = round(price_for_profit, 2)
        cost = landed_cost(listing, attrs, self.market, profile, price=price_for_profit)
        invested = cost.total + repair.mid
        profit = round(exit_net - invested, 2)
        roi = round(profit / invested, 3) if invested > 0 else 0.0
        discount = max(0.0, 1 - cost.total / fair) if fair else 0.0
        risk, reasons = risk_score(listing, attrs, val, profile, discount)
        risk = max(0, min(100, risk + extra_risk))
        days = resale_days(attrs, val)
        mb = max_bid(listing, attrs, self.market, profile, exit_net, repair.mid, ev.get("target_margin", 0.2))
        mid_days = (days[0] + days[1]) / 2
        score = round(max(profit, 0) * (1 - risk / 100) * (0.5 + 0.5 * val.confidence) / (1 + mid_days / 60), 1)

        level = "none"
        for lvl in ("hot", "good", "watch"):
            t = self.cfg["levels"][lvl]
            if (profit >= t["min_profit"] and roi >= t["min_roi"] and risk <= t["max_risk"]
                    and val.confidence >= t["min_confidence"]):
                level = lvl
                break
        if level in ("hot", "good"):
            no_basis = not val.comps and not val.melt_value
            if no_basis and ev.get("require_comps_for_alert", True):
                level = "watch"
            if listing.kind == SaleKind.AUCTION:
                h = _hours_left(listing)
                if h is not None and (h < 0 or h > ev.get("auction_alert_window_hours", 24)):
                    level = "watch"
            if llm_notes and llm_notes.get("verdict") == "skip":
                level = "watch"
        return Deal(
            listing=listing, attrs=attrs, valuation=val, cost=cost, repair=repair, exit_channel=channel,
            exit_net=round(exit_net, 2), profit=profit, roi=roi, risk=risk, risk_reasons=reasons,
            resale_days=days, max_bid=mb, score=score, level=level, llm_notes=llm_notes,
        )

    def apply_review(self, deal: Deal, notes: dict) -> Deal:
        val = deal.valuation
        adj = notes.get("adjusted_fair_value_eur")
        if adj and notes.get("comps_match") in ("different", "none", "similar") and adj > 0:
            val = Valuation(fair_value=adj, low=adj * 0.85, high=adj * 1.15,
                            confidence=min(val.confidence, 0.5) if notes["comps_match"] != "similar" else val.confidence,
                            method=f"{val.method}; corretto dal perito AI", comps=val.comps,
                            melt_value=val.melt_value, notes=val.notes)
        repair = deal.repair
        if notes.get("repairs"):
            lo = sum(r["low_eur"] for r in notes["repairs"])
            hi = sum(r["high_eur"] for r in notes["repairs"])
            repair = RepairEstimate(low=max(lo, repair.low), high=max(hi, repair.high),
                                    items=[r["item"] for r in notes["repairs"]] or repair.items)
        auth = int(notes.get("authenticity_risk") or 0)
        extra = max(0, round(0.4 * (auth - deal.risk)))
        return self.build_deal(deal.listing, deal.attrs, val, repair, extra_risk=extra, llm_notes=notes)

    # --- esecuzione ----------------------------------------------------------------
    async def run_source(self, name: str) -> list[Deal]:
        t0 = time.time()
        try:
            listings = await self.collect(name)
        except Exception as e:
            n = self.db.source_error(name, f"{type(e).__name__}: {e}")
            log.warning("sorgente %s in errore (%d di fila): %s", name, n, e)
            if n == 3:
                await self.notifier.send_text(f"⚠️ La sorgente <b>{name}</b> non risponde da 3 giri: {e}")
            return []
        self.db.source_ok(name, len(listings))
        reeval = self.cfg["evaluation"].get("reeval_hours", 12) * 3600
        todo = [l for l in listings if self.db.needs_eval(l, reeval)]
        sem = asyncio.Semaphore(self.cfg["evaluation"].get("concurrency", 8))

        async def one(l: Listing) -> Deal | None:
            async with sem:
                try:
                    return await self.evaluate(l)
                except Exception as e:
                    log.debug("valutazione fallita %s: %s", l.key, e)
                    return None

        deals = [d for d in await asyncio.gather(*(one(l) for l in todo)) if d]
        quiet = self._quiet_now()
        for d in deals:
            self.db.save_eval(d)
            if self.db.should_alert(d) and (not quiet or d.level == "hot"):
                if await self.notifier.send_deal(d):
                    self.db.mark_alerted(d)
        log.info("%s: %d annunci, %d nuovi/cambiati, %d valutati, %d alert-worthy in %.1fs", name,
                 len(listings), len(todo), len(deals), sum(d.level in ("hot", "good") for d in deals),
                 time.time() - t0)
        self.db.kv_set(f"lastrun:{name}", time.time())
        return deals

    async def harvest_results(self, per_source: int = 30) -> int:
        """Rilegge le aste chiuse e salva i prezzi realmente pagati: diventano comparabili."""
        n = 0
        for name, src in self.sources.items():
            if not hasattr(src, "result"):
                continue
            for row in self.db.pending_results(name, per_source):
                sid = row["key"].split(":", 1)[1]
                try:
                    res = await src.result(sid)
                except Exception as e:
                    log.debug("esito %s: %s", row["key"], e)
                    continue
                if not res or not res.get("closed"):
                    continue
                if res.get("sold") and res.get("paid_eur"):
                    self.db.add_sold(row["key"], name, row["category"], res.get("title") or row["title"],
                                     res["paid_eur"], row["url"])
                    n += 1
                self.db.mark_harvested(row["key"])
        if n:
            log.info("storico venduti: +%d esiti d'asta", n)
        return n

    def due_sources(self) -> list[str]:
        now = time.time()
        out = []
        for name in self.sources:
            last = self.db.kv_get(f"lastrun:{name}") or 0
            if now - last >= self.cfg["sources"][name].get("interval", 60) * 60:
                out.append(name)
        return out

    async def run_once(self, only: list[str] | None = None, force: bool = False) -> list[Deal]:
        self.llm_calls = 0
        await self.market.refresh()
        names = only or (list(self.sources) if force else self.due_sources())
        names = [n for n in names if n in self.sources]
        results = await asyncio.gather(*(self.run_source(n) for n in names))
        await self.harvest_results()
        await self.maybe_digest()
        return [d for r in results for d in r]

    async def daemon(self, tick_s: int = 60) -> None:
        log.info("avvio: sorgenti attive %s", ", ".join(self.sources))
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("giro fallito")
            await asyncio.sleep(tick_s)

    def _quiet_now(self) -> bool:
        q = self.cfg["alerts"].get("quiet_hours") or []
        if len(q) != 2:
            return False
        h = datetime.now().hour
        a, b = q
        return a <= h < b if a < b else h >= a or h < b

    async def maybe_digest(self) -> None:
        hour = self.cfg["alerts"].get("digest_hour")
        if hour is None or datetime.now().hour != hour:
            return
        key = f"digest:{datetime.now():%Y-%m-%d}"
        if self.db.kv_get(key):
            return
        top = self.db.top_deals(24 * 3600, levels=("hot", "good", "watch"), limit=10)
        lines = ["<b>📋 Riepilogo affari delle ultime 24 ore</b>"]
        for d in top:
            lines.append(f"• [{d['level']}] <a href=\"{d['url']}\">{d['title'][:70]}</a> — "
                         f"{d['price']:.0f} {d['currency']}, guadagno ~€{d['profit']:.0f}, affidabilità {d['reliability']}")
        if len(lines) == 1:
            lines.append("Nessun affare sopra soglia oggi.")
        bad = [h["source"] for h in self.db.health() if (h.get("consecutive_errors") or 0) >= 3]
        if bad:
            lines.append("⚠️ Sorgenti in errore: " + ", ".join(bad))
        await self.notifier.send_text("\n".join(lines))
        self.db.kv_set(key, True)


def _hours_left(listing: Listing) -> float | None:
    if not listing.ends_at:
        return None
    end = listing.ends_at if listing.ends_at.tzinfo else listing.ends_at.replace(tzinfo=timezone.utc)
    return (end - datetime.now(timezone.utc)).total_seconds() / 3600
