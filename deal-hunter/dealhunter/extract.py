"""Estrazione rapida (a regole) di categoria, marca, referenza, peso, caratura e difetti.

Gira su ogni annuncio, quindi deve essere velocissima: niente rete, solo regex.
L'analisi AI (llm.py) interviene dopo, solo sui candidati migliori.
"""
from __future__ import annotations

import re
import unicodedata

from .models import Attributes, Category, Listing

# --- monete d'oro/argento da investimento: grammi di metallo fino --------------
# (nome, regex, oro_fino_g, argento_fino_g)
BULLION: list[tuple[str, str, float, float]] = [
    ("mezza sterlina", r"mezza\s+sterlin|half\s+sovereign|1\s*/\s*2\s+sterlin|halbe?\s+sovereign", 3.6612, 0),
    ("sterlina", r"sterlin[ae]\b|sovereign", 7.3224, 0),
    ("krugerrand 1oz", r"krugerrand", 31.1035, 0),
    ("50 pesos", r"50\s*pesos", 37.4995, 0),
    ("20 dollari", r"20\s*(dollari|dollars?|\$)\b.*(oro|gold|liberty|saint|double eagle)|double\s+eagle", 30.0927, 0),
    ("100 corone", r"100\s*(corone|kronen)", 30.4878, 0),
    ("20 corone", r"20\s*(corone|kronen)", 6.0975, 0),
    ("4 ducati", r"4\s*ducat", 13.7639, 0),
    ("ducato", r"\bducat[oi]\b|\bdukat", 3.4440, 0),
    ("20 marchi", r"20\s*(marchi|mark)\b", 7.1685, 0),
    ("10 marchi", r"10\s*(marchi|mark)\b", 3.5842, 0),
    ("10 rubli", r"10\s*rubl|chervonet", 7.7423, 0),
    ("100 lire oro", r"100\s*lire.*oro", 29.0322, 0),
    ("50 lire oro", r"50\s*lire.*oro", 14.5161, 0),
    ("10 franchi/lire oro", r"10\s*(franchi|francs|lire).*oro|oro.*10\s*(franchi|francs|lire)", 2.9032, 0),
    ("marengo 20 franchi/lire", r"marengh?[oi]|napoleon[ei]?\b|vreneli|20\s*(franchi|francs|lire)\b.*oro|oro.*20\s*(franchi|francs|lire)|20\s*francs?\s+or", 5.8065, 0),
    ("oncia oro 1oz", r"(1\s*oz|un'?oncia|1\s*oncia).*(oro|gold)|(maple\s*leaf|philharmoniker|britannia|american\s+eagle|buffalo|kangaroo|canguro|panda|libertad|lunar|nugget).*(oro|gold|goud|\boz\b|1\s*oz|unze|oncia)|"
                      r"(\boz\b|unzen?|onc[ei]a|ounce).*\b(oro|gold|goud)\b|\b(oro|gold|goud)\b.*(\boz\b|unzen?\b|onc[ei]a|ounce)", 31.1035, 0),
    ("1000 lire argento", r"1000\s*lire.*argento", 0, 12.191),
    ("500 lire argento", r"500\s*lire.*argento|500\s*lire\s+caravell", 0, 9.185),
    ("5 lire argento scudo", r"5\s*lire.*(argento|scudo)", 0, 22.5),
    ("20 lire littore", r"20\s*lire.*littore", 0, 16.0),
    ("10 lire biga", r"10\s*lire.*biga", 0, 8.35),
    ("oncia argento 1oz", r"(1\s*oz|un'?oncia|1\s*oncia).*(argento|silver)|(maple|philharmoniker|britannia|eagle|kookaburra).*(argento|silver|silber|zilver)", 0, 31.1035),
]
BULLION_RE = [(n, re.compile(p, re.I), g, s) for n, p, g, s in BULLION]

KARAT_FINENESS = {9: 0.375, 14: 0.585, 18: 0.750, 22: 0.916, 24: 0.999}

