"""Build a synthetic shard artifact set the way the real pipeline would.

This deliberately re-implements the *pack* side rather than importing
lineage-fission: the masternode must be verifiable on its own, with no
dependency on the producer's code.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile

META = ".fission-artifact.json"
LOG = ".fission-ninja-log"

NINJA_RULES = """\
rule cc
  command = {join} $out $in
rule ar
  command = {join} $out $in
rule install
  command = {join} $out $in
rule img
  command = {join} $out $in
"""


def ninja_rules() -> str:
    """The same rules on every platform, including Windows.

    `cat` and `cp` do not exist there, so the fixture's commands are a Python
    helper invoked with this interpreter. The command line is what ninja hashes
    into its log, so it must be identical for the build and for the later
    "is this tree up to date" check -- both happen on the same machine and with
    the same interpreter, which is exactly the situation here.
    """
    join = f'"{sys.executable}" "{os.path.join(os.path.dirname(os.path.abspath(__file__)), "copyjoin.py")}"'
    return NINJA_RULES.format(join=join)


def _write(path: str, data: str, mtime_ns: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ninja_path() -> str | None:
    return os.environ.get("NINJA") or shutil.which("ninja")


def build_tree(root: str, modules: int = 6, seed: int = 5, use_ninja: bool = True) -> dict:
    """A tiny Android-shaped build: sources -> objects -> archive -> install.

    The build log must come from real ninja: it stores a hash of the evaluated
    command line, and a fabricated log makes every edge look changed. So the
    tree is actually built here when ninja is available.
    """
    os.makedirs(root, exist_ok=True)
    lines = [ninja_rules()]
    installs = []
    sources = []
    base = 1_700_000_000_000_000_000
    for m in range(modules):
        section = ["external", "frameworks", "system", "packages", "device", "hardware"][m % 6]
        name = f"{section}/mod{m}"
        objs = []
        for i in range(2):
            src = f"{name}/s{i}.txt"
            _write(os.path.join(root, src), f"{name} source {i}\n", base + m * 1000 + i)
            sources.append(src)
            obj = f"out/obj/{name}/s{i}.o"
            _write(os.path.join(root, obj), f"{name} object {i}\n", base + m * 1000 + 100 + i)
            lines.append(f"build {obj}: cc {src}\n")
            objs.append(obj)
        lib = f"out/obj/{name}/lib.a"
        _write(os.path.join(root, lib), f"{name} archive\n", base + m * 1000 + 200)
        lines.append(f"build {lib}: ar {' '.join(objs)}\n")
        inst = f"out/{name}/inst"
        _write(os.path.join(root, inst), f"{name} installed\n", base + m * 1000 + 300)
        lines.append(f"build {inst}: install {lib}\n")
        installs.append(inst)
    # The image is produced by whichever shard owns the packaging edge, and it
    # can legitimately end up older than installs another shard wrote later.
    # That is the skew the masternode has to reconcile.
    image = "out/product/system.img"
    _write(os.path.join(root, image), "image\n", base + 4000)
    lines.append(f"build {image}: img {' '.join(installs)}\n")
    lines.append("default out/product/system.img\n")
    _write(os.path.join(root, "combined.ninja"), "".join(lines), base + 1)

    ninja = ninja_path()
    built = False
    if use_ninja and ninja:
        proc = subprocess.run(
            [ninja, "-f", "combined.ninja", image],
            cwd=root, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ninja failed: {proc.stdout}\n{proc.stderr}")
        built = True

    return {
        "root": root,
        "built_with_ninja": built,
        "modules": modules,
        "image": image,
        "installs": installs,
        "sources": sorted(sources),
        "all_outputs": sorted(
            os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
            for dp, _dn, fs in os.walk(os.path.join(root, "out"))
            for f in fs
        ),
    }


def skew(root: str, paths: list[str], shift_ns: int) -> None:
    """Emulate a worker that finished later than its peers.

    Both the files and their log entries move, because they must agree with
    each other -- the skew that matters is *between* shards.
    """
    log_path = os.path.join(root, ".ninja_log")
    wanted = set(paths)
    lines = []
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    lines.append(line)
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 5 and parts[3] in wanted:
                    parts[2] = str(int(parts[2]) + shift_ns)
                lines.append("\t".join(parts) + "\n")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    for rel in paths:
        full = os.path.join(root, rel)
        st = os.stat(full)
        os.utime(full, ns=(st.st_atime_ns, st.st_mtime_ns + shift_ns))


def pack(root: str, paths: list[str], tar_path: str, worker: str) -> dict:
    records = []
    for rel in paths:
        full = os.path.join(root, rel)
        st = os.stat(full)
        records.append(
            {
                "path": rel,
                "size": st.st_size,
                "sha256": sha256_file(full),
                "mode": st.st_mode & 0o7777,
                "mtime_ns": st.st_mtime_ns,
            }
        )
    log_path = os.path.join(root, ".ninja_log")
    log_lines = []
    log_header = ""
    if os.path.exists(log_path):
        wanted = {r["path"] for r in records}
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    log_header = log_header or line.strip()
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 5 and parts[3] in wanted:
                    log_lines.append(line.rstrip("\n"))
    meta = {
        "worker": worker,
        "files": records,
        "missing": [],
        "owners": {r["path"]: worker for r in records},
        "ninja_log_entries": len(log_lines),
        # The header must be the one the local ninja actually wrote: a
        # hardcoded version that is newer than the assembler's ninja makes it
        # discard the log and rebuild the whole tree.
        "ninja_log_header": log_header or "# ninja log v5",
    }
    os.makedirs(os.path.dirname(os.path.abspath(tar_path)), exist_ok=True)
    with tarfile.open(tar_path, "w") as tf:
        for rec in records:
            info = tf.gettarinfo(os.path.join(root, rec["path"]), arcname=rec["path"])
            info.mtime = rec["mtime_ns"] / 1e9
            info.uid = info.gid = 0
            with open(os.path.join(root, rec["path"]), "rb") as fh:
                tf.addfile(info, fh)
        for name, text in ((META, json.dumps(meta, sort_keys=True)), (LOG, "\n".join(log_lines))):
            blob = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(blob)
            ti.mtime = 0
            tf.addfile(ti, io.BytesIO(blob))
    return {"worker": worker, "files": len(records), "tar": tar_path}


def write_sums(directory: str) -> str:
    lines = []
    for name in sorted(os.listdir(directory)):
        if name == "SHA256SUMS":
            continue
        full = os.path.join(directory, name)
        if os.path.isfile(full):
            lines.append(f"{sha256_file(full)}  {name}")
    path = os.path.join(directory, "SHA256SUMS")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
