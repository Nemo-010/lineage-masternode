#!/usr/bin/env python3
"""Single-file entry point: works from a checkout with no install step."""

from __future__ import annotations

import sys

from mn.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
