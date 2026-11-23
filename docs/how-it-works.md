# How it works

## Upstream structure

| Observation | Consequence |
|---|---|
| The site is fronted by Akamai Bot Manager (`_abck`, `bm_sz` cookies) | A plain HTTP client works as long as headers are consistent with a real browser; no headless browser needed |
| The new search result page is an Astro app with generated Tailwind classes | CSS-class scrapers break; the result list is read from the `ImpressionTracker` island props instead |
| Island props carry structured data: id, seller id, seller type, promoted flag, attributes (mileage, first registration), images, shipping | More reliable than the DOM and richer than the visible card |
| Ad detail and seller pages still use the legacy JSP markup (`viewad-*`, `aditem`) | A DOM parser handles both markups; it is also the fallback for search pages |
| The `__ka-ls` cookie remembers the last search location and silently localises later pages | Dropped after every response |
| Without a keyword, the last path segment is read as keyword | The URL tail drops `k0` when there is no keyword |
| Default sort is "relevance" | "Newest" needs `sortierung:neuste`; promoted ads still come first and are filtered out |
| Deleted ads redirect (301) to their category | Detected and reported as not found |
| Detail pages contain full phone numbers in the HTML | Deliberately not extracted; fixtures are redacted |

URL shape (all segments optional, order matters; derived from the redirects of the site's own search form):

```
/s-{sortierung:…}/{anbieter:…}/{anzeige:…}/{direktkaufen:aktiv}/{versand:…}/{preis:min:max}/{seite:n}/{keyword}/k0c{cat}l{loc}r{radius}
```

Only the numeric `c`/`l`/`r` ids are interpreted; human-readable slugs are cosmetic and omitted.

## Being a polite, ordinary client

- **One global token bucket** (default 0.5 requests/s, burst 4, max 2 concurrent) shapes *all* upstream traffic,
  regardless of how many MCP/REST clients or proxies are active. Small random jitter on top.
- **Browser-consistent identity**: Chrome user agent with matching `sec-ch-ua*`, `sec-fetch-*`, `accept-*` and
  `priority` headers, HTTP/2, brotli/zstd, keep-alive, a persistent cookie jar per session, a referer chain and a
  homepage visit when a session starts.
- **Immediate back-off**: 403, 429 or a challenge page put the session into an exponential cooldown
  (60 s → 15 min, `Retry-After` respected). Nothing is sent from that session meanwhile, and the next request
  after the cooldown starts with fresh cookies. Challenges are never solved or bypassed.
- **Optional proxies** (`http`, `https`, `socks5`, `socks5h`): every proxy is its own session with its own cookies
  and cooldown. Network failures fail over to the next session and park the dead proxy (30 s → 10 min). HTTP errors
  are not retried across proxies, and the global rate limit stays shared – more proxies do not mean more requests.

## Caching

| Data | TTL |
|---|---|
| Search pages | 60 s |
| Ad details | 10 min |
| Seller pages | 1 h |
| View counter | 5 min |
| Categories | 24 h |
| Locations | 7 days |

- **Single-flight**: concurrent identical requests share one upstream fetch, which runs in a detached task –
  a cancelled caller does not cancel it for the others.
- **Stale-on-error**: while upstream is blocked or unreachable, expired entries up to 6 h old are served.
  Never for "not found": deleted ads are evicted.

## Watches

`check_new_listings` stores the ids it has returned per token and watch. Each check pages through the newest
listings until it reaches an ad it has already seen (max 5 pages ≈ 125 ads) and reports `possible_gap` if even
that was not enough. Watches live in memory and are reset on restart.

## robots.txt

The default (`KA_ROBOTS_POLICY=off`) behaves like a browser acting on a user's request. With `strict`, the live
robots.txt is evaluated with Google semantics (wildcards, `$`, longest match): filter URLs, seller pages and JSON
endpoints are then not requested; price, seller-type, shipping filters and sorting are applied locally instead.

## Measurements

Against the live site, from a cloud container without proxy:

| Measurement | Result |
|---|---|
| Search page (upstream) | 0.4–1.2 s |
| Parsing a result page (27 ads) | 9–18 ms |
| Cache hit | 3–4 ms |
| 5 identical concurrent calls | 1 upstream request |
| Mixed workload: 15 searches, 2 extra pages, 8 ads, 6 sellers | 32 requests in 57 s, 0 blocks |
| Same workload repeated | 0 upstream requests |
| `find_deals` incl. 5 risk checks | 20–40 s, ~17–26 requests |
| MCP `search_listings` response, 27 ads | ~3 000 tokens (concise format) |