WATCH_BRANDS = {
    # nome canonico: (sinonimi, fascia) - la fascia guida revisione e canale di uscita
    "Rolex": ((r"rolex",), 1),
    "Patek Philippe": ((r"patek",), 1),
    "Audemars Piguet": ((r"audemars", r"\bap\s+royal"), 1),
    "Vacheron Constantin": ((r"vacheron",), 1),
    "A. Lange & Söhne": ((r"lange\s*(&|und|e)?\s*s[oö]hne",), 1),
    "Richard Mille": ((r"richard\s+mille",), 1),
    "Omega": ((r"\bomega\b",), 2),
    "Tudor": ((r"\btudor\b",), 2),
    "Cartier": ((r"cartier",), 2),
    "Breitling": ((r"breitling",), 2),
    "IWC": ((r"\biwc\b",), 2),
    "Jaeger-LeCoultre": ((r"jaeger", r"\bjlc\b", r"lecoultre"), 2),
    "Panerai": ((r"panerai",), 2),
    "Hublot": ((r"hublot",), 2),
    "Zenith": ((r"\bzenith\b",), 2),
    "Blancpain": ((r"blancpain",), 2),
    "Breguet": ((r"breguet",), 2),
    "Grand Seiko": ((r"grand\s+seiko",), 2),
    "Chopard": ((r"chopard",), 2),
    "Bulgari": ((r"bulgari", r"bvlgari"), 2),
    "Longines": ((r"longines",), 3),
    "TAG Heuer": ((r"tag\s*heuer", r"\bheuer\b"), 3),
    "Tissot": ((r"tissot",), 3),
    "Seiko": ((r"\bseiko\b",), 3),
    "Oris": ((r"\boris\b",), 3),
    "Hamilton": ((r"hamilton",), 3),
    "Nomos": ((r"\bnomos\b",), 3),
    "Sinn": ((r"\bsinn\b",), 3),
    "Baume & Mercier": ((r"baume",), 3),
    "Universal Genève": ((r"universal\s+gen",), 2),
    "Eberhard": ((r"eberhard",), 3),
}
WATCH_BRAND_RE = [(b, [re.compile(p, re.I) for p in pats], tier) for b, (pats, tier) in WATCH_BRANDS.items()]

WATCH_MODELS = {
    # ordine = priorità: i modelli specifici prima, "oyster perpetual" (scritto su quasi tutti i Rolex) per ultimo
    "Rolex": ["sea-dweller", "deepsea", "submariner", "day-date", "datejust", "daytona", "gmt-master", "explorer",
              "yacht-master", "air-king", "milgauss", "sky-dweller", "cellini", "bubbleback", "oyster perpetual"],
    "Omega": ["speedmaster", "seamaster", "constellation", "de ville", "aqua terra", "planet ocean", "railmaster"],
    "Audemars Piguet": ["royal oak offshore", "royal oak", "code 11.59"],
    "Patek Philippe": ["nautilus", "aquanaut", "calatrava", "complications", "gondolo"],
    "Cartier": ["santos", "tank", "ballon bleu", "panthere", "pasha", "roadster", "drive"],
    "Tudor": ["black bay", "pelagos", "ranger", "royal", "submariner"],
    "Breitling": ["navitimer", "superocean", "chronomat", "avenger", "premier"],
    "Panerai": ["luminor", "radiomir", "submersible"],
    "IWC": ["portugieser", "pilot", "portofino", "aquatimer", "ingenieur"],
    "Jaeger-LeCoultre": ["reverso", "master", "polaris"],
}

BAG_BRANDS = {
    "Hermès": (r"herm[eè]s", ["birkin", "kelly", "constance", "evelyne", "picotin", "lindy", "garden party", "bolide"]),
    "Chanel": (r"chanel", ["2.55", "timeless", "classic flap", "boy", "19", "wallet on chain", "gabrielle"]),
    "Louis Vuitton": (r"louis\s+vuitton|\blv\b", ["speedy", "neverfull", "alma", "keepall", "pochette", "capucines"]),
    "Dior": (r"\bdior\b", ["lady dior", "saddle", "book tote"]),
    "Goyard": (r"goyard", ["saint louis", "artois"]),
    "Bottega Veneta": (r"bottega", ["jodie", "cassette", "pouch"]),
    "Prada": (r"\bprada\b", ["galleria", "re-edition"]),
    "Gucci": (r"\bgucci\b", ["jackie", "marmont", "dionysus", "horsebit"]),
    "Celine": (r"c[eé]line", ["luggage", "triomphe", "belt"]),
    "Fendi": (r"\bfendi\b", ["baguette", "peekaboo"]),
}
BAG_WORDS = re.compile(r"\bborsa\b|\bbag\b|\btasche\b|\bsac\b|pochette|zaino|tote|clutch", re.I)

