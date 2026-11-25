import csv
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import psutil


def probe(argv, timeout=4):
    if not shutil.which(argv[0]):
        return {"error": f"{argv[0]} is unavailable"}
    try:
        result = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=False)
        if result.returncode:
            return {"error": result.stderr.strip() or f"{argv[0]} exited {result.returncode}"}
        return {"output": result.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": str(error)}


def os_release():
    try:
        return platform.freedesktop_os_release()
    except OSError:
        return {}


def session_type():
    session = os.environ.get("XDG_SESSION_TYPE")
    if session in {"wayland", "x11"}:
        return session
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "headless"


def cpu_info():
    try:
        with Path("/proc/cpuinfo").open() as details:
            fields = {}
            for line in details:
                key, separator, value = line.partition(":")
                if not separator:
                    break
                fields[key.strip()] = value.strip()
    except OSError:
        fields = {}
    return {"name": fields.get("model name") or fields.get("Hardware") or platform.machine(),
            "vendor": fields.get("vendor_id"),
            "physical_cores": psutil.cpu_count(logical=False), "threads": psutil.cpu_count(),
            "flags": (fields.get("flags") or fields.get("Features", "")).split()}


def gpu_info():
    cols = ("name", "driver_version", "memory.total", "memory.used", "memory.free",
            "utilization.gpu", "temperature.gpu", "power.draw", "clocks.gr", "fan.speed")
    queried = probe(["nvidia-smi", "--query-gpu=" + ",".join(cols), "--format=csv,noheader,nounits"])
    if "output" in queried:
        rows = []
        for line in csv.reader(queried["output"].splitlines(), skipinitialspace=True):
            if len(line) != len(cols):
                continue
            values = [None if value.startswith("[") or value == "N/A" else value for value in line]
            rows.append(dict(zip(cols, values)))
        return {"devices": rows, "names": [row["name"] for row in rows], "error": None,
                "driver_report": probe(["nvidia-smi"], timeout=3).get("output", "").splitlines()[:4]}
    pci = probe(["lspci"])
    displays = [line for line in pci.get("output", "").splitlines()
                if any(term in line for term in ("VGA", "Display controller", "3D controller"))]
    return {"devices": [], "names": displays, "error": queried.get("error"), "pci_error": pci.get("error")}


def process_memory():
    memory = {"BudAI": psutil.Process().memory_info().rss, "Ollama": None, "ComfyUI": None}
    for process in psutil.process_iter(["name", "cmdline", "memory_info"]):
        try:
            row = process.info
            args = row.get("cmdline") or []
            name = "Ollama" if row["name"] == "ollama" else None
            if any("comfyui" in part.lower() and part.endswith(".py") for part in args[:3]):
                name = "ComfyUI"
            if name and row.get("memory_info"):
                memory[name] = (memory[name] or 0) + row["memory_info"].rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return memory


def disk_totals(directory):
    info = shutil.disk_usage(directory)
    return {"bytes_total": info.total, "bytes_used": info.used, "bytes_free": info.free}


def private_size(directory):
    if not directory.is_dir():
        return 0
    total = 0
    for base, _, files in os.walk(directory, followlinks=False):
        for name in files:
            try:
                total += (Path(base) / name).lstat().st_size
            except FileNotFoundError:
                continue
    return total


def profile(directory):
    return {
        "os": os_release().get("PRETTY_NAME"), "kernel": platform.release(),
        "cpu": cpu_info(), "gpu": gpu_info(),
        "ram_bytes": psutil.virtual_memory().total, "swap_bytes": psutil.swap_memory().total,
        "storage": disk_totals(directory), "block_devices": probe(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE"]),
        "session": session_type(), "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        "displays": probe(["xrandr", "--listmonitors"]),
        "microphones": probe(["pactl", "list", "short", "sources"]),
        "speakers": probe(["pactl", "list", "short", "sinks"]),
        "network_interfaces": list(psutil.net_if_addrs()),
    }


def write_profile(directory):
    info = profile(directory)
    path = directory / "hardware.json"
    temporary = directory / f".hardware-{os.getpid()}-{time.monotonic_ns()}"
    try:
        temporary.write_text(json.dumps(info, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return info


class Monitor:
    def __init__(self, directory):
        self.directory = directory
        self.last_network = {}
        self.cached_gpu = (0, None)

    def snapshot(self):
        now = time.monotonic()
        rates = {}
        for name, counters in psutil.net_io_counters(pernic=True).items():
            old = self.last_network.get(name)
            self.last_network[name] = (now, counters.bytes_recv, counters.bytes_sent)
            if old and now > old[0]:
                rates[name] = {"receive_bytes_s": max(0, round((counters.bytes_recv - old[1]) / (now - old[0]))),
                               "send_bytes_s": max(0, round((counters.bytes_sent - old[2]) / (now - old[0])))}
        if now - self.cached_gpu[0] > 3:
            self.cached_gpu = (now, gpu_info())
        ram = psutil.virtual_memory()
        swap = psutil.swap_memory()
        temperatures = getattr(psutil, "sensors_temperatures", lambda: {})()
        frequency = psutil.cpu_freq()
        cores = psutil.cpu_percent(interval=0.12, percpu=True)
        return {
            "cpu": dict(cpu_info(), utilization=sum(cores) / len(cores) if cores else None,
                        per_core=cores, frequency_mhz=frequency.current if frequency else None),
            "temperatures": {kind: [{"label": value.label, "celsius": value.current} for value in values]
                             for kind, values in temperatures.items()},
            "gpu": self.cached_gpu[1],
            "memory": {"total_bytes": ram.total, "used_bytes": ram.used, "available_bytes": ram.available,
                       "swap_total_bytes": swap.total, "swap_used_bytes": swap.used,
                       "processes_bytes": process_memory()},
            "storage": dict(disk_totals(self.directory),
                            database_bytes=(self.directory / "bud.sqlite3").stat().st_size
                            if (self.directory / "bud.sqlite3").exists() else 0,
                            media_bytes=private_size(self.directory / "artifacts"),
                            logs_bytes=private_size(self.directory / "logs")),
            "network": rates, "session": session_type(),
        }
