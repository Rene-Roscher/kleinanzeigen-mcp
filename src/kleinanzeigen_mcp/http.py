"""Polite, browser-consistent HTTP access to kleinanzeigen.de.

Design goals, in order:

1. **Don't hammer the site.** One process-wide token bucket + a concurrency
   cap shape *all* outbound traffic regardless of how many API/MCP clients are
   connected or how many proxies are configured. Caching and single-flight
   (see ``cache.py``) sit in front of it.
2. **Look like what we are: an ordinary browser session.** Consistent Chrome
   headers (UA, client hints and ``sec-fetch-*`` agree), a persistent cookie
   jar, HTTP/2, a referer chain and a homepage visit at session start.
3. **Back off immediately when told to.** 403/429/challenge pages put the
   affected session into an exponential cooldown during which it sends
   *nothing*; callers get answers from (stale) cache instead. Challenges are
   never solved or bypassed.

Optional proxies (HTTP(S)/SOCKS5) each get their own :class:`Session`. They
add resilience (one cooled-down egress doesn't stop the service) but share
the same global rate limit.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Settings
from .robots import Robots

log = logging.getLogger(__name__)

# Cookies that make kleinanzeigen "remember" the last search location/keyword
# and silently localise later pages. We want every request to be explicit.
_STICKY_COOKIES = ("__ka-ls", "__ka-sh")

_CHALLENGE_MARKERS = (
    "Access Denied",
    "bist du ein Mensch",
    "Bist du ein Mensch",
    "captcha-delivery",
    "/_sec/cp_challenge",
    "sec-if-cpt-container",
)


class UpstreamError(Exception):
    """kleinanzeigen.de could not be reached or answered unexpectedly."""


class BlockedError(UpstreamError):
    """We were rate limited / challenged. Nothing is sent upstream until ``retry_after``."""

    def __init__(self, msg: str, retry_after: float) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


class NotFoundError(UpstreamError):
    """The ad / seller does not exist (anymore)."""


class PolicyError(UpstreamError):
    """The request is not allowed by the configured robots policy."""


@dataclass(slots=True)
class FetchResult:
    url: str
    status: int
    text: str
    redirected_from: str | None = None


class TokenBucket:
    """Classic token bucket; ``acquire`` sleeps until a token is available."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.capacity = float(max(1, burst))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        async with self._lock:
            waited = 0.0
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return waited
                delay = (1 - self._tokens) / self.rate
                waited += delay
                await asyncio.sleep(delay)


@dataclass(slots=True)
class FetcherStats:
    requests: int = 0
    errors: int = 0
    blocks: int = 0
    retries: int = 0
    last_status: int | None = None
    total_wait_s: float = 0.0
    status_counts: dict[int, int] = field(default_factory=dict)


def parse_proxy_list(raw: str, file: str | None = None) -> list[str | None]:
    """Parse ``KA_PROXIES`` / ``KA_PROXY_FILE``. ``None`` stands for a direct connection."""
    items: list[str] = re.split(r"[,\n]", raw or "")
    if file:
        items += Path(file).read_text(encoding="utf-8").splitlines()
    out: list[str | None] = []
    for item in items:
        item = item.split("#", 1)[0].strip()
        if not item:
            continue
        if item.lower() == "direct":
            out.append(None)
            continue
        if "://" not in item:  # bare host:port → assume HTTP proxy
            item = "http://" + item
        scheme = urlsplit(item).scheme.lower()
        if scheme not in ("http", "https", "socks5", "socks5h"):
            raise ValueError(f"unsupported proxy scheme {scheme!r} in {item!r}")
        out.append(item)
    return out


def _redact(proxy: str | None) -> str:
    if proxy is None:
        return "direct"
    p = urlsplit(proxy)
    return f"{p.scheme}://{p.hostname}:{p.port}" if p.port else f"{p.scheme}://{p.hostname}"


