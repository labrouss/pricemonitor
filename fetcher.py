"""
Polite HTTP fetcher: respects robots.txt, honors crawl-delay, rate-limits,
and retries with exponential backoff. This is the foundation every scraper
in this project must go through — it is the layer that keeps us a "good
citizen" against the target site.
"""

import time
import logging
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import requests

logger = logging.getLogger(__name__)

# Identify the bot honestly. Put a real contact URL/email here so the site
# owner can reach you (or block you cleanly) rather than treating you as a
# malicious anonymous scraper. Being identifiable is part of scraping ethically.
USER_AGENT = (
    "PriceMonitorBot/0.1 (+https://example.org/price-monitor; "
    "contact: you@example.org) python-requests"
)

DEFAULT_CRAWL_DELAY = 3.0  # seconds between requests if robots.txt doesn't specify


class PoliteFetcher:
    def __init__(self, base_url, user_agent=USER_AGENT,
                 min_delay=DEFAULT_CRAWL_DELAY, timeout=(10, 60), max_retries=3):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        # timeout is (connect_timeout, read_timeout). Large sitemap XML files
        # can take a while to download, so the read budget is generous.
        self.timeout = timeout
        self.max_retries = max_retries

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
        })

        # Load and parse robots.txt
        self._rp = RobotFileParser()
        robots_url = urljoin(self.base_url + "/", "robots.txt")
        self._robots_url = robots_url
        self._last_request_ts = 0.0
        self._load_robots(robots_url, min_delay)

    def _load_robots(self, robots_url, min_delay):
        try:
            resp = self.session.get(robots_url, timeout=self.timeout)
            if resp.status_code == 200:
                self._rp.parse(resp.text.splitlines())
                logger.info("Loaded robots.txt from %s", robots_url)
            else:
                # No robots.txt (404 etc.) — by convention everything is allowed,
                # but we stay polite with our default delay regardless.
                logger.warning("robots.txt returned %s; assuming allow-all",
                               resp.status_code)
                self._rp = None
        except requests.RequestException as e:
            logger.warning("Could not fetch robots.txt (%s); assuming allow-all", e)
            self._rp = None

        # Respect crawl-delay from robots.txt if present, else our floor.
        crawl_delay = None
        if self._rp is not None:
            try:
                crawl_delay = self._rp.crawl_delay(self.user_agent)
            except Exception:
                crawl_delay = None
        self.delay = max(min_delay, crawl_delay or 0)
        logger.info("Effective crawl delay: %.1fs", self.delay)

    def can_fetch(self, url):
        """Return True if robots.txt permits fetching this URL for our UA."""
        if self._rp is None:
            return True
        return self._rp.can_fetch(self.user_agent, url)

    def get(self, url):
        """
        Fetch a URL politely. Returns a requests.Response or None.
        Refuses (returns None) if robots.txt disallows the path.
        """
        if not url.startswith("http"):
            url = urljoin(self.base_url + "/", url.lstrip("/"))

        if not self.can_fetch(url):
            logger.warning("robots.txt disallows %s — skipping", url)
            return None

        # Rate limit: ensure at least self.delay between requests
        elapsed = time.time() - self._last_request_ts
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

        backoff = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout, stream=True)
                self._last_request_ts = time.time()

                if resp.status_code == 200:
                    # Read the full body within the read-timeout budget.
                    # iter_content keeps the connection progressing rather than
                    # blocking on one giant read that trips the timeout.
                    chunks = []
                    total = 0
                    MAX_BYTES = 50 * 1024 * 1024  # 50 MB safety cap
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            chunks.append(chunk)
                            total += len(chunk)
                            if total > MAX_BYTES:
                                logger.warning("Response from %s exceeded %d bytes; truncating",
                                               url, MAX_BYTES)
                                break
                    resp._content = b"".join(chunks)
                    resp._content_consumed = True
                    return resp
                if resp.status_code == 429 or resp.status_code >= 500:
                    # Server is asking us to slow down / is struggling — back off.
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                    logger.warning("HTTP %s on %s; backing off %.1fs (attempt %d/%d)",
                                   resp.status_code, url, wait, attempt, self.max_retries)
                    time.sleep(wait)
                    backoff *= 2
                    continue
                # Other 4xx — not retryable
                logger.warning("HTTP %s on %s — not retrying", resp.status_code, url)
                return None
            except requests.RequestException as e:
                logger.warning("Request error on %s: %s (attempt %d/%d)",
                               url, e, attempt, self.max_retries)
                time.sleep(backoff)
                backoff *= 2

        logger.error("Giving up on %s after %d attempts", url, self.max_retries)
        return None

    def get_sitemaps(self):
        """Return sitemap URLs advertised in robots.txt, if any."""
        sitemaps = []
        if self._rp is not None:
            try:
                sm = self._rp.site_maps()
                if sm:
                    sitemaps = list(sm)
            except Exception:
                pass
        return sitemaps

    def get_many(self, urls, workers=4, pace=0.75):
        """
        Fetch many URLs concurrently but politely.

        - `workers` parallel threads (keep small, 3-5, to stay courteous).
        - `pace` is the minimum seconds between request STARTS across ALL
          threads, enforced by a shared lock. So the overall request rate is
          ~1/pace per second regardless of worker count. With workers=4 and
          pace=0.75 you get ~1.3 req/s — far faster than the 3s serial floor,
          but still gentle (comparable to a browser loading a page's assets).

        Only use this where robots.txt imposes no crawl-delay. If a crawl-delay
        is set, respect it instead (use serial get()).

        Yields (url, response_or_None) as results complete.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # If the site declared a crawl-delay, do NOT speed up — honor it.
        effective_pace = max(pace, self.delay if self.delay else 0)
        # But if self.delay is the default floor (3s) and the caller explicitly
        # asked to go faster, only override when robots set no real delay.
        if self._rp is not None:
            cd = None
            try:
                cd = self._rp.crawl_delay(self.user_agent)
            except Exception:
                cd = None
            if cd:
                effective_pace = max(effective_pace, float(cd))
            else:
                effective_pace = pace  # no robots crawl-delay -> use caller pace

        pace_lock = threading.Lock()
        next_slot = [0.0]

        def paced_get(url):
            # Reserve a time slot so request starts are spaced by effective_pace
            with pace_lock:
                now = time.time()
                start_at = max(now, next_slot[0])
                next_slot[0] = start_at + effective_pace
            wait = start_at - time.time()
            if wait > 0:
                time.sleep(wait)
            if not self.can_fetch(url):
                return url, None
            # Use a per-thread-safe simple request (bypass serial get()'s own
            # delay bookkeeping, since pacing is handled here).
            backoff = 2.0
            for attempt in range(1, self.max_retries + 1):
                try:
                    resp = self.session.get(url, timeout=self.timeout, stream=True)
                    if resp.status_code == 200:
                        chunks, total = [], 0
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                chunks.append(chunk)
                                total += len(chunk)
                                if total > 50 * 1024 * 1024:
                                    break
                        resp._content = b"".join(chunks)
                        resp._content_consumed = True
                        return url, resp
                    if resp.status_code == 429 or resp.status_code >= 500:
                        time.sleep(backoff)
                        backoff *= 2
                        continue
                    return url, None
                except requests.RequestException:
                    time.sleep(backoff)
                    backoff *= 2
            return url, None

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(paced_get, u): u for u in urls}
            for fut in as_completed(futures):
                yield fut.result()
