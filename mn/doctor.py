"""Tell the truth about this machine before it spends a night downloading."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field

from . import util

# What a finished LineageOS artefact set costs, measured on a real device
# (alioth, lineage-24.0): the flashable zip is 1.443 GB. The rest is headroom.
OTA_GB = 1.443


@dataclass
class Mode:
    name: str
    needed_gb: float
    possible: bool
    why: str


@dataclass
class Report:
    free_gb: float
    total_gb: float
    budget_gb: float
    ram_gb: float
    cpus: int
    platform: str
    modes: list[Mode] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "free_gb": round(self.free_gb, 2),
            "total_gb": round(self.total_gb, 2),
            "budget_gb": self.budget_gb,
            "ram_gb": round(self.ram_gb, 2),
            "cpus": self.cpus,
            "platform": self.platform,
            "modes": [
                {"name": m.name, "needed_gb": m.needed_gb, "possible": m.possible, "why": m.why}
                for m in self.modes
            ],
            "ok": self.ok,
        }

    @property
    def ok(self) -> bool:
        return any(m.possible for m in self.modes)


def _ram_gb() -> float:
    try:
        if hasattr(os, "sysconf"):
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        pass
    return 0.0


def inspect(root: str, budget_gb: float, assets_gb: float = 0.0) -> Report:
    usage = shutil.disk_usage(root)
    free = usage.free / 1e9
    total = usage.total / 1e9
    budget = min(budget_gb, free) if budget_gb else free

    modes = []
    # verify-only: the finished zip + checksums, plus unpack headroom
    verify_need = OTA_GB * 1.3
    modes.append(
        Mode(
            "verify",
            round(verify_need, 2),
            budget >= verify_need,
            f"download {OTA_GB:.2f} GB of artefacts and check their hashes",
        )
    )
    # assemble: shard archives stay on disk while the tree is unpacked, and the
    # images are written afterwards, so peak is roughly 3x the archive set.
    assemble_need = max(assets_gb * 3.0, 8.0)
    modes.append(
        Mode(
            "assemble",
            round(assemble_need, 2),
            budget >= assemble_need,
            f"unpack {assets_gb:.2f} GB of shards into a tree and produce images",
        )
    )
    # build: not this machine's job
    modes.append(
        Mode("build", 120.0, False if total < 120 else budget >= 120, "sync ~100 GB and compile; not a masternode task")
    )

    return Report(
        free_gb=free,
        total_gb=total,
        budget_gb=budget_gb,
        ram_gb=_ram_gb(),
        cpus=os.cpu_count() or 1,
        platform=util.platform_line(),
        modes=modes,
    )


def render(report: Report) -> str:
    note = ""
    if report.budget_gb:
        note = f"  (budget {report.budget_gb:.0f} GB"
        if report.free_gb < report.budget_gb:
            note += f", capped at the {report.free_gb:.1f} GB actually free"
        note += ")"
    lines = [
        f"platform   {report.platform}",
        f"disk       {report.free_gb:.1f} GB free of {report.total_gb:.1f} GB" + note,
        f"memory     {report.ram_gb:.1f} GB",
    ]
    for m in report.modes:
        state = "ok  " if m.possible else "no  "
        lines.append(f"{state}{m.name:9} needs ~{m.needed_gb:6.1f} GB  {m.why}")
    return "\n".join(lines)


def main(root: str, budget_gb: float, assets_gb: float) -> int:
    report = inspect(root, budget_gb, assets_gb)
    print(render(report))
    return 0 if report.ok else 1
