#!/usr/bin/env python3
"""Run the masternode against a simulated fleet and print what happened.

The fleet side is synthesised: there is no LineageOS tree in this repo, so
`test/synth.py` produces the artifact contract that `fission pack` produces
(real tar, real sha256, real `.ninja_log` entries). Everything on the
masternode side is the shipped code, doing real work on real files.

    python3 demo/run_demo.py [--keep]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "test"))

import synth  # noqa: E402
from mn import util  # noqa: E402

SECTIONS = {
    "w0": ("external", "system"),
    "w1": ("frameworks", "packages"),
    "w2": ("device", "hardware"),
}
BUDGET_GB = 30.0


def cli(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "masternode.py"), *args],
        capture_output=True, text=True, cwd=ROOT,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")
    print("-" * (len(title) + 4))


def section_of(rel: str) -> str:
    """out/obj/<section>/... and out/<section>/... both map to <section>."""
    parts = rel.split("/")
    rest = parts[1:] if parts[0] == "out" else parts
    if rest and rest[0] == "obj":
        rest = rest[1:]
    return rest[0] if rest else "misc"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    ap.add_argument("--shards", type=int, default=3, choices=(1, 2, 3))
    ap.add_argument(
        "--budget-gb", type=float, default=BUDGET_GB,
        help="the storage budget to ask doctor about (default: 30)",
    )
    ap.add_argument(
        "--scratch",
        help="where to build the demo (default: a directory next to this repo, "
             "because /tmp is often a small tmpfs and doctor would rightly refuse)",
    )
    args = ap.parse_args()
    names = list(SECTIONS)[: args.shards]

    parent = args.scratch or os.path.join(os.path.dirname(ROOT), ".mn-demo")
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="run-", dir=parent)
    started = time.time()
    print(f"masternode demo   scratch={tmp}   budget={args.budget_gb:.0f} GB")

    step(0, "fleet: build shards (synthesised, but real tars and real hashes)")
    source = os.path.join(tmp, "fleet-tree")
    info = synth.build_tree(source, modules=6)
    if not info["built_with_ninja"]:
        print("    WARNING: ninja not on PATH, so there is no real build log;")
        print("             step 6 will be skipped. Set NINJA=/path/to/ninja.")
    release = os.path.join(tmp, "release")
    groups: dict[str, list[str]] = {name: [] for name in names}
    for rel in info["all_outputs"]:
        sec = section_of(rel)
        for name in names:
            if sec in SECTIONS[name]:
                groups[name].append(rel)
    # Anything the section map does not name (packaging edges such as
    # out/product/*) still has to live somewhere.
    assigned = {p for paths in groups.values() for p in paths}
    leftovers = [p for p in info["all_outputs"] if p not in assigned]
    groups[names[0]].extend(leftovers)
    if leftovers:
        print(f"    leftovers (packaging edges): {len(leftovers)} files -> shard-{names[0]}")
    for name in names:
        groups[name] = sorted(set(groups[name]))
        # Emulate real workers finishing at different times: the later a shard
        # finished, the newer its outputs are. This is what makes a merged tree
        # stale for ninja, and it is what freshen exists to fix.
        shift = {"w0": 0, "w1": 2_000_000_000, "w2": 4_000_000_000}.get(name, 0)
        if shift:
            synth.skew(source, groups[name], shift)
        tar = os.path.join(release, f"shard-{name}.tar")
        synth.pack(source, groups[name], tar, name)
        print(f"    shard-{name}: {len(groups[name]):2d} files -> {util.human(os.path.getsize(tar))}")
    sums = synth.write_sums(release)
    print(f"    SHA256SUMS: {len(open(sums).read().splitlines())} entries")

    step(1, "masternode doctor")
    asset_bytes = sum(os.path.getsize(os.path.join(release, f)) for f in os.listdir(release))
    assets_gb = asset_bytes / 1e9
    code, out = cli("doctor", "--root", tmp, "--budget-gb", str(args.budget_gb), "--assets-gb", str(assets_gb))
    print(out)
    if code != 0:
        # Not a pipeline failure: doctor is doing its job on a scratch disk that
        # is smaller than the budget. Say which disk, and keep going, because
        # the demo's own artifacts are kilobytes.
        free = shutil.disk_usage(tmp).free
        print(
            f"\n    doctor answered no: {util.human(free)} free under {tmp} is less\n"
            f"    than the {args.budget_gb:.0f} GB budget, so no mode is possible there. That\n"
            f"    is the honest answer. This demo's artifacts are {util.human(asset_bytes)}, so the\n"
            f"    pipeline below still runs; use --scratch DIR on a bigger disk to see\n"
            f"    doctor agree, or --budget-gb to ask about a budget you actually have."
        )

    step(2, "masternode fetch")
    incoming = os.path.join(tmp, "incoming")
    code, out = cli("fetch", "--source", "dir:" + release, "--dest", incoming)
    print(out)
    assert code == 0, "fetch failed"

    step(3, "masternode verify")
    code, out = cli("verify", "--sums", os.path.join(incoming, "SHA256SUMS"), "--dest", incoming)
    print(out)
    assert code == 0, "verify failed"

    step(4, "masternode merge")
    dest = os.path.join(tmp, "tree")
    ownership = os.path.join(tmp, "split")
    os.makedirs(ownership, exist_ok=True)
    for name, paths in groups.items():
        with open(os.path.join(ownership, f"owned.{name}.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(paths) + "\n")
    code, out = cli("merge", "--dest", dest,
                    "--artifact", os.path.join(incoming, "shard-*.tar"),
                    "--ownership-dir", ownership)
    print(out)
    assert code == 0, "merge failed"

    step(5, "masternode freshen")
    shutil.copy(os.path.join(source, "combined.ninja"), os.path.join(dest, "combined.ninja"))
    for rel in info["sources"]:
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(source, rel), dst)
    t0 = time.time()
    code, out = cli("freshen", "--root", dest, "--mode", "uniform")
    print(out)
    print(f"    (uniform restamp took {time.time() - t0:.3f}s; no ninja graph parsed)")
    assert code == 0, "freshen failed"

    step(6, "does ninja agree there is nothing left to do?")
    ninja = synth.ninja_path()
    if ninja and info["built_with_ninja"]:
        proc = subprocess.run([ninja, "-f", "combined.ninja", "-n", "out/product/system.img"],
                              cwd=dest, capture_output=True, text=True)
        pending = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("[")]
        last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "(no output)"
        print(f"    {last}")
        print(f"    pending edges: {len(pending)}")
        assert not pending, f"ninja still wants to build: {pending[:5]}"
    else:
        print("    ninja not on PATH; skipped (unit tests cover this with a graph walk)")

    step(7, "masternode report + compare against the fleet tree")
    code, out = cli("report", "--dest", dest)
    print(out)
    mismatched = [
        rel for rel in info["all_outputs"]
        if not os.path.exists(os.path.join(dest, rel))
        or util.sha256_file(os.path.join(source, rel)) != util.sha256_file(os.path.join(dest, rel))
    ]
    print(f"    {len(info['all_outputs'])} files compared, {len(mismatched)} mismatched")
    print(f"    total wall time: {time.time() - started:.2f}s")

    if args.keep:
        print(f"\nkept: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not mismatched else 1


if __name__ == "__main__":
    raise SystemExit(main())
