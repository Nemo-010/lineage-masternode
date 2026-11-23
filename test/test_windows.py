"""Exercise the Windows code paths without Windows.

`util.WIN` is a module flag, so the long-path logic can be tested for real on
Linux -- which is the only way to know it is right before the one attempt on a
machine that might be WSL or native Windows.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mn import util  # noqa: E402


class WindowsPaths(unittest.TestCase):
    def test_short_path_is_untouched(self):
        # Built from this platform's root: "/build/..." is not absolute on
        # Windows, and the point of the test is the 260-char prefix, not
        # abspath's drive handling.
        p = os.path.join(os.path.abspath(os.sep), "build", "lineage", "out")
        with mock.patch.object(util, "WIN", True):
            self.assertEqual(util.long_path(p), os.path.abspath(p))

    def test_long_path_gets_the_prefix(self):
        deep = "/build/lineage/" + "/".join(["out"] + ["deep_component_name"] * 12)
        self.assertGreater(len(deep), 240)
        with mock.patch.object(util, "WIN", True):
            got = util.long_path(deep)
        self.assertTrue(got.startswith("\\\\?\\"), got)
        self.assertTrue(got.endswith(os.path.basename(deep)))

    def test_already_prefixed_is_left_alone(self):
        with mock.patch.object(util, "WIN", True):
            p = "\\\\?\\C:\\build\\x"
            self.assertEqual(util.long_path(p), p)

    def test_unc_share_uses_unc_form(self):
        with mock.patch.object(util, "WIN", True):
            p = "\\\\server\\share\\" + "d" * 300
            got = util.long_path(p)
        self.assertTrue(got.startswith("\\\\?\\UNC\\"), got)

    def test_off_windows_nothing_changes(self):
        p = "/" + "a" * 400
        with mock.patch.object(util, "WIN", False):
            self.assertEqual(util.long_path(p), p)

    def test_symlinks_are_skipped_on_windows(self):
        """Merge must not die on a symlink member when it cannot create one."""
        import io
        import tarfile
        import tempfile

        from mn import merge

        tmp = tempfile.mkdtemp(prefix="mn-win-")
        try:
            root = os.path.join(tmp, "tree")
            os.makedirs(os.path.join(root, "out"), exist_ok=True)
            with open(os.path.join(root, "out", "real"), "w", encoding="utf-8") as fh:
                fh.write("data\n")

            tar_path = os.path.join(tmp, "w0.tar")
            meta = {
                "worker": "w0",
                "files": [{"path": "out/real", "size": 5, "sha256": util.sha256_file(
                    os.path.join(root, "out", "real")), "mode": 0o644, "mtime_ns": 1}],
                "owners": {"out/real": "w0"},
                "ninja_log_header": "# ninja log v7",
            }
            import json

            with tarfile.open(tar_path, "w") as tf:
                info = tf.gettarinfo(os.path.join(root, "out", "real"), arcname="out/real")
                with open(os.path.join(root, "out", "real"), "rb") as fh:
                    tf.addfile(info, fh)
                link = tarfile.TarInfo("out/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "real"
                tf.addfile(link)
                blob = json.dumps(meta).encode()
                ti = tarfile.TarInfo(".fission-artifact.json")
                ti.size = len(blob)
                tf.addfile(ti, io.BytesIO(blob))

            dest = os.path.join(tmp, "merged")
            with mock.patch.object(util, "WIN", True), mock.patch.object(merge.os, "name", "nt"):
                report = merge.merge([tar_path], dest)
            self.assertEqual(report.symlinks, 0)
            self.assertEqual(report.skipped_members, 1, "symlink should be skipped and counted")
            self.assertTrue(os.path.exists(os.path.join(dest, "out", "real")))
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
