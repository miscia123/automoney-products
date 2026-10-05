"""Dati di mercato di riferimento: cambi BCE e prezzo spot di oro e argento.

Il valore di fusione è il pavimento più affidabile che esista: un gioiello d'oro
generico o una sterlina valgono almeno il loro metallo fino, e un compro oro o un
banco metalli li ritira in giornata. Per questo è il primo controllo del bot.
"""
from __future__ import annotations

import logging
import re

from .db import DB
from .http import Http

log = logging.getLogger(__name__)

TROY_OZ_G = 31.1034768
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
GOLDPRICE_URL = "https://data-asg.goldprice.org/dbXRates/EUR"
GOLDAPI_URL = "https://api.gold-api.com/price/{sym}"  # USD per oncia, gratuito e senza chiave

# valori di riserva se tutte le fonti sono irraggiungibili (aggiornali in config)
FALLBACK_FX = {"EUR": 1.0, "USD": 1.08, "GBP": 0.85, "JPY": 165.0, "CHF": 0.94, "AED": 3.97}


class Market:
    def __init__(self, http: Http, db: DB, overrides: dict | None = None):
        self.http = http
        self.db = db
        self.overrides = overrides or {}
        self.fx: dict[str, float] = dict(FALLBACK_FX)  # unità di valuta per 1 EUR
        self.gold_eur_g: float | None = self.overrides.get("gold_eur_g")
        self.silver_eur_g: float | None = self.overrides.get("silver_eur_g")

    async def refresh(self) -> None:
        await self._refresh_fx()
        await self._refresh_metals()

    async def _refresh_fx(self) -> None:
        cached = self.db.kv_get("fx", ttl_s=6 * 3600)
        if cached:
            self.fx.update(cached)
            return
        try:
            xml = await self.http.get_text(ECB_URL, retries=1)
            rates = {m.group(1): float(m.group(2))
                     for m in re.finditer(r"currency='([A-Z]{3})'\s+rate='([\d.]+)'", xml)}
            if rates:
                rates["EUR"] = 1.0
                self.fx.update(rates)
                self.db.kv_set("fx", rates)
        except Exception as e:
            log.warning("cambi BCE non disponibili, uso valori di riserva: %s", e)

    async def _refresh_metals(self) -> None:
        if self.gold_eur_g and self.silver_eur_g:
            return
        cached = self.db.kv_get("metals", ttl_s=30 * 60)
        if cached:
            self.gold_eur_g, self.silver_eur_g = cached["gold"], cached["silver"]
            return
        gold = silver = None
        try:
            data = await self.http.get_json(GOLDPRICE_URL, retries=1,
                                            headers={"Referer": "https://goldprice.org/"})
            item = data["items"][0]
            gold = float(item["xauPrice"]) / TROY_OZ_G
            silver = float(item["xagPrice"]) / TROY_OZ_G
        except Exception as e:
            log.info("goldprice.org non disponibile (%s), provo gold-api.com", e)
            try:
                usd = self.fx.get("USD", FALLBACK_FX["USD"])
                xau = await self.http.get_json(GOLDAPI_URL.format(sym="XAU"), retries=1)
                xag = await self.http.get_json(GOLDAPI_URL.format(sym="XAG"), retries=1)
                gold = float(xau["price"]) / usd / TROY_OZ_G
                silver = float(xag["price"]) / usd / TROY_OZ_G
            except Exception as e2:
                log.warning("prezzo dei metalli non disponibile: %s", e2)
        if gold and silver:
            self.gold_eur_g, self.silver_eur_g = gold, silver
            self.db.kv_set("metals", {"gold": gold, "silver": silver})
        else:
            prev = self.db.kv_get("metals")  # ultimo valore noto, anche se vecchio
            if prev:
                self.gold_eur_g, self.silver_eur_g = prev["gold"], prev["silver"]

    def to_eur(self, amount: float | None, currency: str) -> float | None:
        if amount is None:
            return None
        rate = self.fx.get(currency.upper())
        if not rate:
            raise ValueError(f"valuta sconosciuta: {currency}")
        return amount / rate

    def from_eur(self, amount: float, currency: str) -> float:
        return amount * self.fx.get(currency.upper(), 1.0)

    def melt_value(self, fine_gold_g: float | None = None, fine_silver_g: float | None = None) -> float | None:
        total = 0.0
        if fine_gold_g:
            if not self.gold_eur_g:
                return None
            total += fine_gold_g * self.gold_eur_g
        if fine_silver_g:
            if not self.silver_eur_g:
                return None
            total += fine_silver_g * self.silver_eur_g
        return round(total, 2) if total else None
