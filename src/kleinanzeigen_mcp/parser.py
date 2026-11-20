"""HTML → models.

Kleinanzeigen currently serves two front-ends side by side:

* the new **Astro** search result page (Tailwind classes, no semantic CSS). Its
  ``<astro-island>`` props carry the result list as structured data – that is
  our primary, most robust source;
* the legacy **JSP** pages (``aditem`` / ``viewad-*`` ids) for ad details,
  seller lists and some A/B buckets of the search page.

Every list parser therefore tries the structured island first and falls back
to DOM parsing that understands both markups.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser, LexborNode

from .models import (
    AdDetail,
    AdSummary,
    AdType,
    Category,
    Location,
    Price,
    SellerSummary,
    SellerType,
)

BERLIN = ZoneInfo("Europe/Berlin")
BASE = "https://www.kleinanzeigen.de"

_WS = re.compile(r"\s+")
_ISLAND_RE = re.compile(r"<astro-island\b([^>]*)>", re.S)
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_TOTAL_RE = re.compile(r"(\d[\d.]*)\s*-\s*(\d[\d.]*)\s+von\s+(\d[\d.]*)")
_ZIP_RE = re.compile(r"\b(\d{5})\b")
_AD_ID_RE = re.compile(r"/(\d{6,})-\d+-\d+")


def clean(s: str | None) -> str:
    if not s:
        return ""
    if "&" in s:
        s = htmllib.unescape(s)  # '&#x2F;' etc. inside island JSON strings
    return _WS.sub(" ", s.replace("​", "").replace("\xa0", " ")).strip()


def absolute(url: str | None) -> str | None:
    if not url:
        return None
    return url if url.startswith("http") else BASE + url


_AD_CAT_RE = re.compile(r"/\d{6,}-(\d+)-\d+")


def category_from_url(url: str) -> str | None:
    """``/s-anzeige/x/3562841376-278-4303`` → ``278`` (the ad's leaf category)."""
    m = _AD_CAT_RE.search(url)
    return m.group(1) if m else None


def ad_id_from_url(url: str) -> str | None:
    m = _AD_ID_RE.search(url)
    return m.group(1) if m else None


# --------------------------------------------------------------------- astro
def astro_decode(v: Any) -> Any:
    """Decode Astro's island prop serialisation (``[type, value]`` tuples)."""
    if isinstance(v, list) and v and isinstance(v[0], int):
        if len(v) == 1:
            return None
        t, val = v[0], v[1]
        if t == 0:
            if isinstance(val, dict):
                return {k: astro_decode(x) for k, x in val.items()}
            return val
        if t == 1:
            return [astro_decode(x) for x in val]
        if t == 3:  # Date
            return val
        if t == 4:  # Map
            return {str(astro_decode(k)): astro_decode(x) for k, x in val}
        if t == 5:  # Set
            return [astro_decode(x) for x in val]
        return val
    if isinstance(v, dict):
        return {k: astro_decode(x) for k, x in v.items()}
    return v


def iter_islands(html: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for m in _ISLAND_RE.finditer(html):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        comp = attrs.get("component-url", "").rsplit("/", 1)[-1].split(".", 1)[0]
        raw = attrs.get("props")
        if not raw:
            continue
        try:
            props = json.loads(htmllib.unescape(raw))
        except ValueError:
            continue
        yield comp, {k: astro_decode(v) for k, v in props.items()}


def find_island(html: str, component: str) -> dict[str, Any] | None:
    return next((p for c, p in iter_islands(html) if c == component), None)


# ------------------------------------------------------------ small parsers
def parse_price(text: str | None, old: str | None = None) -> Price:
    t = clean(text)
    p = Price(text=t)
    low = t.lower()
    p.negotiable = "vb" in low.split() or low.endswith("vb")
    p.free = "verschenken" in low
    if m := re.search(r"(\d[\d.]*(?:,\d+)?)\s*€", t):
        p.amount_eur = float(m.group(1).replace(".", "").replace(",", "."))
    if old and (m := re.search(r"(\d[\d.]*(?:,\d+)?)\s*€", old)):
        p.old_amount_eur = float(m.group(1).replace(".", "").replace(",", "."))
    if p.free and p.amount_eur is None:
        p.amount_eur = 0.0
    return p


def parse_posted(text: str | None, now: datetime | None = None) -> datetime | None:
    t = clean(text)
    if not t:
        return None
    now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    hm = re.search(r"(\d{1,2}):(\d{2})", t)
    hour, minute = (int(hm.group(1)), int(hm.group(2))) if hm else (0, 0)
    low = t.lower()
    if low.startswith("heute"):
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if low.startswith("gestern"):
        d = now - timedelta(days=1)
        return d.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if m := re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", t):
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), hour, minute, tzinfo=BERLIN)
        except ValueError:
            return None
    return None


