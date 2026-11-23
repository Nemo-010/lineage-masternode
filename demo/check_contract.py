#!/usr/bin/env python3
"""Check that the masternode's merge agrees with the producer's.

The two sides implement the artifact contract independently, on purpose: if
they drift, a real run loses files or ships stale outputs. This packs a set of
artifacts with lineage-fission, merges it twice (once with each implementation)
and compares the trees byte for byte.

    python3 demo/check_contract.py [--fission /path/to/lineage-fission]

Skips (exit 0) when the producer is not next to this repo.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fission", default=os.path.join(os.path.dirname(ROOT), "lineage-fission"))
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    producer = args.fission
    if not os.path.isdir(os.path.join(producer, "fission")):
        print(f"skipped: no lineage-fission at {producer}")
        return 0
    ninja = os.environ.get("NINJA") or shutil.which("ninja")
    if not ninja:
        print("skipped: ninja not on PATH (needed to build the producer's test project)")
        return 0

    sys.path.insert(0, producer)
    sys.path.insert(0, os.path.join(producer, "test"))
    from fission import artifacts as fission_artifacts  # noqa: E402
    import gen_project  # noqa: E402

    sys.path.insert(0, ROOT)
    from mn import merge as mn_merge  # noqa: E402
    from mn import util  # noqa: E402

    tmp = tempfile.mkdtemp(prefix="contract-", dir=os.path.dirname(ROOT))
    try:
        ref = os.path.join(tmp, "ref")
        meta = gen_project.generate(ref)
        subprocess.run([ninja, "-f", "combined.ninja", meta["goal_all"]], cwd=ref, check=True,
                       capture_output=True)

        plan = os.path.join(tmp, "plan.json")
        with open(plan, "w", encoding="utf-8") as fh:
            json.dump({"digest": "contract", "projects": [{"path": m} for m in meta["modules"]],
                       "policy": {"rules": []}}, fh)
        split_dir = os.path.join(tmp, "split")
        subprocess.run(
            [sys.executable, "-m", "fission.cli", "split", "--ninja", "combined.ninja",
             "--root", ref, "--plan", plan, "--workers", "3",
             "--ninja-log", os.path.join(ref, ".ninja_log"), "--out", split_dir],
            cwd=producer, check=True, capture_output=True,
        )
        with open(os.path.join(split_dir, "plan.json"), encoding="utf-8") as fh:
            split = json.load(fh)

        tars, ownership = [], {}
        for w in split["workers"]:
            ws = os.path.join(tmp, f"ws-{w['id']}")
            shutil.copytree(ref, ws)
            shutil.rmtree(os.path.join(ws, "out"))
            with open(os.path.join(split_dir, f"targets.{w['id']}.txt"), encoding="utf-8") as fh:
                targets = [line.strip() for line in fh if line.strip()]
            subprocess.run([ninja, "-f", "combined.ninja", *targets], cwd=ws, check=True,
                           capture_output=True)
            with open(os.path.join(split_dir, f"owned.{w['id']}.txt"), encoding="utf-8") as fh:
                owned = [line.strip() for line in fh if line.strip()]
            for path in owned:
                ownership[path] = w["id"]
            tar = os.path.join(tmp, f"{w['id']}.tar")
            fission_artifacts.pack(ws, owned, tar, w["id"])
            tars.append(tar)

        left = os.path.join(tmp, "asm-fission")
        shutil.copytree(ref, left)
        shutil.rmtree(os.path.join(left, "out"))
        fission_artifacts.merge(tars, left, strict=True, ownership=ownership)

        right = os.path.join(tmp, "asm-masternode")
        shutil.copytree(ref, right)
        shutil.rmtree(os.path.join(right, "out"))
        report = mn_merge.merge(tars, right, ownership)

        differences = []
        count = 0
        for dirpath, _dirnames, files in os.walk(os.path.join(ref, "out")):
            for name in files:
                count += 1
                rel = os.path.relpath(os.path.join(dirpath, name), os.path.join(ref, "out"))
                a, b = os.path.join(left, "out", rel), os.path.join(right, "out", rel)
                if not os.path.exists(b) or util.sha256_file(a) != util.sha256_file(b):
                    differences.append(rel)

        print(json.dumps({
            "files_compared": count,
            "differences": differences[:20],
            "difference_count": len(differences),
            "masternode_written": report.written,
            "masternode_verified": report.verified,
            "masternode_log_entries": report.log_entries,
            "log_header": report.log_header,
        }, indent=2))
        return 0 if not differences else 1
    finally:
        if args.keep:
            print(f"kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
