"""An in-process stand-in for urllib's opener.

Sockets are not always available (CI sandboxes, locked-down Windows hosts), but
the interesting behaviour -- resume from a partial file, 206 replies, 503
retries, a connection dying mid-body -- still has to be tested. This speaks the
same API as urllib.request.OpenerDirector as far as fetch.py uses it.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.request


class Response:
    def __init__(self, body: bytes, status: int = 200):
        self.status = status
        self._body = body
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data = self._body[self._pos:]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos:self._pos + n]
        self._pos += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """Serves a directory over the opener API. Knobs: fail_first, drop_after."""

    def __init__(self, directory: str):
        self.directory = directory
        self.fail_first = 0
        self.drop_after: int | None = None
        self.drop_once = False
        self.hits: list[str] = []
        self.ranges: list[tuple[str, int]] = []
        self._dropped: set[str] = set()

    def open(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        path = urllib.request.urlparse(url).path.lstrip("/")
        self.hits.append(path)
        n = sum(1 for h in self.hits if h == path)
        if self.fail_first and n <= self.fail_first:
            raise urllib.error.HTTPError(url, 503, "flaky link", {}, None)

        if path == "" or path.endswith("/"):
            full = os.path.join(self.directory, path)
            if not os.path.isdir(full):
                raise urllib.error.HTTPError(url, 404, "not found", {}, None)
            names = sorted(n for n in os.listdir(full) if os.path.isfile(os.path.join(full, n)))
            body = ("<html>" + "".join(f'<a href="{n}">{n}</a>' for n in names) + "</html>").encode()
            return Response(body, 200)

        full = os.path.join(self.directory, path)
        if not os.path.isfile(full):
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        with open(full, "rb") as fh:
            data = fh.read()

        rng = req.get_header("Range") if hasattr(req, "get_header") else None
        start = 0
        if rng:
            m = re.match(r"bytes=(\d+)-", rng)
            if m:
                start = int(m.group(1))
        body = data[start:]
        self.ranges.append((path, start))
        if self.drop_after is not None and len(body) > self.drop_after:
            if self.drop_once and path in self._dropped:
                return Response(body, 206 if start else 200)
            self._dropped.add(path)
            return Dropped(body[: self.drop_after], 200)
        return Response(body, 206 if start else 200)


class Dropped(Response):
    """A response whose connection dies after the bytes it pretends to have."""

    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body, status)

    def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._body):
            raise ConnectionResetError("connection dropped mid-body")
        return super().read(n)
