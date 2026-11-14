from datetime import datetime, timedelta

import httpx

from kleinanzeigen_mcp import scoring
from kleinanzeigen_mcp.client import KleinanzeigenClient, _reference_from_title
from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.models import AdDetail, AdSummary, AdType, Price, Seller, SellerSummary, SellerType
from kleinanzeigen_mcp.parser import BERLIN
from kleinanzeigen_mcp.urls import SearchQuery

NOW = datetime(2026, 11, 14, 15, 0, tzinfo=BERLIN)


def since(days: int) -> str:
    return (NOW - timedelta(days=days)).strftime("%d.%m.%Y")


def detail(price=300.0, desc="Verkaufe meinen RAM, läuft einwandfrei. Abholung oder Versand möglich.",
           title="Corsair Vengeance DDR5 32GB (2x16GB) 6000", images=6, shipping="+ Versand ab 4,99 €",
           **seller) -> AdDetail:
    return AdDetail(
        id="1", url="u", title=title, description=desc, price=Price(amount_eur=price, text=f"{price} €"),
        images=[f"i{n}" for n in range(images)], shipping=shipping,
        seller=SellerSummary(seller_type=SellerType.private, **seller), fetched_at=NOW,
    )


def summary(i: int, price: float, *, title="DDR5 32GB", posted_h: float = 100, zip_code="10115",
            ad_type=AdType.offer) -> AdSummary:
    return AdSummary(id=str(i), url="u", title=title, price=Price(amount_eur=price), zip_code=zip_code,
                     posted_at=NOW - timedelta(hours=posted_h), ad_type=ad_type)


def test_new_account_with_perfect_price_is_high_risk():
    r = scoring.assess(detail(price=420, active_since=since(1)), median_eur=450, now=NOW)
    assert r.level == "high" and r.account_age_days == 1
    assert any("Betrugsmuster" in x for x in r.reasons)


def test_established_seller_is_low_risk_even_if_cheap():
    r = scoring.assess(detail(price=280, active_since=since(9 * 365), rating="TOP",
                              badges=["Sehr freundlich", "Besonders zuverlässig"]), median_eur=450, now=NOW)
    assert r.level == "low" and r.score == 0
    assert any("unter Marktmedian" in x for x in r.reasons)  # still transparent about the price


def test_way_too_cheap_and_red_flags():
    desc = "Neu und OVP. Zahlung nur per PayPal Freunde und Familie. Bin zurzeit im Ausland, schreib mir auf WhatsApp."
    r = scoring.assess(detail(price=150, desc=desc, active_since=since(400)), median_eur=450, now=NOW)
    assert r.level == "high"
    text = " ".join(r.reasons)
    assert "Käuferschutz" in text and "außerhalb" in text and "Ausland" in text and "zu gut" in text


def test_possibly_hijacked_old_account():
    seller = Seller(
        profile=SellerSummary(user_id="1"),
        ads=[summary(i, 400, posted_h=5 + i, zip_code=z) for i, z in enumerate(["10115", "80331", "50667", "20095"])],
        fetched_at=NOW,
    )
    r = scoring.assess(detail(active_since=since(8 * 365), rating="OK"), median_eur=450, seller=seller, now=NOW)
    text = " ".join(r.reasons)
    assert "gekapertes Konto" in text and "PLZ-Regionen" in text


def test_no_false_positives_for_normal_texts():
    desc = ("Ich verkaufe zwei Riegel, HDMI-Signal am PC war stabil. Montage kein Problem. "
            "Zahlung per Überweisung oder PayPal (Waren und Dienstleistungen). Rechnung vorhanden.")
    r = scoring.assess(detail(desc=desc, active_since=since(5 * 365), rating="TOP"), median_eur=320, now=NOW)
    assert r.level == "low" and not r.reasons
    assert not any("Titel nennt" in h for h in r.hints)


def test_capacity_mismatch_hint_but_not_for_kits():
    real = scoring.assess(detail(title="Samsung 32GB DDR5", desc="Kapazität: 16GB, DDR5-5600"), now=NOW)
    assert any("Titel nennt 32 GB" in h for h in real.hints)
    kit = scoring.assess(detail(title="DDR5 RAM 32GB (2x16GB)", desc="Zwei Module à 16GB"), now=NOW)
    assert not any("Titel nennt" in h for h in kit.hints)


