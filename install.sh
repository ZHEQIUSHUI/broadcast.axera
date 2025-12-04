#!/bin/bash

# 配置变量
APP_NAME="device_broadcast"
SERVICE_FILE="device_broadcast.service"
INSTALL_PATH="/usr/local/bin"
SYSTEMD_PATH="/etc/systemd/system"

# 检查是否以 root 运行
if [ "$EUID" -ne 0 ]; then
  echo "❌ 请使用 sudo 或 root 权限运行此脚本"
  exit 1
fi

echo "--- 开始安装 $APP_NAME ---"

# 1. 停止旧服务（如果已安装）
if systemctl is-active --quiet $SERVICE_FILE; then
    echo "停止旧服务..."
    systemctl stop $SERVICE_FILE
    systemctl disable $SERVICE_FILE
fi

# 2. 安装可执行文件
echo "复制程序到 $INSTALL_PATH ..."
if [ -f "./$APP_NAME" ]; then
    cp ./$APP_NAME $INSTALL_PATH/
    chmod +x $INSTALL_PATH/$APP_NAME
else
    echo "❌ 错误：当前目录下找不到 $APP_NAME 文件！"
    exit 1
fi

# 3. 安装服务文件
echo "配置系统服务..."
if [ -f "./$SERVICE_FILE" ]; then
    cp ./$SERVICE_FILE $SYSTEMD_PATH/
    chmod 644 $SYSTEMD_PATH/$SERVICE_FILE
else
    echo "❌ 错误：当前目录下找不到 $SERVICE_FILE 文件！"
    exit 1
fi

# 4. 刷新并启动
echo "启动服务..."
systemctl daemon-reload
systemctl enable $SERVICE_FILE
systemctl start $SERVICE_FILE

# 5. 验证状态
sleep 1
if systemctl is-active --quiet $SERVICE_FILE; then
    echo "✅ 安装成功！服务已运行，且开机自启。"
else
    echo "⚠️ 服务启动失败，可能是缺少动态库。"
    echo "请尝试手动运行 /usr/local/bin/$APP_NAME 看看报错信息。"
fi