"""Kernel-enforced confinement for the oracle subprocess.

The loop executes untrusted model output the only way it can be judged: it writes the
model's text to one impl file and runs ``python -m pytest``, which *imports and executes*
that file. This module is the boundary that contains that execution. It wraps the test
command in macOS ``sandbox-exec`` under a deny-by-default SBPL profile, so the subprocess
(and every child it forks — the sandbox is inherited across ``exec``) may only:

- read the task worktree, disposable write box, and active Python runtime,
- write *inside the write box* where the JUnit report and bounded stream captures land,
- and never use the network or ambient host files as an indirect feedback-egress channel.

Layered on top: a hard CPU/file-size ``setrlimit`` cap (Layer 1), bounded file-backed
stdout/stderr tails, and a wall-clock timeout whose teardown SIGKILLs the whole process group —
on overrun, and on any other interrupted wait — so no hang, noisy child, runaway, or mid-run
failure leaves unbounded parent state or a confined process behind (Layer 2).
The module is a stdlib-only leaf — it takes a plain ``timeout_s`` float, never a domain
type — so it stays decoupled and reusable.

Fail-closed: if ``sandbox-exec`` is absent the spawn raises rather than running untrusted
code unconfined. Since the local models are Apple-silicon MLX, the real path is always
macOS; a missing front-end means a broken host, not a fallback to run without a cage.
"""

from __future__ import annotations

import contextlib
import os
import resource
import shutil
import signal
import subprocess  # nosec B404 (D-SANDBOX-001: argv carries no model input, not shell-interpreted)
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

DEFAULT_ORACLE_TIMEOUT_S = 120.0
"""Wall-clock backstop for one oracle run when the caller supplies no tighter budget."""

_MAX_CPU_SECONDS = 60
"""RLIMIT_CPU: a busy-loop burns CPU faster than wall time — kills compute runaways."""

_MAX_FILE_BYTES = 128 * 1024 * 1024
"""RLIMIT_FSIZE: caps any single write, bounding a disk-fill even inside the box."""

_CAPTURE_TAIL_BYTES = 64 * 1024
"""Maximum diagnostic bytes returned per stream; failures are conventionally at the tail."""

_PROFILE_TEMPLATE = """\
(version 1)
(deny default)
(import "system.sb")
(allow process*)
(allow sysctl-read)
(allow mach-lookup)
{metadata_rules}
{read_rules}
(allow file-write* (subpath "{box}"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/dtracehelper"))
(allow file-write* (literal "/dev/tty"))
(deny network*)
"""


class SandboxUnavailable(RuntimeError):
    """Raised when ``sandbox-exec`` is absent — untrusted code is never run unconfined."""


