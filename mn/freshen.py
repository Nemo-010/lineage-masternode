"""Make a merged tree look consistent to ninja. Metadata only, no compiling.

Each shard stamped its outputs at its own wall-clock time, so after a merge an
output can be older than an input another shard built later, and ninja calls
the whole tree dirty. Ninja also trusts the *log's* recorded mtime over the
file's, and rebuilds any output whose command line is not in the log. So this
does both: bump file mtimes in dependency order, and rewrite the matching log
entries.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import graph, util

NUDGE_NS = 1_000_000


@dataclass
class Report:
    considered: int = 0
    bumped: int = 0
    log_rewritten: int = 0
    max_skew_ns: int = 0
    missing_inputs: list[str] = field(default_factory=list)
    missing_outputs: int = 0
    suspicious_skew: bool = False
    log_missing: bool = False
    clamped: int = 0
    mode: str = "uniform"

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "bumped": self.bumped,
            "log_rewritten": self.log_rewritten,
            "max_skew_s": round(self.max_skew_ns / 1e9, 3),
            "missing_outputs": self.missing_outputs,
            "missing_inputs": self.missing_inputs[:50],
            "missing_inputs_count": len(self.missing_inputs),
            "suspicious_skew": self.suspicious_skew,
            "log_missing": self.log_missing,
            "clamped": self.clamped,
            "mode": self.mode,
            "ok": self.ok,
        }

    @property
    def ok(self) -> bool:
        return not self.missing_inputs


def freshen(ninja_files: list[str], root: str) -> Report:
    edges = graph.load(ninja_files, root)
    order = graph.topo(edges)
    report = Report()
    mtime: dict[str, int] = {}
    bumped: dict[str, int] = {}

    for idx in order:
        edge = edges[idx]
        outs = [o for o in edge.outputs if os.path.exists(util.long_path(os.path.join(root, o)))]
        if not outs:
            report.missing_outputs += len(edge.outputs)
            continue
        report.considered += 1
        newest = 0
        for dep in edge.inputs:
            if dep in mtime:
                newest = max(newest, mtime[dep])
                continue
            full = os.path.join(root, dep)
            if not os.path.exists(util.long_path(full)):
                report.missing_inputs.append(dep)
                continue
            newest = max(newest, os.stat(util.long_path(full)).st_mtime_ns)
        for out in outs:
            full = os.path.join(root, out)
            cur = os.stat(util.long_path(full)).st_mtime_ns
            if newest and cur <= newest:
                skew = newest - cur + NUDGE_NS
                report.max_skew_ns = max(report.max_skew_ns, skew)
                report.bumped += 1
                cur = newest + NUDGE_NS
                bumped[out] = cur
                util.utime(full, cur)
            mtime[out] = cur

    # A gap of hours is not worker clock skew, it is a wrong tree or a wrong
    # clock. Say so instead of quietly "fixing" it.
    report.suspicious_skew = report.max_skew_ns > 3600 * 10**9

    log_path = os.path.join(root, ".ninja_log")
    if bumped:
        if os.path.exists(util.long_path(log_path)):
            report.log_rewritten = _rewrite_log(log_path, bumped)
        else:
            # Without a log, ninja rebuilds everything regardless of mtimes:
            # the mtime fix is necessary but no longer sufficient.
            report.log_missing = True
    return report


def _rewrite_log(path: str, mtime_by_output: dict[str, int]) -> int:
    with util.open_any(path, "r") as fh:
        lines = fh.readlines()
    changed = 0
    out: list[str] = []
    for line in lines:
        if line.startswith("#"):
            out.append(line)
            continue
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 5 and parts[3] in mtime_by_output:
            parts[2] = str(mtime_by_output[parts[3]])
            changed += 1
        out.append("\t".join(parts) + "\n")
    tmp = path + ".mn-tmp"
    with util.open_any(tmp, "w") as fh:
        fh.writelines(out)
    util.replace(tmp, path)
    return changed


def restamp(root: str, now_ns: int | None = None) -> Report:
    """Give every file one timestamp. No graph parse, and no ordering to get wrong.

    Ninja calls an edge dirty only when an output is *older* than an input. If
    every output carries the same stamp T and every input is <= T, no edge is
    dirty. Correctness does not rest on timestamps here: the merged tree was
    hash-verified on the way in.

    Two details make it robust:

    * the stamp is `now`, not "now + 1h": a future stamp would make a later
      edit look older than its output and silently skip a rebuild;
    * anything already *newer* than the stamp is clamped down to it. That
      covers a worker whose clock ran fast, which would otherwise leave edges
      dirty forever.

    The `.ninja_log` is the authority on which paths are outputs -- the same
    list ninja consults -- so no dependency graph is needed. That is the point:
    a real combined.ninja costs ~3 minutes and gigabytes of RAM to parse in
    Python, and this machine may have neither to spare.
    """
    log_path = os.path.join(root, ".ninja_log")
    report = Report()
    report.mode = "uniform"
    if not os.path.exists(util.long_path(log_path)):
        report.log_missing = True
        return report

    with util.open_any(log_path, "r") as fh:
        lines = fh.readlines()

    outputs: set[str] = set()
    parsed: list[list[str]] = []
    for line in lines:
        if line.startswith("#"):
            parsed.append([line.rstrip("\n")])
            continue
        parts = line.rstrip("\n").split("\t")
        parsed.append(parts)
        if len(parts) >= 5:
            outputs.add(parts[3])
    report.considered = len(outputs)

    stamp = now_ns if now_ns is not None else _now_ns()
    present: set[str] = set()

    skip = {".git", ".repo"}
    for dirpath, dirnames, files in os.walk(util.long_path(root)):
        dirnames[:] = [d for d in dirnames if d not in skip]
        rel_dir = os.path.relpath(dirpath, root)
        for name in files:
            rel = name if rel_dir == "." else os.path.join(rel_dir, name).replace(os.sep, "/")
            full = os.path.join(dirpath, name)
            is_output = rel in outputs
            try:
                mtime = os.stat(full).st_mtime_ns
            except OSError:
                continue
            if is_output:
                present.add(rel)
                util.utime(full, stamp)
                report.bumped += 1
            elif mtime > stamp:
                # Clamp: a future-dated input would keep its edge dirty.
                util.utime(full, stamp)
                report.clamped += 1

    report.missing_outputs = len(outputs) - len(present)
    report.log_rewritten = len(present)

    tmp = log_path + ".mn-tmp"
    with util.open_any(tmp, "w") as fh:
        for parts in parsed:
            if len(parts) >= 5:
                parts[2] = str(stamp)
                fh.write("\t".join(parts) + "\n")
            else:
                fh.write(parts[0] + "\n")
    util.replace(tmp, log_path)
    return report


def _now_ns() -> int:
    import time

    return time.time_ns()
