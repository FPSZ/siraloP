"""SubprocessAdapter — ExecutionPort inside THIS container (image mode, no DinD).

Run tools as child processes; isolate challenges by working directory. Caps
concurrency with a per-instance semaphore to respect the 8GiB/2CPU/512pid budget
(ADR-0001 §3.6), and never lets a runaway command hang the loop: on timeout the
whole process tree is killed and an ``ExecResult(timed_out=True)`` is returned.

Cross-platform (dev = Windows, prod = Linux container):
  * POSIX — spawn in a new session (``start_new_session=True``) so the shell and
    its descendants share one process group, then ``os.killpg(.., SIGKILL)``.
  * Windows — spawn with ``CREATE_NEW_PROCESS_GROUP`` and tear the tree down with
    ``taskkill /F /T /PID`` (falling back to ``proc.kill()``).

Implements ports.execution.ExecutionPort. All methods are async; blocking file
I/O is offloaded to a thread so the event loop stays responsive.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent.domain.state import ExecResult

IS_WINDOWS = os.name == "nt"

# How long to wait, after a kill, for the dying tree's buffered output to drain
# and the process to be reaped. The tree is already SIGKILL'd, so this is a
# safety net against a stuck pipe, not a normal wait.
_REAP_TIMEOUT_S = 5.0


class SubprocessAdapter:
    """Execute commands and manage files as child processes of this container."""

    def __init__(
        self,
        root: str | None = None,
        *,
        max_concurrency: int = 4,
        output_cap: int = 64000,
        posix_shell: str | None = None,
    ) -> None:
        self._root = os.path.abspath(root) if root else tempfile.mkdtemp(prefix="chal-")
        os.makedirs(self._root, exist_ok=True)
        # Prod is a Linux container whose /bin/sh honors POSIX syntax (`;` and `&&`
        # chaining, ls/cat/unzip); the agent is prompted for that world. On Windows
        # DEV, create_subprocess_shell would use cmd.exe, which does NOT treat `;`
        # as a separator — the agent's chained commands then fail. Mirror prod by
        # routing commands through a POSIX shell (bash/sh) when one is on PATH.
        # `posix_shell` overrides detection ("" forces cmd.exe; a path forces that
        # shell) for tests / explicit config. On real POSIX we leave it None and
        # create_subprocess_shell already uses /bin/sh.
        if posix_shell is not None:
            self._posix_shell = posix_shell or None
        elif IS_WINDOWS:
            self._posix_shell = shutil.which("bash") or shutil.which("sh")
        else:
            self._posix_shell = None
        # Per-instance (NOT module-global) gate on concurrently running commands.
        # Created here; asyncio.Semaphore binds lazily to the running loop on
        # first ``await`` (Python 3.10+), so construction outside a loop is fine.
        self._sem = asyncio.Semaphore(max_concurrency)
        self._output_cap = output_cap
        # Persistent shell sessions, one per working directory. The agent drives a
        # STATEFUL shell: `cd`, env vars and shell functions persist across execute
        # calls — matching how a terminal actually behaves. A stateless fresh shell
        # per command makes multi-step work (extract an archive, THEN read a file
        # inside it) fall apart, which is exactly how the agent used to get lost.
        # Each racing lane gets its own workspace+session (see new_workspace), so
        # lanes never share shell state.
        self._sessions: dict[str, _ShellSession] = {}
        self._ws_counter = 0
        self._closed = False
        # Session shell: the Windows POSIX shell when detected, else bash, else sh.
        # Invoked non-interactively, reading commands from stdin.
        shell = self._posix_shell or shutil.which("bash") or "/bin/sh"
        argv = [shell]
        if os.path.basename(shell).lower().startswith("bash"):
            argv += ["--norc", "--noprofile"]
        self._session_argv = argv

    # -- ExecutionPort ----------------------------------------------------

    async def run(
        self, cmd: str, *, timeout_s: int = 60, workdir: str | None = None
    ) -> ExecResult:
        """Run a shell command, capturing stdout and stderr.

        Never raises on a command failure: a non-zero exit becomes an
        ``ExecResult`` carrying that ``exit_code``; a timeout kills the whole
        process tree and returns ``ExecResult(timed_out=True)``. Combined output
        is truncated to ``output_cap`` (``truncated=True`` when it was cut).
        """
        async with self._sem:
            return await self._run_inner(cmd, timeout_s, workdir)

    async def read_file(self, path: str) -> bytes:
        """Read a file's bytes. Raises FileNotFoundError if it is absent."""
        loop = asyncio.get_running_loop()

        def _read() -> bytes:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(path)
            return p.read_bytes()

        return await loop.run_in_executor(None, _read)

    async def write_file(self, path: str, data: bytes) -> None:
        """Write bytes, creating parent directories and overwriting any file."""
        loop = asyncio.get_running_loop()

        def _write() -> None:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

        await loop.run_in_executor(None, _write)

    async def new_workspace(self, challenge_id: str) -> str:
        """A FRESH, unique, absolute working directory for one solve attempt.

        Unique per call so racing lanes never share persistent-shell state. Seeded
        with the top-level files staged directly under the adapter root (e.g. the
        challenge's attachments), so every attempt starts from the same materials.
        """
        n = self._ws_counter
        self._ws_counter += 1
        safe = os.path.basename(str(challenge_id)) or "chal"
        loop = asyncio.get_running_loop()

        def _mk() -> str:
            d = os.path.abspath(os.path.join(self._root, f"{safe}-ws{n}"))
            os.makedirs(d, exist_ok=True)
            try:  # seed attachments (top-level files staged under the root)
                for name in os.listdir(self._root):
                    src = os.path.join(self._root, name)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(d, name))
            except OSError:
                pass
            return d

        return await loop.run_in_executor(None, _mk)

    # -- internals --------------------------------------------------------

    async def _run_inner(
        self, cmd: str, timeout_s: int, workdir: str | None
    ) -> ExecResult:
        if self._closed:
            return ExecResult(stdout="", stderr="", exit_code=-1, timed_out=False)
        loop = asyncio.get_running_loop()
        cwd = os.path.abspath(workdir) if workdir else self._root
        # Defensive: a missing cwd would make spawning raise; create it so run()
        # keeps its "don't raise on ordinary failures" contract.
        await loop.run_in_executor(None, lambda: os.makedirs(cwd, exist_ok=True))
        session = self._sessions.get(cwd)
        if session is None:
            session = _ShellSession(
                self._session_argv, cwd, self._spawn_kwargs(), kill=self._kill_tree
            )
            self._sessions[cwd] = session
        return await session.run(cmd, timeout_s, output_cap=self._output_cap)

    async def aclose(self) -> None:
        """Terminate every persistent shell session (idempotent). A long-lived
        adapter (dev harness) should call this; a one-shot process exit also tears
        the child shells down via the kill flags."""
        self._closed = True
        sessions, self._sessions = self._sessions, {}
        for s in sessions.values():
            try:
                await s.aclose()
            except Exception:
                pass

    @staticmethod
    def _spawn_kwargs() -> dict[str, Any]:
        """Spawn options that make the child the head of its own killable group."""
        if sys.platform == "win32":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Hard-kill the command AND every process it spawned, cross-platform."""
        pid = proc.pid
        if pid is None:
            return
        if sys.platform == "win32":
            if not await self._taskkill(pid):
                self._safe_kill(proc)  # fallback: at least kill the root
            return
        try:
            # start_new_session made the shell a group leader (pgid == pid); one
            # killpg reaches the shell and all its descendants.
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
        except Exception:
            self._safe_kill(proc)

    @staticmethod
    async def _taskkill(pid: int) -> bool:
        """Kill a Windows process tree via taskkill; True on success."""
        loop = asyncio.get_running_loop()

        def _call() -> bool:
            try:
                r = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    check=False,
                )
                return r.returncode == 0
            except Exception:
                return False

        try:
            return await loop.run_in_executor(None, _call)
        except Exception:
            return False

    @staticmethod
    def _safe_kill(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
        except Exception:
            pass

    async def _reap(
        self, proc: asyncio.subprocess.Process
    ) -> tuple[bytes, bytes]:
        """After a kill, collect any buffered output and reap the process."""
        try:
            return await asyncio.wait_for(proc.communicate(), timeout=_REAP_TIMEOUT_S)
        except TimeoutError:
            self._safe_kill(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_S)
            except TimeoutError:
                pass
            return b"", b""
        except Exception:
            return b"", b""

    @staticmethod
    def _cap_combined(stdout: str, stderr: str, cap: int) -> tuple[str, str, bool]:
        """Truncate so ``len(stdout) + len(stderr) <= cap``, keeping both streams.

        A small stream is kept whole and the other gets the remaining budget; if
        both exceed half the cap they split it evenly. Heads are kept (the start
        of tool output is usually the high-signal part). Returns the possibly
        trimmed pair plus whether anything was cut.
        """
        total = len(stdout) + len(stderr)
        if total <= cap:
            return stdout, stderr, False
        half = cap // 2
        if len(stdout) <= half:
            return stdout, stderr[: cap - len(stdout)], True
        if len(stderr) <= half:
            return stdout[: cap - len(stderr)], stderr, True
        return stdout[:half], stderr[: cap - half], True


class _ShellSession:
    """A persistent, single shell process for one working directory.

    Commands run strictly in order (serialized by a lock); the shell keeps its
    state — current directory, environment, shell functions — BETWEEN calls, so
    the agent can `cd` into an extracted archive in one step and read a file in
    it the next. stdout+stderr are merged (one ordered stream the agent reads).

    Protocol: each command is written to the shell's stdin, followed by a line
    that prints a unique random marker plus ``$?``. We read the merged stream up
    to that marker — everything before it is the command's output; the digits
    after it are the exit code. A command that hangs past ``timeout_s`` has its
    whole process tree killed and the session dropped (state is lost, which is
    acceptable for the exceptional timeout path); the next call starts fresh.
    """

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        spawn_kwargs: dict[str, Any],
        *,
        kill: Any,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._spawn_kwargs = spawn_kwargs
        self._kill = kill  # adapter._kill_tree(proc) — kills the tree, cross-platform
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._marker = "__CTFY_" + os.urandom(8).hex() + "__"

    async def _ensure(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._cwd,
            **self._spawn_kwargs,
        )

    async def run(self, cmd: str, timeout_s: int, *, output_cap: int) -> ExecResult:
        async with self._lock:
            try:
                await self._ensure()
            except Exception:
                return ExecResult(stdout="", stderr="", exit_code=-1, timed_out=False)
            proc = self._proc
            assert proc is not None and proc.stdin is not None
            payload = (
                cmd + "\n" + f"printf '%s%d\\n' '{self._marker}' \"$?\"\n"
            ).encode("utf-8", "replace")
            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
            except Exception:
                await self._discard(proc)
                return ExecResult(stdout="", stderr="", exit_code=-1, timed_out=False)
            try:
                out, code, died = await asyncio.wait_for(
                    self._read_until(proc), timeout_s
                )
            except TimeoutError:
                await self._discard(proc)  # command hung — drop the session
                return ExecResult(stdout="", stderr="", exit_code=-1, timed_out=True)
            if died:
                await self._discard(proc)
            truncated = len(out) > output_cap
            if truncated:
                out = out[:output_cap]
            return ExecResult(
                stdout=out, stderr="", exit_code=code,
                timed_out=False, truncated=truncated,
            )

    async def _read_until(self, proc: asyncio.subprocess.Process) -> tuple[str, int, bool]:
        """Read the merged stream until the marker; return (output, exit_code, died)."""
        assert proc.stdout is not None
        mbytes = self._marker.encode()
        buf = bytearray()
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:  # EOF: the shell exited (e.g. the command ran `exit`)
                return buf.decode("utf-8", "replace"), -1, True
            buf += chunk
            idx = buf.find(mbytes)
            if idx == -1:
                continue
            nl = buf.find(b"\n", idx)
            if nl == -1:
                continue  # marker seen but need the full exit-code line
            try:
                code = int(buf[idx + len(mbytes):nl])
            except ValueError:
                code = -1
            return bytes(buf[:idx]).decode("utf-8", "replace"), code, False

    async def _discard(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = None
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await self._kill(proc)
        except Exception:
            pass

    async def aclose(self) -> None:
        async with self._lock:
            proc, self._proc = self._proc, None
            if proc is not None and proc.returncode is None:
                await self._discard(proc)
