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


async def test_ambiguous_category_is_an_error_not_a_guess(fixture):
    html = fixture("categories.html.gz")
    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(lambda r: html_resp(html)))
    with pytest.raises(ValueError, match="ambiguous category"):
        await c.category_id("möbel")
    assert await c.category_id("Haushaltsgeräte") == "176"
    assert await c.category_id("haushaltsgeraete") == "176"
    assert await c.category_id("handy-telekom") == "173"
    await c.aclose()


async def test_mcp_zero_results_hint(fixture):
    from kleinanzeigen_mcp.mcp_server import build_mcp

    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(lambda r: html_resp("<html></html>")))
    mcp = build_mcp(lambda: c)
    res = await mcp.call_tool("search_listings", {"query": "sehr lange spezielle suche mit vielen wörtern"})
    data = res.structured_content
    assert data["count"] == 0 and "fewer" in data["hints"][0] and "words" in data["hints"][0]
    await c.aclose()


async def test_frankfurt_prefers_the_city_with_districts():
    payload = ('{"_0":"Deutschland","_7950":"Frankfurt (Oder) - Brandenburg","_4292":"Frankfurt am Main - Hessen",'
               '"_4343":"West - Frankfurt am Main","_14055":"Sachsenhausen - Frankfurt am Main"}')
    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "application/json"}, text=payload)))
    assert await c.location_id("Frankfurt") == "4292"
    assert c._location_alts["4292"] == ["Frankfurt (Oder) - Brandenburg"]
    await c.aclose()


async def test_time_window_pages_until_complete(fixture):
    from datetime import datetime, timedelta

    from kleinanzeigen_mcp.parser import BERLIN
    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(lambda r: html_resp("")))
    now = datetime.now(BERLIN)
    calls: list[int] = []

    async def fake_search(q, *, pages=1, include_promoted=True, limit=None, exclude_terms=None):
        calls.append(q.page)
        ads = [AdSummary(id=str(q.page * 100 + i), url="u", title="x", price=Price(),
                         posted_at=now - timedelta(minutes=(q.page - 1) * 50 + i * 2)) for i in range(25)]
        from kleinanzeigen_mcp.models import SearchResult
        return SearchResult(url="u", page=q.page, has_next_page=True, ads=ads, fetched_at=now)

    c.search = fake_search  # type: ignore[method-assign]
    res = await c.latest(SearchQuery(query="x"), max_age_minutes=120, limit=None)
    assert calls == [1, 2, 3] and res.complete is True
    assert all(a.posted_at >= now - timedelta(minutes=120) for a in res.ads) and len(res.ads) > 25
    await c.aclose()


def test_defect_skips_price_comparison_and_same_day_account():
    from datetime import datetime

    from kleinanzeigen_mcp import scoring
    from kleinanzeigen_mcp.models import AdDetail, SellerSummary
    from kleinanzeigen_mcp.parser import BERLIN
    now = datetime(2026, 11, 14, 12, tzinfo=BERLIN)
    d = AdDetail(id="1", url="u", title="iPhone 15 Pro", description="Rückglas gesprungen, funktioniert.",
                 price=Price(amount_eur=240), attributes={"Zustand": "Defekt"}, images=["a", "b", "c"],
                 posted_at=datetime(2026, 10, 22, tzinfo=BERLIN), fetched_at=now,
                 seller=SellerSummary(active_since="22.10.2026"))
    r = scoring.assess(d, median_eur=500, now=now)
    assert r.price_vs_median is None and not any("Marktmedian" in x for x in r.reasons)
    assert any("Preisvergleich entfällt" in h for h in r.hints)
    assert any("am Tag der Anzeige erstellt" in x for x in r.reasons)


async def test_category_filter_and_lookup_tolerate_umlauts(fixture):
    from kleinanzeigen_mcp.mcp_server import build_mcp
    html = fixture("categories.html.gz")
    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(lambda r: html_resp(html)))
    assert await c.category_id("fahrrad") == "217"
    res = await build_mcp(lambda: c).call_tool("list_categories", {"filter": "fahrrad"})
    assert any(x["id"] == "217" for x in res.structured_content["categories"])
    await c.aclose()


async def test_get_seller_by_ad(fixture):
    from kleinanzeigen_mcp.mcp_server import build_mcp
    pages = {"ad": fixture("ad_private.html.gz"), "seller": fixture("seller_private.html.gz")}
    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(
        lambda r: html_resp(pages["seller" if "bestandsliste" in r.url.path else "ad"])))
    res = await build_mcp(lambda: c).call_tool("get_seller", {"ad": "3571224603"})
    assert res.structured_content["profile"]["user_id"] == "166595111" and res.structured_content["ads"]
    await c.aclose()


async def test_exact_city_reports_no_district_alternatives():
    payload = ('{"_0":"Deutschland","_9409":"Hamburg","_9446":"Wandsbek - Hamburg",'
               '"_16436":"Hamburg-Nord - Hamburg","_12853":"Hamburg Barmbek - Hamburg-Nord"}')
    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "application/json"}, text=payload)))
    assert await c.location_id("Hamburg") == "9409"
    assert c._location_alts["9409"] == []
    await c.aclose()


def test_laptop_ram_detector():
    from kleinanzeigen_mcp import scoring
    for t in ["Samsung 32GB DDR5-5600 (M425R4GA3PB0)", "Dell 32GB CAMM RAM Modul", "32GB DDR5 SO-DIMM",
              "16GB DDR5 SODIMM", "Crucial 32GB Laptop RAM"]:
        assert scoring.LAPTOP_RAM.search(t), t
    for t in ["Samsung 32GB DDR5 UDIMM M323R4GA3BB0", "Corsair Vengeance 2x16GB DDR5 6000", "G.Skill Trident Z5"]:
        assert not scoring.LAPTOP_RAM.search(t), t
    assert scoring.wants_desktop_ram(["SODIMM"]) and not scoring.wants_desktop_ram(["hülle"])


async def test_exclude_terms_top_up_and_report(fixture):
    pages = {1: fixture("search_new.html.gz"), 2: fixture("search_autos.html.gz")}
    c = KleinanzeigenClient(settings(), transport=httpx.MockTransport(
        lambda r: html_resp(pages[2 if "seite:2" in r.url.path else 1])))
    res = await c.search(SearchQuery(query="x"), include_promoted=True, limit=25, exclude_terms=["iphone"])
    assert len(res.ads) == 25 and not any("iphone" in a.title.lower() for a in res.ads)
    assert any("exclude_terms removed" in n for n in res.notes)
    await c.aclose()


def test_html_entities_unescaped():
    from kleinanzeigen_mcp.parser import clean
    assert clean("16GB &#x2F; 32GB &amp; mehr") == "16GB / 32GB & mehr"
