#!/usr/bin/env python3
"""
Diagnose whether GITHUB_TOKEN can create releases on GITHUB_REPO.
Run: docker exec -ti -e GITHUB_TOKEN=... -e GITHUB_REPO=labrouss/pricemonitor \
        price_monitor-worker-1 python3 check_token.py
"""
import os
import json
import urllib.request
import urllib.error

t = os.environ.get("GITHUB_TOKEN")
repo = os.environ.get("GITHUB_REPO", "labrouss/pricemonitor")
if not t:
    raise SystemExit("set GITHUB_TOKEN")

h = {"Authorization": f"Bearer {t}",
     "Accept": "application/vnd.github+json",
     "User-Agent": "token-check",
     "X-GitHub-Api-Version": "2022-11-28"}


def get(url):
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace")


# 1. Token type + classic-scope check (fine-grained tokens report no scopes)
st, hdr, who = get("https://api.github.com/user")
if st == 200:
    print(f"token authenticates as: {who.get('login')}")
else:
    print(f"token /user check failed: HTTP {st} — {who}")
scopes = hdr.get("X-OAuth-Scopes")
print(f"classic-token scopes: {scopes if scopes else '(none — likely a fine-grained token)'}")

# 2. Repo access + reported permissions
st, hdr, d = get(f"https://api.github.com/repos/{repo}")
if st != 200:
    print(f"cannot access repo {repo}: HTTP {st} — {d}")
    print(">>> The token can't even see the repo. Fix repository ACCESS first.")
    raise SystemExit(1)
print(f"repo: {d['full_name']} | private: {d['private']}")
perms = d.get("permissions", {})
print(f"reported permissions: {perms}")
if perms.get("push") or perms.get("admin"):
    print(">>> Token HAS write access to repo contents — release creation should work.")
    print(">>> If it still 403s, the fine-grained token is missing the 'Contents: "
          "Read and write' PERMISSION specifically (repo access alone isn't enough).")
else:
    print(">>> Token is READ-ONLY on this repo. That's the 403 cause.")
    print(">>> Fix: fine-grained token -> Permissions -> Contents -> Read and write.")
    print(">>>      classic token -> check the full 'repo' scope.")
