# HTTP server & REST API

`kleinanzeigen-mcp serve` starts one process that serves

- **MCP over streamable HTTP** at `/mcp` (stateless, JSON responses), and
- a **REST API** under `/v1` with OpenAPI docs at `/docs`.

Both share the same cache, rate limit and upstream sessions. Running one central instance is the
recommended setup when several people or machines use the server.

## Authentication

Every request except `GET /health` needs a token:

```
Authorization: Bearer <token>
X-API-Key: <token>              # alternative
```

Create tokens with the CLI and configure them on the server:

```bash
kleinanzeigen-mcp gen-token my-laptop
# token (give this to the client):  ka_…
# KA_API_TOKENS entry (plain):      my-laptop:ka_…
# KA_API_TOKENS entry (hash only):  my-laptop:sha256:3f2a…
```

`KA_API_TOKENS` accepts a comma-separated list of `token`, `name:token` or `name:sha256:<hex>`. Prefer the hash
form so the server never stores the plain token. Tokens are compared in constant time.

Each token is limited to `KA_CLIENT_RATE_PER_MIN` requests per minute (default 60). Watches
(`check_new_listings`, `/v1/watches`) are scoped per token, so two clients can use the same watch name.

## Endpoints

Common search parameters: `query`, `location` (city or PLZ), `location_id`, `radius_km`, `category`
(name or id), `price_min`, `price_max`, `seller_type` (`private`/`commercial`), `ad_type`
(`offer`/`wanted`), `shipping` (`true`/`false`), `buy_now`.

| Method & path | Description |
|---|---|
| `GET /v1/search` | Search. Extra: `sort` (`newest`, `relevance`, `price_asc`, `price_desc`), `page`, `pages` (1–5), `include_promoted`, `limit` |
| `GET /v1/latest` | Newest organic listings. Extra: `max_age_minutes`, `pages`, `limit` |
| `GET /v1/deals` | Deal finder. Extra: `must_include` (repeatable, `\|` for alternatives), `exclude` (repeatable), `check_top`, `limit`, `min_ratio` |
| `GET /v1/ads/{id}` | Full ad details. `include_views=true` also fetches the view counter |
| `GET /v1/ads/{id}/risk` | Risk assessment for one ad. Optional `reference_query`, `must_include` |
| `GET /v1/sellers/{user_id}` | Seller profile and listings. `page` for more ads |
| `GET /v1/locations?q=` | Resolve a place name or PLZ to location ids |
| `GET /v1/categories` | Category tree with ids and counts |
| `POST /v1/watches/{name}/check` | Saved search; body = search parameters as JSON. Returns only new ads |
| `GET /v1/status` | Cache, rate limit and upstream session statistics |
| `GET /health` | Liveness, no auth |

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8080/v1/deals?query=ddr5%2032gb&must_include=32gb|2x16&exclude=sodimm&exclude=laptop"

curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "rennrad", "location": "München", "radius_km": 30, "price_max": 800}' \
  http://localhost:8080/v1/watches/rennrad-muc/check
```

## Errors

| Status | Meaning |
|---|---|
| `400` | Invalid input (unknown/ambiguous location or category, missing criteria) – the message says what to change |
| `401` | Missing or wrong token |
| `403` | Not allowed with `KA_ROBOTS_POLICY=strict` |
| `404` | Ad or seller not found (sold, deleted or expired) |
| `429` | Per-token rate limit reached, see `Retry-After` |
| `502` | Kleinanzeigen unreachable |
| `503` | Kleinanzeigen is rate-limiting the server, see `Retry-After`; cached answers keep working |

## Deployment

```bash
cp .env.example .env          # set KA_API_TOKENS
docker compose up -d --build
```

The compose file binds to `127.0.0.1:8080`. Put a TLS-terminating reverse proxy in front for remote access.
With tokens configured, the SDK's DNS-rebinding check is disabled (a rebinding page cannot know the token);
set `KA_ALLOWED_HOSTS=mcp.example.com` to enforce a host allow-list anyway.

Run a single instance: several instances would each have their own rate limit and cache.
