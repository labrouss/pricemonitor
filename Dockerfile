FROM python:3.12-slim

# System deps occasionally needed by lxml/psycopg builds (binary wheels usually
# cover it, but keep build-essential available for safety on odd platforms).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        postgresql-client \
        curl \
        cron \
        tzdata \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/worker-entrypoint.sh

ENV DB_BACKEND=postgres
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# Default: run the web app with a production server (waitress).
CMD ["python", "-m", "waitress", "--listen=0.0.0.0:5000", "webapp:app"]
