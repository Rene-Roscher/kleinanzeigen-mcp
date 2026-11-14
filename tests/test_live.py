"""Smoke tests against the real site. Run sparingly: ``uv run pytest -m live``."""

import pytest

from kleinanzeigen_mcp.client import KleinanzeigenClient
from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.urls import SearchQuery

pytestmark = pytest.mark.live


async def test_live_search_detail_seller():
    async with KleinanzeigenClient(Settings(_env_file=None)) as c:
        res = await c.latest(SearchQuery(query="ddr5"), limit=5)
        assert res.total and res.total > 100
        assert len(res.ads) == 5
        a = res.ads[0]
        assert a.title and a.url.startswith("https://www.kleinanzeigen.de/s-anzeige/")
        assert a.posted_at is not None and a.seller_type is not None

        d = await c.get_ad(a.id)
        assert d.id == a.id and d.description and d.images

        if a.seller_id:
            s = await c.get_seller(a.seller_id)
            assert s.profile.user_id == a.seller_id

        assert (await c.location_id("München")) == "6411"
        assert any(x.id == "225" for x in await c.categories())
