"""Browser reale (Playwright) per i siti che rifiutano i client HTTP: Buyee, Chrono24, Gobid.

È opzionale: `pip install playwright` e un Chromium. I siti con PerimeterX/Cloudflare
rifiutano spesso il browser headless, quindi di default parte in modalità visibile
(su un server senza schermo: `xvfb-run dealhunter daemon`). Il profilo è persistente
così i cookie di "verifica superata" restano tra un giro e l'altro.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random

log = logging.getLogger(__name__)


class BrowserHttp:
    """Stessa interfaccia minima di Http.get_text, ma attraverso un Chromium vero."""

    def __init__(self, profile_dir: str = "data/browser-profile", headless: bool | None = None,
                 warmup: dict[str, str] | None = None, proxy: str | None = None):
        self.profile_dir = profile_dir
        self.headless = headless if headless is not None else os.environ.get("DEALHUNTER_HEADLESS") == "1"
        self.warmup = warmup or {}  # host -> pagina da aprire prima (es. buyee.jp -> home)
        self.proxy = proxy
        self._pw = None
        self._ctx = None
        self._warmed: set[str] = set()
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError("serve Playwright: pip install playwright && playwright install chromium") from e
        self._pw = await async_playwright().start()
        kw = {"headless": self.headless, "locale": "it-IT"}
        exe = os.environ.get("DEALHUNTER_CHROMIUM")
        if exe:
            kw["executable_path"] = exe
        if self.proxy:
            kw["proxy"] = {"server": self.proxy}
        self._ctx = await self._pw.chromium.launch_persistent_context(self.profile_dir, **kw)
        return self

    async def __aexit__(self, *exc):
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()

    async def get_text(self, url: str, params: dict | None = None, **_) -> str:
        from urllib.parse import urlencode, urlparse

        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        host = urlparse(url).hostname or ""
        async with self._lock:  # una pagina alla volta: più lento ma molto meno sospetto
            page = await self._ctx.new_page()
            try:
                if host in self.warmup and host not in self._warmed:
                    await page.goto(self.warmup[host], wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(random.uniform(2, 4))
                    self._warmed.add(host)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(random.uniform(1.5, 3.5))
                return await page.content()
            finally:
                await page.close()
