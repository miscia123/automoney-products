import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dealhunter.analysis import estimate_repair, landed_cost, max_bid
from dealhunter.config import SOURCE_PROFILES, load_config
from dealhunter.db import DB
from dealhunter.engine import Engine
from dealhunter.extract import extract
from dealhunter.market import Market
from dealhunter.models import Category, Comparable, Listing, SaleKind
from dealhunter.sources.auctions import parse_buyee, parse_catawiki_search
from dealhunter.sources.base import parse_price
from dealhunter.sources.ebay import iter_cards
from dealhunter.sources.subito import parse_ad
from dealhunter.sources.vinted import parse_item
from dealhunter.valuation import value

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


def L(title, price=1000.0, source="subito", desc="", **kw):
    return Listing(source=source, source_id="1", url="https://x", title=title, price=price, description=desc, **kw)


@pytest.fixture
def market(tmp_path):
    return Market(http=None, db=DB(tmp_path / "t.sqlite"), overrides={"gold_eur_g": 100.0, "silver_eur_g": 1.0})


# --- estrazione ------------------------------------------------------------------
def test_extract_bullion_and_gold():
    a = extract(L("Sterlina oro Elisabetta II 1966"))
    assert a.category == Category.BULLION_COIN and a.fine_gold_g == pytest.approx(7.3224)
    a = extract(L("Lotto 10 marenghi oro 20 lire"))
    assert a.category == Category.BULLION_COIN and a.fine_gold_g == pytest.approx(58.065)
    a = extract(L("Bracciale oro giallo 18kt", desc="peso 40 grammi, punzone 750"))
    assert a.category == Category.GOLD and a.karat == 18 and a.grams == 40


def test_extract_watch_reference_and_flags():
    a = extract(L("Rolex Submariner Date 116610LN 2015 full set"))
    assert a.category == Category.WATCH and a.brand == "Rolex" and a.model == "submariner"
    assert a.reference == "116610LN" and "full_set" in a.flags
    assert a.query_text == "Rolex submariner 116610LN"
    a = extract(L("Omega Seamaster automatico non funzionante da revisionare"))
    assert {"broken", "service"} <= a.flags
    assert "fake_risk" in extract(L("Orologio tipo rolex submariner")).flags
    assert "plated" in extract(L("Collana placcato oro 18k 20 grammi")).flags


def test_extract_cards_and_bags():
    a = extract(L("Charizard Base Set PSA 9 holo"))
    assert a.category == Category.CARD and a.grade == "PSA 9"
    a = extract(L("Borsa Hermès Kelly 28 togo gold"))
    assert a.category == Category.BAG and a.brand == "Hermès" and a.model == "kelly"


@pytest.mark.parametrize("txt,val", [("EUR 1.350,00", 1350.0), ("1,350.00 €", 1350.0), ("520,000 yen", 520000.0),
                                     ("2.600 €", 2600.0), ("€ 99,5", 99.5), ("", None)])
def test_parse_price(txt, val):
    assert parse_price(txt) == val


# --- parser delle sorgenti ---------------------------------------------------------
def test_subito_parser():
    ads = json.loads((FIX / "subito.json").read_text())["ads"]
    l = parse_ad(ads[0])
    assert l.source_id == "612345678" and l.price == 2600 and l.location == "Milano, Lombardia"
    assert l.seller_type.value == "private" and l.images[0].endswith("rule=gallery-desktop-2x-jpeg")
    assert parse_ad(ads[1]) is None  # senza prezzo


def test_vinted_parser():
    it = json.loads((FIX / "vinted.json").read_text())["items"][0]
    l = parse_item(it)
    assert l.price == 1100 and l.url == "https://www.vinted.it/items/5550001-cartier-tank-solo"
    assert l.seller_rating == 0.98


def test_ebay_cards_both_layouts():
    cards = list(iter_cards((FIX / "ebay_sold.html").read_text()))
    assert [c["id"] for c in cards] == ["296512345678", "296512345679"]  # niente Shop on eBay, intervalli, meno parole
    assert cards[0]["price"] == 6450 and cards[0]["shipping"] == 25
    assert cards[0]["sold_at"].month == 9 and cards[1]["shipping"] == 0
    assert cards[1]["title"].startswith("Rolex Datejust 16234")


def test_buyee_and_catawiki_parsers():
    b = parse_buyee((FIX / "buyee.html").read_text())[0]
    assert b.source_id == "x1122334455" and b.price == 520000 and b.currency == "JPY" and b.bids == 14
    lots = parse_catawiki_search((FIX / "catawiki_search.html").read_text())
    assert [l["id"] for l in lots] == [91000001]


