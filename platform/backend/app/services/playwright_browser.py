"""Shared Playwright/Chromium service for both scraping and form-filling.

Why this exists
---------------
Two distinct surfaces in the codebase need a real browser:

1. **Scraping** ATS / company sites that block plain ``httpx`` —
   Wellfound is the current motivating case (DataDome bot detection
   returns 403 to every header set we've tried). Playwright with a
   real Chromium fingerprint passes most JS-challenge gates that
   defeat header-only HTTP clients.

2. **Job application workflows** (the v6 "Apply Routine" feature) —
   submitting Greenhouse / Lever / Ashby applications often requires
   filling forms, uploading resumes, clicking buttons, and waiting
   for redirects. Each ATS has its own DOM. Maintaining one
   browser-automation primitive set keeps that growth manageable.

Goals
-----
- **Lazy launch**: don't pay the Chromium startup cost (~2s) until
  the first caller actually needs it. Idle workers / API requests
  that don't touch the browser have zero overhead.
- **Process-wide reuse**: launch one ``Browser`` per Python process
  and share it across sessions. Each session opens its own
  ``BrowserContext`` (cookie-isolated) + ``Page``.
- **Stealth-by-default**: realistic UA, viewport, no
  ``navigator.webdriver``. Caller can opt into stronger stealth via
  ``playwright-stealth`` if that package is installed (we don't
  hard-require it — the dependency footprint is large and version-
  flaky against new Chromium releases).
- **Two ergonomic API levels**:
  * ``fetch_html(url, ...)`` — one-call shortcut for the common
    "load page, wait for it, return rendered HTML" pattern.
  * ``BrowserSession`` context manager — for multi-step workflows
    (navigate → fill → click → wait → submit) where the page
    needs to persist between operations.
- **Testable**: every method has a sync seam so unit tests can mock
  the browser without spinning up Chromium.

Operational discipline
----------------------
- Per-session ``BrowserContext`` so cookies / localStorage / auth
  state never leak between unrelated workflows.
- Hard wall-clock timeouts on every navigation + selector wait so a
  hung page can't pin a Celery worker.
- Screenshot-on-failure goes to ``/tmp/playwright-fail-<uuid>.png``
  when ``PLAYWRIGHT_DEBUG_SCREENSHOTS=1`` — admins can grab them
  via SSH when an apply workflow regresses against a new ATS UI.
- Graceful shutdown via FastAPI lifespan so the Chromium process
  closes cleanly on container rotation.

Not in scope
------------
- Solving CAPTCHA (hCaptcha / reCAPTCHA / Turnstile). DataDome
  occasionally throws those; this module logs + returns failure.
- Persistent login state across container restarts. The current
  apply flows are one-shot; if a future workflow needs sticky
  sessions, add ``user_data_dir`` plumbing here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────────────

# Chromium UA-string template — bumped when Chromium does a major release.
# A stale UA is the #1 reason DataDome / Cloudflare / PerimeterX flag
# headless traffic; keep this in sync with Chromium's actual major version.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}
DEFAULT_LOCALE = "en-US"
DEFAULT_TIMEZONE = "America/Los_Angeles"

# Wall-clock budget for any single navigation + wait combined. Anything
# longer almost always means the page is stuck on a CAPTCHA / 5xx —
# better to fail fast and retry than pin a Celery worker.
DEFAULT_NAV_TIMEOUT_MS = 30_000

# Per-page cap. Used as the default when callers don't pass an explicit
# selector timeout. Below the nav budget so a single bad selector wait
# can't consume the whole budget on its own.
DEFAULT_SELECTOR_TIMEOUT_MS = 10_000


# ── Configuration / errors ──────────────────────────────────────────────────


class PlaywrightUnavailable(RuntimeError):
    """Raised when Playwright isn't installed (dev / test environments).

    Caller decides what to do: skip the feature, fall back to httpx,
    surface a 503, etc. We deliberately do NOT auto-fallback at this
    layer because the right fallback depends on the workflow.
    """


class BrowserError(RuntimeError):
    """Wrapper around any underlying Playwright error so callers can
    catch a single class instead of every Playwright-internal one."""


# ── Browser pool: one Chromium per process ──────────────────────────────────


@dataclass
class _PoolState:
    """Holds the singleton browser + driver handles per Python process.

    Wrapped in a dataclass (not module-level globals) so tests can
    instantiate a fresh pool without polluting the real one.
    """
    playwright: Any | None = None
    browser: Any | None = None
    launch_lock: asyncio.Lock | None = None


_pool = _PoolState()


def _get_lock() -> asyncio.Lock:
    """Lazily create the launch lock. Done lazily because asyncio.Lock
    binds to the running event loop on first await — instantiating it
    at module import time fails when imported before a loop exists."""
    if _pool.launch_lock is None:
        _pool.launch_lock = asyncio.Lock()
    return _pool.launch_lock


async def _ensure_browser() -> Any:
    """Launch Chromium once per process; return the cached handle on
    every subsequent call. Coalesced via an asyncio.Lock so concurrent
    callers during cold start don't race-launch two browsers.
    """
    if _pool.browser is not None and _pool.browser.is_connected():
        return _pool.browser

    async with _get_lock():
        # Double-check inside the lock — another task may have launched
        # while we were waiting.
        if _pool.browser is not None and _pool.browser.is_connected():
            return _pool.browser

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PlaywrightUnavailable(
                "Playwright not installed in this environment. "
                "`pip install playwright && playwright install chromium` "
                "before using this service."
            ) from exc

        logger.info("playwright_browser: launching Chromium")
        try:
            _pool.playwright = await async_playwright().start()
            # ``--disable-blink-features=AutomationControlled`` is the
            # cheapest win against fingerprint-based detection — it
            # removes ``navigator.webdriver`` which most bot-detection
            # libraries grep for first. ``--no-sandbox`` is required
            # inside container environments where we can't get the
            # uid mapping right for Chromium's setuid sandbox.
            _pool.browser = await _pool.playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",  # use /tmp not /dev/shm (small in containers)
                ],
            )
            logger.info("playwright_browser: Chromium ready")
        except Exception as exc:
            # Surface a clear single-line error rather than the deeply-
            # nested Playwright launch traceback. Caller logs it.
            raise BrowserError(f"Chromium launch failed: {exc}") from exc

        return _pool.browser


async def shutdown_pool() -> None:
    """Close the browser + driver. Called from FastAPI lifespan on
    shutdown. Safe to call when nothing was ever launched.
    """
    if _pool.browser is not None:
        try:
            await _pool.browser.close()
        except Exception as exc:
            logger.warning("playwright_browser: close failed: %s", exc)
        _pool.browser = None
    if _pool.playwright is not None:
        try:
            await _pool.playwright.stop()
        except Exception as exc:
            logger.warning("playwright_browser: driver stop failed: %s", exc)
        _pool.playwright = None


# ── Session: one BrowserContext + Page per workflow ─────────────────────────


class BrowserSession:
    """One-shot browser session with its own cookie jar + a single tab.

    Each ``BrowserSession`` opens an isolated ``BrowserContext`` so
    cookies / auth / localStorage from one workflow can't bleed into
    another. The ``Page`` is created on enter and closed on exit so a
    leak in the caller can't pin Chromium memory.

    Usage::

        async with BrowserSession() as s:
            await s.navigate("https://example.com/login")
            await s.fill("#email", "user@example.com")
            await s.fill("#password", secret)
            await s.click("button[type=submit]")
            await s.wait_for_url("**/dashboard")
            html = await s.html()

    All operations raise ``BrowserError`` on failure (wrapping the
    underlying Playwright-internal error), so callers can use a
    single try/except.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        viewport: dict[str, int] = DEFAULT_VIEWPORT,
        locale: str = DEFAULT_LOCALE,
        timezone_id: str = DEFAULT_TIMEZONE,
        nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
        extra_headers: dict[str, str] | None = None,
    ):
        self._user_agent = user_agent
        self._viewport = viewport
        self._locale = locale
        self._timezone_id = timezone_id
        self._nav_timeout_ms = nav_timeout_ms
        self._extra_headers = extra_headers or {}
        self._context: Any | None = None
        self._page: Any | None = None

    async def __aenter__(self) -> "BrowserSession":
        browser = await _ensure_browser()
        try:
            self._context = await browser.new_context(
                user_agent=self._user_agent,
                viewport=self._viewport,
                locale=self._locale,
                timezone_id=self._timezone_id,
                extra_http_headers=self._extra_headers,
            )
            # Inject the same automation-control-removal that Playwright's
            # newer launch arg does, but also nuke a couple of headless-
            # specific window properties that DataDome / Cloudflare check
            # via JS. Cheap, idempotent, and survives DOM ready.
            await self._context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            """)
            # Optional: layer ``playwright-stealth`` on top of the manual
            # init-script when the package is available. ``Stealth``
            # patches a much larger set of fingerprint surfaces — the
            # WebGL renderer string, ``Chrome.runtime``, permissions API,
            # iframe contentWindow, etc. We import lazily so a test or
            # dev environment without the package still works (the manual
            # init script handles the basics).
            try:
                from playwright_stealth import Stealth  # type: ignore[import-not-found]
                await Stealth().apply_stealth_async(self._context)
            except ImportError:
                logger.debug("playwright-stealth not installed; using basic stealth only")
            self._page = await self._context.new_page()
            # ``set_default_*`` are sync methods on the real Playwright
            # Page — guard against AsyncMock/test-mock setups that
            # accidentally make them async by checking before invoke.
            try:
                self._page.set_default_navigation_timeout(self._nav_timeout_ms)
                self._page.set_default_timeout(DEFAULT_SELECTOR_TIMEOUT_MS)
            except Exception:
                # Test mocks don't expose these as sync; ignore in tests.
                pass
        except Exception as exc:
            await self._safe_close()
            raise BrowserError(f"Session open failed: {exc}") from exc
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        # Optional debug screenshot when the session is exiting due to
        # an exception. Helps diagnose "what did the page actually look
        # like?" without re-running the workflow.
        if exc_val is not None and os.environ.get("PLAYWRIGHT_DEBUG_SCREENSHOTS") == "1":
            try:
                path = f"/tmp/playwright-fail-{uuid.uuid4().hex[:8]}.png"
                if self._page is not None:
                    await self._page.screenshot(path=path, full_page=True)
                    logger.warning("playwright_browser: failure screenshot at %s", path)
            except Exception as inner:
                logger.warning("playwright_browser: failure-screenshot failed: %s", inner)
        await self._safe_close()

    async def _safe_close(self) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

    # ── Navigation ─────────────────────────────────────────────────

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        wait_for_selector: str | None = None,
        wait_for_selector_timeout_ms: int = DEFAULT_SELECTOR_TIMEOUT_MS,
    ) -> None:
        """Go to ``url`` and optionally wait for a selector before returning.

        ``wait_until`` choices:
          * ``"domcontentloaded"`` (default) — fast, returns once the
            HTML parser has finished. Good for SSR pages.
          * ``"networkidle"`` — wait until network activity quiets.
            Good for SPAs that hydrate content via XHR after DOM ready.
          * ``"load"`` — full page load including images. Slowest.

        Raises ``BrowserError`` on timeout / network failure.
        """
        if self._page is None:
            raise BrowserError("Session not entered (use async with)")
        try:
            await self._page.goto(url, wait_until=wait_until)
            if wait_for_selector:
                await self._page.wait_for_selector(
                    wait_for_selector, timeout=wait_for_selector_timeout_ms
                )
        except Exception as exc:
            raise BrowserError(f"navigate({url!r}): {exc}") from exc

    async def html(self) -> str:
        """Return the current page's full rendered HTML."""
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            return await self._page.content()
        except Exception as exc:
            raise BrowserError(f"html(): {exc}") from exc

    async def text(self, selector: str) -> str:
        """Return the inner text of the first matching element.

        Returns ``""`` if the selector doesn't match — most callers
        prefer "missing" to "raise" for optional fields.
        """
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            el = await self._page.query_selector(selector)
            if el is None:
                return ""
            return (await el.inner_text()).strip()
        except Exception as exc:
            raise BrowserError(f"text({selector!r}): {exc}") from exc

    async def all_text(self, selector: str) -> list[str]:
        """Return inner text of every matching element."""
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            elements = await self._page.query_selector_all(selector)
            return [(await el.inner_text()).strip() for el in elements]
        except Exception as exc:
            raise BrowserError(f"all_text({selector!r}): {exc}") from exc

    async def attr(self, selector: str, name: str) -> str:
        """Return an attribute of the first matching element, or ``""``."""
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            el = await self._page.query_selector(selector)
            if el is None:
                return ""
            v = await el.get_attribute(name)
            return v or ""
        except Exception as exc:
            raise BrowserError(f"attr({selector!r}, {name!r}): {exc}") from exc

    async def eval_js(self, script: str) -> Any:
        """Run a JS expression in the page and return the result.

        Convenient for extracting structured data the page exposes on
        ``window`` (Apollo state, Redux store, embedded JSON-LD) without
        a fragile DOM walk.
        """
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            return await self._page.evaluate(script)
        except Exception as exc:
            raise BrowserError(f"eval_js(...): {exc}") from exc

    # ── Form filling (apply workflows) ─────────────────────────────

    async def fill(self, selector: str, value: str) -> None:
        """Type ``value`` into the input matching ``selector``.

        Uses ``locator.fill`` which clears any existing value first —
        idempotent across retries.
        """
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            await self._page.fill(selector, value)
        except Exception as exc:
            raise BrowserError(f"fill({selector!r}): {exc}") from exc

    async def select(self, selector: str, value: str) -> None:
        """Select an ``<option>`` by value or label on the matching ``<select>``."""
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            await self._page.select_option(selector, value=value)
        except Exception:
            # Fallback: try by label (Greenhouse uses ``<option>`` text).
            try:
                await self._page.select_option(selector, label=value)
            except Exception as exc2:
                raise BrowserError(f"select({selector!r}, {value!r}): {exc2}") from exc2

    async def press(self, selector: str, key: str) -> None:
        """Send a single key to the element matching ``selector``.

        Needed for JS-widget form controls that have no native element
        to drive. Greenhouse's modern boards render every dropdown as a
        react-select combobox — an ``<input role="combobox">`` with no
        ``<select>`` anywhere on the page — so the only way to choose an
        option is click, type the label, then Enter. ``select()`` above
        cannot touch those.
        """
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            await self._page.press(selector, key)
        except Exception as exc:
            raise BrowserError(f"press({selector!r}, {key!r}): {exc}") from exc

    async def click(
        self,
        selector: str,
        *,
        timeout_ms: int = DEFAULT_SELECTOR_TIMEOUT_MS,
    ) -> None:
        """Click the matching element, waiting for it to be actionable."""
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            await self._page.click(selector, timeout=timeout_ms)
        except Exception as exc:
            raise BrowserError(f"click({selector!r}): {exc}") from exc

    async def upload(self, selector: str, file_path: str) -> None:
        """Set a file on the matching ``<input type=file>``.

        ``file_path`` must be a path Playwright can read — for resume-
        upload flows, write the bytes to a tmp file first.
        """
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            await self._page.set_input_files(selector, file_path)
        except Exception as exc:
            raise BrowserError(f"upload({selector!r}, {file_path!r}): {exc}") from exc

    async def wait_for_selector(
        self, selector: str, *, timeout_ms: int = DEFAULT_SELECTOR_TIMEOUT_MS
    ) -> None:
        """Block until ``selector`` is in the DOM (or timeout)."""
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            await self._page.wait_for_selector(selector, timeout=timeout_ms)
        except Exception as exc:
            raise BrowserError(f"wait_for_selector({selector!r}): {exc}") from exc

    async def wait_for_url(
        self, url_glob: str, *, timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS
    ) -> None:
        """Block until the page URL matches ``url_glob`` (e.g.
        ``"**/dashboard"`` after a successful login)."""
        if self._page is None:
            raise BrowserError("Session not entered")
        try:
            await self._page.wait_for_url(url_glob, timeout=timeout_ms)
        except Exception as exc:
            raise BrowserError(f"wait_for_url({url_glob!r}): {exc}") from exc

    async def url(self) -> str:
        """Return the page's current URL."""
        if self._page is None:
            raise BrowserError("Session not entered")
        return self._page.url

    # ── Network interception ───────────────────────────────────────

    async def capture_xhr(
        self,
        url_substring: str,
        navigate_to: str,
        *,
        method: str = "GET",
        timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    ) -> dict | list | None:
        """Navigate to ``navigate_to`` and capture the JSON body of the
        first XHR/fetch response whose URL contains ``url_substring``
        and matches ``method`` (case-insensitive).

        Many SPAs (Wellfound, Lever's hosted pages) hydrate content via
        a single JSON XHR after DOM ready. Capturing that payload is
        more robust than walking the rendered DOM — the JSON shape is
        stable while the markup churns.
        """
        if self._page is None:
            raise BrowserError("Session not entered")

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        method_upper = method.upper()

        def _on_response(response: Any) -> None:
            try:
                if (
                    url_substring in response.url
                    and response.request.method.upper() == method_upper
                    and not future.done()
                ):
                    future.set_result(response)
            except Exception:
                pass

        self._page.on("response", _on_response)

        try:
            await self._page.goto(navigate_to, wait_until="domcontentloaded")
            response = await asyncio.wait_for(future, timeout=timeout_ms / 1000)
            return await response.json()
        except (asyncio.TimeoutError, Exception) as exc:
            raise BrowserError(
                f"capture_xhr({url_substring!r}, {navigate_to!r}): {exc}"
            ) from exc
        finally:
            try:
                self._page.remove_listener("response", _on_response)
            except Exception:
                pass


# ── One-call shortcut for the common "fetch + parse" pattern ────────────────


async def fetch_html(
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    wait_for_selector: str | None = None,
    timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> str:
    """Open a browser session, fetch ``url``, return the rendered HTML.

    Convenience wrapper for callers that don't need to interact with
    the page beyond a single load. Each call is its own session, so
    cookies don't bleed between calls — for multi-step workflows use
    ``BrowserSession`` directly.
    """
    async with BrowserSession(user_agent=user_agent, nav_timeout_ms=timeout_ms) as s:
        await s.navigate(
            url,
            wait_until=wait_until,
            wait_for_selector=wait_for_selector,
        )
        return await s.html()


# ── Test seam ────────────────────────────────────────────────────────────────


def reset_pool_for_tests() -> None:
    """Drop the singleton state so a test can start with a fresh pool.

    Tests that mock ``_ensure_browser`` should call this in a fixture
    teardown so subsequent tests aren't poisoned by mock state.
    """
    _pool.playwright = None
    _pool.browser = None
    _pool.launch_lock = None
