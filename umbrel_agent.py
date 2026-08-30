#!/usr/bin/env python3
from __future__ import annotations

import glob
import json
import os
import platform
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PB_URL = os.environ.get("GESTORE_PB_URL", "https://api.gestore.website").rstrip("/")
PB_EMAIL = os.environ.get("GESTORE_PB_EMAIL", "").strip()
PB_PASSWORD = os.environ.get("GESTORE_PB_PASSWORD", "")
PB_TOKEN = os.environ.get("GESTORE_PB_TOKEN", "").strip()
NODE_NAME = os.environ.get("GESTORE_UMBREL_NODE", "umbrel").strip() or "umbrel"
INTERVAL = max(5, int(os.environ.get("GESTORE_TELEMETRY_INTERVAL", "15") or 15))
HOST_PROC = Path(os.environ.get("GESTORE_HOST_PROC", "/host/proc"))
HOST_SYS = Path(os.environ.get("GESTORE_HOST_SYS", "/host/sys"))
HOST_ROOT = Path(os.environ.get("GESTORE_HOST_ROOT", "/host/root"))
APP_DATA = Path(os.environ.get("GESTORE_DATA_DIR", "/data"))
COLLECTION = "server_telemetry"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%fZ")


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(errors="ignore").strip()
    except Exception:
        return default


