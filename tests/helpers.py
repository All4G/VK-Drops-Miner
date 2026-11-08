import asyncio
import queue
import unittest

import main as m
from playwright.async_api import async_playwright


async def open_page(pw, context_kwargs=None):
    """Headless Chromium. Skips the test when no browser is installed."""
    try:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    except Exception as e:  # pragma: no cover
        raise unittest.SkipTest(f"Chromium is not installed (playwright install chromium): {e}")
    ctx = await browser.new_context(**(context_kwargs or {}))
    return browser, ctx


def make_worker(page, context=None):
    """A worker wired to an existing page, with instant sleeps (tests must be fast)."""
    q = queue.Queue()
    w = m.VKMinerWorker(q, m.load_config())
    w.page, w.context = page, context or page.context

    async def fast_sleep(seconds):
        await asyncio.sleep(min(seconds, 0.2))

    async def no_sweep(_campaign):
        return 0

    async def no_save():
        return None

    w._sleep = fast_sleep
    w.sweep_campaign_claims = no_sweep
    w.save_auth = no_save
    return w, q


def drain_logs(q):
    out = []
    while not q.empty():
        ev = q.get()
        if ev[0] == "log":
            out.append(ev[1])
    return out


CAMPAIGN = {"key": "k", "name": "Test", "game": "G", "dates": "", "section": "Активные", "card_index": 0}
