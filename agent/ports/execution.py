"""ExecutionPort — run commands / read-write files in an isolated environment.

FROZEN CONTRACT — see CONTRIBUTING.md before changing a signature.

The domain does NOT care whether this is a child process in THIS container
(image mode, no Docker-in-Docker) or a per-challenge container (dev).
Implementations: adapters/execution/{subprocess_exec,docker_sandbox}.py
"""
from __future__ import annotations

from typing import Protocol

from agent.domain.state import ExecResult


class ExecutionPort(Protocol):
    async def run(
        self, cmd: str, *, timeout_s: int = 60, workdir: str | None = None
    ) -> ExecResult:
        """Run a shell command and capture output.

        MUST NOT raise on a command failure or a timeout — a non-zero exit is an
        ``ExecResult`` with that ``exit_code``; a timeout is
        ``ExecResult(timed_out=True)`` after the process tree is killed. Output is
        truncated to a sane cap (``truncated=True`` when it was). Reserve raising
        for a broken environment (e.g. the sandbox is gone).
        """
        ...

    async def read_file(self, path: str) -> bytes:
        """Read a file from the environment. Raises FileNotFoundError if absent."""
        ...

    async def write_file(self, path: str, data: bytes) -> None:
        """Write a file, OVERWRITING it and CREATING parent directories."""
        ...

    async def new_workspace(self, challenge_id: str) -> str:
        """Return a fresh, absolute, per-challenge working directory path."""
        ...
