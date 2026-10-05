"""Vendite giudiziarie di beni mobili: Portale Vendite Pubbliche, Astegiudiziarie.it, Fallcoaste.

PVP e Astegiudiziarie espongono API JSON pubbliche (verificate su scraper open source
del 2026). Fallcoaste è HTML: leggiamo le pagine di categoria di orologi, gioielli e
preziosi e poi la scheda di ogni lotto nuovo. Il filtro su "è un oggetto prezioso?" lo
fa poi extract.py sul testo del lotto.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..models import Category, Listing, SaleKind, SellerType
from .base import Source, parse_price

log = logging.getLogger(__name__)

PVP_API = "https://pvp.giustizia.it/ric-496b258c-986a1b71/ric-ms/ricerca/vendite"
PVP_DETAIL = "https://pvp.giustizia.it/pvp/it/detail_annuncio.page?idAnnuncio={id}"
AG_API = "https://webapi.astegiudiziarie.it/api/Search"
AG_HEADERS = {"Content-Type": "application/json", "Origin": "https://www.astegiudiziarie.it",
              "Referer": "https://www.astegiudiziarie.it/"}
FALLCOASTE = "https://www.fallcoaste.it"
FALLCOASTE_CATS = ["/categoria/orologi-e-gioielli-245.html", "/categoria/preziosi-635.html",
                   "/categoria/arte-oreficeria-orologeria-antiquariato-633.html"]

PRECIOUS_RE = re.compile(
    r"\boro\b|orolog|rolex|omega|cartier|patek|gioiell|brillant|diamant|preziosi|anello|bracciale|collana|"
    r"sterlin|marengh|lingott|moneta|monete|argento|dipint|quadro|opera d.arte|scultur|borsa|herm[eè]s|chanel",
    re.I,
)


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class Judicial(Source):
    name = "judicial"
    profile = "judicial"
    catalog_mode = True

    async def catalog(self) -> list[Listing]:
        out: list[Listing] = []
        errors: list[Exception] = []
        subs = (self._pvp, self._astegiudiziarie, self._fallcoaste)
        for fn in subs:
            try:
                got = await fn()
                log.info("judicial/%s: %d lotti", fn.__name__.strip("_"), len(got))
                out += got
            except Exception as e:  # una sotto-sorgente rotta non deve fermare le altre
                log.warning("judicial/%s errore: %s", fn.__name__.strip("_"), e)
                errors.append(e)
        if len(errors) == len(subs):
            raise errors[0]
        return out

    async def _pvp(self) -> list[Listing]:
        out = []
        for page in range(self.cfg.get("pvp_max_pages", 3)):
            data = await self.http.post_json(
                f"{PVP_API}?page={page}&size=500&sort=dataVendita,asc",
                {"tipoLotto": "MOBILI", "filtroAnnunci": 1},
                headers={"Origin": "https://pvp.giustizia.it", "Referer": "https://pvp.giustizia.it/pvp/"},
            )
            body = data.get("body") or data
            for lot in body.get("content", []):
                desc = lot.get("descLotto") or ""
                if not PRECIOUS_RE.search(desc + " " + str(lot.get("categoriaBene") or "")):
                    continue
                price = lot.get("offertaMinima") or lot.get("prezzoBaseAsta")
                if not price:
                    continue
                addr = lot.get("indirizzo") or {}
                out.append(Listing(
                    source="judicial", source_id=f"pvp-{lot.get('id')}",
                    url=PVP_DETAIL.format(id=lot.get("id")), title=desc[:140], description=desc,
                    price=float(price), kind=SaleKind.AUCTION,
                    ends_at=_dt(lot.get("dataOraVendita") or lot.get("dataVendita")),
                    location=", ".join(x for x in (addr.get("citta"), addr.get("provincia")) if x) or None,
                    seller_type=SellerType.INSTITUTION, country="IT",
                    raw={"tribunale": lot.get("tribunale"), "procedura": lot.get("procedura"),
                         "prezzo_base": lot.get("prezzoBaseAsta"), "categoria": lot.get("categoriaBene")},
                ))
            if body.get("last", True):
                break
        return out

    async def _astegiudiziarie(self) -> list[Listing]:
        search = {
            "tipoRicerca": 2, "idTipologie": [11], "orderBy": 6, "storica": False,
            "idCategorie": None, "prezzoDa": None, "prezzoA": None, "regione": None, "provincia": None,
            "comune": None, "idTribunale": None, "dataVenditaDa": None, "dataVenditaA": None,
        }
        pins = await self.http.post_json(f"{AG_API}/Map", search, headers=AG_HEADERS)
        ids = [p["idLotto"] for p in pins if p.get("idLotto")][: self.cfg.get("ag_max_lots", 400)]
        out = []
        for i in range(0, len(ids), 20):
            recs = await self.http.post_json(f"{AG_API}/Data", ids[i:i + 20], headers=AG_HEADERS)
            for r in recs or []:
                price = r.get("prezzoBase")
                if not price:
                    continue
                desc = r.get("descrizione") or ""
                url = r.get("urlSchedaDettagliata") or f"https://www.astegiudiziarie.it/vendita/{r.get('idLotto')}"
                if url.startswith("/"):
                    url = "https://www.astegiudiziarie.it" + url
                out.append(Listing(
                    source="judicial", source_id=f"ag-{r.get('idLotto')}", url=url,
                    title=desc[:140], description=desc,
                    price=round(float(price) * 0.75, 2),  # offerta minima = 75% del prezzo base
                    kind=SaleKind.AUCTION,
                    ends_at=_dt(r.get("dataVendita") or r.get("dataInizioGara")),
                    location=", ".join(x for x in (r.get("comune"), r.get("provincia")) if x) or None,
                    seller_type=SellerType.INSTITUTION, country="IT",
                    images=[r["urlPhoto"]] if r.get("urlPhoto") else [],
                    raw={"tribunale": r.get("tribunale"), "prezzo_base": price,
                         "telematica": r.get("venditaTelematica")},
                ))
        return out

    async def _fallcoaste(self) -> list[Listing]:
        lot_urls: list[str] = []
        for cat in FALLCOASTE_CATS:
            for page in range(1, self.cfg.get("fallcoaste_pages", 3) + 1):
                html = await self.http.get_text(f"{FALLCOASTE}{cat}", params={"page": str(page)})
                found = sorted(set(re.findall(r'href="(/vendita/[^"]+-\d+\.html)"', html)))
                new = [u for u in found if u not in lot_urls]
                if not new:
                    break
                lot_urls += new
        known = self.cfg.get("_known") or (lambda key: False)
        out = []
        for path in lot_urls[: self.cfg.get("fallcoaste_max_lots", 150)]:
            sid = "fc-" + re.search(r"-(\d+)\.html$", path).group(1)
            if known(f"judicial:{sid}"):
                continue  # scheda già letta: il prezzo base non cambia fino al prossimo esperimento
            try:
                html = await self.http.get_text(FALLCOASTE + path)
            except Exception as e:
                log.debug("fallcoaste %s: %s", path, e)
                continue
            if l := parse_fallcoaste_lot(html, FALLCOASTE + path, sid):
                out.append(l)
        return out


def parse_fallcoaste_lot(html: str, url: str, sid: str) -> Listing | None:
    tree = HTMLParser(html)
    h1 = tree.css_first("h1")
    title = h1.text(strip=True) if h1 else ""
    text = tree.body.text(separator=" ", strip=True) if tree.body else ""
    m = re.search(r"(?:offerta\s+minima|prezzo\s+base|base\s+d.asta|offerta\s+corrente|prezzo\s+attuale)"
                  r"[^\d€]{0,40}(?:€\s*)?([\d.]+(?:,\d{2})?)", text, re.I)
    if not title or not m:
        return None
    price = parse_price(m.group(1))
    if not price:
        return None
    end = None
    em = re.search(r"(?:fine|termine|scadenza)[^\d]{0,30}(\d{2})/(\d{2})/(\d{4})(?:\D+(\d{2}):(\d{2}))?", text, re.I)
    if em:
        d, mo, y, hh, mm = em.groups()
        try:
            end = datetime(int(y), int(mo), int(d), int(hh or 12), int(mm or 0))
        except ValueError:
            pass
    desc_node = tree.css_first(".descrizione, #descrizione, .description, [itemprop=description]")
    desc = desc_node.text(separator=" ", strip=True) if desc_node else text[:2000]
    img = tree.css_first("meta[property='og:image']")
    return Listing(
        source="judicial", source_id=sid, url=url, title=title, description=desc,
        price=price, kind=SaleKind.AUCTION, ends_at=end, seller_type=SellerType.INSTITUTION,
        country="IT", images=[img.attributes["content"]] if img and img.attributes.get("content") else [],
        category_hint=Category.JEWELRY,
    )
