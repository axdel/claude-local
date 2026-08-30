"""Load benchmark cases from their on-disk fixtures and ``case.toml`` manifests.

A case directory is data, not code: ``case.toml`` names the implementation hole
(``impl_path``), its model-visible neighbors (``context_paths``), and the loop bounds
(``[budget]``); ``spec.md``, ``oracle.py``, and ``blank/<impl_path>`` sit beside it.
``load_case`` reads those, snapshots the whole golden app tree so the assembled worktree
imports and boots, and returns a validated :class:`BenchmarkCase`. ``load_suite`` loads every
rung directory under a cases root into a name-keyed suite in ladder order.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from claude_local import Budget, ContextFile

from .case import BenchmarkCase


def load_case(case_dir: Path, *, golden_app_root: Path) -> BenchmarkCase:
    """Build a validated case from its directory and the golden app it fills.

    ``golden_app_root`` is the complete reference app every case assembles; its parent
    anchors the ``app/``-prefixed paths shared by the manifest and the golden tree.
    """
    manifest = tomllib.loads((case_dir / "case.toml").read_text(encoding="utf-8"))
    impl_path = str(manifest["impl_path"])
    budget = manifest["budget"]
    return BenchmarkCase.from_fixtures(
        impl_path=impl_path,
        spec_text=(case_dir / "spec.md").read_text(encoding="utf-8"),
        oracle_text=(case_dir / "oracle.py").read_text(encoding="utf-8"),
        golden_tree=_golden_tree(golden_app_root),
        blank_stub=(case_dir / "blank" / impl_path).read_text(encoding="utf-8"),
        context_paths=tuple(manifest["context_paths"]),
        budget=Budget(
            max_attempts=budget["max_attempts"],
            max_tokens=budget["max_tokens"],
            timeout_s=budget["timeout_s"],
        ),
    )


def load_suite(cases_dir: Path, *, golden_app_root: Path) -> dict[str, BenchmarkCase]:
    """Load every rung under ``cases_dir`` into a suite keyed by directory name, in ladder order.

    Each immediate subdirectory of ``cases_dir`` is one rung; ``sorted`` on the directory names
    gives the ladder order (``01_scaffold`` before ``02_schemas``). Non-directory entries are
    skipped, so a stray file beside the rungs never becomes a case.
    """
    return {
        case_dir.name: load_case(case_dir, golden_app_root=golden_app_root)
        for case_dir in sorted(cases_dir.iterdir())
        if case_dir.is_dir()
    }


def _golden_tree(golden_app_root: Path) -> tuple[ContextFile, ...]:
    """Snapshot every golden app module as the complete tree a case assembles."""
    tree_root = golden_app_root.parent
    return tuple(
        ContextFile(
            path=path.relative_to(tree_root).as_posix(),
            content=path.read_text(encoding="utf-8"),
        )
        for path in sorted(golden_app_root.rglob("*.py"))
    )
