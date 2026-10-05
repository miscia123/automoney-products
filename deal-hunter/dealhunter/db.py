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
CREATE TABLE IF NOT EXISTS sold_history (
  key TEXT PRIMARY KEY,
  source TEXT,
  category TEXT,
  title TEXT,
  price_eur REAL,
  sold_at REAL,
  url TEXT
);
CREATE INDEX IF NOT EXISTS ix_sold_cat ON sold_history(category, sold_at);
CREATE TABLE IF NOT EXISTS deal_status (
  key TEXT PRIMARY KEY,
  status TEXT,
  note TEXT,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS coverage (
  source TEXT,
  run_at REAL,
  duration_s REAL,
  queries INTEGER,
  query_errors INTEGER,
  listings INTEGER,
  new_listings INTEGER,
  capped_queries INTEGER,
  gap_s REAL,
  ok INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS ix_coverage ON coverage(source, run_at);
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
        for col in ("ends_at REAL", "harvested INTEGER", "category TEXT"):  # migrazione dei db vecchi
            try:
                self.conn.execute(f"ALTER TABLE listings ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass

    # --- annunci -----------------------------------------------------------
    def needs_eval(self, listing: Listing, reeval_after_s: float) -> bool:
        """Valuta solo annunci nuovi, con prezzo cambiato o la cui valutazione è scaduta."""
        row = self.conn.execute(
            "SELECT price, last_eval FROM listings WHERE key=?", (listing.key,)
        ).fetchone()
        now = time.time()
        ends = listing.ends_at.timestamp() if listing.ends_at else None
        if row is None:
            self.conn.execute(
                "INSERT INTO listings(key, source, title, url, price, currency, first_seen, last_seen, ends_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (listing.key, listing.source, listing.title, listing.url, listing.price,
                 listing.currency, now, now, ends),
            )
            self.conn.commit()
            return True
        self.conn.execute(
            "UPDATE listings SET last_seen=?, price=?, title=?, ends_at=COALESCE(?, ends_at) WHERE key=?",
            (now, listing.price, listing.title, ends, listing.key),
        )
        self.conn.commit()
        if row["price"] != listing.price:
            return True
        return row["last_eval"] is None or now - row["last_eval"] > reeval_after_s

    def save_eval(self, deal: Deal) -> None:
        self.conn.execute(
            "UPDATE listings SET last_eval=?, last_level=?, deal_json=?, category=? WHERE key=?",
            (time.time(), deal.level, json.dumps(deal_summary(deal), default=str), deal.attrs.category.value,
             deal.listing.key),
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

    def seen(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM listings WHERE key=?", (key,)).fetchone() is not None

    def has_any(self, source: str) -> bool:
        return self.conn.execute("SELECT 1 FROM listings WHERE source=? LIMIT 1", (source,)).fetchone() is not None

    def save_unvalued(self, listing: Listing, category: str, reason: str, retry: bool) -> None:
        """Prezioso senza prezzo di riferimento: non va perso, finisce in "da verificare a mano"."""
        summary = {"key": listing.key, "source": listing.source, "title": listing.title, "url": listing.url,
                   "price": listing.price, "currency": listing.currency, "kind": listing.kind.value,
                   "ends_at": listing.ends_at.isoformat() if listing.ends_at else None, "category": category,
                   "level": "unvalued", "reason": reason, "images": listing.images[:1], "country": listing.country,
                   "location": listing.location, "score": 0}
        self.conn.execute(
            "UPDATE listings SET last_eval=?, last_level='unvalued', deal_json=?, category=? WHERE key=?",
            (None if retry else time.time(), json.dumps(summary), category, listing.key))
        self.conn.commit()

    def mark_skipped(self, listing: Listing) -> None:
        self.conn.execute("UPDATE listings SET last_eval=?, last_level='skip' WHERE key=?",
                          (time.time(), listing.key))
        self.conn.commit()

    def set_status(self, key: str, status: str, note: str | None = None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO deal_status(key, status, note, updated_at) VALUES (?,?,?,?)",
                          (key, status, note, time.time()))
        self.conn.commit()

    def deals(self, since_s: float, levels: tuple[str, ...] = ("hot", "good", "watch"), limit: int = 500) -> list[dict]:
        q = ("SELECT l.deal_json, l.first_seen, l.last_seen, s.status, s.note FROM listings l"
             " LEFT JOIN deal_status s ON s.key = l.key"
             " WHERE l.last_seen > ? AND l.last_level IN (%s) AND l.deal_json IS NOT NULL" % ",".join("?" * len(levels)))
        out = []
        for r in self.conn.execute(q, (time.time() - since_s, *levels)):
            d = json.loads(r["deal_json"])
            d["first_seen"], d["last_seen"] = r["first_seen"], r["last_seen"]
            d["status"], d["note"] = r["status"] or "nuovo", r["note"]
            out.append(d)
        out.sort(key=lambda d: d.get("score", 0), reverse=True)
        return out[:limit]

    def counts(self, since_s: float) -> dict:
        t = time.time() - since_s
        row = self.conn.execute(
            "SELECT COUNT(*) n, SUM(first_seen > ?) new, SUM(last_level='hot') hot, SUM(last_level='good') good,"
            " SUM(last_level='watch') watch, SUM(last_level='unvalued') unvalued FROM listings WHERE last_seen > ?",
            (t, t)).fetchone()
        return {k: row[k] or 0 for k in row.keys()}

    def log_coverage(self, source: str, **kw) -> None:
        cols = ["source", "run_at", *kw]
        self.conn.execute(f"INSERT INTO coverage({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          (source, time.time(), *kw.values()))
        self.conn.execute("DELETE FROM coverage WHERE run_at < ?", (time.time() - 30 * 86400,))
        self.conn.commit()

    def coverage(self, source: str, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM coverage WHERE source=? ORDER BY run_at DESC LIMIT ?", (source, limit))]

    def top_deals(self, since_s: float, levels=("hot", "good"), limit: int = 20) -> list[dict]:
        q = (
            "SELECT deal_json FROM listings WHERE last_seen > ? AND last_level IN (%s)"
            % ",".join("?" * len(levels))
        )
        rows = self.conn.execute(q, (time.time() - since_s, *levels)).fetchall()
        deals = [json.loads(r["deal_json"]) for r in rows if r["deal_json"]]
        deals.sort(key=lambda d: d.get("score", 0), reverse=True)
        return deals[:limit]

    # --- esiti delle aste chiuse (storico dei venduti costruito dal bot) ------------
    def pending_results(self, source: str, limit: int = 30, grace_s: float = 900) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT key, title, url, category FROM listings WHERE source=? AND harvested IS NULL"
            " AND category IS NOT NULL AND ends_at IS NOT NULL AND ends_at < ? AND ends_at > ?"
            " ORDER BY ends_at DESC LIMIT ?",
            (source, time.time() - grace_s, time.time() - 14 * 86400, limit),
        ).fetchall()

    def mark_harvested(self, key: str) -> None:
        self.conn.execute("UPDATE listings SET harvested=1 WHERE key=?", (key,))
        self.conn.commit()

    def add_sold(self, key: str, source: str, category: str, title: str, price_eur: float, url: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sold_history(key, source, category, title, price_eur, sold_at, url)"
            " VALUES (?,?,?,?,?,?,?)", (key, source, category, title, price_eur, time.time(), url))
        self.conn.commit()

    def sold_history(self, category: str, days: int = 365, limit: int = 5000) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT title, price_eur, sold_at, url, source FROM sold_history WHERE category=? AND sold_at > ?"
            " ORDER BY sold_at DESC LIMIT ?", (category, time.time() - days * 86400, limit)).fetchall()

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
        "images": l.images[:1],
        "country": l.country,
        "location": l.location,
        "bids": l.bids,
        "predicted_final": l.raw.get("predicted_final"),
        "attrs": {"brand": deal.attrs.brand, "model": deal.attrs.model, "reference": deal.attrs.reference,
                  "karat": deal.attrs.karat, "grams": deal.attrs.grams, "flags": sorted(deal.attrs.flags)},
        "cost": deal.cost.__dict__ | {"total": deal.cost.total},
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
             "kind": c.kind, "sold_at": c.sold_at.isoformat() if c.sold_at else None}
            for c in v.comps[:12]
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
