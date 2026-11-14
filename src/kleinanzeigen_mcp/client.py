"""High level, cached access to kleinanzeigen.de."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

import httpx

from . import parser, scoring
from .cache import AsyncTTLCache
from .config import Settings, get_settings
from .http import Fetcher, NotFoundError, PolicyError
from .models import (
    AdDetail,
    AdSummary,
    AdType,
    Category,
    Deal,
    DealsResult,
    Location,
    MarketStats,
    RiskAssessment,
    SearchResult,
    Seller,
    Sort,
)
from .urls import SearchQuery, keyword_slug

log = logging.getLogger(__name__)

MAX_PAGES = 5  # per call; kleinanzeigen itself stops at 50 pages of 25


class _Watch:
    __slots__ = ("seen", "created", "last_check")

    def __init__(self) -> None:
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.created = time.time()
        self.last_check = 0.0

    def remember(self, ids: list[str], cap: int = 2000) -> None:
        for i in ids:
            self.seen[i] = None
            self.seen.move_to_end(i)
        while len(self.seen) > cap:
            self.seen.popitem(last=False)


class KleinanzeigenClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings or get_settings()
        self.fetcher = Fetcher(self.s, transport=transport)
        self.cache = AsyncTTLCache(
            self.s.cache_max_entries, self.s.stale_max_age_s, no_stale_for=(NotFoundError, PolicyError)
        )
        self._watches: OrderedDict[str, _Watch] = OrderedDict()
        self._watch_lock = asyncio.Lock()
        self._location_names: dict[str, str] = {}

    async def aclose(self) -> None:
        await self.fetcher.aclose()

    async def __aenter__(self) -> KleinanzeigenClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------- search
    async def _search_page(self, q: SearchQuery) -> tuple[SearchResult, bool]:
        path = q.path()

        async def load() -> SearchResult:
            res = await self.fetcher.get_page(path)
            html = res.text
            ads = parser.parse_ad_list(html, with_distance=bool(q.location_id and q.radius_km))
            total = parser.parse_total(html)
            if total is None and not ads:
                total = 0
            return SearchResult(
                url=self.s.base_url + path,
                total=total,
                page=q.page,
                has_next_page=parser.has_next_page(html, q.page, total, len(ads)),
                ads=ads,
                fetched_at=datetime.now(UTC),
            )

        return await self.cache.get_or_load("search:" + path, self.s.ttl_search, load)

    async def search(
        self,
        q: SearchQuery,
        *,
        pages: int = 1,
        include_promoted: bool = True,
        limit: int | None = None,
    ) -> SearchResult:
        """Run a search. ``pages`` > 1 walks consecutive result pages (sequentially)."""
        if q.query is not None and not keyword_slug(q.query):
            q = SearchQuery(**{**_asdict(q), "query": None})  # blank / punctuation-only keyword
        if not q.query and not q.category_id and not q.location_id:
            raise ValueError("give at least a query, a category or a location")
        pages = max(1, min(MAX_PAGES, pages))
        wanted = q
        notes: list[str] = []
        strict = self.s.robots_policy == "strict"
        if strict:
            # robots.txt disallows filter URLs: fetch the plain listing, filter/sort locally.
            q = SearchQuery(query=q.query, category_id=q.category_id, location_id=q.location_id,
                            sort=Sort.relevance, page=q.page)
            notes.append("robots strict mode: price/seller/ad type/shipping filters and sorting were "
                         "applied locally to the fetched page(s); results come from relevance order, "
                         "totals are unfiltered")
            if wanted.radius_km:
                notes.append("robots strict mode: radius is not supported – searched the whole location")

        seen: set[str] = set()
        out: list[AdSummary] = []

        def take(ads: list[AdSummary]) -> None:
            for a in ads:
                if a.id in seen or (not include_promoted and a.is_promoted):
                    continue
                if strict and not _matches(a, wanted):
                    continue
                seen.add(a.id)
                out.append(a)

        first, all_cached = await self._search_page(q)
        take(first.ads)
        last, page = first, q.page
        while pages > 1 and last.has_next_page and (limit is None or len(out) < limit):
            pages -= 1
            page += 1
            last, c = await self._search_page(SearchQuery(**{**_asdict(q), "page": page}))
            all_cached = all_cached and c
            take(last.ads)

        if strict and wanted.sort != Sort.relevance:
            out = _sort_local(out, wanted.sort)
        if limit is not None:
            out = out[:limit]
        return first.model_copy(
            update={
                "ads": out,
                "page": q.page,
                "has_next_page": last.has_next_page,
                "cached": all_cached,
                "location_name": self._location_names.get(str(q.location_id)) if q.location_id else None,
                "notes": notes,
            }
        )

    async def latest(
        self, q: SearchQuery, *, max_age_minutes: int | None = None, limit: int | None = 25, pages: int = 1
    ) -> SearchResult:
        """Newest organic listings first (paid TOP/priority placements removed)."""
        q = SearchQuery(**{**_asdict(q), "sort": Sort.newest, "page": 1})
        res = await self.search(q, pages=pages, include_promoted=False)
        ads = _sort_local(res.ads, Sort.newest)
        if max_age_minutes is not None:
            cutoff = datetime.now(parser.BERLIN) - timedelta(minutes=max_age_minutes)
            ads = [a for a in ads if a.posted_at is None or a.posted_at >= cutoff]
        if limit is not None:
            ads = ads[:limit]
        return res.model_copy(update={"ads": ads})

    async def check_watch(
        self, watch_key: str, q: SearchQuery, *, emit_initial: bool = False, max_pages: int = MAX_PAGES
    ) -> tuple[list[AdSummary], bool, bool]:
        """Return only ads not seen before for this watch: ``(new_ads, first_check, possible_gap)``.

        Pages through the newest listings until it reaches an ad it has already
        seen, so nothing is missed between two checks unless more than
        ``max_pages`` pages (~25 ads each) appeared – then ``possible_gap`` is True.
        On the first check page 1 becomes the baseline (returned only with ``emit_initial``).
        """
        base = SearchQuery(**{**_asdict(q), "sort": Sort.newest, "page": 1})
        async with self._watch_lock:
            w = self._watches.get(watch_key)
            first = w is None
            seen = set(w.seen) if w else set()

        collected: list[AdSummary] = []
        possible_gap = False
        for page in range(1, (1 if first else max_pages) + 1):
            res = await self.search(SearchQuery(**{**_asdict(base), "page": page}), include_promoted=False)
            collected.extend(res.ads)
            if first or any(a.id in seen for a in res.ads) or not res.has_next_page:
                break
        else:
            possible_gap = True

        async with self._watch_lock:
            w = self._watches.get(watch_key)
            if w is None:
                w = self._watches[watch_key] = _Watch()
                while len(self._watches) > 1000:
                    self._watches.popitem(last=False)
            uniq = {a.id: a for a in collected}
            new = _sort_local([a for a in uniq.values() if a.id not in w.seen], Sort.newest)
            w.remember(list(uniq))
            w.last_check = time.time()
            self._watches.move_to_end(watch_key)
        if first and not emit_initial:
            return [], True, False
        return new, first, possible_gap

    def drop_watch(self, watch_key: str) -> bool:
        return self._watches.pop(watch_key, None) is not None

    # --------------------------------------------------------- deals / risk
    def _stats_query(self, q: SearchQuery) -> SearchQuery:
        # newest listings = current asking prices (relevance order skews towards old/promoted ads)
        return SearchQuery(**{**_asdict(q), "sort": Sort.newest, "ad_type": AdType.offer, "page": 1,
                              "price_min": None, "price_max": None})

    async def market(
        self, q: SearchQuery, *, must_include: list[str] | None = None, exclude: list[str] | None = None,
        pages: int = 3,
    ) -> tuple[MarketStats, list[AdSummary]]:
        """Robust price statistics for comparable current offers (outliers trimmed).

        Without an explicit category the comparison is restricted to the category most
        matches belong to – otherwise 'rtx 4090' would mix graphics cards with laptops and PCs.
        """
        sq = self._stats_query(q)
        res = await self.search(sq, pages=pages)
        excl = [*scoring.DEFAULT_EXCLUDE, *(exclude or [])]
        sample = [a for a in res.ads
                  if a.ad_type == AdType.offer and scoring.title_matches(a.title, must_include, excl)]
        desc = f"{len(res.ads)} Angebote für {sq.path()}"
        if must_include:
            desc += f", Titel enthält {must_include}"
        cat = str(q.category_id) if q.category_id else None
        breakdown: list[dict] = []
        if not cat and sample:
            terms = must_include or (q.query or "").split()
            cat, breakdown = scoring.pick_category(sample, terms)
            if cat:
                matched = len(sample)
                sample = [a for a in sample if a.category_id == cat]
                desc += f", nur Kategorie {cat} (automatisch gewählt, {len(sample)} von {matched} Treffern)"
        stats = scoring.market_stats(sample, desc)
        stats.category_id = cat
        stats.category_breakdown = breakdown
        return stats, sample

    async def _risk_for(self, ad_id: str, median: float | None) -> tuple[RiskAssessment, AdDetail]:
        detail = await self.get_ad(ad_id)
        seller = None
        if detail.seller.user_id:
            try:
                seller = await self.get_seller(detail.seller.user_id)
            except Exception as e:  # seller page is a bonus signal
                log.info("seller %s not available for risk check: %s", detail.seller.user_id, e)
        return scoring.assess(detail, median_eur=median, seller=seller), detail

    async def find_deals(
        self,
        q: SearchQuery,
        *,
        must_include: list[str] | None = None,
        exclude: list[str] | None = None,
        check_top: int = 5,
        limit: int = 15,
        min_ratio: float = 0.3,
        max_ratio: float = 1.0,
        stats_pages: int = 3,
        deal_pages: int = 2,
    ) -> DealsResult:
        """Cheapest realistic offers relative to the market median, top ones risk-checked."""
        stats, sample = await self.market(q, must_include=must_include, exclude=exclude, pages=stats_pages)
        if not stats.median_eur or stats.sample_size < 5:
            raise ValueError(
                f"too few comparable offers for a market price ({stats.sample_size}) – "
                "broaden the query or relax must_include"
            )
        med = stats.median_eur
        lo = max(q.price_min or 0, int(min_ratio * med))
        hi = int(max_ratio * med + 0.999)
        if q.price_max is not None:
            hi = min(hi, q.price_max)
        dq = SearchQuery(**{**_asdict(q), "sort": Sort.price_asc, "ad_type": AdType.offer, "page": 1,
                            "price_min": lo, "price_max": hi, "category_id": stats.category_id or q.category_id})
        res = await self.search(dq, pages=deal_pages)

        excl = [*scoring.DEFAULT_EXCLUDE, *(exclude or [])]
        excluded = {"title_filter": 0, "wanted": 0, "placeholder_price": 0, "out_of_range": 0}
        cands: dict[str, AdSummary] = {}
        for a in [*res.ads, *sample]:
            if a.id in cands:
                continue
            amt = a.price.amount_eur
            if a.ad_type != AdType.offer:
                excluded["wanted"] += 1
            elif not scoring.title_matches(a.title, must_include, excl):
                excluded["title_filter"] += 1
            elif stats.category_id and a.category_id and a.category_id != str(stats.category_id) \
                    and not q.category_id:
                excluded["other_category"] = excluded.get("other_category", 0) + 1
            elif scoring.is_placeholder_price(amt, med):
                excluded["placeholder_price"] += 1
            elif not (lo <= (amt or 0) <= hi):
                excluded["out_of_range"] += 1
            else:
                cands[a.id] = a
        ordered = sorted(cands.values(), key=lambda a: (a.price.amount_eur or 0, a.id))[:limit]
        deals = [
            Deal(ad=a, price_vs_median=round(a.price.amount_eur / med, 2),
                 savings_eur=round(med - a.price.amount_eur, 2))
            for a in ordered
        ]

        top = deals[: max(0, check_top)]
        results = await asyncio.gather(*(self._risk_for(d.ad.id, med) for d in top), return_exceptions=True)
        for d, r in zip(top, results, strict=True):
            if isinstance(r, BaseException):
                log.info("risk check for %s failed: %s", d.ad.id, r)
                continue
            d.risk, d.seller = r[0], r[1].seller
            d.condition = r[1].attributes.get("Zustand")

        best = next((d.ad.id for d in deals if d.risk and d.risk.level == "low"
                     and not any("defekt" in h for h in d.risk.hints)), None)
        return DealsResult(
            query=dq.path(),
            market=stats,
            deals=deals,
            checked=sum(1 for d in deals if d.risk),
            excluded=excluded,
            best_low_risk_id=best,
            fetched_at=datetime.now(UTC),
        )

    async def assess_listing(
        self, ad: str, *, reference_query: str | None = None, must_include: list[str] | None = None,
    ) -> dict:
        """Risk check for one listing, compared against the market for similar offers."""
        detail = await self.get_ad(ad)
        ref = reference_query or _reference_from_title(detail.title)
        stats, _ = await self.market(
            SearchQuery(query=ref, category_id=detail.category_id), must_include=must_include
        )
        median = stats.median_eur if stats.sample_size >= 5 else None
        risk, detail = await self._risk_for(detail.id, median)
        return {"ad": detail, "market": stats, "risk": risk}

    # ----------------------------------------------------------- ad detail
    async def get_ad(self, ad: str, *, include_views: bool = False) -> AdDetail:
        ad_id = normalize_ad_id(ad)
        path = f"/s-anzeige/{ad_id}"

        async def load() -> AdDetail:
            res = await self.fetcher.get_page(path)
            if "/s-anzeige/" not in res.url or 'id="viewad-title"' not in res.text:
                raise NotFoundError(f"ad {ad_id} not found (deleted or expired)")
            return parser.parse_ad_detail(res.text, res.url)

        detail, cached = await self.cache.get_or_load("ad:" + ad_id, self.s.ttl_ad, load)
        detail = detail.model_copy(update={"cached": cached})
        if include_views:
            detail.views = await self.get_views(ad_id, referer=detail.url)
        return detail

    async def get_ads(self, ads: list[str], *, include_views: bool = False) -> list[AdDetail | dict]:
        """Fetch several ads concurrently (the fetcher still enforces the global rate)."""
        async def one(a: str) -> AdDetail | dict:
            try:
                return await self.get_ad(a, include_views=include_views)
            except Exception as e:  # report per item, don't fail the batch
                return {"id": a, "error": f"{type(e).__name__}: {e}"}

        return list(await asyncio.gather(*(one(a) for a in ads[:20])))

    async def get_views(self, ad_id: str, *, referer: str | None = None) -> int | None:
        # Note: this is the endpoint the ad page itself calls; it counts a view.
        async def load() -> int | None:
            import json

            res = await self.fetcher.get_json(f"/s-vac-inc-get.json?adId={ad_id}", referer=referer)
            try:
                return int(json.loads(res.text).get("numVisits"))
            except (ValueError, TypeError, AttributeError):
                return None

        views, _ = await self.cache.get_or_load("views:" + ad_id, self.s.ttl_views, load)
        return views

    # -------------------------------------------------------------- seller
    async def get_seller(self, user_id: str | int, *, page: int = 1) -> Seller:
        uid = str(user_id).strip()
        if not uid.isdigit():
            raise ValueError("user_id must be numeric (see seller_id / seller.user_id)")
        path = f"/s-bestandsliste.html?userId={uid}" + (f"&pageNum={page}" if page > 1 else "")

        async def load() -> Seller:
            res = await self.fetcher.get_page(path)
            if "userprofile" not in res.text and "data-adid" not in res.text:
                raise NotFoundError(f"seller {uid} not found")
            return Seller(
                profile=parser.parse_seller_profile(res.text, uid),
                ads=parser.parse_ad_list(res.text),
                fetched_at=datetime.now(UTC),
            )

        seller, cached = await self.cache.get_or_load(f"seller:{uid}:{page}", self.s.ttl_seller, load)
        return seller.model_copy(update={"cached": cached})

    # ---------------------------------------------------------- reference
    async def resolve_location(self, query: str) -> list[Location]:
        q = query.strip()
        if not q:
            return []

        async def load() -> list[Location]:
            from urllib.parse import quote

            res = await self.fetcher.get_json(f"/s-ort-empfehlungen.json?query={quote(q)}")
            return parser.parse_locations(res.text)

        locs, _ = await self.cache.get_or_load("loc:" + q.lower(), self.s.ttl_locations, load)
        return locs

    async def location_id(self, location: str | int | None) -> str | None:
        """Accept a numeric location id, a 5-digit PLZ or a place name."""
        if location is None or str(location).strip() == "":
            return None
        loc = str(location).strip()
        if loc.isdigit() and len(loc) != 5:
            return loc  # already an id
        return (await self.best_location(loc)).id

    async def best_location(self, query: str) -> Location:
        """Resolve a place name / PLZ, rejecting the site's fuzzy 'did you mean' matches."""
        found = await self.resolve_location(query)
        nq = _norm(query)
        first_word = _norm(re.split(r"[\s,/-]+", query.strip())[0])

        def place(name: str) -> str:
            return _norm(name.split(" - ", 1)[0])

        ranked = (
            [x for x in found if place(x.name) == nq]
            + [x for x in found if place(x.name).startswith(nq)]
            + [x for x in found if first_word and place(x.name).startswith(first_word)]
            + [x for x in found if nq and nq in _norm(x.name)]
        )
        if not ranked:
            hint = ", ".join(x.name for x in found[:5]) or "none"
            raise ValueError(f"unknown location {query!r} (site suggested: {hint})")
        best = ranked[0]
        self._location_names[best.id] = best.name
        return best

    async def categories(self) -> list[Category]:
        async def load() -> list[Category]:
            res = await self.fetcher.get_page("/s-kategorien.html")
            return parser.parse_categories(res.text)

        cats, _ = await self.cache.get_or_load("categories", self.s.ttl_categories, load)
        return cats

    async def category_id(self, category: str | int | None) -> str | None:
        """Accept a numeric id, a slug (``handy-telekom``) or a (partial) German name."""
        if category is None or str(category).strip() == "":
            return None
        c = str(category).strip()
        if c.isdigit():
            return c
        cats = await self.categories()
        low = c.lower()
        for pred in (
            lambda x: x.slug == low,
            lambda x: x.name.lower() == low,
            lambda x: low in x.name.lower(),
            lambda x: low in x.slug,
        ):
            hit = next((x for x in cats if pred(x)), None)
            if hit:
                return hit.id
        raise ValueError(f"unknown category {c!r} – use list_categories")

    def status(self) -> dict:
        st = self.cache.stats
        return {
            "upstream": self.fetcher.info(),
            "cache": {
                "entries": len(self.cache),
                "hits": st.hits,
                "misses": st.misses,
                "coalesced": st.coalesced,
                "stale_served": st.stale_served,
            },
            "watches": len(self._watches),
        }


