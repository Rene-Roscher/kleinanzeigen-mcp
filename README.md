<h1 align="center">kleinanzeigen-mcp</h1>

<p align="center">
  <b>Live Kleinanzeigen.de listings for AI assistants – search, deal finder and scam check over MCP or REST.</b>
</p>

<p align="center">
  <a href="https://github.com/Rene-Roscher/kleinanzeigen-mcp/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Rene-Roscher/kleinanzeigen-mcp/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776ab">
  <img alt="MCP" src="https://img.shields.io/badge/MCP-stdio%20%7C%20streamable%20HTTP-6f42c1">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

Ask your assistant things like *"Find the cheapest DDR5 32 GB desktop RAM and check it for scams"* or
*"What new fridges were posted in Hamburg in the last 3 hours?"* – and get real, current listings
from Germany's largest classifieds marketplace.

- ⚡ **Fast** – plain HTTP/2, no headless browser. A result page loads in ~0.5–1 s and parses in ~10 ms.
- 🎯 **Built for agents** – 8 focused tools, search rules explained where the model writes the query,
  compact responses (~60 % fewer tokens), hints that say how to continue.
- 💸 **Deal finder** – robust market median from current comparable offers, then the cheapest realistic ones.
- 🛡️ **Scam check** – explainable 0–100 risk score: account age, *new account + perfect price*,
  ratings, possibly hijacked accounts, WhatsApp / PayPal Friends & Family / prepayment red flags.
- 🤝 **Polite by design** – one global rate limit, caching with single-flight, instant back-off on 403/429.
- 🔐 **Server mode** – REST API + MCP over HTTP with hashed API tokens and per-token limits; optional proxy pool.

## Contents

