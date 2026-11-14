"""Market price statistics and a light, explainable scam-risk heuristic.

Nothing here is a verdict. Every point added or removed comes with a German,
human-readable reason so an agent (or person) can judge the listing itself.
"""

from __future__ import annotations

import re
import statistics
from datetime import date, datetime

from .models import AdDetail, AdSummary, AdType, MarketStats, RiskAssessment, Seller, SellerType
from .parser import BERLIN

# Titles that are not a normal offer of the item itself.
DEFAULT_EXCLUDE = (
    "suche", "gesucht", "ankauf", "kaufe ", "wir kaufen", "tausche", "tausch ", "defekt", "defeckt",
    "kaputt", "bastler", "ersatzteil", "ohne funktion", "nicht funktionsfähig", "nicht funktionsfaehig",
)

_GB_RE = re.compile(r"(\d{1,4})\s?gb\b", re.I)


# --------------------------------------------------------------- matching
def title_matches(title: str, must_include: list[str] | None, exclude: list[str] | None) -> bool:
    """``must_include``: every entry must match; an entry may list alternatives with ``|``
    (e.g. ``"32gb|2x16"``). ``exclude``: none of the substrings may occur."""
    t = " " + title.lower() + " "
    for term in exclude or ():
        if term.lower() in t:
            return False
    for term in must_include or ():
        alts = [x.strip().lower() for x in term.split("|") if x.strip()]
        if alts and not any(a in t for a in alts):
            return False
    return True


def title_position(title: str, terms: list[str]) -> float:
    """Mean relative position (0 = start, 1 = end/missing) of the terms in the title.

    The item itself names the searched thing up front ('Corsair 32GB DDR5 …'), bundles
    mention it late ('Mini PC Ryzen 9 … 32GB DDR5'). Used to pick the right category.
    """
    t = title.lower()
    pos = []
    for term in terms:
        hits = [t.find(a.strip().lower()) for a in term.split("|") if a.strip() and a.strip().lower() in t]
        pos.append(min(hits) / max(1, len(t)) if hits else 1.0)
    return statistics.fmean(pos) if pos else 1.0


def pick_category(ads: list[AdSummary], terms: list[str]) -> tuple[str | None, list[dict]]:
    """Choose the category the searched item itself lives in, plus a breakdown of the others."""
    groups: dict[str, list[AdSummary]] = {}
    for a in ads:
        if a.category_id:
            groups.setdefault(a.category_id, []).append(a)
    if not groups:
        return None, []
    breakdown = []
    for cat, items in groups.items():
        prices = [a.price.amount_eur for a in items if a.price.amount_eur and a.price.amount_eur > 2]
        breakdown.append({
            "category_id": cat,
            "count": len(items),
            "median_eur": round(statistics.median(prices), 2) if prices else None,
            "title_position": round(statistics.fmean(title_position(a.title, terms) for a in items), 2),
        })
    breakdown.sort(key=lambda b: -b["count"])
    min_n = max(5, int(0.15 * len(ads)))
    eligible = [b for b in breakdown if b["count"] >= min_n]
    if not eligible:
        return None, breakdown[:5]
    best = min(eligible, key=lambda b: (b["title_position"], -b["count"]))
    return best["category_id"], breakdown[:5]


def is_placeholder_price(amount: float | None, median: float | None) -> bool:
    """'1 € VB', '1234567 €' and friends."""
    if amount is None:
        return True
    if amount <= 2:
        return True
    if median:
        return amount < 0.1 * median or amount > 20 * median
    return False


# ------------------------------------------------------------ market stats
def market_stats(ads: list[AdSummary], query_desc: str) -> MarketStats:
    prices = sorted(
        a.price.amount_eur for a in ads
        if a.price.amount_eur is not None and a.price.amount_eur > 2 and a.ad_type == AdType.offer
    )
    raw_n = len(prices)
    if raw_n >= 4:
        q1, _, q3 = statistics.quantiles(prices, n=4)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        prices = [p for p in prices if lo <= p <= hi]
    if not prices:
        return MarketStats(basis=query_desc, sample_size=0, outliers_removed=raw_n, low_confidence=True)
    qs = statistics.quantiles(prices, n=4) if len(prices) >= 4 else [prices[0], statistics.median(prices),
                                                                     prices[-1]]
    return MarketStats(
        basis=query_desc,
        sample_size=len(prices),
        low_confidence=len(prices) < 15,
        outliers_removed=raw_n - len(prices),
        median_eur=round(statistics.median(prices), 2),
        mean_eur=round(statistics.fmean(prices), 2),
        p25_eur=round(qs[0], 2),
        p75_eur=round(qs[2], 2),
        min_eur=prices[0],
        max_eur=prices[-1],
    )


