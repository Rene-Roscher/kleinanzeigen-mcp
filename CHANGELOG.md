# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.1] – 2026-11-25

### Fixed
- Built-in title filters dropped every ad starting with "Verkaufe …" because `kaufe ` matched inside
  `verkaufe`; the built-in terms are now anchored at word starts.
- `exclude_terms` / `must_include` ignore hyphens, dots and underscores, so `sodimm` also removes `SO-DIMM`.

## [0.2.0] – 2026-11-24

### Added
- `find_deals`: market median from current comparable offers, cheapest realistic offers and a risk check
  for the cheapest ones.
- `assess_listing`: explainable 0–100 scam-risk score for a single ad.
- Automatic comparison category (the category the item itself is listed in, not bundles).
- Laptop RAM detection by part number (SO-DIMM, CAMM) for desktop RAM searches.
- `exclude_terms` for search and watches; results are topped up from further pages.
- `max_age_minutes` pages until the time window is covered and reports `complete`.
- `get_seller` accepts an ad id or URL.
- Project `.mcp.json` and `uvx` quick start.

### Changed
- MCP tool layer reworked: 8 focused tools, search rules in the server instructions, concise responses by
  default (`response_format="detailed"` for all fields), hints for empty or mixed-category results.
- Ambiguous category names return the options instead of picking one; ambiguous places prefer the large city
  and list alternatives.
- `get_listing` takes a list of ids; image URLs are opt-in.

### Fixed
- A cancelled caller no longer cancels a shared fetch for other waiters.
- Deleted ads are never served from stale cache.
- Search keywords that appear in a page title are no longer mistaken for a block.
- Server errors are not repeated across all proxies.
- Watches no longer miss ads when more than one page appeared between checks.
- Robots-strict mode applies the requested sort locally.

## [0.1.0] – 2026-11-07

### Added
- Search, ad detail, seller, location and category access for kleinanzeigen.de.
- Parser for the structured Astro island data with a DOM fallback for new and legacy markup.
- Polite HTTP/2 fetcher: global token bucket, browser-consistent headers, cooldown on 403/429/challenges.
- TTL/LRU cache with single-flight and stale-on-error.
- Optional proxy pool (HTTP, HTTPS, SOCKS5, SOCKS5h) with failover.
- MCP server over stdio and streamable HTTP; REST API with OpenAPI docs.
- API tokens (plain or SHA-256), per-token rate limit.
- Saved searches (`check_new_listings`).
- Optional robots.txt strict mode.
- Docker image and compose file.
