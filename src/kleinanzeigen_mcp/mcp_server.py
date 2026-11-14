"""MCP tools on top of :class:`KleinanzeigenClient`.

Design notes (agent-facing tool design):
* few, clearly separated tools – each description says when to use it *and when not*;
* the search-engine rules of kleinanzeigen.de are spelled out where the agent writes the query;
* compact JSON by default (``response_format="detailed"`` for everything), no indentation;
* every response tells the agent how to continue (``next_page``, ``hint``s, short URLs);
* errors are actionable (what went wrong + what to try instead).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .auth import current_token_name
from .client import KleinanzeigenClient
from .http import BlockedError, NotFoundError, PolicyError, UpstreamError
from .models import AdDetail, AdSummary, AdType, Deal, SearchResult, SellerType, Sort
from .urls import SearchQuery

BASE = "https://www.kleinanzeigen.de"

INSTRUCTIONS = """\
Live data from Kleinanzeigen.de, Germany's largest classifieds marketplace (mostly private sellers).

Which tool:
- Browse / find items, newest first → search_listings
- "Best price", "cheapest", "is X a good deal", "worth buying?" → find_deals
- "Is this listing a scam / trustworthy?" for one ad → assess_listing
- Full text, specs, photos of specific ads → get_listing (pass up to 20 ids at once)
- Who is the seller, what else do they sell → get_seller
- "Tell me when something new appears" / repeated polling → check_new_listings

How kleinanzeigen search works (important for good queries):
- Write queries in GERMAN ("kühlschrank", not "fridge": 83 000 vs 284 hits). Umlauts, ue/ü,
  plurals and "rtx4090"/"rtx 4090" are normalised by the site.
- ALL words must match (AND). Keep queries short and distinctive: 1-3 words such as a model
  ("rtx 4090", "iphone 15 pro") plus maybe one spec ("32gb"). Every extra word removes results.
- There is NO OR, NO minus and NO phrase search. To exclude things use `exclude_terms`
  (e.g. ["hülle", "defekt"]); for alternatives run separate searches.
- The search also matches descriptions, so generic words return noise (e.g. holiday flats
  for "kühlschrank"). Set `category` for generic items; the response hints when results are
  spread over many categories.
- `location` accepts a city or PLZ; add `radius_km` for the surroundings. Results include the
  resolved place (`location_name`) – check it for ambiguous names such as Frankfurt.

Interpreting results:
- Prices are EUR; "VB" = negotiable, price may be just a placeholder. `posted` is Europe/Berlin.
- Promoted (paid) ads are hidden by default because they are often old.
- Risk scores from find_deals/assess_listing are heuristics: relay the reasons, recommend
  pickup or "Sicher bezahlen", but never call a seller a scammer.
- Calls are cached and rate limited server-side; repeating a call is cheap, a new search
  costs ~1-2 s, find_deals ~20-40 s.
"""

_RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)

# ------------------------------------------------------------- parameters
Query = Annotated[
    str | None,
    Field(description="German keywords, all must match (AND). Short and specific, e.g. 'rtx 4090', "
                      "'ddr5 32gb', 'rennrad 56'. No OR/minus/quotes – use exclude_terms instead."),
]
Exclude = Annotated[
    list[str] | None,
    Field(description="Drop ads whose TITLE contains any of these, e.g. ['hülle', 'defekt', 'tausch']."),
]
Loc = Annotated[str | None, Field(description="City, district or 5-digit PLZ, e.g. 'Berlin', '80331'.")]
Radius = Annotated[
    int | None, Field(description="km around `location`; snapped to 1,2,3,5,10,15,20,30,50,100,150,200.",
                      ge=1, le=200),
]
Cat = Annotated[
    str | None,
    Field(description="German category name or id, e.g. 'Autos', 'Fahrräder & Zubehör', "
                      "'PC-Zubehör & Software' (RAM, GPUs), 'Smartphones', 'Haushaltsgeräte', '173'. "
                      "Ambiguous names return the options; see list_categories."),
]
PMin = Annotated[int | None, Field(description="Minimum price in EUR.", ge=0)]
PMax = Annotated[int | None, Field(description="Maximum price in EUR.", ge=0)]
SellerT = Annotated[
    Literal["private", "commercial"] | None, Field(description="Only private or commercial sellers.")
]
AdT = Annotated[
    Literal["offer", "wanted"],
    Field(description="'offer' = items for sale (default), 'wanted' = Gesuche (people looking to buy)."),
]
Ship = Annotated[bool | None, Field(description="True = shipping offered, False = pickup only.")]
Fmt = Annotated[
    Literal["concise", "detailed"],
    Field(description="'concise' (default, ~5x fewer tokens) or 'detailed' (all fields, image URLs)."),
]


# ---------------------------------------------------------------- helpers
def _result(data: dict[str, Any]) -> CallToolResult:
    """Compact JSON text (no indentation, real umlauts) + structured content."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json", exclude_none=True)


