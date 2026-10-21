# agent/ports/ — FROZEN contracts

Inherit [/AGENTS.md](../../AGENTS.md). These five Protocols (`platform`, `llm`,
`execution`, `result`, `clock`) are the spine of the system.

- **Changing a method signature is a breaking change.** Every adapter and every
  domain caller is written against the exact semantics in the docstrings. Change
  one only with an **ADR + a heads-up to the team** — never as a drive-by.
- The **docstrings are the specification.** When you implement or call a port,
  the documented behavior (what it returns, what it must not raise, how it
  paces) is binding. Examples that are easy to get wrong:
  - `ExecutionPort.run` never raises on command failure/timeout — it returns an
    `ExecResult` (timeout ⇒ `timed_out=True` after the process tree is killed).
  - `PlatformPort.submit` returns a `Verdict` for ordinary outcomes
    (CORRECT/WRONG/CAPPED/UNGRADED/PAUSED); it raises only on transport/auth.
  - `ResultSink.write` is **sync and never raises**, and writes atomically.
  - `ClockPort` is the only way the domain waits.
- Ports import **only** `agent.domain.state` + `typing`. No adapters, no
  `httpx`. (`import-linter` enforces this.)
- Prefer adding a **new** port or a new method over mutating an existing one.
