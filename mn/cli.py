"""masternode.py -- pull, verify and assemble a split build on a small machine.

    masternode.py doctor  --budget-gb 30
    masternode.py fetch   --source gh:owner/repo:TAG --dest incoming
    masternode.py verify  --sums incoming/SHA256SUMS --dest incoming
    masternode.py merge   --dest tree --artifact incoming/shard-*.tar --ownership split/
    masternode.py freshen --ninja out/combined-alioth.ninja --root tree
    masternode.py report
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from . import doctor, fetch as fetchmod, freshen, merge as mergemod, util


def _fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"masternode: {msg}", file=sys.stderr)
    raise SystemExit(1)


def cmd_doctor(args) -> int:
    report = doctor.inspect(args.root, args.budget_gb, args.assets_gb)
    print(doctor.render(report))
    if args.json:
        util.dump_json(args.json, report.as_dict())
    return 0 if report.ok else 1


def cmd_fetch(args) -> int:
    sums = {}
    for s in args.sums:
        sums.update(fetchmod.parse_sha256sums(s))
    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        result = fetchmod.fetch(
            args.source,
            args.dest,
            sha256=sums,
            token=token,
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    except fetchmod.FetchError as exc:
        _fail(str(exc))
    print(json.dumps(result.as_dict(), indent=2))
    if not result.ok:
        _fail(f"{len(result.failed)} download(s) failed")
    return 0


def cmd_verify(args) -> int:
    sums = {}
    for s in args.sums:
        sums.update(fetchmod.parse_sha256sums(s))
    if not sums:
        _fail("no hashes found in the given SHA256SUMS file(s)")
    missing, bad = [], []
    for name, want in sorted(sums.items()):
        path = os.path.join(args.dest, name)
        if not os.path.exists(util.long_path(path)):
            missing.append(name)
            continue
        if util.sha256_file(path) != want:
            bad.append(name)
    report = {"checked": len(sums), "missing": missing, "mismatched": bad, "ok": not missing and not bad}
    print(json.dumps(report, indent=2))
    if not report["ok"]:
        _fail("verification failed")
    return 0


def cmd_merge(args) -> int:
    tars = []
    for pattern in args.artifact:
        import shlex

        for token in shlex.split(pattern):
            tars.extend(sorted(glob.glob(token)) or ([token] if os.path.exists(token) else []))
    if not tars:
        _fail("no artifacts matched")
    ownership = mergemod.load_ownership(args.ownership_dir) if args.ownership_dir else {}
    try:
        report = mergemod.merge(tars, args.dest, ownership)
    except mergemod.MergeError as exc:
        _fail(str(exc))
    print(json.dumps(report.as_dict(), indent=2))
    if args.json:
        util.dump_json(args.json, report.as_dict())
    if not report.ok:
        _fail("merge failed: unowned or unverifiable paths")
    return 0


def cmd_freshen(args) -> int:
    # Ninja paths are normally written relative to the tree, so resolve them
    # there rather than against whatever directory the shell was in.
    ninja_files = [
        n if os.path.isabs(n) else os.path.join(args.root, n) for n in args.ninja
    ]
    if args.mode == "graph" and not ninja_files:
        _fail("--mode graph needs --ninja")
    missing = [n for n in ninja_files if not os.path.exists(util.long_path(n))]
    if missing:
        _fail(f"no such ninja file: {', '.join(missing)}")
    if args.mode == "uniform":
        report = freshen.restamp(args.root)
    else:
        report = freshen.freshen(ninja_files, args.root)
    print(json.dumps(report.as_dict(), indent=2))
    if report.log_missing:
        print(
            "masternode: warning: no .ninja_log in this tree, so ninja will rebuild "
            "everything even though mtimes are consistent",
            file=sys.stderr,
        )
    return 0 if report.ok else 1


def cmd_report(args) -> int:
    size, files = util.tree_bytes(args.dest)
    data = {
        "dest": os.path.abspath(args.dest),
        "bytes": size,
        "files": files,
        "human": util.human(size),
        "free_gb": round(doctor.inspect(args.dest, 0).free_gb, 2),
    }
    if args.json:
        util.dump_json(args.json, data)
    print(json.dumps(data, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="masternode", description="pull, verify and assemble a split build")
    p.add_argument("--version", action="version", version="lineage-masternode 0.1.0")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="what this machine can actually do")
    d.add_argument("--root", default=".")
    d.add_argument("--budget-gb", type=float, default=0.0, help="storage you are allowed to use (0 = all free)")
    d.add_argument("--assets-gb", type=float, default=0.0, help="size of the shard archives, if known")
    d.add_argument("--json")
    d.set_defaults(func=cmd_doctor)

    f = sub.add_parser("fetch", help="download artifacts (http, gh:TAG, or a directory)")
    f.add_argument("--source", required=True, help="https://.../ | gh:owner/repo:TAG | dir:PATH | PATH")
    f.add_argument("--dest", required=True)
    f.add_argument("--sums", action="append", default=[], help="SHA256SUMS file to check against")
    f.add_argument("--token", help="GitHub token (default: GITHUB_TOKEN / GH_TOKEN)")
    f.add_argument("--timeout", type=float, default=60.0)
    f.add_argument("--dry-run", action="store_true")
    f.set_defaults(func=cmd_fetch)

    v = sub.add_parser("verify", help="check sha256 of already-downloaded files")
    v.add_argument("--sums", action="append", required=True)
    v.add_argument("--dest", required=True)
    v.set_defaults(func=cmd_verify)

    m = sub.add_parser("merge", help="merge shard artifacts into a tree")
    m.add_argument("--dest", required=True)
    m.add_argument("--artifact", action="append", required=True, help="glob or path (repeatable)")
    m.add_argument("--ownership-dir", help="directory with owned.<worker>.txt from `fission split`")
    m.add_argument("--json")
    m.set_defaults(func=cmd_merge)

    fr = sub.add_parser("freshen", help="reconcile mtimes + build log so ninja has nothing to do")
    fr.add_argument("--ninja", action="append", default=[],
                    help="combined ninja file (only needed for --mode graph)")
    fr.add_argument("--root", required=True)
    fr.add_argument("--mode", choices=("uniform", "graph"), default="uniform",
                    help="uniform: restamp logged outputs (no graph parse, seconds). "
                         "graph: walk dependencies (accurate order, ~3 min and GBs of RAM on a real tree)")
    fr.set_defaults(func=cmd_freshen)

    r = sub.add_parser("report", help="what is on disk now")
    r.add_argument("--dest", required=True)
    r.add_argument("--json")
    r.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