JEWELRY_BRANDS = {
    "Cartier": r"cartier", "Bulgari": r"bulgari|bvlgari", "Van Cleef & Arpels": r"van\s+cleef|alhambra",
    "Tiffany": r"tiffany", "Pomellato": r"pomellato", "Damiani": r"damiani", "Buccellati": r"buccellati",
    "Chopard": r"chopard", "Boucheron": r"boucheron", "Messika": r"messika", "Dodo": r"\bdodo\b",
    "Graff": r"\bgraff\b", "Harry Winston": r"harry\s+winston", "Chantecler": r"chantecler",
}
JEWELRY_MODELS = {
    "Cartier": ["juste un clou", "love", "trinity", "panthere", "panthère", "clash", "ecrou"],
    "Bulgari": ["serpenti", "b.zero1", "bzero1", "b zero1", "divas dream", "tubogas", "fiorever"],
    "Van Cleef & Arpels": ["vintage alhambra", "sweet alhambra", "magic alhambra", "alhambra", "perlée", "perlee",
                           "frivole"],
    "Tiffany": ["t wire", "hardwear", "knot", "smile", "return to tiffany", "victoria", "elsa peretti"],
    "Pomellato": ["nudo", "iconica", "sabbia", "m'ama non m'ama", "tango"],
    "Damiani": ["belle epoque", "d.side", "margherita"],
    "Chopard": ["happy diamonds", "ice cube", "happy hearts"],
    "Messika": ["move uno", "move noa", "move"],
    "Dodo": ["granelli", "nodo"],
    "Buccellati": ["macri", "opera"],
}
JEWELRY_WORDS = re.compile(
    r"anello|collana|bracciale|orecchin|ciondolo|catena|collier|spilla|parure|fede|solitario|"
    r"ring|necklace|bracelet|earring|pendant|brooch|schmuck|ohrring|halskette|armband|bague|collier|"
    r"gemelli|manschettenkn|cufflink",
    re.I,
)
WATCH_WORDS = re.compile(r"orologio|\bwatch\b|armbanduhr|\buhr\b|montre|chronograph|cronografo", re.I)
COIN_WORDS = re.compile(r"moneta|monete|\bcoin\b|münze|munze|pi[eè]ce\b|numismat", re.I)
CARD_WORDS = re.compile(
    r"pok[eé]mon|charizard|pikachu|magic\s+the\s+gathering|\bmtg\b|yu-?gi-?oh|one\s+piece\s+card|"
    r"lorcana|\bpsa\s*\d|\bbgs\s*\d|\bcgc\s*\d|booster\s+box|display\s+booster|carta\s+collezionabil",
    re.I,
)
WINE_WORDS = re.compile(r"\bvino\b|bottigli|\bwine\b|barolo|brunello|sassicaia|romanée|petrus|champagne|whisky|cognac", re.I)
ART_WORDS = re.compile(r"dipinto|olio su tela|olio su tavola|quadro\s+(d.autore|firmato|antico|ad olio|olio)|litografia|"
                       r"serigrafia|scultura|acquerello|painting|lithograph", re.I)
GOLD_WORDS = re.compile(r"\boro\b|\bgold\b|\bgolden\b|\bgelbgold|weißgold|rotgold|\bor\s+(jaune|blanc|rose)", re.I)
SILVER_WORDS = re.compile(r"argento|\bsilver\b|silber|\bargent\b", re.I)
# frazioni d'oncia (1/10 oz, 1/4 Unze, mezza oncia...): valgono la frazione, non l'oncia intera
FRACTION_RE = re.compile(r"\b1\s*/\s*(2|4|5|10|20|25|50|100|200|500|1000)\s*-?\s*(?:oz|unzen?|onc[ei]a|once|ounce|onza|ons)\b|"
                         r"\b(mezz[ao]|half|halbe?)\s+(?:oz|unzen?|onc[ei]a|ounce)\b", re.I)
