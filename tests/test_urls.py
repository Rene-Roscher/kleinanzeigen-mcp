import pytest

from kleinanzeigen_mcp.models import AdType, SellerType, Sort
from kleinanzeigen_mcp.urls import SearchQuery


@pytest.mark.parametrize(
    "q,expected",
    [
        (SearchQuery(query="ddr5"), "/s-sortierung:neuste/ddr5/k0"),
        (SearchQuery(query="iPhone 13  Pro", sort=Sort.relevance), "/s-iphone-13-pro/k0"),
        (SearchQuery(category_id=216, price_min=1000, price_max=5000), "/s-sortierung:neuste/preis:1000:5000/c216"),
        (
            SearchQuery(query="iphone 13 pro", location_id=3331, radius_km=7, seller_type=SellerType.private,
                        ad_type=AdType.offer, shipping=True),
            "/s-sortierung:neuste/anbieter:privat/anzeige:angebote/versand:ja/iphone-13-pro/k0l3331r5",
        ),
        (SearchQuery(query="fahrrad", sort=Sort.price_desc, page=2), "/s-sortierung:teuerste/seite:2/fahrrad/k0"),
        (SearchQuery(query="sofa", buy_now=True, price_max=200), "/s-sortierung:neuste/direktkaufen:aktiv/preis::200/sofa/k0"),
        (SearchQuery(category_id=173, location_id=3331), "/s-sortierung:neuste/c173l3331"),
        (SearchQuery(query="über größe"), "/s-sortierung:neuste/%C3%BCber-gr%C3%B6%C3%9Fe/k0"),
    ],
)
def test_paths(q, expected):
    assert q.path() == expected


def test_extra_filter_validation():
    assert SearchQuery(query="x", category_id=173, extra={"global.zustand": "like_new"}).path().endswith(
        "k0c173+global.zustand:like_new"
    )
    with pytest.raises(ValueError):
        SearchQuery(query="x", extra={"a/b": "x"}).path()
