#!/usr/bin/env python3
"""
Publish the latest-prices snapshot to a GitHub Release using only the Python
standard library (no curl needed). Creates/updates a moving "latest" release
plus a dated one, and uploads the snapshot files as assets.

Requires env:
  GITHUB_TOKEN   fine-grained PAT with contents:write on the repo
  GITHUB_REPO    e.g. "labrouss/pricemonitor"

Usage:
  python3 publish_snapshot.py [SNAP_DIR]        # default SNAP_DIR=snapshot
"""

import os
import sys
import json
import datetime
import urllib.request
import urllib.error

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"

ASSETS = ["prices-latest.sqlite", "prices-latest.json",
          "prices-latest.csv", "manifest.json"]


def _req(method, url, token, data=None, headers=None, raw=False):
    h = {"Authorization": f"Bearer {token}",
         "Accept": "application/vnd.github+json",
         "User-Agent": "price-monitor-publish",
         "X-GitHub-Api-Version": "2022-11-28"}
    if headers:
        h.update(headers)
    body = data if raw else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = resp.read()
            return resp.status, (json.loads(payload) if payload and not raw
                                 and resp.headers.get("Content-Type", "").startswith("application/json")
                                 else payload)
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def publish_one(repo, token, snap_dir, tag, name, prerelease, body):
    # Delete an existing release with this tag so "latest" can move cleanly.
    status, rel = _req("GET", f"{API}/repos/{repo}/releases/tags/{tag}", token)
    if status == 200 and isinstance(rel, dict) and rel.get("id"):
        rid = rel["id"]
        print(f"[publish] removing old release {tag} (id {rid})")
        _req("DELETE", f"{API}/repos/{repo}/releases/{rid}", token)
        _req("DELETE", f"{API}/repos/{repo}/git/refs/tags/{tag}", token)

    # Create the release.
    status, rel = _req("POST", f"{API}/repos/{repo}/releases", token, data={
        "tag_name": tag, "name": name, "body": body,
        "prerelease": bool(prerelease), "make_latest": "true" if tag == "latest" else "false",
    })
    if status not in (200, 201) or not isinstance(rel, dict) or not rel.get("id"):
        print(f"[publish] ERROR creating release {tag}: HTTP {status}")
        print((rel[:500] if isinstance(rel, (bytes, bytearray)) else rel))
        return False
    rid = rel["id"]

    # Upload each asset.
    for fname in ASSETS:
        path = os.path.join(snap_dir, fname)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        url = f"{UPLOADS}/repos/{repo}/releases/{rid}/assets?name={fname}"
        st, _ = _req("POST", url, token, data=data, raw=True,
                     headers={"Content-Type": "application/octet-stream"})
        print(f"[publish] uploaded {fname} -> {tag} (HTTP {st})")
    print(f"[publish] {tag} done: https://github.com/{repo}/releases/tag/{tag}")
    return True


def main():
    snap_dir = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        print("[publish] GITHUB_TOKEN and GITHUB_REPO must be set."); return 1

    # Build a human body from the manifest if present.
    body = "Latest price snapshot."
    mpath = os.path.join(snap_dir, "manifest.json")
    if os.path.isfile(mpath):
        try:
            m = json.load(open(mpath, encoding="utf-8"))
            body = (f"Snapshot generated {m.get('generated_at')} — "
                    f"{m.get('rows')} rows, {m.get('products')} products across "
                    f"{len(m.get('retailers', []))} chains: "
                    f"{', '.join(m.get('retailers', []))}.")
        except Exception:
            pass

    date_tag = "data-" + datetime.datetime.now().strftime("%Y%m%d")
    ok1 = publish_one(repo, token, snap_dir, "latest", "Latest prices", False, body)
    ok2 = publish_one(repo, token, snap_dir, date_tag, f"Prices {date_tag}", True, body)

    if ok1:
        print("[publish] stable URLs for users:")
        for a in ("prices-latest.json", "prices-latest.csv", "prices-latest.sqlite"):
            print(f"  https://github.com/{repo}/releases/latest/download/{a}")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
