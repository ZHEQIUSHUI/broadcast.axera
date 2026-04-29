# 通用设备广播与监控面板

这个项目现在不再只面向 AX 设备，而是一个通用的 Linux 设备广播与管理方案：

- 自动识别 `AX / Raspberry Pi / x86 / 其他 ARM Linux`
- 自动上报 `OS 发行版 / 内核 / libc / 架构 / 用户 / 机型`
- 支持 `CPU / 内存 / 显存` 指标
- AX 设备额外支持 `UID / version / board_id / chip_type / CMM`（其中 `chip_type` 来自 `/proc/ax_proc/chip_type`）
- 仪表盘支持动态设备卡片、型号筛选、设备详情页、网页 SSH 终端
- 支持多办公室聚合：可添加其他 Dashboard 作为远程源，设备卡片预览位置，并支持位置筛选
- SSH 终端支持浏览器本地保存密码，下次可直接进入
- 支持网页批量更新：可选 `SSH / Telnet / 自动 SSH->Telnet 回退`
- 支持远程安装网段扫描：可输入 `10.126.35.x / 10.126.35.0/24` 自动遍历 `1-255`
- 批量更新最终统一调用 `install.sh`，继续兼容 `systemd / init.d(S90) / rc.local / crontab / nohup`

## 页面预览

### 主页，设备筛选 / 信息卡片

![主页截图](assets/image_1.jpg)

- 页面顶部会汇总在线设备、平均 CPU、高 CPU 设备数、GPU / AX 设备数。
- 型号筛选支持按设备类型快速过滤，例如 `AX620Q / AX630C / AX650 / x86`。
- 设备信息卡片会展示在线状态、IP、默认用户、架构、运行时长，以及 `CPU / 内存 / GPU / AX CMM` 指标；AX 设备还会展示 `version / chip_type / board_id` 等信息。
- 每张卡片都可以直接进入 `详情`、`终端`、`复制 IP`，并参与批量更新或远程安装。

### 批量安装 / 更新

![批量更新截图](assets/image_2.jpg)

- 批量更新支持三种目标来源同时混用：在线已选设备、手动输入 IP / 主机、网段扫描。
- 网段扫描支持 `10.126.35.x`、`10.126.35.*`、`10.126.35.0/24`、`10.126.35` 这几种写法。
- Dashboard 会先并发 `ping + SSH/Telnet 端口探测`，只在结果区展示探测到的机器，并显示扫描进度。
- 登录成功后会自动判断目标机版本：未安装则安装，版本过旧则更新，已是最新版本则跳过。
- 支持 `SSH`、`Telnet`、`自动 SSH 后 Telnet`，也支持多组账号密码按顺序重试。

### 设备详细信息页

![设备详细信息页](assets/image_detail.jpg)

- 详情页会集中展示设备的主机名、IP、系统版本、系统 ID、架构、内核、libc、厂商、机型、在线时间等信息。
- 页面顶部保留关键运行指标摘要，便于在查看元信息时继续判断设备状态。
- AX 设备会额外展示 `UID / version / chip_type / board_id / CMM` 等信息。
- 支持直接编辑设备标题和备注，适合记录机位、负责人、用途、网络说明等内容。
- 标题和备注保存在当前 Dashboard 主机本地，服务重启后仍会保留。

### SSH：网页版 SSH（webssh2）

![网页版 SSH](assets/image_ssh.jpg)

- SSH 页面默认通过 `webssh2` 新窗口打开，Dashboard 负责生成目标地址，不再在当前页直接承载 SSH 交互。
- `Telnet` 仍然使用 Dashboard 当前页面内置终端，适合没有 SSH 的设备。
- 如果你是直接运行 `python3 dashboard.py`（非 Docker），需要单独安装并运行一个 `webssh2` 服务；如果使用本项目的 Docker 镜像，则镜像已内置并默认启动一个 `webssh2`（监听 `2222`）。
- 本项目默认使用 `http://{dashboard_host}:2222/ssh/host/{host}` 作为跳转模板，可通过环境变量 `WEBSSH2_URL_TEMPLATE` 改掉。
- 如果暂时没有部署 `webssh2`，可把环境变量 `WEBSSH2_ENABLED=0`，SSH 会回退到当前页面内置终端。

