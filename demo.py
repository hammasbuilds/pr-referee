"""Show what pr-referee does, in one command, with nothing to set up.

    python demo.py

Scores the referee against diffs whose answer is known by construction:
some provably change behaviour, some provably do not. Reviewing real pull
requests produces opinions; this produces a number, because the ground
truth was built in.

The target is `toolz`, a real library vendored under targets/ - not a fixture
built to flatter the tool. Runs with --no-llm, so it needs no model and no GPU - the structural
checks are the half that can be scored deterministically.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    print('pr-referee: can it tell a behaviour-changing diff from an equivalent one?', flush=True)
    print(flush=True)
    result = subprocess.run(
        [sys.executable, "-m", 'pr_referee.cli', *['bench', 'targets/toolz', '--no-llm', '--limit', '5']],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    if result.returncode != 0:
        return result.returncode
    print(flush=True)
    for line in ['Run the full referee, with the model, using:', '    pr-referee review <repo>', '    pr-referee bench <repo>            # add --no-llm to skip the model']:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
