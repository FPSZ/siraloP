"""Contract: SubprocessAdapter honors the frozen ExecutionPort — never raises on
ordinary failure, kills on timeout, truncates, and does real file I/O. Commands
use sys.executable so the suite is cross-platform (Windows dev, Linux prod).
"""
from __future__ import annotations

import sys

from agent.adapters.execution.subprocess_exec import SubprocessAdapter

# Forward slashes so the quoted interpreter path is safe under both cmd.exe and a
# POSIX shell (Windows dev routes through bash; backslashes are escape chars there).
PY = '"' + sys.executable.replace("\\", "/") + '"'


async def test_semicolon_chaining_runs_both_commands():
    # `;` is POSIX chaining the agent relies on (prod = Linux /bin/sh). This must
    # work in dev too — on Windows the adapter routes through bash/sh, not cmd.exe.
    res = await SubprocessAdapter().run(
        f"{PY} -c \"print('one')\" ; {PY} -c \"print('two')\""
    )
    assert res.exit_code == 0
    assert "one" in res.stdout and "two" in res.stdout


async def test_run_captures_stdout_and_exit0():
    res = await SubprocessAdapter().run(f"{PY} -c \"print('hello')\"")
    assert res.exit_code == 0
    assert "hello" in res.stdout
    assert res.timed_out is False


async def test_nonzero_exit_does_not_raise():
    res = await SubprocessAdapter().run(f"{PY} -c \"import sys; sys.exit(3)\"")
    assert res.exit_code == 3
    assert res.timed_out is False


async def test_timeout_kills_and_flags():
    res = await SubprocessAdapter().run(
        f"{PY} -c \"import time; time.sleep(30)\"", timeout_s=1
    )
    assert res.timed_out is True  # returned, not raised


async def test_output_is_truncated():
    res = await SubprocessAdapter(output_cap=20).run(f"{PY} -c \"print('x'*1000)\"")
    assert res.truncated is True
    assert len(res.stdout) + len(res.stderr) <= 20


async def test_write_then_read_roundtrip(tmp_path):
    adapter = SubprocessAdapter(root=str(tmp_path))
    target = str(tmp_path / "sub" / "data.bin")
    await adapter.write_file(target, b"payload")
    assert await adapter.read_file(target) == b"payload"


async def test_read_missing_raises_filenotfound(tmp_path):
    adapter = SubprocessAdapter(root=str(tmp_path))
    try:
        await adapter.read_file(str(tmp_path / "nope"))
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")


async def test_new_workspace_is_absolute_and_exists(tmp_path):
    import os

    ws = await SubprocessAdapter(root=str(tmp_path)).new_workspace("c1")
    assert os.path.isabs(ws)
    assert os.path.isdir(ws)
