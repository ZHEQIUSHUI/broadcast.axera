import collections
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import tarfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlsplit

import paramiko
from flask import Flask, abort, jsonify, render_template, request, send_file

UDP_PORT = int(os.getenv("UDP_PORT", "9999"))
BUFFER_SIZE = 8192
DEVICE_TIMEOUT_SECONDS = int(os.getenv("DEVICE_TIMEOUT_SECONDS", "12"))
DEVICE_HISTORY_LIMIT = int(os.getenv("DEVICE_HISTORY_LIMIT", "40"))
TERMINAL_IDLE_TIMEOUT_SECONDS = int(os.getenv("TERMINAL_IDLE_TIMEOUT_SECONDS", "1800"))
TERMINAL_BUFFER_LIMIT = int(os.getenv("TERMINAL_BUFFER_LIMIT", "200000"))
POLL_WAIT_MS = int(os.getenv("TERMINAL_POLL_WAIT_MS", "350"))
WEBSSH2_ENABLED = os.getenv("WEBSSH2_ENABLED", "1").lower() not in ("0", "false", "no")
WEBSSH2_URL_TEMPLATE = os.getenv("WEBSSH2_URL_TEMPLATE", "http://{dashboard_host}:2222/ssh/host/{host}")
UPDATE_JOB_RETENTION_SECONDS = int(os.getenv("UPDATE_JOB_RETENTION_SECONDS", "3600"))
UPDATE_MAX_PARALLEL = int(os.getenv("UPDATE_MAX_PARALLEL", "4"))
UPDATE_REMOTE_TIMEOUT_SECONDS = int(os.getenv("UPDATE_REMOTE_TIMEOUT_SECONDS", "480"))
UPDATE_LOG_LIMIT = int(os.getenv("UPDATE_LOG_LIMIT", "60000"))

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
RUNTIME_ROOT = os.path.join(APP_ROOT, ".runtime")
UPDATE_JOBS_ROOT = os.path.join(RUNTIME_ROOT, "update_jobs")
DEVICE_METADATA_PATH = os.path.join(RUNTIME_ROOT, "device_metadata.json")

ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_SINGLE_RE = re.compile(r"\x1b[@-_]")

TELNET_IAC = 255
TELNET_DONT = 254
TELNET_DO = 253
TELNET_WONT = 252
TELNET_WILL = 251
TELNET_SB = 250
TELNET_SE = 240

app = Flask(__name__, template_folder=os.path.join(APP_ROOT, "templates"))

online_devices = {}
device_lock = threading.Lock()
device_metadata = {}
metadata_lock = threading.Lock()

terminal_sessions = {}
terminal_lock = threading.Lock()
update_jobs = {}
update_lock = threading.Lock()


class PasswordRequiredError(Exception):
    pass


def safe_int(value):
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def safe_float(value):
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clean_text(value, fallback=""):
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip()
    return str(value)


def parse_size_text_to_kb(value):
    if not value:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMG]?B)?", str(value), re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "KB").upper()
    if unit == "KB":
        return int(amount)
    if unit == "MB":
        return int(amount * 1024)
    if unit == "GB":
        return int(amount * 1024 * 1024)
    return int(amount / 1024)


def sanitize_terminal_text(text):
    text = ANSI_OSC_RE.sub("", text)
    text = ANSI_CSI_RE.sub("", text)
    text = ANSI_SINGLE_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "")
    output = []
    for ch in text:
        if ch in ("\x08", "\x7f"):
            if output:
                output.pop()
            continue
        if ch == "\x00":
            continue
        output.append(ch)
    return "".join(output)


def now_string():
    return datetime.now().strftime("%H:%M:%S")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def clamp_int(value, minimum, maximum, fallback):
    parsed = safe_int(value)
    if parsed is None:
        return fallback
    return max(minimum, min(maximum, parsed))


def tail_text(text, limit=UPDATE_LOG_LIMIT):
    if not text:
        return ""
    if len(text) <= limit:
        return text
    trimmed = text[-limit:]
    return f"... [trimmed {len(text) - limit} chars]\n{trimmed}"


def atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp_path, path)


def normalize_metadata_text(value, limit):
    text = clean_text(value)
    if len(text) > limit:
        text = text[:limit]
    return text


def make_device_metadata_key(prefix, value):
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip().lower()
    return f"{prefix}:{text}" if text else ""


def device_metadata_keys_for_payload(payload, ip_address=""):
    keys = []
    seen = set()

    def add(prefix, value):
        key = make_device_metadata_key(prefix, value)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    add("uid", payload.get("uid"))
    add("board_id", payload.get("board_id"))
    add("ip", payload.get("ip") or ip_address)
    return keys


