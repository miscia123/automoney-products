"""Interfaccia comune delle sorgenti di annunci."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..http import Http
from ..models import Category, Listing

log = logging.getLogger(__name__)


@dataclass
class Query:
    q: str
    category: Category | None = None
    min_price: float | None = None
    max_price: float | None = None
    translations: dict[str, str] = field(default_factory=dict)  # es. {"ja": "ロレックス", "de": "..."}
    extra: dict[str, Any] = field(default_factory=dict)  # parametri specifici per sorgente

    def text(self, lang: str) -> str:
        return self.translations.get(lang, self.q)

    def foreign_text(self, lang: str) -> str | None:
        """Testo per un sito estero: la traduzione, oppure la query se non contiene parole italiane
        (marche e referenze vanno bene ovunque). None = query da saltare su quel sito."""
        if lang in self.translations:
            return self.translations[lang]
        import re

        if re.search(r"\b(orologio|oro|bracciale|collana|catena|sterlina|marengo|lingotto|usato|grammi|"
                     r"argento|anello|sigillato|borsa)\b", self.q, re.I):
            return None
        return self.q


class Source:
    """Una sorgente sa cercare annunci per una query. Niente valutazione qui dentro."""

    name: str = "base"
    profile: str = "base"  # chiave in SOURCE_PROFILES
    lang: str = "it"
    needs_browser: bool = False
    # se True la sorgente non usa le query della watchlist ma scarica tutto il catalogo
    # (es. aste su pegno, vendite giudiziarie) e il filtro avviene dopo
    catalog_mode: bool = False

    def __init__(self, http: Http, cfg: dict[str, Any] | None = None, secrets: dict | None = None):
        self.http = http
        self.cfg = cfg or {}
        self.secrets = secrets or {}

    async def search(self, query: Query) -> list[Listing]:
        raise NotImplementedError

    async def catalog(self) -> list[Listing]:
        raise NotImplementedError


def parse_price(text: str | None) -> float | None:
    """'EUR 1.350,00' / '1,350.00 €' / '42,000 yen' -> float."""
    import re

    if not text:
        return None
    t = re.sub(r"[^\d,.\-]", "", text)
    if not t:
        return None
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        head, _, tail = t.rpartition(",")
        t = t.replace(",", ".") if len(tail) in (1, 2) else t.replace(",", "")
    elif t.count(".") == 1 and len(t.rpartition(".")[2]) == 3:
        t = t.replace(".", "")  # 1.350 all'italiana
    elif t.count(".") > 1:
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None
