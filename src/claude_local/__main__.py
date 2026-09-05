"""``python -m claude_local`` — the installed console script's equivalent for an uninstalled tree.

Both entry points resolve to the same ``cli.main``, so a task run out of a checkout and one run
from PATH cannot diverge. A dispatching orchestrator always uses the console script (it execs a
bare binary name); this module is what makes the same front door reachable without installing.
"""

from __future__ import annotations

from claude_local.cli import main

raise SystemExit(main())
