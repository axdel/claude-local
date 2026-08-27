"""Kernel-enforced confinement for the oracle subprocess.

The loop executes untrusted model output the only way it can be judged: it writes the
model's text to one impl file and runs ``uv run pytest``, which *imports and executes*
that file. This module is the boundary that contains that execution. It wraps the test
command in macOS ``sandbox-exec`` under a deny-by-default SBPL profile, so the subprocess
(and every child it forks — the sandbox is inherited across ``exec``) may only:

- read files (harmless: network egress and out-of-box writes are both shut, so nothing
  read can be exfiltrated or used to tamper with a trusted artifact),
- write *inside one caller-supplied box* (the disposable dir the JUnit report lands in),
- and nothing else — no network, no write anywhere else on disk.

Layered on top: a hard CPU/file-size ``setrlimit`` cap (Layer 1) and a wall-clock timeout
that SIGKILLs the whole process group (Layer 2), so a hang or runaway cannot outlive its
budget. The module is a stdlib-only leaf — it takes a plain ``timeout_s`` float, never a
domain type — so it stays decoupled and reusable.

Fail-closed: if ``sandbox-exec`` is absent the spawn raises rather than running untrusted
code unconfined. Since the local models are Apple-silicon MLX, the real path is always
macOS; a missing front-end means a broken host, not a fallback to run without a cage.
"""

from __future__ import annotations

import contextlib
import os
import resource
import signal
import subprocess  # nosec B404 (D-SANDBOX-001: argv is an orchestrator-controlled constant)
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

DEFAULT_ORACLE_TIMEOUT_S = 120.0
"""Wall-clock backstop for one oracle run when the caller supplies no tighter budget."""

_MAX_CPU_SECONDS = 60
"""RLIMIT_CPU: a busy-loop burns CPU faster than wall time — kills compute runaways."""

_MAX_FILE_BYTES = 128 * 1024 * 1024
"""RLIMIT_FSIZE: caps any single write, bounding a disk-fill even inside the box."""

_PROFILE_TEMPLATE = """\
(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow mach-lookup)
(allow file-read*)
(allow file-write* (subpath "{box}"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/dtracehelper"))
(allow file-write* (literal "/dev/tty"))
(deny network*)
"""


class SandboxUnavailable(RuntimeError):
    """Raised when ``sandbox-exec`` is absent — untrusted code is never run unconfined."""


class SandboxTimeout(TimeoutError):
    """Raised when the confined command exceeds its wall-clock budget and is killed."""


def sandbox_available() -> bool:
    """Return True when the macOS sandbox front-end is present and usable."""
    return sys.platform == "darwin" and Path(_SANDBOX_EXEC).is_file()


def sandboxed_spawn(
    cmd: Sequence[str],
    cwd: Path,
    write_box: Path,
    *,
    timeout_s: float = DEFAULT_ORACLE_TIMEOUT_S,
) -> None:
    """Run ``cmd`` under kernel confinement, writable only within ``write_box``.

    Args:
        cmd: The command to execute (orchestrator-supplied; no model input on argv).
        cwd: Working directory for the child (the read-only worktree in production).
        write_box: The one directory the child may write to (the disposable report dir).
        timeout_s: Wall-clock budget; on overrun the whole process group is SIGKILLed.

    Raises:
        SandboxUnavailable: ``sandbox-exec`` is not present on this host.
        SandboxTimeout: the command exceeded ``timeout_s`` and was killed.
    """
    if not sandbox_available():
        raise SandboxUnavailable(
            "sandbox-exec is unavailable; refusing to run untrusted code unconfined"
        )
    profile = _build_profile(write_box)
    env = _sandbox_env(write_box)
    # The profile lives in a parent-owned temp file OUTSIDE the box, so the confined child
    # (which cannot write outside the box) can never rewrite the policy judging it.
    with tempfile.NamedTemporaryFile("w", suffix=".sb", prefix="oracle-", delete=False) as handle:
        handle.write(profile)
        profile_path = handle.name
    try:
        full_cmd = [_SANDBOX_EXEC, "-f", profile_path, *cmd]
        proc = subprocess.Popen(  # noqa: S603 # nosec B603 (D-SANDBOX-001)
            full_cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own session/group, so a hang's whole tree is killable
            preexec_fn=_apply_rlimits,
        )
        try:
            proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as expired:
            _kill_session(proc)
            proc.communicate()
            raise SandboxTimeout(
                f"oracle exceeded the {timeout_s:g}s wall-clock budget"
            ) from expired
    finally:
        os.unlink(profile_path)


def _build_profile(write_box: Path) -> str:
    """Render the deny-default SBPL profile with the realpath-resolved writable box."""
    box = _sbpl_quote(os.path.realpath(str(write_box)))
    return _PROFILE_TEMPLATE.format(box=box)


def _sbpl_quote(path: str) -> str:
    """Escape a path for embedding in an SBPL double-quoted string literal."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _sandbox_env(write_box: Path) -> dict[str, str]:
    """Minimal allowlisted environment — drops every parent secret (no API tokens)."""
    box = str(write_box)
    parent = os.environ
    return {
        "PATH": parent.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": parent.get("HOME", box),
        "LANG": parent.get("LANG", "en_US.UTF-8"),
        "TMPDIR": box,
        "UV_CACHE_DIR": str(Path(box) / "uvcache"),
        "UV_NO_SYNC": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _apply_rlimits() -> None:
    """Child-side (post-fork, pre-exec) hard caps: CPU seconds and single-file size."""
    resource.setrlimit(resource.RLIMIT_CPU, (_MAX_CPU_SECONDS, _MAX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_MAX_FILE_BYTES, _MAX_FILE_BYTES))


def _kill_session(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's whole process group (it leads a new session).

    ``ProcessLookupError`` is suppressed for the benign race where the child has already
    exited between the timeout firing and the signal landing — the group is gone either way.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