- [Quick start](#quick-start)
- [Tools](#tools)
- [Searching well](#searching-well)
- [Deal finder & scam check](#deal-finder--scam-check)
- [Server mode (HTTP + REST)](#server-mode-http--rest)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Development](#development)
- [Disclaimer](#disclaimer)

## Quick start

You need [uv](https://docs.astral.sh/uv/) – `uvx` is to Python what `npx` is to Node.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # or: brew install uv
```

### Claude Code

```bash
claude mcp add -s user kleinanzeigen -- uvx --from git+https://github.com/Rene-Roscher/kleinanzeigen-mcp.git kleinanzeigen-mcp
```

That's it – the client starts the server on demand over stdio. The first start takes 10–20 s while uv
installs dependencies, afterwards ~2 s. Check with `/mcp` inside Claude Code.

### Claude Desktop, Cursor & other MCP clients

Add this to your client's MCP config (`claude_desktop_config.json`, `~/.cursor/mcp.json`, …):

```json
{
  "mcpServers": {
    "kleinanzeigen": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Rene-Roscher/kleinanzeigen-mcp.git", "kleinanzeigen-mcp"]
    }
  }
}
```

> **Private repository?** Use `git+ssh://git@github.com/Rene-Roscher/kleinanzeigen-mcp.git` with your SSH key,
> or authenticate git for HTTPS (`gh auth login`). Update to the latest version with `uvx --refresh …`.

### Try it from the terminal

```bash
uvx --from git+https://github.com/Rene-Roscher/kleinanzeigen-mcp.git kleinanzeigen-mcp search "rtx 4090" -l München -r 50
```

## Tools

| Tool | Use it for |
|---|---|
| `search_listings` | Browse and find items, newest first. Filters: location + radius, category, price, private/commercial, offers/wanted, shipping, *Direkt kaufen*, `max_age_minutes`, `exclude_terms`. |
| `find_deals` | "Cheapest / best price / is this a good deal?" – market price + cheapest realistic offers + scam check. |
| `assess_listing` | "Is this ad trustworthy?" for a single listing. |
| `get_listing` | Full details for 1–20 ads: description, attributes (condition, mileage, …), photos, shipping, seller. |
| `get_seller` | A seller's profile (rating, badges, active since, response time) and their other ads – by seller id or ad. |
| `check_new_listings` | Saved search: returns only ads it has not reported before. Pages until it reaches known ads, so nothing is lost between checks. |
| `find_location` / `list_categories` | Only for disambiguation – the other tools accept place and category names directly. |

<details>
<summary><b>Example: <code>find_deals</code> response (shortened)</b></summary>

```json
{
  "market": {
    "median_eur": 472.5, "p25_eur": 430.0, "sample_size": 18, "low_confidence": false,
    "category": "PC-Zubehör & Software (225)"
  },
  "deals": [
    {
      "title": "32 GB Samsung DDR5 UDIMM RAM 2x16GB", "price": "295 €", "vs_median": 0.62, "saves_eur": 177.5,
      "location": "10247 Friedrichshain", "seller": "private", "flags": ["shipping"],
      "risk": {
        "level": "low", "score": 0, "account_age_days": 5328,
        "reasons": ["+15: Preis 38 % unter Marktmedian"],
        "trust":   ["-10: Konto seit 14 Jahren aktiv", "-15: Zufriedenheit TOP", "-10: Zuverlässigkeits-Badge"]
      },
      "url": "https://www.kleinanzeigen.de/s-anzeige/3478982077"
    }
  ],
  "best_low_risk_id": "3478982077",
  "excluded": {"title_filter": 38, "placeholder_price": 1, "laptop_ram": 2}
}
```
</details>

## Searching well

The server instructions teach these rules to the model, but they help humans too:

| Rule | Why |
|---|---|
| Use **German** keywords | `kühlschrank` → 83 000 hits, `fridge` → 284 |
| **All words must match** | Keep it to 1–3 distinctive words: `rtx 4090`, `iphone 15 pro 256gb` |
| No OR, no minus, no phrases | `iphone -hülle` is searched as `iphone hülle` – use `exclude_terms` instead |
| Generic words need a **category** | The search also matches descriptions (`kühlschrank` finds holiday flats) |
| Check `location_name` | `Frankfurt` → Frankfurt am Main; alternatives are listed in the response |

Umlauts (`ü`/`ue`), plurals and spacing (`rtx4090` / `rtx 4090`) are normalised by the site.

## Deal finder & scam check

`find_deals("ddr5 32gb", must_include=["32gb|2x16", "ddr5"], exclude_terms=["sodimm", "laptop"])`

1. **Market price** – median of ~75 current comparable offers. Wanted ads, *Ankauf*, defective items and
   placeholder prices (`1 € VB`) are dropped, outliers trimmed (IQR).
2. **Right category, automatically** – the category where the *item itself* is listed, not bundles containing it
   (RAM rather than mini PCs with "32GB DDR5" in the title).
3. **Cheapest realistic offers** between 30 % of the median and the median.
4. **Risk check** for the cheapest ones (detail page + seller profile, cached).

| Signal | Points |
|---|---|
| Price < 50 % / < 70 % of the median | +30 / +15 |
| Account ≤ 3 / ≤ 14 / ≤ 60 days old | +40 / +30 / +15 |
| Account ≤ 30 days **and** price at or below the median | +20 |
| Account created on the day the ad went up | +15 |
| Old account but ≥ 70 % of its ads posted in the last 48 h – *possibly hijacked* | +25 |
| Seller's ads spread over ≥ 3 postcode regions | +15 |
| Contact off-platform (WhatsApp, e-mail, phone in text) / PayPal F&F, gift cards / prepayment only | +20 / +25 / +10 |
| Seller "abroad", pickup excluded, few photos, no rating | +5 … +15 |
| TOP/OK rating, reliability badge, account ≥ 3 years, pickup only, invoice, unremarkable history | −15 … −5 |

`low` < 30 ≤ `medium` < 60 ≤ `high`. Trust signals count at most −20 when the price is below half the
market – an old, well-rated account must not neutralise an implausible price, because that is exactly what a
hijacked account looks like. Quality **hints** flag per-unit prices, title/description mismatches
(*32 GB* vs *16 GB*), defects and laptop RAM detected by part number.

It is a heuristic with reasons, not a verdict. Details: [docs/scoring.md](docs/scoring.md).

## Server mode (HTTP + REST)

Run one central instance so all clients share one cache and one rate limit.

```bash
uvx --from git+https://github.com/Rene-Roscher/kleinanzeigen-mcp.git kleinanzeigen-mcp gen-token my-laptop
# → prints a token and the KA_API_TOKENS entry (store only the sha256 hash on the server)

KA_API_TOKENS="my-laptop:sha256:<hash>" kleinanzeigen-mcp serve --host 0.0.0.0 --port 8080
```

- MCP (streamable HTTP): `http://host:8080/mcp`
- REST + OpenAPI docs: `http://host:8080/v1/…`, `http://host:8080/docs`

```bash
claude mcp add --transport http kleinanzeigen https://your-host/mcp --header "Authorization: Bearer ka_…"
curl -H "Authorization: Bearer ka_…" "https://your-host/v1/search?query=ddr5&location=Berlin&radius_km=20"
```

Docker: `cp .env.example .env && docker compose up -d --build`. Full endpoint list: [docs/http-api.md](docs/http-api.md).

## Configuration

Environment variables (prefix `KA_`) or a `.env` file:

| Variable | Default | Meaning |
|---|---|---|
| `KA_API_TOKENS` | – | `name:token` or `name:sha256:<hex>`, comma separated. Empty = no auth (local use only) |
| `KA_CLIENT_RATE_PER_MIN` | `60` | Requests per minute and token to this server |
| `KA_RATE_PER_S` / `KA_BURST` | `0.5` / `4` | **Global** pace towards kleinanzeigen.de, shared by all clients and proxies |
| `KA_MAX_CONCURRENCY` | `2` | Concurrent upstream requests |
| `KA_TTL_SEARCH` / `_AD` / `_SELLER` | `60` / `600` / `3600` | Cache lifetimes in seconds |
| `KA_STALE_MAX_AGE_S` | `21600` | Max age of expired cache entries served while upstream is unavailable |
| `KA_PROXIES` | – | e.g. `socks5h://u:p@host:1080,http://u:p@host2:8080,direct` |
| `KA_PROXY_FILE` | – | One proxy URL per line, `#` comments allowed |
| `KA_USER_AGENT` / `KA_CHROME_MAJOR` | Chrome 141 | Must match – the client hints are derived from them |
| `KA_ALLOWED_HOSTS` | – | Host allow-list for `/mcp` (DNS-rebinding protection) |
| `KA_ROBOTS_POLICY` | `off` | `strict`: only request URLs allowed by robots.txt; filters are then applied locally |

## How it works

```
 MCP stdio / HTTP ─┐
 REST /v1 ─────────┼─► KleinanzeigenClient ─► TTL cache (LRU, single-flight, stale-on-error)
 token auth ───────┘                       └► Fetcher ─► token bucket (global) ─► session pool
                                                          (cookies, HTTP/2, cooldown – one per proxy)
                    Parser: Astro island props → DOM fallback (new + legacy markup)
```

- The new search page ships the result list as structured data inside its Astro islands – seller id, seller type,
  promoted flag, attributes, images. That is parsed first; a DOM parser covers the legacy markup still used for
  detail and seller pages.
- One browser-like session per egress: consistent Chrome headers, cookie jar, referer chain, homepage visit at start.
- A process-wide token bucket shapes *all* upstream traffic. On 403/429/challenge pages the session cools down
  exponentially and answers come from cache. Challenges are never solved or bypassed.

Measured against the live site: 32 requests in 57 s with zero blocks; repeating the same workload served
entirely from cache. More: [docs/how-it-works.md](docs/how-it-works.md).

## Development

```bash
git clone https://github.com/Rene-Roscher/kleinanzeigen-mcp.git && cd kleinanzeigen-mcp
uv sync
uv run pytest                 # offline tests against anonymised HTML fixtures
uv run pytest -m live         # smoke test against the real site (run sparingly)
uv run ruff check src tests
```

The repository ships a `.mcp.json`, so an MCP client opened in the project folder picks up the local server.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

This is an independent project and is not affiliated with or endorsed by Kleinanzeigen. It reads publicly visible
pages at a human pace. Automated access may conflict with the site's terms of use – check them before using this
commercially or at scale. Risk scores are heuristics based on public signals and are no proof of fraud.

## License

[MIT](LICENSE)
