"""Shared pytest fixtures for the claude-local test suite.

A fixture defined here auto-injects into every test in this directory tree, so a fixture
several test modules need lives here once rather than being copied into each. Plain test
helpers stay in ordinary importable modules (``factories``, ``sse_wire``) — conftest is for
fixtures and hooks, not functions tests would ``import``.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def _project_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``VIRTUAL_ENV`` so a sandboxed ``uv run pytest`` resolves from a temp cwd.

    ``TestRunner``'s default spawn passes no ``env=``, so the child inherits this process's
    environment, and the inner ``uv`` resolves its interpreter from ``VIRTUAL_ENV``. Pinning it to
    ``sys.prefix`` lets the child resolve pytest from the external scratch worktree regardless of
    how this suite was launched. ``monkeypatch`` restores the prior value after the test.
    """
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