def _short_url(ad_id: str) -> str:
    return f"{BASE}/s-anzeige/{ad_id}"


def _posted(a: AdSummary | AdDetail) -> str | None:
    if not a.posted_at:
        return getattr(a, "posted_text", None) or None
    has_time = ":" in (getattr(a, "posted_text", "") or "")
    return a.posted_at.strftime("%Y-%m-%d %H:%M" if has_time else "%Y-%m-%d")


def _ad(a: AdSummary, fmt: str = "concise") -> dict[str, Any]:
    if fmt == "detailed":
        return _dump(a)
    flags = [name for name, on in (
        ("shipping", a.shipping), ("buy_now", a.buy_now), ("wanted", a.ad_type == AdType.wanted),
        ("promoted", a.is_promoted), ("negotiable", a.price.negotiable), ("free", a.price.free),
    ) if on]
    d = {
        "id": a.id,
        "title": a.title,
        "price": a.price.text or None,
        "eur": a.price.amount_eur,
        "location": a.location or None,
        "km": a.distance_km,
        "posted": _posted(a),
        "seller": a.seller_type.value if a.seller_type else None,
        "seller_id": a.seller_id,
        "shop": a.shop_name,
        "flags": flags,
        "tags": a.tags,
        "teaser": a.description or None,
        "url": _short_url(a.id),
    }
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def _err(e: Exception) -> Exception:
    """Turn anticipated failures into ToolErrors so the model sees the reason and a way forward."""
    if isinstance(e, ToolError):
        return e
    if isinstance(e, BlockedError):
        return ToolError(
            f"Kleinanzeigen is rate-limiting this server; retry in ~{int(e.retry_after)}s. "
            "Do not retry immediately; cached results for the same query keep working."
        )
    if isinstance(e, NotFoundError):
        return ToolError(f"Not found: {e}. The ad was probably sold or deleted.")
    if isinstance(e, PolicyError):
        return ToolError(f"Not available in robots-strict mode: {e}")
    if isinstance(e, UpstreamError):
        return ToolError(f"Kleinanzeigen unreachable: {e}. Try again in a minute.")
    if isinstance(e, ValueError):
        return ToolError(f"Invalid input: {e}")
    return e


async def _search_hints(c: KleinanzeigenClient, res: SearchResult, category: str | None,
                        query: str | None) -> list[str]:
    hints: list[str] = []
    if not res.ads:
        words = len((query or "").split())
        hints.append(
            "No results. All words must match – try fewer/shorter German keywords"
            + (f" (your query has {words} words)" if words > 2 else "")
            + ", remove filters, or widen radius_km."
        )
        return hints
    if not category and len(res.ads) >= 10:
        counts = Counter(a.category_id for a in res.ads if a.category_id)
        if counts and counts.most_common(1)[0][1] / len(res.ads) < 0.5:
            try:
                names = {cat.id: cat.name for cat in await c.categories()}
            except Exception:
                names = {}
            top = ", ".join(f"{names.get(cid, cid)} ({n})" for cid, n in counts.most_common(4))
            hints.append(f"Results are spread over several categories: {top}. "
                         "Set `category` to narrow down.")
    return hints


