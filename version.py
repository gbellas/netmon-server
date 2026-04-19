"""Version metadata for the NetMon server.

`__version__` is the canonical release tag (bump on every tagged release).
`GIT_SHA` is resolved at import time from `git rev-parse HEAD`, falling
back to the "unknown" sentinel if the process can't run git (e.g. when
the server runs from an installed DMG with no .git directory).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__version__ = "1.0.0"


def _resolve_git_sha() -> str:
    try:
        repo_root = Path(__file__).resolve().parent
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode("ascii", "replace").strip() or "unknown"
    except Exception:
        return "unknown"


GIT_SHA: str = _resolve_git_sha()
