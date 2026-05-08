FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS dist-builder

WORKDIR /src

ARG DOWNLOAD_TOOLCHAINS=0
ARG TOOLCHAIN_AARCH64_URL="https://developer.arm.com/-/media/Files/downloads/gnu-a/9.2-2019.12/binrel/gcc-arm-9.2-2019.12-x86_64-aarch64-none-linux-gnu.tar.xz"
ARG TOOLCHAIN_ARMV7_URL="https://releases.linaro.org/components/toolchain/binaries/7.5-2019.12/arm-linux-gnueabihf/gcc-linaro-7.5.0-2019.12-x86_64_arm-linux-gnueabihf.tar.xz"
ARG TOOLCHAIN_RISCV64_URL="https://github.com/ZHEQIUSHUI/assets/releases/download/risc-v/gcc-14.3-riscv64-unknown-linux-gnu-2.39.tar.xz"
ARG TOOLCHAIN_AX620E_UCLIBC_URL="https://github.com/AXERA-TECH/ax620q_bsp_sdk/releases/download/v2.0.0/arm-AX620E-linux-uclibcgnueabihf_V3_20240320.tgz"

RUN echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        g++ \
        tar \
        wget \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "${DOWNLOAD_TOOLCHAINS}" = "1" ]; then \
        set -eu; \
        echo "[dist-builder] downloading extra cross toolchains..."; \
        wget -O /tmp/gcc-arm-aarch64.tar.xz "${TOOLCHAIN_AARCH64_URL}"; \
        tar -C /opt -xJf /tmp/gcc-arm-aarch64.tar.xz; \
        rm -f /tmp/gcc-arm-aarch64.tar.xz; \
        wget -O /tmp/gcc-linaro-armv7.tar.xz "${TOOLCHAIN_ARMV7_URL}"; \
        tar -C /opt -xJf /tmp/gcc-linaro-armv7.tar.xz; \
        rm -f /tmp/gcc-linaro-armv7.tar.xz; \
        wget -O /tmp/gcc-riscv64.tar.xz "${TOOLCHAIN_RISCV64_URL}"; \
        tar -C /opt -xJf /tmp/gcc-riscv64.tar.xz; \
        rm -f /tmp/gcc-riscv64.tar.xz; \
        wget -O /tmp/ax620e-uclibc.tgz "${TOOLCHAIN_AX620E_UCLIBC_URL}"; \
        tar -C /opt -xzf /tmp/ax620e-uclibc.tgz; \
        rm -f /tmp/ax620e-uclibc.tgz; \
    else \
        set -eu; \
        echo "[dist-builder] using Debian cross compilers"; \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            g++-aarch64-linux-gnu \
            g++-arm-linux-gnueabihf; \
        rm -rf /var/lib/apt/lists/*; \
    fi

COPY device_broadcast.cpp build.sh ./
RUN chmod +x build.sh && ./build.sh


FROM node:22-slim AS webssh2-builder

WORKDIR /src/webssh2

RUN echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4

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

RUN echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4

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