def load_device_metadata_records():
    if not os.path.exists(DEVICE_METADATA_PATH):
        return {}

    try:
        with open(DEVICE_METADATA_PATH, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except Exception as exc:
        print(f"failed to load device metadata: {exc}")
        return {}

    if isinstance(payload, dict) and isinstance(payload.get("records"), dict):
        records = payload["records"]
    elif isinstance(payload, dict):
        records = payload
    else:
        return {}

    normalized = {}
    for key, record in records.items():
        if not isinstance(record, dict):
            continue
        title = normalize_metadata_text(record.get("title"), 80)
        note = normalize_metadata_text(record.get("note"), 400)
        if not title and not note:
            continue
        normalized[str(key)] = {
            "title": title,
            "note": note,
            "updated_at": clean_text(record.get("updated_at")) or now_iso(),
        }
    return normalized


def persist_device_metadata_records_locked():
    atomic_write_json(
        DEVICE_METADATA_PATH,
        {
            "version": 1,
            "updated_at": now_iso(),
            "records": device_metadata,
        },
    )


def resolve_device_metadata(payload, ip_address=""):
    keys = device_metadata_keys_for_payload(payload, ip_address)
    with metadata_lock:
        for key in keys:
            record = device_metadata.get(key)
            if record:
                return dict(record)
    return {}


def apply_device_metadata(device):
    metadata = resolve_device_metadata(device, device.get("ip"))
    custom_title = metadata.get("title", "")
    custom_note = metadata.get("note", "")
    device["custom_title"] = custom_title
    device["custom_note"] = custom_note
    device["display_name"] = custom_title or device.get("hostname") or device.get("ip") or "Unknown"
    device["metadata_updated_at"] = metadata.get("updated_at", "")
    return device


device_metadata = load_device_metadata_records()


def normalize_device_payload(payload, ip_address):
    hostname = clean_text(payload.get("hostname")) or clean_text(payload.get("board_id")) or ip_address
    default_user = clean_text(payload.get("user"), "root") or "root"
    os_name = clean_text(payload.get("os_pretty_name")) or clean_text(payload.get("os_name"), "Linux")
    device_type = clean_text(payload.get("device_type"), "Generic Linux")
    device_kind = clean_text(payload.get("device_kind"), "generic_linux")
    board_model = clean_text(payload.get("board_model")) or clean_text(payload.get("board_id"))
    board_vendor = clean_text(payload.get("platform_vendor")) or clean_text(payload.get("board_vendor"))
    arch = clean_text(payload.get("arch")) or clean_text(payload.get("machine"))

    mem_total_kb = safe_int(payload.get("mem_total_kb") or payload.get("sys_mem_total_kb"))
    mem_available_kb = safe_int(payload.get("mem_available_kb") or payload.get("sys_mem_free_kb"))
    mem_free_kb = safe_int(payload.get("mem_free_kb") or payload.get("sys_mem_free_kb"))
    mem_used_kb = safe_int(payload.get("mem_used_kb"))
    mem_buffers_kb = safe_int(payload.get("mem_buffers_kb"))
    mem_cached_kb = safe_int(payload.get("mem_cached_kb"))
    mem_sreclaimable_kb = safe_int(payload.get("mem_sreclaimable_kb"))
    mem_shmem_kb = safe_int(payload.get("mem_shmem_kb"))
    mem_cache_effective_kb = safe_int(payload.get("mem_cache_effective_kb"))
    if mem_used_kb is None and mem_total_kb is not None and mem_available_kb is not None:
        mem_used_kb = mem_total_kb - mem_available_kb

    mem_used_percent = safe_float(payload.get("mem_used_percent"))
    if mem_used_percent is None and mem_total_kb and mem_used_kb is not None:
        mem_used_percent = mem_used_kb * 100.0 / mem_total_kb

    gpu_total_mb = safe_int(payload.get("gpu_mem_total_mb"))
    gpu_used_mb = safe_int(payload.get("gpu_mem_used_mb"))
    gpu_used_percent = safe_float(payload.get("gpu_mem_used_percent"))
    if gpu_used_percent is None and gpu_total_mb and gpu_used_mb is not None:
        gpu_used_percent = gpu_used_mb * 100.0 / gpu_total_mb
    gpu_usage_percent = safe_float(payload.get("gpu_usage_percent"))

    cmm_total_kb = safe_int(payload.get("cmm_total_kb"))
    if cmm_total_kb is None:
        cmm_total_kb = parse_size_text_to_kb(payload.get("cmm_total"))
    cmm_free_kb = safe_int(payload.get("cmm_free_kb"))
    cmm_used_kb = safe_int(payload.get("cmm_used_kb"))
    if cmm_used_kb is None and cmm_total_kb is not None and cmm_free_kb is not None:
        cmm_used_kb = cmm_total_kb - cmm_free_kb
    cmm_used_percent = safe_float(payload.get("cmm_used_percent"))
    if cmm_used_percent is None and cmm_total_kb and cmm_used_kb is not None:
        cmm_used_percent = cmm_used_kb * 100.0 / cmm_total_kb

    gpu_present = bool(payload.get("gpu_present")) or any(
        value is not None for value in (gpu_total_mb, gpu_used_mb, gpu_used_percent, gpu_usage_percent)
    )
    is_ax = bool(payload.get("is_ax")) or device_kind == "ax"
    is_raspberry_pi = bool(payload.get("is_raspberry_pi")) or device_kind == "raspberry_pi"

    if not is_ax:
        cmm_total_kb = None
        cmm_free_kb = None
        cmm_used_kb = None
        cmm_used_percent = None

    return {
        "ip": ip_address,
        "hostname": hostname,
        "display_name": hostname,
        "custom_title": "",
        "custom_note": "",
        "default_user": default_user,
        "device_type": device_type,
        "device_kind": device_kind,
        "platform_vendor": board_vendor or "Generic",
        "board_model": board_model,
        "board_vendor": clean_text(payload.get("board_vendor")),
        "arch": arch,
        "machine": clean_text(payload.get("machine")),
        "kernel": clean_text(payload.get("kernel")),
        "libc": clean_text(payload.get("libc")),
        "os_name": os_name,
        "os_id": clean_text(payload.get("os_id")),
        "os_like": clean_text(payload.get("os_like")),
        "os_version": clean_text(payload.get("os_version")),
        "uid": clean_text(payload.get("uid")),
        "version": clean_text(payload.get("version")),
        "board_id": clean_text(payload.get("board_id")),
        "cpu_usage_percent": safe_float(payload.get("cpu_usage_percent")),
        "cpu_cores": safe_int(payload.get("cpu_cores")),
        "uptime_seconds": safe_int(payload.get("uptime_seconds")),
        "mem_total_kb": mem_total_kb,
        "mem_available_kb": mem_available_kb,
        "mem_free_kb": mem_free_kb,
        "mem_used_kb": mem_used_kb,
        "mem_used_percent": mem_used_percent,
        "mem_buffers_kb": mem_buffers_kb,
        "mem_cached_kb": mem_cached_kb,
        "mem_sreclaimable_kb": mem_sreclaimable_kb,
        "mem_shmem_kb": mem_shmem_kb,
        "mem_cache_effective_kb": mem_cache_effective_kb,
        "gpu_present": gpu_present,
        "gpu_vendor": clean_text(payload.get("gpu_vendor")),
        "gpu_note": clean_text(payload.get("gpu_note")),
        "gpu_mem_total_mb": gpu_total_mb,
        "gpu_mem_used_mb": gpu_used_mb,
        "gpu_mem_used_percent": gpu_used_percent,
        "gpu_usage_percent": gpu_usage_percent,
        "cmm_total_kb": cmm_total_kb,
        "cmm_free_kb": cmm_free_kb,
        "cmm_used_kb": cmm_used_kb,
        "cmm_used_percent": cmm_used_percent,
        "is_ax": is_ax,
        "is_raspberry_pi": is_raspberry_pi,
        "schema_version": clean_text(payload.get("schema_version"), "1"),
        "timestamp_ms": safe_int(payload.get("timestamp_ms")),
    }


def update_device_history(device):
    history = device.setdefault(
        "history",
        {
            "cpu": collections.deque(maxlen=DEVICE_HISTORY_LIMIT),
            "mem": collections.deque(maxlen=DEVICE_HISTORY_LIMIT),
            "gpu": collections.deque(maxlen=DEVICE_HISTORY_LIMIT),
            "cmm": collections.deque(maxlen=DEVICE_HISTORY_LIMIT),
        },
    )
    if device.get("cpu_usage_percent") is not None:
        history["cpu"].append(round(device["cpu_usage_percent"], 2))
    if device.get("mem_used_percent") is not None:
        history["mem"].append(round(device["mem_used_percent"], 2))
    if device.get("gpu_usage_percent") is not None:
        history["gpu"].append(round(device["gpu_usage_percent"], 2))
    elif device.get("gpu_mem_used_percent") is not None:
        history["gpu"].append(round(device["gpu_mem_used_percent"], 2))
    if device.get("cmm_used_percent") is not None:
        history["cmm"].append(round(device["cmm_used_percent"], 2))


def serialize_device(device):
    payload = dict(device)
    payload["history"] = {key: list(values) for key, values in device.get("history", {}).items()}
    return payload


def build_summary(devices):
    summary = {
        "device_count": len(devices),
        "device_types": {},
        "avg_cpu_usage_percent": None,
        "high_cpu_count": 0,
        "high_mem_count": 0,
        "ax_count": 0,
        "gpu_count": 0,
    }

    cpu_values = []
    for device in devices:
        summary["device_types"][device["device_type"]] = summary["device_types"].get(device["device_type"], 0) + 1
        if device.get("is_ax"):
            summary["ax_count"] += 1
        if device.get("gpu_present"):
            summary["gpu_count"] += 1
        if device.get("cpu_usage_percent") is not None:
            cpu_values.append(device["cpu_usage_percent"])
            if device["cpu_usage_percent"] >= 80:
                summary["high_cpu_count"] += 1
        if device.get("mem_used_percent") is not None and device["mem_used_percent"] >= 80:
            summary["high_mem_count"] += 1

    if cpu_values:
        summary["avg_cpu_usage_percent"] = round(sum(cpu_values) / len(cpu_values), 2)
    return summary


def get_online_devices_by_ip():
    with device_lock:
        return {ip: dict(device) for ip, device in online_devices.items()}


def create_update_target_snapshot(device):
    return {
        "ip": device["ip"],
        "display_name": device.get("display_name") or device["ip"],
        "default_user": device.get("default_user") or "root",
        "device_type": device.get("device_type") or "Generic Linux",
        "device_kind": device.get("device_kind") or "generic_linux",
        "os_name": device.get("os_name") or "Linux",
        "source": "online",
    }


def create_manual_target_snapshot(host, default_user="root"):
    host = clean_text(host)
    return {
        "ip": host,
        "display_name": host,
        "default_user": clean_text(default_user, "root") or "root",
        "device_type": "远程安装目标",
        "device_kind": "manual_target",
        "os_name": "待连接",
        "source": "manual",
    }


def normalize_target_hosts(raw_hosts):
    if not isinstance(raw_hosts, list):
        return []

    hosts = []
    seen = set()
    for item in raw_hosts:
        host = clean_text(item)
        if not host or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
    return hosts


def build_update_job_public(job_id, targets, strategy, parallelism):
    return {
        "job_id": job_id,
        "status": "queued",
        "created_at": time.time(),
        "created_at_str": now_iso(),
        "started_at": None,
        "finished_at": None,
        "strategy": strategy,
        "parallelism": parallelism,
        "target_count": len(targets),
        "completed_count": 0,
        "success_count": 0,
        "failed_count": 0,
        "bundle_ready": False,
        "bundle_version": None,
        "build_output": "",
        "error": "",
        "targets": [
            {
                "ip": target["ip"],
                "display_name": target["display_name"],
                "device_type": target["device_type"],
                "os_name": target["os_name"],
                "source": target.get("source", "online"),
                "status": "pending",
                "transport": None,
                "message": "等待开始",
                "started_at": None,
                "finished_at": None,
                "attempts": [],
                "log": "",
            }
            for target in targets
        ],
    }


def find_update_target(job, ip_address):
    for target in job["targets"]:
        if target["ip"] == ip_address:
            return target
    return None


def recompute_update_job_counts(job):
    job["completed_count"] = sum(1 for item in job["targets"] if item["status"] in ("success", "failed"))
    job["success_count"] = sum(1 for item in job["targets"] if item["status"] == "success")
    job["failed_count"] = sum(1 for item in job["targets"] if item["status"] == "failed")


def update_job_target_state(job_id, ip_address, **fields):
    with update_lock:
        job = update_jobs.get(job_id)
        if not job:
            return None
        target = find_update_target(job, ip_address)
        if not target:
            return None
        target.update(fields)
        if "log" in fields:
            target["log"] = tail_text(target.get("log", ""))
        recompute_update_job_counts(job)
        return target


def mark_update_job_failed(job_id, error_message):
    with update_lock:
        job = update_jobs.get(job_id)
        if not job:
            return
        job["status"] = "failed"
        job["error"] = error_message
        job["finished_at"] = time.time()
        for target in job["targets"]:
            if target["status"] == "pending":
                target["status"] = "failed"
                target["message"] = error_message
                target["finished_at"] = time.time()
        recompute_update_job_counts(job)


def finish_update_job(job_id):
    with update_lock:
        job = update_jobs.get(job_id)
        if not job:
            return
        if job["status"] == "failed" and job["failed_count"] == 0:
            job["failed_count"] = job["target_count"]
        elif job["failed_count"] > 0:
            job["status"] = "partial_success" if job["success_count"] > 0 else "failed"
        else:
            job["status"] = "success"
        job["finished_at"] = time.time()


def build_update_bundle(job_id):
    job_root = os.path.join(UPDATE_JOBS_ROOT, job_id)
    os.makedirs(job_root, exist_ok=True)

    build_script = os.path.join(APP_ROOT, "build.sh")
    result = subprocess.run(
        ["bash", build_script],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    build_output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"build.sh failed with exit code {result.returncode}\n{tail_text(build_output)}")

    archive_path = os.path.join(job_root, "device_broadcast_update.tar.gz")
    bundle_root = "broadcast.axera_update"
    package_files = [
        "install.sh",
        "build.sh",
        "device_broadcast.cpp",
        "S90device_broadcast",
        "device_monitor.service",
        "README.md",
    ]

    with tarfile.open(archive_path, "w:gz") as archive:
        for relative_path in package_files:
            absolute_path = os.path.join(APP_ROOT, relative_path)
            if os.path.exists(absolute_path):
                archive.add(absolute_path, arcname=os.path.join(bundle_root, relative_path))

        dist_dir = os.path.join(APP_ROOT, "dist")
        if os.path.isdir(dist_dir):
            for entry in sorted(os.listdir(dist_dir)):
                absolute_path = os.path.join(dist_dir, entry)
                if os.path.isfile(absolute_path):
                    archive.add(absolute_path, arcname=os.path.join(bundle_root, "dist", entry))

    return {
        "job_root": job_root,
        "archive_path": archive_path,
        "token": uuid.uuid4().hex,
        "version": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "build_output": tail_text(build_output),
    }


def serialize_update_job(job):
    payload = dict(job)
    payload.pop("bundle_path", None)
    payload.pop("bundle_token", None)
    payload["targets"] = [dict(item) for item in job["targets"]]
    return payload


def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"UDP listener started on 0.0.0.0:{UDP_PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
            ip_address = addr[0]
            payload = json.loads(data.decode("utf-8", errors="replace"))
            normalized = normalize_device_payload(payload, ip_address)
            apply_device_metadata(normalized)
            normalized["last_seen"] = time.time()
            normalized["last_seen_str"] = now_string()

            with device_lock:
                existing = online_devices.get(ip_address, {})
                history = existing.get("history")
                if history:
                    normalized["history"] = history
                update_device_history(normalized)
                online_devices[ip_address] = normalized
        except json.JSONDecodeError:
            continue
        except Exception as exc:
            print(f"UDP listener error: {exc}")
            time.sleep(0.2)


def cleanup_loop():
    while True:
        time.sleep(2)
        threshold = time.time() - DEVICE_TIMEOUT_SECONDS
        with device_lock:
            offline_ips = [ip for ip, info in online_devices.items() if info.get("last_seen", 0) < threshold]
            for ip in offline_ips:
                del online_devices[ip]


class BufferedTerminalSession:
    def __init__(self, host, port, protocol):
        self.host = host
        self.port = port
        self.protocol = protocol
        self.buffer = ""
        self.buffer_offset = 0
        self.closed = False
        self.exit_message = None
        self.last_activity = time.time()
        self.lock = threading.Lock()
        self.data_event = threading.Event()

    def _append_output(self, text):
        if not text:
            return
        with self.lock:
            self.buffer += text
            if len(self.buffer) > TERMINAL_BUFFER_LIMIT:
                overflow = len(self.buffer) - TERMINAL_BUFFER_LIMIT
                self.buffer = self.buffer[overflow:]
                self.buffer_offset += overflow
        self.data_event.set()

    def poll(self, cursor, wait_ms=0):
        if cursor is None:
            cursor = 0

        with self.lock:
            current_end = self.buffer_offset + len(self.buffer)
            should_wait = cursor >= current_end and not self.closed and wait_ms > 0

        if should_wait:
            self.data_event.clear()
            self.data_event.wait(wait_ms / 1000.0)

        self.last_activity = time.time()
        with self.lock:
            if cursor < self.buffer_offset:
                cursor = self.buffer_offset
            start = max(cursor - self.buffer_offset, 0)
            chunk = self.buffer[start:]
            next_cursor = self.buffer_offset + len(self.buffer)
        return {
            "cursor": next_cursor,
            "output": chunk,
            "closed": self.closed,
        }

    def finalize(self, message):
        if self.closed:
            return
        self.closed = True
        self.exit_message = message
        self._append_output(message)
        self.data_event.set()

    def write(self, data):
        raise NotImplementedError

    def resize(self, cols, rows):
        del cols, rows

    def close(self):
        self.closed = True
        self.data_event.set()


class SshTerminalSession(BufferedTerminalSession):
    def __init__(self, host, username, port=22, password=None):
        super().__init__(host=host, port=port, protocol="ssh")
        self.username = username
        self.password = password
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.channel = None

        self._connect()
        self.reader = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader.start()

    def _connect(self):
        kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": 10,
            "banner_timeout": 10,
            "auth_timeout": 10,
        }
        if self.password:
            kwargs.update({"password": self.password, "allow_agent": False, "look_for_keys": False})
        else:
            kwargs.update({"allow_agent": True, "look_for_keys": True})

        try:
            self.client.connect(**kwargs)
        except paramiko.AuthenticationException as exc:
            self.client.close()
            if self.password:
                raise
            raise PasswordRequiredError(str(exc))

        self.channel = self.client.invoke_shell(term="xterm", width=140, height=40)
        self.channel.settimeout(0.0)
        auth_mode = "password" if self.password else "ssh key/agent"
        self._append_output(f"[ssh connected] {self.username}@{self.host}:{self.port} via {auth_mode}\n")

    def _reader_loop(self):
        try:
            while not self.closed:
                if self.channel.closed:
                    break

                received = False
                while self.channel.recv_ready():
                    chunk = self.channel.recv(4096)
                    if not chunk:
                        break
                    received = True
                    decoded = sanitize_terminal_text(chunk.decode("utf-8", errors="replace"))
                    self._append_output(decoded)

                if received:
                    self.last_activity = time.time()
                time.sleep(0.03)
        except Exception as exc:
            self._append_output(f"\n[ssh error] {exc}\n")
        finally:
            try:
                if self.channel is not None:
                    self.channel.close()
            except Exception:
                pass
            self.client.close()
            self.finalize("[ssh session closed]\n")

    def write(self, data):
        if self.closed or self.channel is None or self.channel.closed:
            raise RuntimeError("session closed")
        self.channel.send(data)
        self.last_activity = time.time()

    def resize(self, cols, rows):
        if self.closed or self.channel is None or self.channel.closed:
            return
        self.channel.resize_pty(width=max(40, min(cols, 240)), height=max(12, min(rows, 80)))

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.channel is not None:
                self.channel.close()
        except Exception:
            pass
        try:
            self.client.close()
        except Exception:
            pass
        self.data_event.set()


