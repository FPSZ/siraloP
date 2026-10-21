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
import signal
import subprocess
import tempfile
from pathlib import Path

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
    ) -> None:
        self._root = os.path.abspath(root) if root else tempfile.mkdtemp(prefix="chal-")
        os.makedirs(self._root, exist_ok=True)
        # Per-instance (NOT module-global) gate on concurrently running commands.
        # Created here; asyncio.Semaphore binds lazily to the running loop on
        # first ``await`` (Python 3.10+), so construction outside a loop is fine.
        self._sem = asyncio.Semaphore(max_concurrency)
        self._output_cap = output_cap

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
        """Return a fresh, absolute, per-challenge working directory path."""
        loop = asyncio.get_running_loop()

        def _mk() -> str:
            d = os.path.abspath(os.path.join(self._root, challenge_id))
            os.makedirs(d, exist_ok=True)
            return d

        return await loop.run_in_executor(None, _mk)

    # -- internals --------------------------------------------------------

    async def _run_inner(
        self, cmd: str, timeout_s: int, workdir: str | None
    ) -> ExecResult:
        loop = asyncio.get_running_loop()
        cwd = os.path.abspath(workdir) if workdir else self._root
        # Defensive: a missing cwd would make spawning raise; create it so run()
        # keeps its "don't raise on ordinary failures" contract.
        await loop.run_in_executor(None, lambda: os.makedirs(cwd, exist_ok=True))

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            **self._spawn_kwargs(),
        )

        timed_out = False
        try:
            # communicate() buffers the full output; the cap is applied after.
            # Bounded by timeout_s, so output stays practically finite.
            raw_out, raw_err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except TimeoutError:
            timed_out = True
            await self._kill_tree(proc)
            raw_out, raw_err = await self._reap(proc)

        stdout = raw_out.decode("utf-8", errors="replace") if raw_out else ""
        stderr = raw_err.decode("utf-8", errors="replace") if raw_err else ""
        stdout, stderr, truncated = self._cap_combined(stdout, stderr, self._output_cap)

        exit_code = proc.returncode if proc.returncode is not None else -1
        return ExecResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
            truncated=truncated,
        )

    @staticmethod
    def _spawn_kwargs() -> dict[str, object]:
        """Spawn options that make the child the head of its own killable group."""
        if IS_WINDOWS:
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Hard-kill the command AND every process it spawned, cross-platform."""
        pid = proc.pid
        if pid is None:
            return
        if IS_WINDOWS:
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
