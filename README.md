# Supermarket Price Monitor — Sklavenitis (starter)

A polite, robots.txt-respecting price scraper with full price history,
built as the foundation for cross-retailer price analysis.

## What it does
- Fetches `robots.txt` and **refuses disallowed paths**; honors `crawl-delay`.
- Rate-limits (default 3s/request) and backs off on 429/5xx.
- Discovers product URLs via the site's **sitemap** (the polite way).
- Parses prices from **JSON-LD structured data** (stable) before any
  fragile HTML scraping.
- Stores a **time series** of prices in SQLite (`prices.db`) — required
  for any later trend / anomaly analysis.

## Setup
```bash
pip install -r requirements.txt
```

## Run (start SMALL)
```bash
# Sklavenitis (category harvesting, ~24 products/page)
python main.py scrape --retailer sklavenitis --limit 50 --max-categories 2

# AB Βασιλόπουλος (ab.gr — product pages via sitemap, robots-compliant)
python main.py scrape --retailer ab --limit 50

# Bazaar (bazaar-online.gr — OpenCart, category listing harvest)
python main.py scrape --retailer bazaar --limit 200

# Mymarket (mymarket.gr — server-rendered, JSON-LD + cards, ?page=N)
python main.py scrape --retailer mymarket --limit 200

# ⭐ posokanei (posokanei.gov.gr — official government API, ALL chains, matched)
python main.py scrape --retailer posokanei --limit 500

python main.py show
```

## ⭐ posokanei (posokanei.gov.gr) — the recommended source
The official government price-comparison platform, via its public REST API
(api.posokanei.gov.gr). robots.txt allows bots (Crawl-delay: 1); the API needs
no auth. It covers ~8,400 products across all 10 Greek chains (Sklavenitis, AB,
Masoutis, My Market, Lidl, Galaxias, SYNKA, Kritikos, Market In, Halkiadakis)
plus European comparison retailers — and products are ALREADY MATCHED across
chains (each /products/{id} returns one product's price at every retailer).

This single source replaces the individual scrapers and, crucially, solves the
cross-chain product-matching problem (every retailer row shares the posokanei
product id as a key) — including the two chains that resisted direct scraping
(Masoutis: auth-gated; Lidl: WAF).

For sustained/automated use, consider asking the operators (Υπ. Ανάπτυξης /
Independent Market Authority) for an official data feed.

## How Lidl (lidl-hellas.gr) works
Nuxt SSR. Product data is in each page's JSON-LD + meta description
("<name> - <price>€ (<date>)"). Discovery via the sitemap product feed;
one product per fetch. Some Lidl items are in-store-only with NO online price
("- undefined€") and are skipped.

Lidl runs an aggressive WAF — automated requests can draw 403s. The module is
deliberately SERIAL (no concurrency) and STOPS after repeated blocks rather
than trying to evade. Do NOT add UA rotation/proxies to bypass blocks; if Lidl
refuses sustained crawling, use the government price platform instead.

## How Mymarket (mymarket.gr) works
Server-rendered (Hotwired/Stimulus). Category pages embed a JSON-LD ItemList
(clean name/sku/url/brand) plus HTML cards with the displayed price; the module
pairs them by sku (the JSON-LD price is a per-unit base, not the shown price).
Discovery uses /sitemap/categories.xml; pagination is ?page=N. Mymarket sits
behind Imperva Incapsula (WAF) — if requests get challenged, raise the crawl
delay rather than evading. No EAN exposed.

## Note on Masoutis (masoutis.gr)
Not included: its product data is served only via a credentialed API that
returns HTTP 403 without the app's session key/PassKey. Replaying those to
defeat the auth gate isn't something this project does. For Masoutis, use the
government price platform (posokanei / e-Katanalotis) instead.

## How Bazaar (bazaar-online.gr) works
Bazaar runs OpenCart (server-rendered HTML). No declared sitemap, so the
module discovers leaf categories from the homepage navigation (~790 links),
then harvests each category's listing pages, paginating via `?page=N` (16
products/page). Each product card carries id, name, price, unit price, and
packaging; weighed items are priced per-kilo, piece items show a final price.
Sales (`price-old`/`price-new`) are captured. No EAN exposed.

