import json

import httpx
from fastapi.testclient import TestClient

from kleinanzeigen_mcp.api import create_app
from kleinanzeigen_mcp.auth import hash_token, parse_tokens
from kleinanzeigen_mcp.client import KleinanzeigenClient
from kleinanzeigen_mcp.config import Settings

TOKEN = "ka_test_token_123"


def make(fixture, **kw):
    html = fixture("search_new.html.gz")
    detail = fixture("ad_private.html.gz")
    locs = fixture("locations_berlin.json")

    def handler(r: httpx.Request):
        if r.url.path.startswith("/s-anzeige/"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text=detail)
        if r.url.path == "/s-ort-empfehlungen.json":
            return httpx.Response(200, headers={"content-type": "application/json"}, text=locs)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    s = Settings(_env_file=None, rate_per_s=1000, burst=100, jitter_s=0, warmup=False,
                 api_tokens=f"alice:{TOKEN},bob:sha256:{hash_token('other')}", **kw)
    client = KleinanzeigenClient(s, transport=httpx.MockTransport(handler))
    return create_app(s, client=client)


def test_token_parsing():
    t = parse_tokens("raw, a:b:c, n:sha256:" + "00" * 32)
    assert [x.name for x in t] == ["token1", "a", "n"]


def test_auth_required(fixture):
    with TestClient(make(fixture)) as tc:
        assert tc.get("/health").status_code == 200
        assert tc.get("/v1/search", params={"query": "iphone"}).status_code == 401
        assert tc.get("/v1/search", params={"query": "iphone"},
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = tc.get("/v1/search", params={"query": "iphone", "limit": 5},
                   headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["ads"]) == 5 and body["total"] == 159120
        assert tc.get("/v1/ads/3571224603", headers={"X-API-Key": "other"}).json()["id"] == "3571224603"


def test_bad_request_and_location(fixture):
    with TestClient(make(fixture)) as tc:
        h = {"Authorization": f"Bearer {TOKEN}"}
        assert tc.get("/v1/search", headers=h).status_code == 400  # no criteria
        r = tc.get("/v1/latest", params={"query": "iphone", "location": "Berlin"}, headers=h)
        assert r.status_code == 200 and "l3331" in r.json()["url"]


def test_per_token_rate_limit(fixture):
    with TestClient(make(fixture, client_rate_per_min=3)) as tc:
        h = {"Authorization": f"Bearer {TOKEN}"}
        codes = [tc.get("/v1/status", headers=h).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]


def test_mcp_over_http(fixture):
    with TestClient(make(fixture)) as tc:
        h = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream",
             "Content-Type": "application/json"}
        assert tc.post("/mcp", json={}, headers={"Accept": h["Accept"]}).status_code == 401
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        r = tc.post("/mcp", json=init, headers=h)
        assert r.status_code == 200, r.text
        r = tc.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    headers={**h, "mcp-protocol-version": "2025-06-18"})
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert {"search_listings", "latest_listings", "get_listing", "get_seller",
                "check_new_listings", "find_location", "list_categories"} <= names
        r = tc.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "latest_listings",
                                             "arguments": {"query": "iphone", "limit": 3}}},
                    headers={**h, "mcp-protocol-version": "2025-06-18"})
        res = r.json()["result"]
        assert not res.get("isError"), res
        data = res.get("structuredContent") or json.loads(res["content"][0]["text"])
        assert len(data["ads"]) == 3


def test_non_utf8_header_does_not_crash(fixture):
    with TestClient(make(fixture)) as tc:
        r = tc.get("/v1/status", headers={"Authorization": f"Bearer {TOKEN}", "X-Odd": "caf\xe9".encode("latin-1")})
        assert r.status_code == 200


def test_mcp_watches_are_scoped_per_token(fixture):
    from kleinanzeigen_mcp.auth import hash_token  # noqa: F401
    app = make(fixture)
    with TestClient(app) as tc:
        def call(token):
            h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                 "mcp-protocol-version": "2025-06-18"}
            tc.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "1"}}})
            r = tc.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "check_new_listings",
                                   "arguments": {"watch_name": "same", "query": "iphone", "emit_initial": False}}})
            res = r.json()["result"]
            return res.get("structuredContent") or json.loads(res["content"][0]["text"])

        assert call(TOKEN)["first_check"] is True
        assert call(TOKEN)["first_check"] is False
        assert call("other")["first_check"] is True  # bob has his own watch with the same name