# ------------------------------------------------------------------- risk
_OFF_PLATFORM = re.compile(
    r"whats\s?app|telegram|[\w.+-]+@[\w-]+\.[a-z]{2,}|(?:\+49|0049|\b0)1[5-7]\d[\d /-]{6,}", re.I
)
# Payment without any buyer protection – the scam classics.
_RISKY_PAYMENT = re.compile(
    r"freunde\s*(?:und|&)\s*familie|familie\s*(?:und|&)\s*freunde|\bf\s?&\s?f\b|friends\s*(?:and|&)\s*family|"
    r"western\s*union|moneygram|paysafe|gutschein(?:karte)?\s+als\s+zahlung|amazon[- ]gutschein|crypto|bitcoin",
    re.I,
)
# Common with honest sellers too, but no protection either.
_PREPAY = re.compile(r"\bvorkasse\b|nur\s+(?:per\s+)?überweisung|nur\s+(?:per\s+)?ueberweisung", re.I)
_NO_PICKUP = re.compile(r"nur\s+versand|keine\s+abholung|abholung\s+nicht\s+möglich|kein\s+abholen", re.I)
_ABROAD = re.compile(r"(?:bin|zurzeit|derzeit|momentan)\s+im\s+ausland|beruflich\s+unterwegs|auf\s+montage|"
                    r"auf\s+see\b", re.I)
_PER_UNIT = re.compile(r"preis\s+(?:gilt\s+)?(?:pro|je)\b|pro\s+(?:stück|riegel|modul|stk)|\bje\s+riegel", re.I)
_INVOICE = re.compile(r"rechnung|kaufbeleg|garantie\s+bis|gewährleistung", re.I)