class SandboxTimeout(TimeoutError):
    """A killed oracle command's timeout fact and bounded diagnostic stream tails."""

    def __init__(
        self,
        message: str,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


def sandbox_available() -> bool:
    """Return True when the macOS sandbox front-end is present and usable."""
    return sys.platform == "darwin" and Path(_SANDBOX_EXEC).is_file()


def sandboxed_spawn(
    cmd: Sequence[str],
    cwd: Path,
    write_box: Path,
    *,
    timeout_s: float = DEFAULT_ORACLE_TIMEOUT_S,
) -> tuple[bytes, bytes]:
    """Run ``cmd`` under confinement and return bounded stdout and stderr tails.

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
    # The profile is passed inline with -p, never via a temp file: there is no policy file on
    # disk for the confined child to rewrite, and nothing to unlink — so no cleanup can race
    # sandbox-exec's lazy, post-fork profile read and orphan (or incidentally kill) the child.
    resolved_cmd = _resolve_command(cmd)
    full_cmd = [
        _SANDBOX_EXEC,
        "-p",
        _build_profile(cwd, write_box, resolved_cmd),
        *resolved_cmd,
    ]
    with (
        tempfile.TemporaryFile(dir=write_box) as stdout_file,
        tempfile.TemporaryFile(dir=write_box) as stderr_file,
    ):
        proc = subprocess.Popen(  # noqa: S603 # nosec B603 (D-SANDBOX-001)
            full_cmd,
            cwd=cwd,
            env=_sandbox_env(write_box),
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,  # own session/group, so a hang's whole tree is killable
            preexec_fn=_apply_rlimits,
        )
        timeout_error: subprocess.TimeoutExpired | None = None
        try:
            proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as expired:
            timeout_error = expired
        finally:
            # Tear the whole group down on ANY exit where the child is still live — a timeout OR an
            # unexpected interruption of communicate() — so no confined process is ever orphaned.
            if proc.poll() is None:
                _kill_session(proc)
                proc.communicate()
        stdout = _read_tail(stdout_file)
        stderr = _read_tail(stderr_file)
        if timeout_error is not None:
            raise SandboxTimeout(
                f"oracle exceeded the {timeout_s:g}s wall-clock budget",
                stdout=stdout,
                stderr=stderr,
            ) from timeout_error
        return stdout, stderr


def _read_tail(stream: BinaryIO) -> bytes:
    """Read at most the diagnostic tail cap from a seekable binary stream."""
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - _CAPTURE_TAIL_BYTES))
    return stream.read()


def _resolve_command(cmd: Sequence[str]) -> tuple[str, ...]:
    """Resolve argv[0] to an absolute path without collapsing a virtualenv symlink."""
    executable = shutil.which(cmd[0], path=_sandbox_path())
    return (executable, *cmd[1:]) if executable is not None else tuple(cmd)


def _build_profile(cwd: Path, write_box: Path, cmd: Sequence[str]) -> str:
    """Render a deny-default profile with explicit runtime and task read roots."""
    read_roots = {
        os.path.realpath(str(cwd)),
        os.path.realpath(str(write_box)),
        os.path.realpath(sys.prefix),
        os.path.realpath(sys.base_prefix),
    }
    executable = shutil.which(cmd[0], path=_sandbox_path())
    if executable is not None:
        read_roots.update((executable, os.path.realpath(executable)))
    metadata_rules = "\n".join(
        f'(allow file-read-metadata file-test-existence (literal "{_sbpl_quote(path)}"))'
        for path in _path_ancestors(read_roots)
    )
    read_rules = "\n".join(
        f'(allow file-read* (subpath "{_sbpl_quote(path)}"))' for path in sorted(read_roots)
    )
    box = _sbpl_quote(os.path.realpath(str(write_box)))
    return _PROFILE_TEMPLATE.format(
        box=box,
        metadata_rules=metadata_rules,
        read_rules=read_rules,
    )


def _sbpl_quote(path: str) -> str:
    """Escape a path for embedding in an SBPL double-quoted string literal."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def _path_ancestors(paths: set[str]) -> tuple[str, ...]:
    """Return unique path components needed to resolve each allowlisted root."""
    ancestors = {str(parent) for path in paths for parent in Path(path).parents}
    return tuple(sorted(ancestors))


def _sandbox_path() -> str:
    """Return the minimal executable search path inherited by the confined process."""
    return os.pathsep.join(
        (str(Path(sys.prefix) / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
    )


def _sandbox_env(write_box: Path) -> dict[str, str]:
    """Minimal allowlisted environment — drops every parent secret, and points HOME at the box.

    HOME is the disposable box, never the developer's real home, so a credential reader keyed on
    ``$HOME`` (``~/.ssh``, ``~/.aws``, ``~/.netrc``, ``~/.config/gh``) resolves into an empty
    directory rather than the operator's secrets. No parent API tokens are forwarded at all.
    """
    box = str(write_box)
    parent = os.environ
    return {
        "PATH": _sandbox_path(),
        "HOME": box,
        "LANG": parent.get("LANG", "en_US.UTF-8"),
        "TMPDIR": box,
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _apply_rlimits() -> None:
    """Child-side (post-fork, pre-exec) hard caps: CPU seconds and single-file size."""
    resource.setrlimit(resource.RLIMIT_CPU, (_MAX_CPU_SECONDS, _MAX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_MAX_FILE_BYTES, _MAX_FILE_BYTES))


def _kill_session(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's whole process group (it leads a new session).

    ``ProcessLookupError`` is suppressed for the benign race where the child has already
    exited between the wait ending and the signal landing — the group is gone either way.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
