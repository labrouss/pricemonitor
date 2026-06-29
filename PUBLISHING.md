# Publishing a public price snapshot

Your server keeps doing the scraping and stores the full price history in
Postgres. Once a night it exports a **latest-prices snapshot** (no history) and
pushes it to a GitHub Release, so anyone can fetch the data from one stable URL
without touching your server or database.

## Why publish from the server (not scrape in GitHub Actions)

Scraping from GitHub Actions runs into three hard problems: jobs time out at 6h
(the polite 3s-per-request scrapers take ~2h *each*), runners use shared Azure
datacenter IPs that retailer sites often block, and the runner filesystem is
wiped each run so there's nowhere to keep the growing history. Publishing a
snapshot from the server avoids all three — GitHub only ever hosts a file you
already produced.

## What gets published

Three formats of the *current* price per product per chain (duplicates folded to
canonical), plus a manifest:

- `prices-latest.sqlite` — queryable; open in any SQLite tool
- `prices-latest.json` — for web apps / scripts
- `prices-latest.csv` — opens in Excel / Sheets
- `manifest.json` — when it was generated, row/product counts, chains

## One-time setup

1. Create a GitHub repo (public, so the releases are fetchable by anyone).
2. Create a **fine-grained Personal Access Token** scoped to that repo with
   **Contents: read and write** permission. (Settings -> Developer settings ->
   Fine-grained tokens.)
3. Add the token and repo to the server's container environment. In
   `docker-compose.yml`, under the `worker` service `environment:` (and keep the
   real values in `.env`, never committed):

   ```yaml
   worker:
     environment:
       GITHUB_TOKEN: ${GITHUB_TOKEN}
       GITHUB_REPO: yourname/price-monitor
   ```

   and in `.env`:
   ```
   GITHUB_TOKEN=github_pat_xxx
   ```

That's it — the nightly cron job (11:30, after dedup) exports and publishes. If
`GITHUB_TOKEN` is unset it just exports locally and skips the push, so it's safe
to enable before you've configured the token.

## Run it manually to test

```bash
docker exec -ti -e DB_BACKEND=postgres price_monitor-worker-1 \
    python3 export_snapshot.py --out /app/snapshot

docker exec -ti -e DB_BACKEND=postgres \
    -e GITHUB_TOKEN=... -e GITHUB_REPO=you/price-monitor \
    price_monitor-worker-1 ./publish_snapshot.sh /app/snapshot
```

## How users fetch the data

The "latest" release always points at the newest snapshot, so these URLs are
stable:

```
https://github.com/<you>/price-monitor/releases/latest/download/prices-latest.json
https://github.com/<you>/price-monitor/releases/latest/download/prices-latest.csv
https://github.com/<you>/price-monitor/releases/latest/download/prices-latest.sqlite
```

Example — fetch and query with one line each:

```bash
# JSON
curl -sL .../releases/latest/download/prices-latest.json | jq '.[0]'

# CSV
curl -sLO .../releases/latest/download/prices-latest.csv

# SQLite
curl -sLO .../releases/latest/download/prices-latest.sqlite
sqlite3 prices-latest.sqlite "SELECT product, retailer, price FROM prices LIMIT 5;"
```

Dated snapshots (`data-YYYYMMDD` tags) are also kept for anyone who wants
history of the snapshots themselves.

## Syncing the snapshot INTO a local app

A second machine (your laptop's PySide app, or a webapp instance that does NOT
run the scrapers) can pull the published snapshot into its own database. Each
sync downloads `prices-latest.json` from the latest release and replays it
through the normal ingest path, so local price history accrues with every sync.

```bash
# into local SQLite (PySide app default)
python3 main.py sync --repo labrouss/pricemonitor --db prices.db

# into local Postgres (a webapp instance)
DB_BACKEND=postgres python3 main.py sync --repo labrouss/pricemonitor
```

Or from the webapp over HTTP:
```
POST /api/sync           {"repo": "labrouss/pricemonitor"}
```

For a private repo, set `GITHUB_TOKEN` (a fine-grained PAT with Contents:read)
so the download authenticates.

To keep a laptop in sync automatically, add a local cron / Task Scheduler entry
running the `main.py sync` line nightly after the server publishes (~12:00).

**Important — don't sync on the server that produces the snapshot.** That server
already has this data from its own scraping; pulling the snapshot back in would
duplicate it. Sync is for *other* machines that consume the published data, not
the producer.

## A note on terms & attribution

Publish only data you're comfortable redistributing. The posokanei figures come
from an official government portal; per-retailer scraped prices are facts, but
check each source's terms before redistributing in bulk, and attribute
posokanei.gov.gr as the cross-chain source. Keep the snapshot to prices/product
identity — don't republish copyrighted descriptions or images.
