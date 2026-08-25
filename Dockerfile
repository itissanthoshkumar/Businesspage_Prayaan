# Prayaan Business Pages — production image.
# Runs behind the Caddy reverse proxy in deploy/docker-compose.yml, which is
# why --proxy-headers is on: Caddy terminates TLS and sets X-Forwarded-*.
FROM python:3.12-slim

# git: the upload store commits customer photos into its own repo.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Production posture by default; .env can override for a staging build.
ENV PBN_SHOW_SWITCHER=false \
    PBN_PUBLIC_DIR=false \
    PBN_TRUST_PROXY=true

EXPOSE 8797
# --forwarded-allow-ips=* is safe ONLY because this port is never published:
# in the compose network, Caddy is the sole thing that can reach it.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8797", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
