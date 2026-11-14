"""CLI: ``kleinanzeigen-mcp [stdio|serve|gen-token|search]``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import get_settings


def _stdio() -> None:
    from .client import KleinanzeigenClient
    from .mcp_server import build_mcp

    holder: dict[str, KleinanzeigenClient] = {}

    def get_client() -> KleinanzeigenClient:
        if "c" not in holder:
            holder["c"] = KleinanzeigenClient()
        return holder["c"]

    build_mcp(get_client).run("stdio")


def _serve(host: str | None, port: int | None) -> None:
    import uvicorn

    from .api import create_app

    s = get_settings()
    uvicorn.run(create_app(s), host=host or s.host, port=port or s.port, log_level=s.log_level.lower(),
                proxy_headers=True, forwarded_allow_ips="*")


def _gen_token(name: str) -> None:
    from .auth import generate_token, hash_token

    tok = generate_token()
    print(f"token (give this to the client):  {tok}")
    print(f"KA_API_TOKENS entry (plain):      {name}:{tok}")
    print(f"KA_API_TOKENS entry (hash only):  {name}:sha256:{hash_token(tok)}")


async def _search(args: argparse.Namespace) -> None:
    from .client import KleinanzeigenClient
    from .models import Sort
    from .urls import SearchQuery

    async with KleinanzeigenClient() as c:
        q = SearchQuery(
            query=args.query,
            location_id=await c.location_id(args.location),
            radius_km=args.radius,
            category_id=await c.category_id(args.category),
            price_min=args.price_min,
            price_max=args.price_max,
            sort=Sort(args.sort),
        )
        res = await c.search(q, limit=args.limit)
        if args.json:
            print(res.model_dump_json(indent=2, exclude_none=True))
            return
        print(f"{res.total} Treffer – {res.url}")
        for a in res.ads:
            flag = "★" if a.is_promoted else " "
            print(f"{flag} {a.posted_text:>14} | {a.price.text:>12} | {a.location[:28]:28} | {a.title[:70]}")
            print(f"  {'':14}   {a.url}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="kleinanzeigen-mcp")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("stdio", help="MCP over stdio (default; for Claude Code / Desktop)")
    sp = sub.add_parser("serve", help="HTTP server: REST /v1 + MCP /mcp")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    gp = sub.add_parser("gen-token", help="create an API token")
    gp.add_argument("name", nargs="?", default="client")
    qp = sub.add_parser("search", help="quick search from the terminal")
    qp.add_argument("query", nargs="?")
    qp.add_argument("-l", "--location")
    qp.add_argument("-r", "--radius", type=int)
    qp.add_argument("-c", "--category")
    qp.add_argument("--price-min", type=int)
    qp.add_argument("--price-max", type=int)
    qp.add_argument("-s", "--sort", default="newest", choices=["newest", "relevance", "price_asc", "price_desc"])
    qp.add_argument("-n", "--limit", type=int, default=25)
    qp.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=get_settings().log_level, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    match args.cmd:
        case None | "stdio":
            _stdio()
        case "serve":
            _serve(args.host, args.port)
        case "gen-token":
            _gen_token(args.name)
        case "search":
            asyncio.run(_search(args))
        case _:
            ap.print_help()


if __name__ == "__main__":
    main()
