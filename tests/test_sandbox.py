"""Confinement property tests for the oracle sandbox.

The sandbox is the kernel boundary that executes untrusted model code (the impl file,
imported by ``uv run pytest``). These tests drive ``sandboxed_spawn`` with real
subprocesses on the live kernel and assert the two security properties hold — a write
*outside* the writable box is denied, and network egress is denied — plus the one
capability it must preserve (a write *inside* the box) and the hang backstop (a
wall-clock timeout that kills a runaway process group).

Skipped where ``sandbox-exec`` is unavailable (non-macOS CI): the property under test is
a macOS-kernel fact, so there is nothing meaningful to assert without the kernel.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

from claude_local.sandbox import SandboxTimeout, sandbox_available, sandboxed_spawn

pytestmark = pytest.mark.skipif(
    not sandbox_available(),
    reason="sandbox-exec unavailable — confinement is a macOS-kernel property",
)


def _run(payload: str, box: Path, *, timeout_s: float = 30.0) -> None:
    """Execute a Python payload under the sandbox, with ``box`` as the writable root."""
    sandboxed_spawn([sys.executable, "-c", payload], cwd=box, write_box=box, timeout_s=timeout_s)


def test_write_outside_the_box_is_denied(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    escape = tmp_path / "escape.txt"  # a sibling of the box — outside the writable root
    payload = (
        "import pathlib\n"
        "try:\n"
        f"    pathlib.Path({str(escape)!r}).write_text('pwned')\n"
        "except OSError:\n"
        "    pass\n"
    )
    _run(payload, box)
    # Oracle: deny-default SBPL grants write only under the box, so the escape never lands.
    assert not escape.exists()


def test_network_egress_is_denied(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    outcome = box / "net.txt"  # inside the box, so the child can always record its result
    # A real loopback listener in the trusted parent: reachable iff the sandbox allows
    # sockets, so the assertion never depends on external network being up or down.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        payload = (
            "import socket, pathlib\n"
            "try:\n"
            f"    conn = socket.create_connection(('127.0.0.1', {port}), timeout=3)\n"
            "    conn.close()\n"
            "    result = 'connected'\n"
            "except OSError as exc:\n"
            "    result = 'blocked:' + type(exc).__name__\n"
            f"pathlib.Path({str(outcome)!r}).write_text(result)\n"
        )
        _run(payload, box)
    # Oracle: (deny network*) blocks the outbound connect even to loopback.
    assert outcome.read_text().startswith("blocked")


def test_write_inside_the_box_is_allowed(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    report = box / "oracle.xml"  # exactly what pytest must be free to write
    payload = f"import pathlib; pathlib.Path({str(report)!r}).write_text('<ok/>')"
    _run(payload, box)
    # Oracle: the single file-write allow rule (subpath box) — the sandbox must not over-restrict.
    assert report.read_text() == "<ok/>"


def test_a_hanging_command_is_killed_at_the_timeout(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    started = time.monotonic()
    with pytest.raises(SandboxTimeout):
        _run("import time; time.sleep(30)", box, timeout_s=2.0)
    # Oracle: the 2s wall-clock cap fires long before the 30s sleep would return.
    assert time.monotonic() - started < 15.0
