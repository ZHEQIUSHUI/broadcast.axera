# 通用设备广播与监控面板

这个项目现在不再只面向 AX 设备，而是一个通用的 Linux 设备广播与管理方案：

- 自动识别 `AX / Raspberry Pi / x86 / 其他 ARM Linux`
- 自动上报 `OS 发行版 / 内核 / libc / 架构 / 用户 / 机型`
- 支持 `CPU / 内存 / 显存` 指标
- AX 设备额外支持 `UID / version / board_id / CMM`
- 仪表盘支持动态设备卡片、指标历史曲线、网页 SSH 终端
- SSH 终端支持浏览器本地保存密码，下次可直接进入
- 支持网页批量更新：可选 `SSH / Telnet / 自动 SSH->Telnet 回退`
- 批量更新最终统一调用 `install.sh`，继续兼容 `systemd / init.d(S90) / rc.local / crontab / nohup`

## 依赖

```bash
pip install flask paramiko
```

## 编译

### 一次性编译所有本机支持的版本

```bash
./build.sh
```

编译产物会输出到 `dist/`，脚本会自动检测并使用本机已有的：

- `g++`
- `aarch64-none-linux-gnu-g++`
- `arm-linux-gnueabihf-g++`
- `arm-AX620E-linux-uclibcgnueabihf-g++`

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

网页批量更新说明：

- 在页面中勾选多台设备后，可直接发起“群发更新”
- Dashboard 会先在本机执行 `build.sh`，然后生成最新更新包
- SSH 设备使用 `SFTP + install.sh`
- Telnet 设备会通过 `wget/curl/busybox wget/python` 从 Dashboard 拉取更新包，再执行 `install.sh`
- `install.sh` 仍会自动判断目标机是否能注册服务，不能时自动回退到 `nohup`

注意：

- SSH / Telnet / sudo 密码如勾选记住，只保存在当前浏览器的本地存储中，不会在服务端落盘
- Telnet 自动更新至少需要目标机具备 `curl / wget / busybox wget / python3 / python` 其中之一用于下载更新包

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
