# lineage-masternode

The small machine at the end of a split LineageOS build. It pulls the shard
artifacts, checks every hash, merges them into one tree, makes ninja agree the
tree is up to date, and reports what it actually got.

One file of dependency: **none**. Stdlib only, Python 3.9+ (tested on 3.14),
and it is written to run on Linux, macOS and Windows.

## Try it in one command

No LineageOS tree, no network, no `pip install`:

    python3 demo/run_demo.py

That builds a synthetic fleet (three shards, real tars, real hashes, a real
`.ninja_log` from real ninja), then runs the shipped code against it: doctor →
fetch → verify → merge → freshen → report, ending with a ninja run that must
have nothing to do. Expect ~1 second and `pending edges: 0`.

    python3 -m unittest discover -s test      # 23 tests

## Running it for real

    masternode.py doctor   --budget-gb 30
    masternode.py fetch    --source gh:owner/repo:fission-1234 --dest incoming
    masternode.py verify   --sums incoming/SHA256SUMS --dest incoming
    masternode.py merge    --dest tree --artifact 'incoming/shard-*.tar' --ownership-dir split
    masternode.py freshen  --root tree          # uniform restamp: seconds, no graph
    masternode.py report   --dest tree

## What 30 GB buys you

`doctor` answers this from the real disk, not from a table:

```
platform   linux python 3.12 12 cpu
disk       22.6 GB free of 255.7 GB  (budget 30 GB)
memory     12.4 GB
ok  verify    needs ~   1.9 GB  download 1.44 GB of artefacts and check their hashes
ok  assemble  needs ~   8.0 GB  unpack 0.00 GB of shards into a tree and produce images
no  build     needs ~ 120.0 GB  sync ~100 GB and compile; not a masternode task
```

- **verify** — the flashable zip for alioth is 1.443 GB (measured from the
  LineageOS API), so 30 GB is comfortable.
- **assemble** — possible if the shard archives are a few GB, because unpacking
  needs roughly 3x their size in peak space. `doctor` takes the real archive
  size once you have downloaded, so this stops being a guess.
- **build** — never. A masternode that claims it can compile is lying.

## Pulling shards from a GitHub release

`fetch` takes an `https://` release URL, a `gh:owner/repo:TAG` shorthand, or a
plain directory (`dir:PATH`, useful for a local dry run). The `gh:` path goes
through the REST API with `urllib`, so no `gh` CLI is needed — but GitHub
rate-limits anonymous API calls to 60/hour, which a release listing burns
quickly. Give it a token:

    export GITHUB_TOKEN=...          # or GH_TOKEN, or --token ...
    python3 masternode.py fetch --source gh:owner/repo:fission-1234 --dest incoming

No scope beyond `public_repo` (or none at all, for a public release) is needed;
the token is used as a bearer header and nothing else.

## What it refuses to do

The interesting behaviour is what it *declines*:

- **`freshen` fails if the source tree is missing.** Reconciling mtimes needs
  the inputs as well as the outputs. A tree with only artifacts gets
  `missing_inputs` and `ok: false` instead of a cheerful "done".
- **`merge` fails on unowned or unverifiable paths**, and reports every
  collision where two workers produced different bytes for one path (the plan's
  owner wins, and the count is in the report).
- **`freshen` flags `suspicious_skew`** when the gap is over an hour, because
  that is not worker clock skew — it is a wrong tree or a wrong clock.
- **`fetch` deletes a file whose sha256 does not match** rather than leaving a
  poisoned artifact behind.

## Sizing: what the assembler actually costs

Measured on a 12 vCPU / NVMe box, warm cache (CI runners are 2-4x slower on
disk, comparable on SHA):

| Operation | Measured | Notes |
|---|---|---|
| sha256 (verify) | 1.56 GB/s | CPU-bound, SHA extensions |
| tar extract (merge) | 0.71 GB/s | warm cache; ~0.2 GB/s on network-backed disks |
| tar create (pack) | 0.83 GB/s | |
| deflate -6 (OTA zip) | 3.1 GB/min | single core, incompressible payload |
| zip STORED | 58 GB/min | what an OTA does with `payload.bin` |
| uniform restamp | ~106,000 files/s | 1M files ≈ 9 s |
| **Python parse of a ninja graph** | **8.1 MB/s** | 1.5 GB combined.ninja ≈ **3.1 min + GBs of RAM** |

That last row is why `freshen` defaults to `--mode uniform`: it restamps every
logged output to one timestamp instead of walking the dependency graph. Ninja
calls an edge dirty only when an output is *older* than an input, so one shared
stamp is sufficient, and correctness rests on the sha256 verification, not on
timestamps. `--mode graph` still exists for the paranoid.

Consequence for the 30 GB budget: the masternode no longer needs the 1.5 GB
`combined.ninja` at all, nor the memory to hold a few million edges.

Worked assembly estimate for a 20 GB merged tree, ~1.44 GB OTA:

    download shards    20 GB at 20 MB/s        17 min   (network, dominates)
    verify             20 GB at 1.5 GB/s         13 s
    merge              20 GB at 0.7 GB/s         29 s
    restamp            ~500k files               5 s
    images + OTA zip   ~5 GB write + deflate     2-4 min
                                                ------
                                                ~20-25 min

