"""Build kleinanzeigen.de search URLs.

Shape (path segments are all optional, order matters)::

    /s-{category-slug}/{location-slug}/{sortierung:…}/{anbieter:…}/{anzeige:…}
       /{direktkaufen:aktiv}/{versand:…}/preis:{min}:{max}/seite:{n}/{keyword}/k0c{cat}l{loc}r{radius}

(derived from the redirects ``/s-suchanfrage.html`` produces). Without a keyword
the tail drops the ``k0`` – otherwise the last segment is taken as keyword.
The site only interprets the numeric ``c``/``l``/``r`` ids in the tail; the
human-readable slugs are cosmetic, so we omit them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote

from .models import AdType, SellerType, Sort

_SORT = {
    Sort.newest: "sortierung:neuste",
    Sort.price_asc: "sortierung:preis",
    Sort.price_desc: "sortierung:teuerste",
    Sort.relevance: None,
}

RADII_KM = (1, 2, 3, 5, 10, 15, 20, 30, 50, 100, 150, 200)


def keyword_slug(q: str) -> str:
    q = unicodedata.normalize("NFC", q).strip().lower()
    q = re.sub(r"[\s/]+", "-", q)
    q = re.sub(r"-{2,}", "-", q).strip("-")
    return quote(q, safe="-.")


def snap_radius(km: int | None) -> int | None:
    if not km:
        return None
    return min(RADII_KM, key=lambda r: (abs(r - km), r))


@dataclass(slots=True)
class SearchQuery:
    query: str | None = None
    category_id: str | int | None = None
    location_id: str | int | None = None
    radius_km: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    sort: Sort = Sort.newest
    seller_type: SellerType | None = None
    ad_type: AdType | None = None
    shipping: bool | None = None
    buy_now: bool = False
    page: int = 1
    # raw category specific filters, e.g. {"handy_telekom.versand_s": "ja", "global.zustand": "like_new"}
    extra: dict[str, str] = field(default_factory=dict)

    def path(self) -> str:
        segs: list[str] = []
        if s := _SORT.get(self.sort):
            segs.append(s)
        if self.seller_type:
            segs.append("anbieter:" + ("privat" if self.seller_type == SellerType.private else "gewerblich"))
        if self.ad_type:
            segs.append("anzeige:" + ("angebote" if self.ad_type == AdType.offer else "gesuche"))
        if self.buy_now:
            segs.append("direktkaufen:aktiv")
        if self.shipping is not None:
            segs.append("versand:" + ("ja" if self.shipping else "nein"))
        if self.price_min is not None or self.price_max is not None:
            lo = "" if self.price_min is None else str(int(self.price_min))
            hi = "" if self.price_max is None else str(int(self.price_max))
            segs.append(f"preis:{lo}:{hi}")
        if self.page and self.page > 1:
            segs.append(f"seite:{int(self.page)}")
        slug = keyword_slug(self.query) if self.query else ""
        if slug:
            segs.append(slug)

        tail = "k0" if slug else ""
        if self.category_id:
            tail += f"c{int(self.category_id)}"
        if self.location_id:
            tail += f"l{int(self.location_id)}"
            if r := snap_radius(self.radius_km):
                tail += f"r{r}"
        for k, v in sorted(self.extra.items()):
            if not re.fullmatch(r"[a-z0-9_.]+", k) or not re.fullmatch(r"[a-zA-Z0-9_\-]+", v):
                raise ValueError(f"invalid filter {k}:{v}")
            tail += f"+{k}:{v}"
        segs.append(tail or "k0")
        return "/s-" + "/".join(segs)

    def cache_key(self) -> str:
        return "search:" + self.path()
