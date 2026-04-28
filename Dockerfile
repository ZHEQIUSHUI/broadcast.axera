FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS dist-builder

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


FROM node:22-slim AS webssh2-builder

WORKDIR /src/webssh2

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        make \
        g++ \
    && rm -rf /var/lib/apt/lists/*

COPY third_party/webssh2/package.json third_party/webssh2/package-lock.json ./
COPY third_party/webssh2/scripts ./scripts
RUN npm ci

COPY third_party/webssh2 ./
RUN npm run build && npm prune --omit=dev


FROM node:22-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        python3 \
        python3-venv \
        iputils-ping \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && pip install --no-cache-dir -r requirements.txt

COPY dashboard.py ./
COPY templates ./templates
COPY assets ./assets
COPY docker ./docker
COPY install.sh install_dashboard_service.sh build.sh device_broadcast.cpp ./
COPY dashboard.service device_monitor.service S90device_broadcast ./
COPY --from=dist-builder /src/dist ./dist

COPY --from=webssh2-builder /src/webssh2/dist /opt/webssh2/dist
COPY --from=webssh2-builder /src/webssh2/node_modules /opt/webssh2/node_modules
COPY --from=webssh2-builder /src/webssh2/package.json /opt/webssh2/package.json

RUN chmod +x /app/docker/start.sh

RUN mkdir -p /app/.runtime

EXPOSE 25000/tcp
EXPOSE 9999/udp
EXPOSE 2222/tcp

VOLUME ["/app/.runtime"]

ENTRYPOINT ["tini", "--"]
CMD ["/app/docker/start.sh"]
