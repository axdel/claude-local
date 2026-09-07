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
; No mach-lookup grant: (deny network*) does not cover Mach IPC, so an unscoped one is a side
; channel out of the cage. Measured — a confined pbpaste read the developer's clipboard through
; it. The oracle needs none beyond what system.sb already scopes; see D-SANDBOX-008.
{metadata_rules}
{read_rules}
(allow file-write* (subpath "{box}"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/dtracehelper"))
; /dev/tty is withheld by design (a terminal egress channel); see D-SANDBOX-005
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
        read_roots.update(_symlink_chain(executable))
        read_roots.add(os.path.realpath(executable))
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


def _canonicalize_parents(path: str) -> str:
    """Resolve every directory above ``path`` while leaving its final component intact.

    ``os.path.realpath`` cannot be used on the whole path here: it would collapse the very
    symlink this module exists to name, reducing the chain back to its endpoints. Only the
    parents are resolved, so the link is still named individually — under the name the kernel
    will actually match it by.
    """
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def _symlink_chain(path: str) -> tuple[str, ...]:
    """Return every path the kernel visits while resolving ``path``, hop by hop.

    ``os.path.realpath`` reports only the destination, because it collapses every component
    at once. Path resolution instead walks each name a symlink points at, and an intermediate
    hop can sit outside the resolved runtime root entirely: uv installs a *version-alias
    directory* symlink (``cpython-3.14-...`` -> ``cpython-3.14.7-...``) and points the venv
    launcher through it, so the traversed middle is a sibling of ``sys.base_prefix`` rather
    than a child. Granting the chain's endpoints alone leaves that middle ungranted and the
    deny-default profile refuses the spawn.

    Each hop is reported **twice** when the two spellings differ — as written, and with its
    parents resolved — because crossing a symlink rewrites the remainder of the path and the
    kernel needs both halves. It reads the link under the name that points at it (``/tmp``,
    the version alias), then matches every component below under the resolved name
    (``/private/tmp``). Naming only the first leaves the components beneath ungranted; naming
    only the resolved form leaves the link itself untraversable, so the walk stops at the very
    hop it exists to reach. Emitting one spelling and not the other is a denied spawn either
    way, and the failure hides in the common case — a missing grant is silently covered
    whenever another root's subpath happens to span the same tree.

    Yielding the traversed paths keeps the profile *narrower* than granting the alias
    directory's subtree would: each hop is one file, so its grant covers only that file.
    The walk terminates on a symlink cycle by construction, having visited each path once.
    """
    chain: list[str] = []
    visited: set[str] = set()
    current = os.path.abspath(path)
    while current not in visited:
        visited.add(current)
        chain.append(current)
        resolved_parents = _canonicalize_parents(current)
        if resolved_parents != current:
            chain.append(resolved_parents)
        if not os.path.islink(current):
            break
        target = os.readlink(current)
        current = (
            target
            if os.path.isabs(target)
            else os.path.normpath(os.path.join(os.path.dirname(current), target))
        )
    return tuple(chain)


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

    ``PYTHONHASHSEED`` is pinned because the child's diagnostics are read back as model feedback:
    an unpinned interpreter draws a fresh hash seed per process, so a failing set or dict renders
    its elements in a different order every run and one unchanged failure asks a different question
    each time it is fed back (D-ORACLE-005).
    """
    box = str(write_box)
    parent = os.environ
    return {
        "PATH": _sandbox_path(),
        "HOME": box,
        "LANG": parent.get("LANG", "en_US.UTF-8"),
        "TMPDIR": box,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
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
