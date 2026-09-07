"""Confinement property tests for the oracle sandbox.

The sandbox is the kernel boundary that executes untrusted model code (the impl file,
imported by ``python -m pytest``). These tests drive ``sandboxed_spawn`` with real
subprocesses on the live kernel and assert its security properties hold — ambient host
reads, out-of-box writes, and network egress are denied; HOME is the disposable box —
plus the capabilities it must preserve (runtime/worktree reads, in-box writes, and bounded
diagnostics) and the two teardown backstops: a wall-clock timeout that kills a runaway
process group, and an unconditional group kill so a non-timeout interruption never
orphans the confined process tree.

Skipped where ``sandbox-exec`` is unavailable (non-macOS CI): the property under test is
a macOS-kernel fact, so there is nothing meaningful to assert without the kernel.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_local.sandbox import (
    SandboxTimeout,
    SandboxUnavailable,
    _build_profile,
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


def test_read_outside_the_worktree_and_box_is_denied(tmp_path: Path) -> None:
    """Captured diagnostics cannot relay arbitrary host-file contents to the model request."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    box = tmp_path / "box"
    box.mkdir()
    secret = tmp_path / "operator-secret.txt"
    secret.write_text("credential-sentinel", encoding="utf-8")
    payload = (
        "import pathlib\n"
        "try:\n"
        f"    print(pathlib.Path({str(secret)!r}).read_text())\n"
        "except OSError as exc:\n"
        "    print('blocked:' + type(exc).__name__)\n"
    )

    stdout, _ = sandboxed_spawn(
        [sys.executable, "-c", payload], cwd=worktree, write_box=box, timeout_s=30.0
    )

    assert stdout.startswith(b"blocked:")
    assert b"credential-sentinel" not in stdout


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


