"""A throwaway HTTP server that behaves like a release host.

It supports directory listings, Range requests (so resume is exercised for
real), and it can be told to fail the first N requests or drop a connection
mid-body, because "the link is flaky" is the normal case for a 30 GB pull.
"""

from __future__ import annotations

import functools
import http.server
import os
import re
import socketserver
import threading


class Handler(http.server.SimpleHTTPRequestHandler):
    fail_first = 0
    drop_after = None
    hits: list[str] = []
    lock = threading.Lock()

    def log_message(self, fmt, *args):  # keep test output clean
        pass

    def _count(self, path: str) -> int:
        with self.lock:
            self.hits.append(path)
            return sum(1 for h in self.hits if h == path)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        n = self._count(path)

        if Handler.fail_first and n <= Handler.fail_first:
            self.send_error(503, "flaky link, try again")
            return

        if path.endswith("/"):
            return self._listing(path)

        full = os.path.join(self.directory, path.lstrip("/"))
        if not os.path.isfile(full):
            self.send_error(404)
            return

        size = os.path.getsize(full)
        rng = self.headers.get("Range")
        start = 0
        if rng:
            m = re.match(r"bytes=(\d+)-", rng)
            if m:
                start = int(m.group(1))
        if start:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        else:
            self.send_response(200)
        body_len = size - start
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(body_len))
        self.end_headers()

        with open(full, "rb") as fh:
            fh.seek(start)
            sent = 0
            while True:
                block = fh.read(1 << 16)
                if not block:
                    break
                if Handler.drop_after is not None and sent + len(block) > Handler.drop_after:
                    keep = Handler.drop_after - sent
                    if keep > 0:
                        self.wfile.write(block[:keep])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(block)
                sent += len(block)

    def _listing(self, path):
        full = os.path.join(self.directory, path.lstrip("/"))
        if not os.path.isdir(full):
            self.send_error(404)
            return
        names = sorted(n for n in os.listdir(full) if os.path.isfile(os.path.join(full, n)))
        body = "<html><body>" + "".join(f'<a href="{n}">{n}</a>\n' for n in names) + "</body></html>"
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server:
    def __init__(self, directory: str):
        self.directory = directory

    def __enter__(self):
        # In Python 3, SimpleHTTPRequestHandler.__init__ overwrites
        # self.directory with os.getcwd() when it is only a class attribute,
        # so the directory has to travel through the constructor.
        handler = functools.partial(Handler, directory=self.directory)
        socketserver.TCPServer.allow_reuse_address = True
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    @property
    def hits(self) -> list[str]:
        return Handler.hits

    @property
    def handler(self):
        return self.httpd.RequestHandlerClass