class Session:
    """One 'browser': its own connection pool, cookie jar and block state."""

    def __init__(
        self, settings: Settings, proxy: str | None, transport: httpx.AsyncBaseTransport | None
    ) -> None:
        self.s = settings
        self.proxy = proxy
        self.label = _redact(proxy)
        self._transport = transport
        self.blocked_until = 0.0
        self.consecutive_blocks = 0
        self.down_until = 0.0  # network-level failure (dead proxy etc.), not a block
        self.consecutive_failures = 0
        self.requests = 0
        self.blocks = 0
        self.warmed_up = not settings.warmup
        self.warmup_lock = asyncio.Lock()
        self.client = self._new_client()

    def _base_headers(self) -> dict[str, str]:
        v = self.s.chrome_major
        return {
            "user-agent": self.s.user_agent,
            "accept-language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": f'"Google Chrome";v="{v}", "Not?A_Brand";v="8", "Chromium";v="{v}"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.s.platform,
        }

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.s.base_url,
            http2=self._transport is None,
            headers=self._base_headers(),
            timeout=httpx.Timeout(self.s.timeout_s, connect=10.0),
            follow_redirects=False,
            proxy=None if self._transport is not None else self.proxy,
            transport=self._transport,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=60),
        )

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self.blocked_until - time.monotonic())

    @property
    def available(self) -> bool:
        now = time.monotonic()
        return self.blocked_until <= now and self.down_until <= now

    def mark_down(self) -> float:
        self.consecutive_failures += 1
        pause = min(600.0, 30.0 * 2 ** (self.consecutive_failures - 1))
        self.down_until = time.monotonic() + pause
        return pause

    async def reset(self) -> None:
        """Drop cookies/connections and start a fresh session."""
        old, self.client = self.client, self._new_client()
        self.warmed_up = not self.s.warmup
        await old.aclose()

    def drop_sticky_cookies(self) -> None:
        jar = self.client.cookies.jar
        for c in list(jar):
            if c.name in _STICKY_COOKIES:
                jar.clear(c.domain, c.path, c.name)

    def info(self) -> dict:
        return {
            "egress": self.label,
            "requests": self.requests,
            "blocks": self.blocks,
            "cooldown_s": round(self.cooldown_remaining, 1),
            "down_s": round(max(0.0, self.down_until - time.monotonic()), 1),
        }


