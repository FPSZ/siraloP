"""Contract: SubprocessAdapter honors the frozen ExecutionPort — never raises on
ordinary failure, kills on timeout, truncates, does real file I/O, and drives a
PERSISTENT shell per workspace (cwd / env persist between commands).

Commands use sys.executable so the suite is cross-platform (Windows dev routes
through bash; Linux prod uses bash/sh).
"""
from __future__ import annotations

import os
import sys

from agent.adapters.execution.subprocess_exec import SubprocessAdapter

# Forward slashes so the quoted interpreter path is safe under both cmd.exe and a
# POSIX shell (Windows dev routes through bash; backslashes are escape chars there).
PY = '"' + sys.executable.replace("\\", "/") + '"'


async def _once(cmd: str, **kw):
    """Run one command in a throwaway adapter and tear its shell session down."""
    a = SubprocessAdapter()
    try:
        return await a.run(cmd, **kw)
    finally:
        await a.aclose()


async def test_semicolon_chaining_runs_both_commands():
    # `;` is POSIX chaining the agent relies on (prod = Linux shell). This must
    # work in dev too — on Windows the adapter routes through bash/sh, not cmd.exe.
    res = await _once(f"{PY} -c \"print('one')\" ; {PY} -c \"print('two')\"")
    assert res.exit_code == 0
    assert "one" in res.stdout and "two" in res.stdout


async def test_run_captures_stdout_and_exit0():
    res = await _once(f"{PY} -c \"print('hello')\"")
    assert res.exit_code == 0
    assert "hello" in res.stdout
    assert res.timed_out is False


async def test_nonzero_exit_does_not_raise():
    res = await _once(f"{PY} -c \"import sys; sys.exit(3)\"")
    assert res.exit_code == 3
    assert res.timed_out is False


async def test_timeout_kills_and_flags():
    res = await _once(f"{PY} -c \"import time; time.sleep(30)\"", timeout_s=1)
    assert res.timed_out is True  # returned, not raised


async def test_output_is_truncated():
    a = SubprocessAdapter(output_cap=20)
    try:
        res = await a.run(f"{PY} -c \"print('x'*1000)\"")
    finally:
        await a.aclose()
    assert res.truncated is True
    assert len(res.stdout) + len(res.stderr) <= 20


async def test_shell_session_persists_cwd_and_env():
    # The defining property of the persistent shell: a `cd` / `export` in one
    # command is still in effect for the next (same workspace). This is what lets
    # the agent extract an archive then read a file inside it across turns.
    a = SubprocessAdapter()
    try:
        ws = await a.new_workspace("c1")
        await a.run("mkdir -p sub && cd sub && export FOO=bar123", workdir=ws)
        res = await a.run("pwd; echo $FOO", workdir=ws)
    finally:
        await a.aclose()
    assert res.exit_code == 0
    assert "/sub" in res.stdout  # cwd carried over (pwd shows the subdir)
    assert "bar123" in res.stdout  # env carried over


async def test_new_workspace_is_unique_and_seeded(tmp_path):
    # Each workspace is distinct (racing lanes never share shell state) and is
    # seeded with the files staged under the adapter root (the attachments).
    (tmp_path / "archive.zip").write_bytes(b"PK\x03\x04seed")
    a = SubprocessAdapter(root=str(tmp_path))
    try:
        ws1 = await a.new_workspace("c1")
        ws2 = await a.new_workspace("c1")
        assert ws1 != ws2
        assert os.path.isfile(os.path.join(ws1, "archive.zip"))
        assert os.path.isfile(os.path.join(ws2, "archive.zip"))
    finally:
        await a.aclose()


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
    ws = await SubprocessAdapter(root=str(tmp_path)).new_workspace("c1")
    assert os.path.isabs(ws)
    assert os.path.isdir(ws)
