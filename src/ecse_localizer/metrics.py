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
    disk = shutil.disk_usage(output_dir if output_dir.exists() else output_dir.parent)
    return {
        "cpu": collect_cpu_metrics(),
        "memory": collect_memory_metrics(),
        "gpu": collect_gpu_metrics(),
        "disk": {
            "path": str(output_dir),
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "used_percent": round((disk.used / disk.total) * 100, 2) if disk.total else 0,
        },
    }


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
