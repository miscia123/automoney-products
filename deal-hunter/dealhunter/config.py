"""Configurazione: valori predefiniti + config.yaml dell'utente + variabili d'ambiente."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

# Profilo di ogni sorgente: commissioni lato acquirente, spedizione tipica,
# paese, rischio di base della piattaforma (0..100) e tutele per chi compra.
SOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "affide": dict(premium=0.25, fixed_fee=0, vat_on_premium=True, shipping=20, country="IT",
                   base_risk=8, seller="institution",
                   protection="lotti periziati da banco su pegno; ritiro in filiale o spedizione"),
    "catawiki": dict(premium=0.09, fixed_fee=3.0, vat_on_premium=False, shipping=25, country="EU",
                     base_risk=18, seller="mixed",
                     protection="pagamento trattenuto fino a 3 giorni dopo la consegna; esperti filtrano i lotti"),
    "ebay": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=12, country="EU",
                 base_risk=22, seller="mixed", protection="Garanzia cliente eBay; Authenticity Guarantee su orologi e borse"),
    "subito": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=10, country="IT",
                   base_risk=35, seller="private",
                   protection="tutela solo con TuttoSubito; in mano: verifica dal vivo"),
    "vinted": dict(premium=0.05, fixed_fee=0.70, vat_on_premium=False, shipping=6, country="EU",
                   base_risk=32, seller="private", protection="Protezione acquisti Vinted"),
    "wallapop": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=8, country="IT",
                     base_risk=40, seller="private", protection="Wallapop Protect solo con spedizione"),
    "zoll": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=25, country="DE",
                 base_risk=20, seller="institution",
                 protection="ente pubblico tedesco: nessuna garanzia ma niente truffe; spedizione all'estero"
                            " solo se prevista nell'asta, altrimenti ritiro in Germania"),
    "buyee": dict(premium=0.0, fixed_fee=3.0, vat_on_premium=False, shipping=35, country="JP",
                  base_risk=28, seller="mixed", protection="proxy affidabile; venditore giapponese spesso preciso"),
    "judicial": dict(premium=0.15, fixed_fee=0, vat_on_premium=True, shipping=40, country="IT",
                     base_risk=25, seller="institution",
                     protection="vendita giudiziaria: nessuna garanzia per vizi (art. 2922 c.c.)"),
    "liveauctioneers": dict(premium=0.28, fixed_fee=0, vat_on_premium=False, shipping=60, country="US",
                            base_risk=25, seller="business", protection="casa d'asta; leggi le condizioni"),
    "kleinanzeigen": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=25, country="DE",
                          base_risk=38, seller="private",
                          protection="nessuna tutela fuori dalla Germania: PayPal Beni e servizi o ritiro di persona"),
    "marktplaats": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=25, country="NL",
                        base_risk=38, seller="private", protection="tra privati: PayPal Beni e servizi o ritiro"),
    "willhaben": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=20, country="AT",
                      base_risk=38, seller="private", protection="PayLivery solo in Austria"),
    "ricardo": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=30, country="CH",
                    base_risk=28, seller="mixed",
                    protection="molti venditori spediscono solo in Svizzera; all'import IVA 22%"),
    "watchexchange": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=60, country="US",
                          base_risk=30, seller="private",
                          protection="comunità con storico delle transazioni; PayPal Beni e servizi"),
    "watchcollecting": dict(premium=0.10, fixed_fee=0, vat_on_premium=True, shipping=40, country="GB",
                            base_risk=18, seller="mixed",
                            protection="casa d'aste curata; commissione 10% + IVA (minimo alto sui lotti economici)"),
    "forum": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=15, country="IT",
                  base_risk=28, seller="private",
                  protection="reputazione del forum; chiedi feedback e paga tracciato"),
    "chrono24": dict(premium=0.0, fixed_fee=0, vat_on_premium=False, shipping=0, country="EU",
                     base_risk=18, seller="mixed", protection="pagamento fiduciario Chrono24"),
}

# Canale di rivendita per categoria: (nome, quota netta incassata sul valore di mercato,
# costo fisso di vendita in EUR). Il "veloce" è la via d'uscita in pochi giorni.
EXIT_CHANNELS: dict[str, dict[str, tuple[str, float, float]]] = {
    "watch": {"main": ("Chrono24/Catawiki (privato)", 0.91, 30), "fast": ("commerciante di orologi", 0.82, 0)},
    "gold": {"main": ("compro oro / banco metalli", 1.0, 0), "fast": ("compro oro", 1.0, 0)},
    "bullion_coin": {"main": ("banco metalli / numismatico", 1.0, 0), "fast": ("banco metalli", 1.0, 0)},
    "jewelry": {"main": ("Catawiki / asta gioielli", 0.85, 15), "fast": ("gioielliere dell'usato", 0.70, 0)},
    "coin": {"main": ("Catawiki / asta numismatica", 0.85, 10), "fast": ("commerciante numismatico", 0.75, 0)},
    "card": {"main": ("Cardmarket", 0.93, 3), "fast": ("negozio di carte", 0.70, 0)},
    "bag": {"main": ("Vestiaire Collective", 0.84, 15), "fast": ("rivenditore di lusso usato", 0.68, 0)},
    "art": {"main": ("casa d'aste", 0.78, 50), "fast": ("galleria / mercante", 0.60, 0)},
    "wine": {"main": ("iDealwine / Catawiki", 0.82, 20), "fast": ("enoteca", 0.65, 0)},
    "collectible": {"main": ("eBay", 0.88, 10), "fast": ("commerciante", 0.65, 0)},
    "other": {"main": ("eBay / Subito", 0.88, 10), "fast": ("commerciante", 0.60, 0)},
}

# Quota del valore metallo pagata in uscita (compro oro / banco metalli)
MELT_EXIT = {"gold": 0.92, "bullion_coin": 0.98}

# Dazi all'importazione da paesi extra UE (indicativi) e IVA all'import in Italia
IMPORT_DUTY = {"watch": 0.045, "jewelry": 0.04, "gold": 0.025, "bag": 0.03, "card": 0.0, "coin": 0.0,
               "bullion_coin": 0.0, "art": 0.0, "wine": 0.0, "collectible": 0.0, "other": 0.03}
IMPORT_VAT = {"bullion_coin": 0.0, "art": 0.10, "coin": 0.10, "default": 0.22}
IMPORT_HANDLING_EUR = 15.0  # sdoganamento del corriere

DEFAULTS: dict[str, Any] = {
    "db_path": "data/dealhunter.sqlite",
    "watchlist": "config/watchlist.yaml",
    "log_level": "INFO",
    "proxy": None,
    "market": {},  # override: gold_eur_g, silver_eur_g
    "comps": {
        "ttl_hours": 24,
        "ebay_domains": ["ebay.it", "ebay.de"],
        "min_similarity": 0.3,
        "max_per_query": 40,
        "chrono24": True,  # prezzi richiesti come riferimento (se la sorgente chrono24 è attiva)
        "own_history": True,  # esiti delle aste chiuse riletti dal bot
        "watchcollecting": True,  # archivio venduti di watchcollecting.com (orologi)
    },
    "evaluation": {
        "reeval_hours": 12,
        "auction_alert_window_hours": 24,  # alert sulle aste solo se finiscono entro N ore
        "target_margin": 0.20,  # margine voluto per calcolare l'offerta massima
        "require_comps_for_alert": True,
    },
    "levels": {
        "hot": {"min_profit": 400, "min_roi": 0.30, "max_risk": 45, "min_confidence": 0.55},
        "good": {"min_profit": 150, "min_roi": 0.20, "max_risk": 60, "min_confidence": 0.40},
        "watch": {"min_profit": 60, "min_roi": 0.12, "max_risk": 75, "min_confidence": 0.25},
    },
    "alerts": {
        "telegram": {"enabled": True, "levels": ["hot", "good"]},
        "email": {"enabled": False, "levels": ["hot"]},
        "digest_hour": 20,
        "quiet_hours": [],  # es. [0, 7]: solo i "hot" di notte
    },
    "llm": {
        "enabled": "auto",  # auto = attivo se c'è ANTHROPIC_API_KEY
        "model": "claude-opus-5-5",
        "effort": "medium",
        "min_level": "good",
        "max_calls_per_run": 25,
        "send_images": True,
    },
    "sources": {
        # intervalli in minuti; i marketplace di privati sono i più "veloci"
        "subito": {"enabled": True, "interval": 10},
        "vinted": {"enabled": True, "interval": 15},
        "ebay": {"enabled": True, "interval": 15},
        "catawiki": {"enabled": True, "interval": 30, "scan": True, "scan_categories": ["333"]},
        "affide": {"enabled": True, "interval": 180},
        "zoll": {"enabled": True, "interval": 60},
        "buyee": {"enabled": True, "interval": 60},
        "judicial": {"enabled": True, "interval": 360},
        "liveauctioneers": {"enabled": False, "interval": 240},
        "chrono24": {"enabled": False, "interval": 60},  # richiede Playwright (Cloudflare)
        "wallapop": {"enabled": False, "interval": 20},
        "kleinanzeigen": {"enabled": True, "interval": 20},
        "marktplaats": {"enabled": True, "interval": 30},
        "willhaben": {"enabled": True, "interval": 30},
        "ricardo": {"enabled": False, "interval": 60},
        "watchexchange": {"enabled": True, "interval": 15},
        "watchcollecting": {"enabled": True, "interval": 60},
        "orologipassioni": {"enabled": True, "interval": 60},
    },
    "http": {
        "default": {"concurrency": 2, "min_interval": 1.5},
        "hosts": {
            "ebay.it": {"concurrency": 2, "min_interval": 2.0},
            "ebay.de": {"concurrency": 2, "min_interval": 2.0},
            "subito.it": {"concurrency": 2, "min_interval": 1.5},
            "vinted.it": {"concurrency": 1, "min_interval": 3.0},
            "catawiki.com": {"concurrency": 2, "min_interval": 2.0},
            "zoll-auktion.de": {"concurrency": 2, "min_interval": 1.5},
            "buyee.jp": {"concurrency": 2, "min_interval": 2.0},
            "chrono24.it": {"concurrency": 1, "min_interval": 3.5},
            "kleinanzeigen.de": {"concurrency": 1, "min_interval": 5.0},
            "reddit.com": {"concurrency": 1, "min_interval": 30.0},
            "marktplaats.nl": {"concurrency": 1, "min_interval": 2.0},
            "2dehands.be": {"concurrency": 1, "min_interval": 2.0},
            "willhaben.at": {"concurrency": 1, "min_interval": 2.5},
            "ricardo.ch": {"concurrency": 1, "min_interval": 2.0},
            "forumfree.it": {"concurrency": 1, "min_interval": 2.0},
        },
    },
}


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path or os.environ.get("DEALHUNTER_CONFIG", "config/config.yaml"))
    if p.exists():
        cfg = deep_merge(cfg, yaml.safe_load(p.read_text()) or {})
    cfg["secrets"] = {
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
        "smtp_url": os.environ.get("DEALHUNTER_SMTP_URL"),  # smtps://user:pass@host:465
        "email_to": os.environ.get("DEALHUNTER_EMAIL_TO"),
        "ebay_client_id": os.environ.get("EBAY_CLIENT_ID"),
        "ebay_client_secret": os.environ.get("EBAY_CLIENT_SECRET"),
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }
    if os.environ.get("DEALHUNTER_PROXY"):
        cfg["proxy"] = os.environ["DEALHUNTER_PROXY"]
    return cfg


def load_watchlist(cfg: dict) -> dict[str, Any]:
    p = Path(cfg["watchlist"])
    if not p.exists():
        raise FileNotFoundError(f"watchlist non trovata: {p}")
    return yaml.safe_load(p.read_text()) or {}
