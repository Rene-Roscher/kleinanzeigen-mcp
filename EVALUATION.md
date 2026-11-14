# Evaluation: Kleinanzeigen-Zugriff für Agenten

Stand: 14.11.2026. Alle Zahlen sind live gegen kleinanzeigen.de gemessen, aus einem Cloud-Container ohne Proxy.

## 1. Wie Kleinanzeigen heute ausliefert

| Befund | Folge |
|---|---|
| **Akamai Bot Manager** davor (Cookies `_abck`, `bm_sz`) | Ein gewöhnlicher HTTP-Client kommt durch, wenn die Browser-Header stimmen. Ein Browser ist nicht nötig. |
| Die **Suchseite ist neu (Astro, Tailwind)**, die Klassen sind generiert und nicht semantisch | Ältere Scraper, die die Klassen `aditem-*` parsen, liefern auf der Suche nichts mehr. |
| Die Suchseite enthält die **Ergebnisliste strukturiert** in den `astro-island`-Props (`ImpressionTracker.resultAds`) | Das ist die stabilste Datenquelle: Verkäufer-ID, Anbietertyp, TOP-Flag, Attribute (km, EZ), Bilder, Versand. |
| **Detail- und Verkäuferseiten sind noch JSP** (`viewad-*`, `aditem`) | Diese Seiten parsen wir per DOM. Der Parser kann beide Markups. |
| Der Cookie `__ka-ls` merkt sich die letzte Suche bzw. den Ort | Er verfälscht spätere Seiten und wird nach jedem Request verworfen. |
| Ohne Keyword wird das letzte Pfadsegment als Keyword gelesen | Bei Suchen ohne Keyword fällt deshalb das `k0` im URL-Ende weg (über die Redirects von `/s-suchanfrage.html` verifiziert). |
| Die Standard-Sortierung ist „Relevanz“ | Für „aktuellste“ Anzeigen muss explizit `sortierung:neuste` gesetzt werden. TOP-Anzeigen stehen trotzdem oben und können Wochen alt sein. `latest_listings` filtert sie heraus. |
| Gelöschte Anzeigen leiten per 301 auf die Kategorie um | Der Server erkennt das und gibt `404` bzw. „Not found“ zurück. |
| Detailseiten enthalten teils die **volle Telefonnummer** im HTML | Der Parser liest sie bewusst nicht aus (Datenschutz). In den Fixtures ist sie geschwärzt. |

## 2. Messwerte

| Messung | Ergebnis |
|---|---|
| Suche (Upstream) | 0,4–1,2 s pro Seite (HTTP/2) |
| Parsen einer Ergebnisseite (27 Anzeigen) | 9–18 ms |
| Cache-Treffer (Suche oder Verkäufer) | 3–4 ms statt 1–2 s |
| 5 identische Calls parallel | 1 Upstream-Request (Single-Flight) |
| MCP-Testbatterie (9 Tools, 13 Calls inkl. 5 parallel) | 15 Upstream-Requests insgesamt |
| Dauertest: 31 Abrufe (15 Suchen, Seite 2–3, 8 Detailseiten, 6 Verkäufer) | 32 Requests in 57 s, **0 Blocks, 100 % HTTP 200** |
| Wiederholung desselben Dauertests | **0** Upstream-Requests (alles aus dem Cache) |
| Proxy-Pool: SOCKS5h + HTTP + ein toter Proxy | Alle Abfragen OK. Der tote Proxy wird nach 1 Fehlversuch 30 s geparkt (danach exponentiell länger). |

