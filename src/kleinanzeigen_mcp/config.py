"""Runtime configuration (env vars prefixed with ``KA_``, or a ``.env`` file)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# A current, unremarkable desktop Chrome on Windows. Keep it in sync with the
# client-hint headers in ``http.py`` – a UA that contradicts its own
# ``sec-ch-ua`` headers is more suspicious than either one alone.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KA_", env_file=".env", extra="ignore")

    # --- upstream ---------------------------------------------------------
    base_url: str = "https://www.kleinanzeigen.de"
    user_agent: str = DEFAULT_USER_AGENT
    chrome_major: int = Field(141, description="Must match the Chrome version in user_agent.")
    platform: str = Field('"Windows"', description="sec-ch-ua-platform value, quoted.")
    timeout_s: float = 15.0
    # Optional outbound proxies. Comma/newline separated URLs, e.g.
    #   http://user:pass@1.2.3.4:8080, socks5://user:pass@5.6.7.8:1080, socks5h://host:1080
    # ``direct`` is a valid entry meaning "no proxy". Every entry becomes its own
    # browser session (own cookies, own cooldown). The global rate limit below is
    # SHARED by all of them – more proxies do not mean more requests.
    proxies: str = ""
    proxy_file: str | None = Field(None, description="File with one proxy URL per line (# comments ok).")
    # also include a direct (no-proxy) session in the pool when proxies are configured
    proxy_include_direct: bool = False
    warmup: bool = Field(True, description="Visit the homepage once at session start.")
    # "off": behave like a browser acting for a user (default).
    # "strict": only request URLs kleinanzeigen's robots.txt allows. Filters the
    # site disallows (price, seller type, shipping, radius, sort, ...) are then
    # applied locally; seller pages, view counter and location JSON are refused.
    robots_policy: str = Field("off", pattern="^(off|strict)$")

    # --- politeness / anti-block -----------------------------------------
    # Sustained request rate to kleinanzeigen.de across *all* clients of this
    # server. A human clicking around does maybe one page every few seconds;
    # 0.5 rps with a small burst stays well inside that envelope.
    rate_per_s: float = Field(0.5, gt=0)
    burst: int = Field(4, ge=1)
    max_concurrency: int = Field(2, ge=1)
    jitter_s: float = 0.4
    max_retries: int = 2
    cooldown_base_s: float = 60.0
    cooldown_max_s: float = 900.0

    # --- cache TTLs (seconds) --------------------------------------------
    ttl_search: int = 60
    ttl_ad: int = 600
    ttl_seller: int = 3600
    ttl_views: int = 300
    ttl_categories: int = 86_400
    ttl_locations: int = 7 * 86_400
    cache_max_entries: int = 5000
    # If upstream blocks us we may answer from expired cache entries up to this age.
    stale_max_age_s: int = 6 * 3600

    # --- our own API ------------------------------------------------------
    # Comma separated list. Each entry is either a raw token, ``name:token`` or
    # ``name:sha256:<hex>`` (store only the hash). Empty = auth disabled
    # (only sensible for local stdio use).
    api_tokens: str = ""
    client_rate_per_min: int = 60
    # Host header allow-list for the MCP endpoint (DNS-rebinding protection), e.g.
    # "mcp.example.com,localhost:*". Empty = protection on only when no tokens are set.
    allowed_hosts: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
