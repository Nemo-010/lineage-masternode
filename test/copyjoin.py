"""Concatenate files. The fixture's stand-in for `cat` and `cp`.

The synthetic build in this repo has to run on Windows too, and Windows has
neither of those commands. A Python helper is the one interpreter the tests can
guarantee is present, since they are Python tests.

    python copyjoin.py OUT IN [IN ...]
"""

import shutil
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    out, *inputs = argv[1:]
    with open(out, "wb") as dst:
        for path in inputs:
            with open(path, "rb") as src:
                shutil.copyfileobj(src, dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
