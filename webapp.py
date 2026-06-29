"""
Local web app for the price monitor — a browser front-end over prices.db.

This is a LOCAL research tool: it binds to 127.0.0.1 only and reads the same
SQLite database the scrapers/ingester write. It is not meant to be a public
service (that would raise data-reuse and terms questions — use the official
posokanei platform for public price comparison).

Run:  python webapp.py
Then open http://127.0.0.1:5000

Endpoints:
  GET /                         -> the single-page UI
  GET /api/stats                -> dataset summary
  GET /api/retailers            -> retailers + offer counts
  GET /api/search?q=&retailer=  -> matching products (one row per offer)
  GET /api/product/<id>         -> identity + cross-chain prices
  GET /api/offer/<id>/history   -> price time-series for one offer
"""

import os
from flask import Flask, jsonify, request, Response

from db import get_store
import auth

DB_PATH = os.environ.get("PRICE_DB", "prices.db")

app = Flask(__name__)


@app.after_request
def cors(resp):
    # When auth is enabled (internet-exposed), restrict cross-origin calls to the
    # configured app origin. In local single-user mode, allow any origin so the
    # grocery app works from a file:// page or elsewhere on the LAN.
    if auth.auth_enabled():
        origin = os.environ.get("APP_BASE_URL", "")
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Vary"] = "Origin"
    else:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def store():
    # Fresh connection per request: SQLite connections aren't shareable across
    # Flask's worker threads, and opening is cheap.
    return get_store(DB_PATH)


# Initialise OIDC auth if configured (no-op in single-user/local mode).
auth.init_auth(app)
auth.register_routes(app, store)


@app.route("/api/stats")
def api_stats():
    s = store()
    try:
        return jsonify(s.stats())
    finally:
        s.close()


@app.route("/api/retailers")
def api_retailers():
    s = store()
    try:
        return jsonify(s.retailers())
    finally:
        s.close()


@app.route("/api/categories")
def api_categories():
    s = store()
    try:
        return jsonify(s.categories())
    finally:
        s.close()


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    retailer = request.args.get("retailer") or None
    category = request.args.get("category") or None
    on_offer = request.args.get("on_offer") == "1"
    limit = min(int(request.args.get("limit", 300)), 1000)
    s = store()
    try:
        rows = s.search_products(query=q, retailer=retailer, category=category,
                                 on_offer=on_offer, limit=limit)
        # Collapse to one entry per product for the results list, but keep the
        # per-retailer rows so the UI can show "cheapest of N chains".
        by_product = {}
        for r in rows:
            pid = r["product_id"]
            grp = by_product.setdefault(pid, {
                "product_id": pid, "name": r["name"], "brand": r["brand"],
                "category": r["category"], "image_url": r["image_url"],
                "offers": [],
            })
            grp["offers"].append({
                "offer_id": r["offer_id"], "retailer": r["retailer"],
                "price": r["price"], "list_price": r["list_price"],
            })
        out = []
        for grp in by_product.values():
            prices = [o["price"] for o in grp["offers"] if o["price"] is not None]
            grp["min_price"] = min(prices) if prices else None
            grp["max_price"] = max(prices) if prices else None
            grp["retailer_count"] = len(grp["offers"])
            out.append(grp)
        out.sort(key=lambda g: g["name"] or "")
        return jsonify(out)
    finally:
        s.close()


@app.route("/api/product/<int:product_id>/history")
def api_product_history(product_id):
    """Price history across all chains for one product (for trend charts)."""
    s = store()
    try:
        return jsonify(s.product_price_history(product_id))
    finally:
        s.close()


@app.route("/api/product/<int:product_id>")
def api_product(product_id):
    s = store()
    try:
        info = s.product_info(product_id)
        if not info:
            return jsonify({"error": "not found"}), 404
        offers = s.product_offers(product_id)
        return jsonify({"product": info, "offers": offers})
    finally:
        s.close()


@app.route("/api/offer/<int:offer_id>/history")
def api_offer_history(offer_id):
    s = store()
    try:
        return jsonify(s.price_history(offer_id))
    finally:
        s.close()


