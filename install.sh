#!/bin/bash

# 配置变量
APP_NAME="device_broadcast"
# Systemd 相关
SERVICE_FILE="device_monitor.service"
SYSTEMD_PATH="/etc/systemd/system"
# Init.d 相关 (Busybox)
INIT_SCRIPT="S90device_broadcast"
INIT_PATH="/etc/init.d"
# 安装目标路径
INSTALL_PATH="/usr/bin"

# 检查是否以 root 运行
if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 请使用 sudo 或 root 权限运行此脚本"
  exit 1
fi

echo "--- 开始安装 $APP_NAME (Nohup 模式) ---"

# 1. 统一安装可执行文件
if [ ! -d "$INSTALL_PATH" ]; then
    mkdir -p "$INSTALL_PATH"
fi

echo "1. 复制程序到 $INSTALL_PATH ..."
if [ -f "./$APP_NAME" ]; then
    cp ./$APP_NAME $INSTALL_PATH/
    chmod +x $INSTALL_PATH/$APP_NAME
else
    echo "❌ 错误：当前目录下找不到 $APP_NAME 文件！"
    exit 1
fi

# 2. 判断系统类型并安装服务
if command -v systemctl >/dev/null 2>&1; then
    # ================= Systemd 模式 =================
    echo "2. 检测到 Systemd 环境，配置服务..."
    # ... (Systemd 部分保持不变，因为你主要在用 Init.d)
    if [ -f "./$SERVICE_FILE" ]; then
        cp ./$SERVICE_FILE $SYSTEMD_PATH/
        chmod 644 $SYSTEMD_PATH/$SERVICE_FILE
        systemctl daemon-reload
        systemctl enable $SERVICE_FILE
        systemctl restart $SERVICE_FILE
        echo "✅ Systemd 服务安装并启动成功。"
    fi

elif [ -d "$INIT_PATH" ]; then
    # ================= Init.d 模式 (重点修改) =================
    echo "2. 检测到 SysVinit/Busybox 环境，配置启动脚本..."
    
    # 先停止旧进程
    if [ -f "$INIT_PATH/$INIT_SCRIPT" ]; then
        $INIT_PATH/$INIT_SCRIPT stop >/dev/null 2>&1
    fi
    killall -q $APP_NAME >/dev/null 2>&1

    if [ -f "./$INIT_SCRIPT" ]; then
        cp ./$INIT_SCRIPT $INIT_PATH/
        chmod 755 $INIT_PATH/$INIT_SCRIPT
        echo "✅ 启动脚本已安装到 $INIT_PATH/$INIT_SCRIPT"
    else
        echo "❌ 错误：找不到 $INIT_SCRIPT"
        exit 1
    fi

    # 3. 尝试立即启动服务
    echo "3. 准备启动服务..."
    
    # 简单的网络检查 (保留你的逻辑)
    MAX_RETRIES=5
    COUNT=0
    echo "   正在检查网络连接..."
    while [ $COUNT -lt $MAX_RETRIES ]; do
        if route -n | grep -q "^0.0.0.0" || ifconfig | grep -v "127.0.0.1" | grep "inet addr" >/dev/null; then
             echo "   网络已就绪。"
             break
        fi
        echo "   等待网络初始化..."
        sleep 1
        COUNT=$((COUNT+1))
    done

    # 执行启动命令 (调用 S90 脚本的 start 函数)
    echo "   正在启动..."
    $INIT_PATH/$INIT_SCRIPT start
    
    # 验证是否运行
    sleep 2
    
    # 强力检测：ps aux 或者 ps
    # grep -v grep 排除掉 grep 命令自己
    # grep -v install.sh 排除掉当前脚本
    if ps | grep "$APP_NAME" | grep -v grep | grep -v install.sh >/dev/null; then
        echo "✅ 安装完成！程序正在后台运行 (PID: $(pidof $APP_NAME))"
        echo "   已设置 S90 开机自启。"
    else
        echo "❌ 启动看似失败。没有检测到进程。"
        echo "   排查建议："
        echo "   1. 尝试手动运行: $INSTALL_PATH/$APP_NAME"
        echo "   2. 检查是否有权限问题或缺少动态库"
    fi

else
    echo "❌ 未知的系统初始化方式"
    exit 1
fi