def test_mach_service_lookup_beyond_the_platform_baseline_is_denied(tmp_path: Path) -> None:
    """Oracle: the trusted parent knows the host's real ComputerName; the confined child must not.

    ``(deny network*)`` does not cover Mach IPC, so an unscoped ``(allow mach-lookup)`` is a side
    channel out of the cage — the child can reach any XPC service registered on the host. Measured
    on this branch while the blanket grant was still present: a confined ``pbpaste`` read a
    sentinel straight off the developer's clipboard, and ``scutil`` returned the machine's real
    name; with the grant removed, the clipboard came back empty and ``scutil`` fell back to a
    generic default. That is exactly the "cannot exfiltrate secrets" claim the confinement makes.

    ComputerName is read through the SystemConfiguration Mach service and is a read-only query, so
    it gives the same signal as the pasteboard without writing to the developer's real clipboard.
    The parent supplies ground truth by running it unconfined — the same shape as the network test
    standing up its own listener, so the assertion never depends on a hardcoded host value.
    """
    scutil = shutil.which("scutil")
    if scutil is None:
        pytest.skip("scutil is absent, so the host has no SystemConfiguration query to compare")
    real_name = subprocess.run(  # noqa: S603 - fixed argv, no shell, no untrusted input
        [scutil, "--get", "ComputerName"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if not real_name:
        pytest.skip("this host reports no ComputerName, so there is no secret to withhold")

    box = tmp_path / "box"
    box.mkdir()
    confined, _ = sandboxed_spawn([scutil, "--get", "ComputerName"], cwd=box, write_box=box)

    assert confined.decode().strip() != real_name


def test_the_profile_grants_no_unscoped_mach_lookup(tmp_path: Path) -> None:
    """Every grant in the profile names what it applies to; ``mach-lookup`` is not exempt.

    Oracle: SBPL semantics under ``(deny default)`` — a grant with no filter applies to every
    service, so ``(allow mach-lookup)`` on its own line is the whole Mach namespace. This is the
    deterministic backstop for the behavioral test above, which depends on a host having a
    SystemConfiguration service to ask. Reading the rendered profile is reading the security
    artifact itself, not an implementation detail: it is what the kernel is handed.
    """
    box = tmp_path / "box"
    box.mkdir()

    profile = _build_profile(tmp_path, box, [sys.executable])

    # Grants only: a ';' comment line may name mach-lookup while granting nothing.
    grants = [
        stripped
        for line in profile.splitlines()
        if (stripped := line.strip()).startswith("(allow") and "mach-lookup" in stripped
    ]
    assert all("global-name" in grant or "xpc-service-name" in grant for grant in grants), (
        f"unscoped mach-lookup grant in the oracle profile: {grants}"
    )


def test_two_spawns_agree_on_a_string_hash(tmp_path: Path) -> None:
    """The child's hash seed is pinned, so any set or dict it renders orders identically.

    Oracle: CPython randomizes ``hash(str)`` per process from a seed it draws at startup, and
    ``PYTHONHASHSEED=0`` is the documented way to disable that. Two unpinned interpreters agree on
    a string's hash only by a 1-in-2**64 coincidence, so equality here is evidence of the pin and
    of nothing else. The oracle run needs it because pytest renders a failing set comparison in
    iteration order, and that order is fed back to the model as repair feedback (INV-004).
    """
    box = tmp_path / "box"
    box.mkdir()
    payload = "print(hash('claude-local'))"

    first, _ = sandboxed_spawn([sys.executable, "-c", payload], cwd=box, write_box=box)
    second, _ = sandboxed_spawn([sys.executable, "-c", payload], cwd=box, write_box=box)

    assert first == second


def test_write_inside_the_box_is_allowed(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    report = box / "oracle.xml"  # exactly what pytest must be free to write
    payload = f"import pathlib; pathlib.Path({str(report)!r}).write_text('<ok/>')"
    _run(payload, box)
    # Oracle: the single file-write allow rule (subpath box) — the sandbox must not over-restrict.
    assert report.read_text() == "<ok/>"


def test_an_interpreter_reached_through_a_directory_symlink_still_runs(tmp_path: Path) -> None:
    """A runtime named *through* a symlinked directory still launches under confinement.

    Reproduces the layout uv installs: a version-alias directory symlink (``cpython-3.14-...``
    -> ``cpython-3.14.7-...``) beside the real runtime, with the launcher pointing through the
    alias. ``realpath`` collapses every component and so reports only the destination, while the
    kernel resolves the path the launcher literally names — so a profile granting just the
    chain's endpoints leaves the traversed middle ungranted and the spawn is refused with
    ``Operation not permitted``.

    Oracle: the sandbox must permit the very runtime it was handed (the capability half of
    D-SANDBOX-004), and POSIX path resolution visits every component of every hop. Both facts
    are independent of this module — neither was read off the profile builder. Grant only the
    endpoints and this goes red, which is the F2P proof it bites.
    """
    runtime_root = Path(sys.base_prefix)
    real_interpreter = Path(os.path.realpath(sys.executable))
    alias = tmp_path / "runtime-version-alias"
    alias.symlink_to(runtime_root)  # a DIRECTORY symlink, as uv publishes
    launcher = tmp_path / "python3"
    launcher.symlink_to(alias / real_interpreter.relative_to(runtime_root))

    box = tmp_path / "box"
    box.mkdir()
    ran = box / "ran.txt"
    payload = f"import pathlib; pathlib.Path({str(ran)!r}).write_text('ok')"

    _stdout, stderr = sandboxed_spawn(
        [str(launcher), "-c", payload], cwd=box, write_box=box, timeout_s=30.0
    )

    assert ran.exists(), f"the runtime never ran: {stderr.decode(errors='replace')}"
    assert ran.read_text() == "ok"


def test_an_interpreter_reached_through_a_symlinked_parent_directory_still_runs(
    tmp_path: Path,
) -> None:
    """A runtime whose path *descends through* a symlinked directory still launches.

    The sibling test above puts the symlink at the hop's final component. This puts one in the
    middle of the hop's directory portion, with further levels beneath it — the shape ``/tmp ->
    /private/tmp`` has, built explicitly here so the platform's own symlink is not the fixture.

    Oracle: POSIX resolution rewrites the remainder of the path once it crosses a symlink, so
    the kernel matches every component below ``gateway`` under its canonical ``inner`` name. A
    profile that names those components by the pre-resolution name grants nothing the kernel
    will ever match — the rules are present and dead, which is why this fails as a *denied
    spawn* rather than a missing rule. Neither fact was read off the profile builder.

    The launcher points outside the fixture on purpose: were it a real file, the endpoint grant
    derived from ``realpath`` would cover the very directories under test and mask the defect —
    which is exactly how the production layout hides it, since ``realpath(sys.base_prefix)``
    happens to cover the alias's target tree.
    """
    inner = tmp_path / "inner"
    (inner / "nested/bin").mkdir(parents=True)
    launcher = inner / "nested/bin/python3"
    launcher.symlink_to(os.path.realpath(sys.executable))
    gateway = tmp_path / "gateway"
    gateway.symlink_to(inner)  # crossing this rewrites every component after it

    box = tmp_path / "box"
    box.mkdir()
    ran = box / "ran.txt"
    payload = f"import pathlib; pathlib.Path({str(ran)!r}).write_text('ok')"

    _stdout, stderr = sandboxed_spawn(
        [str(gateway / "nested/bin/python3"), "-c", payload],
        cwd=box,
        write_box=box,
        timeout_s=30.0,
    )

    assert ran.exists(), f"the runtime never ran: {stderr.decode(errors='replace')}"
    assert ran.read_text() == "ok"


def test_spawn_returns_captured_stdout_and_stderr(tmp_path: Path) -> None:
    """The parent receives both diagnostic streams from the confined process."""
    box = tmp_path / "box"
    box.mkdir()
    payload = "import sys; print('oracle-out'); print('oracle-err', file=sys.stderr)"

    stdout, stderr = sandboxed_spawn(
        [sys.executable, "-c", payload], cwd=box, write_box=box, timeout_s=30.0
    )

    assert stdout == b"oracle-out\n"
    assert stderr == b"oracle-err\n"


def test_spawn_bounds_each_captured_stream_to_its_diagnostic_tail(tmp_path: Path) -> None:
    """Untrusted process output cannot make the parent's captured value grow without bound."""
    box = tmp_path / "box"
    box.mkdir()
    stream_bytes = 70_000
    payload = (
        "import sys; "
        f"sys.stdout.write('o' * {stream_bytes} + 'stdout-tail'); "
        f"sys.stderr.write('e' * {stream_bytes} + 'stderr-tail')"
    )

    stdout, stderr = sandboxed_spawn(
        [sys.executable, "-c", payload], cwd=box, write_box=box, timeout_s=30.0
    )

    assert len(stdout) == 65_536
    assert len(stderr) == 65_536
    assert stdout.endswith(b"stdout-tail")
    assert stderr.endswith(b"stderr-tail")


def test_a_hanging_command_is_killed_at_the_timeout_with_bounded_diagnostics(
    tmp_path: Path,
) -> None:
    box = tmp_path / "box"
    box.mkdir()
    payload = (
        "import sys, time; "
        "print('started', flush=True); "
        "print('still-running', file=sys.stderr, flush=True); "
        "time.sleep(30)"
    )
    started = time.monotonic()

    with pytest.raises(SandboxTimeout) as excinfo:
        sandboxed_spawn([sys.executable, "-c", payload], cwd=box, write_box=box, timeout_s=2.0)

    assert time.monotonic() - started < 15.0
    assert excinfo.value.stdout == b"started\n"
    assert excinfo.value.stderr == b"still-running\n"


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
