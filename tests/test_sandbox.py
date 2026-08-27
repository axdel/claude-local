"""Confinement property tests for the oracle sandbox.

The sandbox is the kernel boundary that executes untrusted model code (the impl file,
imported by ``uv run pytest``). These tests drive ``sandboxed_spawn`` with real
subprocesses on the live kernel and assert its security properties hold — a write
*outside* the writable box is denied, network egress is denied, and the child's HOME is
the disposable box (never the developer's real home) — plus the one capability it must
preserve (a write *inside* the box) and the two teardown backstops: a wall-clock timeout
that kills a runaway process group, and an unconditional group kill so a non-timeout
interruption never orphans the confined process tree.

Skipped where ``sandbox-exec`` is unavailable (non-macOS CI): the property under test is
a macOS-kernel fact, so there is nothing meaningful to assert without the kernel.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_local.sandbox import (
    SandboxTimeout,
    SandboxUnavailable,
    sandbox_available,
    sandboxed_spawn,
)

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


def test_home_points_into_the_box_not_the_developer_home(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    outcome = box / "home.txt"  # inside the box, so the child can always record what it saw
    payload = (
        "import os, pathlib; "
        f"pathlib.Path({str(outcome)!r}).write_text(os.environ.get('HOME', ''))"
    )
    _run(payload, box)
    # Oracle: the confined child's HOME is the disposable box, never the developer's real home,
    # so a credential reader keyed on $HOME (~/.ssh, ~/.aws, ~/.netrc, ~/.config/gh) resolves into
    # an empty box. The box path is the caller's input, derivable without running the sandbox.
    assert outcome.read_text() == str(box)


def test_a_non_timeout_interruption_still_kills_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = tmp_path / "box"
    box.mkdir()
    started = box / "started.txt"  # the child's "I am running" signal, written inside the box
    marker = box / "leaked.txt"  # a delayed write; lands only if the group outlives the failure
    # The child announces startup, then writes its real marker only after a delay. The delay is the
    # window a leaked (un-torn-down) process group would survive to complete.
    payload = (
        "import time, pathlib; "
        f"pathlib.Path({str(started)!r}).write_text('go'); "
        "time.sleep(2); "
        f"pathlib.Path({str(marker)!r}).write_text('leaked')"
    )
    real_communicate = subprocess.Popen.communicate
    injected = {"pending": True}

    def interrupt_once(
        self: subprocess.Popen[bytes], *_args: object, **_kwargs: object
    ) -> tuple[bytes, bytes]:
        # Fail the FIRST communicate (the timed wait) with a NON-timeout error — but only once the
        # confined child has actually started, so the fault lands on a live process group rather
        # than one still bootstrapping. The teardown's own reap call (the second communicate, after
        # the group kill) has pending cleared and delegates to the real, argless communicate.
        if injected["pending"]:
            for _ in range(250):  # ~5s ceiling; child signals startup within a few hundred ms
                if started.exists():
                    break
                time.sleep(0.02)
            injected["pending"] = False
            raise RuntimeError("injected non-timeout interruption")
        return real_communicate(self)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupt_once)
    with pytest.raises(RuntimeError, match="injected non-timeout"):
        _run(payload, box, timeout_s=30.0)
    time.sleep(3)  # past the child's ~2s delayed write, with margin, before asserting
    # Oracle: teardown must SIGKILL the whole group on ANY communicate failure, not only a timeout,
    # so the orphaned child never reaches its delayed write. Kill-on-timeout-only lets the marker
    # land; correct teardown prevents it. The expected absence is derived from the confinement
    # requirement (no confined process outlives the harness), not from running the code.
    assert not marker.exists()


def test_spawn_refuses_when_the_kernel_front_end_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``sandbox-exec`` absent, the spawn REFUSES rather than run untrusted code unconfined.

    This asserts the security precondition itself, so it forces ``sandbox_available`` False even
    on a host that has the sandbox — the module skip only fires where it is genuinely absent, and
    there this refusal is the real runtime behavior. No child is spawned: the guard raises first.
    Oracle: the refuse-unconfined contract (D-SANDBOX-001) defines the raise; drop the guard and
    the spawn runs confinement-less, so this test goes red — the F2P proof it bites.
    """
    monkeypatch.setattr("claude_local.sandbox.sandbox_available", lambda: False)
    with pytest.raises(SandboxUnavailable):
        sandboxed_spawn([sys.executable, "-c", "pass"], cwd=tmp_path, write_box=tmp_path)
