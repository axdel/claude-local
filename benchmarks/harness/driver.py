"""Per-case benchmark driver with an owned disposable worktree lifecycle.

The driver composes a golden tree, replaces exactly the implementation hole with its blank stub,
and invokes the public ``claude_local.implement`` front door. The case worktree is removed on
success or failure after ``implement`` has copied its result into the returned ``Outcome``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from claude_local import Outcome, implement

from .case import BenchmarkCase

if TYPE_CHECKING:
    import httpx


class BenchmarkDriver:
    """Assemble and run one benchmark case while owning all scratch worktree children."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        generation_params: Mapping[str, object] | None = None,
        scratch_root: Path | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._generation_params = dict(generation_params or {})
        self._scratch_root = scratch_root

    def run_case(self, case: BenchmarkCase, *, http_client: httpx.Client) -> Outcome:
        """Run ``case`` through ``implement`` and remove its assembled worktree on exit."""
        if self._scratch_root is not None:
            self._scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="claude-local-benchmark-",
            dir=self._scratch_root,
        ) as temporary_directory:
            worktree = Path(temporary_directory)
            for golden_file in case.golden_tree:
                _write_case_file(worktree, golden_file.path, golden_file.content)
            _write_case_file(worktree, case.task.impl_path, case.blank_stub)
            return implement(
                case.task,
                base_url=self._base_url,
                model=self._model,
                generation_params=self._generation_params,
                worktree=worktree,
                http_client=http_client,
            )


def _write_case_file(worktree: Path, relative_path: str, content: str) -> None:
    """Write one pre-validated committed case fixture below its assembled worktree path."""
    destination = worktree / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