class TelnetTerminalSession(BufferedTerminalSession):
    def __init__(self, host, username="", port=23, password=None):
        super().__init__(host=host, port=port, protocol="telnet")
        self.username = username or ""
        self.password = password or ""
        self.sock = None
        self.telnet_pending = bytearray()
        self.auto_prompt_tail = ""
        self.username_sent = False
        self.password_sent = False
        self.auto_login_deadline = time.time() + 12

        self._connect()
        self.reader = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader.start()

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.settimeout(0.1)
        self.sock = sock
        self._append_output(
            f"[telnet connected] {self.host}:{self.port}\n"
            "[info] Telnet is plaintext. If auto-login does not trigger, type username/password manually.\n"
        )

    def _send_bytes(self, payload):
        if not payload or self.sock is None:
            return
        self.sock.sendall(payload)

    def _negotiate_telnet(self, raw):
        data = bytes(self.telnet_pending) + raw
        self.telnet_pending = bytearray()
        output = bytearray()
        i = 0

        while i < len(data):
            byte = data[i]
            if byte != TELNET_IAC:
                output.append(byte)
                i += 1
                continue

            if i + 1 >= len(data):
                self.telnet_pending.extend(data[i:])
                break

            command = data[i + 1]
            if command == TELNET_IAC:
                output.append(TELNET_IAC)
                i += 2
            elif command in (TELNET_DO, TELNET_DONT, TELNET_WILL, TELNET_WONT):
                if i + 2 >= len(data):
                    self.telnet_pending.extend(data[i:])
                    break
                option = data[i + 2]
                if command == TELNET_DO:
                    self._send_bytes(bytes([TELNET_IAC, TELNET_WONT, option]))
                elif command == TELNET_WILL:
                    self._send_bytes(bytes([TELNET_IAC, TELNET_DONT, option]))
                i += 3
            elif command == TELNET_SB:
                end = data.find(bytes([TELNET_IAC, TELNET_SE]), i + 2)
                if end == -1:
                    self.telnet_pending.extend(data[i:])
                    break
                i = end + 2
            else:
                i += 2

        return bytes(output)

    def _maybe_auto_login(self, text):
        if time.time() > self.auto_login_deadline:
            return

        lower_tail = (self.auto_prompt_tail + text).lower()[-300:]
        if self.username and not self.username_sent and any(prompt in lower_tail for prompt in ("login:", "username:", "login as:")):
            self._send_bytes((self.username + "\r\n").encode("utf-8"))
            self.username_sent = True
        if self.password and not self.password_sent and "password:" in lower_tail:
            self._send_bytes((self.password + "\r\n").encode("utf-8"))
            self.password_sent = True
        self.auto_prompt_tail = lower_tail

    def _reader_loop(self):
        try:
            while not self.closed:
                try:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        break
                except socket.timeout:
                    time.sleep(0.03)
                    continue
                except BlockingIOError:
                    time.sleep(0.03)
                    continue

                decoded_bytes = self._negotiate_telnet(chunk)
                if not decoded_bytes:
                    continue

                try:
                    decoded = decoded_bytes.decode("utf-8", errors="replace")
                except Exception:
                    decoded = decoded_bytes.decode("latin-1", errors="replace")
                decoded = sanitize_terminal_text(decoded)
                self._append_output(decoded)
                self._maybe_auto_login(decoded)
                self.last_activity = time.time()
        except Exception as exc:
            self._append_output(f"\n[telnet error] {exc}\n")
        finally:
            try:
                if self.sock is not None:
                    self.sock.close()
            except Exception:
                pass
            self.finalize("[telnet session closed]\n")

    def write(self, data):
        if self.closed or self.sock is None:
            raise RuntimeError("session closed")
        payload = data.replace("\n", "\r\n").encode("utf-8", errors="replace")
        self._send_bytes(payload)
        self.last_activity = time.time()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass
        self.data_event.set()


