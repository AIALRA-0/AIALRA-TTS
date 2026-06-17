from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def now_id(prefix: str = "run") -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"


def slugify(text: str, max_len: int = 96) -> str:
    stem = Path(text).stem
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    cleaned = cleaned[:max_len].strip("._ ")
    digest = hashlib.sha1(stem.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"{cleaned}_{digest}" if cleaned else digest


def setup_logger(name: str, log_path: str | Path) -> logging.Logger:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(formatter)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def run_cmd(
    cmd: list[str],
    *,
    logger: logging.Logger | None = None,
    cwd: str | Path | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    if logger:
        logger.info("RUN %s", " ".join(quote_arg(c) for c in cmd))
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )
    if logger and proc.stdout:
        logger.info("STDOUT %s", proc.stdout.strip()[:4000])
    if logger and proc.stderr:
        logger.info("STDERR %s", proc.stderr.strip()[:4000])
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
    return proc


def quote_arg(arg: str) -> str:
    return f'"{arg}"' if any(ch.isspace() for ch in arg) else arg


def write_json(path: str | Path, data: Any) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def copy_text(src: str | Path, dst: str | Path) -> None:
    Path(dst).write_text(Path(src).read_text(encoding="utf-8", errors="replace"), encoding="utf-8")


def which(name: str) -> str | None:
    from shutil import which as _which

    return _which(name)


def count_files(root: str | Path, suffixes: Iterable[str]) -> int:
    suffix_set = {s.lower() for s in suffixes}
    return sum(1 for p in Path(root).rglob("*") if p.is_file() and p.suffix.lower() in suffix_set)


def path_for_ffmpeg_filter(path: str | Path) -> str:
    s = str(Path(path).resolve()).replace("\\", "/")
    s = s.replace(":", r"\:")
    return s.replace("'", r"\'")
