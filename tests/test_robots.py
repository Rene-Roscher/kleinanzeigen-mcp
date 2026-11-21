import httpx
import pytest

from kleinanzeigen_mcp.client import KleinanzeigenClient
from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.http import PolicyError
from kleinanzeigen_mcp.models import SellerType
from kleinanzeigen_mcp.robots import Robots
from kleinanzeigen_mcp.urls import SearchQuery

ROBOTS = """
User-agent: *
Allow: /?utm_source=ekde$
Disallow: /*?utm_source=ekde
Disallow: /s-bestandsliste.html
Disallow: /*/sortierung:*
Disallow: /*/preis:*
Disallow: /*/l*r10
Disallow: /*/k0*r10
Disallow: /*.json
"""


def test_google_semantics():
    r = Robots(ROBOTS)
    assert r.allowed("/s-ddr5/k0")
    assert r.allowed("/s-anzeige/x/123-1-2")
    assert not r.allowed("/s-bestandsliste.html?userId=1")
    assert not r.allowed("/s-ort-empfehlungen.json?query=x")
    assert not r.allowed("/s-autos/sortierung:neuste/c216")
    assert not r.allowed("/s-berlin/ddr5/k0l3331r10")
    assert r.allowed("/s-berlin/ddr5/k0l3331")
    assert r.allowed("/?utm_source=ekde") and not r.allowed("/foo?utm_source=ekde")


async def test_strict_mode_filters_locally(fixture):
    html = fixture("search_new.html.gz")
    paths: list[str] = []

    def handler(req: httpx.Request):
        paths.append(req.url.raw_path.decode())
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    s = Settings(_env_file=None, rate_per_s=1000, burst=100, jitter_s=0, warmup=False, robots_policy="strict")
    c = KleinanzeigenClient(s, transport=httpx.MockTransport(handler))
    res = await c.search(SearchQuery(query="iphone", price_max=50, seller_type=SellerType.private))
    assert paths[:2] == ["/robots.txt", "/s-iphone/k0"]  # then up to 2 extra pages to fill the result
    assert all("preis:" not in p and "anbieter:" not in p for p in paths)
    assert res.ads and all(a.price.amount_eur <= 50 and a.seller_type == SellerType.private for a in res.ads)
    assert res.notes
    with pytest.raises(PolicyError):
        await c.get_seller("166595111")
    with pytest.raises(PolicyError):
        await c.resolve_location("Berlin")
    await c.aclose()
