import socket
import threading
import json
import time
from flask import Flask, render_template_string
from datetime import datetime

# 配置
UDP_PORT = 9999
BUFFER_SIZE = 2048

# 全局变量，存储在线设备信息
# Key: 设备IP, Value: 设备信息字典
online_devices = {}
device_lock = threading.Lock()

app = Flask(__name__)

# HTML 模板 (已修改：增加了复制功能和相关样式/脚本)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Axera Device Monitor</title>
    <meta http-equiv="refresh" content="5"> 
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f4f9; padding: 20px; }
        h1 { color: #333; }
        table { width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; vertical-align: middle; }
        th { background-color: #007bff; color: white; font-weight: 600; text-transform: uppercase; font-size: 0.85rem; letter-spacing: 0.5px; }
        tr:last-child td { border-bottom: none; }
        tr:hover { background-color: #f8f9fa; }
        
        .status-dot { height: 10px; width: 10px; background-color: #28a745; border-radius: 50%; display: inline-block; margin-right: 5px; }
        
        /* 内存条样式 */
        .mem-bar-bg { width: 100px; height: 8px; background-color: #e9ecef; border-radius: 4px; overflow: hidden; display: inline-block; vertical-align: middle; margin-top: 5px; }
        .mem-bar-fill { height: 100%; background-color: #17a2b8; transition: width 0.5s ease; }
        
        /* 复制按钮样式 */
        .copy-btn {
            cursor: pointer;
            background-color: transparent;
            border: 1px solid #dee2e6;
            color: #6c757d;
            padding: 2px 6px;
            font-size: 11px;
            border-radius: 4px;
            margin-left: 8px;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
        }
        .copy-btn:hover {
            background-color: #e2e6ea;
            color: #007bff;
            border-color: #adb5bd;
        }
        .copy-btn:active {
            transform: translateY(1px);
        }
        .copy-icon { width: 12px; height: 12px; fill: currentColor; }
        
        .open-btn {
            background: none;
            border: none;
            cursor: pointer;
            margin-left: 5px;
        }
        .open-icon {
            width: 18px;
            height: 18px;
            fill: #2196F3;
        }
        .open-btn:hover .open-icon {
            fill: #0b7dda;
        }
        
        /* 提示框 (Toast) */
        #toast {
            visibility: hidden;
            min-width: 200px;
            background-color: #333;
            color: #fff;
            text-align: center;
            border-radius: 4px;
            padding: 10px;
            position: fixed;
            z-index: 1;
            left: 50%;
            bottom: 30px;
            transform: translateX(-50%);
            font-size: 14px;
            opacity: 0;
            transition: opacity 0.3s, bottom 0.3s;
        }
        #toast.show {
            visibility: visible;
            opacity: 1;
            bottom: 50px;
        }
    </style>
</head>
<body>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
        <h1>局域网设备监控 (Axera Boards)</h1>
        <div style="color:#666;">在线设备: <strong>{{ devices|length }}</strong></div>
    </div>

    <table>
        <thead>
            <tr>
                <th>状态</th>
                <th>用户</th>
                <th>IP 地址</th>
                <th>Board ID</th>
                <th>UID</th>
                <th>版本</th>
                <th>CMM 内存</th>
                <th>系统内存 (空闲/总计)</th>
                <th>CPU</th>
                <th>最后更新</th>
            </tr>
        </thead>
        <tbody>
            {% for ip, dev in devices.items() %}
            <tr>
                <td><span class="status-dot"></span>Online</td>
                
                <td style="white-space: nowrap;">
                    <span id="user-{{ loop.index }}">{{ dev.user }}</span>
                    <button class="copy-btn" onclick="copyText('{{ dev.user }}')" title="复制用户名">
                        <svg class="copy-icon" viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
                    </button>
                </td>

                <td style="white-space: nowrap;">
                    <strong id="ip-{{ loop.index }}">{{ ip }}</strong>
                    <button class="copy-btn" onclick="copyText('{{ ip }}')" title="复制IP">
                        <svg class="copy-icon" viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
                    </button>
                    <button class="open-btn" onclick="openSSH('{{ ip }}')" title="打开SSH">
                        <svg viewBox="0 0 24 24" class="open-icon">
                            <path d="M10 17l5-5-5-5v10z"/>
                        </svg>
                    </button>
                </td>

                <td>{{ dev.board_id }}</td>
                <td><small style="color:#888; font-family:monospace;">{{ dev.uid[-6:] if dev.uid|length > 6 else dev.uid }}</small></td>
                <td>{{ dev.version }}</td>
                <td>{{ dev.cmm_total }}</td>
                <td>
                    <div style="font-size:12px; margin-bottom:2px;">
                        {{ (dev.sys_mem_free_kb / 1024)|round(0)|int }}M / {{ (dev.sys_mem_total_kb / 1024)|round(0)|int }}M
                    </div>
                    {% set mem_percent = ((dev.sys_mem_total_kb - dev.sys_mem_free_kb) / dev.sys_mem_total_kb * 100) %}
                    <div class="mem-bar-bg">
                        <div class="mem-bar-fill" style="width: {{ mem_percent }}%; background-color: {{ 'orange' if mem_percent > 80 else '#17a2b8' }};"></div>
                    </div>
                </td>
                <td>
                    <span style="font-weight:bold; color: {{ 'red' if dev.cpu_usage_percent > 90 else 'inherit' }}">{{ dev.cpu_usage_percent }}%</span>
                </td>
                <td style="color:#666; font-size:0.9em;">{{ dev.last_seen_str }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    {% if devices|length == 0 %}
        <div style="text-align:center; padding: 40px; color: #999;">
            <h3>📡 正在扫描局域网广播...</h3>
            <p>请确保设备与服务器在同一网段，并且 UDP 9999 端口未被防火墙拦截。</p>
        </div>
    {% endif %}

    <div id="toast">已复制到剪贴板</div>

    <script>
        function copyText(text) {
            // 使用现代 Clipboard API
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(text).then(() => {
                    showToast("已复制: " + text);
                }, (err) => {
                    console.error('Could not copy text: ', err);
                    fallbackCopyText(text); // 尝试降级处理
                });
            } else {
                // 降级处理 (针对没有HTTPS或旧浏览器)
                fallbackCopyText(text);
            }
        }
        
        function openSSH(ip) {
            let url = `http://10.126.33.124:2222/ssh/host/${ip}`;
            window.open(url, "_blank");
        }

        function fallbackCopyText(text) {
            const textArea = document.createElement("textarea");
            textArea.value = text;
            textArea.style.position = "fixed";  // 避免滚动到底部
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            try {
                document.execCommand('copy');
                showToast("已复制: " + text);
            } catch (err) {
                console.error('Fallback: Oops, unable to copy', err);
                showToast("复制失败，请手动复制");
            }
            document.body.removeChild(textArea);
        }

        function showToast(message) {
            var toast = document.getElementById("toast");
            toast.innerText = message;
            toast.className = "show";
            setTimeout(function(){ toast.className = toast.className.replace("show", ""); }, 2000);
        }
    </script>
</body>
</html>
"""

def udp_listener():
    """后台线程：监听 UDP 广播"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # 绑定到所有接口
    sock.bind(('0.0.0.0', UDP_PORT))
    print(f"UDP Listener started on port {UDP_PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
            ip_address = addr[0]
            try:
                # 解析 JSON
                info = json.loads(data.decode('utf-8'))
                
                # 添加时间戳
                info['last_seen'] = time.time()
                info['last_seen_str'] = datetime.now().strftime("%H:%M:%S")
                
                with device_lock:
                    online_devices[ip_address] = info
            except json.JSONDecodeError:
                print(f"Received invalid JSON from {ip_address}")
        except Exception as e:
            print(f"Socket error: {e}")

def cleanup_loop():
    """后台线程：清理离线设备 (超过10秒没心跳则移除)"""
    while True:
        time.sleep(5)
        now = time.time()
        with device_lock:
            # 找出超时的设备 IP
            offline_ips = [ip for ip, info in online_devices.items() if now - info['last_seen'] > 10]
            for ip in offline_ips:
                print(f"Device {ip} went offline.")
                del online_devices[ip]

@app.route('/')
def index():
    with device_lock:
        # 按 IP 排序显示
        sorted_devices = dict(sorted(online_devices.items()))
    return render_template_string(HTML_TEMPLATE, devices=sorted_devices)

if __name__ == '__main__':
    # 启动 UDP 监听线程
    t_udp = threading.Thread(target=udp_listener, daemon=True)
    t_udp.start()

    # 启动清理线程
    t_clean = threading.Thread(target=cleanup_loop, daemon=True)
    t_clean.start()

    # 启动 Web 服务器
    # 提示：浏览器复制功能通常要求 HTTPS 或 localhost。
    # 如果是局域网 HTTP 访问，代码中已包含 fallbackCopyText 方法来兼容。
    app.run(host='0.0.0.0', port=25000, debug=False)