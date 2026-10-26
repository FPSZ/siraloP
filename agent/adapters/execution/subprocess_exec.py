"""Supervised, bounded tool execution with per-workspace Bash state.

Each command has its own OS process lifecycle. Workspace snapshots restore shell
data, never completion or exit status; stdin is EOF, not an engine control stream.
This is process hygiene and cancellation, not a filesystem/network OS sandbox.
"""
from __future__ import annotations

import asyncio
import codecs
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent.adapters._redaction import _secret_matcher, redact
from agent.adapters.execution._security import WindowsDirectory, WindowsJob, protect_controller
from agent.domain.state import ExecResult

IS_WINDOWS = os.name == "nt"

_REAP_TIMEOUT_S = 5.0
_CREDENTIAL_NAMES = (
    "CTFY_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
)


def _bash_path(override: str | None) -> str:
    if override:
        return override
    if IS_WINDOWS:
        # System32/bash.exe is the WSL launcher, not a local POSIX shell.
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            candidate = os.path.join(base, "Git", "bin", "bash.exe")
            if os.path.isfile(candidate):
                return candidate
        for entry in os.get_exec_path():
            if {"windows", "windowsapps"} & set(entry.lower().replace("\\", "/").split("/")):
                continue
            candidate = os.path.join(entry, "bash.exe")
            if os.path.isfile(candidate):
                return candidate
    else:
        discovered = shutil.which("bash")
        if discovered:
            return discovered
    raise RuntimeError("Broken execution environment: a local Bash installation is required")


