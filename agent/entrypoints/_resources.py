"""Close adapter resources before an entrypoint releases its event loop."""
from __future__ import annotations

import anyio

_CLOSE_TIMEOUT_S = 5.0


def split_timeout(total_s: float | None) -> tuple[float | None, float]:
    """Reserve a bounded part of an optional total limit for resource cleanup."""
    if total_s is None:
        return None, _CLOSE_TIMEOUT_S
    total_s = max(0.0, total_s)
    cleanup_s = min(_CLOSE_TIMEOUT_S, total_s * 0.1)
    return total_s - cleanup_s, cleanup_s



async def close_ports(*ports: object, timeout_s: float = _CLOSE_TIMEOUT_S) -> None:
    """Close independent adapters concurrently, including cancellation unwinding.

    Lifecycle hooks remain optional capabilities, not frozen-port additions.
    A broken or stuck close cannot prevent another close or delay result delivery.
    """
    async def close_one(port: object) -> None:
        try:
            close = getattr(port, "aclose", None)
            if close is not None:
                with anyio.move_on_after(max(0.0, timeout_s)):
                    await close()
        except BaseException:  # noqa: B036 - cleanup must not replace the saved result
            pass

    with anyio.CancelScope(shield=True):
        async with anyio.create_task_group() as group:
            for port in ports:
                group.start_soon(close_one, port)
