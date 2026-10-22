# agent/ports/ — FROZEN contracts

Extends [root AGENTS.md](../../AGENTS.md); `agent/ports/` only. These five
Protocols (`platform`, `llm`, `execution`, `result`, `clock`) are the spine
every adapter and domain caller is written against.

- **A signature change is a breaking change.** Change a port only with an
  **ADR + a heads-up to the team** — never as a drive-by. Prefer adding a new
  port or method over mutating an existing one.
- **Docstrings are the spec.** What a method returns, what it must not raise,
  how it paces — all binding. The ones easy to get wrong:
  - `ExecutionPort.run` never raises on failure/timeout — returns an
    `ExecResult` (non-zero `exit_code`; timeout ⇒ `timed_out=True` after the
    process tree is killed).
  - `PlatformPort.submit` returns a `Verdict` for every ordinary outcome
    (CORRECT / WRONG / CAPPED / UNGRADED / PAUSED); it raises only on
    transport/auth failures the resilience layer retries.
  - `ResultSink.write` is **sync and never raises**, and writes atomically.
  - `ClockPort` is the only way the domain waits (`now` / `sleep` /
    `sleep_until`).
- **Import only** `agent.domain.state` + `typing`. No adapters, no `httpx`
  (`lint-imports` enforces this).