def terminal_cleanup_loop():
    while True:
        time.sleep(10)
        now = time.time()
        stale = []
        with terminal_lock:
            for session_id, session in terminal_sessions.items():
                if session.closed or now - session.last_activity > TERMINAL_IDLE_TIMEOUT_SECONDS:
                    stale.append(session_id)
            for session_id in stale:
                session = terminal_sessions.pop(session_id)
                session.close()


def update_cleanup_loop():
    while True:
        time.sleep(30)
        deadline = time.time() - UPDATE_JOB_RETENTION_SECONDS
        stale_jobs = []
        with update_lock:
            for job_id, job in update_jobs.items():
                finished_at = job.get("finished_at")
                if finished_at and finished_at < deadline:
                    stale_jobs.append(job_id)
            for job_id in stale_jobs:
                job = update_jobs.pop(job_id, None)
                if not job:
                    continue
                bundle_path = job.get("bundle_path")
                if bundle_path and os.path.exists(bundle_path):
                    try:
                        os.remove(bundle_path)
                    except OSError:
                        pass
                job_root = os.path.join(UPDATE_JOBS_ROOT, job_id)
                if os.path.isdir(job_root):
                    try:
                        for entry in os.listdir(job_root):
                            os.remove(os.path.join(job_root, entry))
                        os.rmdir(job_root)
                    except OSError:
                        pass


