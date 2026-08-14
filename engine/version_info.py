"""KataGo version 信息探测（进程外一次性跑 version 命令，避免阻塞分析服务）。"""
import os
import re
import subprocess
from typing import Optional, Dict

import config


_VERSION_CACHE = None  # dict or None


def _run_version(exe_path: str, cwd: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(
            [exe_path, "version"],
            cwd=cwd, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return (r.stdout or "") + "\n" + (r.stderr or "")
    except Exception:
        return ""


_VERSION_LINE_RE = re.compile(r"KataGo\s+v([\w\.\-]+)")
_BACKEND_RE = re.compile(r"Using\s+([\w\+]+)\s+backend")
_CUDA_RE = re.compile(r"Compiled\s+with\s+CUDA\s+version\s+([\w\.]+)")


def probe_katago_version(force: bool = False) -> Dict[str, Optional[str]]:
    """返回 {version, backend, cuda_build}。探测失败/不可用返回空值，结果进程内缓存。"""
    global _VERSION_CACHE
    if (not force) and _VERSION_CACHE is not None:
        return _VERSION_CACHE
    info: Dict[str, Optional[str]] = {
        "version": None,
        "backend": None,
        "cuda_build": None,
    }
    exe = config.KATAGO_EXE
    if not (os.path.isfile(exe) and config.katago_available()):
        _VERSION_CACHE = info
        return info
    out = _run_version(exe, config.KATAGO_DIR)
    m = _VERSION_LINE_RE.search(out)
    if m: info["version"] = m.group(1)
    m2 = _BACKEND_RE.search(out)
    if m2: info["backend"] = m2.group(1)
    m3 = _CUDA_RE.search(out)
    if m3: info["cuda_build"] = m3.group(1)
    _VERSION_CACHE = info
    return info