# monete d'investimento in argento con lo stesso nome di quelle d'oro (Krugerrand Silber, Silver Eagle, "Ag")
SILVER_COIN_RE = re.compile(r"argento|silver|silber|zilver|\bag\b|feinsilber", re.I)
GOLD_COIN_RE = re.compile(r"\boro\b|\bgold\b|\bgoud\b|\bor\b|\bau\b", re.I)
# titoli imbottiti di nomi di monete ("Philharmoniker Eagle Krügerrand Maple Leaf..."): non si sa cosa vendono
COIN_NAMES_RE = re.compile(r"philharmoniker|eagle|kr[uü]gerrand|maple|kookaburra|britannia|panda|bison|buffalo|"
                           r"libertad|lunar|nugget|kangaroo|sovereign|sterlin|marengh|vreneli|dukat", re.I)
# annunci di chi cerca o di oggetti rubati: il prezzo non è un'offerta di vendita
WANTED_RE = re.compile(r"^\W*(cerco|compro|acquisto|suche|kaufe|ankauf|gezocht|zoek|wtb|cherche|j.ach[eè]te)\b|"
                       r"gestolen|gestohlen|\brubat[oa]\b|\bstolen\b|belohnung|beloning|ricompensa", re.I)
BAR_WORDS = re.compile(r"lingott|lingotin|\bbarr?a\b|\bbar\b|barren|gold\s*bar", re.I)

FAKE_RE = re.compile(
    r"replica|\bcopia\b|\bfalso\b|\bfake\b|imitazione|non\s+originale|\bclone\b|homage|omaggio\s+a|"
    r"\btipo\s+(rolex|omega|cartier|hermes|chanel)|\bstile\s+(rolex|omega|cartier|hermes|chanel)|"
    r"ispirat[oa]|\baaa\b|bigiotteria|\bnachbildung\b|\bimitat",
    re.I,
)
PLATED_RE = re.compile(
    r"placcat|laminat|\bdorat[oa]|gold[\s-]*(plated|filled|tone)|vergoldet|doubl[eé]|vermeil|"
    r"\bgp\b|\bgf\b|\bplaqu[eé]",
    re.I,
)
FLAG_PATTERNS = {
    "broken": r"non\s+funzion|non\s+va\b|guast|\brott[oa]\b|per\s+(pezzi|ricambi)|da\s+riparare|"
              r"defekt|for\s+parts|not\s+working|\bhs\b|ne\s+fonctionne\s+pas|spares",
    "service": r"da\s+revisionare|necessita\s+(di\s+)?revisione|\bferm[oa]\b|si\s+ferma|"
               r"ritarda|anticipa|needs?\s+service|revision\s+n[oö]tig|da\s+sistemare",
    "no_box": r"solo\s+orologio|senza\s+(scatola|corredo|garanzia)|watch\s+only|no\s+box|ohne\s+box",
    "full_set": r"full\s*set|scatola\s+e\s+garanzia|box\s*(&|and|e)\s*papers|corredo\s+completo|"
                r"garanzia\s+(originale|italiana)|completo\s+di\s+(scatola|garanzia)",
    "damaged": r"crep[ao]|ammacc|scheggi|vetro\s+(rotto|graffiato)|mancant|manca\s|strappo|"
               r"macchi[ae]|scolorit",
    "scratches": r"graff|segni\s+d.uso|usura|scratch",
    "lot": r"\blotto\b|\blot\s+of\b|\bstock\b|\bpacchetto\s+di\b",
    "unverified": r"non\s+so\s+se\s+(sia\s+)?(vero|originale)|da\s+verificare|non\s+garantit|"
                  r"come\s+da\s+foto|venduto\s+come\s+visto",
}
FLAG_RE = {k: re.compile(p, re.I) for k, p in FLAG_PATTERNS.items()}

