FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates git-lfs wget\
    && rm -rf /var/lib/apt/lists/*

RUN wget --no-check-certificate https://gitlabce01.aselsan.com.tr/-/snippets/15/raw/main/AselsanCA.crt?inline=false -O AselsanCA.crt
RUN wget --no-check-certificate https://gitlabce01.aselsan.com.tr/-/snippets/15/raw/main/AselsanInternetCA.crt?inline=false -O AselsanInternetCA.crt
RUN cp AselsanCA.crt /usr/local/share/ca-certificates/
RUN cp AselsanInternetCA.crt /usr/local/share/ca-certificates/
RUN rm AselsanCA.crt AselsanInternetCA.crt
RUN update-ca-certificates

RUN cat <<EOT >> /etc/pip.conf
[global]
use-feature = truststore
EOT

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

EXPOSE 8080
CMD ["git-cache-gateway", "serve"]