## 依赖

```bash
python3 -m pip install -r requirements.txt
```

## 编译

### 一次性编译所有本机支持的版本

```bash
./build.sh
```

编译产物会输出到 `dist/`，脚本会自动扫描 `PATH` 和常见工具链目录中的 `gcc/g++` 及交叉编译器，并自动匹配可编译的目标。

在 CI / Docker 镜像构建中，会在 `dist-builder` 阶段执行一次 `./build.sh`，通常会产出并打包：

- `device_broadcast-x86_64-linux-gnu`
- `device_broadcast-aarch64-linux-gnu`
- `device_broadcast-armv7-linux-gnueabihf`

如需额外的 `device_broadcast-armv7-ax620e-uclibc`，需要在 builder 环境里额外准备 AX620E uclibc 工具链（见下方下载地址）。本仓库的 GitHub Actions Docker 打包工作流已开启 `DOWNLOAD_TOOLCHAINS=1`，会在构建时自动下载这些工具链并尝试产出该文件。

如果只想先看当前机器发现到了哪些编译器：

```bash
./build.sh --list-compilers
```

### 交叉编译器下载（可选，可补进 CI / Docker builder）

`build.sh` 会自动扫描 `PATH` 以及一些常见目录来发现交叉编译器（例如 `/opt/*/bin`、`/home/axera/gcc-arm-*/bin`、`/home/axera/gcc-linaro-*/bin`、`/home/axera/arm-AX620E-linux-uclibcgnueabihf/bin`）。

常用工具链下载地址（x86_64 host）：

- aarch64-none-linux-gnu（glibc）：https://developer.arm.com/-/media/Files/downloads/gnu-a/9.2-2019.12/binrel/gcc-arm-9.2-2019.12-x86_64-aarch64-none-linux-gnu.tar.xz
- arm-linux-gnueabihf（glibc）：https://releases.linaro.org/components/toolchain/binaries/7.5-2019.12/arm-linux-gnueabihf/gcc-linaro-7.5.0-2019.12-x86_64_arm-linux-gnueabihf.tar.xz
- AX620E uclibc（arm-AX620E-linux-uclibcgnueabihf）：https://github.com/AXERA-TECH/ax620q_bsp_sdk/releases/download/v2.0.0/arm-AX620E-linux-uclibcgnueabihf_V3_20240320.tgz

把这些工具链解压到上面这些目录之一后再执行 `./build.sh`，即可让 `dist/` 里多出对应架构的预编译文件；CI/Docker 的 builder 阶段也可以用相同方式下载解压后再执行 `./build.sh`，从而把更多架构产物 bake 进镜像。

如果你希望 `docker build` 时自动下载这些工具链并编译（例如产出 `armv7-ax620e-uclibc`），可用：

```bash
docker build --build-arg DOWNLOAD_TOOLCHAINS=1 -t broadcast-axera-dashboard:local .
```

### 本地单独编译

```bash
g++ -std=c++11 -O2 -Wall -Wextra -o device_broadcast device_broadcast.cpp
```

## 安装设备端 Agent

```bash
sudo ./install.sh
```

安装脚本会自动：

- 获取当前安装用户，作为默认运行用户
- 如果本机能编译，则直接本地编译后安装
- 如果本机不能编译，则按 `arch + libc + device kind` 选择 `dist/` 中最合适的预编译包
- 优先使用 `systemd`
- 其次回退到 `init.d / rc.local / crontab`
- 再不行则自动 `nohup` 常驻

安装完成后会输出：

- 二进制路径
- 启动 runner 路径
- 日志路径
- 当前采用的常驻方式

## 启动 Dashboard

### 直接运行

```bash
python3 dashboard.py
```

默认地址：

```text
http://<dashboard-ip>:25000
```

### Docker 运行（推荐给快速部署）

如果你不想在宿主机安装 Python 依赖，可以直接使用 Docker 镜像运行 Dashboard。

> 注意：设备端 agent 默认向 `255.255.255.255:9999` 广播。为了确保容器能稳定收到 UDP 广播，推荐使用 `--network host`。

