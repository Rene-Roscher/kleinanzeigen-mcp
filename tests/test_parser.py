from datetime import datetime

from kleinanzeigen_mcp import parser as P
from kleinanzeigen_mcp.models import AdType, SellerType


def test_search_island(fixture):
    html = fixture("search_new.html.gz")
    ads = P.parse_ad_list(html)
    assert len(ads) == 27
    assert P.parse_total(html) == 159120
    by_id = {a.id: a for a in ads}

    a = by_id["3571224603"]
    assert a.title == "iPhone 11 (Defekt)"
    assert a.price.amount_eur == 25 and not a.price.negotiable
    assert a.zip_code == "22081" and "Barmbek" in a.location
    assert a.posted_text == "Heute, 15:07" and a.posted_at is not None
    assert a.seller_id == "166595111" and a.seller_type == SellerType.private
    assert a.image_count == 2 and a.image_url.startswith("https://img.kleinanzeigen.de/")
    assert not a.is_promoted and a.ad_type == AdType.offer

    top = by_id["3520214988"]
    assert top.is_promoted and top.seller_type == SellerType.commercial
    assert top.shop_url.endswith("/pro/Handy-Reparatur-Express-iPhone-6-6S-7-8-Plus-X-Display-Reparatur")

    wanted = by_id["2155237497"]
    assert wanted.ad_type == AdType.wanted and wanted.shipping

    buy = by_id["3571224296"]
    assert buy.buy_now and "Direkt kaufen" not in buy.tags


def test_search_autos_tags(fixture):
    ads = P.parse_ad_list(fixture("search_autos.html.gz"))
    golf = next(a for a in ads if a.id == "3571228635")
    assert golf.tags == ["50.785 km", "EZ 11/2021"]
    assert golf.price.amount_eur == 21750


def test_search_distance(fixture):
    ads = P.parse_ad_list(fixture("search_berlin_price.html.gz"), with_distance=True)
    assert all(a.distance_km is not None for a in ads)
    assert all(100 <= (a.price.amount_eur or 0) <= 500 for a in ads if not a.is_promoted)


def test_dom_fallback_new_markup(fixture):
    html = fixture("search_new.html.gz").replace("ImpressionTracker", "Disabled")
    ads = P.parse_ad_list(html)
    assert len(ads) == 27
    a = next(x for x in ads if x.id == "3571224296")
    assert a.title.lower().startswith("iphone 11")
    assert a.price.amount_eur == 100
    assert a.posted_text == "Heute, 15:07"
    pro = next(x for x in ads if x.id == "3520214988")
    assert pro.shop_name and pro.shop_name != "PRO"


def test_legacy_list_markup(fixture):
    ads = P.parse_ad_list(fixture("seller_private.html.gz"))
    assert len(ads) == 8
    assert ads[0].title == "iPhone 11 (Defekt)"
    assert ads[0].price.amount_eur == 25
    assert ads[0].zip_code == "22081"


def test_ad_detail_private(fixture):
    d = P.parse_ad_detail(fixture("ad_private.html.gz"), "u")
    assert d.id == "3571224603"
    assert d.attributes["Marke"] == "Apple" and d.attributes["Speicher (GB)"] == "64 GB"
    assert d.category_id == "173" and d.categories[-1] == "Smartphones"
    assert d.latitude and d.longitude and d.region == "Hamburg"
    assert d.shipping == "Nur Abholung"
    assert "\n" in d.description
    assert len(d.images) == len({u.split("?")[0] for u in d.images}) > 1
    s = d.seller
    assert s.user_id == "166595111" and s.seller_type == SellerType.private
    assert s.rating == "NA JA" and s.ads_online == 8 and s.active_since == "17.09.2026"


def test_ad_detail_commercial(fixture):
    d = P.parse_ad_detail(fixture("ad_commercial.html.gz"), "u")
    assert d.seller.seller_type == SellerType.commercial
    assert d.seller.rating == "OK" and "Zuverlässig" in d.seller.badges
    assert d.seller.shop_url and d.seller.ads_online == 45


def test_seller_profile(fixture):
    p = P.parse_seller_profile(fixture("seller_private.html.gz"), "166595111")
    assert p.name == "Privat" and p.seller_type == SellerType.private
    assert p.response_time and "3 Stunden" in p.response_time


def test_categories_and_locations(fixture):
    cats = P.parse_categories(fixture("categories.html.gz"))
    assert len(cats) == len({c.id for c in cats}) > 100
    phone = next(c for c in cats if c.id == "173")
    assert phone.parent_id == "161" and phone.slug == "handy-telekom"
    locs = P.parse_locations(fixture("locations_berlin.json"))
    assert locs[0].id == "3331" and locs[0].name == "Berlin"


def test_price_parsing():
    assert P.parse_price("1.234 € VB").amount_eur == 1234
    assert P.parse_price("1.234 € VB").negotiable
    p = P.parse_price(" VB")
    assert p.amount_eur is None and p.negotiable
    assert P.parse_price("Zu verschenken").free
    assert P.parse_price("12,50 €").amount_eur == 12.5


def test_posted_parsing():
    now = datetime(2026, 11, 14, 15, 30, tzinfo=P.BERLIN)
    assert P.parse_posted("Heute, 09:05", now) == now.replace(hour=9, minute=5, second=0)
    assert P.parse_posted("Gestern, 23:59", now).day == 13
    assert P.parse_posted("01.02.2026", now) == datetime(2026, 2, 1, tzinfo=P.BERLIN)
    assert P.parse_posted("", now) is None