# ricambi e accessori venduti da soli: non sono l'oggetto (quadrante, cinturino, scatola vuota...)
PARTS_RE = re.compile(
    r"^\W*(quadrante|pulsant[ei]|lancette|corona|cinturino|bracciale\s+(per|di)\s+rolex|maglie|maglia|fibbia|"
    r"chiusura|deployante|scatola|box|cofanetto|garanzia|vetro|ghiera|lunetta|inserto|movimento|calibro|cassa\s+vuota|"
    r"dial|strap|bezel|crown|hands|links?|buckle|clasp|parts|papers|zifferblatt|armband\s+f[uü]r|band|"
    r"wijzerplaat|horlogeband|doos|kast)\b"
    r"|\b(solo|only|nur)\s+(scatola|box|garanzia|papers|quadrante|dial)\b"
    r"|\bper\s+rolex\b|\bfor\s+rolex\b|\bf[uü]r\s+rolex\b|\bvoor\s+rolex\b",
    re.I,
)
# materiale della cassa: oro pieno, acciaio-oro, diamanti; i comparabili devono coincidere
MAT_GOLD_RE = re.compile(r"\b(oro|gold|goud)\b(?!\s*(plated|placcat|filled))|\b(18\s*k|18\s*kt|750)\b|"
                         r"everose|rose\s*gold|white\s*gold|yellow\s*gold|platino|platinum|gelbgold|weißgold|witgoud", re.I)
MAT_BICOLOR_RE = re.compile(r"bicolor|bi-?colou?r|two[\s-]?tone|acciaio\s+e\s+oro|staal\s*/?\s*goud|stahl\s*/?\s*gold|"
                            r"rolesor|steel\s*(and|&|/)\s*gold|oro\s*/\s*acciaio", re.I)
MAT_DIAMOND_RE = re.compile(r"diamant|diamond|brillant|pav[eé]", re.I)


def material(title: str) -> str:
    """Classe di materiale dal titolo: diamanti > bicolore > oro > acciaio."""
    if MAT_DIAMOND_RE.search(title):
        return "diamonds"
    if MAT_BICOLOR_RE.search(title):
        return "bicolor"
    if MAT_GOLD_RE.search(title):
        return "gold"
    return "steel"

GRAMS_RE = re.compile(r"(\d{1,4}(?:[.,]\d{1,3})?)\s*(?:g\b|gr\b|grs\b|gramm[io]?\b|grams?\b|gramm\b)", re.I)
WEIGHT_WORD_RE = re.compile(r"(?:peso|pesa|weight|gewicht|poids|grammi|gr\.?)\s*(?:totale|lordo|netto|di|ca\.?|circa|:)?\s*"
                            r"(?:(?:complessivo|totale)\s*)?(?:g\b\.?|gr\b\.?|grammi\b)?\s*:?\s*"
                            r"(\d{1,4}(?:[.,]\d{1,3})?)", re.I)
FINENESS_NUMBERS = {375, 585, 750, 800, 835, 900, 916, 925, 999}
KARAT_RE = re.compile(r"\b(9|14|18|22|24)\s*(?:k|kt|ct|carati|karat)\b|\b(375|585|750|916|999)\b", re.I)
SILVER_FINENESS_RE = re.compile(r"\b(800|835|900|925|999)\b")
ROLEX_REF_RE = re.compile(r"\b(1[0-9]{5}[A-Z]{0,4}|[1-9][0-9]{3,4}(?:[A-Z]{1,3})?)\b")
GENERIC_REF_RE = re.compile(r"\b(?:ref\.?|referenza|reference|réf\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9.\-/]{3,15})", re.I)
GRADE_RE = re.compile(r"\b(PSA|BGS|CGC|SGC)\s*(10|9\.5|9|8\.5|8|7)\b", re.I)

STOPWORDS = set(
    "vendo vende vendesi cerco nuovo nuova usato usata ottimo ottime ottima perfetto perfetta condizioni "
    "originale originali con senza per da di del della dei delle il lo la le gli un una uno e ed a in su "
    "affare occasione prezzo trattabile spedizione gratis gratuita regalo idea new used the and of with for "
    "mint uomo donna man woman lady men damen herren vintage".split()
)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", t).strip()


def _num(s: str) -> float:
    return float(s.replace(",", "."))


