"""Just enough ninja to reconcile mtimes after a merge.

The masternode does not build. It only needs the dependency edges so it can
make a merged tree look consistent to ninja: an output must not be older than
its newest input, or ninja re-runs edges that are already correct (and the
masternode has no compiler to re-run them with).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import util


class GraphError(RuntimeError):
    pass


@dataclass
class Edge:
    outputs: list[str]
    inputs: list[str]
    rule: str


def _logical(text: str):
    buf = ""
    for raw in text.splitlines():
        stripped = raw.rstrip()
        trailing = len(stripped) - len(stripped.rstrip("$"))
        if trailing % 2 == 1:
            buf += stripped[:-1]
            continue
        buf += raw
        yield buf
        buf = ""
    if buf:
        yield buf


def _tokens(line: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "$" and i + 1 < n:
            nxt = line[i + 1]
            if nxt in "$ :":
                cur.append(nxt)
                i += 2
                continue
            cur.append(c)
            i += 1
            continue
        if c in " \t":
            if cur:
                out.append("".join(cur))
                cur = []
            i += 1
            continue
        if c == ":":
            if cur:
                out.append("".join(cur))
                cur = []
            out.append(":")
            i += 1
            continue
        cur.append(c)
        i += 1
    if cur:
        out.append("".join(cur))
    return out


def load(paths: list[str], root: str) -> list[Edge]:
    edges: list[Edge] = []
    seen: set[str] = set()

    def parse(path: str) -> None:
        real = os.path.realpath(path)
        if real in seen:
            return
        seen.add(real)
        with util.open_any(path, "r") as fh:
            lines = list(_logical(fh.read()))
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if line[0] in " \t":
                continue
            if stripped.startswith("build "):
                toks = _tokens(stripped[6:])
                if ":" not in toks:
                    continue
                c = toks.index(":")
                outs = toks[:c]
                rest = toks[c + 1:]
                if not rest:
                    continue
                rule, ins = rest[0], rest[1:]
                explicit = []
                for t in ins:
                    if t in ("|", "||"):
                        break
                    explicit.append(t)
                edges.append(Edge(outputs=outs, inputs=explicit, rule=rule))
                continue
            if stripped.startswith(("subninja ", "include ")):
                target = stripped.split(" ", 1)[1].strip()
                for base in (os.path.dirname(path), root):
                    cand = os.path.join(base, target) if base else target
                    if os.path.exists(util.long_path(cand)):
                        parse(cand)
                        break
                continue
        return

    for p in paths:
        parse(p)
    if not edges:
        raise GraphError("no build edges found; is this the combined ninja file?")
    return edges


def topo(edges: list[Edge]) -> list[int]:
    producer: dict[str, int] = {}
    for i, e in enumerate(edges):
        for o in e.outputs:
            producer[o] = i
    indeg = [0] * len(edges)
    users: list[list[int]] = [[] for _ in edges]
    for i, e in enumerate(edges):
        for dep in e.inputs:
            j = producer.get(dep)
            if j is not None and j != i:
                indeg[i] += 1
                users[j].append(i)
    ready = [i for i, d in enumerate(indeg) if d == 0]
    order: list[int] = []
    while ready:
        i = ready.pop()
        order.append(i)
        for u in users[i]:
            indeg[u] -= 1
            if indeg[u] == 0:
                ready.append(u)
    if len(order) != len(edges):
        raise GraphError("dependency cycle in the ninja graph")
    return order