Zum Vergleich: [DanielWTE/ebay-kleinanzeigen-api](https://github.com/DanielWTE/ebay-kleinanzeigen-api), die Basis der meisten
bestehenden MCP-Projekte wie [Sprayer115/ebay-kleinanzeigen-api-mcp](https://github.com/Sprayer115/ebay-kleinanzeigen-api-mcp),
rendert jede Seite mit **Playwright/Chromium**. Laut README hat es keinen Cache und keine Auth, und zwischen den Seiten
wartet es 2 s. Ein echter Browser ist sehr nah am „normalen User“, kostet aber pro Seite Sekunden und
hunderte MB RAM. Hier reicht plain HTTP, weil die Daten ohnehin im HTML stecken.

## 3. Wie wir „nicht geblockt werden“, und was bewusst fehlt

**Umgesetzt:** Wir verhalten uns wie *eine* normale Browser-Session.
- Konsistenter Chrome-Fingerprint auf Header-Ebene: UA, `sec-ch-ua*`, `sec-fetch-*`, `accept-*` und `priority` passen zueinander. Dazu HTTP/2, brotli/zstd, Keep-Alive.
- Persistente Cookies (Akamai sieht eine durchgehende Session), ein Homepage-Besuch zum Sessionstart, Referer-Kette.
- **Ein globaler Token-Bucket** (Standard 0,5 req/s, Burst 4, maximal 2 parallel) plus Jitter. Das gilt für alle Clients und alle Proxys *zusammen*.
- Cache (Suche 60 s, Anzeige 10 min, Verkäufer 1 h, Kategorien 24 h, Orte 7 Tage), Single-Flight und Stale-on-Error.
- Bei 403, 429 oder einer Challenge-Seite sofort exponentieller Cooldown (60 s bis 15 min, `Retry-After` wird beachtet). Währenddessen geht **kein** Request raus, Antworten kommen aus dem (alten) Cache.
- Proxys sind optional. Jeder Proxy hat eigene Cookies und einen eigenen Cooldown. Ein geblockter Proxy wird *nicht* durch schnelleres Weiterfeuern über die anderen ausgeglichen, weil das globale Limit bleibt.

**Bewusst nicht umgesetzt:** CAPTCHA- oder Challenge-Lösen, Fälschen des TLS-Fingerprints (JA3/JA4), rotierende
User-Agents und Proxy-Rotation, um das Volumen hochzuskalieren. Das wäre aktive Umgehung von Schutzmaßnahmen. Es hätte ein
anderes rechtliches Gewicht und wird mit einem Wettrüsten gegen Akamai erkauft.

**Bekanntes Restrisiko:** Unser TLS-Fingerprint ist der von Python/httpx, nicht der von Chrome. Akamai *könnte* das
zusammen mit dem Chrome-UA als Widerspruch werten. Im Test ist das bei moderatem Tempo nicht passiert. Kommt es doch dazu,
sind die richtigen Hebel ein niedrigeres Tempo, längere TTLs und `KA_ROBOTS_POLICY=strict`, keine Tricks.

## 4. Rechtliches und robots.txt (bitte selbst bewerten)

- **Nutzungsbedingungen:** Kleinanzeigen untersagt nach meinem Kenntnisstand automatisierte Zugriffe bzw. das
  Auslesen von Daten. Ich habe die aktuelle Fassung nicht geprüft, bitte vor produktivem oder gewerblichem Einsatz selbst lesen.
  Für den privaten Gebrauch in kleinem Umfang ist das Risiko praktisch ein Block, kein Rechtsstreit. Bei kommerziellem
  Weiterverkauf der Daten sieht das anders aus.
- **robots.txt** (Gruppe `User-agent: *`):
  - erlaubt: Keyword-, Kategorie- und Ortssuche ohne Radius, Paginierung, Detailseiten (`/s-anzeige/…`), Kategorieliste;
  - **gesperrt:** Verkäuferseiten (`/s-bestandsliste.html`), alle `*.json` (Orts-Autocomplete, Aufrufzähler) und
    Filter-URLs (`/preis:`, `/sortierung:`, `/anbieter:`, `/anzeige:`, `/versand:`, `/direktkaufen:`, Radius `r5…r200`).
  - Die Muster sehen nach SEO-Steuerung des Crawl-Budgets aus, nicht nach einem Scraping-Verbot. Trotzdem sind sie ein
    klares Signal des Betreibers.
- Dieser Server läuft standardmäßig als **user-getriebener Agent** (`KA_ROBOTS_POLICY=off`): Jede Anfrage wird von einer
  konkreten Nutzerfrage ausgelöst, wie bei einem Browser. Mit **`KA_ROBOTS_POLICY=strict`** gehen nur Requests raus, die
  robots.txt erlaubt (live geladen, mit Google-Semantik geprüft). Preis, Anbieter, Versand und Sortierung werden dann lokal
  angewendet, `get_seller` und die Ortsauflösung über Namen sind gesperrt. Dafür ist die Datenqualität geringer: Es sind die
  relevantesten statt der neuesten Treffer, gefiltert auf den geladenen Seiten.
- **DSGVO:** Verkäufernamen sind personenbezogene Daten. Sie liegen nur im RAM-Cache (TTL ≤ 1 h) und werden nicht dauerhaft
  gespeichert. Telefonnummern werden nicht ausgelesen. `include_views` ist opt-in, weil der Endpunkt
  (`/s-vac-inc-get.json`) den Aufrufzähler der Anzeige **erhöht**.

## 5. Empfehlungen

1. **Eine Instanz zentral betreiben**, damit sich alle Agenten einen Cache und ein Rate-Limit teilen. Mehrere
   Instanzen würden das Tempo multiplizieren.
2. Das Tempo bei 0,5–1 req/s lassen. Für Agenten fühlt sich das dank Cache trotzdem schnell an.
3. `/v1/status` bzw. `service_status` beobachten: steigt `blocks` über 0, das Tempo senken.
4. `uv run pytest -m live` z. B. täglich laufen lassen. Kleinanzeigen fährt gerade zwei Frontends parallel und A/B-Tests,
   Markup-Änderungen sind wahrscheinlich.
5. Ausbauoptionen: persistenter Cache (SQLite/Redis) für mehrere Instanzen; Watches persistieren (aktuell im RAM, gehen
   beim Neustart verloren); Webhook/Push für `check_new_listings`; Kategorie-spezifische Filter (`extra` im `SearchQuery`
   wird schon unterstützt, ist aber noch nicht als Tool-Parameter freigegeben).