def create_terminal_session(protocol, host, username, port, password):
    if protocol == "ssh":
        return SshTerminalSession(host=host, username=username, port=port or 22, password=password)
    if protocol == "telnet":
        return TelnetTerminalSession(host=host, username=username, port=port or 23, password=password)
    raise ValueError("unsupported protocol")


def collect_paramiko_command_output(channel, timeout_seconds):
    chunks = []
    deadline = time.time() + timeout_seconds

    while True:
        received = False
        while channel.recv_ready():
            received = True
            chunks.append(channel.recv(4096).decode("utf-8", errors="replace"))
        while channel.recv_stderr_ready():
            received = True
            chunks.append(channel.recv_stderr(4096).decode("utf-8", errors="replace"))

        if channel.exit_status_ready():
            while channel.recv_ready():
                chunks.append(channel.recv(4096).decode("utf-8", errors="replace"))
            while channel.recv_stderr_ready():
                chunks.append(channel.recv_stderr(4096).decode("utf-8", errors="replace"))
            return channel.recv_exit_status(), tail_text(sanitize_terminal_text("".join(chunks)))

        if received:
            deadline = time.time() + timeout_seconds
        elif time.time() > deadline:
            channel.close()
            raise TimeoutError(f"remote command timeout after {timeout_seconds}s")
        else:
            time.sleep(0.08)


def build_remote_install_script(workdir, archive_path, login_user, use_sudo, sudo_password):
    workdir_q = shlex.quote(workdir)
    archive_q = shlex.quote(archive_path)
    bundle_dir_q = shlex.quote(os.path.join(workdir, "broadcast.axera_update"))
    login_user_q = shlex.quote(login_user or "root")
    use_sudo_q = shlex.quote("1" if use_sudo else "0")
    sudo_password_q = shlex.quote(sudo_password or "")

    return f"""set -eu
WORKDIR={workdir_q}
ARCHIVE={archive_q}
BUNDLE_DIR={bundle_dir_q}
LOGIN_USER={login_user_q}
USE_SUDO={use_sudo_q}
SUDO_PASSWORD={sudo_password_q}

extract_archive() {{
    if command -v tar >/dev/null 2>&1; then
        tar -xzf "$ARCHIVE" -C "$WORKDIR"
        return 0
    fi
    if command -v busybox >/dev/null 2>&1; then
        busybox tar -xzf "$ARCHIVE" -C "$WORKDIR"
        return 0
    fi
    echo "tar not found" >&2
    return 1
}}

run_install() {{
    cd "$BUNDLE_DIR"
    chmod +x install.sh build.sh >/dev/null 2>&1 || true
    export DEVICE_BROADCAST_USER="$LOGIN_USER"
    if [ "$(id -u)" -eq 0 ]; then
        sh ./install.sh
        return $?
    fi
    if [ "$USE_SUDO" = "1" ] && command -v sudo >/dev/null 2>&1; then
        if [ -n "$SUDO_PASSWORD" ]; then
            printf '%s\\n' "$SUDO_PASSWORD" | sudo -S env DEVICE_BROADCAST_USER="$DEVICE_BROADCAST_USER" sh ./install.sh
        else
            sudo env DEVICE_BROADCAST_USER="$DEVICE_BROADCAST_USER" sh ./install.sh
        fi
        return $?
    fi
    sh ./install.sh
}}

mkdir -p "$WORKDIR"
rm -rf "$BUNDLE_DIR"
extract_archive
run_install
"""


def build_telnet_fetch_script(workdir, archive_path, download_url):
    workdir_q = shlex.quote(workdir)
    archive_q = shlex.quote(archive_path)
    url_q = shlex.quote(download_url)

    return f"""set -eu
WORKDIR={workdir_q}
ARCHIVE={archive_q}
DOWNLOAD_URL={url_q}

mkdir -p "$WORKDIR"

download_archive() {{
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --connect-timeout 20 --max-time 1200 -o "$ARCHIVE" "$DOWNLOAD_URL"
        return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -O "$ARCHIVE" "$DOWNLOAD_URL"
        return 0
    fi
    if command -v busybox >/dev/null 2>&1; then
        busybox wget -O "$ARCHIVE" "$DOWNLOAD_URL"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$DOWNLOAD_URL" "$ARCHIVE" <<'__PY__'
import sys
from urllib.request import urlopen
url, path = sys.argv[1], sys.argv[2]
response = urlopen(url, timeout=60)
try:
    with open(path, "wb") as fh:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            fh.write(chunk)
finally:
    response.close()
__PY__
        return 0
    fi
    if command -v python >/dev/null 2>&1; then
        python - "$DOWNLOAD_URL" "$ARCHIVE" <<'__PY__'
import sys
from urllib2 import urlopen
url, path = sys.argv[1], sys.argv[2]
response = urlopen(url, timeout=60)
try:
    with open(path, "wb") as fh:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            fh.write(chunk)
finally:
    response.close()
__PY__
        return 0
    fi
    echo "no downloader available" >&2
    return 1
}}

download_archive
"""