def _deal(d: Deal) -> dict[str, Any]:
    out: dict[str, Any] = {**_ad(d.ad), "vs_median": d.price_vs_median, "saves_eur": d.savings_eur}
    if d.condition:
        out["condition"] = d.condition
    if d.risk:
        r = d.risk
        out["risk"] = {k: v for k, v in {
            "level": r.level, "score": r.score, "account_age_days": r.account_age_days,
            "reasons": r.reasons, "trust": r.positives, "hints": r.hints,
        }.items() if v not in (None, [])}
    if d.seller:
        s = d.seller
        out["seller_profile"] = {k: v for k, v in {
            "name": s.name, "rating": s.rating, "badges": s.badges, "active_since": s.active_since,
            "ads_online": s.ads_online,
        }.items() if v not in (None, [])}
    return out


# ------------------------------------------------------------------ tools
def build_mcp(get_client: Any) -> MCPServer:
    """``get_client`` is a zero-arg callable returning the shared client."""
    mcp = MCPServer("kleinanzeigen", instructions=INSTRUCTIONS, version="0.2.0")

    async def _query(c: KleinanzeigenClient, *, query, location, radius_km, category, price_min=None,
                     price_max=None, seller_type=None, ad_type="offer", shipping=None, buy_now=False,
                     sort=Sort.newest, page=1) -> SearchQuery:
        return SearchQuery(
            query=query or None,
            location_id=await c.location_id(location),
            radius_km=radius_km,
            category_id=await c.category_id(category),
            price_min=price_min,
            price_max=price_max,
            sort=Sort(sort),
            seller_type=SellerType(seller_type) if seller_type else None,
            ad_type=AdType(ad_type) if ad_type else None,
            shipping=shipping,
            buy_now=buy_now,
            page=page,
        )

    @mcp.tool(title="Kleinanzeigen: Anzeigen suchen", annotations=_RO)
    async def search_listings(
        query: Query = None,
        exclude_terms: Exclude = None,
        location: Loc = None,
        radius_km: Radius = None,
        category: Cat = None,
        price_min: PMin = None,
        price_max: PMax = None,
        sort: Annotated[
            Literal["newest", "price_asc", "price_desc", "relevance"],
            Field(description="'newest' (default), 'price_asc', 'price_desc' or 'relevance'. Note: price_asc "
                              "starts with placeholders like '1 € VB' – for real bargains use find_deals."),
        ] = "newest",
        max_age_minutes: Annotated[
            int | None,
            Field(description="Only ads posted within the last N minutes (needs sort='newest').", ge=1),
        ] = None,
        seller_type: SellerT = None,
        ad_type: AdT = "offer",
        shipping: Ship = None,
        buy_now: Annotated[bool, Field(description="Only ads with 'Direkt kaufen' (buyer protection).")] = False,
        include_promoted: Annotated[
            bool, Field(description="Also return paid/promoted ads (often old).")
        ] = False,
        page: Annotated[int, Field(description="Result page, 25 ads each (see next_page).", ge=1, le=50)] = 1,
        pages: Annotated[int, Field(description="Fetch this many pages at once (1-5).", ge=1, le=5)] = 1,
        limit: Annotated[int | None, Field(description="Return at most N ads.", ge=1, le=125)] = None,
        response_format: Fmt = "concise",
    ) -> dict[str, Any]:
        """Search current Kleinanzeigen listings (newest first by default) with filters.

        Use for browsing and finding items. Not for "best price" questions (use find_deals) and
        not for full ad text (use get_listing with the returned ids).

        Query rules: German keywords, ALL words must match, keep it to 1-3 distinctive words
        (e.g. 'rtx 4090', 'iphone 15 pro 256gb', 'kinderwagen bugaboo'). No OR / minus / quotes;
        use `exclude_terms` to drop unwanted titles. Set `category` for generic words.

        Returns: total hits, resolved location, ads (id, title, price, eur, location, posted,
        seller type/id, flags such as shipping/buy_now/negotiable, teaser, url), next_page, hints.
        At least one of query, category or location is required.
        """
        c = get_client()
        try:
            q = await _query(c, query=query, location=location, radius_km=radius_km, category=category,
                             price_min=price_min, price_max=price_max, seller_type=seller_type,
                             ad_type=ad_type, shipping=shipping, buy_now=buy_now, sort=sort, page=page)
            if sort == "newest":
                res = await c.latest(q, max_age_minutes=max_age_minutes, limit=limit, pages=pages,
                                     exclude_terms=exclude_terms, include_promoted=include_promoted)
            else:
                res = await c.search(q, pages=pages, include_promoted=include_promoted, limit=limit,
                                     exclude_terms=exclude_terms)
            hints = await _search_hints(c, res, category, query)
            if max_age_minutes and sort != "newest":
                hints.append("max_age_minutes is only applied with sort='newest'.")
        except Exception as e:
            raise _err(e) from e
        out: dict[str, Any] = {
            "total": res.total,
            "location_name": res.location_name,
            "count": len(res.ads),
            "ads": [_ad(a, response_format) for a in res.ads],
            "next_page": page + pages if res.has_next_page else None,
            "hints": hints + res.notes,
            "source_url": res.url,
        }
        return _result({k: v for k, v in out.items() if v not in (None, [])})

    @mcp.tool(title="Kleinanzeigen: Beste Angebote + Scam-Check", annotations=_RO)
    async def find_deals(
        query: Annotated[
            str, Field(description="German keywords for the item, e.g. 'ddr5 32gb', 'rtx 4090'.")
        ],
        must_include: Annotated[
            list[str] | None,
            Field(description="Title must contain every entry; '|' separates alternatives. Keeps the market "
                              "comparable, e.g. ['32gb|2x16', 'ddr5'] or ['15 pro', '256']."),
        ] = None,
        exclude_terms: Annotated[
            list[str] | None,
            Field(description="Drop titles containing any of these, e.g. ['sodimm', 'laptop'] or "
                              "['max', 'hülle']. Wanted ads, Ankauf, defekt, Tausch are always dropped."),
        ] = None,
        location: Loc = None,
        radius_km: Radius = None,
        category: Annotated[
            str | None,
            Field(description="Optional. If omitted, the category where the item itself is listed is picked "
                              "automatically (e.g. RAM, not PCs containing RAM)."),
        ] = None,
        price_max: PMax = None,
        seller_type: SellerT = None,
        shipping: Ship = None,
        check_top: Annotated[
            int, Field(description="Scam-check the N cheapest (2 requests each).", ge=0, le=10)
        ] = 5,
        limit: Annotated[int, Field(description="Max deals to return.", ge=1, le=50)] = 10,
        min_ratio: Annotated[
            float, Field(description="Ignore offers below this share of the median (placeholders).", ge=0.05, le=1)
        ] = 0.3,
    ) -> dict[str, Any]:
        """Find the cheapest realistic offers for an item, compared with the current market price,
        and scam-check the cheapest ones.

        Use for "cheapest / best deal / good price / is X worth it". Steps: (1) median price of ~75
        current comparable offers (outliers, placeholders, wanted ads removed); (2) cheapest offers
        between min_ratio×median and the median; (3) risk score 0-100 for the top `check_top`
        (account age, new account + attractive price, ratings, badges, possibly hijacked accounts,
        WhatsApp/PayPal-F&F/prepayment in text, photos). Takes 20-40 s.

        Make the comparison like-for-like with must_include/exclude_terms (e.g. desktop RAM:
        must_include ['32gb|2x16'], exclude_terms ['sodimm', 'laptop']). Check `market.sample_size`,
        `market.low_confidence` and `market.category_breakdown`; re-run with `category` if the
        auto-picked category is wrong. `best_low_risk_id` = cheapest checked deal with low risk.
        """
        c = get_client()
        try:
            q = await _query(c, query=query, location=location, radius_km=radius_km, category=category,
                             price_max=price_max, seller_type=seller_type, shipping=shipping)
            res = await c.find_deals(q, must_include=must_include, exclude=exclude_terms, check_top=check_top,
                                     limit=limit, min_ratio=min_ratio)
        except Exception as e:
            raise _err(e) from e
        m = res.market
        market = {k: v for k, v in {
            "median_eur": m.median_eur, "p25_eur": m.p25_eur, "p75_eur": m.p75_eur,
            "sample_size": m.sample_size, "low_confidence": m.low_confidence or None,
            "category_id": m.category_id, "category_breakdown": m.category_breakdown, "basis": m.basis,
        }.items() if v not in (None, [])}
        return _result({
            "market": market,
            "deals": [_deal(d) for d in res.deals],
            "checked": res.checked,
            "best_low_risk_id": res.best_low_risk_id,
            "excluded": res.excluded,
            "note": res.disclaimer,
        })

    @mcp.tool(title="Kleinanzeigen: Anzeige auf Scam prüfen", annotations=_RO)
    async def assess_listing(
        ad: Annotated[str, Field(description="Ad id (e.g. '3571224603') or kleinanzeigen URL.")],
        reference_query: Annotated[
            str | None, Field(description="Search for the market price, e.g. 'iphone 15 pro 256gb'. "
                                          "Default: derived from the ad title.")
        ] = None,
        must_include: Annotated[list[str] | None, Field(description="Title terms for comparable offers.")] = None,
    ) -> dict[str, Any]:
        """Scam-risk check for ONE listing: risk score 0-100 with reasons and trust signals, the
        seller's profile, and the price compared with similar current offers.

        Use when the user asks whether a specific ad is trustworthy. For finding good offers use
        find_deals (it already includes this check for the cheapest ones). Takes ~5-15 s.
        """
        try:
            r = await get_client().assess_listing(ad, reference_query=reference_query, must_include=must_include)
        except Exception as e:
            raise _err(e) from e
        d, risk, m = r["ad"], r["risk"], r["market"]
        return _result({
            "ad": {k: v for k, v in {
                "id": d.id, "title": d.title, "price": d.price.text, "eur": d.price.amount_eur,
                "location": d.location, "posted": _posted(d), "shipping": d.shipping,
                "photos": len(d.images), "url": _short_url(d.id),
            }.items() if v not in (None, "")},
            "risk": {k: v for k, v in {
                "level": risk.level, "score": risk.score, "vs_median": risk.price_vs_median,
                "account_age_days": risk.account_age_days, "reasons": risk.reasons,
                "trust": risk.positives, "hints": risk.hints,
            }.items() if v not in (None, [])},
            "seller": _dump(d.seller),
            "market": {k: v for k, v in {
                "median_eur": m.median_eur, "sample_size": m.sample_size,
                "low_confidence": m.low_confidence or None, "basis": m.basis,
            }.items() if v is not None},
            "note": "Heuristik aus öffentlichen Signalen – kein Beweis.",
        })

    @mcp.tool(title="Kleinanzeigen: Anzeigen-Details", annotations=_RO)
    async def get_listing(
        ads: Annotated[
            list[str],
            Field(description="1-20 ad ids or kleinanzeigen URLs (ids come from search results).",
                  min_length=1, max_length=20),
        ],
        include_views: Annotated[
            bool, Field(description="Also fetch the view counter (+1 request per ad).")
        ] = False,
    ) -> dict[str, Any]:
        """Full details of one or more listings: complete description, attributes (e.g. Zustand,
        Marke, km, Erstzulassung), all image URLs, coordinates, shipping cost, posting date and the
        seller's profile (rating, badges, active since, ads online).

        Use after search_listings when you need specifics. Batch ids in one call instead of
        calling repeatedly. Deleted/sold ads are reported per item as errors.
        """
        res = await get_client().get_ads(ads, include_views=include_views)
        items = []
        for r in res:
            if isinstance(r, dict):
                items.append(r)
                continue
            d = _dump(r)
            d.pop("cached", None)
            d["posted"] = _posted(r)
            d.pop("posted_at", None)
            items.append(d)
        return _result({"listings": items})

    @mcp.tool(title="Kleinanzeigen: Verkäufer", annotations=_RO)
    async def get_seller(
        user_id: Annotated[str, Field(description="Numeric seller id (`seller_id` from search results).")],
        page: Annotated[int, Field(description="Page of the seller's ads (25 each).", ge=1, le=20)] = 1,
    ) -> dict[str, Any]:
        """A seller's public profile (name, private/commercial, rating, badges, active since,
        response time) and their current listings.

        Use to judge a seller or to see what else they offer. Commercial shops without a numeric
        id cannot be looked up. Cached for 1 hour.
        """
        try:
            s = await get_client().get_seller(user_id, page=page)
        except Exception as e:
            raise _err(e) from e
        return _result({
            "profile": _dump(s.profile),
            "ads": [_ad(a) for a in s.ads],
            "next_page": page + 1 if len(s.ads) >= 25 else None,
        })

    @mcp.tool(title="Kleinanzeigen: Neue Anzeigen beobachten",
              annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True, idempotentHint=False))
    async def check_new_listings(
        watch_name: Annotated[
            str, Field(description="Your stable name for this saved search, e.g. 'ddr5-berlin'.")
        ],
        query: Query = None,
        exclude_terms: Exclude = None,
        location: Loc = None,
        radius_km: Radius = None,
        category: Cat = None,
        price_min: PMin = None,
        price_max: PMax = None,
        seller_type: SellerT = None,
        shipping: Ship = None,
        emit_initial: Annotated[
            bool, Field(description="On the first call also return the current listings (default true).")
        ] = True,
    ) -> dict[str, Any]:
        """Saved search: returns only listings this watch has not returned before.

        Use for "notify me / what's new since last time". Call again with the SAME watch_name
        and filters; it pages until it reaches already-seen ads, so nothing is lost between
        calls unless more than ~125 new ads appeared (then possible_gap=true). Watches live in
        server memory and are reset on restart.
        """
        c = get_client()
        try:
            q = await _query(c, query=query, location=location, radius_km=radius_km, category=category,
                             price_min=price_min, price_max=price_max, seller_type=seller_type, shipping=shipping)
            key = f"{current_token_name.get()}|{watch_name}|{q.path()}|{sorted(exclude_terms or [])}"
            new, first, gap = await c.check_watch(key, q, emit_initial=emit_initial)
        except Exception as e:
            raise _err(e) from e
        if exclude_terms:
            from .scoring import title_matches
            new = [a for a in new if title_matches(a.title, None, exclude_terms)]
        out: dict[str, Any] = {"watch": watch_name, "first_check": first, "new_count": len(new),
                               "ads": [_ad(a) for a in new]}
        if gap:
            out["possible_gap"] = True
            out["hint"] = "More new ads appeared than 5 pages hold – check more often."
        return _result(out)

    @mcp.tool(title="Kleinanzeigen: Ort auflösen", annotations=_RO)
    async def find_location(
        query: Annotated[str, Field(description="City, district or PLZ, e.g. 'Frankfurt', '10115'.")],
    ) -> dict[str, Any]:
        """Resolve a place name or PLZ to Kleinanzeigen locations (id + name).

        Only needed to disambiguate (e.g. 'Frankfurt' → Frankfurt am Main vs. Frankfurt (Oder));
        all search tools accept plain place names. Pass the chosen id as `location`.
        """
        try:
            locs = await get_client().resolve_location(query)
        except Exception as e:
            raise _err(e) from e
        return _result({"locations": [_dump(x) for x in locs]})

    @mcp.tool(title="Kleinanzeigen: Kategorien", annotations=_RO)
    async def list_categories(
        filter: Annotated[
            str | None, Field(description="Substring of the German category name, e.g. 'pc', 'fahrrad'.")
        ] = None,
    ) -> dict[str, Any]:
        """Kleinanzeigen category tree (id, name, parent, number of ads).

        Only needed if a category name passed to a search was not recognised; search tools accept
        German category names directly. Use `filter` – the full list has ~160 entries.
        """
        try:
            cats = await get_client().categories()
        except Exception as e:
            raise _err(e) from e
        if filter:
            f = filter.lower()
            cats = [c for c in cats if f in c.name.lower() or f in c.slug]
        return _result({"categories": [_dump(c) for c in cats]})

    return mcp
