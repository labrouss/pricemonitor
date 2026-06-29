#!/usr/bin/env bash
# Publish the latest-prices snapshot to a GitHub Release, so users can fetch a
# stable URL. Uses a moving "latest" tag (re-pointed each run) plus a dated
# release for history.
#
# Requires: GITHUB_TOKEN (fine-grained PAT, contents:write on the repo),
#           GITHUB_REPO  (e.g. "kostas/price-monitor"),
#           a snapshot dir produced by export_snapshot.py.
#
# Usage:
#   GITHUB_TOKEN=... GITHUB_REPO=you/price-monitor ./publish_snapshot.sh ./snapshot
#
# The "latest" release is force-updated; a dated tag (data-YYYYMMDD) is also made
# so older snapshots remain downloadable.

set -euo pipefail

SNAP_DIR="${1:-snapshot}"
: "${GITHUB_TOKEN:?set GITHUB_TOKEN (fine-grained PAT, contents:write)}"
: "${GITHUB_REPO:?set GITHUB_REPO, e.g. you/price-monitor}"

API="https://api.github.com/repos/${GITHUB_REPO}"
UPLOADS="https://uploads.github.com/repos/${GITHUB_REPO}"
AUTH=(-H "Authorization: Bearer ${GITHUB_TOKEN}" -H "Accept: application/vnd.github+json")

date_tag="data-$(date +%Y%m%d)"

publish_one () {
    local tag="$1" name="$2" prerelease="$3"
    # Delete an existing release+tag with this name so we can recreate cleanly
    # (lets the "latest" tag move to the new assets).
    local rid
    rid="$(curl -s "${AUTH[@]}" "${API}/releases/tags/${tag}" | grep -o '"id": *[0-9]*' | head -1 | grep -o '[0-9]*' || true)"
    if [ -n "${rid:-}" ]; then
        echo "[publish] removing old release ${tag} (id ${rid})"
        curl -s -X DELETE "${AUTH[@]}" "${API}/releases/${rid}" >/dev/null || true
        curl -s -X DELETE "${AUTH[@]}" "${API}/git/refs/tags/${tag}" >/dev/null || true
    fi
    # Create the release.
    local body
    body="$(cat "${SNAP_DIR}/manifest.json" 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);print(f"Snapshot generated {d[\"generated_at\"]} — {d[\"rows\"]} rows, {d[\"products\"]} products across {len(d[\"retailers\"])} chains.")' 2>/dev/null || echo "Latest price snapshot.")"
    local payload
    payload="$(python3 -c "import json,sys; print(json.dumps({'tag_name':'${tag}','name':'${name}','body':sys.argv[1],'prerelease':${prerelease}}))" "$body")"
    local resp upload_url
    resp="$(curl -s -X POST "${AUTH[@]}" "${API}/releases" -d "${payload}")"
    rid="$(echo "$resp" | grep -o '"id": *[0-9]*' | head -1 | grep -o '[0-9]*')"
    if [ -z "${rid:-}" ]; then
        echo "[publish] ERROR creating release ${tag}:"; echo "$resp" | head -20; exit 1
    fi
    # Upload each snapshot asset.
    for f in prices-latest.sqlite prices-latest.json prices-latest.csv manifest.json; do
        local path="${SNAP_DIR}/${f}"
        [ -f "$path" ] || continue
        echo "[publish] uploading ${f} -> ${tag}"
        curl -s -X POST "${AUTH[@]}" \
            -H "Content-Type: application/octet-stream" \
            --data-binary @"${path}" \
            "${UPLOADS}/releases/${rid}/assets?name=${f}" >/dev/null
    done
    echo "[publish] ${tag} done: https://github.com/${GITHUB_REPO}/releases/tag/${tag}"
}

# Moving "latest" (what users fetch), plus a dated snapshot for history.
publish_one "latest" "Latest prices" "false"
publish_one "${date_tag}" "Prices ${date_tag}" "true"

echo "[publish] stable URL for users:"
echo "  https://github.com/${GITHUB_REPO}/releases/latest/download/prices-latest.json"
echo "  https://github.com/${GITHUB_REPO}/releases/latest/download/prices-latest.csv"
echo "  https://github.com/${GITHUB_REPO}/releases/latest/download/prices-latest.sqlite"
