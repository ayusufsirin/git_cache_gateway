FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates git-lfs \
    && mkdir -p /etc/git-cache-gateway/ca \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Build-time CA support:
# Put company/internal CA files in ./ca/*.crt or ./ca/*.pem before `docker compose build`.
# They are installed before pip runs, so pip can trust company TLS interception/proxies.
RUN chmod +x /app/scripts/*.sh \
    && /app/scripts/install-ca-certificates.sh /app/ca \
    && python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir .

EXPOSE 8080
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["git-cache-gateway", "serve"]