def _tool_environment(home: str, shell: str) -> dict[str, str]:
    """Allow essential tool discovery/OS plumbing, not control-plane settings."""
    allowed = (
        "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT",
        "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS", "SSL_CERT_FILE",
        "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    tool_paths = [str(Path(shell).parent)]
    if IS_WINDOWS:
        git_tools = Path(shell).parent.parent / "usr" / "bin"
        if git_tools.is_dir():
            tool_paths.append(str(git_tools))
    # Only PREPEND tool dirs that are not already on PATH. Prepending a system dir
    # that is already present — the Linux image's /usr/bin, where bash lives —
    # would reorder it AHEAD of /usr/local/bin and shadow the pip-installed CTF
    # interpreter (Crypto/pwntools/sympy/gmpy2) with the bare system python3,
    # crippling every crypto/pwn challenge: the model's `import Crypto` fails and
    # it burns turns trying to pip-install (which cannot succeed on the offline
    # competition image). Windows' Git-bundled tool dirs are not already on PATH,
    # so they are still prepended as intended.
    inherited = env.get("PATH", os.defpath)
    seen = {
        os.path.normcase(os.path.normpath(p))
        for p in inherited.split(os.pathsep)
        if p
    }
    new_paths = [
        p for p in tool_paths if os.path.normcase(os.path.normpath(p)) not in seen
    ]
    env["PATH"] = os.pathsep.join([*new_paths, inherited]) if new_paths else inherited
    temp = os.path.join(home, "tmp")
    config = os.path.join(home, "config")
    os.makedirs(temp, exist_ok=True)
    os.makedirs(config, exist_ok=True)
    # MSYS shares /tmp mounts between concurrent shells. Its native TEMP/TMP
    # root must outlive individual workspaces; TMPDIR remains workspace-private.
    runtime_temp = tempfile.gettempdir() if IS_WINDOWS else temp
    env.update({
        "HOME": home, "USERPROFILE": home,
        "APPDATA": config, "LOCALAPPDATA": os.path.join(home, "cache"),
        "TMP": runtime_temp, "TEMP": runtime_temp, "TMPDIR": temp,
        "XDG_CONFIG_HOME": config, "XDG_CACHE_HOME": os.path.join(home, "cache"),
        "XDG_DATA_HOME": os.path.join(home, "data"), "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PAGER": "cat", "GIT_PAGER": "cat", "MANPAGER": "cat",
        "SYSTEMD_PAGER": "cat", "LESS": "-F", "NO_COLOR": "1", "CLICOLOR": "0",
        "PWNLIB_NOTERM": "1", "PWNLIB_SILENT": "1",
        "DEBIAN_FRONTEND": "noninteractive",
        "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    })
    return env


class SubprocessAdapter:
    """Execute commands and manage files as child processes of this container."""

    def __init__(
        self,
        root: str | None = None,
        *,
        max_concurrency: int = 4,
        output_cap: int = 64000,
        posix_shell: str | None = None,
    ) -> None:
        protect_controller()
        self._root = os.path.abspath(root) if root else tempfile.mkdtemp(prefix="chal-")
        os.makedirs(self._root, exist_ok=True)
        self._session_argv = [_bash_path(posix_shell), "--norc", "--noprofile"]
        self._sem = asyncio.Semaphore(max_concurrency)
        self._output_cap = max(0, output_cap)
        self._sessions: dict[str, _ShellSession] = {}
        self._ws_counter = 0
        self._closed = False

    # -- ExecutionPort ----------------------------------------------------

    async def run(
        self, cmd: str, *, timeout_s: int = 60, workdir: str | None = None
    ) -> ExecResult:
        """Run a shell command, capturing stdout and stderr.

        Never raises on a command failure: a non-zero exit becomes an
        ``ExecResult`` carrying that ``exit_code``; a timeout kills the whole
        process tree and returns ``ExecResult(timed_out=True)``. Combined output
        is truncated to ``output_cap`` (``truncated=True`` when it was cut).
        """
        async with self._sem:
            return await self._run_inner(cmd, timeout_s, workdir)

    async def read_file(self, path: str) -> bytes:
        """Read a file's bytes. Raises FileNotFoundError if it is absent."""
        loop = asyncio.get_running_loop()

        def _read() -> bytes:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(path)
            return p.read_bytes()

        return await loop.run_in_executor(None, _read)

    async def write_file(self, path: str, data: bytes) -> None:
        """Write bytes, creating parent directories and overwriting any file."""
        loop = asyncio.get_running_loop()

        def _write() -> None:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

        await loop.run_in_executor(None, _write)

    async def new_workspace(self, challenge_id: str) -> str:
        """A FRESH, unique, absolute working directory for one solve attempt.

        Unique per call so racing lanes never share persistent-shell state. Seeded
        with the top-level files staged directly under the adapter root (e.g. the
        challenge's attachments), so every attempt starts from the same materials.
        """
        n = self._ws_counter
        self._ws_counter += 1
        safe = os.path.basename(str(challenge_id)) or "chal"
        loop = asyncio.get_running_loop()

        def _mk() -> str:
            d = os.path.abspath(os.path.join(self._root, f"{safe}-ws{n}"))
            os.makedirs(d, exist_ok=True)
            try:  # seed attachments (top-level files staged under the root)
                for name in os.listdir(self._root):
                    src = os.path.join(self._root, name)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(d, name))
            except OSError:
                pass
            return d

        return await loop.run_in_executor(None, _mk)

    # -- internals --------------------------------------------------------

    async def _run_inner(
        self, cmd: str, timeout_s: int, workdir: str | None
    ) -> ExecResult:
        if self._closed:
            return ExecResult(stdout="", stderr="", exit_code=-1, timed_out=False)
        loop = asyncio.get_running_loop()
        cwd = os.path.abspath(workdir) if workdir else self._root
        # Defensive: a missing cwd would make spawning raise; create it so run()
        # keeps its "don't raise on ordinary failures" contract.
        await loop.run_in_executor(None, lambda: os.makedirs(cwd, exist_ok=True))
        session = self._sessions.get(cwd)
        if session is None:
            try:
                session = _ShellSession(
                    self._session_argv, cwd, self._spawn_kwargs(), kill=self._kill_tree
                )
            except Exception as error:
                return ExecResult(
                    stdout="", stderr=redact(str(error), redact_bearer=False),
                    exit_code=-1, timed_out=False,
                )
            self._sessions[cwd] = session
        return await session.run(cmd, timeout_s, output_cap=self._output_cap)

    async def aclose(self) -> None:
        """Terminate active commands and any redirected background services."""
        self._closed = True
        sessions, self._sessions = self._sessions, {}
        for s in sessions.values():
            try:
                await s.aclose()
            except Exception:
                pass

    @staticmethod
    def _spawn_kwargs() -> dict[str, Any]:
        """Spawn options that make the child the head of its own killable group."""
        if sys.platform == "win32":
            # CREATE_SUSPENDED: assign to a Job before any Bash startup/user code.
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004}
        return {"start_new_session": True}

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Hard-kill the command AND every process it spawned, cross-platform."""
        pid = proc.pid
        if pid is None:
            return
        if sys.platform == "win32":
            if not await self._taskkill(pid):
                self._safe_kill(proc)
            return
        try:
            # The group can outlive its shell (explicitly redirected services).
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
        except Exception:
            self._safe_kill(proc)

    @staticmethod
    async def _taskkill(pid: int) -> bool:
        """Kill a Windows process tree via taskkill; True on success."""
        loop = asyncio.get_running_loop()

        def _call() -> bool:
            try:
                r = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    check=False,
                )
                return r.returncode == 0
            except Exception:
                return False

        try:
            return await loop.run_in_executor(None, _call)
        except Exception:
            return False

    @staticmethod
    def _safe_kill(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
        except Exception:
            pass



class _ShellSession:
    """Serialize commands while carrying only untrusted workspace shell data.

    The command is sourced from its own file, never stdin. EXIT writes ordinary
    Bash variables, exports, functions and cwd for the next invocation. Changing
    that snapshot or the EXIT trap can change workspace state, but cannot make
    the OS report that a still-running process has completed.
    """

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        spawn_kwargs: dict[str, Any],
        *,
        kill: Any,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._spawn_kwargs = spawn_kwargs
        self._kill = kill
        self._lock = asyncio.Lock()
        self._storage = tempfile.TemporaryDirectory(prefix="shell-state-")
        self._directory = Path(self._storage.name)
        self._generation = 0
        self._state = self._directory / "state-0.bash"
        self._home = str(self._directory / "home")
        self._env = _tool_environment(self._home, argv[0])
        self._directory_fd: int | None = None
        if sys.platform != "win32":
            self._directory_fd = os.open(
                self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        self._directory_lock = WindowsDirectory(str(self._directory)) if IS_WINDOWS else None
        self._groups: list[asyncio.subprocess.Process] = []
        self._jobs: list[WindowsJob] = []
        self._closed = False

    def _prepare_command(self, cmd: str) -> tuple[str, int]:
        """Create once through a pinned directory, then use the creating handle."""
        name = "command-" + os.urandom(16).hex() + ".bash"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if sys.platform == "win32":
            flags |= os.O_BINARY
            descriptor = os.open(self._directory / name, flags, 0o600)
        else:
            flags |= os.O_NOFOLLOW
            descriptor = os.open(name, flags, 0o600, dir_fd=self._directory_fd)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write((cmd + "\n").encode("utf-8"))
            os.lseek(descriptor, 0, os.SEEK_SET)
            if not IS_WINDOWS:
                # Source the inherited file descriptor, not a replaceable path.
                # Unlinking also prevents a later command from poisoning it.
                os.unlink(name, dir_fd=self._directory_fd)
                return f"/dev/fd/{descriptor}", descriptor
            # The open CRT descriptor denies deletion; the directory handle
            # prevents parent replacement. Keep it open until the command ends.
            return (self._directory / name).as_posix(), descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _script(self, command: str) -> str:
        state = shlex.quote(self._state.as_posix())
        command_path = shlex.quote(command)
        trap_command = shlex.quote(
            f'_siralo_status=$?; _siralo_snapshot > {state}; builtin exit "$_siralo_status"'
        )
        return f"""
if builtin test -f {state}; then builtin source {state}; fi
_siralo_snapshot() {{
    builtin printf 'builtin cd -- %q\\n' "$(builtin pwd -P)"
    while IFS= builtin read -r _siralo_decl; do
        _siralo_flags=${{_siralo_decl#declare -}}
        _siralo_flags=${{_siralo_flags%% *}}
        case "$_siralo_flags" in *r*) continue ;; esac
        _siralo_name=${{_siralo_decl#declare }}
        _siralo_name=${{_siralo_name#* }}
        _siralo_name=${{_siralo_name%%=*}}
        case "$_siralo_name" in
            _siralo_*|BASH*|EUID|UID|PPID|SHELLOPTS|BASHPID|FUNCNAME|GROUPS) continue ;;
            DIRSTACK|PIPESTATUS|LINENO|OPTIND|OPTARG|RANDOM|SRANDOM|SECONDS) continue ;;
            EPOCH*|SHLVL|_|PWD|OLDPWD) continue ;;
        esac
        builtin printf '%s\\n' "$_siralo_decl"
    done < <(builtin declare -p)
    while IFS= builtin read -r _siralo_name; do
        case "$_siralo_name" in _siralo_*) continue ;; esac
        builtin declare -f "$_siralo_name"
    done < <(builtin compgen -A function)
}}
builtin trap {trap_command} EXIT
builtin source {command_path}
"""

    def _prune_completed_groups(self) -> None:
        live = []
        for proc in self._groups:
            try:
                if sys.platform != "win32":
                    os.killpg(proc.pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            live.append(proc)
        self._groups = live
        for job in self._jobs[:]:
            if not job.active():
                job.close()
                self._jobs.remove(job)

    async def run(self, cmd: str, timeout_s: int, *, output_cap: int) -> ExecResult:
        async with self._lock:
            if self._closed:
                return ExecResult(stdout="", stderr="", exit_code=-1, timed_out=False)
            secrets = tuple(os.environ[name] for name in _CREDENTIAL_NAMES if os.environ.get(name))
            matcher, max_span = _secret_matcher(extra_secrets=secrets)
            # Keep only a possible credential suffix plus the bounded, already
            # redacted head. Redacting after a raw-byte cap would leak partial
            # credentials when repeated replacements shrink the retained prefix.
            lookahead = max(0, max_span - 1)
            captured: list[str] = []
            captured_length = 0
            pending = ""
            discarded = False
            proc: asyncio.subprocess.Process | None = None
            job: WindowsJob | None = None

            def emit(text: str) -> None:
                nonlocal captured_length, discarded
                clean = redact(text, extra_secrets=secrets, redact_bearer=False)
                remaining = output_cap - captured_length
                head = clean[:remaining]
                if head:
                    captured.append(head)
                    captured_length += len(head)
                discarded |= len(clean) > remaining

            def consume(text: str, *, final: bool = False) -> None:
                nonlocal pending, discarded
                if captured_length >= output_cap:
                    discarded |= bool(text or pending)
                    pending = ""
                    return
                pending += text
                while pending:
                    safe_length = len(pending) if final else max(0, len(pending) - lookahead)
                    if not safe_length:
                        break
                    # Consume a complete earliest credential if it crosses the
                    # safe prefix boundary; never feed half a known key to redact.
                    match = matcher.search(pending) if matcher is not None else None
                    boundary = (
                        match.end()
                        if match is not None and match.start() < safe_length else safe_length
                    )
                    emit(pending[:boundary])
                    pending = pending[boundary:]
                    if captured_length >= output_cap:
                        discarded |= bool(pending)
                        pending = ""
                        break

            async def drain() -> None:
                assert proc is not None and proc.stdout is not None
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                while chunk := await proc.stdout.read(4096):
                    consume(decoder.decode(chunk))
                consume(decoder.decode(b"", final=True), final=True)

            async def finish() -> int:
                assert proc is not None
                await drain()
                return await proc.wait()

            collector: asyncio.Task[int] | None = None
            spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
            command_fd: int | None = None
            try:
                # Reapply in the actual spawning thread: capability and
                # no_new_privs state are thread-scoped on Linux.
                protect_controller()
                command, command_fd = self._prepare_command(cmd)
                spawn_kwargs = dict(self._spawn_kwargs)
                if not IS_WINDOWS:
                    spawn_kwargs["pass_fds"] = (command_fd,)
                if IS_WINDOWS:
                    job = WindowsJob()
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    *self._argv, "-c", self._script(command),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=self._cwd, env=self._env, limit=8192,
                    **spawn_kwargs,
                ))
                # Cancellation during process creation must still acquire and
                # tear down the process, rather than orphan it before assignment.
                proc = await asyncio.shield(spawn)
                if not IS_WINDOWS:
                    self._groups.append(proc)
                if job is not None:
                    job.assign(proc.pid)
                    self._jobs.append(job)
                    job.resume(proc.pid)
                collector = asyncio.create_task(finish())
                code = await asyncio.wait_for(asyncio.shield(collector), timeout_s)
            except TimeoutError:
                await self._discard(proc, job, collector)
                return ExecResult(stdout="", stderr="", exit_code=-1, timed_out=True)
            except asyncio.CancelledError:
                if proc is None and spawn is not None:
                    try:
                        proc = await spawn
                    except Exception:
                        pass
                await self._discard(proc, job, collector)
                raise
            except Exception as error:
                await self._discard(proc, job, collector)
                return ExecResult(
                    stdout="",
                    stderr=redact(str(error), extra_secrets=secrets, redact_bearer=False),
                    exit_code=-1,
                    timed_out=False,
                )
            finally:
                if command_fd is not None:
                    os.close(command_fd)
            self._prune_completed_groups()
            return ExecResult(
                stdout="".join(captured), stderr="", exit_code=code,
                timed_out=False, truncated=discarded,
            )

    async def _discard(
        self,
        proc: asyncio.subprocess.Process | None,
        job: WindowsJob | None,
        collector: asyncio.Task[int] | None,
    ) -> None:
        # Invalidate by switching paths, not by trusting or modifying a possibly
        # attacker-replaced snapshot. Cleanup must never depend on its contents.
        self._generation += 1
        self._state = self._directory / f"state-{self._generation}.bash"
        if job is not None:
            job.close()
            if job in self._jobs:
                self._jobs.remove(job)
        if proc is not None:
            await self._kill(proc)
            if proc in self._groups:
                self._groups.remove(proc)
            if collector is None:
                async def reap() -> int:
                    assert proc.stdout is not None
                    while await proc.stdout.read(4096):
                        pass
                    return await proc.wait()
                collector = asyncio.create_task(reap())
        if collector is not None:
            try:
                await asyncio.wait_for(asyncio.shield(collector), _REAP_TIMEOUT_S)
            except (Exception, asyncio.CancelledError):
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), _REAP_TIMEOUT_S)
            except TimeoutError:
                pass

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            for job in self._jobs:
                job.close()
            self._jobs.clear()
            for proc in self._groups:
                await self._kill(proc)
                try:
                    await asyncio.wait_for(proc.wait(), _REAP_TIMEOUT_S)
                except TimeoutError:
                    pass
            self._groups.clear()
            if self._directory_lock is not None:
                self._directory_lock.close()
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None
            self._storage.cleanup()