def request(path: str, *, method: str = "GET", body=None, token: str = ""):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(PB_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {"message": raw.decode("utf-8", "ignore")}
        return exc.code, payload


def auth_token() -> str:
    if PB_TOKEN:
        return PB_TOKEN
    if not PB_EMAIL or not PB_PASSWORD:
        raise RuntimeError("Configura GESTORE_PB_EMAIL/GESTORE_PB_PASSWORD oppure GESTORE_PB_TOKEN")
    status, payload = request(
        "/api/collections/_superusers/auth-with-password",
        method="POST",
        body={"identity": PB_EMAIL, "password": PB_PASSWORD},
    )
    if status >= 300 or not payload.get("token"):
        raise RuntimeError(f"Autenticazione PocketBase fallita: HTTP {status} {payload}")
    return str(payload["token"])


def ensure_collection(token: str) -> None:
    status, _ = request(f"/api/collections/{COLLECTION}", token=token)
    if status < 300:
        return
    if status != 404:
        raise RuntimeError(f"Impossibile leggere collection {COLLECTION}: HTTP {status}")
    schema = {
        "name": COLLECTION,
        "type": "base",
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "fields": [
            {"name": "node", "type": "text", "required": True, "max": 80},
            {"name": "captured_at", "type": "date", "required": True},
            {"name": "payload", "type": "json", "required": True, "maxSize": 1048576},
        ],
        "indexes": ["CREATE UNIQUE INDEX idx_server_telemetry_node ON server_telemetry (node)"],
    }
    status, payload = request("/api/collections", method="POST", body=schema, token=token)
    if status >= 300:
        raise RuntimeError(f"Creazione collection {COLLECTION} fallita: HTTP {status} {payload}")


def cpu_snapshot():
    raw = read_text(HOST_PROC / "stat")
    first = raw.splitlines()[0].split() if raw else []
    if len(first) < 5 or first[0] != "cpu":
        return None
    vals = [int(x) for x in first[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    return total, idle


def cpu_model_and_cores():
    text = read_text(HOST_PROC / "cpuinfo")
    model = ""
    cores = 0
    mhz = []
    for line in text.splitlines():
        if line.startswith("processor"):
            cores += 1
        elif not model and (line.startswith("model name") or line.startswith("Hardware")):
            model = line.split(":", 1)[-1].strip()
        elif line.startswith("cpu MHz"):
            try:
                mhz.append(float(line.split(":", 1)[-1].strip()))
            except Exception:
                pass
    return model or platform.processor() or "CPU", cores or (os.cpu_count() or 0), round(sum(mhz) / len(mhz), 1) if mhz else None


def mem_stats():
    values = {}
    for line in read_text(HOST_PROC / "meminfo").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        try:
            values[k] = int(v.strip().split()[0]) * 1024
        except Exception:
            continue
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    pct = (used / total * 100.0) if total else 0.0
    return total, used, available, pct, values.get("Cached", 0)


def net_totals():
    rx = tx = 0
    for line in read_text(HOST_PROC / "net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        if name.strip() == "lo":
            continue
        parts = rest.split()
        if len(parts) >= 9:
            rx += int(parts[0])
            tx += int(parts[8])
    return rx, tx


def disk_stats(path: Path):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        available = st.f_bavail * st.f_frsize
        used = max(0, total - available)
        pct = (used / total * 100.0) if total else 0.0
        return total, used, available, pct
    except Exception:
        return 0, 0, 0, 0.0


def temperature_c():
    values = []
    for pattern in (str(HOST_SYS / "class/thermal/thermal_zone*/temp"), str(HOST_SYS / "class/hwmon/hwmon*/temp*_input")):
        for filename in glob.glob(pattern):
            try:
                raw = float(Path(filename).read_text().strip())
                value = raw / 1000.0 if raw > 200 else raw
                if 0 < value < 150:
                    values.append(value)
            except Exception:
                pass
    return round(max(values), 1) if values else None


def process_count():
    try:
        return sum(1 for p in HOST_PROC.iterdir() if p.name.isdigit())
    except Exception:
        return 0


def host_info():
    hostname = read_text(HOST_PROC / "sys/kernel/hostname", socket.gethostname())
    kernel = read_text(HOST_PROC / "sys/kernel/osrelease", platform.release())
    os_name = read_text(HOST_ROOT / "etc/os-release")
    pretty = "Linux"
    for line in os_name.splitlines():
        if line.startswith("PRETTY_NAME="):
            pretty = line.split("=", 1)[1].strip().strip('"')
            break
    return hostname, kernel, pretty


def uptime_seconds():
    try:
        return float(read_text(HOST_PROC / "uptime").split()[0])
    except Exception:
        return 0.0


def load_avg():
    try:
        parts = read_text(HOST_PROC / "loadavg").split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except Exception:
        return []


def collect(prev_cpu=None, prev_net=None, prev_time=None):
    now = time.monotonic()
    current_cpu = cpu_snapshot()
    cpu_pct = 0.0
    if prev_cpu and current_cpu:
        total_delta = current_cpu[0] - prev_cpu[0]
        idle_delta = current_cpu[1] - prev_cpu[1]
        if total_delta > 0:
            cpu_pct = max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))
    total_mem, used_mem, avail_mem, mem_pct, cache_mem = mem_stats()
    current_net = net_totals()
    elapsed = max(0.001, now - prev_time) if prev_time else 1.0
    rx_s = max(0.0, (current_net[0] - prev_net[0]) / elapsed) if prev_net else 0.0
    tx_s = max(0.0, (current_net[1] - prev_net[1]) / elapsed) if prev_net else 0.0
    disk_total, disk_used, disk_avail, disk_pct = disk_stats(HOST_ROOT)
    data_total, data_used, data_avail, data_pct = disk_stats(APP_DATA)
    model, cores, mhz = cpu_model_and_cores()
    hostname, kernel, os_name = host_info()
    payload = {
        "telemetry_available": True,
        "source": "umbrel-agent",
        "node": NODE_NAME,
        "timestamp": utc_now(),
        "hostname": hostname,
        "os": os_name,
        "kernel": kernel,
        "arch": platform.machine(),
        "uptime_seconds": round(uptime_seconds()),
        "temperature_c": temperature_c(),
        "process_count": process_count(),
        "cpu": {
            "percent": round(cpu_pct, 1),
            "cores": cores,
            "model": model,
            "frequency_mhz": mhz,
            "load_avg": load_avg(),
        },
        "ram": {
            "percent": round(mem_pct, 1),
            "total_bytes": total_mem,
            "used_bytes": used_mem,
            "available_bytes": avail_mem,
            "cache_bytes": cache_mem,
            "total_gb": round(total_mem / 1073741824, 2),
            "used_gb": round(used_mem / 1073741824, 2),
            "available_gb": round(avail_mem / 1073741824, 2),
        },
        "disk": {
            "percent": round(disk_pct, 1),
            "total_bytes": disk_total,
            "used_bytes": disk_used,
            "available_bytes": disk_avail,
            "total_gb": round(disk_total / 1073741824, 2),
            "used_gb": round(disk_used / 1073741824, 2),
            "available_gb": round(disk_avail / 1073741824, 2),
        },
        "gestore_data_disk": {
            "percent": round(data_pct, 1),
            "total_bytes": data_total,
            "used_bytes": data_used,
            "available_bytes": data_avail,
        },
        "network": {
            "rx_bytes": current_net[0],
            "tx_bytes": current_net[1],
            "rx_bytes_s": round(rx_s),
            "tx_bytes_s": round(tx_s),
        },
    }
    return payload, current_cpu, current_net, now


def upsert(token: str, payload: dict) -> None:
    filter_expr = urllib.parse.quote(f'node="{NODE_NAME}"')
    status, data = request(
        f"/api/collections/{COLLECTION}/records?perPage=1&filter={filter_expr}", token=token
    )
    if status >= 300:
        raise RuntimeError(f"Lettura telemetry fallita: HTTP {status} {data}")
    items = data.get("items") or []
    body = {"node": NODE_NAME, "captured_at": utc_now(), "payload": payload}
    if items:
        record_id = items[0]["id"]
        status, data = request(
            f"/api/collections/{COLLECTION}/records/{record_id}", method="PATCH", body=body, token=token
        )
    else:
        status, data = request(f"/api/collections/{COLLECTION}/records", method="POST", body=body, token=token)
    if status >= 300:
        raise RuntimeError(f"Scrittura telemetry fallita: HTTP {status} {data}")


def main() -> None:
    print(f"[umbrel-agent] node={NODE_NAME} pocketbase={PB_URL} interval={INTERVAL}s", flush=True)
    token = ""
    prev_cpu = prev_net = prev_time = None
    while True:
        try:
            if not token:
                token = auth_token()
                ensure_collection(token)
                print("[umbrel-agent] PocketBase collegato", flush=True)
            payload, prev_cpu, prev_net, prev_time = collect(prev_cpu, prev_net, prev_time)
            upsert(token, payload)
            print(
                f"[umbrel-agent] CPU {payload['cpu']['percent']}% · RAM {payload['ram']['percent']}% · DISK {payload['disk']['percent']}%",
                flush=True,
            )
        except Exception as exc:
            print(f"[umbrel-agent] errore: {exc}", flush=True)
            token = ""
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