本镜像默认会同时启动内置的 WebSSH2（用于网页 SSH 跳转），监听 `0.0.0.0:2222`。如果不想启用，可设置 `WEBSSH2_ENABLED=0`。

本地构建：

```bash
docker build -t broadcast-axera-dashboard:local .
```

从 CI 下载镜像并导入（Actions Artifact / Release 里的 `*.tar.gz`）：

```bash
# x86_64
gzip -dc broadcast-axera-dashboard-*-linux-amd64.tar.gz | docker load

# aarch64
gzip -dc broadcast-axera-dashboard-*-linux-arm64.tar.gz | docker load
```

运行（推荐：host 网络）：

```bash
docker run --rm -it \
  --network host \
  -v "$(pwd)/.runtime:/app/.runtime" \
  -e DASHBOARD_SITE_LABEL="本地" \
  broadcast-axera-dashboard:local
```

如果宿主机的 `2222` 端口已被占用，可改成：

- `-e WEBSSH2_LISTEN_PORT=2223`
- 同时设置 `-e WEBSSH2_URL_TEMPLATE='http://{dashboard_host}:2223/ssh/host/{host}'`

运行（不使用 host 网络，使用端口映射）：

```bash
docker run --rm -it \
  -p 25000:25000 \
  -p 2222:2222 \
  -p 9999:9999/udp \
  -v "$(pwd)/.runtime:/app/.runtime" \
  broadcast-axera-dashboard:local
```

如果使用端口映射模式但收不到设备广播，建议把设备端 agent 改为单播到 Dashboard 宿主机 IP（设置环境变量 `BROADCAST_IP=<dashboard-host-ip>`）。

### 对外接口（设备列表 / Tag 筛选）

Dashboard 启动后，其他人可通过 Dashboard 端口（默认 `25000`）获取设备列表，并通过 tag 进行筛选（tag 规则与页面“型号筛选”一致）。

- `GET /api/devices`：返回全部在线设备列表（含 summary + devices）。
- `POST /api/devices/query`：按 tag 筛选并返回设备列表，同时返回当前可用 tag 及数量。

`/api/devices/query` 请求体（JSON）：

- `tag`：单个 tag（字符串）。
- `tags`：多个 tag（字符串数组，或用逗号/空格分隔的字符串）。
- `include_history`：是否包含历史曲线数据（默认 `false`）。

常用 tag：

- `axera`：所有 AX 设备
- `ax:<model>`：某个具体 AX 型号（例如 `ax:ax620q`）
- `x86`：x86 设备
- `raspberry_pi`：树莓派
- `other`：其他设备

示例：

```bash
# 全量设备
curl -s http://<dashboard-ip>:25000/api/devices

# 只取 AX 设备
curl -s -X POST http://<dashboard-ip>:25000/api/devices/query \
  -H 'Content-Type: application/json' \
  -d '{"tag":"axera"}'

# 同时匹配多个 tag（任意命中即返回）
curl -s -X POST http://<dashboard-ip>:25000/api/devices/query \
  -H 'Content-Type: application/json' \
  -d '{"tags":["ax:ax620q","x86"]}'
```

### 多办公室聚合（远程 Dashboard）

如果每个办公室都启动一套 Dashboard，可以在任意一台 Dashboard 上把其他办公室的设备列表聚合到当前页面显示（远程设备为只读展示，终端 / 群发更新建议在对应办公室的 Dashboard 上操作）。

页面操作：

- 点击“位置筛选”右侧的“远程面板”
- 添加 `服务器（IP:端口）` + `位置标签` + `备注`
- 保存后会自动刷新设备列表；设备卡片会增加 `位置` 预览，并可按位置筛选

说明：

- 远程设备在当前页面为**只读展示**（显示为 `Remote`），终端 / 群发更新建议在对应办公室的 Dashboard 上操作。
- 如果你升级代码后访问 `GET /api/remotes` 仍返回 `404`，说明服务还在跑旧代码：请重启 Dashboard（例如 `sudo systemctl restart dashboard.service`，或重新运行 `python3 dashboard.py`）。

可选环境变量（标记当前 Dashboard 所在位置）：

- `DASHBOARD_SITE_LABEL`：本机位置标签（默认 `本地`）
- `DASHBOARD_SITE_NOTE`：本机备注（可选）