def test_per_unit_price_hint():
    r = scoring.assess(detail(title="16GB - 32GB DDR5", desc="Preis pro Riegel"), now=NOW)
    assert any("pro Stück" in h for h in r.hints)


def test_market_stats_trims_outliers_and_placeholders():
    ads = [summary(i, p) for i, p in enumerate([1, 2, 420, 430, 440, 450, 455, 460, 470, 480, 9999])]
    ads.append(summary(99, 10, ad_type=AdType.wanted))
    st = scoring.market_stats(ads, "x")
    assert st.median_eur == 452.5 and st.sample_size == 8 and st.outliers_removed == 1
    assert scoring.is_placeholder_price(1, 450) and scoring.is_placeholder_price(40, 450)
    assert not scoring.is_placeholder_price(200, 450)


def test_title_matching():
    assert scoring.title_matches("Kingston Fury 2x16GB DDR5", ["32gb|2x16", "ddr5"], None)
    assert not scoring.title_matches("Kingston Fury 16GB DDR5", ["32gb|2x16"], None)
    assert not scoring.title_matches("Suche DDR5 32GB", None, list(scoring.DEFAULT_EXCLUDE))
    assert _reference_from_title("Corsair Vengeance 32GB DDR5 6000 *NEU*") == "corsair vengeance 32gb ddr5"


async def test_find_deals_end_to_end(fixture):
    intact = fixture("ad_private.html.gz").replace("Defekt", "Gut")
    pages = {"search": fixture("search_new.html.gz"), "ad": intact, "seller": fixture("seller_private.html.gz")}

    def handler(r: httpx.Request):
        kind = "ad" if r.url.path.startswith("/s-anzeige/") else (
            "seller" if "bestandsliste" in r.url.path else "search")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=pages[kind])

    s = Settings(_env_file=None, rate_per_s=1000, burst=100, jitter_s=0, warmup=False)
    c = KleinanzeigenClient(s, transport=httpx.MockTransport(handler))
    res = await c.find_deals(SearchQuery(query="iphone"), check_top=2, limit=5)
    assert res.market.median_eur and res.market.sample_size >= 5
    assert 0 < len(res.deals) <= 5 and res.checked == min(2, len(res.deals))
    assert res.market.category_id == "173"  # phones, not the cases/repair ads on the same page
    prices = [d.ad.price.amount_eur for d in res.deals]
    assert prices == sorted(prices)
    assert all(d.price_vs_median <= 1.0 for d in res.deals)
    assert res.deals[0].risk and res.deals[0].risk.reasons is not None
    assert res.excluded["wanted"] + res.excluded["title_filter"] >= 1  # 'ANKAUF …' / Gesuch dropped
    await c.aclose()


def test_pick_category_prefers_the_item_over_bundles():
    pcs = [AdSummary(id=f"p{i}", url="u", title=f"Mini PC Ryzen 9 7940HS 1TB SSD WiFi 32GB DDR5 #{i}",
                     price=Price(amount_eur=700), category_id="228") for i in range(12)]
    ram = [AdSummary(id=f"r{i}", url="u", title=f"32GB DDR5 Kit Corsair #{i}",
                     price=Price(amount_eur=300), category_id="225") for i in range(7)]
    cat, breakdown = scoring.pick_category(pcs + ram, ["32gb|2x16", "ddr5"])
    assert cat == "225"  # fewer ads, but the searched item itself
    assert {b["category_id"] for b in breakdown} == {"225", "228"}


async def test_find_deals_drops_defective_items_after_check(fixture):
    pages = {"search": fixture("search_new.html.gz"), "ad": fixture("ad_private.html.gz"),
             "seller": fixture("seller_private.html.gz")}

    def handler(r: httpx.Request):
        kind = "ad" if r.url.path.startswith("/s-anzeige/") else (
            "seller" if "bestandsliste" in r.url.path else "search")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=pages[kind])

    s = Settings(_env_file=None, rate_per_s=1000, burst=100, jitter_s=0, warmup=False)
    c = KleinanzeigenClient(s, transport=httpx.MockTransport(handler))
    res = await c.find_deals(SearchQuery(query="iphone"), check_top=2, limit=5)
    assert res.excluded.get("defect_after_check", 0) >= 1 and res.best_low_risk_id is None
    await c.aclose()