# --- valutazione -------------------------------------------------------------------
def test_gold_value_from_melt(market):
    l = L("Bracciale oro 18kt", 2600, desc="40 grammi 750")
    v = value(l, extract(l), [], market)
    assert v.melt_value == pytest.approx(3000) and v.fair_value == pytest.approx(3000) and v.confidence >= 0.85


def test_comps_filtering_drops_fakes_and_outliers(market):
    l = L("Rolex Datejust 16234 acciaio")
    a = extract(l)
    comps = [Comparable(p, f"Rolex Datejust 16234 n{i}", "ebay_it_sold") for i, p in
             enumerate([6400, 6500, 6600, 6450, 6550, 6500])]
    comps += [Comparable(900, "Rolex Datejust 16234 replica", "ebay_it_sold"),
              Comparable(25000, "Rolex Datejust 16234 diamanti oro", "ebay_it_sold"),
              Comparable(5000, "Omega speedmaster", "ebay_it_sold")]
    v = value(l, a, comps, market)
    assert 6400 <= v.fair_value <= 6600
    assert all(c.price_eur < 20000 and "replica" not in c.title for c in v.comps)


def test_landed_cost_import_from_japan(market):
    l = L("Rolex Explorer 14270", 520000, source="buyee", currency="JPY", country="JP")
    a = extract(l)
    c = landed_cost(l, a, market, SOURCE_PROFILES["buyee"])
    item = 520000 / 165.0
    assert c.item_eur == pytest.approx(item, abs=1)
    assert c.import_vat > 0.2 * item and c.import_duty > 0.04 * item and c.other_fees == 15


def test_affide_premium_and_max_bid(market):
    l = L("Anello oro 18kt", 1000, source="affide", desc="gr 20", kind=SaleKind.AUCTION)
    a = extract(l)
    prof = SOURCE_PROFILES["affide"]
    c = landed_cost(l, a, market, prof)
    assert c.buyer_premium == 250 and c.vat_on_premium == 55
    mb = max_bid(l, a, market, prof, exit_net=2000, repair_mid=0, margin=0.2)
    # al prezzo massimo il margine deve essere esattamente il 20%
    total = landed_cost(l, a, market, prof, price=mb).total
    assert 2000 / total == pytest.approx(1.2, abs=0.01)


def test_repair_estimates():
    r = estimate_repair(L("Rolex Datejust non funzionante"), extract(L("Rolex Datejust non funzionante")))
    assert r.low >= 800 and r.high >= 2000
    assert estimate_repair(L("Bracciale oro 18kt rotto 30g"), extract(L("Bracciale oro 18kt rotto 30g"))).high == 0


# --- pipeline completa senza rete -----------------------------------------------------
class FakeComps:
    def __init__(self, comps):
        self.comps = comps

    async def get(self, listing, attrs):
        return list(self.comps)


@pytest.fixture
def engine(tmp_path, market):
    cfg = load_config(ROOT / "config" / "config.example.yaml")
    cfg["db_path"] = str(tmp_path / "e.sqlite")
    cfg["watchlist"] = str(ROOT / "config" / "watchlist.yaml")
    e = Engine(cfg)
    e.market = market
    return e


async def test_pipeline_gold_bracelet_priced_fairly_is_not_a_deal(engine):
    engine.comps = FakeComps([])
    ad = json.loads((FIX / "subito.json").read_text())["ads"][0]
    d = await engine.evaluate(parse_ad(ad))
    # 40 g x 0,75 x 100 €/g = 3000 di oro fino; compro oro paga il 92% = 2760
    assert d.valuation.melt_value == pytest.approx(3000)
    assert d.exit_net == pytest.approx(2760) and d.cost.total == pytest.approx(2600)
    assert d.profit == pytest.approx(160) and d.level == "none"  # ROI 6%: non basta
    assert d.resale_days == (1, 3)


async def test_pipeline_gold_bracelet_underpriced_is_good(engine):
    engine.comps = FakeComps([])
    ad = json.loads((FIX / "subito.json").read_text())["ads"][0]
    l = parse_ad(ad)
    l.price = 2000
    d = await engine.evaluate(l)
    assert d.profit == pytest.approx(760) and d.roi == pytest.approx(0.38, abs=0.01)  # ritiro a mano: niente spedizione
    assert d.level == "good" and 40 <= d.risk <= 55  # privato: pesare e saggiare prima di pagare


