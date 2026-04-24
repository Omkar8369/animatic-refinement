"""Local-friendly wrapper for Node 7's CLI.

On RunPod (and any standard Python install), `python -m pipeline.cli_node7 ...`
works directly from the repo root. On the user's Windows machine the
Python interpreter is the one embedded in ComfyUI portable, which uses a
`python313._pth` file and deliberately ignores `PYTHONPATH`. So the repo
root never ends up on `sys.path` automatically, and `-m pipeline.cli_node7`
fails with `ModuleNotFoundError: No module named 'pipeline'`.

This wrapper adds the repo root to `sys.path` explicitly and delegates
to `pipeline.cli_node7.main`, so it works identically in both environments:

    # Windows (embedded Python, --dry-run only -- no VRAM for the real run):
    "C:\\...\\python_embeded\\python.exe" run_node7.py \\
        --node6-result <path> --queue <path> --dry-run

    # RunPod (standard Python, ComfyUI listening on 8188):
    python run_node7.py --node6-result <path> --queue <path>
    # (or equivalently: python -m pipeline.cli_node7 ...)

Live runs require a RunPod pod with ComfyUI running (locked decision
#13). Node 7 is the only node whose real path cannot be exercised on
the laptop because of VRAM/weight-download constraints.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.cli_node7 import main  # noqa: E402 - path fixup must happen first


if __name__ == "__main__":
    sys.exit(main())