## How AB (ab.gr) works
ab.gr is a Next.js + SAP Commerce (Hybris) site. Its robots.txt **disallows**
`/search`, `/api/*search`, `/en/`, and account/checkout paths, but **allows**
product pages (`/el/eshop/.../p/{code}`) and the sitemap. So the AB module
discovers Greek product URLs from the sitemap and parses each product page's
schema.org JSON-LD (price is nested under `offers.priceSpecification.price`).
We never touch disallowed paths. One product per fetch, so AB crawls slower
than Sklavenitis — bound runs with `--limit`.

Note: AB does not expose EAN barcodes, so cross-chain product matching with
Sklavenitis must rely on name/brand/size rather than a shared barcode.

## How it works (Sklavenitis)
Sklavenitis is a Vue SPA with no JSON-LD. Prices live in the HTML of
**category/listing pages**, where each product sits in a
`<div class="product prGa_{sku}">` card containing its sku, stock, current
price, previous price (discount), and unit price. So the scraper **harvests
category pages** (~24 products each) rather than fetching product pages one by
one — far fewer requests, and it captures discounts and per-unit prices for free.

## Before you scale up — read this
1. **Check Sklavenitis's Terms of Service.** robots.txt allowing a path is a
   technical signal, not legal permission. The ToS governs.
2. Keep volume low and the crawl delay generous. You are a guest.
3. Identify your bot honestly: edit `USER_AGENT` in `fetcher.py` with a real
   contact URL/email.
4. If `scrape` records 0 products, the sitemap discovery needs tuning —
   inspect a real product URL and adjust `PRODUCT_URL_HINT` in
   `sklavenitis.py`. If product pages have no JSON-LD, add an HTML fallback
   in `scrape_product()`.

## Next steps (the actual goal)
- Add the other four retailers as sibling modules (same interface: a `run()`
  that discovers + parses into the shared store).
- **Product matching**: map the same product across chains (by barcode/EAN
  where available — far more reliable than name matching).
- **Analysis layer**: from `price_history`, compute per-product price
  timelines across retailers and flag *patterns worth investigating* —
  e.g. simultaneous identical price changes. Treat these as **leads for the
  Competition Commission**, never as conclusions. Parallel pricing is legal;
  only an authority with subpoena power can establish collusion.

## Browse the data (desktop app)
A PySide6 GUI to search products and view price history:
```bash
pip install PySide6
python app.py                 # uses prices.db
python app.py path/to.db      # or point at another database
```
Search by name, toggle "On offer" / "In stock", click a row to see that
product's price-history chart (price line, observation markers, and the
"was" list price when on offer).

## Files
- `fetcher.py`  — polite HTTP layer (robots.txt, rate-limit, backoff)
- `parser.py`   — JSON-LD product extraction (shared)
- `storage.py`  — SQLite schema + price history
- `sklavenitis.py` — retailer-specific discovery/parsing
- `main.py`     — CLI

## Local web app (research UI)

A browser-based interface over your `prices.db`, as an alternative to the
PySide6 desktop GUI. It's a LOCAL research tool: it binds to 127.0.0.1 only and
is not intended as a public service.

```
pip install flask
python webapp.py
# open http://127.0.0.1:5000
```

Features:
- Search across all products (Greek/accent-insensitive).
- Per-product cross-chain price spread (cheapest → dearest, with the gap shown).
- Price-history chart overlaying every chain's series for the selected product.
- Filter by chain; dataset summary (products, chains, multi-chain count).