Only ~5 minutes of that is this machine's work; the rest is the network.

## Cross-platform notes

Written for Windows as well as POSIX, because the machine may be WSL or native:

- `\\?\` long-path prefix is applied automatically past ~240 chars; AOSP
  intermediate paths are longer than MAX_PATH.
- `sha256sum -c` is reimplemented in Python (`verify`), since Windows has no
  `sha256sum`.
- Symlink members are created on POSIX and skipped (and counted) on Windows,
  where they need privileges.
- No `gh` CLI, no `git`, no `ninja` requirement: GitHub releases are read over
  the REST API with `urllib`, and the ninja graph is parsed by this repo.
- Atomic `os.replace`, no `fcntl`, no `fork`.

The Windows logic is tested by flipping the platform flag
(`test/test_windows.py`): long-path prefixing, UNC shares, already-prefixed
paths, and a symlink member being skipped and counted instead of killing the
merge. Writing those tests found two real bugs — `abspath()` was mangling UNC
paths, and unclaimed symlink members were being dropped silently.

What is *not* proven: this has not been executed on Windows itself. The logic
is exercised, the platform is not.

## Measured run

`python3 demo/run_demo.py` builds a synthetic fleet (three shards, real tars,
real ninja build log), then runs the shipped masternode code against it:

```
[4] masternode merge
    written 25, deduped 0, collisions 0, verified 25, log entries 25
    log header "# ninja log v6"          <- whatever this machine's ninja wrote
[5] masternode freshen   (uniform restamp, 0.052s, no graph parsed)
    considered 25, bumped 25, log_rewritten 25, missing_inputs 0, ok true
[6] does ninja agree there is nothing left to do?
    ninja: no work to do.
    pending edges: 0
[7] 39 files compared, 0 mismatched
    total wall time: 0.33s
```

The header above is `v6` because this machine has ninja 1.12.1; ninja 1.13
writes `v7`. It is read from the tree, never assumed — a log whose version is
newer than the reader's ninja is discarded with *"build log version is too new;
starting over"*, and then every edge is rebuilt. That is the trap this fixture
used to fall into.

The fleet side is synthesised — there is no LineageOS tree here — but the
artifacts are real tars with real hashes and a **real ninja build log**, and
every masternode step is the shipped code.

`python3 demo/check_contract.py` goes further: it packs artifacts with the
producer (`lineage-fission`), merges them with **both** independent
implementations, and compares the trees. Last run: 75 files compared, 0
differences, same `# ninja log v6` header.

## Tests

    python3 -m unittest discover -s test -v      # 23 tests

Covers: the Windows path/symlink paths, resume after a connection dies mid-body (and that the resumed request
actually sent a `Range` header), retry after 503, permanent failure reported
rather than hidden, checksum mismatch refused and deleted, merge dedupe vs
collision-with-owner, artifact without metadata rejected, dependency-cycle
detection, freshen fixing a genuinely stale tree, freshen refusing a tree with
no sources, and `doctor` never claiming a build is possible.

The HTTP tests normally use an injectable transport (`test/fake_transport.py`)
so they run even where binding a socket is forbidden. A real `http.server`
round trip is also exercised whenever binding works, which is where a defect of
its own turned up: `SimpleHTTPRequestHandler` overwrites the `directory` class
attribute with `os.getcwd()` in `__init__`, so the "real server" test was
serving this repo instead of the fixture directory and passed on the wrong
files. The directory now goes through the constructor
(`functools.partial`), and the test fails if a single byte differs.

## Three things that are easy to get wrong

All were found by building this, and each one would waste the one attempt:

1. **tar stores mtime in whole seconds.** Merged outputs came back older than
   their inputs and ninja declared the whole tree dirty. The artifact manifest
   carries nanoseconds; merge restores from that.
2. **a fabricated `.ninja_log` makes ninja rebuild everything.** Ninja stores a
   hash of the evaluated command line and the file's mtime; inventing either
   makes every edge look changed ("command line changed" / "command line not
   found in log"). The log must travel from the workers. `fetch`/`merge` keep
   it, and `freshen` rewrites only the mtime field of entries it actually
   bumped.
3. **the log's version header is not decoration.** v5 and v6 and v7 differ, and
   a reader that finds a header newer than itself prints *"build log version is
   too new; starting over"* and silently rebuilds the tree from scratch. The
   header travels with the artifacts and merge keeps the newest one it is given;
   it is never hardcoded to whatever the author had installed.

## Layout

    masternode.py       entry point
    mn/util.py          long paths, hashing, atomic replace, disk math
    mn/fetch.py         http / gh: / dir: sources, resume, retry, checksums
    mn/merge.py         ownership-aware merge, log reassembly, verification
    mn/graph.py         compact ninja reader (edges + topo order)
    mn/freshen.py       mtime reconciliation + log rewrite
    mn/doctor.py        what this machine can honestly do
    mn/cli.py           subcommands
    test/               23 tests + the synthetic fleet
    demo/run_demo.py    the measured run above
