"""Contract: real exit status, timeout cleanup, bounded capture, file I/O and
restored workspace state through the frozen ExecutionPort.

Commands use sys.executable and local Bash on Windows and Linux.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
import tracemalloc

import pytest

from agent.adapters.execution.subprocess_exec import (
    SubprocessAdapter,
    _tool_environment,
)

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


async def test_timeout_preserves_output_captured_before_kill():
    res = await _once(
        f"{PY} -c \"import time; print('before-timeout', flush=True); time.sleep(30)\"",
        timeout_s=1,
    )
    assert res.timed_out is True
    assert "before-timeout" in res.stdout


async def test_output_is_truncated():
    a = SubprocessAdapter(output_cap=20)
    try:
        res = await a.run(f"{PY} -c \"print('x'*1000)\"")
    finally:
        await a.aclose()
    assert res.truncated is True
    assert len(res.stdout) + len(res.stderr) <= 20
    assert res.stdout.startswith("x" * 16)


async def test_output_capture_keeps_tail_marker():
    adapter = SubprocessAdapter(output_cap=64)
    try:
        res = await adapter.run(
            f"{PY} -c \"import sys; sys.stdout.write('x'*1000+'TAIL_MARKER')\"",
        )
    finally:
        await adapter.aclose()
    assert res.truncated is True
    assert "TAIL_MARKER" in res.stdout


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
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "sample.bin").write_bytes(b"nested-seed")
    a = SubprocessAdapter(root=str(tmp_path))
    try:
        ws1 = await a.new_workspace("c1")
        ws2 = await a.new_workspace("c1")
        assert ws1 != ws2
        assert os.path.isfile(os.path.join(ws1, "archive.zip"))
        assert os.path.isfile(os.path.join(ws2, "archive.zip"))
        assert os.path.isfile(os.path.join(ws1, "assets", "sample.bin"))
        assert os.path.isfile(os.path.join(ws2, "assets", "sample.bin"))
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


def _python(source: str) -> str:
    return f"{PY} -c {shlex.quote(source)}"


async def test_tools_and_grandchildren_receive_only_safe_environment(tmp_path, monkeypatch):
    credential_names = (
        "CTFY_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    )
    for name in credential_names:
        monkeypatch.setenv(name, "synthetic-control-" + name.lower())
    control_home = tmp_path / "controller-home"
    control_home.mkdir()
    (control_home / ".netrc").write_text("synthetic-private-config", encoding="utf-8")
    injection = tmp_path / "startup.bash"
    touched = tmp_path / "startup-ran"
    injection.write_text(f"touch {shlex.quote(touched.as_posix())}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(control_home))
    monkeypatch.setenv("USERPROFILE", str(control_home))
    monkeypatch.setenv("APPDATA", str(control_home))
    monkeypatch.setenv("LOCALAPPDATA", str(control_home))
    monkeypatch.setenv("BASH_ENV", str(injection))
    monkeypatch.setenv("ENV", str(injection))
    monkeypatch.setenv("LD_PRELOAD", "/synthetic/not-a-library.so")
    monkeypatch.setenv("PYTHONPATH", "/synthetic/controller-only")
    probe = (
        "import json,os,shutil,subprocess,sys,tempfile;"
        "assert shutil.which('bash');"
        "temp=tempfile.TemporaryFile();temp.write(b'usable');temp.close();"
        "child=json.loads(subprocess.check_output([sys.executable,'-c',"
        "'import os,json;print(json.dumps(dict(os.environ)))'],text=True));"
        "print(json.dumps([dict(os.environ),child]))"
    )
    result = await _once(_python(probe))
    assert result.exit_code == 0
    environments = json.loads(result.stdout)
    for environment in environments:
        assert all(name not in environment for name in credential_names)
        assert all(
            name not in environment for name in ("BASH_ENV", "ENV", "LD_PRELOAD", "PYTHONPATH")
        )
        assert environment["PATH"]
        assert environment["TEMP"]
        assert environment["HOME"] != str(control_home)
        assert environment["USERPROFILE"] != str(control_home)
        assert environment["APPDATA"] != str(control_home)
        assert environment["LOCALAPPDATA"] != str(control_home)
    assert not touched.exists()


def test_preexisting_tool_dir_is_not_reprepended_over_preferred(tmp_path, monkeypatch):
    """A tool dir already on PATH must not be re-prepended ahead of a dir that
    precedes it there.

    In the Linux competition image bash lives in /usr/bin, while the pip-installed
    CTF interpreter (Crypto/pwntools/sympy/gmpy2) sits in /usr/local/bin, which
    precedes /usr/bin on the default PATH. Re-prepending the shell's /usr/bin would
    shadow /usr/local/bin's python3 with the bare system python3 that has none of
    the CTF libs — the model's `import Crypto` would fail and it would burn turns
    pip-installing (impossible on the offline image). The pre-existing ordering
    must survive, and a dir already on PATH must not be duplicated.
    """
    preferred = tmp_path / "preferred-bin"  # stands in for /usr/local/bin
    shell_dir = tmp_path / "shell-bin"      # stands in for /usr/bin (bash lives here)
    other = tmp_path / "other-bin"
    for directory in (preferred, shell_dir, other):
        directory.mkdir()
    monkeypatch.setenv(
        "PATH", os.pathsep.join(str(d) for d in (preferred, shell_dir, other))
    )
    env = _tool_environment(str(tmp_path / "home"), str(shell_dir / "bash"))

    def norm(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    parts = [norm(p) for p in env["PATH"].split(os.pathsep) if p]
    assert norm(str(preferred)) in parts and norm(str(shell_dir)) in parts
    # The preferred dir keeps its lead; the shell's own dir is not re-prepended.
    assert parts.index(norm(str(preferred))) < parts.index(norm(str(shell_dir)))
    assert parts.count(norm(str(shell_dir))) == 1


async def test_stdin_cannot_supply_engine_frames_or_forge_completion(tmp_path):
    # This reproduces the old exploit: read the queued printf control line from
    # stdin, extract its marker, then emit a successful completion before sleeping.
    # A safe adapter supplies EOF instead and waits for the real exit code.
    complete = tmp_path / "really-finished"
    source = (
        "import os,re,sys,time;"
        "line=sys.stdin.readline();"
        "marker=re.search(r'(__CTFY_[0-9a-f]+__)',line);"
        "print((marker.group(1) if marker else '__CTFY_0123456789abcdef__')+'0',flush=True);"
        "print('stdin='+repr(line),flush=True);"
        "time.sleep(0.25);"
        f"open({str(complete)!r},'w').write('done');"
        "sys.exit(7)"
    )
    result = await _once(_python(source))
    assert result.exit_code == 7
    assert complete.read_text() == "done"
    assert "stdin=''" in result.stdout
    assert "__CTFY_0123456789abcdef__0" in result.stdout


async def test_shell_explicit_exit_preserves_real_nonzero_status():
    result = await _once("printf '__CTFY_fake__0\\n'; exit 19")
    assert result.exit_code == 19
    assert "__CTFY_fake__0" in result.stdout


async def test_timeout_kills_grandchild_before_delayed_side_effect(tmp_path):
    side_effect = tmp_path / "too-late"
    ready = tmp_path / "grandchild-started"
    child = f"import time;time.sleep(1.8);open({str(side_effect)!r},'w').write('bad')"
    source = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"open({str(ready)!r},'w').write('ready');"
        "time.sleep(30)"
    )
    result = await _once(_python(source), timeout_s=1)
    assert result.timed_out
    assert ready.exists()
    await asyncio.sleep(2)
    assert not side_effect.exists()


async def test_cancellation_reaps_tree_discards_state_and_prevents_output_crosstalk(tmp_path):
    adapter = SubprocessAdapter(root=str(tmp_path))
    ready = tmp_path / "ready"
    late = tmp_path / "late"
    child = (
        "import time;"
        "time.sleep(0.8);"
        f"open({str(late)!r},'w').write('bad');"
        "print('OLD-LATE-OUTPUT',flush=True)"
    )
    source = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
        f"open({str(ready)!r},'w').write('ready');"
        "print('OLD-EARLY-OUTPUT',flush=True);time.sleep(30)"
    )
    try:
        await adapter.run("export OLD_STATE=should-disappear")
        running = asyncio.create_task(adapter.run(_python(source)))
        try:
            async with asyncio.timeout(5):
                while not ready.exists():
                    await asyncio.sleep(0.01)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            result = await adapter.run("printf 'NEW-OUTPUT:%s' \"${OLD_STATE-unset}\"")
            await asyncio.sleep(1)
            assert result.exit_code == 0
            assert result.stdout == "NEW-OUTPUT:unset"
            assert not late.exists()
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
    finally:
        await adapter.aclose()


async def test_shell_plain_variables_arrays_functions_and_workspace_isolation(tmp_path):
    adapter = SubprocessAdapter(root=str(tmp_path))
    try:
        first = await adapter.new_workspace("same")
        second = await adapter.new_workspace("same")
        setup = await adapter.run(
            "mkdir -p nested; cd nested; PLAIN='ordinary value'; "
            "export EXPORTED='exported value'; ITEMS=('one value' two); "
            "declare -A LOOKUP=([key]='map value'); "
            "greet() { printf 'function:%s:%s:%s:%s\\n' "
            "\"$PLAIN\" \"$EXPORTED\" \"${ITEMS[0]}\" \"${LOOKUP[key]}\"; }",
            workdir=first,
        )
        assert setup.exit_code == 0
        restored = await adapter.run("pwd; greet", workdir=first)
        assert restored.exit_code == 0
        assert "/nested" in restored.stdout
        assert "function:ordinary value:exported value:one value:map value" in restored.stdout
        isolated = await adapter.run(
            "printf 'plain:%s\\n' \"${PLAIN-unset}\"; declare -F greet", workdir=second
        )
        assert isolated.stdout == "plain:unset\n"
        assert isolated.exit_code != 0
    finally:
        await adapter.aclose()


async def test_redirected_background_service_survives_command_boundary(tmp_path):
    adapter = SubprocessAdapter(root=str(tmp_path))
    ready = tmp_path / "background-ready"
    source = f"import time;time.sleep(0.15);open({str(ready)!r},'w').write('ready');time.sleep(30)"
    try:
        result = await adapter.run(_python(source) + " </dev/null >service.log 2>&1 &", timeout_s=2)
        assert result.exit_code == 0
        assert not result.timed_out
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        following = await adapter.run("printf following")
        assert following.stdout == "following"
    finally:
        await adapter.aclose()


async def test_output_capture_memory_is_bounded_not_just_return_value(tmp_path):
    adapter = SubprocessAdapter(root=str(tmp_path), output_cap=1024)
    # Prime process/event-loop initialization before measuring Python allocations.
    try:
        await adapter.run("true")
        tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            source = "import os;chunk=b'x'*65536;[os.write(1,chunk) for _ in range(512)]"
            result = await adapter.run(_python(source))
            peak = tracemalloc.get_traced_memory()[1] - baseline
        finally:
            tracemalloc.stop()
        assert result.exit_code == 0
        assert result.truncated
        assert result.stdout == "x" * 1024
        assert peak < 4 * 1024 * 1024
    finally:
        await adapter.aclose()


async def test_known_control_secrets_are_redacted_without_hiding_challenge_bearer(monkeypatch):
    secret = "synthetic-control-secret-not-a-real-credential"
    monkeypatch.setenv("CTFY_TOKEN", secret)
    result = await _once(_python(f"print({secret!r});print('Bearer challenge-owned-token')"))
    assert result.exit_code == 0
    assert secret not in result.stdout
    assert "Bearer challenge-owned-token" in result.stdout


async def test_redaction_precedes_output_cap_even_when_secret_straddles_cap(monkeypatch):
    secret = "synthetic-control-secret-crossing-the-output-limit"
    monkeypatch.setenv("LLM_API_KEY", secret)
    adapter = SubprocessAdapter(output_cap=20)
    try:
        result = await adapter.run(_python(f"print('prefix-prefix-'+{secret!r})"))
    finally:
        await adapter.aclose()
    assert "synthe" not in result.stdout
    assert secret not in result.stdout


async def test_repeated_secrets_and_chunk_boundaries_cannot_leak_truncated_key(monkeypatch):
    from agent.adapters._redaction import redact

    secret = "synthetic-repeated-control-credential-" + "k" * 47
    monkeypatch.setenv("OPENAI_AUTH_TOKEN", secret)
    adapter = SubprocessAdapter(output_cap=3000)
    try:
        repeated = await adapter.run(_python(f"import sys;sys.stdout.write({secret!r}*10000)"))
        assert repeated.exit_code == 0
        assert repeated.truncated
        expected = redact(secret, redact_bearer=False) * 10000
        assert len(repeated.stdout) == 3000
        assert repeated.stdout.startswith(expected[:2400])
        assert repeated.stdout.endswith(expected[-600:])
        boundary = await adapter.run(_python(f"import sys;sys.stdout.write('x'*2990+{secret!r})"))
        assert boundary.exit_code == 0
        assert "synthetic" not in boundary.stdout
        assert boundary.stdout.startswith("x" * 2400)
        unicode_secret = "synthetic-control-雪-credential"
        monkeypatch.setenv("OPENAI_AUTH_TOKEN", unicode_secret)
        encoded = json.dumps(unicode_secret, ensure_ascii=True)[1:-1]
        escaped = await adapter.run(_python(f"import sys;sys.stdout.write({encoded!r}*10000)"))
        assert escaped.exit_code == 0
        assert escaped.stdout == (redact(encoded, redact_bearer=False) * 10000)[:3000]
    finally:
        await adapter.aclose()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux procfs protection")
async def test_linux_tool_cannot_read_controller_environment(tmp_path):
    # Use a separate controller whose credential exists at exec time: mutating
    # os.environ in this test process would not faithfully populate procfs.
    script = tmp_path / "controller.py"
    script.write_text(
        "import asyncio,os,shlex,sys\n"
        "from agent.adapters.execution.subprocess_exec import SubprocessAdapter\n"
        "async def main():\n"
        " a=SubprocessAdapter()\n"
        " source=\"import os; p='/proc/\"+str(os.getpid())+\"/environ'; "
        "print(open(p,'rb').read())\"\n"
        " try:\n"
        "  r=await a.run(shlex.quote(sys.executable)+' -c '+shlex.quote(source))\n"
        "  print(r.exit_code);print(r.stdout)\n"
        " finally: await a.aclose()\n"
        "asyncio.run(main())\n",
        encoding="utf-8",
    )
    environment = {
        name: os.environ[name]
        for name in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    environment["CTFY_TOKEN"] = "synthetic-proc-controller-token"
    environment["PYTHONPATH"] = os.getcwd()
    controller = await asyncio.create_subprocess_exec(
        sys.executable, str(script), env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(controller.communicate(), 15)
    assert controller.returncode == 0, stderr.decode()
    assert stdout.decode().splitlines()[0] != "0"
    assert "PermissionError" in stdout.decode()
    assert b"synthetic-proc-controller-token" not in stdout


@pytest.mark.parametrize("capability_set", ["effective", "permitted", "inheritable"])
def test_linux_ptrace_capable_controller_fails_closed(tmp_path, monkeypatch, capability_set):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from agent.adapters.execution import _security

    def capget(_header, data):
        setattr(data[0], capability_set, 1 << 19)
        return 0

    libc = SimpleNamespace(capget=Mock(side_effect=capget), prctl=Mock(return_value=0))
    monkeypatch.setattr(_security.sys, "platform", "linux")
    monkeypatch.setattr(_security.ctypes, "CDLL", lambda *args, **kwargs: libc)
    with pytest.raises(RuntimeError, match="CAP_SYS_PTRACE"):
        SubprocessAdapter(root=str(tmp_path))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux no_new_privs")
async def test_linux_tools_and_grandchildren_cannot_regain_exec_privileges():
    child = "import ctypes;print(ctypes.CDLL(None).prctl(39,0,0,0,0))"
    source = (
        "import ctypes,json,pathlib,subprocess,sys;"
        "libc=ctypes.CDLL(None);"
        "status=dict(line.split(':',1) "
        "for line in pathlib.Path('/proc/self/status').read_text().splitlines());"
        "print(json.dumps({"
        "'nnp':libc.prctl(39,0,0,0,0),"
        "'unset':libc.prctl(38,0,0,0,0),"
        f"'grandchild_nnp':int(subprocess.check_output([sys.executable,'-c',{child!r}])),"
        "'caps':[int(status[name].strip(),16) for name in ('CapEff','CapPrm','CapInh','CapAmb')]"
        "}))"
    )
    result = await _once(_python(source))
    assert result.exit_code == 0
    observed = json.loads(result.stdout)
    assert observed["nnp"] == observed["grandchild_nnp"] == 1
    assert observed["unset"] == -1
    assert all(not (capabilities & (1 << 19)) for capabilities in observed["caps"])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux ptrace debugging")
async def test_linux_tool_can_still_ptrace_its_own_child():
    source = """
