"""Merge shard artifacts into one tree, without trusting any of them.

Rules, in order:

  * a path produced twice with the same sha256 is deduped (that is the price
    of splitting, not a fault);
  * a path produced twice with different bytes goes to the worker the plan
    assigned it to, and the collision is counted and reported;
  * a path with no producer is a hard error;
  * every written file is re-hashed after it lands.

The workers' own ``.ninja_log`` entries travel in the artifacts and are
reassembled here, because ninja rebuilds any output whose command line is not
in the log -- and this machine has no compiler to rebuild it with.
"""

from __future__ import annotations

import io
import os
import tarfile
from dataclasses import dataclass, field

from . import util

META = ".fission-artifact.json"
LOG = ".fission-ninja-log"


class MergeError(RuntimeError):
    pass


@dataclass
class Report:
    written: int = 0
    deduped: int = 0
    collisions: list[dict] = field(default_factory=list)
    unowned: list[str] = field(default_factory=list)
    verify_failed: list[str] = field(default_factory=list)
    verified: int = 0
    log_entries: int = 0
    log_header: str = ""
    symlinks: int = 0
    skipped_members: int = 0

    def as_dict(self) -> dict:
        return {
            "written": self.written,
            "deduped": self.deduped,
            "collisions": self.collisions[:50],
            "collision_count": len(self.collisions),
            "unowned": self.unowned[:50],
            "unowned_count": len(self.unowned),
            "verified": self.verified,
            "verify_failed": self.verify_failed[:50],
            "verify_failed_count": len(self.verify_failed),
            "log_entries": self.log_entries,
            "log_header": self.log_header,
            "symlinks": self.symlinks,
            "skipped_members": self.skipped_members,
        }

    @property
    def ok(self) -> bool:
        return not self.unowned and not self.verify_failed


def _meta_of(tar_path: str) -> dict:
    with tarfile.open(util.long_path(tar_path), "r") as tf:
        try:
            member = tf.getmember(META)
        except KeyError as exc:
            raise MergeError(f"{tar_path}: not a fission artifact (no {META})") from exc
        fh = tf.extractfile(member)
        if fh is None:
            raise MergeError(f"{tar_path}: empty {META}")
        import json

        return json.loads(fh.read().decode("utf-8", "replace"))


def _log_entries(tar_path: str) -> list[str]:
    with tarfile.open(util.long_path(tar_path), "r") as tf:
        try:
            member = tf.getmember(LOG)
        except KeyError:
            return []
        fh = tf.extractfile(member)
        if fh is None:
            return []
        return [
            line for line in fh.read().decode("utf-8", "replace").splitlines()
            if line.strip() and not line.startswith("#")
        ]


def _newest_header(headers: list[str]) -> str:
    best, best_v = "# ninja log v5", 5
    for h in headers:
        digits = "".join(c for c in h.split()[-1] if c.isdigit())
        if digits.isdigit() and int(digits) > best_v:
            best, best_v = h, int(digits)
    return best


def _safe_member(name: str) -> bool:
    if name.startswith(("/", "\\")) or ":" in name.split("/")[0]:
        return False
    parts = name.replace("\\", "/").split("/")
    return not any(p in ("..", "") for p in parts)


def load_ownership(directory: str) -> dict[str, str]:
    """Read owned.<worker>.txt files written by `fission split`."""
    owner: dict[str, str] = {}
    if not directory or not os.path.isdir(directory):
        return owner
    for name in os.listdir(directory):
        if not (name.startswith("owned.") and name.endswith(".txt")):
            continue
        worker = name[len("owned."):-len(".txt")]
        with util.open_any(os.path.join(directory, name), "r") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    owner[line] = worker
    return owner


