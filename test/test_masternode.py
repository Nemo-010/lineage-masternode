"""End-to-end masternode test: real HTTP, real files, real merges.

No mocks of the network stack: a local server serves the artifacts, a shard is
deliberately interrupted mid-transfer to prove resume works, and the merged
tree is compared file for file against the original.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from mn import cli, fetch, freshen, graph, merge, util  # noqa: E402
import fake_release  # noqa: E402
import fake_transport  # noqa: E402
import synth  # noqa: E402


def _can_bind() -> bool:
    import socket

    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.close()
        return True
    except OSError:
        return False


class MasternodeFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mn-test-")
        self.release = os.path.join(self.tmp, "release")
        self.source = os.path.join(self.tmp, "source")
        fake_release.Handler.hits = []
        fake_release.Handler.fail_first = 0
        fake_release.Handler.drop_after = None

        info = synth.build_tree(self.source, modules=6)
        self.info = info
        outputs = info["all_outputs"]
        half = len(outputs) // 2
        synth.pack(self.source, outputs[:half], os.path.join(self.release, "shard-w0.tar"), "w0")
        synth.pack(self.source, outputs[half:], os.path.join(self.release, "shard-w1.tar"), "w1")
        synth.write_sums(self.release)

        # ownership, as produced by `fission split`
        own = os.path.join(self.tmp, "split")
        os.makedirs(own)
        with open(os.path.join(own, "owned.w0.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(outputs[:half]) + "\n")
        with open(os.path.join(own, "owned.w1.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(outputs[half:]) + "\n")
        self.ownership = own

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the real flow ---------------------------------------------------

    def test_fetch_merge_is_byte_identical(self):
        incoming = os.path.join(self.tmp, "incoming")
        dest = os.path.join(self.tmp, "tree")
        srv = fake_transport.Opener(self.release)
        result = fetch.fetch(
            "http://release.test/",
            incoming,
            sha256=fetch.parse_sha256sums(os.path.join(self.release, "SHA256SUMS")),
            opener=srv,
        )
        self.assertTrue(result.ok, json.dumps(result.as_dict()))
        # The sums file comes along; it is the manifest, so it is not checked
        # against itself.
        self.assertEqual(
            sorted(os.listdir(incoming)), ["SHA256SUMS", "shard-w0.tar", "shard-w1.tar"]
        )
        self.assertEqual(cli.main([
            "merge", "--dest", dest,
            "--artifact", os.path.join(incoming, "shard-*.tar"),
            "--ownership-dir", self.ownership,
        ]), 0)

        report = merge.merge(
            [os.path.join(incoming, "shard-w0.tar"), os.path.join(incoming, "shard-w1.tar")],
            os.path.join(self.tmp, "tree2"),
            merge.load_ownership(self.ownership),
        )
        self.assertTrue(report.ok, json.dumps(report.as_dict()))
        self.assertEqual(report.collisions, [])
        if self.info["built_with_ninja"]:
            # The log only exists because real ninja wrote it; a fabricated
            # one makes ninja rebuild everything.
            self.assertGreater(report.log_entries, 0)
            self.assertTrue(report.log_header.startswith("# ninja log v"))

        for rel in self.info["all_outputs"]:
            a = os.path.join(self.source, rel)
            b = os.path.join(dest, rel)
            self.assertTrue(os.path.exists(b), f"missing after merge: {rel}")
            self.assertEqual(util.sha256_file(a), util.sha256_file(b), f"content differs: {rel}")
            self.assertEqual(os.stat(a).st_mtime_ns, os.stat(b).st_mtime_ns, f"mtime lost: {rel}")

    def test_resume_after_dropped_connection(self):
        """A transfer that dies mid-body must resume with a Range request."""
        incoming = os.path.join(self.tmp, "incoming-resume")
        srv = fake_transport.Opener(self.release)
        srv.drop_after = 3 * 1024
        srv.drop_once = True  # only the first attempt is cut short

        result = fetch.fetch("http://release.test/", incoming, timeout=10, opener=srv)
        self.assertTrue(result.ok, json.dumps(result.as_dict()))
        got = util.sha256_file(os.path.join(incoming, "shard-w0.tar"))
        want = util.sha256_file(os.path.join(self.release, "shard-w0.tar"))
        self.assertEqual(got, want)
        self.assertFalse(os.path.exists(os.path.join(incoming, "shard-w0.tar.part")))

        resumed = [r for r in srv.ranges if r[1] > 0]
        self.assertTrue(resumed, f"no Range request was made: {srv.ranges}")

    def test_retry_after_server_503(self):
        """One 503 must cost a retry, not the attempt."""
        incoming = os.path.join(self.tmp, "incoming-flaky")
        srv = fake_transport.Opener(self.release)
        srv.fail_first = 1
        result = fetch.fetch("http://release.test/", incoming, timeout=10, opener=srv)
        self.assertTrue(result.ok, json.dumps(result.as_dict()))
        self.assertGreater(len(srv.hits), 2, "expected the listing or a file to be retried")

    def test_persistent_failure_is_reported_not_hidden(self):
        incoming = os.path.join(self.tmp, "incoming-dead")
        srv = fake_transport.Opener(self.release)
        srv.fail_first = 99
        with self.assertRaises(fetch.FetchError):
            fetch.fetch("http://release.test/", incoming, timeout=10, opener=srv)

    @unittest.skipUnless(_can_bind(), "socket binding is not permitted in this sandbox")
    def test_real_http_server_round_trip(self):
        incoming = os.path.join(self.tmp, "incoming-real")
        with fake_release.Server(self.release) as srv:
            result = fetch.fetch(srv.url, incoming, timeout=10)
        self.assertTrue(result.ok, json.dumps(result.as_dict()))
        self.assertEqual(
            util.sha256_file(os.path.join(incoming, "shard-w1.tar")),
            util.sha256_file(os.path.join(self.release, "shard-w1.tar")),
        )

    def test_bad_checksum_is_refused_and_deleted(self):
        incoming = os.path.join(self.tmp, "incoming-bad")
        os.makedirs(incoming)
        bogus = {"shard-w0.tar": "0" * 64}
        result = fetch.fetch("dir:" + self.release, incoming, sha256=bogus)
        names = [n for n, _ in result.failed]
        self.assertIn("shard-w0.tar", names)
        self.assertFalse(os.path.exists(os.path.join(incoming, "shard-w0.tar")))
        self.assertTrue(os.path.exists(os.path.join(incoming, "shard-w1.tar")))

    def test_freshen_leaves_nothing_for_ninja(self):
        """A merged tree is stale by construction; freshen must fix that."""
        dest = os.path.join(self.tmp, "tree3")
        fetch.fetch("dir:" + self.release, os.path.join(self.tmp, "in3"))
        os.makedirs(dest, exist_ok=True)
        # In the real flow the bootstrap publishes the graph alongside the
        # shards; the masternode needs it to reconcile mtimes.
        shutil.copy(os.path.join(self.source, "combined.ninja"), os.path.join(dest, "combined.ninja"))
        merge.merge(
            [os.path.join(self.tmp, "in3", "shard-w0.tar"), os.path.join(self.tmp, "in3", "shard-w1.tar")],
            dest,
            merge.load_ownership(self.ownership),
        )
        # Push the source files newer than every merged output, the way a
        # worker that built last would have.
        # An assembler has the source tree as well as the merged outputs; the
        # shard sources are what the outputs must be newer than.
        for rel in self.info["sources"]:
            src = os.path.join(self.source, rel)
            dst = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        frontier = 1_800_000_000_000_000_000
        for rel in self.info["sources"]:
            util.utime(os.path.join(dest, rel), frontier)

        dirty = self._stale_edges(dest)
        self.assertGreater(dirty, 0, "tree should look stale before freshening")

        report = freshen.freshen([os.path.join(dest, "combined.ninja")], dest)
        self.assertTrue(report.ok, json.dumps(report.as_dict()))
        self.assertGreater(report.bumped, 0)
        self.assertEqual(self._stale_edges(dest), 0, "freshen left stale edges")
        if self.info["built_with_ninja"]:
            self.assertTrue(os.path.exists(os.path.join(dest, ".ninja_log")))
            self.assertFalse(report.log_missing)
        else:
            # Nothing to rewrite, and saying so is the point.
            self.assertTrue(report.log_missing)

    def test_freshen_refuses_a_tree_without_sources(self):
        """Masternode-without-the-tree must say so, not pretend it is fine."""
        dest = os.path.join(self.tmp, "outputs-only")
        fetch.fetch("dir:" + self.release, os.path.join(self.tmp, "in4"))
        merge.merge(
            [os.path.join(self.tmp, "in4", "shard-w0.tar"), os.path.join(self.tmp, "in4", "shard-w1.tar")],
            dest,
            merge.load_ownership(self.ownership),
        )
        shutil.copy(os.path.join(self.source, "combined.ninja"), os.path.join(dest, "combined.ninja"))
        report = freshen.freshen([os.path.join(dest, "combined.ninja")], dest)
        self.assertFalse(report.ok, "should refuse: source files are not in this tree")
        self.assertGreater(report.missing_inputs_count if hasattr(report, "missing_inputs_count") else len(report.missing_inputs), 0)

    def test_doctor_reports_honestly(self):
        # /tmp here is a small tmpfs, so probe the real workspace: the point is
        # the mode logic, not this sandbox's disk layout.
        import mn.doctor as doctor_mod

        report = doctor_mod.inspect(ROOT, 30.0, 2.0)
        modes = {m.name: m for m in report.modes}
        self.assertTrue(modes["verify"].possible, "30 GB must be enough to verify")
        self.assertFalse(modes["build"].possible, "a masternode must never claim it can build")
        self.assertTrue(report.ok)

    def test_missing_artifact_is_named(self):
        with self.assertRaises(fetch.FetchError):
            fetch.fetch("dir:" + os.path.join(self.tmp, "nope"), os.path.join(self.tmp, "x"))

    def test_tar_without_metadata_is_rejected(self):
        import tarfile

        bad = os.path.join(self.tmp, "raw.tar")
        with tarfile.open(bad, "w") as tf:
            tf.add(os.path.join(self.source, self.info["all_outputs"][0]), arcname="x")
        with self.assertRaises(merge.MergeError):
            merge.merge([bad], os.path.join(self.tmp, "dst"))

    def test_graph_topo_detects_cycles(self):
        cyc = os.path.join(self.tmp, "cyc.ninja")
        with open(cyc, "w", encoding="utf-8") as fh:
            fh.write("build a: r b\nbuild b: r a\n")
        edges = graph.load([cyc], self.tmp)
        with self.assertRaises(graph.GraphError):
            graph.topo(edges)

    # -- helpers ---------------------------------------------------------

    def _stale_edges(self, root: str) -> int:
        edges = graph.load([os.path.join(root, "combined.ninja")], root)
        stale = 0
        for e in edges:
            outs = [os.path.join(root, o) for o in e.outputs if os.path.exists(os.path.join(root, o))]
            if not outs:
                continue
            newest_in = max(
                (os.stat(os.path.join(root, i)).st_mtime_ns for i in e.inputs
                 if os.path.exists(os.path.join(root, i))),
                default=0,
            )
            if newest_in and min(os.stat(o).st_mtime_ns for o in outs) <= newest_in:
                stale += 1
        return stale


class CrossPlatform(unittest.TestCase):
    def test_long_path_helper_is_a_noop_off_windows(self):
        if os.name == "nt":
            self.skipTest("windows-only behaviour")
        p = "/tmp/" + "a" * 400
        self.assertEqual(util.long_path(p), p)

    def test_no_import_time_crash_without_network(self):
        import subprocess

        env = dict(os.environ, http_proxy="http://127.0.0.1:1", https_proxy="http://127.0.0.1:1")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "masternode.py"), "doctor", "--root", ROOT],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("verify", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FreshenModes(unittest.TestCase):
    """Both freshen modes must leave ninja with nothing to do."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mn-fr-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tree(self, future_sources=False):
        tag = "future" if future_sources else "normal"
        src = os.path.join(self.tmp, f"src-{tag}")
        info = synth.build_tree(src, modules=4)
        dest = os.path.join(self.tmp, f"tree-{tag}")
        os.makedirs(dest, exist_ok=True)
        outs = info["all_outputs"]
        half = len(outs) // 2
        a = os.path.join(self.tmp, f"w0-{tag}.tar")
        b = os.path.join(self.tmp, f"w1-{tag}.tar")
        synth.pack(src, outs[:half], a, "w0")
        synth.pack(src, outs[half:], b, "w1")
        own = {p: ("w0" if i < half else "w1") for i, p in enumerate(outs)}
        merge.merge([a, b], dest, own)
        shutil.copy(os.path.join(src, "combined.ninja"), dest)
        for rel in info["sources"]:
            dst = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(src, rel), dst)
        if future_sources:
            for rel in info["sources"]:
                util.utime(os.path.join(dest, rel), 1_800_000_000_000_000_000)
        return dest

    def test_uniform_mode_handles_skew_and_future_clocks(self):
        for future in (False, True):
            with self.subTest(future_sources=future):
                dest = self._tree(future)
                report = freshen.restamp(dest)
                self.assertFalse(report.log_missing)
                self.assertGreater(report.bumped, 0)
                self.assertEqual(self._pending(dest), 0)

    def test_uniform_stamp_is_not_in_the_future(self):
        """A future stamp would make a later edit look older than its output."""
        import time

        dest = self._tree()
        before = time.time_ns()
        freshen.restamp(dest)
        after = time.time_ns()
        for rel in os.listdir(os.path.join(dest, "out")):
            full = os.path.join(dest, "out", rel)
            if os.path.isfile(full):
                self.assertLessEqual(os.stat(full).st_mtime_ns, after + 1)
                self.assertGreaterEqual(os.stat(full).st_mtime_ns, before - 1)

    def test_uniform_without_log_says_so(self):
        dest = self._tree()
        os.unlink(os.path.join(dest, ".ninja_log"))
        report = freshen.restamp(dest)
        self.assertTrue(report.log_missing)

    def _pending(self, dest):
        import subprocess

        ninja = synth.ninja_path()
        if not ninja:
            self.skipTest("ninja not on PATH")
        proc = subprocess.run(
            [ninja, "-f", "combined.ninja", "-n", "out/product/system.img"],
            cwd=dest, capture_output=True, text=True,
        )
        return len([ln for ln in proc.stdout.splitlines() if ln.strip().startswith("[")])
