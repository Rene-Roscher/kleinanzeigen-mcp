"""HTTP server: REST API under ``/v1`` and MCP (streamable HTTP) under ``/mcp``."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from .auth import AuthMiddleware, parse_tokens
from .client import KleinanzeigenClient
from .config import Settings, get_settings
from .http import BlockedError, NotFoundError, PolicyError, UpstreamError
from .mcp_server import build_mcp
from .models import AdDetail, AdSummary, AdType, Category, Location, SearchResult, Seller, SellerType, Sort
from .urls import SearchQuery

log = logging.getLogger(__name__)


class SearchParams(BaseModel):
    query: str | None = None
    location: str | None = None
    location_id: str | None = None
    radius_km: int | None = None
    category: str | None = None
    price_min: int | None = None
    price_max: int | None = None
    seller_type: SellerType | None = None
    ad_type: AdType | None = None
    shipping: bool | None = None
    buy_now: bool = False


class WatchResult(BaseModel):
    watch: str
    first_check: bool
    new_count: int
    ads: list[AdSummary]


def _transport_security(s: Settings, has_tokens: bool) -> TransportSecuritySettings | None:
    hosts = [h.strip() for h in s.allowed_hosts.split(",") if h.strip()]
    if hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                         allowed_origins=[f"https://{h}" for h in hosts]
                                         + [f"http://{h}" for h in hosts])
    if has_tokens:
        # A DNS-rebinding page can't know the bearer token, so the Host check adds nothing
        # but breaks reverse-proxy setups.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return None  # SDK default: protection on for localhost binds


def create_app(settings: Settings | None = None, client: KleinanzeigenClient | None = None) -> FastAPI:
    s = settings or get_settings()
    state: dict[str, KleinanzeigenClient] = {}
    if client is not None:
        state["client"] = client

    def get_client() -> KleinanzeigenClient:
        if "client" not in state:
            state["client"] = KleinanzeigenClient(s)
        return state["client"]

    tokens = parse_tokens(s.api_tokens)
    mcp = build_mcp(get_client)
    mcp_starlette = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        host=s.host, transport_security=_transport_security(s, bool(tokens)),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        get_client()
        async with mcp.session_manager.run():
            yield
        await state["client"].aclose()

    app = FastAPI(
        title="Kleinanzeigen API",
        version="0.1.0",
        description="Fresh Kleinanzeigen.de listings – cached, rate limited, token protected. "
        "MCP endpoint: /mcp (streamable HTTP).",
        lifespan=lifespan,
    )
    # MCP routes go straight into the router (no Mount → no /mcp → /mcp/ redirect).
    for route in mcp_starlette.routes:
        app.router.routes.append(route)

    if not tokens:
        log.warning("KA_API_TOKENS is empty – the HTTP server is UNAUTHENTICATED")
    app.add_middleware(AuthMiddleware, tokens=tokens, per_minute=s.client_rate_per_min)

    # ------------------------------------------------------------ errors
    @app.exception_handler(BlockedError)
    async def _blocked(_: Request, e: BlockedError):
        return JSONResponse({"error": "upstream_rate_limited", "detail": str(e)}, status_code=503,
                            headers={"Retry-After": str(int(e.retry_after) + 1)})

    @app.exception_handler(NotFoundError)
    async def _nf(_: Request, e: NotFoundError):
        return JSONResponse({"error": "not_found", "detail": str(e)}, status_code=404)

    @app.exception_handler(PolicyError)
    async def _policy(_: Request, e: PolicyError):
        return JSONResponse({"error": "forbidden_by_policy", "detail": str(e)}, status_code=403)

    @app.exception_handler(UpstreamError)
    async def _up(_: Request, e: UpstreamError):
        return JSONResponse({"error": "upstream_error", "detail": str(e)}, status_code=502)

    @app.exception_handler(ValueError)
    async def _val(_: Request, e: ValueError):
        return JSONResponse({"error": "bad_request", "detail": str(e)}, status_code=400)

    async def to_query(p: SearchParams, sort: Sort = Sort.newest, page: int = 1) -> SearchQuery:
        c = get_client()
        return SearchQuery(
            query=p.query,
            location_id=p.location_id or await c.location_id(p.location),
            radius_km=p.radius_km,
            category_id=await c.category_id(p.category),
            price_min=p.price_min,
            price_max=p.price_max,
            sort=sort,
            seller_type=p.seller_type,
            ad_type=p.ad_type,
            shipping=p.shipping,
            buy_now=p.buy_now,
            page=page,
        )

    # ------------------------------------------------------------ routes
    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, Any]:
        cd = get_client().fetcher.cooldown_remaining
        return {"ok": True, "upstream_cooldown_s": round(cd, 1)}

    @app.get("/v1/status")
    async def status() -> dict[str, Any]:
        return get_client().status()

    @app.get("/v1/search", response_model=SearchResult, response_model_exclude_none=True)
    async def search(
        p: Annotated[SearchParams, Query()],
        sort: Sort = Sort.newest,
        page: Annotated[int, Query(ge=1, le=50)] = 1,
        pages: Annotated[int, Query(ge=1, le=5)] = 1,
        include_promoted: bool = True,
        limit: Annotated[int | None, Query(ge=1, le=125)] = None,
    ):
        q = await to_query(p, sort, page)
        return await get_client().search(q, pages=pages, include_promoted=include_promoted, limit=limit)

    @app.get("/v1/latest", response_model=SearchResult, response_model_exclude_none=True)
    async def latest(
        p: Annotated[SearchParams, Query()],
        max_age_minutes: Annotated[int | None, Query(ge=1)] = None,
        pages: Annotated[int, Query(ge=1, le=5)] = 1,
        limit: Annotated[int | None, Query(ge=1, le=125)] = 25,
    ):
        q = await to_query(p)
        return await get_client().latest(q, max_age_minutes=max_age_minutes, limit=limit, pages=pages)

    @app.get("/v1/ads/{ad_id}", response_model=AdDetail, response_model_exclude_none=True)
    async def ad(ad_id: Annotated[str, Path(pattern=r"^\d{6,}$")], include_views: bool = False):
        return await get_client().get_ad(ad_id, include_views=include_views)

    @app.get("/v1/sellers/{user_id}", response_model=Seller, response_model_exclude_none=True)
    async def seller(user_id: Annotated[str, Path(pattern=r"^\d+$")],
                     page: Annotated[int, Query(ge=1, le=20)] = 1):
        return await get_client().get_seller(user_id, page=page)

    @app.get("/v1/locations", response_model=list[Location])
    async def locations(q: Annotated[str, Query(min_length=2)]):
        return await get_client().resolve_location(q)

    @app.get("/v1/categories", response_model=list[Category], response_model_exclude_none=True)
    async def categories():
        return await get_client().categories()

    @app.post("/v1/watches/{name}/check", response_model=WatchResult, response_model_exclude_none=True)
    async def watch_check(request: Request, name: Annotated[str, Path(max_length=80)], p: SearchParams,
                          emit_initial: bool = True):
        q = await to_query(p)
        owner = request.scope.get("state", {}).get("token_name", "anonymous")
        new, first = await get_client().check_watch(f"{owner}|{name}|{q.path()}", q, emit_initial=emit_initial)
        return WatchResult(watch=name, first_check=first, new_count=len(new), ads=new)

    return app