def parse_total(html: str) -> int | None:
    m = _TOTAL_RE.search(html)
    return int(m.group(3).replace(".", "")) if m else None


def has_next_page(html: str, page: int, total: int | None, n_ads: int) -> bool:
    if f"seite:{page + 1}/" in html or f"pageNum={page + 1}" in html:
        return True
    if total is not None:
        return page * 25 < total and n_ads > 0
    return False


# ------------------------------------------------------------- result lists
def _ad_from_island(o: dict[str, Any], *, with_distance: bool) -> AdSummary | None:
    ad_id = o.get("id")
    link = o.get("seoLink")
    if not ad_id or not link:
        return None
    attrs = [clean(a) for a in (o.get("attributes") or []) if clean(a)]
    features = set(o.get("appliedFeatures") or [])
    images = o.get("imageList") or []
    store = o.get("storeLink") or {}
    zip_code = clean(o.get("locationName"))
    parent = clean(o.get("parentLocationName"))
    shipping_txt = clean(o.get("shippingAvailableValue"))
    poster = (o.get("posterType") or "").upper()
    dist = o.get("distanceInKilometers")
    tags = [a for a in attrs if a not in ("Gesuch", "Direkt kaufen")]
    return AdSummary(
        id=str(ad_id),
        url=BASE + link,
        category_id=category_from_url(link),
        title=clean(o.get("title")),
        description=clean(o.get("description")),
        price=parse_price(o.get("price")),
        location=clean(f"{zip_code} {parent}"),
        zip_code=zip_code if zip_code.isdigit() else None,
        distance_km=float(dist) if with_distance and dist is not None else None,
        posted_at=parse_posted(o.get("sortingDate")),
        posted_text=clean(o.get("sortingDate")),
        image_url=(images[0].get("xLargeUrl") or images[0].get("adTableThumbnailLargeUrl")) if images else None,
        image_count=len(images),
        tags=tags,
        shipping=bool(shipping_txt),
        buy_now=bool(o.get("buyNowEnabled")) or "Direkt kaufen" in attrs,
        is_promoted=bool(o.get("topAd")) or bool(features & {"TOP_AD", "PRIORITY_AD"}),
        ad_type=AdType.wanted if "Gesuch" in attrs else AdType.offer,
        seller_id=str(o["userId"]) if o.get("userId") else None,
        seller_type=SellerType.commercial if poster == "COMMERCIAL"
        else SellerType.private if poster == "PRIVATE" else None,
        shop_name=clean(store.get("displayText")) or None,
        shop_url=absolute(store.get("url")),
    )


def _icon_text(node: LexborNode, title: str) -> str:
    svg = node.css_first(f'svg[data-title="{title}"]')
    return clean(svg.parent.text()) if svg is not None and svg.parent is not None else ""


