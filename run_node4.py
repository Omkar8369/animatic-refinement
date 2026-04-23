"""Local-friendly wrapper for Node 4's CLI.

On RunPod (and any standard Python install), `python -m pipeline.cli_node4 ...`
works directly from the repo root. On the user's Windows machine the
Python interpreter is the one embedded in ComfyUI portable, which uses a
`python313._pth` file and deliberately ignores `PYTHONPATH`. So the repo
root never ends up on `sys.path` automatically, and `-m pipeline.cli_node4`
fails with `ModuleNotFoundError: No module named 'pipeline'`.

This wrapper adds the repo root to `sys.path` explicitly and delegates
to `pipeline.cli_node4.main`, so it works identically in both environments:

    # Windows (embedded Python):
    "C:\\...\\python_embeded\\python.exe" run_node4.py --node3-result <path>

    # RunPod (standard Python):
    python run_node4.py --node3-result <path>
    # (or equivalently: python -m pipeline.cli_node4 --node3-result <path>)
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.cli_node4 import main  # noqa: E402 - path fixup must happen first


if __name__ == "__main__":
    sys.exit(main())
