"""Dashboard locale: `dealhunter dashboard` apre http://127.0.0.1:8765 nel browser.

Un solo processo fa tutto: serve la pagina, risponde alle API, lancia le scansioni a
richiesta e (se "Automatico" è acceso) gira le sorgenti in scadenza ogni minuto, come
`dealhunter daemon`. Ascolta solo su 127.0.0.1: dalla rete di casa non è raggiungibile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import webbrowser
from importlib import resources
from typing import Any
from urllib.parse import urlparse

from aiohttp import web

from .db import LEVEL_RANK
from .engine import Engine
from .sources import REGISTRY

log = logging.getLogger(__name__)

STATUSES = {"nuovo", "visto", "scartato", "comprato", "offerta fatta"}
SKELETON = """<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>:root{{color-scheme:light}}body{{margin:0;font:14px system-ui,sans-serif}}img{{max-width:100%}}[hidden]{{display:none!important}}</style>
</head><body>
{body}
</body></html>"""


def page_fragment() -> str:
    return resources.files("dealhunter").joinpath("dashboard.html").read_text(encoding="utf-8")


def coverage_status(src: dict, now: float, auto: bool) -> str:
    """ok | running | late | error | never - lo stato che la dashboard colora."""
    if src.get("progress", {}).get("state") == "running":
        return "running"
    h = src.get("health") or {}
    last_ok = h.get("last_ok")
    if (h.get("consecutive_errors") or 0) > 0 and (h.get("last_error") or 0) >= (last_ok or 0):
        return "error"
    if not last_ok:
        return "never"
    if auto and now - last_ok > 2.5 * src["interval_min"] * 60:
        return "late"
    return "ok"


class Dashboard:
    def __init__(self, engine: Engine, auto: bool = True, demo: bool = False):
        self.engine = engine
        self.auto = auto
        self.demo = demo
        self.scan_tasks: set[asyncio.Task] = set()
        self._auto_task: asyncio.Task | None = None

    # --- dati ----------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        eng, db, now = self.engine, self.engine.db, time.time()
        health = {h["source"]: h for h in db.health()}
        running = getattr(eng, "sources", None)  # None = motore non avviato (esportazione statica)
        sources = []
        for name, scfg in eng.cfg["sources"].items():
            cls = REGISTRY.get(name)
            if cls is None or not scfg.get("enabled"):
                continue
            active = name in running if running is not None else True
            last_run = db.kv_get(f"lastrun:{name}")
            interval = scfg.get("interval", 60)
            item = {
                "name": name, "interval_min": interval, "active": active,
                "kind": "catalogo" if cls.catalog_mode else "ricerche",
                "paged": cls.paged,
                "needs_browser": bool(cls.needs_browser or scfg.get("use_browser")),
                "health": health.get(name), "last_run": last_run,
                "next_due": (last_run or 0) + interval * 60 if last_run else now,
                "progress": eng.progress.get(name, {}),
                "coverage": db.coverage(name, 12),
            }
            item["status"] = coverage_status(item, now, self.auto) if active else "off"
            sources.append(item)
        m = getattr(eng, "market", None)
        metals = db.kv_get("metals") or {}
        fx = (m.fx if m else db.kv_get("fx")) or {}
        return {
            "now": now, "auto": self.auto, "demo": self.demo,
            "scanning": any(not t.done() for t in self.scan_tasks),
            "market": {"gold_eur_g": (m and m.gold_eur_g) or metals.get("gold"),
                       "silver_eur_g": (m and m.silver_eur_g) or metals.get("silver"),
                       "fx": {k: fx.get(k) for k in ("USD", "GBP", "CHF", "JPY")}},
            "counts24": db.counts(86400),
            "sources": sources,
            "levels": eng.cfg["levels"],
            "watchlist": len(eng.watchlist.get("queries", [])),
            "telegram": bool(getattr(eng, "notifier", None) and eng.notifier.telegram_ready)
                        or bool(eng.cfg["secrets"].get("telegram_token")),
            "llm": eng.reviewer is not None,
            "events": (((db.kv_get("demo_events") or []) if self.demo else []) + list(eng.events))[-120:],
        }

    def deals(self, hours: float, levels: tuple[str, ...]) -> list[dict]:
        return self.engine.db.deals(hours * 3600, levels=levels, limit=800)

    # --- azioni -----------------------------------------------------------------------
    def start_scan(self, names: list[str] | None) -> list[str]:
        if self.demo:
            self.engine.events.append({"t": time.time(), "level": "info",
                                       "msg": "modalità demo: le scansioni sono disattivate"})
            return []
        targets = [n for n in (names or list(self.engine.sources)) if n in self.engine.sources]
        task = asyncio.create_task(self.engine.run_once(only=targets, force=True))
        self.scan_tasks.add(task)
        task.add_done_callback(self.scan_tasks.discard)
        return targets

    async def auto_loop(self, tick_s: int = 60) -> None:
        while True:
            if self.auto and not self.demo:
                try:
                    await self.engine.run_once()
                except Exception:
                    log.exception("giro automatico fallito")
            await asyncio.sleep(tick_s)

    # --- app aiohttp -----------------------------------------------------------------
    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._same_origin])
        app.add_routes([
            web.get("/", self.h_index),
            web.get("/api/state", self.h_state),
            web.get("/api/deals", self.h_deals),
            web.post("/api/scan", self.h_scan),
            web.post("/api/auto", self.h_auto),
            web.post("/api/status", self.h_status),
        ])
        return app

    @web.middleware
    async def _same_origin(self, request: web.Request, handler):
        # le azioni accettano solo richieste dalla dashboard stessa (niente siti terzi)
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return web.json_response({"error": "origine non consentita"}, status=403)
        return await handler(request)

    async def h_index(self, _req):
        return web.Response(text=SKELETON.format(body=page_fragment()), content_type="text/html")

    async def h_state(self, _req):
        return web.json_response(self.state(), dumps=_dumps)

    async def h_deals(self, req: web.Request):
        hours = float(req.query.get("hours", 72))
        levels = tuple(x for x in req.query.get("levels", "hot,good,watch").split(",")
                       if x in LEVEL_RANK or x == "unvalued")
        return web.json_response(self.deals(hours, levels or ("hot", "good", "watch")), dumps=_dumps)

    async def h_scan(self, req: web.Request):
        body = await _json(req)
        started = self.start_scan(body.get("sources"))
        return web.json_response({"started": started})

    async def h_auto(self, req: web.Request):
        body = await _json(req)
        self.auto = bool(body.get("on"))
        log.info("scansione automatica %s", "attivata" if self.auto else "sospesa")
        return web.json_response({"auto": self.auto})

    async def h_status(self, req: web.Request):
        body = await _json(req)
        key, status = body.get("key"), body.get("status")
        if not key or status not in STATUSES:
            return web.json_response({"error": f"stato non valido, usa uno di: {', '.join(sorted(STATUSES))}"},
                                     status=400)
        self.engine.db.set_status(key, status, (body.get("note") or "")[:500] or None)
        return web.json_response({"ok": True})


async def _json(req: web.Request) -> dict:
    try:
        data = await req.json()
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _dumps(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


async def serve(cfg: dict, host: str = "127.0.0.1", port: int = 8765, auto: bool = True,
                open_browser: bool = True, demo: bool = False) -> None:
    if demo:
        cfg = {**cfg, "_no_browser": True}
    async with Engine(cfg) as eng:
        dash = Dashboard(eng, auto=auto, demo=demo)
        if not demo:
            try:
                await eng.market.refresh()
            except Exception as e:
                log.warning("quotazioni non disponibili all'avvio: %s", e)
        runner = web.AppRunner(dash.app())
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        url = f"http://{host}:{port}/"
        print(f"DealHunter è su {url}  (Ctrl+C per fermare)")
        if open_browser:
            webbrowser.open(url)
        dash._auto_task = asyncio.create_task(dash.auto_loop())
        try:
            await asyncio.Event().wait()
        finally:
            dash._auto_task.cancel()
            await runner.cleanup()


def export_snapshot(cfg: dict, out_path: str, hours: float = 72, demo: bool = False, note: str | None = None) -> str:
    """Pagina statica con i dati di adesso dentro: si apre senza server (anche dal telefono)."""
    eng = Engine(cfg)
    dash = Dashboard(eng, auto=False, demo=demo)
    snap = dash.state() | {"deals": dash.deals(hours, ("hot", "good", "watch", "unvalued")), "snapshot": True,
                           "note": note}
    payload = _dumps(snap).replace("</", "<\\/")
    html = page_fragment().replace("<!--SNAPSHOT-->", f"<script>window.__DH_SNAPSHOT__ = {payload};</script>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
