"""Small helpers that behave the same on Linux, macOS and Windows."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time

CHUNK = 1 << 20
WIN = os.name == "nt"


def long_path(path: str) -> str:
    r"""Windows caps paths at 260 chars unless the \\?\ prefix is used.

    Android build trees happily produce paths longer than that
    (out/soong/.intermediates/<module>/android_common/gen/...), so this is not
    a theoretical concern on Windows -- it is the difference between extracting
    and dying halfway through.

    Order matters: abspath() would rewrite a UNC share, or an already prefixed
    path, into nonsense, so those are recognised first. A leading double
    backslash counts as absolute even when this code is exercised on POSIX.
    """
    if not WIN:
        return path
    p = path
    if p.startswith("\\\\?\\"):
        return p
    is_absolute = p.startswith("\\\\") or os.path.isabs(p)
    if not is_absolute:
        p = os.path.abspath(p)
    if len(p) < 240:
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def open_any(path: str, mode: str = "rb"):
    return open(long_path(path), mode)


def makedirs(path: str) -> None:
    os.makedirs(long_path(path), exist_ok=True)


def replace(src: str, dst: str) -> None:
    """Atomic rename where possible, plain move where Windows says no."""
    try:
        os.replace(long_path(src), long_path(dst))
        return
    except OSError:
        pass
    try:
        if os.path.exists(long_path(dst)):
            os.unlink(long_path(dst))
        shutil.move(long_path(src), long_path(dst))
    except OSError as exc:  # pragma: no cover - Windows sharing violations
        raise RuntimeError(f"cannot move {src} -> {dst}: {exc}") from exc


def chmod(path: str, mode: int) -> None:
    try:
        os.chmod(long_path(path), mode)
    except OSError:
        # Windows ignores most of this; never fail an extract over it.
        pass


def utime(path: str, mtime_ns: int) -> None:
    os.utime(long_path(path), ns=(mtime_ns, mtime_ns))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open_any(path, "rb") as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{n:3.1f} {unit}"
        n /= 1024.0
    return f"{n:3.1f} PiB"


def load_json(path: str) -> dict:
    with open_any(path, "r") as fh:
        return json.load(fh)


def dump_json(path: str, data: dict) -> None:
    makedirs(os.path.dirname(os.path.abspath(path)) or ".")
    tmp = path + ".tmp"
    with open_any(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    replace(tmp, path)


def tree_bytes(path: str) -> tuple[int, int]:
    total = count = 0
    for dirpath, _dirnames, files in os.walk(long_path(path)):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
                count += 1
            except OSError:
                pass
    return total, count


def now() -> float:
    return time.monotonic()


def platform_line() -> str:
    return f"{sys.platform} python {sys.version.split()[0]} {os.cpu_count()} cpu"
