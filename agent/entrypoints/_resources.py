"""Close adapter resources before an entrypoint releases its event loop."""
from __future__ import annotations

import anyio


async def close_ports(*ports: object) -> None:
    """Best-effort cleanup, including cancellation unwinding.

    Lifecycle hooks are adapter capabilities, not additions to the frozen ports.
    A failed close must not prevent another adapter closing or result delivery.
    """
    with anyio.CancelScope(shield=True):
        for port in ports:
            close = getattr(port, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
