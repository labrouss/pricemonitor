"""
AB 403 diagnostic — figure out WHAT KIND of block this is.

This is a DIAGNOSTIC, not an evasion tool. It gathers facts about the 403 so we
can tell apart:
  (a) a transient / rate-limit block (might clear, or ease with politer crawling)
  (b) a deliberate persistent bot wall (we will NOT try to defeat this)

It deliberately does NOT rotate IPs, forge fingerprints, solve challenges, or
otherwise pretend not to be a bot. The ONE legitimate thing it checks is whether
AB serves their PUBLIC robots.txt / sitemap to us at all — because if a site
blocks even its own published robots.txt, that's an indiscriminate block; if it
serves robots.txt but blocks product pages, the robots policy tells us what
they actually permit.
"""
import time
import requests

BASE = "https://www.ab.gr"
PROD = ("https://www.ab.gr/el/eshop/Kava-anapsyktika-nera-xiroi-karpoi/"
        "Krasia/Leyka-krasia/Oinos-Leykos-Makedonikos-750ml/p/7087788")

UA_BOT = ("PriceMonitorBot/0.1 (+https://example.org/price-monitor; "
          "contact: you@example.org) python-requests")


def probe(label, url, ua, extra=None):
    headers = {"User-Agent": ua}
    if extra:
        headers.update(extra)
    try:
        r = requests.get(url, headers=headers, timeout=20)
        body = r.text[:200].replace("\n", " ")
        server = r.headers.get("Server", "?")
        waf = {k: v for k, v in r.headers.items()
               if k.lower() in ("server", "x-cache", "cf-ray", "x-akamai-transformed",
                                "x-iinfo", "set-cookie")}
        print(f"[{label}] {r.status_code}  server={server}")
        print(f"        waf-ish headers: {waf}")
        print(f"        body: {body!r}")
        return r.status_code
    except Exception as e:
        print(f"[{label}] ERROR {e}")
        return None


print("=" * 64)
print("AB 403 DIAGNOSIS — facts only, no evasion")
print("=" * 64)

# 1) Does AB serve its PUBLIC robots.txt to our honest bot UA?
print("\n1) robots.txt with our honest bot UA:")
probe("robots/bot", BASE + "/robots.txt", UA_BOT)

# 2) Does AB serve the homepage to our bot UA?
print("\n2) homepage with our bot UA:")
probe("home/bot", BASE + "/", UA_BOT)

# 3) The product page with our bot UA (the failing case)
print("\n3) product page with our bot UA (the failing request):")
probe("product/bot", PROD, UA_BOT)

# 4) Is it rate-limit shaped? Try the product page 3x slowly and see if the
#    status changes (a steady 403 = policy; intermittent = rate/transient).
print("\n4) product page, 3 slow attempts (steady 403 = policy block):")
for i in range(3):
    probe(f"product/try{i+1}", PROD, UA_BOT)
    time.sleep(5)

print("\n" + "=" * 64)
print("HOW TO READ THIS:")
print("  - robots.txt 200 but product 403  -> they serve their policy but block")
print("    product scraping. Check what robots.txt actually allows (below).")
print("  - everything 403 incl. robots     -> indiscriminate IP/UA block.")
print("  - product 403 steady across tries -> deliberate policy, NOT transient.")
print("  - product flips 200/403           -> rate-limit/transient; politer")
print("    crawling (slower, fewer workers) may be enough — legitimately.")
print("=" * 64)

# Show what robots.txt actually permits, since that's the site's stated policy.
print("\nAB robots.txt (their stated crawling policy):")
try:
    r = requests.get(BASE + "/robots.txt", headers={"User-Agent": UA_BOT}, timeout=20)
    if r.status_code == 200:
        print(r.text[:1500])
    else:
        print(f"  (robots.txt returned {r.status_code})")
except Exception as e:
    print("  error:", e)