# oro fino dichiarato (Affide: "(#of16,97g)"; periti: "oro fino g 16,97", "16,97 g di oro fino")
FINE_GOLD_RE = re.compile(r"#of\s*(\d+(?:[.,]\d+)?)\s*g|oro\s+fino\s*(?:di\s*)?(?:g|gr|grammi)\.?\s*(\d+(?:[.,]\d+)?)|"
                          r"(\d+(?:[.,]\d+)?)\s*(?:g|gr|grammi)\.?\s+(?:di\s+)?oro\s+fino", re.I)


def extract(listing: Listing) -> Attributes:
    title = _norm(listing.title)
    full = f"{title} {_norm(listing.description)[:3000]}"
    a = Attributes()

    for name, rx in FLAG_RE.items():
        if rx.search(full):
            a.flags.add(name)
    if PARTS_RE.search(title):
        a.flags.add("part")
    if FAKE_RE.search(full):
        a.flags.add("fake_risk")
    plated = bool(PLATED_RE.search(full))
    if plated:
        a.flags.add("plated")

    if m := GRADE_RE.search(full):
        a.grade = f"{m.group(1).upper()} {m.group(2)}"

    # karat / titolo
    karat = None
    for m in KARAT_RE.finditer(full):
        if m.group(1):
            karat = int(m.group(1))
        else:
            karat = {375: 9, 585: 14, 750: 18, 916: 22, 999: 24}[int(m.group(2))]
        break
    a.grams = grams = _grams(full, german=listing.country in ("DE", "AT", "CH"))
    if WANTED_RE.search(title):
        a.flags.add("wanted")
    if len({m.group(0).lower()[:5] for m in COIN_NAMES_RE.finditer(title)}) >= 3:
        a.flags.add("keyword_spam")

    # 1) monete/lingotti da investimento
    jewel = JEWELRY_WORDS.search(title) and not (COIN_WORDS.search(title) or BAR_WORDS.search(title))
    # più monete nel titolo ("sterline e marenghi"): si valuta la meno pregiata, per prudenza
    in_title = sorted((b for b in BULLION_RE if b[1].search(title)), key=lambda b: (b[2] or 0) * 1000 + (b[3] or 0))
    for name, rx, gold_g, silver_g in ([] if jewel else in_title[:1] or BULLION_RE):
        if rx.search(title) or (rx.search(full) and (COIN_WORDS.search(full) or FRACTION_RE.search(full))):
            a.category = Category.BULLION_COIN
            a.bullion_name = name
            a.fine_gold_g = gold_g or None
            a.fine_silver_g = silver_g or None
            qty = _quantity(title)
            if qty > 1:
                a.fine_gold_g = (a.fine_gold_g or 0) * qty or None
                a.fine_silver_g = (a.fine_silver_g or 0) * qty or None
                a.flags.add("lot")
            a.query_text = name
            break
    if a.category == Category.BULLION_COIN and re.search(r"platin|platinum|platino|palladi", title, re.I) \
            and not GOLD_COIN_RE.search(title):
        # platino/palladio: niente prezzo spot affidabile qui, si valuta come moneta da collezione
        a.category, a.fine_gold_g, a.fine_silver_g, a.bullion_name = Category.COIN, None, None, None
        a.query_text = ""
    if a.category == Category.BULLION_COIN:
        _fix_bullion(a, title, karat, full)
    if a.category == Category.OTHER and BAR_WORDS.search(title) and GOLD_WORDS.search(title) and grams and not plated:
        a.category = Category.BULLION_COIN
        a.bullion_name = f"lingotto oro {grams:g} g"
        a.fine_gold_g = grams * (KARAT_FINENESS[karat] if karat and karat < 24 else 0.9999)
        a.query_text = f"lingotto oro {grams:g} g"

    # 2) carte
    if a.category == Category.OTHER and CARD_WORDS.search(title):
        a.category = Category.CARD

    # 3) orologi
    watch_brand = None
    for brand, rxs, tier in WATCH_BRAND_RE:
        if any(r.search(title) for r in rxs):
            watch_brand = (brand, tier)
            break
    if a.category == Category.OTHER and (watch_brand and (WATCH_WORDS.search(full) or not JEWELRY_WORDS.search(title))
                                         or (WATCH_WORDS.search(title) and not CARD_WORDS.search(title))):
        a.category = Category.WATCH
        if watch_brand:
            a.brand = watch_brand[0]
            flat = re.sub(r"[\s\-]+", "", title.lower())
            for model in WATCH_MODELS.get(a.brand, []):  # in ordine di priorità
                if re.sub(r"[\s\-]+", "", model) in flat:
                    a.model = model
                    break
        a.reference = _reference(full, a.brand)
        if "full_set" in a.flags:
            a.full_set = True
        elif "no_box" in a.flags:
            a.full_set = False

    # 4) borse
    if a.category == Category.OTHER:
        for brand, (pat, models) in BAG_BRANDS.items():
            if re.search(pat, title, re.I) and (BAG_WORDS.search(full) or any(m in title.lower() for m in models)):
                a.category = Category.BAG
                a.brand = brand
                a.model = next((m for m in models if m in title.lower()), None)
                break

    # 5) gioielli firmati / oro a peso
    if a.category == Category.OTHER and (JEWELRY_WORDS.search(title) or GOLD_WORDS.search(title)):
        jbrand = next((b for b, p in JEWELRY_BRANDS.items() if re.search(p, title, re.I)), None)
        is_gold = bool(GOLD_WORDS.search(full) or karat) and not plated
        if jbrand:
            a.category = Category.JEWELRY
            a.brand = jbrand
            low = title.lower()
            a.model = next((m for m in JEWELRY_MODELS.get(jbrand, []) if m in low), None)
        elif is_gold and (karat or grams):
            a.category = Category.GOLD
        elif JEWELRY_WORDS.search(title):
            a.category = Category.JEWELRY
    if a.category in (Category.GOLD, Category.JEWELRY, Category.WATCH) and not plated:
        if GOLD_WORDS.search(full) or karat:
            a.karat = karat or (18 if GOLD_WORDS.search(full) and "750" in full else None)
        if a.category == Category.WATCH and material(title) not in ("gold", "diamonds"):
            a.karat = None  # acciaio e oro o acciaio con dettagli in oro: niente valore di fusione
    if a.category in (Category.GOLD, Category.JEWELRY) and not plated and (m := FINE_GOLD_RE.search(full)):
        g = _num(next(x for x in m.groups() if x))
        if 0.1 <= g <= 5000:
            a.fine_gold_g = g
    if a.category in (Category.GOLD, Category.JEWELRY) and SILVER_WORDS.search(full) and not a.karat:
        if m := SILVER_FINENESS_RE.search(full):
            a.silver_fineness = int(m.group(1))

    # 6) altro
    if a.category == Category.OTHER:
        if COIN_WORDS.search(title):
            a.category = Category.COIN
        elif WINE_WORDS.search(title):
            a.category = Category.WINE
        elif ART_WORDS.search(title):
            a.category = Category.ART
        elif listing.category_hint:
            a.category = listing.category_hint

    if not a.query_text:
        a.query_text = build_query(title, a)
    return a