class ScriptedTelnetClient:
    def __init__(self, host, port=23, username="", password=None):
        self.host = host
        self.port = port or 23
        self.username = username or ""
        self.password = password or ""
        self.sock = None
        self.pending = bytearray()
        self.auto_prompt_tail = ""
        self.username_sent = False
        self.password_sent = False

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=12)
        self.sock.settimeout(0.25)

    def close(self):
        if self.sock is None:
            return
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _send_bytes(self, payload):
        if self.sock is None:
            raise RuntimeError("telnet socket is closed")
        self.sock.sendall(payload)

    def write_text(self, text):
        payload = text.replace("\n", "\r\n").encode("utf-8", errors="replace")
        self._send_bytes(payload)

    def _negotiate(self, raw):
        data = bytes(self.pending) + raw
        self.pending = bytearray()
        output = bytearray()
        index = 0

        while index < len(data):
            byte = data[index]
            if byte != TELNET_IAC:
                output.append(byte)
                index += 1
                continue

            if index + 1 >= len(data):
                self.pending.extend(data[index:])
                break

            command = data[index + 1]
            if command == TELNET_IAC:
                output.append(TELNET_IAC)
                index += 2
            elif command in (TELNET_DO, TELNET_DONT, TELNET_WILL, TELNET_WONT):
                if index + 2 >= len(data):
                    self.pending.extend(data[index:])
                    break
                option = data[index + 2]
                if command == TELNET_DO:
                    self._send_bytes(bytes([TELNET_IAC, TELNET_WONT, option]))
                elif command == TELNET_WILL:
                    self._send_bytes(bytes([TELNET_IAC, TELNET_DONT, option]))
                index += 3
            elif command == TELNET_SB:
                end = data.find(bytes([TELNET_IAC, TELNET_SE]), index + 2)
                if end == -1:
                    self.pending.extend(data[index:])
                    break
                index = end + 2
            else:
                index += 2

        return bytes(output)

    def _maybe_auto_login(self, text):
        lower_tail = (self.auto_prompt_tail + text).lower()[-300:]
        if self.username and not self.username_sent and any(prompt in lower_tail for prompt in ("login:", "username:", "login as:")):
            self.write_text(self.username + "\n")
            self.username_sent = True
        if self.password and not self.password_sent and "password:" in lower_tail:
            self.write_text(self.password + "\n")
            self.password_sent = True
        self.auto_prompt_tail = lower_tail

    def _has_shell_prompt(self, text):
        if not text:
            return False
        return re.search(r"(?:^|\n)[^\n]{0,120}[#>$%] ?$", text) is not None

    def read_some_text(self):
        if self.sock is None:
            return ""
        try:
            raw = self.sock.recv(4096)
            if not raw:
                raise RuntimeError("telnet connection closed")
        except socket.timeout:
            return ""
        decoded_bytes = self._negotiate(raw)
        if not decoded_bytes:
            return ""
        try:
            text = decoded_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = decoded_bytes.decode("latin-1", errors="replace")
        text = sanitize_terminal_text(text)
        if text:
            self._maybe_auto_login(text)
        return text

    def wait_for_probe(self, timeout_seconds):
        marker = f"__BAX_READY_{uuid.uuid4().hex[:12]}__"
        probe_command = f"echo {shlex.quote(marker)}"
        transcript = []
        tail = ""
        deadline = time.time() + timeout_seconds
        last_nudge = 0.0
        probe_sent = False
        marker_re = re.compile(r"(?:^|\n)\s*" + re.escape(marker) + r"\s*(?:\n|$)")

        while time.time() < deadline:
            chunk = self.read_some_text()
            if chunk:
                transcript.append(chunk)
                tail = (tail + chunk)[-16000:]
                lower_tail = tail.lower()
                if "login incorrect" in lower_tail or "authentication failed" in lower_tail:
                    raise PermissionError("telnet authentication failed")
                if marker_re.search(tail):
                    return tail_text("".join(transcript))
                continue

            lower_tail = self.auto_prompt_tail
            waiting_for_username = any(prompt in lower_tail for prompt in ("login:", "username:", "login as:")) and not self.username_sent
            waiting_for_password = "password:" in lower_tail and not self.password_sent
            prompt_ready = self._has_shell_prompt(tail)

            if not waiting_for_username and not waiting_for_password and prompt_ready and not probe_sent:
                self.write_text(probe_command + "\n")
                probe_sent = True
                continue

            if self.password_sent and not waiting_for_username and not waiting_for_password and not prompt_ready:
                if time.time() - last_nudge >= 1.2:
                    self.write_text("\n")
                    last_nudge = time.time()
                time.sleep(0.06)
                continue

            time.sleep(0.06)

        raise TimeoutError("telnet login timeout or shell prompt not ready")

    def run_script(self, script_text, timeout_seconds):
        wrapper_path = f"/tmp/broadcast.axera_update_{uuid.uuid4().hex[:10]}.sh"
        heredoc = f"__BAX_SCRIPT_{uuid.uuid4().hex[:10]}__"
        marker = f"__BAX_DONE_{uuid.uuid4().hex[:12]}__"
        transcript = []
        tail = ""

        payload = "\n".join(
            [
                f"cat > {shlex.quote(wrapper_path)} <<'{heredoc}'",
                script_text.rstrip("\n"),
                heredoc,
                f"sh {shlex.quote(wrapper_path)}",
                "rc=$?",
                f"echo {marker}:$rc",
            ]
        )
        self.write_text(payload + "\n")

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            chunk = self.read_some_text()
            if chunk:
                transcript.append(chunk)
                tail = (tail + chunk)[-24000:]
                if marker in tail:
                    match = re.search(re.escape(marker) + r":(-?[0-9]+)", tail)
                    if not match:
                        raise RuntimeError("telnet command finished without exit code")
                    return int(match.group(1)), tail_text("".join(transcript))
            else:
                time.sleep(0.06)

        raise TimeoutError(f"telnet remote command timeout after {timeout_seconds}s")


def connect_ssh_client(host, username, port, password):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": host,
        "port": port or 22,
        "username": username,
        "timeout": 12,
        "banner_timeout": 12,
        "auth_timeout": 12,
    }
    if password:
        kwargs.update({"password": password, "allow_agent": False, "look_for_keys": False})
    else:
        kwargs.update({"allow_agent": True, "look_for_keys": True})
    client.connect(**kwargs)
    return client


def execute_update_over_ssh(target, ssh_config, sudo_config, bundle):
    username = clean_text(ssh_config.get("username")) or target.get("default_user") or "root"
    password = ssh_config.get("password") or None
    port = safe_int(ssh_config.get("port")) or 22
    use_sudo = bool(sudo_config.get("enabled"))
    sudo_password = sudo_config.get("password") or password or ""
    remote_workdir = f"/tmp/broadcast.axera_update_{uuid.uuid4().hex[:10]}"
    remote_archive = f"{remote_workdir}/device_broadcast_update.tar.gz"
    script = build_remote_install_script(
        workdir=remote_workdir,
        archive_path=remote_archive,
        login_user=username,
        use_sudo=use_sudo,
        sudo_password=sudo_password,
    )

    client = connect_ssh_client(target["ip"], username=username, port=port, password=password)
    try:
        _, stdout, _ = client.exec_command(f"mkdir -p {shlex.quote(remote_workdir)}")
        mkdir_rc, mkdir_output = collect_paramiko_command_output(stdout.channel, 20)
        if mkdir_rc != 0:
            raise RuntimeError(f"remote mkdir failed\n{mkdir_output}")

        sftp = client.open_sftp()
        try:
            sftp.put(bundle["archive_path"], remote_archive)
        finally:
            sftp.close()

        stdin, stdout, _ = client.exec_command("sh -s")
        stdin.write(script)
        stdin.flush()
        stdin.channel.shutdown_write()
        rc, output = collect_paramiko_command_output(stdout.channel, UPDATE_REMOTE_TIMEOUT_SECONDS)
        if rc != 0:
            raise RuntimeError(output or f"remote install exited with {rc}")
        return {
            "transport": "ssh",
            "message": "SSH 更新完成",
            "log": output,
        }
    finally:
        client.close()