def merge(tars: list[str], dest: str, ownership: dict[str, str] | None = None) -> Report:
    if not tars:
        raise MergeError("no artifacts given")
    ownership = ownership or {}
    metas = [(t, _meta_of(t)) for t in tars]

    claims: dict[str, list[tuple[str, str]]] = {}
    owner_of: dict[str, str] = dict(ownership)
    for _tar, meta in metas:
        worker = meta["worker"]
        for rec in meta["files"]:
            claims.setdefault(rec["path"], []).append((worker, rec["sha256"]))
            owner_of.setdefault(rec["path"], meta.get("owners", {}).get(rec["path"], worker))

    report = Report()
    winner: dict[str, str] = {}
    for path, entries in claims.items():
        digests = {d for _w, d in entries}
        owner = owner_of.get(path)
        if len(digests) == 1:
            if len(entries) > 1:
                report.deduped += 1
            winner[path] = owner if owner in {w for w, _ in entries} else entries[0][0]
            continue
        if owner is None or owner not in {w for w, _ in entries}:
            report.unowned.append(path)
            winner[path] = entries[0][0]
            continue
        winner[path] = owner
        report.collisions.append(
            {
                "path": path,
                "owner": owner,
                "producers": sorted({w for w, _ in entries}),
                "variants": len(digests),
            }
        )

    wanted_by_worker: dict[str, set[str]] = {}
    for path, worker in winner.items():
        wanted_by_worker.setdefault(worker, set()).add(path)

    meta_by_path: dict[str, dict] = {}
    for _tar, meta in metas:
        for rec in meta["files"]:
            if winner.get(rec["path"]) == meta["worker"]:
                meta_by_path[rec["path"]] = rec

    util.makedirs(dest)
    written: set[str] = set()
    for tar, meta in metas:
        wanted = wanted_by_worker.get(meta["worker"], set())
        if not wanted:
            continue
        with tarfile.open(util.long_path(tar), "r") as tf:
            for member in tf:
                if member.name in (META, LOG):
                    continue
                is_link = member.issym() or member.islnk()
                if member.name not in wanted or member.name in written:
                    # A link nobody claimed is worth counting: it means the
                    # artifact and the plan disagree about this path.
                    if is_link:
                        report.skipped_members += 1
                    continue
                if not _safe_member(member.name):
                    raise MergeError(f"{tar}: refusing path {member.name!r}")
                target = os.path.join(dest, member.name)
                util.makedirs(os.path.dirname(target) or ".")
                if is_link:
                    if os.name == "nt":
                        # Windows needs privilege for symlinks; keep the build
                        # usable by materialising the target when we have it.
                        report.skipped_members += 1
                        continue
                    link = member.linkname
                    if os.path.lexists(util.long_path(target)):
                        os.unlink(util.long_path(target))
                    os.symlink(link, util.long_path(target))
                    report.symlinks += 1
                    written.add(member.name)
                    continue
                if not member.isfile():
                    report.skipped_members += 1
                    continue
                src = tf.extractfile(member)
                if src is None:
                    report.skipped_members += 1
                    continue
                tmp = target + ".mn-tmp"
                with util.open_any(tmp, "wb") as out:
                    while True:
                        b = src.read(util.CHUNK)
                        if not b:
                            break
                        out.write(b)
                rec = meta_by_path.get(member.name, {})
                util.chmod(tmp, rec.get("mode", member.mode & 0o7777))
                ns = rec.get("mtime_ns") or int(member.mtime * 1e9)
                util.utime(tmp, ns)
                util.replace(tmp, target)
                written.add(member.name)
                report.written += 1

    # Reassemble the build log from the workers' real entries.
    lines: dict[str, str] = {}
    headers: list[str] = []
    for tar, meta in metas:
        header = meta.get("ninja_log_header")
        if header:
            headers.append(header)
        for line in _log_entries(tar):
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            path = parts[3]
            if winner.get(path, meta["worker"]) == meta["worker"]:
                lines[path] = line
    if lines:
        report.log_entries = len(lines)
        report.log_header = _newest_header(headers)
        log_path = os.path.join(dest, ".ninja_log")
        with util.open_any(log_path, "w") as fh:
            fh.write(report.log_header + "\n")
            for line in lines.values():
                fh.write(line + "\n")

    for path, rec in meta_by_path.items():
        full = os.path.join(dest, path)
        if not os.path.exists(util.long_path(full)) or util.sha256_file(full) != rec["sha256"]:
            report.verify_failed.append(path)
        else:
            report.verified += 1

    if len(winner) != report.written + report.symlinks:
        missing = sorted(set(winner) - written)
        report.unowned.extend(missing[:50])
    return report
