"""
PostgreSQL storage backend — drop-in replacement for the SQLite Store.

Same public interface (upsert_product, record_price, search_products, …) and the
same normalized schema (products → offers → price_history), but backed by
Postgres so MANY scrapers can write CONCURRENTLY without "database is locked".

Key differences from the SQLite version, all internal:
  - A connection pool (psycopg_pool) hands each parallel worker its own
    connection; Postgres handles concurrent writers natively.
  - Identity upserts use INSERT ... ON CONFLICT, which is atomic — two scrapers
    inserting the same product at the same instant resolve to one row safely.
  - Greek/accent-insensitive search uses Postgres `unaccent(lower(...))`, which
    is indexable (a real index, unlike SQLite's per-row custom function).

Configure via DATABASE_URL, e.g.
  postgresql://price:price@localhost:5432/pricemonitor
"""

import os
import re
import time
import random
import unicodedata
from datetime import datetime, timezone, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent() is only STABLE by default, so Postgres won't allow it directly in
-- an index expression. Wrap it in an IMMUTABLE function we can index on.
CREATE OR REPLACE FUNCTION immutable_unaccent(text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT unaccent('unaccent', $1) $$;

CREATE TABLE IF NOT EXISTS products (
    id           BIGSERIAL PRIMARY KEY,
    product_key  TEXT    NOT NULL UNIQUE,
    name         TEXT    NOT NULL,
    brand        TEXT,
    category     TEXT,
    image_url    TEXT,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS offers (
    id           BIGSERIAL PRIMARY KEY,
    product_id   BIGINT NOT NULL REFERENCES products(id),
    retailer     TEXT    NOT NULL,
    retailer_sku TEXT,
    url          TEXT,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ,
    UNIQUE(product_id, retailer)
);

CREATE TABLE IF NOT EXISTS price_history (
    id           BIGSERIAL PRIMARY KEY,
    offer_id     BIGINT NOT NULL REFERENCES offers(id),
    observed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    price        DOUBLE PRECISION,
    list_price   DOUBLE PRECISION,
    currency     TEXT DEFAULT 'EUR',
    in_stock     INTEGER,
    unit_price   DOUBLE PRECISION,
    unit         TEXT,
    raw          TEXT
);

CREATE INDEX IF NOT EXISTS idx_offers_product ON offers(product_id);
CREATE INDEX IF NOT EXISTS idx_price_offer_time ON price_history(offer_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_price_offer ON price_history(offer_id);
-- Indexable accent-insensitive name search via the IMMUTABLE wrapper:
CREATE INDEX IF NOT EXISTS idx_products_name_unaccent
    ON products (immutable_unaccent(lower(name)) text_pattern_ops);

CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL PRIMARY KEY,
    subject     TEXT NOT NULL UNIQUE,
    email       TEXT,
    name        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS lists (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users(id),
    name        TEXT    NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ
);
-- Existing databases predate user_id: add it BEFORE the index that needs it.
ALTER TABLE lists ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_lists_user ON lists(user_id);

CREATE TABLE IF NOT EXISTS list_items (
    id          BIGSERIAL PRIMARY KEY,
    list_id     BIGINT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    product_id  BIGINT REFERENCES products(id),
    name        TEXT    NOT NULL,
    category    TEXT,
    qty         INTEGER DEFAULT 1,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_price DOUBLE PRECISION,
    planned_at  TIMESTAMPTZ,
    planned_retailer TEXT,
    planned_price    DOUBLE PRECISION
);
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS planned_at TIMESTAMPTZ;
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS planned_retailer TEXT;
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS planned_price DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS idx_list_items_list ON list_items(list_id);

-- De-duplication: canonical pointer + fuzzy review queue (mirror of SQLite).
ALTER TABLE products ADD COLUMN IF NOT EXISTS canonical_id BIGINT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS size_value DOUBLE PRECISION;
ALTER TABLE products ADD COLUMN IF NOT EXISTS size_unit TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS size_dim TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS embedding BYTEA;
CREATE TABLE IF NOT EXISTS merge_candidates (
    id          BIGSERIAL PRIMARY KEY,
    product_a   BIGINT NOT NULL REFERENCES products(id),
    product_b   BIGINT NOT NULL REFERENCES products(id),
    score       DOUBLE PRECISION,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    UNIQUE(product_a, product_b)
);
CREATE INDEX IF NOT EXISTS idx_merge_status ON merge_candidates(status);
"""

# Set True after the schema has been initialised once in this process, so we
# don't re-run it on every Store() / request.
_SCHEMA_DONE = False


def _norm(s):
    """Python-side normalize, used only for building product_key (not for search)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower().strip())


def product_key(name, brand=None, shared_sku=None):
    if shared_sku:
        return f"sku:{shared_sku}"
    return f"nb:{_norm(name)}|{_norm(brand)}"


class Store:
    """Postgres-backed, same interface as the SQLite Store."""

    def __init__(self, dsn=None, min_size=1, max_size=10):
        self.dsn = dsn or os.environ.get(
            "DATABASE_URL", "postgresql://price:price@localhost:5432/pricemonitor")
        # A pool so parallel scrapers each check out their own connection.
        self.pool = ConnectionPool(self.dsn, min_size=min_size, max_size=max_size,
                                   kwargs={"row_factory": dict_row}, open=True)
        self._init_schema()

    def _init_schema(self):
        # Run the schema ONCE per process (not per request). A module-level flag
        # guards it, and a Postgres advisory lock guards against multiple
        # processes (web + worker) racing on first start. The schema is fully
        # idempotent (CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT EXISTS,
        # CREATE OR REPLACE FUNCTION) so re-running is harmless.
        global _SCHEMA_DONE
        if _SCHEMA_DONE:
            return
        import logging
        with self.pool.connection() as conn:
            try:
                # Serialize concurrent first-run attempts across processes.
                conn.execute("SELECT pg_advisory_lock(727274)")
                conn.execute(SCHEMA)
                conn.commit()
                _SCHEMA_DONE = True
            except Exception as e:
                conn.rollback()
                logging.getLogger("storage_pg").error("schema init failed: %s", e)
                raise
            finally:
                try:
                    conn.execute("SELECT pg_advisory_unlock(727274)")
                    conn.commit()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Identity upserts — atomic via ON CONFLICT, safe under concurrency.
    # ------------------------------------------------------------------
    def upsert_product(self, retailer, name, url, sku=None, brand=None,
                       category=None, image_url=None, shared_key=None,
                       block_duplicates=None):
        if block_duplicates is None:
            block_duplicates = getattr(self, "block_on_ingest", False)
        """
        Record a (product, retailer) offer; return the OFFER id.

        Concurrency notes: under many parallel scrapers, doing the products and
        offers upserts in ONE transaction let the two tables' locks interleave
        into a deadlock (offers has an FK to products, so Postgres takes a
        ShareLock on the parent product row while inserting a child offer; if
        another worker holds that product row mid-update and wants the offers
        table, you get a cycle). We avoid it two ways:
          1) Commit the product upsert in its own short transaction, so the
             product-row lock is released BEFORE we touch offers.
          2) Retry the whole thing on the rare remaining deadlock — deadlocks
             are transient, so a retry almost always wins.
        """
        pkey = product_key(name, brand, shared_sku=shared_key)

        def _do():
            # --- tx 1: resolve the product id, then COMMIT (short lock) ---
            with self.pool.connection() as conn:
                # Read-first: under repeated scraping the product almost always
                # already exists, so a cheap SELECT avoids the ON CONFLICT DO
                # UPDATE path that causes 'tuple concurrently updated' when many
                # writers touch the same row at once.
                row = conn.execute(
                    "SELECT id FROM products WHERE product_key = %s", (pkey,)
                ).fetchone()
                if row:
                    product_id = row["id"]
                    # Targeted UPDATE by id (only refresh fields that add info).
                    conn.execute(
                        """UPDATE products SET
                               name      = %s,
                               brand     = COALESCE(%s, brand),
                               category  = COALESCE(%s, category),
                               image_url = COALESCE(%s, image_url),
                               last_seen = now()
                           WHERE id = %s""",
                        (name, brand, category, image_url, product_id))
                else:
                    # New product: INSERT, but tolerate a race where another
                    # writer inserted the same key first (ON CONFLICT → re-read).
                    res = conn.execute(
                        """INSERT INTO products (product_key, name, brand, category, image_url, last_seen)
                           VALUES (%s,%s,%s,%s,%s, now())
                           ON CONFLICT (product_key) DO NOTHING
                           RETURNING id""",
                        (pkey, name, brand, category, image_url)).fetchone()
                    if res:
                        product_id = res["id"]
                    else:
                        product_id = conn.execute(
                            "SELECT id FROM products WHERE product_key = %s",
                            (pkey,)).fetchone()["id"]
                conn.commit()

            # --- tx 2: offer upsert (parent row no longer locked by us) ---
            with self.pool.connection() as conn:
                orow = conn.execute(
                    "SELECT id FROM offers WHERE product_id = %s AND retailer = %s",
                    (product_id, retailer)).fetchone()
                if orow:
                    offer_id = orow["id"]
                    conn.execute(
                        """UPDATE offers SET
                               retailer_sku = COALESCE(%s, retailer_sku),
                               url          = COALESCE(%s, url),
                               last_seen    = now()
                           WHERE id = %s""",
                        (sku, url, offer_id))
                else:
                    res = conn.execute(
                        """INSERT INTO offers (product_id, retailer, retailer_sku, url, last_seen)
                           VALUES (%s,%s,%s,%s, now())
                           ON CONFLICT (product_id, retailer) DO NOTHING
                           RETURNING id""",
                        (product_id, retailer, sku, url)).fetchone()
                    if res:
                        offer_id = res["id"]
                    else:
                        offer_id = conn.execute(
                            "SELECT id FROM offers WHERE product_id = %s AND retailer = %s",
                            (product_id, retailer)).fetchone()["id"]
                conn.commit()
            return offer_id, product_id

        offer_id, product_id = self._retry(_do)
        # Post-process outside the retry loop: enrich size, optionally block dupes.
        try:
            self._enrich_product_size(product_id, name)
        except Exception:
            pass
        if block_duplicates:
            try:
                self.block_duplicate_at_ingest(product_id)
            except Exception:
                pass
        return offer_id

    def _enrich_product_size(self, product_id, name):
        try:
            import matcher
            info = matcher.enrich_size(name)
        except Exception:
            info = {}
        if not info:
            return
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE products SET size_value=%s, size_unit=%s, size_dim=%s
                   WHERE id=%s AND size_value IS NULL""",
                (info.get("size_value"), info.get("size_unit"),
                 info.get("size_dim"), product_id))
            conn.commit()

    def block_duplicate_at_ingest(self, product_id):
        import matcher
        rows = self._q(
            "SELECT id, name, brand, category, canonical_id FROM products WHERE id=%s",
            (product_id,))
        if not rows or rows[0]["canonical_id"] is not None:
            return None
        me = rows[0]
        cands = self._q(
            """SELECT id, name, brand FROM products
               WHERE canonical_id IS NULL AND id<>%s
                 AND category IS NOT DISTINCT FROM %s LIMIT 400""",
            (product_id, me["category"]))
        if not cands:
            return None
        best_id, best_score = None, 0.0
        for c in cands:
            sc = matcher.hybrid_score(me["name"], me["brand"], c["name"], c["brand"])
            if sc > best_score:
                best_id, best_score = c["id"], sc
        if best_id is not None and best_score >= matcher.AUTO_BLOCK:
            with self.pool.connection() as conn:
                conn.execute("UPDATE products SET canonical_id=%s WHERE id=%s",
                             (best_id, product_id))
                conn.commit()
            return best_id
        return None

    def _retry(self, fn, attempts=6):
        """
        Run fn(), retrying on transient concurrency failures with small backoff.

        Under many parallel writers two classes of transient error occur, both
        safe to retry because the conflicting transaction has moved on by the
        time we try again:
          - DeadlockDetected: lock cycle, Postgres killed one victim.
          - 'tuple concurrently updated': two sessions ran ON CONFLICT DO UPDATE
            on the SAME row at the same instant (common when scrapers share
            product keys). Raised as a generic error, matched by message.
        """
        last_err = None
        for attempt in range(attempts):
            try:
                return fn()
            except psycopg.errors.DeadlockDetected as e:
                last_err = e
            except psycopg.Error as e:
                msg = str(e).lower()
                if "concurrently updated" in msg or "could not serialize" in msg:
                    last_err = e
                else:
                    raise
            # jittered backoff so retriers don't re-collide in lock-step
            time.sleep(0.04 * (attempt + 1) + random.random() * 0.03)
        raise last_err

    def record_price(self, product_id, price, list_price=None, in_stock=None,
                     unit_price=None, unit=None, currency="EUR", raw=None):
        """Append a price observation. `product_id` is the OFFER id."""
        # in_stock column is INTEGER; tolerate a bool from any caller.
        if isinstance(in_stock, bool):
            in_stock = int(in_stock)
        def _do():
            with self.pool.connection() as conn:
                conn.execute(
                    """INSERT INTO price_history
                       (offer_id, observed_at, price, list_price, currency,
                        in_stock, unit_price, unit, raw)
                       VALUES (%s, now(), %s,%s,%s,%s,%s,%s,%s)""",
                    (product_id, price, list_price, currency, in_stock,
                     unit_price, unit, raw))
                conn.commit()
        self._retry(_do)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def _q(self, sql, params=()):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def latest_prices(self, retailer=None):
        sql = """
        SELECT p.name, p.brand, p.category, p.image_url,
               o.retailer, o.retailer_sku AS sku, o.url,
               h.price, h.list_price, h.observed_at, h.in_stock
        FROM products p
        JOIN offers o ON o.product_id = p.id
        JOIN price_history h ON h.offer_id = o.id
        JOIN (SELECT offer_id, MAX(observed_at) mx FROM price_history
              GROUP BY offer_id) last
          ON last.offer_id = h.offer_id AND last.mx = h.observed_at
        """
        params = ()
        if retailer:
            sql += " WHERE o.retailer = %s"
            params = (retailer,)
        return [dict(r) for r in self._q(sql, params)]

    def _canonical_group(self, product_id):
        """All product ids in the canonical cluster of product_id (master + dups)."""
        rows = self._q("SELECT canonical_id FROM products WHERE id=%s", (product_id,))
        if not rows:
            return [product_id]
        master = rows[0]["canonical_id"] if rows[0]["canonical_id"] is not None else product_id
        ids = [r["id"] for r in self._q(
            "SELECT id FROM products WHERE id=%s OR canonical_id=%s", (master, master))]
        return ids or [product_id]

    def search_products(self, query="", retailer=None, on_offer=False,
                        in_stock_only=False, limit=500, category=None):
        # Step 1: matching CANONICAL products only (duplicates hidden).
        pq = "SELECT id FROM products WHERE canonical_id IS NULL"
        pparams = []
        if query:
            pq += " AND immutable_unaccent(lower(name)) LIKE immutable_unaccent(lower(%s))"
            pparams.append(f"%{query}%")
        if category:
            pq += " AND category = %s"
            pparams.append(category)
        pq += " ORDER BY name LIMIT %s"
        pparams.append(limit)
        masters = [r["id"] for r in self._q(pq, tuple(pparams))]
        if not masters:
            return []

        group_of = {}
        all_ids = []
        for m in masters:
            for pid in self._canonical_group(m):
                group_of[pid] = m
                all_ids.append(pid)

        sql = """
        SELECT p.id AS raw_product_id, p.name, p.brand, p.category, p.image_url,
               o.id AS offer_id, o.retailer, o.retailer_sku AS sku, o.url,
               h.price, h.list_price, h.unit_price, h.unit,
               h.in_stock, h.observed_at
        FROM products p
        JOIN offers o ON o.product_id = p.id
        JOIN price_history h ON h.offer_id = o.id
        JOIN (
            SELECT ph.offer_id, MAX(ph.observed_at) mx
            FROM price_history ph
            JOIN offers o2 ON o2.id = ph.offer_id
            WHERE o2.product_id = ANY(%s)
            GROUP BY ph.offer_id
        ) last ON last.offer_id = h.offer_id AND last.mx = h.observed_at
        WHERE p.id = ANY(%s)
        """
        params = [all_ids, all_ids]
        if retailer:
            sql += " AND o.retailer = %s"
            params.append(retailer)
        if on_offer:
            sql += " AND h.list_price IS NOT NULL AND h.list_price > h.price"
        if in_stock_only:
            sql += " AND h.in_stock = 1"
        sql += " ORDER BY p.name, o.retailer"
        rows = [dict(r) for r in self._q(sql, tuple(params))]
        master_info = {m: self.product_info(m) for m in masters}
        for r in rows:
            m = group_of.get(r["raw_product_id"], r["raw_product_id"])
            r["product_id"] = m
            mi = master_info.get(m)
            if mi:
                r["name"] = mi["name"]; r["brand"] = mi["brand"]
                r["category"] = mi["category"]; r["image_url"] = mi["image_url"]
            r.pop("raw_product_id", None)
        return rows

    def price_history(self, offer_id):
        return [dict(r) for r in self._q(
            """SELECT observed_at, price, list_price, in_stock
               FROM price_history WHERE offer_id = %s ORDER BY observed_at ASC""",
            (offer_id,))]

    def product_offers(self, product_id):
        group = self._canonical_group(product_id)
        sql = """
        SELECT o.id AS offer_id, o.retailer, h.price, h.list_price, h.observed_at
        FROM offers o
        JOIN price_history h ON h.offer_id = o.id
        JOIN (SELECT offer_id, MAX(observed_at) mx FROM price_history
              GROUP BY offer_id) last
          ON last.offer_id = h.offer_id AND last.mx = h.observed_at
        WHERE o.product_id = ANY(%s)
        ORDER BY h.price ASC
        """
        return [dict(r) for r in self._q(sql, (group,))]

    def product_info(self, product_id):
        rows = self._q(
            """SELECT id, product_key, name, brand, category, image_url,
                      first_seen, last_seen FROM products WHERE id = %s""",
            (product_id,))
        return dict(rows[0]) if rows else None

    def categories(self, limit=200):
        return [dict(r) for r in self._q(
            """SELECT category, COUNT(*) AS n FROM products
               WHERE category IS NOT NULL AND TRIM(category) <> ''
               GROUP BY category ORDER BY n DESC LIMIT %s""", (limit,))]

    def retailers(self):
        return [dict(r) for r in self._q(
            """SELECT retailer, COUNT(*) AS offers FROM offers
               GROUP BY retailer ORDER BY offers DESC""")]

    def stats(self):
        one = lambda sql: self._q(sql)[0]["n"]
        return {
            "products": one("SELECT COUNT(*) n FROM products WHERE canonical_id IS NULL"),
            "offers": one("SELECT COUNT(*) n FROM offers"),
            "observations": one("SELECT COUNT(*) n FROM price_history"),
            "retailers": one("SELECT COUNT(DISTINCT retailer) n FROM offers"),
            "multi_chain": one(
                """SELECT COUNT(*) n FROM (
                       SELECT COALESCE(p.canonical_id, p.id) AS master
                       FROM offers o JOIN products p ON p.id = o.product_id
                       GROUP BY COALESCE(p.canonical_id, p.id)
                       HAVING COUNT(DISTINCT o.retailer) > 1) t"""),
        }

    def product_price_history(self, product_id):
        rows = self._q(
            """SELECT o.retailer, h.observed_at, h.price
               FROM offers o JOIN price_history h ON h.offer_id = o.id
               WHERE o.product_id = %s AND h.price IS NOT NULL
               ORDER BY o.retailer, h.observed_at ASC""",
            (product_id,))
        out = {}
        for r in rows:
            out.setdefault(r["retailer"], []).append(
                {"observed_at": r["observed_at"].isoformat(), "price": r["price"]})
        return out

    def biggest_changes(self, days=30, limit=40, product_ids=None):
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        params = [cutoff]
        filt = ""
        if product_ids:
            filt = "AND o.product_id = ANY(%s)"
            params.append(list(product_ids))
        sql = f"""
        WITH latest AS (
            SELECT h.offer_id, h.price, h.observed_at
            FROM price_history h
            JOIN (SELECT offer_id, MAX(observed_at) mx FROM price_history
                  GROUP BY offer_id) m
              ON m.offer_id = h.offer_id AND m.mx = h.observed_at
        ),
        baseline AS (
            SELECT h.offer_id, h.price
            FROM price_history h
            JOIN (SELECT offer_id, MAX(observed_at) mx FROM price_history
                  WHERE observed_at <= %s GROUP BY offer_id) m
              ON m.offer_id = h.offer_id AND m.mx = h.observed_at
        )
        SELECT o.product_id, p.name, p.brand, p.category, o.retailer,
               baseline.price AS old_price, latest.price AS new_price,
               latest.observed_at AS observed_at
        FROM latest
        JOIN baseline ON baseline.offer_id = latest.offer_id
        JOIN offers o ON o.id = latest.offer_id
        JOIN products p ON p.id = o.product_id
        WHERE baseline.price IS NOT NULL AND latest.price IS NOT NULL
          AND baseline.price > 0 AND baseline.price <> latest.price
          {filt}
        """
        rows = self._q(sql, tuple(params))
        best = {}
        for r in rows:
            old, new = r["old_price"], r["new_price"]
            pct = (new - old) / old * 100.0
            cur = best.get(r["product_id"])
            if cur is None or abs(pct) > abs(cur["pct"]):
                best[r["product_id"]] = {
                    "product_id": r["product_id"], "name": r["name"],
                    "brand": r["brand"], "category": r["category"],
                    "retailer": r["retailer"], "old": round(old, 2),
                    "new": round(new, 2), "change": round(new - old, 2),
                    "pct": round(pct, 1),
                    "observed_at": r["observed_at"].isoformat(),
                }
        movers = sorted(best.values(), key=lambda x: abs(x["pct"]), reverse=True)
        return movers[:limit]

    # ------------------------------------------------------------------
    # Server-stored shopping lists
    # ------------------------------------------------------------------
    def upsert_user(self, subject, email=None, name=None):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT id FROM users WHERE subject=%s", (subject,)).fetchone()
            if row:
                conn.execute("UPDATE users SET email=%s, name=%s, last_login=now() WHERE id=%s",
                             (email, name, row["id"]))
                conn.commit()
                return row["id"]
            row = conn.execute(
                "INSERT INTO users (subject, email, name, last_login) VALUES (%s,%s,%s, now()) RETURNING id",
                (subject, email, name)).fetchone()
            conn.commit()
            return row["id"]

    def owns_list(self, list_id, user_id):
        rows = self._q("SELECT user_id FROM lists WHERE id=%s", (list_id,))
        return bool(rows) and rows[0]["user_id"] == user_id

    def owns_item(self, item_id, user_id):
        rows = self._q("""SELECT l.user_id FROM list_items li
                          JOIN lists l ON l.id = li.list_id WHERE li.id=%s""", (item_id,))
        return bool(rows) and rows[0]["user_id"] == user_id

    def create_list(self, name, user_id=None):
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO lists (name, user_id, updated_at) VALUES (%s, %s, now()) RETURNING id",
                (name, user_id)).fetchone()
            conn.commit()
            return row["id"]

    def rename_list(self, list_id, name):
        with self.pool.connection() as conn:
            conn.execute("UPDATE lists SET name=%s, updated_at=now() WHERE id=%s",
                         (name, list_id))
            conn.commit()

    def delete_list(self, list_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM lists WHERE id=%s", (list_id,))
            conn.commit()

    def all_lists(self, user_id=None):
        return [dict(r) for r in self._q("""
            SELECT l.id, l.name, l.created_at, l.updated_at,
                   COUNT(li.id) AS item_count
            FROM lists l LEFT JOIN list_items li ON li.list_id = l.id
            WHERE l.user_id IS NOT DISTINCT FROM %s
            GROUP BY l.id ORDER BY l.updated_at DESC NULLS LAST""", (user_id,))]

    def add_list_item(self, list_id, name, product_id=None, category=None,
                      qty=1, added_price=None):
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO list_items
                   (list_id, product_id, name, category, qty, added_price)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (list_id, product_id, name, category, qty, added_price)).fetchone()
            conn.execute("UPDATE lists SET updated_at=now() WHERE id=%s", (list_id,))
            conn.commit()
            return row["id"]

    def remove_list_item(self, item_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM list_items WHERE id=%s", (item_id,))
            conn.commit()

    def list_review(self, list_id):
        meta = self._q(
            "SELECT id, name, created_at, updated_at FROM lists WHERE id=%s",
            (list_id,))
        if not meta:
            return None
        meta = meta[0]
        items = self._q(
            """SELECT id, product_id, name, category, qty, added_at, added_price
               FROM list_items WHERE list_id=%s ORDER BY added_at""", (list_id,))
        out_items = []
        for it in items:
            entry = dict(it)
            entry["added_at"] = it["added_at"].isoformat() if it["added_at"] else None
            entry["current_min"] = None
            entry["current_prices"] = {}
            entry["since_added"] = None
            if it["product_id"] is not None:
                offers = self.product_offers(it["product_id"])
                prices = {o["retailer"]: o["price"] for o in offers if o["price"] is not None}
                entry["current_prices"] = prices
                if prices:
                    cmin = min(prices.values())
                    entry["current_min"] = round(cmin, 2)
                    if it["added_price"]:
                        entry["since_added"] = round(cmin - it["added_price"], 2)
            out_items.append(entry)
        return {"id": meta["id"], "name": meta["name"],
                "created_at": meta["created_at"].isoformat() if meta["created_at"] else None,
                "updated_at": meta["updated_at"].isoformat() if meta["updated_at"] else None,
                "items": out_items}

    def plan_list(self, list_id):
        items = self._q("SELECT id, product_id FROM list_items WHERE list_id=%s",
                        (list_id,))
        with self.pool.connection() as conn:
            for it in items:
                if it["product_id"] is None:
                    continue
                offers = self.product_offers(it["product_id"])
                priced = [(o["retailer"], o["price"]) for o in offers if o["price"] is not None]
                if not priced:
                    continue
                priced.sort(key=lambda x: x[1])
                ret, pr = priced[0]
                conn.execute(
                    """UPDATE list_items
                       SET planned_at=now(), planned_retailer=%s, planned_price=%s
                       WHERE id=%s""",
                    (ret, round(pr, 2), it["id"]))
            conn.commit()
        return self.revaluate_list(list_id)

    def revaluate_list(self, list_id):
        meta = self._q("SELECT id, name FROM lists WHERE id=%s", (list_id,))
        if not meta:
            return None
        meta = meta[0]
        items = self._q(
            """SELECT id, product_id, name, category, qty,
                      planned_at, planned_retailer, planned_price
               FROM list_items WHERE list_id=%s ORDER BY name""", (list_id,))
        out = []
        n_store_changed = 0
        total_planned = 0.0
        total_now = 0.0
        for it in items:
            entry = {"id": it["id"], "name": it["name"], "qty": it["qty"] or 1,
                     "planned_retailer": it["planned_retailer"],
                     "planned_price": it["planned_price"],
                     "current_retailer": None, "current_price": None,
                     "store_changed": False, "price_delta": None,
                     "planned": it["planned_at"] is not None}
            if it["product_id"] is not None:
                offers = self.product_offers(it["product_id"])
                priced = [(o["retailer"], o["price"]) for o in offers if o["price"] is not None]
                if priced:
                    priced.sort(key=lambda x: x[1])
                    cret, cpr = priced[0]
                    entry["current_retailer"] = cret
                    entry["current_price"] = round(cpr, 2)
                    if it["planned_price"] is not None:
                        entry["price_delta"] = round(cpr - it["planned_price"], 2)
                        total_planned += it["planned_price"] * (it["qty"] or 1)
                        total_now += cpr * (it["qty"] or 1)
                        if it["planned_retailer"] and cret != it["planned_retailer"]:
                            entry["store_changed"] = True
                            n_store_changed += 1
            out.append(entry)
        return {
            "id": meta["id"], "name": meta["name"], "items": out,
            "summary": {
                "stores_changed": n_store_changed,
                "planned_total": round(total_planned, 2) if total_planned else None,
                "current_total": round(total_now, 2) if total_now else None,
                "delta": round(total_now - total_planned, 2) if total_planned else None,
            },
        }

    def list_item_history(self, item_id):
        row = self._q("SELECT product_id FROM list_items WHERE id=%s", (item_id,))
        if not row or row[0]["product_id"] is None:
            return {}
        return self.product_price_history(row[0]["product_id"])

    def sync_list(self, list_id, items, deleted_ids=None):
        """Reconcile a list from an offline client. See SQLite docstring."""
        if not self._q("SELECT 1 FROM lists WHERE id=%s", (list_id,)):
            return None
        existing = {r["id"] for r in self._q(
            "SELECT id FROM list_items WHERE list_id=%s", (list_id,))}
        with self.pool.connection() as conn:
            for d in (deleted_ids or []):
                if isinstance(d, int) and d in existing:
                    conn.execute("DELETE FROM list_items WHERE id=%s AND list_id=%s",
                                 (d, list_id))
            for it in items:
                iid = it.get("server_id")
                if isinstance(iid, int) and iid in existing:
                    conn.execute(
                        "UPDATE list_items SET name=%s, category=%s, qty=%s WHERE id=%s AND list_id=%s",
                        (it.get("name") or "(item)", it.get("cat") or it.get("category"),
                         int(it.get("qty", 1)), iid, list_id))
                else:
                    conn.execute(
                        """INSERT INTO list_items
                           (list_id, product_id, name, category, qty, added_price)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (list_id, it.get("productId"), it.get("name") or "(item)",
                         it.get("cat") or it.get("category"),
                         int(it.get("qty", 1)), it.get("price")))
            conn.execute("UPDATE lists SET updated_at=now() WHERE id=%s", (list_id,))
            conn.commit()
        return self.list_review(list_id)

    # ------------------------------------------------------------------
    # De-duplication (mirror of SQLite)
    # ------------------------------------------------------------------
    def scan_duplicates(self, auto_merge=True, suggest=True):
        import dedup
        prods = [dict(r) for r in self._q(
            """SELECT p.id, p.name, p.brand, p.product_key,
                      EXISTS(SELECT 1 FROM offers o WHERE o.product_id=p.id
                             AND o.retailer='posokanei') AS is_poso
               FROM products p WHERE p.canonical_id IS NULL""")]
        merged = 0
        buckets = {}
        for p in prods:
            buckets.setdefault(dedup.match_key(p["name"], p["brand"]), []).append(p)
        if auto_merge:
            with self.pool.connection() as conn:
                for group in buckets.values():
                    if len(group) < 2:
                        continue
                    group.sort(key=lambda x: (0 if x["is_poso"] else 1, x["id"]))
                    master = group[0]
                    for dup in group[1:]:
                        conn.execute("UPDATE products SET canonical_id=%s WHERE id=%s",
                                     (master["id"], dup["id"]))
                        merged += 1
                conn.commit()

        suggested = 0
        if suggest:
            import matcher
            remaining = [dict(r) for r in self._q(
                "SELECT id, name, brand FROM products WHERE canonical_id IS NULL")]
            pairs = matcher.find_candidate_pairs(remaining)
            with self.pool.connection() as conn:
                for lo_id, hi_id, score in pairs:
                    if score >= 1.0:
                        continue
                    lo, hi = sorted((lo_id, hi_id))
                    conn.execute(
                        """INSERT INTO merge_candidates
                           (product_a, product_b, score, status)
                           VALUES (%s,%s,%s,'pending')
                           ON CONFLICT (product_a, product_b) DO UPDATE
                             SET score = EXCLUDED.score
                             WHERE merge_candidates.status = 'pending'""",
                        (lo, hi, round(score, 3)))
                    suggested += 1
                conn.commit()
        return {"auto_merged": merged, "candidates_added": suggested}

    def merge_candidates(self, limit=200):
        return [dict(r) for r in self._q("""
            SELECT mc.id, mc.score,
                   a.id AS a_id, a.name AS a_name, a.brand AS a_brand,
                   b.id AS b_id, b.name AS b_name, b.brand AS b_brand
            FROM merge_candidates mc
            JOIN products a ON a.id = mc.product_a
            JOIN products b ON b.id = mc.product_b
            WHERE mc.status='pending'
            ORDER BY mc.score DESC LIMIT %s""", (limit,))]

    def resolve_candidate(self, candidate_id, approve, into=None):
        rows = self._q("SELECT product_a, product_b FROM merge_candidates WHERE id=%s",
                       (candidate_id,))
        if not rows:
            return None
        a, b = rows[0]["product_a"], rows[0]["product_b"]
        with self.pool.connection() as conn:
            if approve:
                master = into if into in (a, b) else a
                dup = b if master == a else a
                conn.execute("UPDATE products SET canonical_id=%s WHERE id=%s", (master, dup))
                conn.execute(
                    "UPDATE merge_candidates SET status='approved', decided_at=now() WHERE id=%s",
                    (candidate_id,))
            else:
                conn.execute(
                    "UPDATE merge_candidates SET status='rejected', decided_at=now() WHERE id=%s",
                    (candidate_id,))
            conn.commit()
        return {"id": candidate_id, "approved": bool(approve)}

    def prune_stale_candidates(self):
        import dedup
        rows = self._q("""
            SELECT mc.id, a.name a_name, a.brand a_brand,
                   b.name b_name, b.brand b_brand
            FROM merge_candidates mc
            JOIN products a ON a.id = mc.product_a
            JOIN products b ON b.id = mc.product_b
            WHERE mc.status='pending'""")
        removed = 0
        with self.pool.connection() as conn:
            for r in rows:
                score = dedup.fuzzy_score(r["a_name"], r["a_brand"],
                                          r["b_name"], r["b_brand"])
                if score < dedup.FUZZY_SUGGEST:
                    conn.execute("DELETE FROM merge_candidates WHERE id=%s", (r["id"],))
                    removed += 1
                else:
                    conn.execute("UPDATE merge_candidates SET score=%s WHERE id=%s",
                                 (score, r["id"]))
            conn.commit()
        return {"removed": removed}

    def export_candidates(self, status="pending"):
        where = "WHERE mc.status = %s" if status else ""
        params = (status,) if status else ()
        rows = self._q(f"""
            SELECT mc.id AS candidate_id, mc.score, mc.status,
                   a.id AS a_id, a.name AS a_name, a.brand AS a_brand,
                   a.category AS a_category,
                   b.id AS b_id, b.name AS b_name, b.brand AS b_brand,
                   b.category AS b_category
            FROM merge_candidates mc
            JOIN products a ON a.id = mc.product_a
            JOIN products b ON b.id = mc.product_b
            {where}
            ORDER BY mc.score DESC
        """, params)
        out = []
        for r in rows:
            d = dict(r)
            d["a_retailers"] = ",".join(self._retailers_for(d["a_id"]))
            d["b_retailers"] = ",".join(self._retailers_for(d["b_id"]))
            d["decision"] = ""
            out.append(d)
        return out

    def _retailers_for(self, product_id):
        return [r["retailer"] for r in self._q(
            "SELECT DISTINCT retailer FROM offers WHERE product_id=%s ORDER BY retailer",
            (product_id,))]

    def import_candidate_decisions(self, decisions):
        merged = rejected = skipped = missing = 0
        with self.pool.connection() as conn:
            for d in decisions:
                cid = d.get("candidate_id")
                act = (d.get("decision") or "").strip().lower()
                if not cid or act in ("", "skip"):
                    skipped += 1
                    continue
                row = conn.execute(
                    "SELECT product_a, product_b FROM merge_candidates WHERE id=%s",
                    (cid,)).fetchone()
                if not row:
                    missing += 1
                    continue
                a, b = row["product_a"], row["product_b"]
                if act in ("merge", "approve", "yes", "y", "1", "true"):
                    master, dup = (a, b) if a < b else (b, a)
                    mc = conn.execute("SELECT canonical_id FROM products WHERE id=%s",
                                      (master,)).fetchone()
                    if mc and mc["canonical_id"] is not None:
                        master = mc["canonical_id"]
                    conn.execute("UPDATE products SET canonical_id=%s WHERE id=%s",
                                 (master, dup))
                    conn.execute("UPDATE merge_candidates SET status='approved', "
                                 "decided_at=now() WHERE id=%s", (cid,))
                    merged += 1
                elif act in ("reject", "no", "n", "0", "false", "notsame", "not_same"):
                    conn.execute("UPDATE merge_candidates SET status='rejected', "
                                 "decided_at=now() WHERE id=%s", (cid,))
                    rejected += 1
                else:
                    skipped += 1
            conn.commit()
        return {"merged": merged, "rejected": rejected,
                "skipped": skipped, "missing": missing}

    def purge_retailer(self, retailer):
        """Delete a retailer's offers and their price history (products kept)."""
        with self.pool.connection() as conn:
            ph = conn.execute(
                """DELETE FROM price_history
                   WHERE offer_id IN (SELECT id FROM offers WHERE retailer=%s)""",
                (retailer,)).rowcount
            off = conn.execute(
                "DELETE FROM offers WHERE retailer=%s", (retailer,)).rowcount
            conn.commit()
        return {"price_history": ph, "offers": off}

    def unmerge(self, product_id):
        with self.pool.connection() as conn:
            conn.execute("UPDATE products SET canonical_id=NULL WHERE id=%s", (product_id,))
            conn.commit()
        return {"unmerged": product_id}

    def merge_all_above(self, threshold=0.95):
        rows = self._q(
            """SELECT id, product_a, product_b, score FROM merge_candidates
               WHERE status='pending' AND score >= %s""", (threshold,))
        merged = 0
        with self.pool.connection() as conn:
            for r in rows:
                a, b = r["product_a"], r["product_b"]
                ap = conn.execute("SELECT 1 FROM offers WHERE product_id=%s AND retailer='posokanei'", (a,)).fetchone()
                bp = conn.execute("SELECT 1 FROM offers WHERE product_id=%s AND retailer='posokanei'", (b,)).fetchone()
                if bp and not ap:
                    master, dup = b, a
                else:
                    master, dup = (a, b) if a < b else (b, a)
                mc = conn.execute("SELECT canonical_id FROM products WHERE id=%s", (master,)).fetchone()
                if mc and mc["canonical_id"] is not None:
                    master = mc["canonical_id"]
                conn.execute("UPDATE products SET canonical_id=%s WHERE id=%s", (master, dup))
                conn.execute("UPDATE merge_candidates SET status='approved', decided_at=now() WHERE id=%s", (r["id"],))
                merged += 1
            conn.commit()
        return {"merged": merged, "threshold": threshold}

    def close(self):
        self.pool.close()
