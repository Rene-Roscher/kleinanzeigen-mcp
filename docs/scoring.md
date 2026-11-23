# Market price & scam-risk scoring

The deal finder answers two questions: *what does this item cost right now?* and *which of the cheap offers
look trustworthy?* Everything is computed from public signals and returned with its reasons.

## 1. Market price

1. Fetch the newest offers for the query (`sort=newest`, `anzeige:angebote`, 3 pages; up to 5 when fewer than
   30 comparable offers are found).
2. Keep offers whose title matches `must_include` (every entry must match, `|` separates alternatives) and does
   not contain `exclude_terms` or the built-in junk terms (*suche, ankauf, tausch, defekt, kaputt, bastler, …*).
   `exclude_terms` is also checked against the teaser text.
3. **Category.** Without an explicit `category`, the comparison is restricted to the category in which the item
   itself is listed. For each category among the matches the mean *title position* of the search terms is
   computed (0 = start of the title, 1 = end). The item names the thing up front (*"Corsair 32GB DDR5"*),
   bundles mention it late (*"Mini PC Ryzen 9 … 32GB DDR5"*). Among categories with at least 5 matches and 15 %
   of all matches, the one with the lowest title position wins. Validated on RAM, RTX 4090, iPhone, PS5 and road
   bikes. The breakdown is returned so a client can override the choice.
4. Drop placeholder prices (≤ 2 €, < 10 % or > 20× the median) and trim outliers with the 1.5 × IQR rule.
5. Report median, mean, p25, p75, sample size and `low_confidence` (fewer than 15 comparable offers).

## 2. Deals

Search again sorted by price, restricted to `min_ratio × median … median` (default 30 %–100 %) and the chosen
category, apply the same filters and sort by price. Deals are the cheapest offers in the *whole* market, while
the median comes from the *newest* offers – so many deals can sit below p25.

When `exclude_terms` contains `sodimm`, `laptop`, `notebook` or `camm`, laptop memory is also recognised by
part number (Samsung `M425R…`, `M471…`, CAMM/LPCAMM modules) in title, teaser and full description.

After the risk check, defective items and items whose description contains an excluded term are removed or
flagged and never become `best_low_risk_id` (the cheapest checked deal with level `low`).

## 3. Risk score

Each checked deal loads the ad detail page and – if the seller has a public profile – the seller page (cached
for one hour). Points are added for risk and subtracted for trust; the score is clamped to 0–100.

| Signal | Points |
|---|---|
| Price < 50 % of the median ("too good to be true") | +30 |
| Price < 70 % of the median | +15 |
| Account ≤ 3 days old | +40 |
| Account ≤ 14 days old | +30 |
| Account ≤ 60 days old | +15 |
| Account age unknown (private seller) | +5 |
| Account created within a day of the ad's posting date | +15 |
| Account ≤ 30 days **and** price ≤ median – the classic pattern | +20 |
| Account ≤ 30 days with ≥ 5 ads online | +10 |
| Account > 1 year, ≥ 3 ads, ≥ 70 % of them posted in the last 48 h – *possibly hijacked* | +25 |
| Private seller's ads spread over ≥ 3 postcode regions | +15 |
| WhatsApp/Telegram, e-mail address or mobile number in the text | +20 |
| PayPal Friends & Family, Western Union, gift cards, crypto | +25 |
| Prepayment / bank transfer only | +10 |
| Seller claims to be abroad / on assignment / at sea | +15 |
| Pickup explicitly excluded | +10 |
| No photos / one photo | +10 / +5 |
| Description shorter than 40 characters | +5 |
| No satisfaction rating (private seller) | +5 |
| Satisfaction **TOP** / **OK** | −15 / −5 |
| Reliability badge | −10 |
| Account ≥ 3 years old | −10 |
| Commercial seller | −5 |
| Pickup only – goods can be inspected | −10 |
| Invoice / warranty mentioned | −5 |
| Unremarkable history (≥ 3 ads over ≥ 14 days, one region) | −5 |

**Levels:** `low` < 30 ≤ `medium` < 60 ≤ `high`.

**Trust cap.** When the price is below 50 % of the median, trust signals count at most −20 in total. A
long-standing, well-rated account must not cancel an implausible price – that combination is what a taken-over
account looks like.

**Defective items** get no price comparison (a broken phone is not comparable with working ones).

## 4. Hints (deal quality, not risk)

- price is probably per unit, or the ad covers several variants (*"16GB – 32GB"*, *"Preis pro Riegel"*);
- title and description disagree on capacity (*32 GB* in the title, only *16 GB* in the text; kits such as
  *2×16GB* are understood);
- item is marked defective;
- price implausibly low (< 40 % of the median) – typo, bait or catch;
- price is negotiable (*VB*);
- description mentions an excluded term.

## Limits

- Only public signals. A hijacked account with a single, plausible ad is indistinguishable from the real owner.
- Photos are not reverse-image searched.
- Small markets give unstable medians – watch `sample_size` and `low_confidence`.
- Reasons are returned in German, matching the marketplace.
