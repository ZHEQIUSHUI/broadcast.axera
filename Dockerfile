FROM debian:bookworm-slim AS dist-builder

WORKDIR /src

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        g++ \
        g++-aarch64-linux-gnu \
        g++-arm-linux-gnueabihf \
    && rm -rf /var/lib/apt/lists/*

COPY device_broadcast.cpp build.sh ./
RUN chmod +x build.sh && ./build.sh


FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        iputils-ping \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY dashboard.py ./
COPY templates ./templates
COPY assets ./assets
COPY install.sh install_dashboard_service.sh build.sh device_broadcast.cpp ./
COPY dashboard.service device_monitor.service S90device_broadcast ./
COPY --from=dist-builder /src/dist ./dist

RUN mkdir -p /app/.runtime

EXPOSE 25000/tcp
EXPOSE 9999/udp

VOLUME ["/app/.runtime"]

ENTRYPOINT ["tini", "--"]
CMD ["python", "dashboard.py"]