def normalize_ad_id(ad: str | int) -> str:
    s = str(ad).strip()
    if s.isdigit():
        return s
    if (i := parser.ad_id_from_url(s)) is not None:
        return i
    if m := re.search(r"/s-anzeige/(\d{6,})", s):
        return m.group(1)
    raise ValueError(f"not an ad id or kleinanzeigen URL: {ad!r}")


_STOP = {"neu", "neuwertig", "top", "zustand", "verkaufe", "mit", "und", "für", "fuer", "ovp", "originalverpackt",
         "inkl", "wie", "gebraucht", "sehr", "gut", "the", "a", "vb", "nur", "abholung", "versand"}


def _reference_from_title(title: str) -> str:
    """'Corsair Vengeance 32GB DDR5 6000 CL30 *NEU*' → 'corsair vengeance 32gb ddr5'."""
    words = [w for w in re.findall(r"[a-zA-Z0-9äöüÄÖÜß.+-]+", title.lower()) if w not in _STOP and len(w) > 1]
    return " ".join(words[:4]) or title


def _sort_local(ads: list[AdSummary], sort: Sort) -> list[AdSummary]:
    if sort == Sort.newest:
        return sorted(ads, key=lambda a: (a.posted_at is not None,
                                          a.posted_at or datetime.min.replace(tzinfo=UTC), int(a.id)),
                      reverse=True)
    if sort in (Sort.price_asc, Sort.price_desc):
        priced = [a for a in ads if a.price.amount_eur is not None]
        rest = [a for a in ads if a.price.amount_eur is None]
        return sorted(priced, key=lambda a: a.price.amount_eur, reverse=sort == Sort.price_desc) + rest
    return ads


def _matches(a: AdSummary, q: SearchQuery) -> bool:
    """Local equivalent of the URL filters (used in robots strict mode)."""
    amt = a.price.amount_eur
    if q.price_min is not None and (amt is None or amt < q.price_min):
        return False
    if q.price_max is not None and (amt is None or amt > q.price_max):
        return False
    if q.seller_type and a.seller_type != q.seller_type:
        return False
    if q.ad_type and a.ad_type != q.ad_type:
        return False
    if q.shipping is not None and a.shipping != q.shipping:
        return False
    return not (q.buy_now and not a.buy_now)


_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().translate(_UMLAUT))


def _asdict(q: SearchQuery) -> dict:
    return {f: getattr(q, f) for f in q.__dataclass_fields__}