def _grams(text: str, german: bool = False) -> float | None:
    """Peso in grammi, evitando di scambiare il titolo (750, 925...) per un peso.
    In tedesco "Gr. 56" è la misura dell'anello (Größe), non il peso."""
    cands: list[float] = []
    for m in WEIGHT_WORD_RE.finditer(text):
        if german and re.match(r"gr\b", m.group(0), re.I):
            continue
        g = _num(m.group(1))
        if g.is_integer() and 1900 <= g <= 2035:
            continue  # un anno, non un peso
        cands.append(g)
    for m in GRAMS_RE.finditer(text):
        g = _num(m.group(1))
        if g.is_integer() and 1900 <= g <= 2035:
            continue  # "2020 Gr. S": anno e misura, non 2 chili
        cands.append(g)
    for g in cands:
        if 0.2 <= g <= 5000 and g not in FINENESS_NUMBERS:
            return g
    return None


def _fix_bullion(a: Attributes, title: str, karat: int | None, full: str = "") -> None:
    """Corregge argento al posto dell'oro, frazioni d'oncia e pesi dichiarati più piccoli della moneta."""
    if a.fine_gold_g and SILVER_COIN_RE.search(title) and not GOLD_COIN_RE.search(title):
        # Krugerrand/Eagle/Maple/Philharmoniker d'argento: 1 oncia d'argento ciascuno
        a.fine_silver_g, a.fine_gold_g = a.fine_gold_g, None
        a.bullion_name = f"{a.bullion_name} (argento)".replace("oro ", "")
        a.query_text = a.bullion_name
    per_unit = (a.fine_gold_g or a.fine_silver_g or 0) / max(_quantity(title), 1)
    frac = None
    m = FRACTION_RE.search(title)
    if m and m.group(1) and per_unit < 25 and a.fine_gold_g:
        # "Sovereign 1/200 Oz": vale la frazione d'oncia dichiarata, non la moneta di cui porta il nome
        a.fine_gold_g = round(31.1035 / int(m.group(1)) * max(_quantity(title), 1), 4)
        a.bullion_name = f"oro 1/{m.group(1)} oz"
        a.query_text = a.bullion_name
        return
    if per_unit >= 25 and (m := m or FRACTION_RE.search(full)):
        frac = 1 / int(m.group(1)) if m.group(1) else 0.5
    elif per_unit >= 25 and a.grams and a.grams < 0.8 * per_unit:
        # peso dichiarato più piccolo dell'oncia: vale il peso (con il titolo, se indicato)
        frac = a.grams * (KARAT_FINENESS[karat] if karat else 0.9999) / per_unit
    elif a.fine_silver_g and a.grams and a.grams > per_unit * 1.5:
        a.fine_silver_g = a.grams  # "622 Gramm Feinsilber", "3 Oz 93,3 g": vale il peso totale dichiarato
    if frac:
        if a.fine_gold_g:
            a.fine_gold_g = round(a.fine_gold_g * frac, 4)
        if a.fine_silver_g:
            a.fine_silver_g = round(a.fine_silver_g * frac, 4)
        a.bullion_name = f"{a.bullion_name} x{frac:.3g}"
        a.query_text = a.bullion_name