@app.route("/api/export")
def api_export():
    """
    Compact snapshot of the latest cross-chain prices, for offline use in the
    grocery app. One entry per product: name, brand, category, image, and the
    price at each chain (cheapest first).
    """
    s = store()
    try:
        # Pull every product's offers with latest price, in as few queries as
        # possible. We reuse search_products with an empty query (all products).
        rows = s.search_products(query="", limit=100000)
        by_product = {}
        for r in rows:
            pid = r["product_id"]
            grp = by_product.setdefault(pid, {
                "id": pid, "name": r["name"], "brand": r["brand"],
                "category": r["category"], "image": r["image_url"],
                "prices": {},
            })
            if r["price"] is not None:
                grp["prices"][r["retailer"]] = round(r["price"], 2)
        products = []
        for g in by_product.values():
            if not g["prices"]:
                continue
            vals = list(g["prices"].values())
            g["min"] = min(vals)
            g["max"] = max(vals)
            products.append(g)
        products.sort(key=lambda g: g["name"] or "")
        from datetime import datetime, timezone
        return jsonify({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(products),
            "products": products,
        })
    finally:
        s.close()


@app.route("/api/generate", methods=["POST", "OPTIONS"])
def api_generate():
    """
    Produce a self-contained shopper HTML file with the whole catalog + the
    chosen list baked in. The phone app then works fully offline.
    Body: {"list": {"name": str, "items": [...]}}
    """
    if request.method == "OPTIONS":
        resp = Response("")
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    body = request.get_json(silent=True) or {}
    chosen_list = body.get("list", {"name": "Shopping list", "items": []})
    list_id = body.get("list_id")

    s = store()
    try:
        rows = s.search_products(query="", limit=100000)
        # biggest movers (market-wide) frozen into the file for offline viewing
        movers = s.biggest_changes(days=30, limit=60)
        # If a DB list id was given, bake that real list (so the offline file
        # carries the list id and can sync its edits back later).
        if list_id is not None:
            review = s.list_review(int(list_id))
            if review:
                # Generating the offline file counts as "planning" this list:
                # snapshot each item's current cheapest store as the baseline,
                # so later re-evaluation can flag stores that became cheaper.
                try:
                    s.plan_list(int(list_id))
                except Exception:
                    pass
                chosen_list = {
                    "id": review["id"],
                    "name": review["name"],
                    "items": [{
                        "id": it["id"],
                        "name": it["name"],
                        "cat": it.get("category") or "Other",
                        "qty": it.get("qty") or 1,
                        "price": it.get("current_min") if it.get("current_min") is not None else (it.get("added_price") or 0),
                        "productId": it.get("product_id"),
                        "spread": it.get("current_prices") or None,
                    } for it in review["items"]],
                }
    finally:
        s.close()

    # Build a compact catalog: one entry per product with cross-chain prices.
    by_product = {}
    for r in rows:
        pid = r["product_id"]
        g = by_product.setdefault(pid, {
            "id": pid, "name": r["name"], "brand": r["brand"],
            "category": r["category"], "prices": {},
        })
        if r["price"] is not None:
            g["prices"][r["retailer"]] = round(r["price"], 2)
    catalog = []
    for g in by_product.values():
        if not g["prices"]:
            continue
        vals = list(g["prices"].values())
        g["min"], g["max"] = min(vals), max(vals)
        catalog.append(g)
    catalog.sort(key=lambda g: g["name"] or "")

    from datetime import datetime, timezone
    baked = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "catalog": catalog,
        "list": chosen_list,
        "changes": {
            "days": 30,
            "up": [m for m in movers if m["change"] > 0][:40],
            "down": [m for m in movers if m["change"] < 0][:40],
        },
    }

    # Read the shopper template and inject the baked data.
    tpl_path = os.path.join(os.path.dirname(__file__), "grocery_list.html")
    html = open(tpl_path, encoding="utf-8").read()
    import json as _json
    inject = ("window.BAKED = " +
              _json.dumps(baked, ensure_ascii=False).replace("</", "<\\/") +
              ";")
    html = html.replace("/* ===== BAKED_DATA_PLACEHOLDER ===== */", inject, 1)

    fname = "shopping_" + datetime.now().strftime("%Y%m%d") + ".html"
    return Response(html, mimetype="text/html",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.route("/api/changes")
def api_changes():
    """
    Biggest price movers over a window. Query params:
      days=30, limit=40, products=comma,separated,ids (optional, for 'my lists')
    Returns {up:[...], down:[...]}.
    """
    days = int(request.args.get("days", 30))
    limit = min(int(request.args.get("limit", 40)), 200)
    prod = request.args.get("products")
    product_ids = [int(x) for x in prod.split(",") if x.strip().isdigit()] if prod else None
    s = store()
    try:
        movers = s.biggest_changes(days=days, limit=limit*2, product_ids=product_ids)
        up = [m for m in movers if m["change"] > 0][:limit]
        down = [m for m in movers if m["change"] < 0][:limit]
        return jsonify({"days": days, "up": up, "down": down})
    finally:
        s.close()


@app.route("/api/lists", methods=["GET", "POST"])
@auth.login_required
def api_lists():
    uid = auth.current_user_id()
    s = store()
    try:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            name = (body.get("name") or "Untitled").strip()
            lid = s.create_list(name, user_id=uid)
            return jsonify({"id": lid, "name": name})
        return jsonify(s.all_lists(user_id=uid))
    finally:
        s.close()


@app.route("/api/lists/<int:list_id>", methods=["GET", "PATCH", "DELETE"])
@auth.login_required
def api_list(list_id):
    uid = auth.current_user_id()
    s = store()
    try:
        if auth.auth_enabled() and not s.owns_list(list_id, uid):
            return jsonify({"error": "forbidden"}), 403
        if request.method == "DELETE":
            s.delete_list(list_id)
            return jsonify({"deleted": list_id})
        if request.method == "PATCH":
            body = request.get_json(silent=True) or {}
            name = (body.get("name") or "").strip()
            if name:
                s.rename_list(list_id, name)
            return jsonify({"id": list_id, "name": name})
        review = s.list_review(list_id)
        if review is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(review)
    finally:
        s.close()


@app.route("/api/lists/<int:list_id>/items", methods=["POST"])
@auth.login_required
def api_list_add_item(list_id):
    uid = auth.current_user_id()
    body = request.get_json(silent=True) or {}
    s = store()
    try:
        if auth.auth_enabled() and not s.owns_list(list_id, uid):
            return jsonify({"error": "forbidden"}), 403
        item_id = s.add_list_item(
            list_id,
            name=body.get("name") or "(item)",
            product_id=body.get("product_id"),
            category=body.get("category"),
            qty=int(body.get("qty", 1)),
            added_price=body.get("added_price"),
        )
        return jsonify({"id": item_id})
    finally:
        s.close()


@app.route("/api/list-items/<int:item_id>", methods=["DELETE"])
@auth.login_required
def api_list_remove_item(item_id):
    uid = auth.current_user_id()
    s = store()
    try:
        if auth.auth_enabled() and not s.owns_item(item_id, uid):
            return jsonify({"error": "forbidden"}), 403
        s.remove_list_item(item_id)
        return jsonify({"deleted": item_id})
    finally:
        s.close()


@app.route("/api/list-items/<int:item_id>/history")
@auth.login_required
def api_list_item_history(item_id):
    uid = auth.current_user_id()
    s = store()
    try:
        if auth.auth_enabled() and not s.owns_item(item_id, uid):
            return jsonify({"error": "forbidden"}), 403
        return jsonify(s.list_item_history(item_id))
    finally:
        s.close()


@app.route("/api/lists/<int:list_id>/sync", methods=["POST", "OPTIONS"])
@auth.login_required
def api_list_sync(list_id):
    if request.method == "OPTIONS":
        resp = Response("")
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp
    uid = auth.current_user_id()
    body = request.get_json(silent=True) or {}
    s = store()
    try:
        if auth.auth_enabled() and not s.owns_list(list_id, uid):
            return jsonify({"error": "forbidden"}), 403
        review = s.sync_list(list_id, body.get("items", []),
                             body.get("deleted_ids", []))
        if review is None:
            return jsonify({"error": "list not found"}), 404
        return jsonify(review)
    finally:
        s.close()


@app.route("/api/lists/<int:list_id>/plan", methods=["POST"])
@auth.login_required
def api_list_plan(list_id):
    """Snapshot the current cheapest store/price per item as the plan baseline."""
    uid = auth.current_user_id()
    s = store()
    try:
        if auth.auth_enabled() and not s.owns_list(list_id, uid):
            return jsonify({"error": "forbidden"}), 403
        result = s.plan_list(list_id)
        if result is None:
            return jsonify({"error": "list not found"}), 404
        return jsonify(result)
    finally:
        s.close()


@app.route("/api/lists/<int:list_id>/revaluate")
@auth.login_required
def api_list_revaluate(list_id):
    """Compare each item's planned cheapest store vs now; flag changes."""
    uid = auth.current_user_id()
    s = store()
    try:
        if auth.auth_enabled() and not s.owns_list(list_id, uid):
            return jsonify({"error": "forbidden"}), 403
        result = s.revaluate_list(list_id)
        if result is None:
            return jsonify({"error": "list not found"}), 404
        return jsonify(result)
    finally:
        s.close()


@app.route("/api/dedup/scan", methods=["POST"])
@auth.login_required
def api_dedup_scan():
    """Run a duplicate scan: auto-merge exact matches, queue fuzzy candidates."""
    s = store()
    try:
        return jsonify(s.scan_duplicates())
    finally:
        s.close()


@app.route("/api/dedup/candidates")
@auth.login_required
def api_dedup_candidates():
    s = store()
    try:
        return jsonify(s.merge_candidates())
    finally:
        s.close()


@app.route("/api/dedup/candidates/<int:candidate_id>", methods=["POST"])
@auth.login_required
def api_dedup_resolve(candidate_id):
    body = request.get_json(silent=True) or {}
    approve = bool(body.get("approve"))
    into = body.get("into")
    s = store()
    try:
        res = s.resolve_candidate(candidate_id, approve, into=into)
        if res is None:
            return jsonify({"error": "candidate not found"}), 404
        return jsonify(res)
    finally:
        s.close()


@app.route("/api/sync", methods=["POST"])
@auth.login_required
def api_sync():
    import sync_snapshot, os as _os
    repo = (request.get_json(silent=True) or {}).get("repo", "labrouss/pricemonitor")
    s = store()
    try:
        res = sync_snapshot.run(s, repo=repo,
                                token=_os.environ.get("GITHUB_TOKEN"))
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    finally:
        s.close()


@app.route("/api/dedup/export.csv")
@auth.login_required
def api_dedup_export():
    import csv, io
    status = request.args.get("status", "pending")
    if status == "all":
        status = None
    s = store()
    try:
        rows = s.export_candidates(status=status)
    finally:
        s.close()
    cols = ["candidate_id", "score", "status", "decision",
            "a_id", "a_name", "a_brand", "a_category", "a_retailers",
            "b_id", "b_name", "b_brand", "b_category", "b_retailers"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    csv_data = buf.getvalue()
    return Response(
        csv_data, mimetype="text/csv",
        headers={"Content-Disposition":
                 "attachment; filename=merge_candidates.csv"})


@app.route("/api/dedup/import", methods=["POST"])
@auth.login_required
def api_dedup_import():
    import csv, io
    # Accept either an uploaded file ('file') or raw CSV text in the body.
    raw = None
    if "file" in request.files:
        raw = request.files["file"].read().decode("utf-8-sig")
    elif request.data:
        raw = request.data.decode("utf-8-sig")
    if not raw:
        return jsonify({"error": "no CSV provided"}), 400
    reader = csv.DictReader(io.StringIO(raw))
    decisions = []
    for row in reader:
        cid = row.get("candidate_id") or row.get("id")
        if not cid:
            continue
        try:
            cid = int(cid)
        except (ValueError, TypeError):
            continue
        decisions.append({"candidate_id": cid,
                          "decision": row.get("decision", "")})
    s = store()
    try:
        return jsonify(s.import_candidate_decisions(decisions))
    finally:
        s.close()


@app.route("/api/dedup/prune", methods=["POST"])
@auth.login_required
def api_dedup_prune():
    s = store()
    try:
        return jsonify(s.prune_stale_candidates())
    finally:
        s.close()


@app.route("/api/dedup/merge-all", methods=["POST"])
@auth.login_required
def api_dedup_merge_all():
    body = request.get_json(silent=True) or {}
    threshold = float(body.get("threshold", 0.95))
    threshold = max(0.80, min(threshold, 1.0))   # never below 0.80 via this route
    s = store()
    try:
        return jsonify(s.merge_all_above(threshold))
    finally:
        s.close()


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/shop")
def grocery_app():
    """Serve the grocery shopping-list app (uses live prices from this server)."""
    path = os.path.join(os.path.dirname(__file__), "grocery_list.html")
    if os.path.exists(path):
        return Response(open(path, encoding="utf-8").read(), mimetype="text/html")
    return Response("<h1>grocery_list.html not found</h1>", mimetype="text/html")


# The single-page UI is served inline to keep this a one-file app.
INDEX_HTML = open(
    os.path.join(os.path.dirname(__file__), "webapp_ui.html"),
    encoding="utf-8").read() if os.path.exists(
    os.path.join(os.path.dirname(__file__), "webapp_ui.html")) else "<h1>UI file missing</h1>"


if __name__ == "__main__":
    # Bind to localhost only — this is a local research tool, not a public site.
    print(f"Serving price monitor UI at http://127.0.0.1:5000  (db: {DB_PATH})")
    app.run(host="127.0.0.1", port=5000, debug=False)