接口补充：

- `GET /api/devices?include_remotes=1`：返回本机 + 已配置远程源的设备列表
- `POST /api/devices/query`：请求体支持 `include_remotes=true`（并会额外生成 `site:*` tag 供筛选）
- 远程源管理：`GET/POST /api/remotes`、`DELETE /api/remotes/<id>`

示例：

```bash
# 聚合展示（本地 + 远程）
curl -s http://<dashboard-ip>:25000/api/devices?include_remotes=1

# 添加一个远程 Dashboard 源（示例：深圳办公室）
curl -s -X POST http://<dashboard-ip>:25000/api/remotes \
  -H 'Content-Type: application/json' \
  -d '{"origin":"10.0.0.12:25000","label":"深圳","note":"A 区机房","enabled":true}'

# 按 tag 筛选（包含远程）
curl -s -X POST http://<dashboard-ip>:25000/api/devices/query \
  -H 'Content-Type: application/json' \
  -d '{"include_remotes":true,"tag":"axera"}'

# 按位置筛选（位置 tag 会以 site:* 形式出现在 tag_summary 中）
curl -s -X POST http://<dashboard-ip>:25000/api/devices/query \
  -H 'Content-Type: application/json' \
  -d '{"include_remotes":true,"tag":"site:深圳"}'
```

如果你是直接运行 `python3 dashboard.py`（非 Docker），需要额外准备一个可访问的 `webssh2` 服务；如果使用本项目的 Docker 镜像，则镜像已内置并默认启动。Dashboard 侧只负责按 `WEBSSH2_URL_TEMPLATE` 生成跳转地址。

网页批量更新说明：

- 在页面中勾选多台设备后，可直接发起“群发更新”
- 远程安装界面支持输入网段，例如 `10.126.35.x`，Dashboard 会自动尝试登录该网段 `1-255` 主机
- Dashboard 会先在本机执行 `build.sh`，然后生成最新更新包
- 登录成功后会先探测目标机是否已安装 agent 以及已记录的包版本
- 未安装则自动安装；版本缺失或低于当前包版本则更新；已是新版本则跳过
- SSH 设备使用 `SFTP + install.sh`
- Telnet 设备会通过 `wget/curl/busybox wget/python` 从 Dashboard 拉取更新包，再执行 `install.sh`
- `install.sh` 仍会自动判断目标机是否能注册服务，不能时自动回退到 `nohup`

注意：

- SSH / Telnet / sudo 密码如勾选记住，只保存在当前浏览器的本地存储中，不会在服务端落盘
- Telnet 自动更新至少需要目标机具备 `curl / wget / busybox wget / python3 / python` 其中之一用于下载更新包
- 如果你要走 Telnet 路径下载更新包，请确保你访问 Dashboard 的地址对目标机可达（不要用 `127.0.0.1/localhost` 打开页面，否则目标机将尝试访问它自己的 `127.0.0.1` 去下载更新包）。

### 安装 Dashboard 为常驻服务

```bash
sudo ./install_dashboard_service.sh
```

兼容说明：

- 已经安装过老版本 `dashboard.service` 的机器，脚本会直接原地更新这个服务
- 已经安装过 `broadcast_dashboard.service` 的机器，脚本也会继续兼容
- 默认优先保留旧的 `dashboard.service` 名称，避免升级后所有机器都要重装服务

## 防火墙

Dashboard 主机至少要允许 UDP 9999：

```bash
sudo ufw allow 9999/udp
```

如果要远程访问网页，再额外放行 Dashboard 端口：

```bash
sudo ufw allow 25000/tcp
```

## SSH 终端密码保存

网页终端首次输入密码并勾选“记住密码”后，密码只会保存在当前打开网页的这台机器的当前浏览器本地存储里：

- 不会在 Dashboard 服务端落盘
- 其他电脑打开同一个 Dashboard 页面时拿不到这份密码
- 当前浏览器里可以直接清除本机保存的密码

注意：这是浏览器本地存储，不是服务端加密保险箱；如果同一台机器上的同一浏览器配置文件被别人使用，对方仍然可以复用本地保存的数据。