def account_age_days(active_since: str | None, today: date | None = None) -> int | None:
    if not active_since or not (m := re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", active_since)):
        return None
    try:
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return ((today or datetime.now(BERLIN).date()) - d).days


_KIT_RE = re.compile(r"(\d)\s?[x×]\s?(\d{1,3})\s?gb", re.I)
_KIT_WORDS = re.compile(
    r"\bkit\b|\b[2-8]\s?[x×]\s?\d|\b[2-8]\s?(?:stück|stk|riegel|module)\b|zwei\s+(?:riegel|module)|\bpaar\b", re.I
)


def _gb_values(text: str) -> set[int]:
    vals = {int(x) for x in _GB_RE.findall(text) if 1 <= int(x) <= 2048}
    vals |= {int(n) * int(m) for n, m in _KIT_RE.findall(text)}  # "2x16GB" → 32
    return vals


def assess(
    ad: AdDetail,
    *,
    median_eur: float | None = None,
    seller: Seller | None = None,
    now: datetime | None = None,
) -> RiskAssessment:
    now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    score = 0
    reasons: list[str] = []
    positives: list[str] = []
    hints: list[str] = []

    def add(points: int, text: str) -> None:
        nonlocal score
        score += points
        (reasons if points > 0 else positives).append(f"{'+' if points > 0 else ''}{points}: {text}")

    s = ad.seller
    desc = ad.description or ""
    price = ad.price.amount_eur
    ratio = round(price / median_eur, 2) if price and median_eur else None

    # --- price vs market
    if ratio is not None:
        if ratio < 0.5:
            add(30, f"Preis {int(round((1 - ratio) * 100))} % unter Marktmedian – zu gut um wahr zu sein?")
        elif ratio < 0.7:
            add(15, f"Preis {int(round((1 - ratio) * 100))} % unter Marktmedian")

    # --- account age
    age = account_age_days(s.active_since, now.date())
    if age is None:
        if s.seller_type != SellerType.commercial:
            add(5, "Kontoalter unbekannt")
    elif age <= 3:
        add(40, f"Konto erst {age} Tag(e) alt")
    elif age <= 14:
        add(30, f"Konto erst {age} Tage alt")
    elif age <= 60:
        add(15, f"Konto relativ neu ({age} Tage)")
    elif age >= 3 * 365:
        add(-10, f"Konto seit {age // 365} Jahren aktiv")

    # the classic: fresh account + attractive price
    if age is not None and age <= 30 and ratio is not None and ratio <= 1.0:
        add(20, "Neues Konto + Preis unter/um Marktpreis – typisches Betrugsmuster")

    # --- reputation
    if s.rating == "TOP":
        add(-15, "Zufriedenheit TOP")
    elif s.rating == "OK":
        add(-5, "Zufriedenheit OK")
    elif s.rating is None and s.seller_type != SellerType.commercial:
        add(5, "Keine Zufriedenheitsbewertung")
    if any("zuverlässig" in b.lower() for b in s.badges):
        add(-10, "Zuverlässigkeits-Badge (erfolgreiche Transaktionen)")
    if s.seller_type == SellerType.commercial:
        add(-5, "Gewerblicher Anbieter (Impressum/Gewährleistung)")
    if age is not None and age <= 30 and (s.ads_online or 0) >= 5:
        add(10, f"Neues Konto mit bereits {s.ads_online} Anzeigen")

    # --- possibly hijacked old account (needs the seller's other ads)
    if seller and seller.ads:
        recent = [a for a in seller.ads if a.posted_at and (now - a.posted_at).total_seconds() <= 48 * 3600]
        share = len(recent) / len(seller.ads)
        if len(seller.ads) >= 3 and share >= 0.7 and (age is None or age > 365):
            add(25, f"Altes Konto, aber {len(recent)} von {len(seller.ads)} Anzeigen in den letzten 48 h "
                    "eingestellt – mögliches gekapertes Konto")
        regions = {a.zip_code[:2] for a in seller.ads if a.zip_code}
        spread = len(regions) >= 3 and s.seller_type != SellerType.commercial
        burst = len(seller.ads) >= 3 and share >= 0.7 and (age is None or age > 365)
        if spread:
            add(15, f"Anzeigen des Verkäufers in {len(regions)} verschiedenen PLZ-Regionen")
        dated = [a.posted_at for a in seller.ads if a.posted_at]
        if not spread and not burst and len(dated) >= 3 and (max(dated) - min(dated)).days >= 14:
            add(-5, f"Verkäuferhistorie unauffällig ({len(seller.ads)} Anzeigen über "
                    f"{(max(dated) - min(dated)).days} Tage, gleiche Region)")

    # --- listing content
    if _OFF_PLATFORM.search(desc):
        add(20, "Will Kontakt außerhalb von Kleinanzeigen (WhatsApp/E-Mail/Telefon im Text)")
    if _RISKY_PAYMENT.search(desc):
        add(25, "Zahlung ohne Käuferschutz verlangt (PayPal Freunde & Familie, Western Union, Gutscheine …)")
    elif _PREPAY.search(desc):
        add(10, "Nur Vorkasse/Überweisung – kein Käuferschutz")
    if _NO_PICKUP.search(desc):
        add(10, "Abholung ausgeschlossen")
    if _ABROAD.search(desc):
        add(15, "Verkäufer angeblich nicht vor Ort / im Ausland")
    if len(ad.images) <= 1:
        add(10 if not ad.images else 5, "Kaum Fotos")
    if len(desc.strip()) < 40:
        add(5, "Sehr kurze Beschreibung")
    if ad.shipping and "Nur Abholung" in ad.shipping:
        add(-10, "Nur Abholung – Ware vor Ort prüfbar")
    if _INVOICE.search(desc):
        add(-5, "Rechnung/Garantie erwähnt")

    # --- deal-quality hints (not scam signals)
    if _PER_UNIT.search(desc) or re.search(r"\d+\s?gb\s*-\s*\d+\s?gb", ad.title, re.I):
        hints.append("Preis gilt evtl. pro Stück / mehrere Varianten in einer Anzeige")
    t_gb, d_gb = _gb_values(ad.title), _gb_values(desc)
    is_kit = bool(_KIT_WORDS.search(ad.title) or _KIT_WORDS.search(desc))
    if t_gb and d_gb and not is_kit and max(t_gb) not in d_gb and max(d_gb) < max(t_gb):
        hints.append(f"Titel nennt {max(t_gb)} GB, Beschreibung nur {sorted(d_gb)} GB – Angaben prüfen")
    if ad.price.negotiable:
        hints.append("Preis ist VB – Verhandlung möglich")
    cond = (ad.attributes.get("Zustand") or "").lower()
    if "defekt" in cond or re.search(r"\bdefe[c]?kt\b|\bkaputt\b", ad.title, re.I):
        hints.append("Artikel laut Anzeige defekt – kein vergleichbares Angebot")

    # A trusted-looking account must not neutralise an implausible price completely –
    # that is exactly what a hijacked account looks like.
    credit = sum(int(p.split(":", 1)[0]) for p in positives)
    if ratio is not None and ratio < 0.5 and credit < -20:
        score += -20 - credit
        positives.append("(Vertrauenssignale bei so niedrigem Preis nur bis -20 angerechnet)")
    if ratio is not None and ratio < 0.4:
        hints.append("Preis unplausibel niedrig – Tippfehler, Lockangebot oder Haken? Vor dem Kauf nachfragen.")

    score = max(0, min(100, score))
    level = "high" if score >= 60 else "medium" if score >= 30 else "low"
    return RiskAssessment(
        score=score,
        level=level,
        price_vs_median=ratio,
        account_age_days=age,
        reasons=reasons,
        positives=positives,
        hints=hints,
    )
