"""Import a ``scripts/`` module by path — the one loader every script test shares.

A script lives outside the package and is usually hyphenated, so no import statement can name it;
reaching its pure parts at all means going through ``importlib``. That was hand-rolled once per
script test, and each copy re-typed the module name as a literal beside the path it came from —
two encodings of one fact, where a typo silently registers the script under a name that shadows
something else in ``sys.modules``. Here the name is DERIVED from the filename, so there is nothing
left to mistype.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


def load_script(path: Path) -> ModuleType:
    """Load the script at ``path`` as a module named after its file, and return it.

    Args:
        path: The script to import, hyphenated or not; its stem becomes the module name with
            hyphens mapped to underscores.

    Returns:
        The executed module, registered in ``sys.modules`` under that derived name before it runs
        — which is what lets a dataclass the script defines resolve its own module on lookup.

    Raises:
        AssertionError: The path yielded no loadable spec — a missing or unreadable script.
    """
    name = path.stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
