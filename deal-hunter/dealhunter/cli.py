"""Riga di comando: dealhunter run | daemon | doctor | report | test-alert."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time

from .config import load_config


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="dealhunter", description="Cacciatore di affari su aste e marketplace")
    ap.add_argument("-c", "--config", help="percorso di config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="un giro sulle sorgenti in scadenza (adatto a cron)")
    r.add_argument("--source", action="append", help="solo questa sorgente (ripetibile)")
    r.add_argument("--force", action="store_true", help="ignora gli intervalli e gira tutto")
    d = sub.add_parser("daemon", help="processo sempre attivo con intervalli per sorgente")
    d.add_argument("--tick", type=int, default=60)
    sub.add_parser("doctor", help="prova ogni sorgente e ogni fonte di prezzi venduti")
    rep = sub.add_parser("report", help="migliori affari recenti")
    rep.add_argument("--hours", type=float, default=24)
    rep.add_argument("--json", action="store_true")
    sub.add_parser("test-alert", help="manda un messaggio di prova su Telegram/email")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(level=logging.DEBUG if args.verbose else getattr(logging, cfg["log_level"]),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_dispatch(args, cfg))


async def _dispatch(args, cfg) -> None:
    from .engine import Engine

    if args.cmd == "report":
        from .db import DB

        deals = DB(cfg["db_path"]).top_deals(args.hours * 3600, levels=("hot", "good", "watch"), limit=30)
        if args.json:
            print(json.dumps(deals, indent=2, ensure_ascii=False))
            return
        for d in deals:
            print(f"[{d['level']:5}] {d['source']:10} {d['price']:>9.0f} {d['currency']}  "
                  f"valore €{(d['fair_value'] or 0):>8.0f}  guadagno €{d['profit']:>7.0f}  ROI {d['roi']:.0%}  "
                  f"affid. {d['reliability']:>3}  {d['resale_days'][0]}-{d['resale_days'][1]}gg  {d['title'][:60]}")
            print(f"        {d['url']}")
        return

    async with Engine(cfg) as eng:
        if args.cmd == "run":
            deals = await eng.run_once(only=args.source, force=args.force or bool(args.source))
            hot = [d for d in deals if d.level in ("hot", "good")]
            print(f"valutati {len(deals)} annunci, {len(hot)} sopra soglia")
        elif args.cmd == "daemon":
            await eng.daemon(args.tick)
        elif args.cmd == "test-alert":
            await eng.notifier.send_text("✅ DealHunter: notifiche configurate correttamente.")
            print("inviato" if eng.notifier.telegram_ready else "Telegram non configurato: messaggio nel log")
        elif args.cmd == "doctor":
            await doctor(eng)


async def doctor(eng) -> None:
    """Verifica dal vivo: quali sorgenti rispondono da questa rete, e con quanti risultati."""
    from .sources import Query
    from .models import Category

    await eng.market.refresh()
    print(f"oro: {eng.market.gold_eur_g and round(eng.market.gold_eur_g, 2)} €/g · "
          f"argento: {eng.market.silver_eur_g and round(eng.market.silver_eur_g, 3)} €/g · "
          f"USD {eng.market.fx.get('USD')} JPY {eng.market.fx.get('JPY')}")
    probe = Query(q="rolex", category=Category.WATCH, translations={"ja": "ロレックス"})
    for name, src in eng.sources.items():
        t0 = time.time()
        try:
            got = await (src.catalog() if src.catalog_mode else src.search(probe))
            sample = got[0].title[:50] if got else "-"
            print(f"  OK   {name:16} {len(got):4} risultati in {time.time() - t0:5.1f}s  es: {sample}")
        except Exception as e:
            print(f"  ERR  {name:16} {type(e).__name__}: {str(e)[:120]}")
    from .sources.ebay import ebay_sold
    from .sources.auctions import liveauctioneers_sold

    for label, coro in (("ebay.it venduti", ebay_sold(eng.http, "rolex datejust", "ebay.it")),
                        ("ebay.de venduti", ebay_sold(eng.http, "rolex datejust", "ebay.de")),
                        ("liveauctioneers venduti", liveauctioneers_sold(eng.http, eng.market, "rolex datejust"))):
        try:
            c = await coro
            print(f"  OK   {label:24} {len(c)} prezzi venduti")
        except Exception as e:
            print(f"  ERR  {label:24} {type(e).__name__}: {str(e)[:120]}")
    print("Telegram:", "configurato" if eng.notifier.telegram_ready else "NON configurato")
    print("Perito AI:", "attivo" if eng.reviewer else "spento (manca ANTHROPIC_API_KEY)")


if __name__ == "__main__":
    main()