Files: `webapp.py` (Flask backend + JSON API) and `webapp_ui.html` (the UI).
Point at a different database with `PRICE_DB=/path/to/prices.db python webapp.py`.

Note: search is Unicode-aware because storage registers a custom SQLite
`normalize()` function (the built-in `LOWER()` is ASCII-only and breaks on Greek).

## Running with PostgreSQL + Docker (for parallel collections)

SQLite allows only one writer at a time, so running several scrapers in
parallel can hit "database is locked". Postgres handles concurrent writers
natively. The storage layer supports both backends behind one interface.

### Quick start (Docker)
```
docker compose up -d        # starts Postgres + the web app
# web app:  http://localhost:5000
```

Run scrapers against the same Postgres (from the host):
```
export DB_BACKEND=postgres
export DATABASE_URL=postgresql://price:price@localhost:5432/pricemonitor
python main.py scrape --retailer posokanei --limit 500   # run several in parallel now
```

### Migrate existing SQLite data
```
DATABASE_URL=postgresql://price:price@localhost:5432/pricemonitor \
python migrate_to_pg.py prices.db
```

### Verify parallel writes work
```
DB_BACKEND=postgres DATABASE_URL=postgresql://price:price@localhost:5432/pricemonitor \
python concurrency_test.py
```

### Backend selection
`DB_BACKEND=sqlite` (default) or `postgres`. Postgres reads `DATABASE_URL`.
Everything (scrapers, web app, ingester) honors this — no code changes needed.
The Postgres backend uses a connection pool and `INSERT ... ON CONFLICT`, so
simultaneous writers resolve safely; search uses an indexed `unaccent(lower())`
for fast Greek-aware matching.

## Server-stored shopping lists

Lists now live in the database (not just localStorage), so they persist, can be
renamed, and can be reviewed over time. Items link to catalog products, so each
shows its current cheapest price and the change since you added it.

Endpoints:
  GET    /api/lists                     list all
  POST   /api/lists {name}              create
  GET    /api/lists/<id>                review (items + current prices + deltas)
  PATCH  /api/lists/<id> {name}         rename
  DELETE /api/lists/<id>                delete
  POST   /api/lists/<id>/items {...}    add item (name, product_id, category, qty, added_price)
  DELETE /api/list-items/<id>           remove item
  GET    /api/list-items/<id>/history   per-item cross-chain price history

In the planner web app, the "Lists" tab shows all lists; open one to review
items, current prices, change-since-added, and a per-item price trend. Adding a
mover from the Changes tab (or a product from Search) writes to a server list.

The offline phone file still works as before — it's a generated snapshot, so it
doesn't sync back to the server lists automatically (by design).

## Scheduled collection (cron in the worker container)

A `worker` service runs cron to collect prices on a schedule. The jobs are in
`crontab` (edit to taste); they write to the same Postgres as the web app.

```
docker compose up -d        # starts db + web + worker
docker compose logs -f worker   # watch scheduled runs
```

Default schedule (Europe/Athens time): posokanei full refresh at 03:00, then the
per-retailer scrapers staggered 04:00–06:00. Per-job logs are under
`/var/log/cron/` inside the worker container.

To change the schedule, edit `crontab` (the worker mounts the code, so a
`docker compose restart worker` picks it up). To run a job by hand:
```
docker compose exec worker python3 main.py scrape --retailer posokanei --limit 50
```

## Product metadata coverage (brand / category / image)

- **posokanei**: brand, category, image — all provided by the official API.
- **ab**, **lidl**: brand, category, image — from JSON-LD Product.
- **mymarket**: brand, category, image — from JSON-LD ItemList.
- **sklavenitis**: brand, category — from the GA4 dataLayer (no image in source).
- **bazaar**: image — from listing cards; category — derived from the category
  URL slug (OpenCart cards don't expose brand).

Fields absent in a retailer's source are simply left null; the cross-chain
`posokanei` record is the most complete and is the recommended primary source.
