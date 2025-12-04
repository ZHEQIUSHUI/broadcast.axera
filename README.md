# 设备广播系统

## 启动网页的电脑的防火墙
```
sudo ufw allow 9999/udp
```

## 第三方库
```
pip install flask
```

## 编译
### 本地编译
```
g++ -o device_broadcast device_broadcast.cpp
```
### 交叉编译
```
aarch64-none-linux-gnu-g++ -o device_broadcast device_broadcast.cpp
```

## 运行
### 板端
```
./install.sh
```
### 网页端/接收端
```
python dashboard.py 
```