import ctypes
import os
import signal

libc = ctypes.CDLL(None, use_errno=True)
pid = os.fork()
if pid == 0:
    if libc.ptrace(0, 0, None, None) != 0:  # PTRACE_TRACEME
        os._exit(21)
    os.kill(os.getpid(), signal.SIGSTOP)
    os._exit(0)
_, status = os.waitpid(pid, 0)
assert os.WIFSTOPPED(status), status
assert os.WSTOPSIG(status) == signal.SIGSTOP
assert libc.ptrace(7, pid, None, None) == 0  # PTRACE_CONT
_, status = os.waitpid(pid, 0)
assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, status
print('own-child-debug-ok')
"""
    result = await _once(_python(source))
    assert result.exit_code == 0
    assert result.stdout.strip() == "own-child-debug-ok"


@pytest.mark.parametrize("standard_git", [True, False])
def test_windows_default_shell_avoids_wsl_launcher(monkeypatch, standard_git):
    from agent.adapters.execution import subprocess_exec

    standard = "C:/Program Files/Git/bin/bash.exe"
    portable = "D:/PortableGit/bin/bash.exe"
    launcher = "C:/Users/test/AppData/Local/Microsoft/WindowsApps/bash.exe"
    available = {portable, launcher}
    if standard_git:
        available.add(standard)
    monkeypatch.setattr(subprocess_exec, "IS_WINDOWS", True)
    monkeypatch.setenv("ProgramFiles", "C:/Program Files")
    monkeypatch.setattr(
        os, "get_exec_path",
        lambda: [launcher.rsplit("/", 1)[0], portable.rsplit("/", 1)[0]],
    )
    monkeypatch.setattr(
        os.path, "isfile", lambda path: path.replace("\\", "/") in available
    )
    chosen = subprocess_exec._bash_path(None).replace("\\", "/")
    assert chosen in available - {launcher}


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO and directory-fd anchoring")
@pytest.mark.parametrize("replacement", ["fifo", "directory", "symlink", "parent-directory"])
def test_replaced_command_paths_cannot_block_controller_or_overwrite_sentinel(
    tmp_path, replacement,
):
    """Independent watchdog: a blocked adapter must not hang pytest's own loop."""
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("synthetic-sentinel-unchanged", encoding="utf-8")
    substituted = tmp_path / "substituted"
    substituted.mkdir()
    os.mkfifo(substituted / "command.bash")
    locate = (
        'victim="${BASH_SOURCE[0]}"; '
        'case "$victim" in /dev/fd/*) victim=$(readlink "$victim"); '
        'victim="${victim% (deleted)}" ;; esac; '
    )
    attacks = {
        "fifo": 'rm -f -- "$victim"; mkfifo -- "$victim"',
        "directory": 'rm -f -- "$victim"; mkdir -- "$victim"',
        "symlink": 'rm -f -- "$victim"; ln -s -- ' + shlex.quote(str(sentinel)) + ' "$victim"',
        "parent-directory": (
            'parent="${victim%/*}"; mv -- "$parent" "$parent-moved"; '
            "ln -s -- " + shlex.quote(str(substituted)) + ' "$parent"'
        ),
    }
    controller_script = tmp_path / "watchdog-controller.py"
    controller_script.write_text(
        "import asyncio,json\n"
        "from agent.adapters.execution.subprocess_exec import SubprocessAdapter\n"
        "async def main():\n"
        f" a=SubprocessAdapter(root={str(tmp_path / 'workspace')!r})\n"
        " try:\n"
        f"  first=await a.run({(locate + attacks[replacement])!r},timeout_s=2)\n"
        "  assert first.exit_code==0, first\n"
        "  second=await a.run('printf SECOND',timeout_s=2)\n"
        f"  other=await a.run('printf OTHER',workdir={str(tmp_path / 'other')!r},timeout_s=2)\n"
        "  print(json.dumps([second.exit_code,second.stdout,other.exit_code,other.stdout]))\n"
        " finally: await a.aclose()\n"
        "asyncio.run(main())\n",
        encoding="utf-8",
    )
    environment = {
        name: os.environ[name]
        for name in ("PATH", "SystemRoot", "WINDIR")
        if name in os.environ
    }
    environment.update({
        "PYTHONPATH": os.getcwd(), "TMPDIR": str(tmp_path),
        "TEMP": str(tmp_path), "TMP": str(tmp_path),
        "CTFY_TOKEN": "synthetic-watchdog-controller-token",
    })
    completed = subprocess.run(
        [sys.executable, str(controller_script)], env=environment,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [0, "SECOND", 0, "OTHER"]
    assert sentinel.read_text(encoding="utf-8") == "synthetic-sentinel-unchanged"


async def test_command_preparation_failure_is_a_safe_result_and_other_workspace_survives(
    tmp_path, monkeypatch,
):
    adapter = SubprocessAdapter(root=str(tmp_path))
    try:
        assert (await adapter.run("true")).exit_code == 0
        session = adapter._sessions[adapter._root]

        def fail_prepare(_cmd):
            raise OSError("synthetic command preparation failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(session, "_prepare_command", fail_prepare)
            failed = await adapter.run("printf must-not-run")
        assert failed.exit_code == -1
        assert "synthetic command preparation failure" in failed.stderr
        assert (await adapter.run("printf recovered")).stdout == "recovered"
        isolated = await adapter.run("printf isolated", workdir=str(tmp_path / "other"))
        assert isolated.stdout == "isolated"
    finally:
        await adapter.aclose()


async def test_bulk_shell_snapshot_preserves_many_variables_and_multiline_values():
    adapter = SubprocessAdapter()
    try:
        setup = await adapter.run(
            "for ((i=0;i<500;i++)); do printf -v \"VAR_$i\" 'value:%s' \"$i\"; done; "
            "printf -v MULTILINE '%s\\n%s' first second; "
            "export EXPORTED_MULTILINE=\"$MULTILINE\""
        )
        assert setup.exit_code == 0
        result = await adapter.run(
            "printf '%s\\n' \"$VAR_0\" \"$VAR_499\" \"$MULTILINE\"; "
            + _python(
                "import os;"
                "assert os.environ['EXPORTED_MULTILINE']=='first\\nsecond';print('export-ok')"
            )
        )
        assert result.exit_code == 0
        assert result.stdout.splitlines() == [
            "value:0", "value:499", "first", "second", "export-ok",
        ]
    finally:
        await adapter.aclose()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows suspended creation and Job membership")
@pytest.mark.parametrize("termination", ["timeout", "cancel", "close"])
async def test_windows_forged_old_gate_cannot_run_before_job_assignment(
    tmp_path, monkeypatch, termination,
):
    from pathlib import Path

    from agent.adapters.execution._security import WindowsJob

    adapter = SubprocessAdapter(root=str(tmp_path))
    watcher_ready = tmp_path / "watcher-ready"
    entered = tmp_path / "new-command-entered"
    late = tmp_path / "job-escaped-side-effect"
    watcher = (
        "import pathlib,sys,time;"
        "gate=pathlib.Path(sys.argv[1]).parent/'start';"
        "gate.write_text('forged');"
        f"pathlib.Path({str(watcher_ready)!r}).write_text(str(gate));"
        "exec('while True:\\n gate.write_text(str(time.monotonic_ns()))\\n time.sleep(0.002)')"
    )
    child = (
        "import pathlib,time;time.sleep(1.2);"
        f"pathlib.Path({str(late)!r}).write_text('escaped')"
    )
    source = (
        "import pathlib,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path({str(entered)!r}).write_text('entered');"
        + ("" if termination == "close" else "time.sleep(30)")
    )
    original_assign = WindowsJob.assign
    observed_windows = []
    try:
        started = await adapter.run(
            _python(watcher) + ' "${BASH_SOURCE[0]}" </dev/null >watcher.log 2>&1 &'
        )
        assert started.exit_code == 0
        async with asyncio.timeout(5):
            while not watcher_ready.exists():
                await asyncio.sleep(0.01)
        gate = Path(watcher_ready.read_text())

        def delayed_assign(job, pid):
            before = gate.stat().st_mtime_ns
            # Deliberately widen the real OS scheduling window. The existing
            # background forger runs, but the new process must remain suspended.
            time.sleep(0.4)
            assert gate.stat().st_mtime_ns > before
            assert not entered.exists()
            observed_windows.append(pid)
            original_assign(job, pid)

        monkeypatch.setattr(WindowsJob, "assign", delayed_assign)
        if termination == "cancel":
            running = asyncio.create_task(adapter.run(_python(source)))
            try:
                async with asyncio.timeout(5):
                    while not entered.exists() and not running.done():
                        await asyncio.sleep(0.01)
                assert entered.exists()
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            finally:
                if not running.done():
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
        else:
            result = await adapter.run(
                _python(source), timeout_s=0.3 if termination == "timeout" else 5,
            )
            assert entered.exists()
            if termination == "timeout":
                assert result.timed_out
            else:
                assert result.exit_code == 0
                await adapter.aclose()
        assert len(observed_windows) == 1
    finally:
        await adapter.aclose()
    await asyncio.sleep(1.5)
    assert not late.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows fail-closed suspended process cleanup")
@pytest.mark.parametrize("failure", ["assign", "resume"])
async def test_windows_job_setup_failure_never_executes_user_code(tmp_path, monkeypatch, failure):
    from agent.adapters.execution._security import WindowsJob

    marker = tmp_path / "must-not-execute"

    def fail(_job, _pid):
        raise OSError("synthetic job setup failure")

    monkeypatch.setattr(WindowsJob, failure, fail)
    adapter = SubprocessAdapter(root=str(tmp_path))
    try:
        result = await adapter.run(_python(f"open({str(marker)!r},'w').write('bad')"))
        assert result.exit_code == -1
        assert "synthetic job setup failure" in result.stderr
        assert not marker.exists()
    finally:
        await adapter.aclose()


async def test_unicode_stdout_is_preserved_on_native_python():
    text = "汉字 " + chr(0x20000) + " café — stdout"
    result = await _once(_python(f"print({text!r},end='')"))
    assert result.exit_code == 0
    assert result.stdout == text


async def test_tools_can_use_public_ca_override_without_proxy_credentials(tmp_path, monkeypatch):
    import hashlib
    import ssl
    from pathlib import Path

    import certifi

    bundle = Path(certifi.where()).read_text(encoding="utf-8")
    begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    certificate = begin + bundle.split(begin, 1)[1].split(end, 1)[0] + end + "\n"
    expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
    ca_file = tmp_path / "public-ca.pem"
    ca_file.write_text(certificate, encoding="ascii")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic-user:synthetic-password@proxy.invalid")
    source = (
        "import hashlib,json,os,ssl;"
        "context=ssl.create_default_context("
        "cafile=os.environ['SSL_CERT_FILE'],capath=os.environ['SSL_CERT_DIR']);"
        "print(json.dumps({'fingerprints':[hashlib.sha256(cert).hexdigest() "
        "for cert in context.get_ca_certs(binary_form=True)],"
        "'proxy_auth_absent':'HTTP_PROXY' not in os.environ}))"
    )
    result = await _once(_python(source))
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "fingerprints": [expected], "proxy_auth_absent": True,
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows parent-directory handle protection")
async def test_windows_command_directory_stays_pinned_between_calls(tmp_path):
    adapter = SubprocessAdapter(root=str(tmp_path))
    try:
        assert (await adapter.run("true")).exit_code == 0
        session = adapter._sessions[adapter._root]
        directory = session._directory
        assert directory.is_dir()
        with pytest.raises(OSError) as denied:
            os.rename(directory, str(directory) + "-replaced")
        assert denied.value.winerror in (5, 32)
        following = await adapter.run("printf directory-still-pinned")
        assert following.exit_code == 0
        assert following.stdout == "directory-still-pinned"
    finally:
        await adapter.aclose()


@pytest.mark.skipif(os.name != "nt", reason="Windows metadata write/reparse protection")
async def test_tool_cannot_open_controller_directory_for_reparse_writes(tmp_path):
    source = r'''
import ctypes
import json
import pathlib
import sys
from ctypes import wintypes
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
handle = kernel.CreateFileW(
    str(pathlib.Path(sys.argv[1]).parent), 0x40000000, 7, None, 3, 0x02000000, None,
)
error = ctypes.get_last_error()
denied = handle == ctypes.c_void_p(-1).value
if not denied:
    kernel.CloseHandle(handle)
print(json.dumps({"write_denied": denied and error in (5, 32)}))
'''
    adapter = SubprocessAdapter(root=str(tmp_path))
    try:
        result = await adapter.run(_python(source) + ' "${BASH_SOURCE[0]}"')
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"write_denied": True}
        following = await adapter.run("printf metadata-protected")
        assert following.exit_code == 0
        assert following.stdout == "metadata-protected"
    finally:
        await adapter.aclose()


@pytest.mark.skipif(os.name != "nt", reason="Windows shared MSYS temporary mount")
async def test_windows_tmp_mount_survives_concurrent_workspace_cleanup(tmp_path):
    first = SubprocessAdapter(root=str(tmp_path / "first"))
    second = SubprocessAdapter(root=str(tmp_path / "second"))
    first_ready, second_ready = tmp_path / "first-ready", tmp_path / "second-ready"
    release_first, release_second = tmp_path / "release-first", tmp_path / "release-second"
    running = []
    try:
        first_command = (
            f"printf ready > {shlex.quote(first_ready.as_posix())}; "
            f"while ! test -f {shlex.quote(release_first.as_posix())}; do sleep 0.01; done"
        )
        first_task = asyncio.create_task(first.run(first_command, timeout_s=5))
        running.append(first_task)
        async with asyncio.timeout(5):
            while not first_ready.exists():
                await asyncio.sleep(0.01)
        second_command = (
            f"printf ready > {shlex.quote(second_ready.as_posix())}; "
            f"while ! test -f {shlex.quote(release_second.as_posix())}; do sleep 0.01; done; "
            "temporary=$(mktemp /tmp/ctfy-mount-XXXXXXXX) || exit; "
            "printf usable > \"$temporary\"; cat -- \"$temporary\"; rm -f -- \"$temporary\""
        )
        second_task = asyncio.create_task(second.run(second_command, timeout_s=5))
        running.append(second_task)
        async with asyncio.timeout(5):
            while not second_ready.exists():
                await asyncio.sleep(0.01)
        release_first.touch()
        assert (await first_task).exit_code == 0
        await first.aclose()
        release_second.touch()
        result = await second_task
        assert result.exit_code == 0
        assert result.stdout == "usable"
    finally:
        for task in running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await first.aclose()
        await second.aclose()
