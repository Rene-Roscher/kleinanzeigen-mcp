# kleinanzeigen-mcp

MCP-Server **und** REST-API für aktuelle Anzeigen von [kleinanzeigen.de](https://www.kleinanzeigen.de),
gebaut für Claude Code, Claude Desktop und andere MCP-Clients.

- **Schnell:** reines HTTP/2 statt Headless-Browser. Eine Suchseite lädt in ~0,5–1 s, das Parsen dauert ~10 ms.
- **Robust:** liest die strukturierten Daten, die die neue Suchseite als Astro-Props mitliefert.
  Für das alte Markup (Verkäuferseiten, A/B-Tests) gibt es einen DOM-Fallback.
- **Schonend:** Ein globales Rate-Limit gilt für alle Clients zusammen, dazu TTL-Cache, Single-Flight
  und Stale-on-Error. Bei 403 oder 429 pausiert der Server sofort.
- **Sicher:** API-Tokens (auch nur als SHA-256-Hash speicherbar) und ein Rate-Limit pro Token.
- **Optional mit Proxys:** HTTP, HTTPS, SOCKS5 und SOCKS5h. Jeder Proxy bekommt eine eigene Session.

## Schnellstart

```bash
uv sync
uv run kleinanzeigen-mcp search "ddr5 32gb" -l München -r 50 --price-max 200
```

### In Claude Code einbinden: lokal über stdio

```bash
claude mcp add kleinanzeigen -- uv run --directory /pfad/zu/kleinanzeigen-mcp kleinanzeigen-mcp
```

### In Claude Code einbinden: als Server über HTTP mit Token

```bash
uv run kleinanzeigen-mcp gen-token claude-code   # gibt den Token und den KA_API_TOKENS-Eintrag aus
KA_API_TOKENS="claude-code:sha256:<hash>" uv run kleinanzeigen-mcp serve --host 0.0.0.0 --port 8080
```

```bash
claude mcp add --transport http kleinanzeigen https://dein-host/mcp --header "Authorization: Bearer ka_..."
```

Alternativ als `.mcp.json` im Projekt:

```json
{
  "mcpServers": {
    "kleinanzeigen": {
      "type": "http",
      "url": "https://dein-host/mcp",
      "headers": { "Authorization": "Bearer ${KLEINANZEIGEN_TOKEN}" }
    }
  }
}
```

### Docker

```bash
cp .env.example .env    # KA_API_TOKENS eintragen
docker compose up -d --build
```

## MCP-Tools

| Tool | Zweck |
|---|---|
| `search_listings` | Suche mit Filtern: Ort/PLZ + Radius, Kategorie (Name oder ID), Preis, privat/gewerblich, Angebot/Gesuch, Versand, „Direkt kaufen“, Sortierung, bis zu 5 Seiten |
| `latest_listings` | Neueste organische Anzeigen ohne bezahlte TOP-Platzierungen, optional `max_age_minutes` |
| `check_new_listings` | Gespeicherte Suche abfragen: liefert nur Anzeigen, die diese Watch noch nicht gesehen hat |
| `find_deals` | **Günstigste realistische Angebote** im Vergleich zum aktuellen Marktmedian. Die Top-Kandidaten bekommen einen Scam-Risiko-Score |
| `assess_listing` | Scam-Risiko einer einzelnen Anzeige, inklusive Preisvergleich mit ähnlichen Angeboten |
| `get_listing` / `get_listings` | Volle Details: Beschreibung, Attribute, alle Bilder, Koordinaten, Versand, Verkäufer, optional Aufrufe |
| `get_seller` | Verkäuferprofil (Bewertung, Badges, aktiv seit, Antwortzeit) und seine Anzeigen, 1 h gecacht |
| `find_location` | Ort oder PLZ in eine Kleinanzeigen-Location-ID auflösen |
| `list_categories` | Kategoriebaum mit IDs |
| `service_status` | Cache, Rate-Limit und Zustand der Upstream-Sessions |

Jede Ergebnis-Anzeige enthält schon `seller_id` und `seller_type`. Um Verkäufer zu filtern, ist also kein Extra-Request nötig.

## Deals & Scam-Check

`find_deals("ddr5 32gb", must_include=["32gb|2x16", "ddr5"], exclude=["sodimm", "laptop"])` arbeitet in vier Schritten:

1. **Marktpreis:** aus ca. 75 aktuellen Angeboten. Gesuche, „Ankauf“, „defekt“ und Platzhalterpreise fliegen raus, Ausreißer werden per IQR entfernt.
   Ohne `category` wird die Kategorie automatisch gewählt, und zwar die, in der der *Artikel selbst* steht, nicht Bundles,
   die ihn enthalten (z. B. RAM statt Mini-PCs mit 32 GB DDR5). Die Aufschlüsselung steht in `market.category_breakdown`.
2. **Deals:** nach Preis aufsteigend zwischen `min_ratio × Median` (Standard 30 %) und dem Median.
3. **Risiko-Check** für die `check_top` günstigsten: Detailseite und Verkäuferprofil, daraus ein Score von 0 bis 100 (`low`, `medium` ab 30, `high` ab 60).
4. **`best_low_risk_id`:** das günstigste geprüfte Angebot mit niedrigem Risiko.

| Signal | Punkte |
|---|---|
| Preis < 50 % / < 70 % vom Median | +30 / +15 |
| Konto ≤ 3 / ≤ 14 / ≤ 60 Tage alt | +40 / +30 / +15 |
| Konto ≤ 30 Tage **und** Preis ≤ Median („unter Schnitt oder genau passend“) | +20 |
| Altes Konto, aber ≥ 70 % der Anzeigen in den letzten 48 h → **mögliches gekapertes Konto** | +25 |
| Anzeigen des Verkäufers in ≥ 3 PLZ-Regionen | +15 |
| WhatsApp, E-Mail oder Telefon im Text / PayPal F&F, Western Union, Gutscheine / nur Vorkasse | +20 / +25 / +10 |
| Im Ausland, Abholung ausgeschlossen, kaum Fotos, keine Bewertung | +5 … +15 |
| TOP / OK, Zuverlässigkeits-Badge, Konto ≥ 3 Jahre, nur Abholung, Rechnung, unauffällige Historie | −15 … −5 |

Liegt der Preis unter 50 % vom Median, zählen Vertrauenssignale höchstens mit −20. Ein altes, gut bewertetes Konto
neutralisiert so einen Preis also nicht komplett, denn genau so sieht ein gekapertes Konto aus. `hints` enthält
Hinweise zur Qualität des Angebots, keine Scam-Signale: Stückpreis statt Kit, Titel 32 GB aber Beschreibung 16 GB,
defekt, unplausibel niedriger Preis. Alles ist eine **Heuristik mit Begründung, kein Urteil**.

## REST-API

Die OpenAPI-Doku liegt unter `/docs`. Jeder Request braucht `Authorization: Bearer <token>` oder `X-API-Key`.

```
GET  /v1/search?query=ddr5&location=Berlin&radius_km=20&price_max=150&seller_type=private&sort=newest
GET  /v1/latest?query=ddr5&category=PC-Zubehör&max_age_minutes=60
GET  /v1/deals?query=ddr5%2032gb&must_include=32gb|2x16&exclude=sodimm&check_top=5
GET  /v1/ads/{id}/risk
GET  /v1/ads/{id}?include_views=true
GET  /v1/sellers/{user_id}
GET  /v1/locations?q=80331
GET  /v1/categories
POST /v1/watches/{name}/check      Body: {"query": "ddr5", "location": "Hamburg"}
GET  /v1/status       GET /health (ohne Auth)
```

Fehler: `400` für ungültige Eingaben, `404` für gelöschte Anzeigen, `429` wenn das Token-Limit erreicht ist,
`503` mit `Retry-After`, wenn Kleinanzeigen bremst.

## Konfiguration (Umgebungsvariablen `KA_*`, alternativ `.env`)

| Variable | Default | Bedeutung |
|---|---|---|
| `KA_API_TOKENS` | leer | `name:token` oder `name:sha256:<hex>`, kommagetrennt. Leer heißt: keine Auth (nur für lokal gedacht) |
| `KA_CLIENT_RATE_PER_MIN` | 60 | Requests pro Minute und Token an diesen Server |
| `KA_RATE_PER_S` / `KA_BURST` | 0.5 / 4 | **Globales** Tempo Richtung kleinanzeigen.de |
| `KA_MAX_CONCURRENCY` | 2 | Gleichzeitige Upstream-Requests |
| `KA_TTL_SEARCH` / `_AD` / `_SELLER` | 60 / 600 / 3600 | Cache-Dauer in Sekunden |
| `KA_STALE_MAX_AGE_S` | 21600 | Bis zu diesem Alter darf bei Block oder Fehler ein abgelaufener Cache-Eintrag ausgeliefert werden |
| `KA_PROXIES` | leer | z. B. `socks5h://u:p@h:1080,http://u:p@h2:8080,direct` |
| `KA_PROXY_FILE` | – | Datei mit einem Proxy pro Zeile, `#`-Kommentare erlaubt |
| `KA_USER_AGENT` / `KA_CHROME_MAJOR` | Chrome 141 / Win | Müssen zueinander passen, weil daraus die `sec-ch-ua`-Header gebaut werden |
| `KA_ALLOWED_HOSTS` | leer | Host-Allowlist für `/mcp` (Schutz gegen DNS-Rebinding) |
| `KA_ROBOTS_POLICY` | `off` | `strict`: nur URLs, die robots.txt erlaubt. Filter werden dann lokal angewendet, Verkäuferseiten und JSON-Endpunkte sind gesperrt (siehe EVALUATION.md) |

## Architektur

```
MCP (stdio | /mcp)  ─┐
REST /v1            ─┼─► KleinanzeigenClient ─► AsyncTTLCache (TTL+LRU, Single-Flight, Stale-on-Error)
                     │                        └► Fetcher ─► TokenBucket (global) ─► Session-Pool
Auth + Token-Limit ──┘                                       (je Proxy: Cookies, HTTP/2, Cooldown)
                                                 Parser: Astro-Props → DOM-Fallback (neu + legacy)
```

## Tests

```bash
uv run pytest            # Offline-Tests mit echten, anonymisierten HTML-Fixtures
uv run ruff check src tests
```

Mehr zu Messungen, Risiken und Grenzen steht in [EVALUATION.md](EVALUATION.md).
