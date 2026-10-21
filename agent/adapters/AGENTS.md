# agent/adapters/ — the I/O edge

Inherit [/AGENTS.md](../../AGENTS.md). Adapters implement a port and do the real
I/O (HTTP, subprocess, disk, clock). They are swappable; the domain never knows
which one is wired.

- **Implement the port structurally** (no inheritance). Match every signature
  exactly; honor the port docstring's semantics to the letter.
- **May import** `agent.ports`, `agent.domain.state`, and third-party libs
  (`httpx`, …). **Must not import** `agent.wiring` or `agent.entrypoints`
  (enforced by `import-linter`). One adapter should not import another family.
- **Network adapters:** take `clock` in the constructor; map transient failures
  (HTTP 429/5xx, transport errors) to `TransientError(retry_after=...)` and wrap
  the call in `with_retry(clock=...)` (`_resilience.py`); raise `RuntimeError`
  on hard/auth (4xx) errors. Accept an injectable `client` so tests can pass
  `httpx.MockTransport`. No network at import or construction time.
- **`result/ctfy_result.py`:** sync, atomic (temp + `os.replace`), creates
  parent dirs, scrubs secrets, and **never raises** — it is called from a
  `finally` on every exit path.
- **`execution/subprocess_exec.py`:** cross-platform (Windows dev, Linux prod);
  on timeout kill the whole process tree; cap + truncate output; gate
  concurrency with a per-instance semaphore.
- `clock.py` and `_resilience.py` are shared primitives — reuse them, don't fork
  them. Dev-only stubs (`local_key`, `docker_sandbox`, `ctfy_mcp`, `ctfd`) raise
  `NotImplementedError` until someone implements them against the port.
- **Every adapter ships a `tests/contract/` test** (fakes / `MockTransport`).
