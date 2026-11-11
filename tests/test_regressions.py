"""Regression tests for bugs found in review (one test per finding)."""

import asyncio

import httpx
import pytest

from kleinanzeigen_mcp.cache import AsyncTTLCache
from kleinanzeigen_mcp.client import KleinanzeigenClient
from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.http import Fetcher, NotFoundError, UpstreamError, parse_proxy_list
from kleinanzeigen_mcp.models import AdSummary, Price, Sort
from kleinanzeigen_mcp.urls import SearchQuery


def settings(**kw) -> Settings:
    base = dict(rate_per_s=1000, burst=100, jitter_s=0, warmup=False, max_retries=1, api_tokens="",
                _env_file=None)
    base.update(kw)
    return Settings(**base)


def html_resp(text: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, text=text)


async def test_cancelled_first_caller_does_not_cancel_waiters():
    cache = AsyncTTLCache()
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(0.05)
        return "value"

    t1 = asyncio.create_task(cache.get_or_load("k", 60, slow))
    await started.wait()
    t2 = asyncio.create_task(cache.get_or_load("k", 60, slow))
    await asyncio.sleep(0)
    t1.cancel()
    assert (await t2) == ("value", True)
    assert cache.peek("k") == "value"  # the fetch completed and was cached


async def test_not_found_is_never_served_stale():
    cache = AsyncTTLCache(stale_max_age=3600, no_stale_for=(NotFoundError,))
    cache.set("ad:1", "old detail", ttl=-1)

    async def gone():
        raise NotFoundError("deleted")

    with pytest.raises(NotFoundError):
        await cache.get_or_load("ad:1", 60, gone)
    assert "ad:1" not in cache._data


async def test_http_500_is_not_multiplied_across_proxies():
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(500)

    f = Fetcher(settings(proxies="http://a:1,http://b:1,http://c:1", max_retries=2),
                transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError):
        await f.get_page("/s-x/k0")
    assert len(calls) == 3  # 1 try + 2 retries on ONE session, no failover
    await f.aclose()


async def test_unfollowed_redirect_raises_instead_of_empty_data():
    f = Fetcher(settings(), transport=httpx.MockTransport(
        lambda r: httpx.Response(302, headers={"location": "/elsewhere"})))
    with pytest.raises(UpstreamError):
        await f.get_json("/s-ort-empfehlungen.json?query=x")
    await f.aclose()


async def test_recovered_proxy_is_unparked():
    n = {"i": 0}

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            n["i"] += 1
            if n["i"] == 1:
                raise httpx.ConnectError("down")
            return html_resp("ok")

    f = Fetcher(settings(proxies="http://a:1,http://b:1"), transport=T())
    await f.get_page("/")
    assert not f.sessions[0].available
    f.sessions[0].down_until = 0  # time passes
    await f.get_page("/")
    await f.get_page("/")
    assert f.sessions[0].available and f.sessions[0].consecutive_failures == 0
    await f.aclose()


def test_proxy_password_with_hash_is_kept():
    assert parse_proxy_list("socks5://u:p#ss@h:1080  # my proxy") == ["socks5://u:p#ss@h:1080"]


def test_zero_rate_rejected():
    with pytest.raises(ValueError):
        Settings(_env_file=None, rate_per_s=0)


@pytest.mark.parametrize("q", ["   ", "///", "--"])
async def test_blank_query_is_rejected(q):
    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(lambda r: html_resp("")))
    with pytest.raises(ValueError):
        await c.search(SearchQuery(query=q))
    assert SearchQuery(query=q, location_id=3331).path() == "/s-sortierung:neuste/l3331"
    await c.aclose()


async def test_limit_counts_filtered_ads(fixture):
    pages = {1: fixture("search_new.html.gz"), 2: fixture("search_autos.html.gz")}

    def handler(r):
        return html_resp(pages[2 if "seite:2" in r.url.path else 1])

    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(handler))
    res = await c.search(SearchQuery(query="x"), pages=2, include_promoted=False, limit=27)
    assert len(res.ads) == 27
    await c.aclose()


async def test_strict_mode_sorts_locally(fixture):
    html = fixture("search_new.html.gz")

    def handler(r):
        if r.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /*/preis:*\n")
        return html_resp(html)

    c = KleinanzeigenClient(settings(robots_policy="strict"), transport=httpx.MockTransport(handler))
    res = await c.search(SearchQuery(query="x", sort=Sort.price_asc, radius_km=10, location_id=3331))
    prices = [a.price.amount_eur for a in res.ads if a.price.amount_eur is not None]
    assert prices == sorted(prices)
    assert any("radius" in n for n in res.notes)
    await c.aclose()


async def test_watch_pages_until_known_ad():
    def ad(i: int) -> AdSummary:
        return AdSummary(id=str(i), url=f"https://x/{i}", title=str(i), price=Price())

    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(lambda r: html_resp("")))
    state = {"newest": 100}
    calls: list[int] = []

    async def fake_search(q, *, pages=1, include_promoted=True, limit=None):
        calls.append(q.page)
        top = state["newest"] - (q.page - 1) * 25
        from datetime import UTC, datetime
        return type("R", (), {"ads": [ad(i) for i in range(top, top - 25, -1)], "has_next_page": True,
                              "fetched_at": datetime.now(UTC)})()

    c.search = fake_search  # type: ignore[method-assign]
    new, first, gap = await c.check_watch("w", SearchQuery(query="x"))
    assert first and calls == [1]
    state["newest"] = 160  # 60 new ads appeared → spans 3 pages
    calls.clear()
    new, first, gap = await c.check_watch("w", SearchQuery(query="x"))
    assert calls == [1, 2, 3] and len(new) == 60 and not gap
    state["newest"] = 1000  # far more than 5 pages
    new, first, gap = await c.check_watch("w", SearchQuery(query="x"))
    assert gap
    await c.aclose()