async def test_pipeline_watch_auction_and_fake_skip(engine):
    sold = [Comparable(p, f"Omega Speedmaster Professional 3570.50 #{i}", "ebay_it_sold")
            for i, p in enumerate([4100, 4200, 4300, 4250, 4150, 4200, 4350, 4050, 4300, 4200])]
    engine.comps = FakeComps(sold)
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)  # ancora a 1.500 a 10 minuti dalla fine
    l = Listing(source="catawiki", source_id="91000001", url="https://x",
                title="Omega Speedmaster Professional Moonwatch 3570.50", price=1500, kind=SaleKind.AUCTION,
                ends_at=soon, images=["https://a.jpg"])
    d = await engine.evaluate(l)
    assert d.valuation.fair_value == pytest.approx(4200, abs=60)
    assert 2000 < d.listing.raw["predicted_final"] < 2500  # i cecchini alzeranno comunque il prezzo
    assert d.max_bid and 2000 < d.max_bid < 3500
    assert d.level in ("good", "hot") and d.risk < 45
    far = Listing(source="catawiki", source_id="2", url="https://x", title=l.title, price=1500,
                  kind=SaleKind.AUCTION, ends_at=soon + timedelta(days=5), images=["https://a.jpg"])
    assert (await engine.evaluate(far)).level in ("watch", "none")  # lontana: prezzo finale ~3.300, niente alert
    mid = Listing(source="catawiki", source_id="3", url="https://x", title=l.title, price=1500,
                  kind=SaleKind.AUCTION, ends_at=soon + timedelta(hours=3), images=["https://a.jpg"])
    assert (await engine.evaluate(mid)).listing.raw["predicted_final"] > 3000
    assert await engine.evaluate(L("Orologio replica Omega Speedmaster")) is None


async def test_too_good_to_be_true_private_rolex(engine):
    sold = [Comparable(p, f"Rolex Submariner 116610LN #{i}", "ebay_it_sold")
            for i, p in enumerate([9800, 10000, 10200, 9900, 10100, 10000, 9950, 10050])]
    engine.comps = FakeComps(sold)
    d = await engine.evaluate(L("Rolex Submariner 116610LN", 2500, desc="regalo, vendo urgente"))
    assert d.risk >= 75 and d.level in ("none", "watch")  # sconto del 75% da privato: probabile truffa


# --- orologi: Chrono24, esiti Catawiki, storico proprio --------------------------------
def test_chrono24_parser():
    from dealhunter.sources.chrono24 import parse_chrono24

    ls = parse_chrono24((FIX / "chrono24.html").read_text())
    assert [l.source_id for l in ls] == ["38123456", "38222222"]  # "prezzo su richiesta" scartato
    sub, gmt = ls
    assert sub.price == 8950 and sub.currency == "EUR" and sub.country == "IT" and sub.shipping == 45
    assert sub.title == "Rolex Submariner Date 116610LN Acciaio 2016 full set"
    assert sub.seller_type.value == "private" and sub.images[0].endswith("38123456-480.jpg")
    assert gmt.currency == "USD" and gmt.country == "US" and gmt.url.startswith("https://www.chrono24.it/")


def test_chrono24_us_listing_pays_import(market):
    from dealhunter.sources.chrono24 import parse_chrono24

    gmt = parse_chrono24((FIX / "chrono24.html").read_text())[1]
    c = landed_cost(gmt, extract(gmt), market, SOURCE_PROFILES["chrono24"])
    assert c.import_vat > 0 and c.import_duty > 0


def test_catawiki_closed_lot_result():
    from dealhunter.sources.auctions import parse_catawiki_result

    r = parse_catawiki_result((FIX / "catawiki_lot_closed.html").read_text())
    assert r["closed"] and r["sold"] and r["hammer_eur"] == 3150
    assert r["paid_eur"] == pytest.approx(3150 * 1.09 + 3) and r["estimate"] == [3600, 4200]


async def test_harvest_builds_own_sold_history(engine):
    """Il bot rilegge le aste chiuse e il prezzo pagato diventa un comparabile per i lotti futuri."""
    from dealhunter.comps import CompsEngine
    from dealhunter.models import Attributes

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    l = Listing(source="catawiki", source_id="91000001", url="https://www.catawiki.com/it/l/91000001",
                title="Omega Speedmaster Professional Moonwatch 3570.50", price=2500, kind=SaleKind.AUCTION,
                ends_at=past)
    engine.db.needs_eval(l, 0)
    engine.db.conn.execute("UPDATE listings SET category='watch' WHERE key=?", (l.key,))

    class FakeCatawiki:
        async def result(self, sid):
            from dealhunter.sources.auctions import parse_catawiki_result
            return parse_catawiki_result((FIX / "catawiki_lot_closed.html").read_text())

    engine.sources = {"catawiki": FakeCatawiki()}
    assert await engine.harvest_results() == 1
    assert await engine.harvest_results() == 0  # già raccolto
    ce = CompsEngine(http=None, db=engine.db, market=engine.market, cfg={})
    nl = L("Omega Speedmaster Professional 3570.50 Moonwatch", source="subito")
    comps = await ce._own_history(nl, extract(nl))
    assert len(comps) == 1 and comps[0].price_eur == pytest.approx(3436.5) and comps[0].source == "storico_catawiki"
