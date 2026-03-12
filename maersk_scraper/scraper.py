"""
Maersk schedule scraper using Playwright with network response interception.

Strategy
--------
1. Open the Maersk schedule search page in a headless Chromium browser.
2. Register a response handler that watches for the internal JSON API call
   that the frontend makes when it fetches schedule results.
3. Fill in the origin / destination / date form fields and submit.
4. Wait for the intercepted API response (or fall back to page scraping).
5. Hand the raw JSON to the parser, which returns ORM objects ready to save.
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Request,
    Response,
    async_playwright,
)

from .config import Config
from .models import ScheduleQuery
from .parser import parse_schedule_response

logger = logging.getLogger(__name__)

# ── URL patterns we want to intercept ─────────────────────────────────────────
_SCHEDULE_URL_PATTERNS: list[re.Pattern] = [
    re.compile(r"maersk\.com.*schedule.*point.?to.?point", re.IGNORECASE),
    re.compile(r"maersk\.com.*api.*schedule", re.IGNORECASE),
    re.compile(r"maersk\.com.*schedules.*search", re.IGNORECASE),
    re.compile(r"api\.maersk\.com.*schedule", re.IGNORECASE),
    re.compile(r"maersk\.com.*point-to-point", re.IGNORECASE),
    # Broader fallback — any XHR that likely carries schedule payload
    re.compile(r"maersk\.com.*(transportPlan|sailingSchedule|p2p)", re.IGNORECASE),
]

MAERSK_SCHEDULES_URL = "https://www.maersk.com/schedules/pointToPoint"


# ── Data class for a search request ───────────────────────────────────────────

@dataclass
class SearchRequest:
    origin_code: str
    origin_name: str
    destination_code: str
    destination_name: str
    departure_date: str          # YYYY-MM-DD
    origin_country: str = ""
    destination_country: str = ""


# ── Main scraper class ─────────────────────────────────────────────────────────

class MaerskScraper:
    """Async Playwright-based scraper for Maersk point-to-point schedules."""

    def __init__(self, config: Config):
        self.config = config
        self._context: Optional[BrowserContext] = None
        self._playwright = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()

    async def start(self) -> None:
        """Launch the browser and create a persistent context."""
        self._playwright = await async_playwright().start()
        browser = await self._playwright.chromium.launch(headless=self.config.headless)
        context_kwargs: dict = {
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
            "timezone_id": "UTC",
        }
        if self.config.user_agent:
            context_kwargs["user_agent"] = self.config.user_agent

        self._context = await browser.new_context(**context_kwargs)
        # Block heavy resources we don't need — speeds up loading
        await self._context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot}",
            lambda route, _: route.abort(),
        )
        logger.info("Browser started (headless=%s)", self.config.headless)

    async def stop(self) -> None:
        """Close browser and Playwright instance."""
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed")

    # ── Search ─────────────────────────────────────────────────────────────

    async def search(self, req: SearchRequest) -> tuple[ScheduleQuery, list]:
        """
        Perform a single schedule search and return:
          (ScheduleQuery, list[Schedule])

        The Schedule objects have their .legs populated but are NOT yet
        persisted to the database.
        """
        query = ScheduleQuery(
            origin_code=req.origin_code.upper(),
            origin_name=req.origin_name,
            origin_country=req.origin_country,
            destination_code=req.destination_code.upper(),
            destination_name=req.destination_name,
            destination_country=req.destination_country,
            departure_date=req.departure_date,
            scraped_at=datetime.utcnow(),
        )

        logger.info(
            "Searching: %s (%s) → %s (%s) on %s",
            req.origin_name, req.origin_code,
            req.destination_name, req.destination_code,
            req.departure_date,
        )

        page = await self._context.new_page()
        try:
            json_data = await self._run_search(page, req)
        finally:
            await page.close()

        if json_data is None:
            logger.warning("No schedule data intercepted for %s → %s", req.origin_code, req.destination_code)
            query.results_count = 0
            return query, []

        schedules = parse_schedule_response(json_data, query)
        query.results_count = len(schedules)
        return query, schedules

    # ── Internal page interaction ──────────────────────────────────────────

    async def _run_search(self, page: Page, req: SearchRequest) -> Optional[Any]:
        """
        Navigate to Maersk schedules page, fill the form, and intercept the
        resulting API response.  Returns the parsed JSON payload or None.
        """
        captured: dict[str, Any] = {}
        capture_event = asyncio.Event()

        async def on_response(response: Response) -> None:
            url = response.url
            if not any(p.search(url) for p in _SCHEDULE_URL_PATTERNS):
                return
            if response.status not in (200, 201):
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            try:
                data = await response.json()
                if _looks_like_schedule_data(data):
                    logger.debug("Captured schedule response from: %s", url)
                    captured["data"] = data
                    captured["url"] = url
                    capture_event.set()
            except Exception as exc:
                logger.debug("Could not parse response from %s: %s", url, exc)

        page.on("response", on_response)

        # Navigate to schedules page
        await page.goto(MAERSK_SCHEDULES_URL, wait_until="domcontentloaded", timeout=self.config.browser_timeout_ms)
        logger.debug("Page loaded: %s", MAERSK_SCHEDULES_URL)

        # Accept cookies if present
        await _dismiss_cookie_banner(page)

        # Fill the search form
        filled = await self._fill_search_form(page, req)
        if not filled:
            logger.warning("Could not fill search form — will wait for any intercepted response")

        # Wait for the API response (or timeout)
        try:
            await asyncio.wait_for(capture_event.wait(), timeout=self.config.browser_timeout_ms / 1000)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for schedule API response")

        # Extra settle time
        if self.config.results_wait_ms > 0:
            await page.wait_for_timeout(self.config.results_wait_ms)

        # Final attempt: look for any matching response that arrived late
        if "data" not in captured:
            logger.debug("Trying to extract schedule data from page DOM")
            dom_data = await _extract_from_dom(page)
            if dom_data:
                captured["data"] = dom_data

        return captured.get("data")

    async def _fill_search_form(self, page: Page, req: SearchRequest) -> bool:
        """
        Fill in the Maersk schedule search form.

        The Maersk website is a React SPA — selectors can change between
        deployments.  We try several strategies in order of preference.
        """
        try:
            # Strategy 1: look for labelled inputs / comboboxes
            origin_filled = await _type_in_port_field(
                page,
                ["[data-testid='origin-port']", "[placeholder*='origin' i]",
                 "[aria-label*='origin' i]", "input[name*='origin' i]",
                 "[data-testid='from-port']", "input[id*='origin' i]"],
                req.origin_name or req.origin_code,
                req.origin_code,
            )
            dest_filled = await _type_in_port_field(
                page,
                ["[data-testid='destination-port']", "[placeholder*='destination' i]",
                 "[aria-label*='destination' i]", "input[name*='destination' i]",
                 "[data-testid='to-port']", "input[id*='destination' i]"],
                req.destination_name or req.destination_code,
                req.destination_code,
            )

            # Date field
            await _fill_date_field(page, req.departure_date)

            # Submit
            await _click_search_button(page)

            logger.debug("Form filled: origin=%s dest=%s", origin_filled, dest_filled)
            return origin_filled and dest_filled

        except Exception as exc:
            logger.warning("Error filling form: %s", exc)
            return False


# ── Batch search helper ────────────────────────────────────────────────────────

async def run_batch_search(
    requests: list[SearchRequest],
    config: Config,
    on_result=None,
) -> list[tuple[ScheduleQuery, list]]:
    """
    Run multiple searches sequentially with a configurable delay between them.

    `on_result` is an optional async callback(query, schedules) called after
    each successful search — useful for streaming results to the database.
    """
    results = []
    async with MaerskScraper(config) as scraper:
        for i, req in enumerate(requests):
            if i > 0:
                logger.info("Waiting %.1fs before next search…", config.request_delay_s)
                await asyncio.sleep(config.request_delay_s)
            try:
                query, schedules = await scraper.search(req)
                results.append((query, schedules))
                if on_result:
                    await on_result(query, schedules)
            except Exception as exc:
                logger.error("Search failed for %s → %s: %s", req.origin_code, req.destination_code, exc)
    return results


# ── Page helpers ───────────────────────────────────────────────────────────────

async def _dismiss_cookie_banner(page: Page) -> None:
    """Click the cookie accept button if it exists."""
    selectors = [
        "button[id*='accept' i]",
        "button[data-testid*='accept' i]",
        "button:has-text('Accept all')",
        "button:has-text('Accept cookies')",
        "button:has-text('Accepter')",
        "#onetrust-accept-btn-handler",
        ".cookie-banner button",
        "[aria-label*='accept' i][role='button']",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click(timeout=3000)
                logger.debug("Dismissed cookie banner via: %s", sel)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def _type_in_port_field(
    page: Page,
    selectors: list[str],
    search_text: str,
    port_code: str,
) -> bool:
    """Type a port name into a combobox and select the matching suggestion."""
    for sel in selectors:
        try:
            field = page.locator(sel).first
            if not await field.is_visible(timeout=3000):
                continue
            await field.click()
            await field.fill("")
            await field.type(search_text, delay=60)
            await page.wait_for_timeout(1500)

            # Try to click a suggestion that matches the port code
            suggestion_picked = await _pick_suggestion(page, port_code, search_text)
            if suggestion_picked:
                logger.debug("Selected port %s via suggestion", port_code)
            return True
        except Exception:
            continue
    return False


async def _pick_suggestion(page: Page, port_code: str, fallback_text: str) -> bool:
    """Select a port autocomplete suggestion."""
    suggestion_selectors = [
        f"[data-testid*='suggestion']:has-text('{port_code}')",
        f"li:has-text('{port_code}')",
        f"[role='option']:has-text('{port_code}')",
        f"[role='listbox'] [role='option']:has-text('{fallback_text[:10]}')",
        "[role='listbox'] [role='option']:first-child",
        "[data-testid='suggestion-item']:first-child",
        ".suggestion-list li:first-child",
    ]
    for sel in suggestion_selectors:
        try:
            item = page.locator(sel).first
            if await item.is_visible(timeout=2000):
                await item.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


async def _fill_date_field(page: Page, departure_date: str) -> None:
    """Fill the departure date field."""
    date_selectors = [
        "[data-testid='departure-date']",
        "input[type='date']",
        "input[placeholder*='date' i]",
        "[aria-label*='departure date' i]",
        "input[name*='date' i]",
        "input[id*='date' i]",
    ]
    for sel in date_selectors:
        try:
            field = page.locator(sel).first
            if await field.is_visible(timeout=2000):
                await field.fill(departure_date)
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue


async def _click_search_button(page: Page) -> None:
    """Click the search/find schedules button."""
    button_selectors = [
        "button[type='submit']",
        "button:has-text('Find schedules')",
        "button:has-text('Search')",
        "button:has-text('Get schedules')",
        "[data-testid='search-button']",
        "[data-testid='submit-button']",
        "button[id*='search' i]",
    ]
    for sel in button_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click(timeout=5000)
                logger.debug("Clicked search button via: %s", sel)
                return
        except Exception:
            continue
    # Last resort: press Enter
    await page.keyboard.press("Enter")


async def _extract_from_dom(page: Page) -> Optional[Any]:
    """
    Last-resort: try to find schedule JSON embedded in the page DOM
    (e.g. in a <script id="__NEXT_DATA__"> or window.__INITIAL_STATE__).
    """
    try:
        next_data = await page.evaluate(
            "() => { const el = document.getElementById('__NEXT_DATA__'); "
            "return el ? el.textContent : null; }"
        )
        if next_data:
            data = json.loads(next_data)
            if _looks_like_schedule_data(data):
                return data
            # Try to dig into props.pageProps
            page_props = (
                data.get("props", {}).get("pageProps", {})
            )
            if _looks_like_schedule_data(page_props):
                return page_props
    except Exception as exc:
        logger.debug("DOM extraction failed: %s", exc)
    return None


def _looks_like_schedule_data(data: Any) -> bool:
    """Heuristic check: does this JSON look like schedule API data?"""
    if isinstance(data, list) and data:
        first = data[0]
        return isinstance(first, dict) and any(
            k in first for k in (
                "transitTime", "legs", "departureDateTime", "arrivalDateTime",
                "transportLegs", "routeLegs", "sailingSchedule",
            )
        )
    if isinstance(data, dict):
        return any(
            k in data for k in (
                "schedules", "pointToPointSchedules", "transitTime",
                "legs", "sailingSchedules", "transportPlan",
            )
        )
    return False
