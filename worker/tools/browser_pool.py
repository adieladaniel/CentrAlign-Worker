"""Process-level browser pool: one Playwright driver and one Chromium, an isolated context per run.

Launching Chromium costs about a second and a few hundred MB, so runs share it: every ``BrowserSession`` takes
its own ``BrowserContext`` (separate cookies, storage, pages and downloads) from the pool and gives it back when
the run ends. Closing a context never touches the other runs' contexts.

* Lazily started: nothing is launched until the first ``acquire_context``.
* Headless and headed use separate browsers (headed adds ``slow_mo`` so a demo is watchable).
* A crashed or closed Chromium is detected on the next ``acquire_context`` and relaunched; contexts of the dead
  browser are simply gone, and their sessions notice and tell the agent the page is gone.
* ``max_contexts`` caps simultaneously open contexts; further callers wait up to ``acquire_timeout_s`` and then
  get a ``BrowserPoolBusy`` error.

Playwright objects belong to the event loop that created them, so the default pool (``get_pool()``) is one per
running loop; in a server or worker process that is a single pool for the whole process.
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any

from playwright.async_api import Browser, BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from worker import metrics

log = logging.getLogger(__name__)

HEADED_SLOW_MO_MS = 150
DEFAULT_MAX_CONTEXTS = 8
DEFAULT_ACQUIRE_TIMEOUT_S = 60.0
CONTEXT_OPTIONS: dict[str, Any] = {"accept_downloads": True, "viewport": {"width": 1280, "height": 800}}
POOL_KEY = "browser_pool"  # ctx.extras key under which a test or embedder can inject its own pool


class BrowserPoolError(Exception):
    """The browser could not be provided (message is meant for the agent / operator)."""


class BrowserPoolBusy(BrowserPoolError):
    """All ``max_contexts`` slots stayed in use for the whole acquire timeout."""


class BrowserPool:
    """Shares one Chromium (per headless mode) between runs; hands out isolated contexts."""

    def __init__(
        self,
        max_contexts: int = DEFAULT_MAX_CONTEXTS,
        *,
        acquire_timeout_s: float = DEFAULT_ACQUIRE_TIMEOUT_S,
    ) -> None:
        if max_contexts < 1:
            raise ValueError("max_contexts must be at least 1")
        self.max_contexts = max_contexts
        self.acquire_timeout_s = acquire_timeout_s
        self.launches = 0  # Chromium launches so far (1 when warm, +1 per crash recovery)
        self._slots = asyncio.Semaphore(max_contexts)
        self._lock = asyncio.Lock()  # serialises driver / browser launches
        self._pw: Any = None
        self._browsers: dict[bool, Browser] = {}  # keyed by headless
        self._open: set[BrowserContext] = set()  # contexts that currently hold a slot
        self._crashed: set[bool] = set()  # modes whose Chromium died; the next launch of that mode is a relaunch

    # ---- introspection
    @property
    def active_contexts(self) -> int:
        """Contexts handed out and not yet released."""
        return len(self._open)

    def browser_alive(self, headless: bool = True) -> bool:
        """Is there a connected Chromium of this mode right now?"""
        browser = self._browsers.get(headless)
        return browser is not None and browser.is_connected()

    # ---- acquire / release
    async def acquire_context(self, headless: bool = True) -> BrowserContext:
        """A fresh isolated context, launching (or relaunching) Chromium if needed."""
        try:
            await asyncio.wait_for(self._slots.acquire(), self.acquire_timeout_s)
        except asyncio.TimeoutError:
            raise BrowserPoolBusy(
                f"All {self.max_contexts} browser slots are in use (BROWSER_MAX_CONTEXTS={self.max_contexts}); "
                f"waited {self.acquire_timeout_s:g}s. Try again when other runs finish."
            ) from None
        try:
            for attempt in (1, 2):
                browser = await self._browser(headless)
                try:
                    context = await browser.new_context(**CONTEXT_OPTIONS)
                    break
                except PlaywrightError:
                    if attempt == 2 or browser.is_connected():
                        raise
                    # Chromium died between the liveness check and new_context: relaunch once.
        except BaseException:
            self._slots.release()
            raise
        self._open.add(context)
        metrics.BROWSER_CONTEXTS.inc()
        return context

    async def release(self, context: BrowserContext | None) -> None:
        """Close ``context`` and free its slot. Idempotent; never raises."""
        if context is None:
            return
        try:
            await context.close()
        except Exception:  # noqa: BLE001 - the browser may be gone already
            pass
        if context in self._open:
            self._open.discard(context)
            metrics.BROWSER_CONTEXTS.dec()
            self._slots.release()

    async def shutdown(self) -> None:
        """Close every browser and stop the driver. The pool can be used again afterwards."""
        async with self._lock:
            browsers, pw = list(self._browsers.values()), self._pw
            self._browsers, self._pw = {}, None
            for browser in browsers:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:  # noqa: BLE001
                    pass
            metrics.BROWSER_CONTEXTS.dec(len(self._open))
            self._open.clear()
            self._slots = asyncio.Semaphore(self.max_contexts)

    # ---- launching
    async def _browser(self, headless: bool) -> Browser:
        browser = self._browsers.get(headless)
        if browser is not None and browser.is_connected():
            return browser
        async with self._lock:
            browser = self._browsers.get(headless)
            if browser is not None and browser.is_connected():  # another caller relaunched while we waited
                return browser
            if browser is not None:
                log.warning("Chromium (%s) disconnected; relaunching", "headless" if headless else "headed")
            self._browsers.pop(headless, None)
            return await self._launch(headless)

    async def _launch(self, headless: bool) -> Browser:
        if self._pw is None:
            self._pw = await async_playwright().start()
        try:
            browser = await self._pw.chromium.launch(headless=headless, slow_mo=0 if headless else HEADED_SLOW_MO_MS)
        except PlaywrightError as exc:
            await self._stop_driver()  # a dead driver would fail every later launch
            if "Executable doesn't exist" in str(exc):
                raise BrowserPoolError("Chromium is not installed. Run: python -m playwright install chromium") from exc
            raise
        except BaseException:
            await self._stop_driver()
            raise
        self.launches += 1
        if headless in self._crashed:
            self._crashed.discard(headless)
            metrics.BROWSER_RELAUNCHES.inc()
        browser.on("disconnected", lambda b, h=headless: self._on_disconnected(h, b))
        self._browsers[headless] = browser
        return browser

    async def _stop_driver(self) -> None:
        pw, self._pw = self._pw, None
        if pw is not None:
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass

    def _on_disconnected(self, headless: bool, browser: Browser) -> None:
        if self._browsers.get(headless) is browser:
            del self._browsers[headless]
            self._crashed.add(headless)


_POOLS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, BrowserPool] = weakref.WeakKeyDictionary()


def get_pool(settings: Any = None) -> BrowserPool:
    """The default pool for the running event loop (created on first use, sized by ``BROWSER_MAX_CONTEXTS``)."""
    loop = asyncio.get_running_loop()
    pool = _POOLS.get(loop)
    if pool is None:
        size = getattr(settings, "BROWSER_MAX_CONTEXTS", DEFAULT_MAX_CONTEXTS)
        timeout = getattr(settings, "BROWSER_ACQUIRE_TIMEOUT_S", DEFAULT_ACQUIRE_TIMEOUT_S)
        pool = _POOLS[loop] = BrowserPool(size, acquire_timeout_s=timeout)
    return pool


async def shutdown_pool() -> None:
    """Shut down the default pool of the running loop (call from the app / worker shutdown hook)."""
    pool = _POOLS.pop(asyncio.get_running_loop(), None)
    if pool is not None:
        await pool.shutdown()
