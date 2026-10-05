"""Client HTTP asincrono veloce con impronta TLS da browser, limiti per dominio e retry.

Usa curl_cffi (impersonate="chrome") perché molti marketplace bloccano i client
Python standard in base all'impronta TLS. Ogni dominio ha un proprio limite di
concorrenza e un intervallo minimo tra le richieste, così si possono interrogare
decine di siti in parallelo senza martellarne nessuno.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}


class FetchError(RuntimeError):
    def __init__(self, url: str, status: int | None, msg: str = ""):
        super().__init__(f"{status} {url} {msg}".strip())
        self.url = url
        self.status = status


class BlockedError(FetchError):
    """Il sito ha risposto con una pagina anti-bot (Cloudflare, Datadome, Akamai...)."""


BLOCK_MARKERS = (
    "cf-chl",
    "challenge-platform",
    "Just a moment...",
    "captcha-delivery.com",  # Datadome
    "px-captcha",
    "Access Denied",
)


@dataclass
class HostPolicy:
    concurrency: int = 2
    min_interval: float = 1.0  # secondi tra due richieste allo stesso host


class _HostLimiter:
    def __init__(self, policy: HostPolicy):
        self.sem = asyncio.Semaphore(policy.concurrency)
        self.min_interval = policy.min_interval
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def __aenter__(self):
        await self.sem.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            jitter = random.uniform(0, self.min_interval * 0.3)
            self._next_at = max(now, self._next_at) + self.min_interval + jitter
        if wait > 0:
            await asyncio.sleep(wait)
        return self

    async def __aexit__(self, *exc):
        self.sem.release()


class Http:
    def __init__(
        self,
        policies: dict[str, HostPolicy] | None = None,
        default_policy: HostPolicy | None = None,
        proxy: str | None = None,
        timeout: float = 25.0,
        impersonate: str = "chrome",
    ):
        self.policies = policies or {}
        self.default_policy = default_policy or HostPolicy()
        self._limiters: dict[str, _HostLimiter] = {}
        self.proxy = proxy or os.environ.get("DEALHUNTER_PROXY")
        self.timeout = timeout
        self.impersonate = impersonate
        self._session: AsyncSession | None = None
        # diagnostica: ultima risposta per host (DEALHUNTER_DEBUG_HTTP=1), per capire perché
        # una pagina "andata a buon fine" non ha dato annunci (selettori cambiati, blocchi)
        self.debug = os.environ.get("DEALHUNTER_DEBUG_HTTP") == "1"
        self.last: dict[str, dict] = {}
        # campioni HTML completi per gli host indicati (DEALHUNTER_DUMP_HOSTS=kleinanzeigen,affide):
        # servono a riscrivere un connettore sulla pagina vera
        self.dump_hosts = [h for h in os.environ.get("DEALHUNTER_DUMP_HOSTS", "").split(",") if h]
        self.dumps: dict[str, list[tuple[str, str]]] = {}

    async def __aenter__(self) -> "Http":
        self._session = AsyncSession(
            impersonate=self.impersonate,
            timeout=self.timeout,
            proxy=self.proxy,
            headers=DEFAULT_HEADERS,
        )
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    def _limiter(self, url: str) -> _HostLimiter:
        host = urlparse(url).hostname or ""
        lim = self._limiters.get(host)
        if lim is None:
            policy = self.default_policy
            for suffix, p in self.policies.items():
                if host == suffix or host.endswith("." + suffix):
                    policy = p
                    break
            lim = self._limiters[host] = _HostLimiter(policy)
        return lim

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        retries: int = 3,
        allow_block_retry: bool = True,
    ):
        assert self._session is not None, "usa 'async with Http() as http'"
        last: Exception | None = None
        for attempt in range(retries + 1):
            async with self._limiter(url):
                try:
                    resp = await self._session.request(
                        method, url, params=params, headers=headers, json=json, data=data
                    )
                except Exception as e:  # errori di rete
                    last = FetchError(url, None, repr(e))
                    resp = None
            if resp is not None:
                if resp.status_code in (429, 500, 502, 503, 504):
                    last = FetchError(url, resp.status_code)
                elif resp.status_code in (403, 401) or _looks_blocked(resp):
                    last = BlockedError(url, resp.status_code, "pagina anti-bot")
                    if not allow_block_retry:
                        raise last
                elif resp.status_code >= 400:
                    raise FetchError(url, resp.status_code)
                else:
                    if self.debug:
                        self._remember(url, resp)
                    if self.dump_hosts:
                        self._dump(url, resp)
                    return resp
            if attempt < retries:
                delay = (2**attempt) + random.uniform(0, 1)
                log.debug("retry %s tra %.1fs (%s)", url, delay, last)
                await asyncio.sleep(delay)
        assert last is not None
        raise last

    def _dump(self, url: str, resp) -> None:
        host = urlparse(url).hostname or ""
        key = next((k for k in self.dump_hosts if k in host), None)
        if key and len(self.dumps.setdefault(key, [])) < self.dump_per_host(key):
            self.dumps[key].append((str(resp.url), (resp.text or "")[:600_000]))

    @staticmethod
    def dump_per_host(key: str) -> int:
        return 4

    def _remember(self, url: str, resp) -> None:
        self.last[urlparse(url).hostname or ""] = summarize(str(resp.url), resp.status_code, resp.text or "",
                                                            resp.headers.get("content-type", ""))

    async def get_text(self, url: str, **kw) -> str:
        return (await self.request("GET", url, **kw)).text

    async def get_json(self, url: str, **kw) -> Any:
        return (await self.request("GET", url, **kw)).json()

    async def post_json(self, url: str, payload: Any, **kw) -> Any:
        return (await self.request("POST", url, json=payload, **kw)).json()

    def cookies(self):
        assert self._session is not None
        return self._session.cookies


def summarize(url: str, status: int, text: str, ctype: str) -> dict:
    """Riassunto di una risposta per i log di diagnostica."""
    import re

    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return {
        "url": url[:300], "status": status, "bytes": len(text), "type": ctype,
        "title": (title.group(1).strip()[:120] if title else ""),
        "head": (text[:700] if "json" in ctype or "xml" in ctype else body[:700]),
        "markers": {m: text.count(m) for m in ("aditem", "s-item", "s-card", "itemCard", "js-listing-item",
                                              "__NEXT_DATA__", "/vendita/", "lot-229", "/auktion/", "--id",
                                              "<entry", "?t=") if m in text},
    }


def _looks_blocked(resp) -> bool:
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype:
        return False
    head = resp.text[:4000]
    return any(m in head for m in BLOCK_MARKERS) and len(resp.text) < 60000
