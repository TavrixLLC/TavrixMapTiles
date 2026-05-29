FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG TIPPECANOE_REF=main
ARG PMTILES_REF=v1.30.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        g++ \
        gdal-bin \
        git \
        golang-go \
        jq \
        libsqlite3-dev \
        make \
        pkg-config \
        postgresql-client \
        python3 \
        python3-pip \
        python3-venv \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${TIPPECANOE_REF}" https://github.com/felt/tippecanoe.git /tmp/tippecanoe \
    && make -C /tmp/tippecanoe -j"$(nproc)" \
    && make -C /tmp/tippecanoe install \
    && rm -rf /tmp/tippecanoe

RUN git clone --depth 1 --branch "${PMTILES_REF}" https://github.com/protomaps/go-pmtiles.git /tmp/go-pmtiles \
    && cd /tmp/go-pmtiles \
    && go build -o /usr/local/bin/pmtiles . \
    && rm -rf /tmp/go-pmtiles /root/go

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --break-system-packages --no-cache-dir -r /app/requirements.txt
RUN ln -sf /usr/bin/python3 /usr/local/bin/python

COPY config /app/config
COPY scripts /app/scripts
COPY api /app/api

RUN mkdir -p /app/output /app/tmp /app/logs

ENV CONFIG_DIR=/app/config \
    OUTPUT_DIR=/app/output \
    TMP_DIR=/app/tmp \
    LOG_DIR=/app/logs \
    PYTHONUNBUFFERED=1

CMD ["python3", "scripts/build.py", "--help"]
