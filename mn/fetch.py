"""Pull shard artifacts from anywhere, over stdlib only.

Four sources, because a masternode behind WSL or Windows may not have `gh`:

  http(s)  a directory listing or explicit URLs   (urllib)
  gh       a GitHub release via the REST API       (urllib + token)
  file     a local directory or UNC share          (copy)
  stdin    a list of URLs                          (urllib)

Every download is resumable and hash-checked before it is renamed into place,
so a flaky 30 GB link does not cost the one attempt.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import util

USER_AGENT = "lineage-masternode/0.1"


class FetchError(RuntimeError):
    pass


@dataclass
class Item:
    name: str
    url: str
    sha256: str | None = None
    size: int | None = None
    source: str = ""


@dataclass
class Result:
    items: list[Item] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "items": len(self.items),
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": [{"name": n, "error": e} for n, e in self.failed],
            "bytes": self.bytes,
        }

    @property
    def ok(self) -> bool:
        return not self.failed


def _opener(token: str | None = None):
    handlers: list = []
    if token:
        handlers.append(_AuthHandler(token))
    return urllib.request.build_opener(*handlers)


class _AuthHandler(urllib.request.BaseHandler):
    def __init__(self, token: str):
        self.token = token

    def https_request(self, req):
        req.add_header("Authorization", f"Bearer {self.token}")
        return req


def _retry(fn, attempts: int = 4, base_delay: float = 0.5):
    """Retry on the errors a long download actually produces.

    A flaky link, a proxy hiccup or a 503 from a release host should cost a
    retry, not the whole attempt -- and on a masternode there may be only one.
    """
    import time

    delay = base_delay
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (404, 401, 403):
                raise FetchError(f"HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(delay)
            delay *= 2
    raise FetchError(f"gave up after {attempts} attempts: {last}")


def parse_sha256sums(path: str) -> dict[str, str]:
    """Read a sha256sum -c file. Tolerates the '*' binary marker."""
    out: dict[str, str] = {}
    with util.open_any(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
            if m:
                out[os.path.basename(m.group(2).strip())] = m.group(1).lower()
    return out


def discover_http(
    base_url: str,
    timeout: float = 30.0,
    token: str | None = None,
    opener=None,
) -> list[Item]:
    """List a plain HTTP directory, or accept explicit file URLs."""
    opener = opener or _opener(token)
    if not base_url.endswith("/"):
        # an explicit file
        return [Item(name=os.path.basename(base_url), url=base_url, source="http")]
    req = urllib.request.Request(base_url, headers={"User-Agent": USER_AGENT})

    def get() -> str:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    body = _retry(get)
    items: list[Item] = []
    for href in re.findall(r'href="([^"]+)"', body, flags=re.I):
        if href.startswith(("?", "#", "/", "..")) or href.endswith("/"):
            continue
        url = urllib.parse.urljoin(base_url, href)
        items.append(Item(name=urllib.parse.unquote(os.path.basename(href)), url=url, source="http"))
    if not items:
        raise FetchError(f"{base_url}: no files found in listing")
    return items


def discover_github(
    repo: str,
    tag: str,
    token: str | None = None,
    timeout: float = 30.0,
    opener=None,
) -> list[Item]:
    """Release assets for one tag, via the REST API (no gh CLI needed)."""
    api = f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    opener = opener or _opener(token)

    def get() -> dict:
        with opener.open(req, timeout=timeout) as resp:
            return __import__("json").loads(resp.read().decode())

    try:
        data = _retry(get)
    except FetchError as exc:
        raise FetchError(f"{api}: {exc} (is the tag right? is the token valid?)") from exc
    items = []
    for asset in data.get("assets", []):
        items.append(
            Item(
                name=asset["name"],
                url=asset.get("browser_download_url") or asset["url"],
                size=asset.get("size"),
                source="github",
            )
        )
    if not items:
        raise FetchError(f"{repo}@{tag}: release has no assets")
    return items


def discover_dir(path: str) -> list[Item]:
    if not os.path.isdir(path):
        raise FetchError(f"{path}: not a directory")
    items = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            items.append(Item(name=name, url=full, size=os.path.getsize(full), source="file"))
    if not items:
        raise FetchError(f"{path}: no files")
    return items


def _download_http(item: Item, dest: str, token: str | None, timeout: float, opener=None) -> int:
    """Stream to .part, resume if a partial exists, rename when complete."""
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if item.size is not None and have == item.size:
        util.replace(part, dest)
        return have
    req = urllib.request.Request(item.url, headers={"User-Agent": USER_AGENT})
    if have:
        req.add_header("Range", f"bytes={have}-")
    opener = opener or _opener(token)

    def pull() -> None:
        nonlocal have
        req2 = urllib.request.Request(item.url, headers={"User-Agent": USER_AGENT})
        if have:
            req2.add_header("Range", f"bytes={have}-")
        with opener.open(req2, timeout=timeout) as resp:
            if have and resp.status != 206:
                have = 0  # server ignored the range; start over honestly
            mode = "ab" if have else "wb"
            with util.open_any(part, mode) as fh:
                while True:
                    block = resp.read(util.CHUNK)
                    if not block:
                        break
                    fh.write(block)
                    have += len(block)

    try:
        _retry(pull)
    except FetchError as exc:
        raise FetchError(f"{item.name}: {exc}") from exc
    util.replace(part, dest)
    return os.path.getsize(dest)


def fetch(
    source: str,
    dest_dir: str,
    sha256: dict[str, str] | None = None,
    token: str | None = None,
    timeout: float = 60.0,
    dry_run: bool = False,
    opener=None,
) -> Result:
    """Bring every artifact for `source` into `dest_dir`, verified."""
    util.makedirs(dest_dir)
    if source.startswith(("http://", "https://")):
        items = discover_http(source, timeout=timeout, token=token, opener=opener)
    elif source.startswith("gh:"):
        _, repo, tag = source.split(":", 2)
        items = discover_github(repo, tag, token=token, timeout=timeout, opener=opener)
    elif source.startswith("dir:"):
        items = discover_dir(source[4:])
    elif os.path.isdir(source):
        items = discover_dir(source)
    else:
        raise FetchError(f"don't understand source {source!r} (http://, gh:owner/repo:TAG, dir:PATH)")

    result = Result(items=items)
    sha256 = sha256 or {}
    for item in items:
        dest = os.path.join(dest_dir, item.name)
        # The sums file is the manifest: fetch it, but do not check it against
        # itself. Everything else is checked.
        want = None if item.name == "SHA256SUMS" else (item.sha256 or sha256.get(item.name))
        if os.path.exists(dest):
            if want is None:
                result.skipped.append(item.name)
                continue
            if util.sha256_file(dest) == want:
                result.skipped.append(item.name)
                continue
            os.unlink(util.long_path(dest))
        if dry_run:
            result.skipped.append(item.name)
            continue
        try:
            if item.source == "file":
                tmp = dest + ".part"
                _copy_local(item.url, tmp)
                util.replace(tmp, dest)
                size = os.path.getsize(dest)
            else:
                size = _download_http(item, dest, token, timeout, opener=opener)
        except (FetchError, OSError) as exc:
            result.failed.append((item.name, str(exc)))
            continue
        if want and util.sha256_file(dest) != want:
            os.unlink(util.long_path(dest))
            result.failed.append((item.name, "sha256 mismatch"))
            continue
        result.downloaded.append(item.name)
        result.bytes += size
    return result


def _copy_local(src: str, dst: str) -> None:
    with util.open_any(src, "rb") as r, util.open_any(dst, "wb") as w:
        while True:
            b = r.read(util.CHUNK)
            if not b:
                break
            w.write(b)