def _ad_from_dom(art: LexborNode, *, with_distance: bool) -> AdSummary | None:
    ad_id = art.attributes.get("data-adid")
    href = art.attributes.get("data-href")
    if not ad_id or not href:
        a = art.css_first('a[href*="/s-anzeige/"]')
        href = href or (a.attributes.get("href") if a else None)
        if not ad_id or not href:
            return None
    legacy = "aditem" in (art.attributes.get("class") or "")

    if legacy:
        title_n = art.css_first("h2 a, .text-module-begin a")
        loc = clean(art.css_first(".aditem-main--top--left").text()) if art.css_first(
            ".aditem-main--top--left") else ""
        date = clean(art.css_first(".aditem-main--top--right").text()) if art.css_first(
            ".aditem-main--top--right") else ""
        desc_n = art.css_first(".aditem-main--middle--description")
        price_n = art.css_first(".aditem-main--middle--price-shipping--price")
        old_n = art.css_first(".aditem-main--middle--price-shipping--old-price")
        tags = [clean(t.text()) for t in art.css(".aditem-main--bottom .simpletag, .simpletag")]
        shop_a = art.css_first('a[href^="/pro/"]')
        top = art.css_first(".badge-topad, .icon-feature-topad") is not None
        cnt_n = art.css_first(".galleryimage--counter")
    else:
        title_n = next(
            (a for a in art.css(f'a[href="{href}"]') if a.css_first("img") is None and clean(a.text())), None
        )
        loc = _icon_text(art, "locationOutline")
        date = _icon_text(art, "calendarOutline")
        desc_n = next((p for p in art.css("p") if "€" not in p.text() and len(clean(p.text())) > 3), None)
        price_n = next((p for p in art.css("p") if re.search(r"€|VB|verschenken", p.text())), None)
        old_n = None
        tags = [clean(t.text()) for t in art.css("span[data-dhl-promotion]")]
        shop_a = art.css_first('a[href^="/pro/"]')
        top = False
        cnt_n = art.css_first("[data-image-container] div")

    img = art.css_first("img")
    dist = None
    if with_distance and (m := re.search(r"\((\d+(?:,\d+)?) km\)", loc)):
        dist = float(m.group(1).replace(",", "."))
        loc = clean(loc[: m.start()])
    zm = _ZIP_RE.search(loc)
    shipping = any("Versand" in t for t in tags)
    wanted = "Gesuch" in tags
    tags = [t for t in tags if t and t not in ("Gesuch", "Versand möglich", "Nur Abholung", "Direkt kaufen")]
    shop_name = None
    if shop_a is not None:
        title_attr = clean(shop_a.attributes.get("title") or "")
        shop_name = title_attr.removeprefix("Zum shop ").strip() or clean(shop_a.text()) or None
    cnt = clean(cnt_n.text()) if cnt_n is not None else ""
    return AdSummary(
        id=str(ad_id),
        url=BASE + href,
        category_id=category_from_url(href),
        title=clean(title_n.text()) if title_n is not None else "",
        description=clean(desc_n.text()) if desc_n is not None else "",
        price=parse_price(price_n.text() if price_n is not None else "", old_n.text() if old_n else None),
        location=loc,
        zip_code=zm.group(1) if zm else None,
        distance_km=dist,
        posted_at=parse_posted(date),
        posted_text=date,
        image_url=img.attributes.get("src") if img is not None else None,
        image_count=int(cnt) if cnt.isdigit() else (1 if img is not None else 0),
        tags=tags,
        shipping=shipping,
        is_promoted=top or (not date and not legacy),
        ad_type=AdType.wanted if wanted else AdType.offer,
        seller_type=SellerType.commercial if shop_a is not None else None,
        shop_name=shop_name,
        shop_url=absolute(shop_a.attributes.get("href")) if shop_a is not None else None,
    )


def parse_ad_list(html: str, *, with_distance: bool = False) -> list[AdSummary]:
    island = find_island(html, "ImpressionTracker")
    if island and isinstance(island.get("resultAds"), list):
        ads = []
        for item in island["resultAds"]:
            o = (item or {}).get("organicAdPreview")
            if o and (ad := _ad_from_island(o, with_distance=with_distance)):
                ads.append(ad)
        if ads:
            return ads
    tree = LexborHTMLParser(html)
    ads = []
    seen: set[str] = set()
    for art in tree.css("article[data-adid]"):
        ad = _ad_from_dom(art, with_distance=with_distance)
        if ad and ad.id not in seen:
            seen.add(ad.id)
            ads.append(ad)
    return ads


