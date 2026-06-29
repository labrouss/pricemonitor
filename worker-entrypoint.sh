#!/bin/bash
# Worker entrypoint: run scheduled scrapers via cron.
#
# cron does NOT inherit the container's environment, so DB_BACKEND and
# DATABASE_URL (set by docker-compose) would be invisible to scheduled jobs.
# We export them into a file that the crontab sources, then start cron in the
# foreground so the container stays alive and logs stream to `docker logs`.

set -e

mkdir -p /var/log/cron

# cron jobs do NOT inherit the container environment, and standard Debian cron
# does not reliably source /etc/environment for job execution. The bulletproof
# way is to prepend the needed VAR=value lines to the crontab itself (cron DOES
# honour those). We build a runtime crontab = env lines + the project crontab.
RUNTIME_CRON=/tmp/runtime.crontab
{
  # Export the vars cron jobs need. SHELL/PATH ensure flock & python resolve.
  echo "SHELL=/bin/bash"
  echo "PATH=/usr/local/bin:/usr/bin:/bin"
  for v in DB_BACKEND DATABASE_URL TZ; do
    val="$(printenv "$v" || true)"
    [ -n "$val" ] && echo "$v=$val"
  done
  echo ""
  cat /app/crontab
} > "$RUNTIME_CRON"

# Also persist to /etc/environment as a fallback for interactive shells.
printenv | grep -E '^(DB_BACKEND|DATABASE_URL|TZ)' > /etc/environment || true

# Install the assembled crontab.
crontab "$RUNTIME_CRON"

echo "[worker] cron scheduler started. Environment for jobs:"
grep -E '^(DB_BACKEND|DATABASE_URL|TZ)=' "$RUNTIME_CRON" || true
echo "[worker] scheduled jobs:"
grep -vE '^\s*#|^\s*$|^[A-Z_]+=' "$RUNTIME_CRON" || true
echo "[worker] logs appear under /var/log/cron/ and in docker logs."

# Stream cron logs to stdout so `docker logs` shows scraper output, and run
# cron in the foreground so the container stays alive.
for r in posokanei sklavenitis mymarket bazaar lidl dedup backup publish; do
    touch "/var/log/cron/$r.log"
done
tail -F /var/log/cron/*.log &

cron -f
