"""
SQLite storage — normalized for cross-chain price comparison.

Three tables (one-to-many-to-many):
  products       — one row per REAL product, identified by a cross-chain key
                   (product_key). Holds identity: name, brand, category, image.
  offers         — one row per (product, retailer). A product has many offers,
                   one per chain that carries it. This is the one-to-many link.
  price_history  — one row per (offer, observation time). The price time-series
                   hangs off the OFFER, so each retailer keeps its own history.

Identity rule (product_key):
  - If the source provides a SHARED key (e.g. the posokanei product id, which
    is the same across all retailers), use "sku:<value>".
  - Otherwise (direct scrapers, whose skus are retailer-internal and NOT shared)
    fall back to a deterministic key from name+brand: "nb:<norm_name>|<norm_brand>".
"""

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_key  TEXT    NOT NULL UNIQUE,
    name         TEXT    NOT NULL,
    brand        TEXT,
    category     TEXT,
    image_url    TEXT,
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    retailer     TEXT    NOT NULL,
    retailer_sku TEXT,
    url          TEXT,
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT,
    UNIQUE(product_id, retailer)
);

CREATE TABLE IF NOT EXISTS price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id     INTEGER NOT NULL REFERENCES offers(id),
    observed_at  TEXT    NOT NULL,
    price        REAL,
    list_price   REAL,
    currency     TEXT    DEFAULT 'EUR',
    in_stock     INTEGER,
    unit_price   REAL,
    unit         TEXT,
    raw          TEXT
);

CREATE INDEX IF NOT EXISTS idx_offers_product ON offers(product_id);
CREATE INDEX IF NOT EXISTS idx_price_offer_time ON price_history(offer_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE INDEX IF NOT EXISTS idx_price_offer ON price_history(offer_id);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject     TEXT NOT NULL UNIQUE,   -- OIDC 'sub' (stable provider user id)
    email       TEXT,
    name        TEXT,
    created_at  TEXT NOT NULL,
    last_login  TEXT
);

CREATE TABLE IF NOT EXISTS lists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id),   -- owner (NULL = legacy/unowned)
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_lists_user ON lists(user_id);

CREATE TABLE IF NOT EXISTS list_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    product_id  INTEGER REFERENCES products(id),   -- linked catalog product (nullable)
    name        TEXT    NOT NULL,                   -- snapshot of the name
    category    TEXT,
    qty         INTEGER DEFAULT 1,
    added_at    TEXT    NOT NULL,
    added_price REAL,                               -- price when added (for "since added" deltas)
    planned_at  TEXT,                               -- when this item was last "planned"
    planned_retailer TEXT,                          -- cheapest chain at plan time
    planned_price    REAL                           -- cheapest price at plan time
);
CREATE INDEX IF NOT EXISTS idx_list_items_list ON list_items(list_id);

-- De-duplication: a product can point to a canonical "master" product.
-- canonical_id IS NULL means the product is itself canonical (the default).
-- Queries resolve through this pointer; merges are reversible (just re-null it).
CREATE TABLE IF NOT EXISTS merge_candidates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_a   INTEGER NOT NULL REFERENCES products(id),
    product_b   INTEGER NOT NULL REFERENCES products(id),
    score       REAL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    UNIQUE(product_a, product_b)
);
CREATE INDEX IF NOT EXISTS idx_merge_status ON merge_candidates(status);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def _norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def product_key(name, brand=None, shared_sku=None):
    if shared_sku:
        return f"sku:{shared_sku}"
    return f"nb:{_norm(name)}|{_norm(brand)}"


