# Contributing

```bash
uv sync
uv run pytest                 # offline, fast
uv run ruff check src tests
```

## Guidelines

- **Tests run offline** against recorded HTML in `tests/fixtures/` (gzip). Add a fixture when the site's markup
  changes and keep the old one if both variants are still served.
- **Anonymise fixtures** before committing: no phone numbers, e-mail addresses or real names beyond what a
  listing title shows. Strip scripts and styles to keep them small.
- **Live tests** (`uv run pytest -m live`) hit the real site. Run them sparingly and never in a loop.
- **Be polite**: never raise the default rate limit in code, never add CAPTCHA solving or fingerprint spoofing.
- **Tool descriptions are part of the API.** When changing an MCP tool, check that its description still says
  when to use it, when not to, and what the response contains.

## Releases

See [docs/releasing.md](docs/releasing.md).

## Project layout

```
src/kleinanzeigen_mcp/
  config.py      settings (KA_* environment variables)
  http.py        fetcher: rate limit, sessions, proxies, block detection
  cache.py       async TTL cache with single-flight and stale-on-error
  urls.py        search URL builder
  parser.py      HTML/island parsing
  scoring.py     market statistics and risk scoring
  client.py      high-level client used by MCP and REST
  mcp_server.py  MCP tools
  api.py         FastAPI app (REST + MCP over HTTP)
  auth.py        API tokens and per-token rate limit
  robots.py      robots.txt matcher (Google semantics)
```
