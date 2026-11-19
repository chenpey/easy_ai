"""Wait for and stop agent process trees without relying on external tools."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple


ProcessIdentity = Tuple[int, ...]

# 子进程只继承显式列出的宿主变量：其余（云厂商凭据、PYTHONOPTIMIZE、PYTEST_ADDOPTS、
# CDPATH、RIPGREP_CONFIG_PATH、GIT_* 等）一律不进入被测 Agent、不可信 pytest 与验收脚本。
BASE_ENV_ALLOWLIST: Tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "SYSTEMROOT",  # Windows 子进程所需
    "COMSPEC",  # Windows 子进程所需
    "WINDIR",  # Windows 子进程所需
)

# 被测 codex Agent 额外需要的模型鉴权、网络代理与离线替身控制通道。
AGENT_EXTRA_ENV_ALLOWLIST: Tuple[str, ...] = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT",
    "CODEX_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "EASYDEV_STUB_PLAN",
)

# 离线 pytest 替身的控制通道；不可信测试子进程拿不到 API key 或代理。
PYTEST_STUB_ENV_ALLOWLIST: Tuple[str, ...] = (
    "EASYDEV_PYTEST_PLAN",
    "EASYDEV_PYTEST_TRAILER",
    "EASYDEV_PYTEST_RETURNCODE",
)


def filtered_env(
    *,
    allow_keys: Iterable[str] = (),
    overrides: Optional[Dict[str, Optional[str]]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """按最小白名单从 source（默认 os.environ）构造子进程环境变量；overrides 中键值为 None 的项被剔除。"""
    environment = os.environ if source is None else source
    env = {
        key: environment[key]
        for key in (*BASE_ENV_ALLOWLIST, *allow_keys)
        if environment.get(key) is not None
    }
    for key, value in (overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _linux_snapshot() -> Dict[int, Tuple[int, ProcessIdentity]]:
    snapshot: Dict[int, Tuple[int, ProcessIdentity]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="utf-8").rsplit(
                ") ", 1
            )[1].split()
            snapshot[int(entry.name)] = (int(fields[1]), (int(fields[19]),))
        except (IndexError, OSError, ValueError):
            continue
    return snapshot


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@lru_cache(maxsize=1)
def _darwin_library() -> ctypes.CDLL:
    library = ctypes.CDLL("libproc.dylib")
    library.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.proc_listallpids.restype = ctypes.c_int
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    return library


def _darwin_snapshot() -> Dict[int, Tuple[int, ProcessIdentity]]:
    library = _darwin_library()
    count = library.proc_listallpids(None, 0)
    if count <= 0:
        raise RuntimeError("cannot enumerate processes with libproc")
    pids = (ctypes.c_int * count)()
    actual = library.proc_listallpids(pids, ctypes.sizeof(pids))
    if actual <= 0:
        raise RuntimeError("cannot enumerate processes with libproc")

    snapshot: Dict[int, Tuple[int, ProcessIdentity]] = {}
    for pid in pids[:actual]:
        info = _DarwinProcBsdInfo()
        size = library.proc_pidinfo(
            pid,
            3,  # PROC_PIDTBSDINFO
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if size <= 0:
            continue
        snapshot[int(info.pbi_pid)] = (
            int(info.pbi_ppid),
            (int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)),
        )
    return snapshot


def _process_snapshot() -> Dict[int, Tuple[int, ProcessIdentity]]:
    if os.name != "posix":
        return {}
    if Path("/proc/self/stat").exists():
        return _linux_snapshot()
    if os.uname().sysname == "Darwin":
        return _darwin_snapshot()
    raise RuntimeError("cannot enumerate processes on this POSIX host")


def _descendants(
    root_pids: Set[int],
    snapshot: Dict[int, Tuple[int, ProcessIdentity]],
) -> Dict[int, ProcessIdentity]:
    children: Dict[int, Set[int]] = {}
    for pid, (ppid, _) in snapshot.items():
        children.setdefault(ppid, set()).add(pid)
    found: Dict[int, ProcessIdentity] = {}
    pending = list(root_pids)
    while pending:
        parent = pending.pop()
        for child in children.get(parent, set()):
            if child in found:
                continue
            found[child] = snapshot[child][1]
            pending.append(child)
    return found


def wait_for_process(
    process: subprocess.Popen,
    timeout: float,
    *,
    poll_interval: float = 0.25,
    tracked: Optional[Dict[int, ProcessIdentity]] = None,
) -> Tuple[Optional[int], Dict[int, ProcessIdentity]]:
    """Wait while retaining identities for descendants that later daemonize.

    `tracked` 由调用方传入时原地更新：等待期间被中断（KeyboardInterrupt）时，
    调用方仍能拿到已观测到的后代，不会在清理阶段丢掉已经 `setsid()` 的孙进程。
    """
    deadline = time.monotonic() + timeout
    tracked = {} if tracked is None else tracked
    while True:
        try:
            snapshot = _process_snapshot()
            root = snapshot.get(process.pid)
            if root is not None:
                tracked.setdefault(process.pid, root[1])
            for pid, identity in _descendants({process.pid}, snapshot).items():
                tracked.setdefault(pid, identity)
        except (OSError, RuntimeError):
            pass
        returncode = process.poll()
        if returncode is not None:
            # 刚观察到 leader 退出：此刻同组进程仍属本任务，PGID 尚未被复用，
            # 趁现在收编，避免 terminate_process 阶段因无存活后代而无法证明组归属。
            try:
                final = _process_snapshot()
                for pid, (_, identity) in final.items():
                    try:
                        if pid != process.pid and os.getpgid(pid) == process.pid:
                            tracked.setdefault(pid, identity)
                    except (ProcessLookupError, PermissionError):
                        continue
            except (OSError, RuntimeError):
                pass
            return returncode, tracked
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, tracked
        time.sleep(min(poll_interval, remaining))


def _identity_alive(
    pid: int,
    identity: ProcessIdentity,
    snapshot: Dict[int, Tuple[int, ProcessIdentity]],
) -> bool:
    return snapshot.get(pid, (0, ()))[1] == identity


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        raise RuntimeError(f"cannot signal process {pid}") from error


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        raise RuntimeError(f"cannot signal process group {pgid}") from error


def _signal_known(
    known: Dict[int, ProcessIdentity],
    sig: int,
    snapshot: Optional[Dict[int, Tuple[int, ProcessIdentity]]],
) -> List[str]:
    """只对最近一次成功快照里仍存活的已知 PID 发信号。

    快照不可用时 `snapshot is None`：不向 tracked 的历史 PID 盲发信号（PID 复用
    后可能误杀无关进程），只清理可证明组归属的 leader/组。单个失败不中断其余
    PID，失败信息由调用方汇总。
    """
    if snapshot is None:
        return []
    failures: List[str] = []
    for pid, identity in known.items():
        if not _identity_alive(pid, identity, snapshot):
            continue
        try:
            _signal_pid(pid, sig)
        except RuntimeError as error:
            failures.append(str(error))
    return failures


def _reap_leader(process: subprocess.Popen, timeout: float) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def terminate_process(
    process: subprocess.Popen,
    *,
    grace_seconds: float = 5.0,
    tracked: Optional[Dict[int, ProcessIdentity]] = None,
) -> None:
    """Terminate the direct child, its process group, and known descendants."""
    known = dict(tracked or {})
    root_identity = known.pop(process.pid, None)
    if os.name != "posix":
        if process.poll() is None:
            process.terminate()
            if not _reap_leader(process, grace_seconds):
                process.kill()
                if not _reap_leader(process, grace_seconds):
                    raise RuntimeError(
                        f"process {process.pid} did not terminate after signals"
                    )
        return

    try:
        snapshot = _process_snapshot()
    except (OSError, RuntimeError):
        snapshot = {}
    actual = snapshot.get(process.pid)
    if root_identity is None and process.poll() is None and actual is not None:
        root_identity = actual[1]
    root_alive = bool(root_identity) and _identity_alive(process.pid, root_identity, snapshot)
    if not snapshot:
        root_alive = process.poll() is None
    # 组信号只在能证明该 PGID 仍属本任务时发出：等待期间观测到的后代仍存活**且仍在原组**，
    # 说明组里还有成员，内核不会把这个 PID 回收给别的会话；后代已 setsid 离开原组时不算证据。
    tracked_alive = False
    for pid, identity in known.items():
        if not _identity_alive(pid, identity, snapshot):
            continue
        try:
            if os.getpgid(pid) == process.pid:
                tracked_alive = True
                break
        except (ProcessLookupError, PermissionError):
            continue
    # 收编同组进程需先证明该 PGID 仍属本任务：leader 存活，或有已跟踪后代仍存活在原组。
    # leader 已退出且无存活已跟踪后代时，PGID 可能已被复用给无关进程组，收编会误杀宿主进程。
    owns_group = root_alive or tracked_alive
    if owns_group:
        for pid, (_, identity) in snapshot.items():
            try:
                if pid != process.pid and os.getpgid(pid) == process.pid:
                    known.setdefault(pid, identity)
            except (ProcessLookupError, PermissionError):
                pass
        if root_alive:
            for pid, identity in _descendants({process.pid}, snapshot).items():
                known.setdefault(pid, identity)
        _signal_group(process.pid, signal.SIGTERM)
    if root_alive:
        _signal_pid(process.pid, signal.SIGTERM)
    signal_failures = _signal_known(known, signal.SIGTERM, snapshot or None)

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        _reap_leader(process, 0.05)
        try:
            snapshot = _process_snapshot()
            for pid, identity in _descendants(
                {pid for pid, identity in known.items()
                 if _identity_alive(pid, identity, snapshot)}
                | ({process.pid} if root_identity and _identity_alive(process.pid, root_identity, snapshot) else set()),
                snapshot,
            ).items():
                known.setdefault(pid, identity)
        except (OSError, RuntimeError):
            snapshot = {}
        alive = {
            pid
            for pid, identity in known.items()
            if _identity_alive(pid, identity, snapshot)
        }
        # 快照不可用时不能把“看不到”当成“已退出”：继续等到截止，交给 SIGKILL 分支处理。
        if snapshot and process.poll() is not None and not alive:
            if signal_failures:
                raise RuntimeError(
                    "signal errors: " + ", ".join(sorted(set(signal_failures)))
                )
            return
        time.sleep(0.05)

    try:
        snapshot = _process_snapshot()
    except (OSError, RuntimeError):
        snapshot = {}
    root_alive_now = bool(
        snapshot and root_identity and _identity_alive(process.pid, root_identity, snapshot)
    ) or (not snapshot and process.poll() is None)
    tracked_alive_now = False
    if snapshot:
        for pid, identity in known.items():
            if not _identity_alive(pid, identity, snapshot):
                continue
            try:
                if os.getpgid(pid) == process.pid:
                    tracked_alive_now = True
                    break
            except (ProcessLookupError, PermissionError):
                continue
    if root_alive_now or tracked_alive_now:
        # leader 存活或同组已跟踪后代存活时，PGID 仍属本任务，才发组信号。
        _signal_group(process.pid, signal.SIGKILL)
    if root_alive_now:
        _signal_pid(process.pid, signal.SIGKILL)
    signal_failures.extend(_signal_known(known, signal.SIGKILL, snapshot or None))
    if not _reap_leader(process, max(grace_seconds, 0.05)):
        raise RuntimeError(
            f"process {process.pid} did not terminate after signals"
        )

    try:
        snapshot = _process_snapshot()
    except (OSError, RuntimeError):
        # 无法验证清理结果时不能假报成功。
        raise RuntimeError("cannot verify process cleanup: snapshot unavailable")
    alive: List[int] = [
        pid
        for pid, identity in known.items()
        if _identity_alive(pid, identity, snapshot)
    ]
    kill_deadline = time.monotonic() + max(grace_seconds, 0.05)
    while alive and time.monotonic() < kill_deadline:
        try:
            snapshot = _process_snapshot()
        except (OSError, RuntimeError):
            raise RuntimeError("cannot verify process cleanup: snapshot unavailable")
        alive = [
            pid
            for pid, identity in known.items()
            if _identity_alive(pid, identity, snapshot)
        ]
        if not alive:
            break
        time.sleep(0.05)
    if signal_failures:
        detail = "signal errors: " + ", ".join(sorted(set(signal_failures)))
        if alive:
            detail += f"; descendant processes survived SIGKILL: {sorted(alive)}"
        raise RuntimeError(detail)
    if alive:
        raise RuntimeError(f"descendant processes survived SIGKILL: {sorted(alive)}")


def run_captured(command, *, cwd, env, timeout):
    """Capture an untrusted check in files and always clean its owned process tree."""
    import tempfile
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        # 不用 `with Popen`：其 __exit__ 会无超时 wait()，进程树清理失败时会把超时保护变成永久挂起。
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=os.name == "posix",
        )
        tracked = {}
        try:
            code, _ = wait_for_process(process, timeout, tracked=tracked)
            timed_out = code is None
        finally:
            terminate_process(process, tracked=tracked)
        code = process.wait(timeout=10)
        stdout.seek(0)
        stderr.seek(0)
        return {"returncode": -1 if timed_out else code,
                "timed_out": timed_out,
                "stdout": stdout.read().decode("utf-8", errors="replace"),
                "stderr": stderr.read().decode("utf-8", errors="replace")}