def execute_update_over_telnet(target, telnet_config, sudo_config, bundle_url):
    username = clean_text(telnet_config.get("username"), "")
    password = telnet_config.get("password") or ""
    port = safe_int(telnet_config.get("port")) or 23
    use_sudo = bool(sudo_config.get("enabled"))
    sudo_password = sudo_config.get("password") or password or ""
    remote_workdir = f"/tmp/broadcast.axera_update_{uuid.uuid4().hex[:10]}"
    remote_archive = f"{remote_workdir}/device_broadcast_update.tar.gz"
    download_script = build_telnet_fetch_script(remote_workdir, remote_archive, bundle_url)
    install_script = build_remote_install_script(
        workdir=remote_workdir,
        archive_path=remote_archive,
        login_user=username or target.get("default_user") or "root",
        use_sudo=use_sudo,
        sudo_password=sudo_password,
    )

    client = ScriptedTelnetClient(target["ip"], port=port, username=username, password=password)
    try:
        client.connect()
        probe_output = client.wait_for_probe(25)
        download_rc, download_output = client.run_script(download_script, UPDATE_REMOTE_TIMEOUT_SECONDS)
        if download_rc != 0:
            raise RuntimeError((probe_output + "\n" + download_output).strip())
        install_rc, install_output = client.run_script(install_script, UPDATE_REMOTE_TIMEOUT_SECONDS)
        if install_rc != 0:
            raise RuntimeError((probe_output + "\n" + download_output + "\n" + install_output).strip())
        return {
            "transport": "telnet",
            "message": "Telnet 更新完成",
            "log": tail_text("\n".join([probe_output, download_output, install_output])),
        }
    finally:
        client.close()


def make_bundle_url(dashboard_origin, job_id, token):
    base = (dashboard_origin or "").rstrip("/")
    return f"{base}/api/update/jobs/{job_id}/package/{token}/device_broadcast_update.tar.gz"


def build_transport_order(strategy):
    if strategy == "ssh":
        return ["ssh"]
    if strategy == "telnet":
        return ["telnet"]
    return ["ssh", "telnet"]


def run_update_for_target(target, strategy, ssh_config, telnet_config, sudo_config, bundle, dashboard_origin, job_id):
    attempts = []
    bundle_url = make_bundle_url(dashboard_origin, job_id, bundle["token"])

    for transport in build_transport_order(strategy):
        try:
            if transport == "ssh":
                result = execute_update_over_ssh(target, ssh_config, sudo_config, bundle)
            else:
                result = execute_update_over_telnet(target, telnet_config, sudo_config, bundle_url)
            attempts.append({"transport": transport, "ok": True, "message": result["message"]})
            result["attempts"] = attempts
            return result
        except PasswordRequiredError:
            attempts.append({"transport": transport, "ok": False, "message": "需要密码"})
        except paramiko.AuthenticationException:
            attempts.append({"transport": transport, "ok": False, "message": "认证失败"})
        except Exception as exc:
            attempts.append({"transport": transport, "ok": False, "message": str(exc)})

    combined = "\n\n".join(f"[{item['transport']}] {item['message']}" for item in attempts)
    raise RuntimeError(combined or "all update transports failed")


def run_update_job(job_id, targets, strategy, parallelism, ssh_config, telnet_config, sudo_config, dashboard_origin):
    try:
        bundle = build_update_bundle(job_id)
    except Exception as exc:
        mark_update_job_failed(job_id, str(exc))
        return

    with update_lock:
        job = update_jobs.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = time.time()
        job["bundle_ready"] = True
        job["bundle_version"] = bundle["version"]
        job["build_output"] = bundle["build_output"]
        job["bundle_token"] = bundle["token"]
        job["bundle_path"] = bundle["archive_path"]

    max_workers = max(1, min(parallelism, len(targets), UPDATE_MAX_PARALLEL))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        for target in targets:
            update_job_target_state(
                job_id,
                target["ip"],
                status="running",
                message="正在更新",
                started_at=time.time(),
                finished_at=None,
                transport=None,
                attempts=[],
                log="",
            )
            future = executor.submit(
                run_update_for_target,
                target,
                strategy,
                dict(ssh_config),
                dict(telnet_config),
                dict(sudo_config),
                dict(bundle),
                dashboard_origin,
                job_id,
            )
            future_map[future] = target

        for future in as_completed(future_map):
            target = future_map[future]
            try:
                result = future.result()
                update_job_target_state(
                    job_id,
                    target["ip"],
                    status="success",
                    transport=result["transport"],
                    message=result["message"],
                    finished_at=time.time(),
                    attempts=result.get("attempts", []),
                    log=result.get("log", ""),
                )
            except Exception as exc:
                message = str(exc) or "更新失败"
                attempts = []
                lines = [line.strip() for line in message.splitlines() if line.strip()]
                for line in lines:
                    if line.startswith("[") and "]" in line:
                        transport, _, detail = line[1:].partition("]")
                        attempts.append({"transport": transport, "ok": False, "message": detail.strip()})
                update_job_target_state(
                    job_id,
                    target["ip"],
                    status="failed",
                    transport=None,
                    message=lines[-1] if lines else "更新失败",
                    finished_at=time.time(),
                    attempts=attempts,
                    log=tail_text(message),
                )

    finish_update_job(job_id)


@app.route("/")
def index():
    dashboard_host = request.host.split(":", 1)[0]
    webssh2_url_template = WEBSSH2_URL_TEMPLATE.replace("{dashboard_host}", dashboard_host)
    return render_template(
        "index.html",
        webssh2_enabled=WEBSSH2_ENABLED,
        webssh2_url_template=webssh2_url_template,
        update_max_parallel=UPDATE_MAX_PARALLEL,
    )


@app.route("/api/devices/metadata", methods=["POST"])
def api_device_metadata():
    body = request.get_json(silent=True) or {}
    ip_address = clean_text(body.get("ip"))
    if not ip_address:
        return jsonify({"error": "missing_ip"}), 400

    title = normalize_metadata_text(body.get("title"), 80)
    note = normalize_metadata_text(body.get("note"), 400)

    with device_lock:
        device = online_devices.get(ip_address)
        if not device:
            return jsonify({"error": "device_not_found"}), 404
        metadata_keys = device_metadata_keys_for_payload(device, ip_address)

    primary_key = metadata_keys[0] if metadata_keys else make_device_metadata_key("ip", ip_address)
    updated_at = now_iso()

    with metadata_lock:
        for key in metadata_keys:
            device_metadata.pop(key, None)
        if title or note:
            device_metadata[primary_key] = {
                "title": title,
                "note": note,
                "updated_at": updated_at,
            }
        persist_device_metadata_records_locked()

    with device_lock:
        device = online_devices.get(ip_address)
        if not device:
            return jsonify({"ok": True, "device": None})
        apply_device_metadata(device)
        payload = serialize_device(device)

    return jsonify({"ok": True, "device": payload})


