import asyncio

import httpx
import pytest

from kleinanzeigen_mcp.cache import AsyncTTLCache
from kleinanzeigen_mcp.client import KleinanzeigenClient, normalize_ad_id
from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.http import BlockedError, Fetcher, NotFoundError, parse_proxy_list
from kleinanzeigen_mcp.urls import SearchQuery


def settings(**kw) -> Settings:
    base = dict(rate_per_s=1000, burst=100, jitter_s=0, warmup=False, max_retries=1, cooldown_base_s=30,
                api_tokens="", _env_file=None)
    base.update(kw)
    return Settings(**base)


class Recorder:
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


async def test_single_flight_and_ttl():
    cache = AsyncTTLCache()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return calls

    res = await asyncio.gather(*(cache.get_or_load("k", 60, load) for _ in range(10)))
    assert calls == 1 and all(v == 1 for v, _ in res)
    assert cache.stats.coalesced == 9
    v, hit = await cache.get_or_load("k", 60, load)
    assert hit and v == 1


async def test_stale_on_error():
    cache = AsyncTTLCache(stale_max_age=3600)
    cache.set("k", "old", ttl=-1)  # already expired

    async def boom():
        raise RuntimeError("upstream down")

    v, hit = await cache.get_or_load("k", 60, boom)
    assert v == "old" and hit and cache.stats.stale_served == 1
    with pytest.raises(RuntimeError):
        await cache.get_or_load("other", 60, boom)


async def test_block_triggers_cooldown_and_stops_traffic():
    rec = Recorder(lambda r: httpx.Response(429, headers={"retry-after": "120"}, text="slow down"))
    f = Fetcher(settings(), transport=httpx.MockTransport(rec))
    with pytest.raises(BlockedError) as ei:
        await f.get_page("/s-x/k0")
    assert ei.value.retry_after >= 120
    with pytest.raises(BlockedError):
        await f.get_page("/s-y/k0")
    assert len(rec.requests) == 1  # second call never left the process
    await f.aclose()


async def test_challenge_page_detected():
    rec = Recorder(lambda r: httpx.Response(200, headers={"content-type": "text/html"},
                                            text="<html><title>Access Denied</title></html>"))
    f = Fetcher(settings(), transport=httpx.MockTransport(rec))
    with pytest.raises(BlockedError):
        await f.get_page("/")
    await f.aclose()


async def test_retry_on_5xx_then_success_and_browser_headers():
    n = {"i": 0}

    def handler(r):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "text/html",
                                            "set-cookie": "__ka-ls=k%3Dx; Path=/"}, text="ok")

    rec = Recorder(handler)
    f = Fetcher(settings(), transport=httpx.MockTransport(rec))
    res = await f.get_page("/s-x/k0")
    assert res.text == "ok" and len(rec.requests) == 2
    h = rec.requests[-1].headers
    assert "Chrome/" in h["user-agent"] and h["sec-fetch-mode"] == "navigate"
    assert h["sec-ch-ua"].count("141") == 2 and h["accept-language"].startswith("de-DE")
    assert all(c.name != "__ka-ls" for c in f.sessions[0].client.cookies.jar)
    await f.aclose()


async def test_deleted_ad_redirect_is_not_found():
    def handler(r: httpx.Request):
        if r.url.path.startswith("/s-anzeige/"):
            return httpx.Response(301, headers={"location": "/s-handy-telekom/c173"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>category</html>")

    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(NotFoundError):
        await c.get_ad("https://www.kleinanzeigen.de/s-anzeige/foo/3571224603-173-16396")
    await c.aclose()


async def test_client_search_is_cached(fixture):
    html = fixture("search_new.html.gz")
    rec = Recorder(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(rec))
    q = SearchQuery(query="iphone")
    r1, r2 = await asyncio.gather(c.search(q), c.search(q))
    r3 = await c.latest(q)
    assert len(rec.requests) == 1
    assert len(r1.ads) == 27 and r1.total == 159120
    assert r3.ads and not any(a.is_promoted for a in r3.ads)
    assert r3.ads == sorted(r3.ads, key=lambda a: a.posted_at, reverse=True)

    new, first = await c.check_watch("w", q)
    assert first and new == []
    new, first = await c.check_watch("w", q)
    assert not first and new == []
    await c.aclose()


async def test_proxy_pool_failover():
    calls: list[str] = []

    class Flaky(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(str(request.url))
            if len(calls) <= 1:  # first session fails, pool fails over immediately
                raise httpx.ConnectError("proxy down")
            return httpx.Response(200, headers={"content-type": "text/html"}, text="ok")

    f = Fetcher(settings(proxies="socks5://a:1080, http://b:3128"), transport=Flaky())
    assert [s.label for s in f.sessions] == ["socks5://a:1080", "http://b:3128"]
    assert (await f.get_page("/")).text == "ok"
    assert f.sessions[1].requests == 1
    assert not f.sessions[0].available  # dead proxy parked
    assert (await f.get_page("/x")).text == "ok"
    assert f.sessions[0].requests == 1 and f.sessions[1].requests == 2
    await f.aclose()


def test_proxy_list_parsing(tmp_path):
    p = tmp_path / "proxies.txt"
    p.write_text("# comment\nsocks5h://u:p@h1:1080\n1.2.3.4:8080  # bare\ndirect\n")
    got = parse_proxy_list("http://x:1", str(p))
    assert got == ["http://x:1", "socks5h://u:p@h1:1080", "http://1.2.3.4:8080", None]
    with pytest.raises(ValueError):
        parse_proxy_list("ftp://nope:21")


def test_normalize_ad_id():
    assert normalize_ad_id("3571224603") == "3571224603"
    assert normalize_ad_id("https://www.kleinanzeigen.de/s-anzeige/x/3571224603-173-16396") == "3571224603"
    with pytest.raises(ValueError):
        normalize_ad_id("hello")


async def test_location_resolution_rejects_fuzzy_matches():
    payloads = {
        "Atlantis-Nirgendwo": '{"_0":"Deutschland","_15650":"Altmannstein - Bayern","_10698":"Atting - Bayern"}',
        "muenchen": '{"_0":"Deutschland","_6411":"München - Bayern"}',
        "Frankfurt am Main": '{"_0":"Deutschland","_7950":"Frankfurt (Oder) - Brandenburg","_4292":"Frankfurt am Main - Hessen"}',
        "80331": '{"_0":"Deutschland","_6443":"80331 Altstadt-Lehel"}',
    }

    def handler(r: httpx.Request):
        return httpx.Response(200, headers={"content-type": "application/json"},
                              text=payloads[r.url.params["query"]])

    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="unknown location"):
        await c.location_id("Atlantis-Nirgendwo")
    assert await c.location_id("muenchen") == "6411"
    assert await c.location_id("Frankfurt am Main") == "4292"
    assert await c.location_id("80331") == "6443"
    assert await c.location_id("3331") == "3331"
    await c.aclose()
