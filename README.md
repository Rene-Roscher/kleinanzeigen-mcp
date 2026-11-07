<h1 align="center">kleinanzeigen-mcp</h1>

<p align="center">
  <b>Live Kleinanzeigen.de listings for AI assistants – over MCP or REST.</b>
</p>

<p align="center">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776ab">
  <img alt="MCP" src="https://img.shields.io/badge/MCP-stdio%20%7C%20streamable%20HTTP-6f42c1">
</p>

---

Ask your assistant things like *"Show me new road bikes near Munich under 800 €"* or
*"What new fridges were posted in Hamburg today?"* – and get real, current listings
from Germany's largest classifieds marketplace.

- ⚡ **Fast** – plain HTTP/2, no headless browser. A result page loads in ~0.5–1 s and parses in ~10 ms.
- 🎯 **Built for agents** – search, details, sellers and saved searches as MCP tools.
- 🤝 **Polite by design** – one global rate limit, caching with single-flight, instant back-off on 403/429.
- 🔐 **Server mode** – REST API + MCP over HTTP with hashed API tokens and per-token limits; optional proxy pool.

## Contents

- [Quick start](#quick-start)
- [Tools](#tools)
- [Server mode (HTTP + REST)](#server-mode-http--rest)
- [Configuration](#configuration)
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
> or authenticate git for HTTPS (`gh auth login`). 

### Try it from the terminal

```bash
uvx --from git+https://github.com/Rene-Roscher/kleinanzeigen-mcp.git kleinanzeigen-mcp search "rtx 4090" -l München -r 50
```

## Tools

| Tool | Use it for |
|---|---|
| `search_listings` / `latest_listings` | Search with filters (location + radius, category, price, private/commercial, offers/wanted, shipping, *Direkt kaufen*) or the newest organic ads. |
| `get_listing` / `get_listings` | Full details for one or several ads: description, attributes (condition, mileage, …), photos, shipping, seller. |
| `get_seller` | A seller's profile (rating, badges, active since, response time) and their other ads. |
| `check_new_listings` | Saved search: returns only ads it has not reported before. |
| `find_location` / `list_categories` | Only for disambiguation – the other tools accept place and category names directly. |



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

Docker: `cp .env.example .env && docker compose up -d --build`.

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


## Development

```bash
git clone https://github.com/Rene-Roscher/kleinanzeigen-mcp.git && cd kleinanzeigen-mcp
uv sync
uv run pytest                 # offline tests against anonymised HTML fixtures
uv run pytest -m live         # smoke test against the real site (run sparingly)
uv run ruff check src tests
```


## Disclaimer

This is an independent project and is not affiliated with or endorsed by Kleinanzeigen. It reads publicly visible
pages at a human pace. Automated access may conflict with the site's terms of use – check them before using this
commercially or at scale. Risk scores are heuristics based on public signals and are no proof of fraud.

