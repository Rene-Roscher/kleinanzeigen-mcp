"""MCP tools on top of :class:`KleinanzeigenClient`."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .auth import current_token_name
from .client import KleinanzeigenClient
from .http import BlockedError, NotFoundError, PolicyError, UpstreamError
from .models import AdType, SellerType, Sort
from .urls import SearchQuery

INSTRUCTIONS = """\
Live access to Kleinanzeigen.de (Germany's largest classifieds site).

Typical flow:
1. `search_listings` / `latest_listings` – result lists (title, price, location, date,
   seller_id, seller_type, promoted flag). Free text `location` (city or PLZ) and
   `category` (name or id) are resolved automatically.
2. `get_listing` – full description, attributes, all images, seller profile for one ad.
3. `get_seller` – a seller's profile and other ads (cached for an hour).
4. `check_new_listings` – poll a saved search; returns only ads not seen before.
5. `find_deals` – cheapest realistic offers vs. the market median, top ones scam-risk scored.
   Pass `must_include` (e.g. ['32gb|2x16']) and `exclude` (e.g. ['sodimm','laptop']) so the
   market price compares like with like. `assess_listing` scores a single ad.
   Without `category` the comparison category is auto-picked (where the item itself is listed,
   not bundles containing it); check `market.category_breakdown` and re-run with `category` if needed.
   Risk scores are heuristics – always relay the reasons, never call a seller a scammer.

All calls are cached and rate limited server-side, so repeating a call is cheap.
Prices are EUR. `posted_at` is Europe/Berlin time. Promoted (paid TOP) ads can be
old even when listed first – `latest_listings` filters them out.
"""

_RO = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)

Query = Annotated[str | None, Field(description="Search keywords, e.g. 'ddr5 32gb' or 'rennrad 56'.")]
Loc = Annotated[str | None, Field(description="City, district or 5-digit PLZ, e.g. 'Berlin', '80331'.")]
Radius = Annotated[int | None, Field(description="Radius around location in km (1-200).", ge=0, le=200)]
Cat = Annotated[str | None, Field(description="Category name/slug/id, e.g. 'PC-Zubehör', 'autos', '173'.")]
PMin = Annotated[int | None, Field(description="Minimum price in EUR.", ge=0)]
PMax = Annotated[int | None, Field(description="Maximum price in EUR.", ge=0)]
SellerT = Annotated[Literal["private", "commercial"] | None, Field(description="Only private or commercial.")]
AdT = Annotated[Literal["offer", "wanted"] | None, Field(description="'offer' = Angebote, 'wanted' = Gesuche.")]
Ship = Annotated[bool | None, Field(description="True = only with shipping, False = only pickup.")]
BuyNow = Annotated[bool, Field(description="Only ads with 'Direkt kaufen'.")]
Limit = Annotated[int | None, Field(description="Max ads to return.", ge=1, le=125)]


def _dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _err(e: Exception) -> Exception:
    """Turn anticipated failures into ToolErrors so the model sees the actual reason."""
    if isinstance(e, ToolError):
        return e
    if isinstance(e, BlockedError):
        return ToolError(
            f"Kleinanzeigen is rate-limiting us right now; retry in ~{int(e.retry_after)}s. "
            "No cached data available for this request."
        )
    if isinstance(e, NotFoundError):
        return ToolError(f"Not found: {e}")
    if isinstance(e, PolicyError):
        return ToolError(f"Not available in robots-strict mode: {e}")
    if isinstance(e, UpstreamError):
        return ToolError(f"Kleinanzeigen unreachable: {e}")
    if isinstance(e, ValueError):
        return ToolError(f"Invalid input: {e}")
    return e


def build_mcp(get_client: Any) -> MCPServer:
    """``get_client`` is a zero-arg callable returning the shared client."""
    mcp = MCPServer("kleinanzeigen", instructions=INSTRUCTIONS, version="0.1.0")

    async def _query(
        client: KleinanzeigenClient, *, query, location, radius_km, category, price_min, price_max,
        seller_type, ad_type, shipping, buy_now, sort=Sort.newest, page=1,
    ) -> SearchQuery:
        return SearchQuery(
            query=query or None,
            location_id=await client.location_id(location),
            radius_km=radius_km,
            category_id=await client.category_id(category),
            price_min=price_min,
            price_max=price_max,
            sort=Sort(sort),
            seller_type=SellerType(seller_type) if seller_type else None,
            ad_type=AdType(ad_type) if ad_type else None,
            shipping=shipping,
            buy_now=buy_now,
            page=page,
        )

    @mcp.tool(annotations=_RO)
    async def search_listings(
        query: Query = None,
        location: Loc = None,
        radius_km: Radius = None,
        category: Cat = None,
        price_min: PMin = None,
        price_max: PMax = None,
        sort: Annotated[
            Literal["newest", "relevance", "price_asc", "price_desc"], Field(description="Sort order.")
        ] = "newest",
        seller_type: SellerT = None,
        ad_type: AdT = None,
        shipping: Ship = None,
        buy_now: BuyNow = False,
        page: Annotated[int, Field(description="Result page (25 ads each).", ge=1, le=50)] = 1,
        pages: Annotated[int, Field(description="Fetch this many consecutive pages (1-5).", ge=1, le=5)] = 1,
        include_promoted: Annotated[bool, Field(description="Include paid TOP/priority ads.")] = True,
        limit: Limit = None,
    ) -> dict[str, Any]:
        """Search Kleinanzeigen listings with filters. Returns summaries; use get_listing for details."""
        c = get_client()
        try:
            q = await _query(c, query=query, location=location, radius_km=radius_km, category=category,
                             price_min=price_min, price_max=price_max, seller_type=seller_type,
                             ad_type=ad_type, shipping=shipping, buy_now=buy_now, sort=sort, page=page)
            return _dump(await c.search(q, pages=pages, include_promoted=include_promoted, limit=limit))
        except Exception as e:
            raise _err(e) from e

    @mcp.tool(annotations=_RO)
    async def latest_listings(
        query: Query = None,
        location: Loc = None,
        radius_km: Radius = None,
        category: Cat = None,
        price_min: PMin = None,
        price_max: PMax = None,
        seller_type: SellerT = None,
        ad_type: AdT = None,
        shipping: Ship = None,
        buy_now: BuyNow = False,
        max_age_minutes: Annotated[
            int | None, Field(description="Only ads posted within the last N minutes.", ge=1)
        ] = None,
        pages: Annotated[int, Field(description="Scan this many pages (1-5).", ge=1, le=5)] = 1,
        limit: Limit = 25,
    ) -> dict[str, Any]:
        """The freshest organic listings for a search, newest first (paid placements removed)."""
        c = get_client()
        try:
            q = await _query(c, query=query, location=location, radius_km=radius_km, category=category,
                             price_min=price_min, price_max=price_max, seller_type=seller_type,
                             ad_type=ad_type, shipping=shipping, buy_now=buy_now)
            return _dump(await c.latest(q, max_age_minutes=max_age_minutes, limit=limit, pages=pages))
        except Exception as e:
            raise _err(e) from e

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True, idempotentHint=False))
    async def check_new_listings(
        watch_name: Annotated[str, Field(description="Your name for this saved search, e.g. 'ddr5-berlin'.")],
        query: Query = None,
        location: Loc = None,
        radius_km: Radius = None,
        category: Cat = None,
        price_min: PMin = None,
        price_max: PMax = None,
        seller_type: SellerT = None,
        ad_type: AdT = None,
        shipping: Ship = None,
        emit_initial: Annotated[
            bool, Field(description="On the first check, also return the current listings.")
        ] = True,
    ) -> dict[str, Any]:
        """Poll a saved search: returns only listings this watch has not returned before."""
        c = get_client()
        try:
            q = await _query(c, query=query, location=location, radius_km=radius_km, category=category,
                             price_min=price_min, price_max=price_max, seller_type=seller_type,
                             ad_type=ad_type, shipping=shipping, buy_now=False)
            key = f"{current_token_name.get()}|{watch_name}|{q.path()}"
            new, first, gap = await c.check_watch(key, q, emit_initial=emit_initial)
            out: dict[str, Any] = {"watch": watch_name, "first_check": first, "new_count": len(new),
                                   "ads": [_dump(a) for a in new]}
            if gap:
                out["possible_gap"] = True
                out["note"] = "more new ads appeared than 5 pages hold – check more often"
            return out
        except Exception as e:
            raise _err(e) from e

    @mcp.tool(annotations=_RO)
    async def find_deals(
        query: Annotated[str, Field(description="What you are looking for, e.g. 'ddr5 32gb'.")],
        must_include: Annotated[
            list[str] | None,
            Field(description="Every entry must occur in the title; '|' separates alternatives, "
                              "e.g. ['32gb|2x16', 'ddr5']. Use it to keep the market comparable."),
        ] = None,
        exclude: Annotated[
            list[str] | None,
            Field(description="Title substrings to drop, e.g. ['sodimm', 'laptop', 'gaming pc']. "
                              "Wanted ads, 'Ankauf', 'defekt' etc. are always dropped."),
        ] = None,
        location: Loc = None,
        radius_km: Radius = None,
        category: Cat = None,
        price_max: PMax = None,
        seller_type: SellerT = None,
        shipping: Ship = None,
        check_top: Annotated[
            int, Field(description="Risk-check the N cheapest (2 requests each).", ge=0, le=10)
        ] = 5,
        limit: Annotated[int, Field(description="Max deals to return.", ge=1, le=50)] = 15,
        min_ratio: Annotated[float, Field(description="Ignore offers below this share of the median "
                                                      "(placeholders like '1 € VB').", ge=0.05, le=1)] = 0.3,
    ) -> dict[str, Any]:
        """Cheapest realistic offers vs. the current market median, with a light scam-risk check.

        Builds a market price from current comparable offers (outliers trimmed), then lists the
        cheapest offers between min_ratio×median and the median. The top `check_top` get a risk score
        (0-100) from account age, ratings/badges, price vs. market, the seller's other ads (possible
        hijacked account) and red flags in the text (WhatsApp, PayPal F&F, 'nur Versand' …).
        Takes ~20-40 s because of the polite upstream rate limit.
        """
        c = get_client()
        try:
            q = await _query(c, query=query, location=location, radius_km=radius_km, category=category,
                             price_min=None, price_max=price_max, seller_type=seller_type, ad_type="offer",
                             shipping=shipping, buy_now=False)
            res = await c.find_deals(q, must_include=must_include, exclude=exclude, check_top=check_top,
                                     limit=limit, min_ratio=min_ratio)
            return _dump(res)
        except Exception as e:
            raise _err(e) from e

    @mcp.tool(annotations=_RO)
    async def assess_listing(
        ad: Annotated[str, Field(description="Ad id or kleinanzeigen URL.")],
        reference_query: Annotated[
            str | None, Field(description="Search used for the market price; default: derived from the title.")
        ] = None,
        must_include: Annotated[list[str] | None, Field(description="Title terms for comparable offers.")] = None,
    ) -> dict[str, Any]:
        """Scam-risk check for one listing: score, reasons, trust signals, and price vs. market."""
        try:
            r = await get_client().assess_listing(ad, reference_query=reference_query, must_include=must_include)
        except Exception as e:
            raise _err(e) from e
        d = r["ad"]
        return {
            "ad": {"id": d.id, "url": d.url, "title": d.title, "price": _dump(d.price),
                   "location": d.location, "posted_at": d.posted_at.isoformat() if d.posted_at else None,
                   "shipping": d.shipping, "images": len(d.images)},
            "seller": _dump(d.seller),
            "market": _dump(r["market"]),
            "risk": _dump(r["risk"]),
            "disclaimer": "Heuristik aus öffentlichen Signalen – kein Beweis.",
        }

    @mcp.tool(annotations=_RO)
    async def get_listing(
        ad: Annotated[str, Field(description="Ad id (e.g. '3571224603') or full kleinanzeigen URL.")],
        include_views: Annotated[bool, Field(description="Also fetch the view counter (+1 request).")] = False,
    ) -> dict[str, Any]:
        """Full details of one listing: description, attributes, images, coordinates, seller."""
        try:
            return _dump(await get_client().get_ad(ad, include_views=include_views))
        except Exception as e:
            raise _err(e) from e

    @mcp.tool(annotations=_RO)
    async def get_listings(
        ads: Annotated[list[str], Field(description="Up to 20 ad ids or URLs.", max_length=20)],
    ) -> dict[str, Any]:
        """Full details for several listings at once (errors are reported per item)."""
        res = await get_client().get_ads(ads)
        return {"results": [r if isinstance(r, dict) else _dump(r) for r in res]}

    @mcp.tool(annotations=_RO)
    async def get_seller(
        user_id: Annotated[str, Field(description="Numeric seller id (seller_id from search results).")],
        page: Annotated[int, Field(ge=1, le=20)] = 1,
    ) -> dict[str, Any]:
        """Seller profile (rating, badges, active since, response time) and their current listings."""
        try:
            return _dump(await get_client().get_seller(user_id, page=page))
        except Exception as e:
            raise _err(e) from e

    @mcp.tool(annotations=_RO)
    async def find_location(
        query: Annotated[str, Field(description="City / district / PLZ.")],
    ) -> list[dict[str, Any]]:
        """Resolve a place name or PLZ to Kleinanzeigen location ids."""
        try:
            return [_dump(x) for x in await get_client().resolve_location(query)]
        except Exception as e:
            raise _err(e) from e

    @mcp.tool(annotations=_RO)
    async def list_categories(
        filter: Annotated[str | None, Field(description="Optional substring to filter names.")] = None,
    ) -> list[dict[str, Any]]:
        """All Kleinanzeigen categories with ids (use the id or name as `category`)."""
        try:
            cats = await get_client().categories()
        except Exception as e:
            raise _err(e) from e
        if filter:
            f = filter.lower()
            cats = [c for c in cats if f in c.name.lower() or f in c.slug]
        return [_dump(c) for c in cats]

    @mcp.tool(annotations=_RO)
    async def service_status() -> dict[str, Any]:
        """Cache / rate-limit / upstream health of this server."""
        return get_client().status()

    return mcp