@app.route("/api/devices")
def api_devices():
    with device_lock:
        devices = [serialize_device(device) for _, device in sorted(online_devices.items(), key=lambda item: item[0])]
    return jsonify(
        {
            "generated_at": int(time.time() * 1000),
            "summary": build_summary(devices),
            "devices": devices,
        }
    )


@app.route("/api/terminal/start", methods=["POST"])
def api_terminal_start():
    body = request.get_json(silent=True) or {}
    host = clean_text(body.get("host"))
    username = clean_text(body.get("username"), "root") if body.get("username") is not None else "root"
    protocol = clean_text(body.get("protocol"), "ssh").lower()
    default_port = 23 if protocol == "telnet" else 22
    port = safe_int(body.get("port")) or default_port
    password = body.get("password") or None

    if not host:
        return jsonify({"error": "missing_host"}), 400
    if protocol not in ("ssh", "telnet"):
        return jsonify({"error": "unsupported_protocol"}), 400

    try:
        session = create_terminal_session(protocol=protocol, host=host, username=username, port=port, password=password)
    except PasswordRequiredError:
        return jsonify({"error": "password_required"}), 400
    except paramiko.AuthenticationException:
        return jsonify({"error": "auth_failed", "needs_password": True}), 401
    except Exception as exc:
        return jsonify({"error": "connect_failed", "message": str(exc)}), 500

    session_id = uuid.uuid4().hex
    with terminal_lock:
        terminal_sessions[session_id] = session

    return jsonify(
        {
            "session_id": session_id,
            "cursor": 0,
            "username": username,
            "host": host,
            "port": port,
            "protocol": protocol,
        }
    )


@app.route("/api/terminal/<session_id>/poll")
def api_terminal_poll(session_id):
    cursor = safe_int(request.args.get("cursor")) or 0
    wait_ms = safe_int(request.args.get("wait_ms")) or POLL_WAIT_MS
    with terminal_lock:
        session = terminal_sessions.get(session_id)
    if not session:
        return jsonify({"error": "session_not_found"}), 404
    return jsonify(session.poll(cursor, wait_ms=wait_ms))


@app.route("/api/terminal/<session_id>/input", methods=["POST"])
def api_terminal_input(session_id):
    body = request.get_json(silent=True) or {}
    data = body.get("data", "")
    cols = safe_int(body.get("cols"))
    rows = safe_int(body.get("rows"))

    with terminal_lock:
        session = terminal_sessions.get(session_id)
    if not session:
        return jsonify({"error": "session_not_found"}), 404

    try:
        if cols and rows:
            session.resize(cols, rows)
        if data:
            session.write(data)
    except Exception as exc:
        return jsonify({"error": "write_failed", "message": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/terminal/<session_id>/close", methods=["POST"])
def api_terminal_close(session_id):
    with terminal_lock:
        session = terminal_sessions.pop(session_id, None)
    if session:
        session.close()
    return jsonify({"ok": True})


@app.route("/api/update/jobs", methods=["POST"])
def api_update_create_job():
    body = request.get_json(silent=True) or {}
    target_ips = normalize_target_hosts(body.get("targets"))
    manual_hosts = normalize_target_hosts(body.get("manual_targets"))
    strategy = clean_text(body.get("strategy"), "auto").lower()
    if strategy not in ("auto", "ssh", "telnet"):
        return jsonify({"error": "unsupported_strategy"}), 400
    if not target_ips and not manual_hosts:
        return jsonify({"error": "missing_targets"}), 400

    ssh_body = body.get("ssh") if isinstance(body.get("ssh"), dict) else {}
    telnet_body = body.get("telnet") if isinstance(body.get("telnet"), dict) else {}
    sudo_body = body.get("sudo") if isinstance(body.get("sudo"), dict) else {}
    manual_default_user = (
        clean_text(ssh_body.get("username"))
        or clean_text(telnet_body.get("username"))
        or "root"
    )

    online_devices = get_online_devices_by_ip()
    targets = []
    missing = []
    seen = set()
    for ip_address in target_ips:
        if ip_address in seen:
            continue
        seen.add(ip_address)
        device = online_devices.get(ip_address)
        if not device:
            missing.append(ip_address)
            continue
        targets.append(create_update_target_snapshot(device))

    for host in manual_hosts:
        if host in seen:
            continue
        seen.add(host)
        device = online_devices.get(host)
        if device:
            targets.append(create_update_target_snapshot(device))
        else:
            targets.append(create_manual_target_snapshot(host, manual_default_user))

    if missing:
        return jsonify({"error": "devices_offline", "missing": missing}), 400
    if not targets:
        return jsonify({"error": "missing_targets"}), 400

    parallelism = clamp_int(body.get("parallelism"), 1, UPDATE_MAX_PARALLEL, min(len(targets), UPDATE_MAX_PARALLEL))

    dashboard_origin = clean_text(body.get("dashboard_origin"), request.host_url.rstrip("/"))
    parsed_origin = urlsplit(dashboard_origin)
    if not parsed_origin.scheme or not parsed_origin.netloc:
        dashboard_origin = request.host_url.rstrip("/")

    job_id = uuid.uuid4().hex
    job = build_update_job_public(job_id, targets, strategy, parallelism)
    os.makedirs(UPDATE_JOBS_ROOT, exist_ok=True)
    with update_lock:
        update_jobs[job_id] = job

    worker = threading.Thread(
        target=run_update_job,
        args=(
            job_id,
            targets,
            strategy,
            parallelism,
            {
                "username": clean_text(ssh_body.get("username"), ""),
                "password": ssh_body.get("password") or "",
                "port": safe_int(ssh_body.get("port")) or 22,
            },
            {
                "username": clean_text(telnet_body.get("username"), ""),
                "password": telnet_body.get("password") or "",
                "port": safe_int(telnet_body.get("port")) or 23,
            },
            {
                "enabled": bool(sudo_body.get("enabled")),
                "password": sudo_body.get("password") or "",
            },
            dashboard_origin,
        ),
        daemon=True,
    )
    worker.start()
    return jsonify(serialize_update_job(job))


@app.route("/api/update/jobs/<job_id>")
def api_update_job(job_id):
    with update_lock:
        job = update_jobs.get(job_id)
        if not job:
            return jsonify({"error": "job_not_found"}), 404
        payload = serialize_update_job(job)
    return jsonify(payload)


@app.route("/api/update/jobs/<job_id>/package/<token>/device_broadcast_update.tar.gz")
def api_update_bundle(job_id, token):
    with update_lock:
        job = update_jobs.get(job_id)
        if not job:
            abort(404)
        if token != job.get("bundle_token"):
            abort(404)
        archive_path = job.get("bundle_path")
        if not archive_path or not os.path.exists(archive_path):
            abort(404)
    return send_file(archive_path, as_attachment=True, download_name="device_broadcast_update.tar.gz")


def main():
    threading.Thread(target=udp_listener, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    threading.Thread(target=terminal_cleanup_loop, daemon=True).start()
    threading.Thread(target=update_cleanup_loop, daemon=True).start()
    port = int(os.getenv("DASHBOARD_PORT", "25000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
