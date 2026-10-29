"""私有状态目录：安全打开、有限锁、原子写、按会话/actor 隔离。

状态是 Hook 的缓存，不是账本或授权库。JSON 状态不存 prompt、命令或补丁；
自动快照 `baselines/` 仅存业务原内容，私有 0600，不对外发送。
任何安全/一致性问题都抛 StateUnavailable，由核心告警放行（本次已确定的安全
拒绝不因状态故障解除）。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import time
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):
    # 独立 Plugin 目录：按同目录模块导入，不依赖仓库目录名或安装位置。
    import paths  # type: ignore[no-redef]
else:  # pragma: no cover - 包内导入
    from . import paths  # type: ignore[no-redef]

try:  # POSIX only; Windows 无已验实现时降级为不持久化。
    import fcntl
except ImportError:  # pragma: no cover - 平台分支
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
MAX_JSON_BYTES = 512 * 1024
LOCK_WAIT_SECONDS = 0.05


class StateUnavailable(Exception):
    """状态不可安全使用：不修复、不删除，直接降级。"""


def _sha(parts: List[str]) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def identity_key(*parts: str) -> str:
    return _sha([part if part is not None else "" for part in parts])


def state_root_for(repo_root: Optional[str], override: Optional[str]) -> str:
    if override:
        return os.path.abspath(override)
    base = os.path.realpath(tempfile.gettempdir())
    uid = os.getuid() if hasattr(os, "getuid") else 0
    name = "easydev-hooks-%s" % uid
    if repo_root:
        return os.path.join(base, name, "v1", repo_id_of(repo_root))
    return os.path.join(base, name, "v1", "unbound")


def repo_id_of(repo_root: str) -> str:
    return hashlib.sha256(os.fsencode(os.path.realpath(repo_root))).hexdigest()[:12]


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


PRIVATE_PREFIX = "easydev-hooks-"
normalize_path = paths.normalize_path


def _components(path: str) -> List[str]:
    normalized = normalize_path(path)
    return [part for part in normalized.split(os.sep) if part]


def _private_marker_index(components: List[str]) -> int:
    for index, part in enumerate(components):
        if part.startswith(PRIVATE_PREFIX):
            return index
    # 显式状态根没有该前缀时，只核对最终目录。
    return max(len(components) - 1, 0)


def _open_dir_at(parent_fd: int, name: str, create: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise StateUnavailable("cannot create private directory: %s" % error.errno)
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise StateUnavailable("cannot open private directory: %s" % error.errno)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise StateUnavailable("private path is not a real directory")
        raise StateUnavailable("cannot open private directory: %s" % error.errno)


def _open_chain(path: str, create: bool) -> int:
    """从文件系统根逐层 `openat(O_DIRECTORY|O_NOFOLLOW)`，绝不穿过软链。

    返回持有最终目录的 fd；调用方负责关闭。缺失层在 `create=False` 时抛
    `FileNotFoundError`（调用方按“没有状态”处理），软链/非目录抛
    `StateUnavailable`（调用方按不可用降级）。
    """
    components = _components(path)
    verify_from = _private_marker_index(components)
    fd = os.open(os.sep, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for index, part in enumerate(components):
            child_fd = _open_dir_at(fd, part, create)
            os.close(fd)
            fd = child_fd
            if index >= verify_from:
                _verify_dir(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _verify_dir(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise StateUnavailable("private path is not a directory")
    if info.st_uid != _uid():
        raise StateUnavailable("private directory has unexpected owner")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise StateUnavailable("private directory is not 0700")


def ensure_private_root(path: str) -> str:
    """创建缺失层级，并对私有根及其下层核对 owner=本 UID、非软链、0700。

    系统临时根与用户显式给出的上层目录（可能 0755/1777）不强制 0700；从
    `easydev-hooks-*` 起的每一层才是本插件必须自证安全的私有链。显式
    `--state-root` 没有该前缀时只核对最终目录。

    只规范化已核实的系统临时根别名；其余层级逐层 `dir_fd + O_NOFOLLOW`
    打开并 fstat，祖先软链一律拒绝（不 realpath、不修复、不删除）。
    """
    fd = _open_chain(path, create=True)
    os.close(fd)
    return normalize_path(path)


def _read_json_file(fd: int) -> Dict[str, Any]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise StateUnavailable("state file is not regular")
    if info.st_uid != _uid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise StateUnavailable("state file has unsafe metadata")
    if info.st_nlink != 1:
        raise StateUnavailable("state file is hard-linked")
    if info.st_size > MAX_JSON_BYTES:
        raise StateUnavailable("state file too large")
    # 已 fstat 确认普通文件后才读取；大小已在上限内，读取不会无限长。
    with os.fdopen(os.dup(fd), "rb") as handle:
        payload = handle.read(MAX_JSON_BYTES + 1)
    if len(payload) > MAX_JSON_BYTES:
        raise StateUnavailable("state file too large")
    if not payload:
        raise StateUnavailable("state file is empty")
    # 解码/结构错误同样是状态故障：不能让 JSONDecodeError/UnicodeDecodeError
    # 越过调用方的 StateUnavailable 分支，把已确定的安全拒绝降级成放行。
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, UnicodeError, ValueError, RecursionError):
        raise StateUnavailable("state file is not valid JSON")
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise StateUnavailable("state schema mismatch")
    return value


def _write_json_file(directory: str, name: str, value: Dict[str, Any]) -> None:
    dir_fd = _open_chain(directory, create=True)
    temporary = ".tmp-%s-%d" % (os.getpid(), time.monotonic_ns())
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(temporary, flags, 0o600, dir_fd=dir_fd)
        try:
            os.write(file_fd, payload)
            os.fsync(file_fd)
            os.fchmod(file_fd, 0o600)
        finally:
            os.close(file_fd)
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    except OSError as error:
        raise StateUnavailable("cannot write state: %s" % error.errno)
    finally:
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except OSError:
            pass
        os.close(dir_fd)


def read_state(directory: str, name: str) -> Optional[Dict[str, Any]]:
    try:
        dir_fd = _open_chain(directory, create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            file_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=dir_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise StateUnavailable("cannot open state file: %s" % error.errno)
        try:
            return _read_json_file(file_fd)
        finally:
            os.close(file_fd)
    finally:
        os.close(dir_fd)


def write_state(directory: str, name: str, value: Dict[str, Any]) -> None:
    value = dict(value)
    value["schema_version"] = SCHEMA_VERSION
    ensure_private_root(directory)
    _write_json_file(directory, name, value)


def _snapshot_problem(dir_fd: int, name: str) -> Optional[Tuple[bool, str]]:
    """核对已存在的快照文件；不存在返回 None。"""
    try:
        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=dir_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            return False, "已有快照是符号链接"
        return False, "已有快照不可读"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return False, "已有快照不是普通文件"
        if info.st_uid != _uid():
            return False, "已有快照所有者不是当前用户"
        if stat.S_IMODE(info.st_mode) != 0o600:
            return False, "已有快照权限不是 0600"
        if info.st_nlink != 1:
            return False, "已有快照存在硬链"
        return True, "已有首次快照"
    finally:
        os.close(fd)


def save_snapshot(
    directory: str, relative: str, source: str, max_bytes: int
) -> Tuple[bool, str]:
    """把源文件的当前内容保存为首次快照（0600、非软链、不覆盖已有快照）。

    这是私有缓存，不是业务备份：只写入 `directory` 下按仓库相对路径镜像的文件，
    目录 0700、文件 0600，逐层 `dir_fd + O_NOFOLLOW`。并发写入时只有第一个创建
    成功（`os.link` 原子且不覆盖），其余读到已有快照。失败一律返回原因，由调用
    方拒绝本次写入并给出可操作提示。
    """
    parts = [part for part in relative.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return False, "相对路径不安全"
    try:
        dir_fd = _open_chain(os.path.join(directory, *parts[:-1]) if len(parts) > 1 else directory, create=True)
    except StateUnavailable as error:
        return False, "私有状态根不可安全使用（%s）" % error
    except OSError as error:
        return False, "私有目录不可用（%s）" % error.errno
    name = parts[-1]
    try:
        existing = _snapshot_problem(dir_fd, name)
        if existing is not None:
            return existing
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            source_fd = os.open(source, flags)
        except FileNotFoundError:
            return False, "源文件不存在"
        except OSError as error:
            if error.errno == errno.ELOOP:
                return False, "源文件是符号链接"
            return False, "源文件不可读"
        try:
            info = os.fstat(source_fd)
        except OSError:
            os.close(source_fd)
            return False, "源文件不可读"
        temporary = ".snap-%d-%d" % (os.getpid(), time.monotonic_ns())
        file_fd: Optional[int] = None
        copied = False
        try:
            # source/temporary 两个 FD 的生命周期要覆盖所有返回与异常路径，
            # 包括“非普通文件/超限/写不出去”的拒绝分支。
            if not stat.S_ISREG(info.st_mode):
                return False, "源文件不是普通文件"
            if info.st_size > max_bytes:
                return False, "源文件超过 %d 字节上限" % max_bytes
            try:
                file_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=dir_fd,
                )
            except OSError as error:
                return False, "快照临时文件不可写（%s）" % error.errno
            written = 0
            while True:
                chunk = os.read(source_fd, 65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    # 指纹之后源文件变大：已写出的临时文件不能发布。
                    return False, "源文件超过 %d 字节上限" % max_bytes
                _write_all(file_fd, chunk)
            os.fsync(file_fd)
            os.fchmod(file_fd, 0o600)
            copied = True
        except OSError as error:
            return False, "快照写入失败（%s）" % error.errno
        finally:
            for opened in (source_fd, file_fd):
                if opened is None:
                    continue
                try:
                    os.close(opened)
                except OSError:
                    pass
            if not copied:
                try:
                    os.unlink(temporary, dir_fd=dir_fd)
                except OSError:
                    pass
        created = True
        try:
            # link 不覆盖：并发进程里只有第一个成为首次快照。
            os.link(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except FileExistsError:
            created = False
        except OSError as error:
            return False, "快照不可发布（%s）" % error.errno
        finally:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except OSError:
                pass
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        checked = _snapshot_problem(dir_fd, name)
        if checked is None:
            return False, "快照写入后不可核对"
        if checked[0] and created:
            return True, "已保存首次快照"
        return checked
    finally:
        os.close(dir_fd)


def _write_all(fd: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])


class Locks(object):
    """非阻塞 flock；全部锁累计最多等待 50ms，按名字排序获取。"""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self._fds: List[int] = []

    def __enter__(self) -> "Locks":
        return self

    def acquire(self, names: List[str]) -> None:
        if fcntl is None:
            raise StateUnavailable("file locking unavailable on this platform")
        ensure_private_root(self.directory)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        try:
            for name in sorted(set(names)):
                dir_fd = _open_chain(self.directory, create=True)
                try:
                    fd = os.open(
                        name + ".lock",
                        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=dir_fd,
                    )
                except OSError as error:
                    raise StateUnavailable("cannot open lock: %s" % error.errno)
                finally:
                    os.close(dir_fd)
                # 打开成功即归本对象所有：后面任一把锁失败都要统一释放。
                self._fds.append(fd)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise StateUnavailable("state lock busy")
                        time.sleep(0.005)
        except BaseException:
            # 失败不再把已获锁留给下一次 acquire（长驻进程/进程内测试会持续 busy）。
            self._release()
            raise

    def _release(self) -> Optional[BaseException]:
        """逆序解锁并关闭全部受管 fd；返回首个清理错误，但不中断其余释放。"""
        first_error: Optional[BaseException] = None
        for fd in reversed(self._fds):
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except BaseException as error:
                if first_error is None:
                    first_error = error
            try:
                os.close(fd)
            except OSError as error:
                if first_error is None:
                    first_error = error
        self._fds = []
        return first_error

    def __exit__(self, exc_type, exc, traceback) -> None:
        error = self._release()
        # 已有 body 异常时优先保留它，没有才把清理错误交给调用方。
        if error is not None and exc_type is None:
            raise error