# --------------------------------------------------------------- ad detail
def _text_with_breaks(node: LexborNode | None) -> str:
    if node is None:
        return ""
    raw = re.sub(r"<br\s*/?>", "\n", node.html or "", flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    lines = [clean(x) for x in htmllib.unescape(raw).split("\n")]
    return "\n".join(lines).strip()


def _meta(tree: LexborHTMLParser, prop: str) -> str | None:
    n = tree.css_first(f'meta[property="{prop}"]') or tree.css_first(f'meta[name="{prop}"]')
    return n.attributes.get("content") if n is not None else None


def _seller_badges(scope: LexborNode) -> tuple[str | None, list[str]]:
    rating = None
    badges: list[str] = []
    for tag in scope.css(".userbadge-tag"):
        txt = clean(tag.text())
        if not txt:
            continue
        if "Zufriedenheit" in txt:
            rating = clean(txt.replace("Zufriedenheit", ""))
        else:
            badges.append(txt)
    return rating, badges


def parse_ad_detail(html: str, url: str) -> AdDetail:
    tree = LexborHTMLParser(html)

    title_n = tree.css_first("#viewad-title")
    if title_n is None:
        raise ValueError("not an ad detail page")
    id_box = tree.css_first("#viewad-ad-id-box")
    ad_id = None
    if id_box is not None and (m := re.search(r"\d{6,}", id_box.text())):
        ad_id = m.group(0)
    ad_id = ad_id or ad_id_from_url(url) or ""

    price_n = tree.css_first("#viewad-price")
    old_n = tree.css_first(".boxedarticle--old-price")
    locality = clean(tree.css_first("#viewad-locality").text()) if tree.css_first("#viewad-locality") else ""
    zm = _ZIP_RE.search(locality)

    extra = tree.css_first("#viewad-extra-info")
    posted = None
    if extra is not None and (m := re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", extra.text())):
        posted = parse_posted(m.group(0))

    crumbs = tree.css("#vap-brdcrmb a")
    categories, category_id = [], None
    for a in crumbs:
        href = a.attributes.get("href") or ""
        if m := re.search(r"/c(\d+)", href):
            categories.append(clean(a.text()))
            category_id = m.group(1)

    attributes: dict[str, str] = {}
    for li in tree.css("#viewad-details li.addetailslist--detail"):
        val_n = li.css_first(".addetailslist--detail--value")
        val = clean(val_n.text()) if val_n is not None else ""
        label = clean(li.text(deep=False)) or clean(li.text().replace(val, "", 1))
        if label:
            attributes[label] = val

    features = [clean(n.text()) for n in tree.css(".checktaglist .checktag") if clean(n.text())]
    ship_n = tree.css_first(".boxedarticle--details--shipping")

    images: list[str] = []
    seen_img: set[str] = set()
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            d = json.loads(s.text())
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "ImageObject" and d.get("contentUrl"):
            u = d["contentUrl"]
            if u.split("?", 1)[0] not in seen_img:
                seen_img.add(u.split("?", 1)[0])
                images.append(u)
    if not images:
        for img in tree.css(".galleryimage-element img, #viewad-image"):
            u = img.attributes.get("data-imgsrc") or img.attributes.get("src")
            if u and u not in images:
                images.append(u)

    seller = SellerSummary()
    contact = tree.css_first("#viewad-contact")
    if contact is not None:
        name_n = contact.css_first(".userprofile-vip")
        seller.name = clean(name_n.text()) if name_n is not None else None
        rating, badges = _seller_badges(contact)
        seller.rating, seller.badges = rating, badges
        for d in contact.css(".userprofile-vip-details-text"):
            t = clean(d.text())
            if t.startswith("Aktiv seit"):
                seller.active_since = t.removeprefix("Aktiv seit").strip()
            elif "Gewerblich" in t:
                seller.seller_type = SellerType.commercial
            elif "Privat" in t:
                seller.seller_type = SellerType.private
            elif t.startswith("Antwortet"):
                seller.response_time = t
        if (a := contact.css_first('a[href*="userId="]')) is not None:
            q = parse_qs(urlsplit(a.attributes.get("href") or "").query)
            if q.get("userId"):
                seller.user_id = q["userId"][0]
                seller.profile_url = f"{BASE}/s-bestandsliste.html?userId={seller.user_id}"
    box = tree.css_first("#viewad-profile-box") or contact
    if box is not None:
        if (shop := box.css_first('a[href^="/pro/"]')) is not None:
            seller.shop_url = absolute(shop.attributes.get("href"))
        if m := re.search(r"(\d+)\s+Anzeigen? online", box.text()):
            seller.ads_online = int(m.group(1))
    if seller.ads_online is None and (n := tree.css_first("#poster-other-ads-link")) is not None and (
        m := re.search(r"\d+", n.text())
    ):
        seller.ads_online = int(m.group(0))

    def _f(v: str | None) -> float | None:
        try:
            return float(v) if v else None
        except ValueError:
            return None

    return AdDetail(
        id=ad_id,
        url=_meta(tree, "og:url") or url,
        title=clean(title_n.text()),
        description=_text_with_breaks(tree.css_first("#viewad-description-text")),
        price=parse_price(price_n.text() if price_n is not None else "", old_n.text() if old_n else None),
        location=locality,
        zip_code=zm.group(1) if zm else None,
        region=_meta(tree, "og:region"),
        latitude=_f(_meta(tree, "og:latitude")),
        longitude=_f(_meta(tree, "og:longitude")),
        posted_at=posted,
        categories=categories,
        category_id=category_id,
        attributes=attributes,
        features=features,
        shipping=clean(ship_n.text()) if ship_n is not None else None,
        images=images,
        seller=seller,
        fetched_at=datetime.now(UTC),
    )


# ------------------------------------------------------------------ seller
def parse_seller_profile(html: str, user_id: str) -> SellerSummary:
    tree = LexborHTMLParser(html)
    p = SellerSummary(user_id=user_id, profile_url=f"{BASE}/s-bestandsliste.html?userId={user_id}")
    sec = tree.css_first("section.userprofile")
    if sec is None:
        return p
    if (n := sec.css_first(".userprofile--name")) is not None:
        p.name = clean(n.text().replace("Profil von", "", 1))
    p.rating, p.badges = _seller_badges(sec)
    for d in sec.css(".userprofile-details-text"):
        t = clean(d.text())
        if t.startswith("Aktiv seit"):
            p.active_since = t.removeprefix("Aktiv seit").strip()
        elif "Gewerblich" in t:
            p.seller_type = SellerType.commercial
        elif "Privat" in t:
            p.seller_type = SellerType.private
        elif t.startswith("Antwortet"):
            p.response_time = t
    if (shop := tree.css_first('section.userprofile a[href^="/pro/"]')) is not None:
        p.shop_url = absolute(shop.attributes.get("href"))
    return p


# -------------------------------------------------------------- reference
def parse_locations(payload: str) -> list[Location]:
    data = json.loads(payload)
    out = []
    for k, v in data.items():
        lid = k.lstrip("_")
        if lid.isdigit() and lid != "0":
            out.append(Location(id=lid, name=clean(v)))
    return out


def parse_categories(html: str) -> list[Category]:
    tree = LexborHTMLParser(html)
    out: list[Category] = []
    rx = re.compile(r"^/s-([^/]+)/(?:[^/]+/)?c(\d+)")

    def _count(node: LexborNode) -> int | None:
        span = node.next
        while span is not None and span.tag == "-text":
            span = span.next
        if span is not None and span.tag == "span" and (m := re.search(r"[\d.]+", span.text())):
            return int(m.group(0).replace(".", ""))
        return None

    for block in tree.css("ul.treelist > li"):
        head = block.css_first("h2 a")
        if head is None or not (m := rx.match(head.attributes.get("href") or "")):
            continue
        parent_id = m.group(2)
        out.append(Category(id=parent_id, name=clean(head.text()), slug=m.group(1), count=_count(head)))
        for a in block.css("ul > li > a"):
            if mm := rx.match(a.attributes.get("href") or ""):
                out.append(Category(id=mm.group(2), name=clean(a.text()), slug=mm.group(1),
                                    parent_id=parent_id, count=_count(a)))
    return out
