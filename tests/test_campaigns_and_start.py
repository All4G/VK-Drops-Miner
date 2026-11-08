"""Campaign parsing, starting a stream, fast offline detection and retries
against an emulated live.vkvideo.ru (requests are answered locally)."""
import asyncio
import time
from pathlib import Path

import main as m
from helpers import drain_logs, make_worker, open_page
from playwright.async_api import async_playwright

CAMPAIGNS = (Path(__file__).parent / "fixtures" / "campaigns.html").read_text(encoding="utf-8")
HEAD = '<html><body><div class="TopMenuRightAuthorized_x"></div><div class="ChannelStreamPanel_root_x"></div>'
LIVE = HEAD + ('<div class="StreamStatus_isOnline_x">Live</div><vk-video-player>'
               '<div class="state-container" data-playback-state="playing"></div></vk-video-player></body></html>')
OFFLINE = HEAD + '<vk-video-player><div class="state-container" data-playback-state="paused"></div></vk-video-player></body></html>'


async def _site(ctx, offline_first=0, new_tab=False):
    hits = {"n": 0}
    campaigns = CAMPAIGNS.replace(
        "/*NAV*/", "window.open('https://live.vkvideo.ru/streamer','_blank'); return;" if new_tab else "")

    async def handler(route):
        url = route.request.url
        if "/app/box/campaigns" in url:
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=campaigns)
        elif "streamer" in url:
            hits["n"] += 1
            await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body=OFFLINE if hits["n"] <= offline_first else LIVE)
        else:
            await route.fulfill(status=200, body="")

    await ctx.route("**/*", handler)
    return hits


async def _worker(ctx):
    page = await ctx.new_page()
    w, q = make_worker(page, ctx)
    w.browser = type("B", (), {"is_connected": lambda self: True})()
    return w, q


def test_fetch_lists_active_and_available_but_not_finished():
    async def run():
        async with async_playwright() as pw:
            browser, ctx = await open_page(pw)
            await _site(ctx)
            w, _ = await _worker(ctx)
            campaigns = await w.fetch_campaigns()
            await browser.close()
            return campaigns
    campaigns = asyncio.run(run())
    assert [(c["name"], c["section"]) for c in campaigns] == [
        ("Кампания Альфа", "Активные"), ("Кампания Бета", "Доступные"), ("Кампания Гамма", "Доступные")]
    assert campaigns[1]["game"] == "Игра Два" and "07 ноября" in campaigns[1]["dates"]


def test_blacklist_and_preferred_games():
    async def run():
        async with async_playwright() as pw:
            browser, ctx = await open_page(pw)
            await _site(ctx)
            w, _ = await _worker(ctx)
            w.cfg["drops"]["blacklist_games"] = ["Игра Два"]
            w.cfg["drops"]["preferred_games"] = ["Игра Три"]
            campaigns = await w.fetch_campaigns()
            await browser.close()
            return campaigns
    assert [c["name"] for c in asyncio.run(run())] == ["Кампания Гамма", "Кампания Альфа"]


def test_start_works_for_same_tab_and_new_tab_navigation():
    async def run(new_tab):
        async with async_playwright() as pw:
            browser, ctx = await open_page(pw)
            await _site(ctx, new_tab=new_tab)
            w, _ = await _worker(ctx)
            target = [c for c in await w.fetch_campaigns() if c["name"] == "Кампания Бета"][0]
            status = await w.start_campaign_mining(target)
            url = w.page.url
            await browser.close()
            return status, url
    for new_tab in (False, True):
        status, url = asyncio.run(run(new_tab))
        assert status == "ok" and url.endswith("/streamer"), (new_tab, status, url)


def test_offline_stream_is_detected_fast_and_retried_until_live():
    async def run():
        async with async_playwright() as pw:
            browser, ctx = await open_page(pw)
            hits = await _site(ctx, offline_first=3)
            w, q = await _worker(ctx)
            target = [c for c in await w.fetch_campaigns() if c["name"] == "Кампания Бета"][0]
            t = time.monotonic()
            first = await w.start_campaign_mining(target)
            first_s = time.monotonic() - t

            async def reached_watch(_c):
                return "finished"
            w.watch_loop = reached_watch  # stop after a live stream is reached
            result = await w.mine_campaign(target)
            await browser.close()
            return first, first_s, result, hits["n"], drain_logs(q)
    first, first_s, result, openings, logs = asyncio.run(run())
    assert first == "offline" and first_s < 10          # not the old 30+ s
    assert result == "done" and openings == 4           # 3 dead streams, then a live one
    assert any("Повторная попытка" in l for l in logs)
