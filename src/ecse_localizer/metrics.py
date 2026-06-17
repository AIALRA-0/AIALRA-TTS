from __future__ import annotations

import ctypes
import shutil
import subprocess
from pathlib import Path
from typing import Any


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def collect_system_metrics(config: dict[str, Any]) -> dict[str, Any]:
    output_dir = Path(config.get("output_dir", "."))
    disk = shutil.disk_usage(disk_usage_root(output_dir))
    return {
        "cpu": collect_cpu_metrics(),
        "memory": collect_memory_metrics(),
        "gpu": collect_gpu_metrics(),
        "disk": {
            "scope": "output_volume",
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "used_percent": round((disk.used / disk.total) * 100, 2) if disk.total else 0,
        },
        "local_storage": collect_local_storage_metrics(config),
    }


def disk_usage_root(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current if current.exists() else Path(".")


def collect_local_storage_metrics(config: dict[str, Any]) -> dict[str, Any]:
    worker_cfg = config.get("worker", {}) if isinstance(config.get("worker"), dict) else {}
    include_input = bool(worker_cfg.get("report_input_storage_usage", False))
    max_files = max(1000, int(worker_cfg.get("local_storage_scan_max_files", 250000) or 250000))
    roots = []
    if config.get("output_dir"):
        roots.append(("output", Path(config["output_dir"])))
    if config.get("work_dir"):
        roots.append(("work", Path(config["work_dir"])))
    if include_input and config.get("input_dir"):
        roots.insert(0, ("input", Path(config.get("input_dir", "."))))
    rows: list[dict[str, Any]] = []
    remaining_files = max_files
    for label, root in roots:
        if not root.exists():
            rows.append({"label": label, "exists": False, "bytes": 0, "file_count": 0, "partial": False})
            continue
        usage = directory_usage_limited(root, remaining_files)
        remaining_files = max(0, remaining_files - int(usage["file_count"]))
        rows.append({"label": label, "exists": True, **usage})
    managed_bytes = sum(int(row.get("bytes", 0) or 0) for row in rows if row.get("label") != "input")
    total_bytes = sum(int(row.get("bytes", 0) or 0) for row in rows)
    return {
        "managed_bytes": managed_bytes,
        "total_reported_bytes": total_bytes,
        "roots": rows,
        "input_included": include_input,
        "scan_max_files": max_files,
        "partial": any(bool(row.get("partial")) for row in rows),
    }


def directory_usage_limited(root: Path, max_files: int) -> dict[str, Any]:
    total = 0
    count = 0
    partial = False
    try:
        for child in root.rglob("*"):
            if not child.is_file():
                continue
            if count >= max_files:
                partial = True
                break
            try:
                total += child.stat().st_size
                count += 1
            except OSError:
                partial = True
    except OSError:
        partial = True
    return {"bytes": total, "file_count": count, "partial": partial}


def sanitize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Remove filesystem paths from metrics before storing/sending remotely."""
    if not isinstance(metrics, dict):
        return {}
    return sanitize_metric_value(metrics)


def sanitize_metric_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitize_metric_value(item)
            for key, item in value.items()
            if str(key).lower() not in {"path", "input_dir", "output_dir", "work_dir"}
        }
    if isinstance(value, list):
        return [sanitize_metric_value(item) for item in value]
    return value


def collect_cpu_metrics() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
        value = float((proc.stdout or "0").strip() or 0)
    except Exception:
        value = 0.0
    return {"load_percent": round(value, 2)}


def collect_memory_metrics() -> dict[str, Any]:
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        used = int(stat.ullTotalPhys - stat.ullAvailPhys)
        return {
            "total_bytes": int(stat.ullTotalPhys),
            "used_bytes": used,
            "available_bytes": int(stat.ullAvailPhys),
            "used_percent": round(float(stat.dwMemoryLoad), 2),
        }
    except Exception:
        return {"total_bytes": 0, "used_bytes": 0, "available_bytes": 0, "used_percent": 0.0}


def collect_gpu_metrics() -> list[dict[str, Any]]:
    query = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except Exception as exc:
        return [{"available": False, "error": str(exc)}]
    if proc.returncode != 0:
        return [{"available": False, "error": (proc.stderr or proc.stdout).strip()}]
    rows = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        mem_used = to_float(parts[3])
        mem_total = to_float(parts[4])
        rows.append(
            {
                "available": True,
                "index": int(to_float(parts[0])),
                "name": parts[1],
                "util_percent": to_float(parts[2]),
                "memory_used_mb": mem_used,
                "memory_total_mb": mem_total,
                "memory_used_percent": round((mem_used / mem_total) * 100, 2) if mem_total else 0,
                "temperature_c": to_float(parts[5]),
                "power_w": to_float(parts[6]),
            }
        )
    return rows or [{"available": False, "error": "nvidia-smi returned no GPU rows"}]


def to_float(value: str) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0
