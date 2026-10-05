"""Stato persistente su SQLite: annunci visti, cache dei comparabili, alert inviati."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import Comparable, Deal, Listing

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS listings (
  key TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  title TEXT,
  url TEXT,
  price REAL,
  currency TEXT,
  first_seen REAL,
  last_seen REAL,
  last_eval REAL,
  last_level TEXT,
  last_alert_price REAL,
  last_alert_at REAL,
  deal_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_listings_level ON listings(last_level, last_seen);
CREATE TABLE IF NOT EXISTS comps_cache (
  qkey TEXT PRIMARY KEY,
  fetched_at REAL,
  comps_json TEXT
);
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS source_health (
  source TEXT PRIMARY KEY,
  last_ok REAL,
  last_error REAL,
  last_error_msg TEXT,
  last_count INTEGER,
  consecutive_errors INTEGER DEFAULT 0
);
"""

LEVEL_RANK = {"none": 0, "watch": 1, "good": 2, "hot": 3}


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # --- annunci -----------------------------------------------------------
    def needs_eval(self, listing: Listing, reeval_after_s: float) -> bool:
        """Valuta solo annunci nuovi, con prezzo cambiato o la cui valutazione è scaduta."""
        row = self.conn.execute(
            "SELECT price, last_eval FROM listings WHERE key=?", (listing.key,)
        ).fetchone()
        now = time.time()
        if row is None:
            self.conn.execute(
                "INSERT INTO listings(key, source, title, url, price, currency, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (listing.key, listing.source, listing.title, listing.url, listing.price,
                 listing.currency, now, now),
            )
            self.conn.commit()
            return True
        self.conn.execute(
            "UPDATE listings SET last_seen=?, price=?, title=? WHERE key=?",
            (now, listing.price, listing.title, listing.key),
        )
        self.conn.commit()
        if row["price"] != listing.price:
            return True
        return row["last_eval"] is None or now - row["last_eval"] > reeval_after_s

    def save_eval(self, deal: Deal) -> None:
        self.conn.execute(
            "UPDATE listings SET last_eval=?, last_level=?, deal_json=? WHERE key=?",
            (time.time(), deal.level, json.dumps(deal_summary(deal), default=str), deal.listing.key),
        )
        self.conn.commit()

    def should_alert(self, deal: Deal, realert_drop: float = 0.10) -> bool:
        if deal.level not in ("hot", "good"):
            return False
        row = self.conn.execute(
            "SELECT last_alert_price, last_alert_at, last_level FROM listings WHERE key=?",
            (deal.listing.key,),
        ).fetchone()
        if row is None or row["last_alert_at"] is None:
            return True
        # nuovo alert solo se il prezzo è sceso abbastanza (per le aste: mai, il prezzo sale)
        prev = row["last_alert_price"] or 0
        return prev > 0 and deal.listing.price <= prev * (1 - realert_drop)

    def mark_alerted(self, deal: Deal) -> None:
        self.conn.execute(
            "UPDATE listings SET last_alert_at=?, last_alert_price=? WHERE key=?",
            (time.time(), deal.listing.price, deal.listing.key),
        )
        self.conn.commit()

    def top_deals(self, since_s: float, levels=("hot", "good"), limit: int = 20) -> list[dict]:
        q = (
            "SELECT deal_json FROM listings WHERE last_seen > ? AND last_level IN (%s)"
            % ",".join("?" * len(levels))
        )
        rows = self.conn.execute(q, (time.time() - since_s, *levels)).fetchall()
        deals = [json.loads(r["deal_json"]) for r in rows if r["deal_json"]]
        deals.sort(key=lambda d: d.get("score", 0), reverse=True)
        return deals[:limit]

    # --- cache comparabili -------------------------------------------------
    def get_comps(self, qkey: str, ttl_s: float) -> list[Comparable] | None:
        row = self.conn.execute(
            "SELECT fetched_at, comps_json FROM comps_cache WHERE qkey=?", (qkey,)
        ).fetchone()
        if row is None or time.time() - row["fetched_at"] > ttl_s:
            return None
        return [Comparable(**c) for c in json.loads(row["comps_json"])]

    def put_comps(self, qkey: str, comps: list[Comparable]) -> None:
        payload = json.dumps(
            [{**c.__dict__, "sold_at": c.sold_at.isoformat() if c.sold_at else None} for c in comps]
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO comps_cache(qkey, fetched_at, comps_json) VALUES (?,?,?)",
            (qkey, time.time(), payload),
        )
        self.conn.commit()

    # --- chiave/valore (prezzi metalli, cambi, token) -------------------------
    def kv_get(self, k: str, ttl_s: float | None = None) -> Any:
        row = self.conn.execute("SELECT v, updated_at FROM kv WHERE k=?", (k,)).fetchone()
        if row is None or (ttl_s is not None and time.time() - row["updated_at"] > ttl_s):
            return None
        return json.loads(row["v"])

    def kv_set(self, k: str, v: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kv(k, v, updated_at) VALUES (?,?,?)",
            (k, json.dumps(v), time.time()),
        )
        self.conn.commit()

    # --- salute delle sorgenti -----------------------------------------------
    def source_ok(self, source: str, count: int) -> None:
        self.conn.execute(
            "INSERT INTO source_health(source, last_ok, last_count, consecutive_errors) VALUES (?,?,?,0)"
            " ON CONFLICT(source) DO UPDATE SET last_ok=excluded.last_ok,"
            " last_count=excluded.last_count, consecutive_errors=0",
            (source, time.time(), count),
        )
        self.conn.commit()

    def source_error(self, source: str, msg: str) -> int:
        self.conn.execute(
            "INSERT INTO source_health(source, last_error, last_error_msg, consecutive_errors)"
            " VALUES (?,?,?,1) ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error,"
            " last_error_msg=excluded.last_error_msg, consecutive_errors=consecutive_errors+1",
            (source, time.time(), msg[:500]),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT consecutive_errors FROM source_health WHERE source=?", (source,)
        ).fetchone()
        return int(row["consecutive_errors"])

    def health(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM source_health ORDER BY source")]


def deal_summary(deal: Deal) -> dict:
    l = deal.listing
    v = deal.valuation
    return {
        "key": l.key,
        "source": l.source,
        "title": l.title,
        "url": l.url,
        "price": l.price,
        "currency": l.currency,
        "kind": l.kind.value,
        "ends_at": l.ends_at.isoformat() if l.ends_at else None,
        "category": deal.attrs.category.value,
        "fair_value": v.fair_value,
        "value_range": [v.low, v.high],
        "confidence": round(v.confidence, 2),
        "method": v.method,
        "melt_value": v.melt_value,
        "n_comps": len(v.comps),
        "comps": [
            {"price_eur": c.price_eur, "title": c.title, "url": c.url, "source": c.source,
             "kind": c.kind}
            for c in v.comps[:5]
        ],
        "landed_cost": deal.cost.total,
        "repair": [deal.repair.low, deal.repair.high, deal.repair.items],
        "exit_channel": deal.exit_channel,
        "exit_net": deal.exit_net,
        "profit": deal.profit,
        "roi": deal.roi,
        "risk": deal.risk,
        "reliability": deal.reliability,
        "risk_reasons": deal.risk_reasons,
        "resale_days": list(deal.resale_days),
        "max_bid": deal.max_bid,
        "score": deal.score,
        "level": deal.level,
        "llm": deal.llm_notes,
    }
