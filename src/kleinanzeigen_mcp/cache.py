"""Small async TTL/LRU cache with single-flight and stale-on-error.

* **TTL + LRU** – bounded memory, per-entry expiry.
* **Single-flight** – N concurrent callers for the same key trigger exactly one
  upstream request; the rest await the same future. This matters a lot for
  MCP: agents love to fire the same lookup several times in parallel.
* **Stale-on-error** – if the loader fails (e.g. we are in a block cooldown)
  an expired entry younger than ``stale_max_age`` is served instead.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _Entry:
    value: Any
    stored_at: float
    expires_at: float


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    stale_served: int = 0
    coalesced: int = 0


class AsyncTTLCache:
    def __init__(self, max_entries: int = 5000, stale_max_age: float = 0.0) -> None:
        self._data: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._max = max_entries
        self._stale_max_age = stale_max_age
        self.stats = CacheStats()

    def __len__(self) -> int:
        return len(self._data)

    def peek(self, key: str) -> Any | None:
        e = self._data.get(key)
        if e and e.expires_at > time.monotonic():
            return e.value
        return None

    def set(self, key: str, value: Any, ttl: float) -> None:
        now = time.monotonic()
        self._data[key] = _Entry(value, now, now + ttl)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    async def get_or_load(
        self, key: str, ttl: float, loader: Callable[[], Awaitable[Any]]
    ) -> tuple[Any, bool]:
        """Return ``(value, from_cache)``."""
        now = time.monotonic()
        entry = self._data.get(key)
        if entry and entry.expires_at > now:
            self._data.move_to_end(key)
            self.stats.hits += 1
            return entry.value, True

        if (fut := self._inflight.get(key)) is not None:
            self.stats.coalesced += 1
            return await asyncio.shield(fut), True

        self.stats.misses += 1
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await loader()
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:
            stale = self._data.get(key)
            if stale is not None and time.monotonic() - stale.stored_at <= self._stale_max_age:
                self.stats.stale_served += 1
                fut.set_result(stale.value)
                return stale.value, True
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so asyncio doesn't warn if nobody else awaits
            raise
        else:
            self.set(key, value, ttl)
            fut.set_result(value)
            return value, False
        finally:
            self._inflight.pop(key, None)
