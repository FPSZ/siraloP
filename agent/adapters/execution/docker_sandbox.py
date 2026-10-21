"""DockerSandboxAdapter (dev only) — one container per challenge via aiodocker.

Implements ports.execution.ExecutionPort. TODO(team): implement every method.
"""
from __future__ import annotations


class DockerSandboxAdapter:
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("implement DockerSandboxAdapter")
