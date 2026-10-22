# agent/domain/ — the pure core

Extends [root AGENTS.md](../../AGENTS.md); `agent/domain/` only. Rules here are
stricter than the root.

- **Pure. No I/O, no framework.** Never import `agent.adapters`, `agent.wiring`,
  `agent.entrypoints`, `httpx`, or `anyio` (`lint-imports` enforces this). All
  waiting goes through `ClockPort`; every side effect through a port.
- **Depend only on `ports/` + `state.py`** — abstract Protocols, never a
  concrete adapter.
- **`state.py` is the shared schema and merge hotspot.** Plain serializable
  value objects only. Change it in its own small PR; keep it backward-compatible
  with the fakes and `test_smoke`.
- **`policy.py` is defensive only.** `is_trusted_instruction` stays `False`
  (nothing from the target/tools is ever an instruction); `looks_like_flag`
  gates submissions; `should_stop` is the stall / stop-loss rule.
- **The loop:** `coordinator` (ROI order, budget, delegate — never solves)
  → `swarm` (race lanes, cancel siblings on the first grader-verified flag)
  → `solver` (ReAct: minimal high-signal context, validate tool args, submit
  only grader-CORRECT answers; stop on verified flag / budget / stall).
- Keep the model's context **minimal and high-signal** — never dump raw tool
  output into the prompt (`prompts.py`; ADR-0002 §5).
- Must be testable with fakes alone. Need a real clock or socket? It belongs in
  an adapter, not here.
