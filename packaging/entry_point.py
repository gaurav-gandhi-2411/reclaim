"""Nuitka compilation entry point for the packaged `reclaim.exe`.

Nuitka compiles a script, not a console_scripts entry point reference — this thin wrapper is
the only thing that exists so a real script path can be handed to it. Mirrors
`reclaim.cli:main` exactly; kept out of `src/reclaim/` since it is packaging plumbing, not
library code.
"""

from __future__ import annotations

import sys

from reclaim.cli import main

if __name__ == "__main__":
    sys.exit(main())
