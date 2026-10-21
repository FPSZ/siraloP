# tests/ — how we test

Inherit [/AGENTS.md](../../AGENTS.md).

- **Fakes-first, no real I/O.** Drive everything through
  `agent/testing/fakes.py` (`FakeClock`, `FakePlatform`, `FakeLLM`,
  `FakeExecution`, `RecordingResultSink`) and `httpx.MockTransport`. **No test
  may hit the real network or require real credentials.**
- `asyncio_mode = "auto"` (pyproject) — write `async def test_...` with **no**
  decorator. Fixtures live in `conftest.py`; shared builders in `support.py`.
- **Layout:**
  - `tests/contract/` — one module per port, asserting the **frozen contract**
    an adapter must honor (atomic/never-raise sink, timeout-not-raise executor,
    verdict-status mapping, Retry-After via `FakeClock`, …).
  - `tests/unit/` — the pure control loop with fakes (coordinator folds lane
    results, swarm cancels on first flag, solver counts only grader-verified
    flags, the image entrypoint always writes `result.json`).
  - `tests/test_smoke.py` — imports + the smallest domain guarantees.
- **`FakeClock` never really sleeps** — it advances virtual time and records
  durations on `.slept`, so backoff/pacing is asserted deterministically.
- Don't weaken a test to go green. If a test truly contradicts a port contract,
  fix the test and note it in the PR.
- Run just your slice while iterating: `python -m pytest tests/contract -q`.