class Fetcher:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = settings
        self._bucket = TokenBucket(settings.rate_per_s, settings.burst)
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        proxies = parse_proxy_list(settings.proxies, settings.proxy_file)
        if proxies and settings.proxy_include_direct and None not in proxies:
            proxies.append(None)
        self.sessions = [Session(settings, p, transport) for p in (proxies or [None])]
        self._rr = itertools.cycle(range(len(self.sessions)))
        self.stats = FetcherStats()
        self._robots: Robots | None = None
        self._robots_at = 0.0
        self._robots_lock = asyncio.Lock()
        log.info("fetcher ready with %d session(s): %s", len(self.sessions),
                 ", ".join(s.label for s in self.sessions))

    async def aclose(self) -> None:
        await asyncio.gather(*(s.client.aclose() for s in self.sessions))

    # --------------------------------------------------------------- helpers
    @property
    def cooldown_remaining(self) -> float:
        """0 if at least one session is usable, else time until the first one is."""
        return min(s.cooldown_remaining for s in self.sessions)

    def _pick_session(self) -> Session:
        for _ in range(len(self.sessions)):
            sess = self.sessions[next(self._rr)]
            if sess.available:
                return sess
        if all(s.cooldown_remaining > 0 for s in self.sessions):
            left = self.cooldown_remaining
            raise BlockedError(f"cooling down after upstream block, {left:.0f}s left", left)
        # Only network-dead sessions left (e.g. every proxy down): try the one that
        # recovers first rather than failing outright.
        return min((s for s in self.sessions if s.cooldown_remaining <= 0), key=lambda s: s.down_until)

    def _nav_headers(self, referer: str | None) -> dict[str, str]:
        h = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
            "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin" if referer else "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "priority": "u=0, i",
        }
        if referer:
            h["referer"] = referer
        return h

    def _xhr_headers(self, referer: str | None) -> dict[str, str]:
        return {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-requested-with": "XMLHttpRequest",
            "priority": "u=1, i",
            "referer": referer or self.s.base_url + "/",
        }

    def _enter_cooldown(self, sess: Session, reason: str, retry_after: float | None) -> BlockedError:
        sess.consecutive_blocks += 1
        sess.blocks += 1
        self.stats.blocks += 1
        backoff = min(self.s.cooldown_max_s, self.s.cooldown_base_s * 2 ** (sess.consecutive_blocks - 1))
        if retry_after:
            backoff = max(backoff, min(retry_after, self.s.cooldown_max_s))
        sess.blocked_until = time.monotonic() + backoff
        log.warning("upstream block on %s (%s) – pausing this session for %.0fs", sess.label, reason, backoff)
        return BlockedError(f"kleinanzeigen.de blocked/limited the request ({reason})", backoff)

    @staticmethod
    def _looks_like_challenge(resp: httpx.Response) -> bool:
        if resp.status_code in (403, 429):
            return True
        if "html" not in resp.headers.get("content-type", ""):
            return False
        head = resp.text[:20_000]
        return any(m in head for m in _CHALLENGE_MARKERS) and "data-adid" not in head

    async def _warmup(self, sess: Session) -> None:
        """Land on the homepage once per session, like a person opening the site."""
        if sess.warmed_up:
            return
        async with sess.warmup_lock:
            if sess.warmed_up:
                return
            sess.warmed_up = True
            try:
                await self._send(sess, "/", self._nav_headers(None), allow_redirects=1)
            except BlockedError:
                raise
            except UpstreamError as e:  # warm-up is best-effort ...
                if "network error" in str(e) and len(self.sessions) > 1:
                    sess.warmed_up = False
                    raise  # ... but a dead proxy should be parked right away
                log.info("warm-up via %s failed: %s", sess.label, e)

    # ------------------------------------------------------------ core fetch
    async def _send(
        self, sess: Session, path: str, headers: dict[str, str], allow_redirects: int
    ) -> FetchResult:
        url = path
        redirected_from: str | None = None
        attempt = 0
        while True:
            if (left := sess.cooldown_remaining) > 0:
                raise BlockedError(f"cooling down after upstream block, {left:.0f}s left", left)
            async with self._sem:
                waited = await self._bucket.acquire()
                if self.s.jitter_s:
                    jitter = random.uniform(0, self.s.jitter_s)
                    waited += jitter
                    await asyncio.sleep(jitter)
                self.stats.total_wait_s += waited
                self.stats.requests += 1
                sess.requests += 1
                try:
                    resp = await sess.client.get(url, headers=headers)
                    _ = resp.text  # decode while we still hold the slot
                    error: Exception | None = None
                except (httpx.TransportError, httpx.ProxyError) as e:
                    error = e
                finally:
                    sess.drop_sticky_cookies()

            if error is not None:
                self.stats.errors += 1
                # With several egress sessions, fail over right away instead of retrying here.
                if attempt < self.s.max_retries and len(self.sessions) == 1:
                    attempt += 1
                    self.stats.retries += 1
                    await asyncio.sleep(1.5 * attempt + random.random())
                    continue
                raise UpstreamError(f"network error via {sess.label}: {error!r}") from error

            status = resp.status_code
            self.stats.last_status = status
            self.stats.status_counts[status] = self.stats.status_counts.get(status, 0) + 1

            if self._looks_like_challenge(resp):
                ra = resp.headers.get("retry-after")
                raise self._enter_cooldown(sess, f"HTTP {status}", float(ra) if ra and ra.isdigit() else None)

            if status in (301, 302, 303, 307, 308) and "location" in resp.headers:
                if allow_redirects <= 0:
                    return FetchResult(str(resp.url), status, "", redirected_from)
                redirected_from = redirected_from or str(resp.url)
                url = urljoin(str(resp.url), resp.headers["location"])
                allow_redirects -= 1
                continue

            if status in (404, 410):
                raise NotFoundError(f"not found: {resp.url}")

            if status >= 500:
                self.stats.errors += 1
                if attempt < self.s.max_retries:
                    attempt += 1
                    self.stats.retries += 1
                    await asyncio.sleep(2.0 * attempt + random.random())
                    continue
                raise UpstreamError(f"kleinanzeigen.de answered HTTP {status}")

            if status >= 400:
                raise UpstreamError(f"kleinanzeigen.de answered HTTP {status}")

            sess.consecutive_blocks = 0
            sess.consecutive_failures = 0
            return FetchResult(str(resp.url), status, resp.text, redirected_from)

    async def _robots_allows(self, path: str) -> bool:
        async with self._robots_lock:
            if self._robots is None or time.monotonic() - self._robots_at > 86_400:
                sess = self._pick_session()
                res = await self._send(sess, "/robots.txt", {"accept": "text/plain,*/*"}, allow_redirects=1)
                self._robots, self._robots_at = Robots(res.text), time.monotonic()
        return self._robots.allowed(path)

    async def _request(self, path: str, headers: dict[str, str], allow_redirects: int) -> FetchResult:
        if self.s.robots_policy == "strict" and not await self._robots_allows(path):
            raise PolicyError(f"{path} is disallowed by kleinanzeigen.de/robots.txt (KA_ROBOTS_POLICY=strict)")
        # A network failure on one proxy fails over to the next healthy session
        # (bounded by the pool size). Blocks are *not* retried elsewhere: the
        # caller gets BlockedError and falls back to cache.
        tried = 0
        while True:
            sess = self._pick_session()
            try:
                await self._warmup(sess)
                return await self._send(sess, path, headers, allow_redirects)
            except (BlockedError, NotFoundError, PolicyError):
                raise
            except UpstreamError as e:
                tried += 1
                if len(self.sessions) > 1 and "network error" in str(e):
                    pause = sess.mark_down()
                    log.info("network error via %s – parked for %.0fs, failing over", sess.label, pause)
                if tried >= len(self.sessions):
                    raise

    async def get_page(self, path: str, *, referer: str | None = None, max_redirects: int = 3) -> FetchResult:
        ref = referer or self.s.base_url + "/"
        return await self._request(path, self._nav_headers(ref), allow_redirects=max_redirects)

    async def get_json(self, path: str, *, referer: str | None = None) -> FetchResult:
        return await self._request(path, self._xhr_headers(referer), allow_redirects=0)

    def info(self) -> dict:
        return {
            "rate_per_s": self.s.rate_per_s,
            "burst": self.s.burst,
            "max_concurrency": self.s.max_concurrency,
            "requests": self.stats.requests,
            "errors": self.stats.errors,
            "retries": self.stats.retries,
            "blocks": self.stats.blocks,
            "status_counts": self.stats.status_counts,
            "avg_wait_s": round(self.stats.total_wait_s / self.stats.requests, 3) if self.stats.requests else 0,
            "sessions": [s.info() for s in self.sessions],
        }