def _quantity(title: str) -> int:
    # niente numeri attaccati a punti o cifre ("Auflage 17.242 St", "2.034 Stück" sono tirature, non quantità)
    m = re.search(r"(?<![\d.,])\b(\d{1,3})\s*(x|pz|pezzi|monete|sterline|marenghi|st\.?|stk\.?|stück|pcs)(?=\W|$)",
                  title, re.I)
    if m and re.search(r"auflage|tiratura|mintage|limit", title[max(0, m.start() - 25):m.start()], re.I):
        return 1
    if m:
        n = int(m.group(1))
        return n if 1 < n <= 500 else 1
    return 1


def _reference(text: str, brand: str | None) -> str | None:
    if m := GENERIC_REF_RE.search(text):
        return m.group(1).upper().rstrip(".-/")
    if brand in ("Rolex", "Tudor"):
        for m in ROLEX_REF_RE.finditer(text):
            ref = m.group(1)
            if not re.fullmatch(r"(19|20)\d\d", ref):  # scarta gli anni
                return ref
    return None


def build_query(title: str, a: Attributes) -> str:
    """Testo di ricerca per i comparabili: marca + modello + referenza, altrimenti titolo pulito."""
    parts: list[str] = []
    if a.brand:
        parts.append(a.brand)
    if a.model:
        parts.append(a.model)
    if a.reference:
        parts.append(a.reference)
    if a.grade:
        parts.append(a.grade)
    if len(parts) >= 2 or (a.brand and a.category == Category.BAG):
        return " ".join(parts)
    tokens = [t for t in re.findall(r"[\wÀ-ÿ'.\-]+", title.lower()) if t not in STOPWORDS and len(t) > 1]
    seen: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.append(t)
    base = " ".join(seen[:7])
    return (" ".join(parts) + " " + base).strip() if parts else base


def similarity(a: str, b: str) -> float:
    """Somiglianza tra titoli (Jaccard sui token significativi)."""
    ta = {t for t in re.findall(r"[\wÀ-ÿ]+", a.lower()) if t not in STOPWORDS and len(t) > 1}
    tb = {t for t in re.findall(r"[\wÀ-ÿ]+", b.lower()) if t not in STOPWORDS and len(t) > 1}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