class Store:
    def __init__(self, path="prices.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # Unicode-aware normalize for search: SQLite's built-in LOWER() is
        # ASCII-only, so Greek/accented names won't match. Register _norm so
        # queries can normalize both sides consistently.
        self.conn.create_function("normalize", 1, _norm, deterministic=True)
        self._maybe_migrate_old_schema()
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self):
        """Add columns introduced after a DB was first created (idempotent)."""
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(list_items)").fetchall()}
        for col, decl in [("planned_at", "TEXT"),
                          ("planned_retailer", "TEXT"),
                          ("planned_price", "REAL")]:
            if col not in cols:
                self.conn.execute(f"ALTER TABLE list_items ADD COLUMN {col} {decl}")
        lcols = {r["name"] for r in
                 self.conn.execute("PRAGMA table_info(lists)").fetchall()}
        if "user_id" not in lcols:
            self.conn.execute("ALTER TABLE lists ADD COLUMN user_id INTEGER")
        pcols = {r["name"] for r in
                 self.conn.execute("PRAGMA table_info(products)").fetchall()}
        if "canonical_id" not in pcols:
            self.conn.execute("ALTER TABLE products ADD COLUMN canonical_id INTEGER")
        for col, decl in [("size_value", "REAL"), ("size_unit", "TEXT"),
                          ("size_dim", "TEXT"), ("embedding", "BLOB")]:
            if col not in pcols:
                self.conn.execute(f"ALTER TABLE products ADD COLUMN {col} {decl}")

    @contextmanager
    def _tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _maybe_migrate_old_schema(self):
        tbls = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "products" not in tbls:
            return
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(products)").fetchall()}
        if "product_key" in cols:
            return
        if "retailer" not in cols:
            return

        print("Migrating old database to normalized schema...")
        self.conn.execute("ALTER TABLE products RENAME TO products_old")
        if "price_history" in tbls:
            self.conn.execute("ALTER TABLE price_history RENAME TO price_history_old")
        self.conn.executescript(SCHEMA)

        old_products = self.conn.execute("SELECT * FROM products_old").fetchall()
        ocols = {r["name"] for r in
                 self.conn.execute("PRAGMA table_info(products_old)").fetchall()}
        live_tbls = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        has_ph_old = "price_history_old" in live_tbls

        for op in old_products:
            name = op["name"]
            brand = op["brand"] if "brand" in ocols else None
            sku = op["sku"] if "sku" in ocols else None
            retailer = op["retailer"]
            category = op["category"] if "category" in ocols else None
            image_url = op["image_url"] if "image_url" in ocols else None
            url = op["url"] if "url" in ocols else None

            shared = self._sku_is_shared(sku) if sku else False
            pkey = product_key(name, brand, shared_sku=sku if shared else None)

            pid = self._get_or_create_product(pkey, name, brand, category, image_url)
            oid = self._get_or_create_offer(pid, retailer, sku, url)

            if has_ph_old:
                phcols = {r["name"] for r in self.conn.execute(
                    "PRAGMA table_info(price_history_old)").fetchall()}
                old_ph = self.conn.execute(
                    "SELECT * FROM price_history_old WHERE product_id=?",
                    (op["id"],)).fetchall()
                for h in old_ph:
                    self.conn.execute(
                        """INSERT INTO price_history
                           (offer_id, observed_at, price, list_price, currency,
                            in_stock, unit_price, unit, raw)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (oid, h["observed_at"], h["price"],
                         h["list_price"] if "list_price" in phcols else None,
                         h["currency"] if "currency" in phcols else "EUR",
                         h["in_stock"] if "in_stock" in phcols else None,
                         h["unit_price"] if "unit_price" in phcols else None,
                         h["unit"] if "unit" in phcols else None,
                         h["raw"] if "raw" in phcols else None))

        self.conn.execute("DROP TABLE products_old")
        if has_ph_old:
            self.conn.execute("DROP TABLE price_history_old")
        self.conn.commit()
        print(f"Migration complete: {len(old_products)} old rows restructured.")

    def _sku_is_shared(self, sku):
        n = self.conn.execute(
            "SELECT COUNT(DISTINCT retailer) c FROM products_old WHERE sku=?",
            (sku,)).fetchone()["c"]
        return n > 1

    def _get_or_create_product(self, pkey, name, brand, category, image_url):
        cur = self.conn.execute(
            "SELECT id FROM products WHERE product_key=?", (pkey,))
        row = cur.fetchone()
        now = utcnow()
        if row:
            self.conn.execute(
                """UPDATE products SET
                     name=?, brand=COALESCE(?,brand),
                     category=COALESCE(?,category),
                     image_url=COALESCE(?,image_url), last_seen=?
                   WHERE id=?""",
                (name, brand, category, image_url, now, row["id"]))
            return row["id"]
        cur = self.conn.execute(
            """INSERT INTO products
               (product_key, name, brand, category, image_url, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?)""",
            (pkey, name, brand, category, image_url, now, now))
        return cur.lastrowid

    def _get_or_create_offer(self, product_id, retailer, retailer_sku, url):
        cur = self.conn.execute(
            "SELECT id FROM offers WHERE product_id=? AND retailer=?",
            (product_id, retailer))
        row = cur.fetchone()
        now = utcnow()
        if row:
            self.conn.execute(
                """UPDATE offers SET retailer_sku=COALESCE(?,retailer_sku),
                     url=COALESCE(?,url), last_seen=? WHERE id=?""",
                (retailer_sku, url, now, row["id"]))
            return row["id"]
        cur = self.conn.execute(
            """INSERT INTO offers
               (product_id, retailer, retailer_sku, url, first_seen, last_seen)
               VALUES (?,?,?,?,?,?)""",
            (product_id, retailer, retailer_sku, url, now, now))
        return cur.lastrowid

    def upsert_product(self, retailer, name, url, sku=None, brand=None,
                       category=None, image_url=None, shared_key=None,
                       block_duplicates=None):
        if block_duplicates is None:
            block_duplicates = getattr(self, "block_on_ingest", False)
        with self._tx():
            pkey = product_key(name, brand, shared_sku=shared_key)
            pid = self._get_or_create_product(pkey, name, brand, category, image_url)
            oid = self._get_or_create_offer(pid, retailer, sku, url)
        # Backfill size + (optionally) block obvious duplicates outside the tx.
        self._enrich_product_size(pid, name)
        if block_duplicates:
            try:
                self.block_duplicate_at_ingest(pid)
            except Exception:
                pass   # never let matching break ingestion
        return oid

    def _enrich_product_size(self, product_id, name):
        """Backfill size_value/unit/dim from the name if not already set."""
        try:
            import matcher
            info = matcher.enrich_size(name)
        except Exception:
            info = {}
        if not info:
            return
        with self._tx() as c:
            c.execute(
                """UPDATE products SET size_value=?, size_unit=?, size_dim=?
                   WHERE id=? AND (size_value IS NULL)""",
                (info.get("size_value"), info.get("size_unit"),
                 info.get("size_dim"), product_id))

    def block_duplicate_at_ingest(self, product_id):
        """If this newly-seen product is a high-confidence match to an EXISTING
        canonical product, point it at that master immediately (so it never
        reaches the review queue). Compares only against a small candidate set
        (same category) for speed. Returns the master id if blocked, else None.
        """
        import matcher
        me = self.conn.execute(
            "SELECT id, name, brand, category, canonical_id FROM products WHERE id=?",
            (product_id,)).fetchone()
        if not me or me["canonical_id"] is not None:
            return None
        # Candidate set: other canonical products in the same category.
        cands = self.conn.execute(
            """SELECT id, name, brand FROM products
               WHERE canonical_id IS NULL AND id<>? AND category IS ?
               LIMIT 400""",
            (product_id, me["category"])).fetchall()
        if not cands:
            return None
        best_id, best_score = None, 0.0
        for c in cands:
            sc = matcher.hybrid_score(me["name"], me["brand"], c["name"], c["brand"])
            if sc > best_score:
                best_id, best_score = c["id"], sc
        if best_id is not None and best_score >= matcher.AUTO_BLOCK:
            with self._tx() as cx:
                cx.execute("UPDATE products SET canonical_id=? WHERE id=?",
                           (best_id, product_id))
            return best_id
        return None

    def record_price(self, product_id, price, list_price=None, in_stock=None,
                     unit_price=None, unit=None, currency="EUR", raw=None):
        if isinstance(in_stock, bool):      # store as 1/0, consistent with PG
            in_stock = int(in_stock)
        with self._tx() as c:
            c.execute(
                """INSERT INTO price_history
                   (offer_id, observed_at, price, list_price, currency,
                    in_stock, unit_price, unit, raw)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (product_id, utcnow(), price, list_price, currency,
                 in_stock, unit_price, unit, raw))

    def latest_prices(self, retailer=None):
        q = """
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
            q += " WHERE o.retailer = ?"
            params = (retailer,)
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]

    def _canonical_group(self, product_id):
        """Return all product ids in the canonical cluster of product_id:
        the master plus every duplicate pointing at it. If product_id is itself
        a duplicate, resolves to its master's group."""
        row = self.conn.execute(
            "SELECT canonical_id FROM products WHERE id=?", (product_id,)).fetchone()
        if row is None:
            return [product_id]
        master = row["canonical_id"] if row["canonical_id"] is not None else product_id
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM products WHERE id=? OR canonical_id=?",
            (master, master)).fetchall()]
        return ids or [product_id]

    def search_products(self, query="", retailer=None, on_offer=False,
                        in_stock_only=False, limit=500, category=None):
        """
        Search by name (and optionally category). Returns one row per
        (canonical product, retailer) — duplicates are folded into their master,
        so a merged product shows offers gathered from every chain in its group.
        """
        # Step 1: matching CANONICAL products only (duplicates are hidden).
        pq = "SELECT id FROM products WHERE canonical_id IS NULL"
        pparams = []
        if query:
            pq += " AND normalize(name) LIKE '%' || normalize(?) || '%'"
            pparams.append(query)
        if category:
            pq += " AND category = ?"
            pparams.append(category)
        pq += " ORDER BY name LIMIT ?"
        pparams.append(limit)
        masters = [r["id"] for r in self.conn.execute(pq, pparams).fetchall()]
        if not masters:
            return []

        # Expand each master to its full canonical group, and remember which
        # master each duplicate maps to so offers attribute to the master row.
        group_of = {}   # any product id -> its master id
        all_ids = []
        for m in masters:
            for pid in self._canonical_group(m):
                group_of[pid] = m
                all_ids.append(pid)

        placeholders = ",".join("?" * len(all_ids))
        sql = f"""
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
            WHERE o2.product_id IN ({placeholders})
            GROUP BY ph.offer_id
        ) last ON last.offer_id = h.offer_id AND last.mx = h.observed_at
        WHERE p.id IN ({placeholders})
        """
        params = list(all_ids) + list(all_ids)
        if retailer:
            sql += " AND o.retailer = ?"
            params.append(retailer)
        if on_offer:
            sql += " AND h.list_price IS NOT NULL AND h.list_price > h.price"
        if in_stock_only:
            sql += " AND h.in_stock = 1"
        sql += " ORDER BY p.name, o.retailer"
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        # Re-attribute each row to its MASTER product id and pull master identity.
        master_info = {m: self.product_info(m) for m in masters}
        for r in rows:
            m = group_of.get(r["raw_product_id"], r["raw_product_id"])
            r["product_id"] = m
            mi = master_info.get(m)
            if mi:   # show the master's name/brand/category, not the duplicate's
                r["name"] = mi["name"]; r["brand"] = mi["brand"]
                r["category"] = mi["category"]; r["image_url"] = mi["image_url"]
            r.pop("raw_product_id", None)
        return rows

    def price_history(self, offer_id):
        sql = """
        SELECT observed_at, price, list_price, in_stock
        FROM price_history WHERE offer_id = ? ORDER BY observed_at ASC
        """
        return [dict(r) for r in self.conn.execute(sql, (offer_id,)).fetchall()]

    def product_offers(self, product_id):
        group = self._canonical_group(product_id)
        placeholders = ",".join("?" * len(group))
        sql = f"""
        SELECT o.id AS offer_id, o.retailer, h.price, h.list_price, h.observed_at
        FROM offers o
        JOIN price_history h ON h.offer_id = o.id
        JOIN (SELECT offer_id, MAX(observed_at) mx FROM price_history
              GROUP BY offer_id) last
          ON last.offer_id = h.offer_id AND last.mx = h.observed_at
        WHERE o.product_id IN ({placeholders})
        ORDER BY h.price ASC
        """
        return [dict(r) for r in self.conn.execute(sql, group).fetchall()]

    def product_info(self, product_id):
        """Identity (name/brand/category/image) for one product."""
        row = self.conn.execute(
            """SELECT id, product_key, name, brand, category, image_url,
                      first_seen, last_seen
               FROM products WHERE id = ?""", (product_id,)).fetchone()
        return dict(row) if row else None

    def categories(self, limit=200):
        """Distinct non-empty categories with product counts, most common first."""
        sql = """SELECT category, COUNT(*) AS n FROM products
                 WHERE category IS NOT NULL AND TRIM(category) <> ''
                 GROUP BY category ORDER BY n DESC LIMIT ?"""
        return [dict(r) for r in self.conn.execute(sql, (limit,)).fetchall()]

    def retailers(self):
        """Distinct retailers present in the data, with offer counts."""
        sql = """SELECT retailer, COUNT(*) AS offers
                 FROM offers GROUP BY retailer ORDER BY offers DESC"""
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def stats(self):
        """Summary counts for the dashboard (canonical-aware)."""
        c = self.conn
        # multi_chain: group offers by the product's MASTER (coalesce canonical_id
        # to own id), so a merged product counts the chains across its whole group.
        multi = c.execute("""
            SELECT COUNT(*) n FROM (
                SELECT COALESCE(p.canonical_id, p.id) AS master
                FROM offers o JOIN products p ON p.id = o.product_id
                GROUP BY COALESCE(p.canonical_id, p.id)
                HAVING COUNT(DISTINCT o.retailer) > 1)
        """).fetchone()["n"]
        return {
            "products": c.execute(
                "SELECT COUNT(*) n FROM products WHERE canonical_id IS NULL").fetchone()["n"],
            "offers": c.execute("SELECT COUNT(*) n FROM offers").fetchone()["n"],
            "observations": c.execute("SELECT COUNT(*) n FROM price_history").fetchone()["n"],
            "retailers": c.execute("SELECT COUNT(DISTINCT retailer) n FROM offers").fetchone()["n"],
            "multi_chain": multi,
        }

    def product_price_history(self, product_id):
        """
        Full price history for a product across ALL its chains.
        Returns {retailer: [{observed_at, price}, ...]} for trend charts.
        """
        sql = """
        SELECT o.retailer, h.observed_at, h.price
        FROM offers o
        JOIN price_history h ON h.offer_id = o.id
        WHERE o.product_id = ? AND h.price IS NOT NULL
        ORDER BY o.retailer, h.observed_at ASC
        """
        out = {}
        for r in self.conn.execute(sql, (product_id,)).fetchall():
            out.setdefault(r["retailer"], []).append(
                {"observed_at": r["observed_at"], "price": r["price"]})
        return out

    def biggest_changes(self, days=30, limit=40, product_ids=None):
        """
        Biggest price changes per product over the last `days`.
        For each offer, compare the latest price to the most recent price at or
        before the cutoff, then take each product's largest-magnitude move.
        Returns [{product_id, name, brand, category, retailer, old, new,
                  change, pct, observed_at}] sorted by |pct| desc.
        Optionally restrict to product_ids (e.g. items on the user's lists).
        """
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        filt = ""
        params = [cutoff]
        if product_ids:
            placeholders = ",".join("?" * len(product_ids))
            filt = f"AND o.product_id IN ({placeholders})"
            params += list(product_ids)

        # latest price per offer, and the baseline (latest at/before cutoff)
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
                  WHERE observed_at <= ? GROUP BY offer_id) m
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
        rows = self.conn.execute(sql, params).fetchall()

        # Keep each product's single largest-magnitude move.
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
                    "pct": round(pct, 1), "observed_at": r["observed_at"],
                }
        movers = sorted(best.values(), key=lambda x: abs(x["pct"]), reverse=True)
        return movers[:limit]

    # ------------------------------------------------------------------
    # Server-stored shopping lists
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Users (OIDC-backed; we store only the provider subject + profile)
    # ------------------------------------------------------------------
    def upsert_user(self, subject, email=None, name=None):
        """Find-or-create a user by OIDC subject; return the user id."""
        with self._tx() as c:
            row = c.execute("SELECT id FROM users WHERE subject=?", (subject,)).fetchone()
            if row:
                c.execute("UPDATE users SET email=?, name=?, last_login=? WHERE id=?",
                          (email, name, utcnow(), row["id"]))
                return row["id"]
            cur = c.execute(
                "INSERT INTO users (subject, email, name, created_at, last_login) VALUES (?,?,?,?,?)",
                (subject, email, name, utcnow(), utcnow()))
            return cur.lastrowid

    def owns_list(self, list_id, user_id):
        """True iff this list belongs to user_id. Used to authorize every op."""
        row = self.conn.execute(
            "SELECT user_id FROM lists WHERE id=?", (list_id,)).fetchone()
        return row is not None and row["user_id"] == user_id

    def owns_item(self, item_id, user_id):
        row = self.conn.execute(
            """SELECT l.user_id FROM list_items li
               JOIN lists l ON l.id = li.list_id WHERE li.id=?""",
            (item_id,)).fetchone()
        return row is not None and row["user_id"] == user_id

    def create_list(self, name, user_id=None):
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO lists (name, user_id, created_at, updated_at) VALUES (?,?,?,?)",
                (name, user_id, utcnow(), utcnow()))
            return cur.lastrowid

    def rename_list(self, list_id, name):
        with self._tx() as c:
            c.execute("UPDATE lists SET name=?, updated_at=? WHERE id=?",
                      (name, utcnow(), list_id))

    def delete_list(self, list_id):
        with self._tx() as c:
            c.execute("DELETE FROM list_items WHERE list_id=?", (list_id,))
            c.execute("DELETE FROM lists WHERE id=?", (list_id,))

    def all_lists(self, user_id=None):
        sql = """
        SELECT l.id, l.name, l.created_at, l.updated_at,
               COUNT(li.id) AS item_count
        FROM lists l LEFT JOIN list_items li ON li.list_id = l.id
        WHERE l.user_id IS ?
        GROUP BY l.id ORDER BY l.updated_at DESC
        """
        return [dict(r) for r in self.conn.execute(sql, (user_id,)).fetchall()]

    def add_list_item(self, list_id, name, product_id=None, category=None,
                      qty=1, added_price=None):
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO list_items
                   (list_id, product_id, name, category, qty, added_at, added_price)
                   VALUES (?,?,?,?,?,?,?)""",
                (list_id, product_id, name, category, qty, utcnow(), added_price))
            c.execute("UPDATE lists SET updated_at=? WHERE id=?", (utcnow(), list_id))
            return cur.lastrowid

    def remove_list_item(self, item_id):
        with self._tx() as c:
            c.execute("DELETE FROM list_items WHERE id=?", (item_id,))

    def list_review(self, list_id):
        """
        A list with each item's current cheapest price and the change since it
        was added (and since 30 days ago) — for reviewing items over time.
        """
        meta = self.conn.execute(
            "SELECT id, name, created_at, updated_at FROM lists WHERE id=?",
            (list_id,)).fetchone()
        if not meta:
            return None
        items = self.conn.execute(
            """SELECT id, product_id, name, category, qty, added_at, added_price
               FROM list_items WHERE list_id=? ORDER BY added_at""",
            (list_id,)).fetchall()

        out_items = []
        for it in items:
            entry = dict(it)
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
                "created_at": meta["created_at"], "updated_at": meta["updated_at"],
                "items": out_items}

    def plan_list(self, list_id):
        """
        Snapshot the current cheapest retailer + price for each linked item as
        the 'planned' baseline. Call this when the user plans/generates the list.
        """
        items = self.conn.execute(
            "SELECT id, product_id FROM list_items WHERE list_id=?",
            (list_id,)).fetchall()
        with self._tx() as c:
            for it in items:
                if it["product_id"] is None:
                    continue
                offers = self.product_offers(it["product_id"])
                priced = [(o["retailer"], o["price"]) for o in offers if o["price"] is not None]
                if not priced:
                    continue
                priced.sort(key=lambda x: x[1])
                ret, pr = priced[0]
                c.execute(
                    """UPDATE list_items
                       SET planned_at=?, planned_retailer=?, planned_price=?
                       WHERE id=?""",
                    (utcnow(), ret, round(pr, 2), it["id"]))
        return self.revaluate_list(list_id)

    def revaluate_list(self, list_id):
        """
        For each item with a plan baseline, compare the cheapest store/price THEN
        vs NOW. Flags items where the cheapest retailer changed, or the price
        moved. Returns {name, items:[...], summary}.
        """
        meta = self.conn.execute(
            "SELECT id, name FROM lists WHERE id=?", (list_id,)).fetchone()
        if not meta:
            return None
        items = self.conn.execute(
            """SELECT id, product_id, name, category, qty,
                      planned_at, planned_retailer, planned_price
               FROM list_items WHERE list_id=? ORDER BY name""",
            (list_id,)).fetchall()

        out = []
        n_store_changed = 0
        total_planned = 0.0
        total_now = 0.0
        for it in items:
            entry = {"id": it["id"], "name": it["name"], "qty": it["qty"] or 1,
                     "planned_retailer": it["planned_retailer"],
                     "planned_price": it["planned_price"],
                     "current_retailer": None, "current_price": None,
                     "store_changed": False, "price_delta": None, "planned": it["planned_at"] is not None}
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
        """Price history (across chains) for a list item's linked product."""
        row = self.conn.execute(
            "SELECT product_id FROM list_items WHERE id=?", (item_id,)).fetchone()
        if not row or row["product_id"] is None:
            return {}
        return self.product_price_history(row["product_id"])

    def sync_list(self, list_id, items, deleted_ids=None):
        """
        Reconcile a list from an offline client (the baked phone file).

        `items` is the client's current item set; each may have a server `id`
        (existing item) or no id / a client-only id (new item added offline).
        `deleted_ids` are server item ids the client removed offline.

        Strategy (conservative, last-writer-wins per item):
          - existing items (numeric id present in DB): UPDATE qty/name/category.
          - new items (id missing from DB): INSERT.
          - deleted_ids: DELETE those rows.
        Returns the refreshed review.
        """
        if not self.conn.execute("SELECT 1 FROM lists WHERE id=?", (list_id,)).fetchone():
            return None
        existing = {r["id"] for r in self.conn.execute(
            "SELECT id FROM list_items WHERE list_id=?", (list_id,)).fetchall()}

        with self._tx() as c:
            for d in (deleted_ids or []):
                if isinstance(d, int) and d in existing:
                    c.execute("DELETE FROM list_items WHERE id=? AND list_id=?", (d, list_id))
            for it in items:
                iid = it.get("server_id")
                if isinstance(iid, int) and iid in existing:
                    c.execute(
                        """UPDATE list_items SET name=?, category=?, qty=? WHERE id=? AND list_id=?""",
                        (it.get("name") or "(item)", it.get("cat") or it.get("category"),
                         int(it.get("qty", 1)), iid, list_id))
                else:
                    c.execute(
                        """INSERT INTO list_items
                           (list_id, product_id, name, category, qty, added_at, added_price)
                           VALUES (?,?,?,?,?,?,?)""",
                        (list_id, it.get("productId"), it.get("name") or "(item)",
                         it.get("cat") or it.get("category"),
                         int(it.get("qty", 1)), utcnow(), it.get("price")))
            c.execute("UPDATE lists SET updated_at=? WHERE id=?", (utcnow(), list_id))
        return self.list_review(list_id)

    # ------------------------------------------------------------------
    # De-duplication
    # ------------------------------------------------------------------
    def scan_duplicates(self, auto_merge=True, suggest=True):
        """
        Scan canonical products and:
          * TIER 1: auto-merge products whose exact normalized match_key is equal
            (only when auto_merge=True). Safe/deterministic.
          * TIER 2: queue fuzzy look-alikes as pending merge_candidates for review
            (only when suggest=True).
        Posokanei masters are preferred as the canonical target. Returns a summary.
        """
        import dedup
        rows = self.conn.execute(
            """SELECT p.id, p.name, p.brand, p.product_key,
                      EXISTS(SELECT 1 FROM offers o WHERE o.product_id=p.id
                             AND o.retailer='posokanei') AS is_poso
               FROM products p WHERE p.canonical_id IS NULL""").fetchall()
        prods = [dict(r) for r in rows]

        # TIER 1 — bucket by exact key.
        merged = 0
        buckets = {}
        for p in prods:
            k = dedup.match_key(p["name"], p["brand"])
            buckets.setdefault(k, []).append(p)
        if auto_merge:
            with self._tx() as c:
                for k, group in buckets.items():
                    if len(group) < 2:
                        continue
                    # canonical target: prefer a posokanei product, else lowest id
                    group.sort(key=lambda x: (0 if x["is_poso"] else 1, x["id"]))
                    master = group[0]
                    for dup in group[1:]:
                        c.execute("UPDATE products SET canonical_id=? WHERE id=?",
                                  (master["id"], dup["id"]))
                        merged += 1

        # TIER 2 — candidate pairs via TF-IDF pre-filter + rapidfuzz confirm +
        # size/quantity gates (matcher.find_candidate_pairs). One vectorized pass
        # over the whole catalog replaces the old per-block pairwise loop.
        suggested = 0
        if suggest:
            import matcher
            remaining = [dict(r) for r in self.conn.execute(
                "SELECT id, name, brand FROM products WHERE canonical_id IS NULL").fetchall()]
            pairs = matcher.find_candidate_pairs(remaining)
            with self._tx() as c:
                for lo_id, hi_id, score in pairs:
                    if score >= 1.0:
                        continue          # exact matches are Tier-1's job
                    lo, hi = sorted((lo_id, hi_id))
                    try:
                        c.execute(
                            """INSERT INTO merge_candidates
                               (product_a, product_b, score, status, created_at)
                               VALUES (?,?,?, 'pending', ?)
                               ON CONFLICT(product_a, product_b) DO UPDATE
                                 SET score = excluded.score
                                 WHERE status = 'pending'""",
                            (lo, hi, round(score, 3), utcnow()))
                        suggested += 1
                    except Exception:
                        pass
        return {"auto_merged": merged, "candidates_added": suggested}

    def merge_candidates(self, limit=200):
        """Pending fuzzy candidates with both products' names for review."""
        sql = """
        SELECT mc.id, mc.score,
               a.id AS a_id, a.name AS a_name, a.brand AS a_brand,
               b.id AS b_id, b.name AS b_name, b.brand AS b_brand
        FROM merge_candidates mc
        JOIN products a ON a.id = mc.product_a
        JOIN products b ON b.id = mc.product_b
        WHERE mc.status='pending'
        ORDER BY mc.score DESC LIMIT ?
        """
        return [dict(r) for r in self.conn.execute(sql, (limit,)).fetchall()]

    def resolve_candidate(self, candidate_id, approve, into=None):
        """Approve (merge b->a, or into a chosen master) or reject a candidate."""
        row = self.conn.execute(
            "SELECT product_a, product_b FROM merge_candidates WHERE id=?",
            (candidate_id,)).fetchone()
        if not row:
            return None
        with self._tx() as c:
            if approve:
                a, b = row["product_a"], row["product_b"]
                master = into if into in (a, b) else a
                dup = b if master == a else a
                c.execute("UPDATE products SET canonical_id=? WHERE id=?", (master, dup))
                c.execute(
                    "UPDATE merge_candidates SET status='approved', decided_at=? WHERE id=?",
                    (utcnow(), candidate_id))
            else:
                c.execute(
                    "UPDATE merge_candidates SET status='rejected', decided_at=? WHERE id=?",
                    (utcnow(), candidate_id))
        return {"id": candidate_id, "approved": bool(approve)}

    def prune_stale_candidates(self):
        """Re-score every PENDING candidate against the CURRENT matching logic and
        drop those that no longer qualify (e.g. promo-pack / variant false
        positives created by an older scan). Returns how many were removed."""
        import dedup
        rows = self.conn.execute("""
            SELECT mc.id, a.name a_name, a.brand a_brand,
                   b.name b_name, b.brand b_brand
            FROM merge_candidates mc
            JOIN products a ON a.id = mc.product_a
            JOIN products b ON b.id = mc.product_b
            WHERE mc.status='pending'""").fetchall()
        removed = 0
        with self._tx() as c:
            for r in rows:
                score = dedup.fuzzy_score(r["a_name"], r["a_brand"],
                                          r["b_name"], r["b_brand"])
                if score < dedup.FUZZY_SUGGEST:
                    c.execute("DELETE FROM merge_candidates WHERE id=?", (r["id"],))
                    removed += 1
                else:
                    c.execute("UPDATE merge_candidates SET score=? WHERE id=?",
                              (score, r["id"]))
        return {"removed": removed}

    def export_candidates(self, status="pending"):
        """Return a list of dict rows describing candidates, rich enough to review
        in a spreadsheet. status=None exports all statuses."""
        where = "WHERE mc.status = ?" if status else ""
        params = (status,) if status else ()
        rows = self.conn.execute(f"""
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
        """, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # retailers present on each side (helps judge cross-chain matches)
            d["a_retailers"] = ",".join(self._retailers_for(d["a_id"]))
            d["b_retailers"] = ",".join(self._retailers_for(d["b_id"]))
            d["decision"] = ""        # blank column for the reviewer to fill
            out.append(d)
        return out

    def _retailers_for(self, product_id):
        return [r["retailer"] for r in self.conn.execute(
            "SELECT DISTINCT retailer FROM offers WHERE product_id=? ORDER BY retailer",
            (product_id,)).fetchall()]

    def import_candidate_decisions(self, decisions):
        """Apply a list of {candidate_id, decision} dicts. decision in
        {'merge','reject','skip'/''}. 'merge' keeps the LOWER product id as
        master (consistent with bulk merge). Returns counts."""
        merged = rejected = skipped = missing = 0
        with self._tx() as c:
            for d in decisions:
                cid = d.get("candidate_id")
                act = (d.get("decision") or "").strip().lower()
                if not cid or act in ("", "skip"):
                    skipped += 1
                    continue
                row = c.execute(
                    "SELECT product_a, product_b FROM merge_candidates WHERE id=?",
                    (cid,)).fetchone()
                if not row:
                    missing += 1
                    continue
                a, b = row["product_a"], row["product_b"]
                if act in ("merge", "approve", "yes", "y", "1", "true"):
                    master, dup = (a, b) if a < b else (b, a)
                    mc = c.execute("SELECT canonical_id FROM products WHERE id=?",
                                   (master,)).fetchone()
                    if mc and mc["canonical_id"] is not None:
                        master = mc["canonical_id"]
                    c.execute("UPDATE products SET canonical_id=? WHERE id=?",
                              (master, dup))
                    c.execute("UPDATE merge_candidates SET status='approved', "
                              "decided_at=? WHERE id=?", (utcnow(), cid))
                    merged += 1
                elif act in ("reject", "no", "n", "0", "false", "notsame", "not_same"):
                    c.execute("UPDATE merge_candidates SET status='rejected', "
                              "decided_at=? WHERE id=?", (utcnow(), cid))
                    rejected += 1
                else:
                    skipped += 1
        return {"merged": merged, "rejected": rejected,
                "skipped": skipped, "missing": missing}

    def purge_retailer(self, retailer):
        """Delete a retailer's offers and their price history. Products are kept
        (they're shared across chains). Returns counts deleted."""
        with self._tx() as c:
            offer_ids = [row["id"] for row in c.execute(
                "SELECT id FROM offers WHERE retailer=?", (retailer,)).fetchall()]
            ph = 0
            if offer_ids:
                placeholders = ",".join("?" * len(offer_ids))
                cur = c.execute(
                    f"DELETE FROM price_history WHERE offer_id IN ({placeholders})",
                    offer_ids)
                ph = cur.rowcount
            cur = c.execute("DELETE FROM offers WHERE retailer=?", (retailer,))
            off = cur.rowcount
        return {"price_history": ph, "offers": off}

    def unmerge(self, product_id):
        """Reverse a merge: make a product canonical again."""
        with self._tx() as c:
            c.execute("UPDATE products SET canonical_id=NULL WHERE id=?", (product_id,))
        return {"unmerged": product_id}

    def merge_all_above(self, threshold=0.95):
        """Approve+merge every pending candidate with score >= threshold.
        For each, the lower product id is kept as master (posokanei preferred is
        handled at scan time). Returns how many were merged."""
        rows = self.conn.execute(
            """SELECT id, product_a, product_b, score FROM merge_candidates
               WHERE status='pending' AND score >= ?""", (threshold,)).fetchall()
        merged = 0
        with self._tx() as c:
            for r in rows:
                a, b = r["product_a"], r["product_b"]
                # prefer a posokanei product as master, else lower id
                ap = c.execute("SELECT 1 FROM offers WHERE product_id=? AND retailer='posokanei'", (a,)).fetchone()
                bp = c.execute("SELECT 1 FROM offers WHERE product_id=? AND retailer='posokanei'", (b,)).fetchone()
                if bp and not ap:
                    master, dup = b, a
                else:
                    master, dup = (a, b) if a < b else (b, a)
                    if master != a and master != b:
                        master, dup = a, b
                # ensure master isn't itself already a duplicate
                mc = c.execute("SELECT canonical_id FROM products WHERE id=?", (master,)).fetchone()
                if mc and mc["canonical_id"] is not None:
                    master = mc["canonical_id"]
                c.execute("UPDATE products SET canonical_id=? WHERE id=?", (master, dup))
                c.execute("UPDATE merge_candidates SET status='approved', decided_at=? WHERE id=?",
                          (utcnow(), r["id"]))
                merged += 1
        return {"merged": merged, "threshold": threshold}

    def close(self):
        self.conn.close()

