"""仓库根、路径/patch 提取与基线只读核对。"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

GIT_TIMEOUT_SECONDS = 0.3
GIT_TIMEOUT_MS = int(GIT_TIMEOUT_SECONDS * 1000)
# 指纹只读普通文件且有界；FIFO/设备/目录、超大文件与截止超时都按降级处理。
FINGERPRINT_MAX_BYTES = 8 * 1024 * 1024
FINGERPRINT_PROBLEMS = ("symlink", "nonregular", "toolarge", "unreadable", "deadline")


def _system_temp_aliases() -> Dict[str, str]:
    """已核实的系统临时根别名（如 macOS `/tmp` → `/private/tmp`）。

    只登记“临时根本身”的别名，不做任意路径 realpath：私有 suffix 与业务路径
    里出现的软链必须按无跟随规则判定，而不是先消解再当普通路径。
    """
    aliases: Dict[str, str] = {}
    for candidate in (tempfile.gettempdir(), os.environ.get("TMPDIR", ""), "/tmp"):
        if not candidate:
            continue
        absolute = os.path.abspath(candidate)
        real = os.path.realpath(absolute)
        if real != absolute:
            aliases[absolute] = real
    return aliases


def normalize_path(path: str) -> str:
    """绝对化路径，且只规范化已核实的系统临时根前缀一次。"""
    absolute = os.path.abspath(path)
    for raw, real in sorted(_system_temp_aliases().items(), key=lambda item: -len(item[0])):
        if absolute == raw:
            return real
        if absolute.startswith(raw + os.sep):
            return real + absolute[len(raw) :]
    return absolute


def remaining_ms(deadline: Optional[float]) -> Optional[int]:
    if deadline is None:
        return None
    return max(int((deadline - time.monotonic()) * 1000), 0)


class GitUnavailable(Exception):
    pass


def git_binary() -> str:
    found = shutil.which("git")
    if not found:
        raise GitUnavailable("git executable not found")
    return os.path.realpath(found)


def _git_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_SYSTEM": os.devnull,
            "GIT_ATTR_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    for key in list(env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    env.pop("GIT_CONFIG_COUNT", None)
    env.pop("GIT_CONFIG_PARAMETERS", None)
    return env


def run_git(
    cwd: str,
    args: List[str],
    timeout_ms: int = GIT_TIMEOUT_MS,
    deadline: Optional[float] = None,
) -> Optional[Tuple[int, bytes, bytes]]:
    """只读查询：argv 传参、禁用 hooks/fsmonitor/全局配置、不拼 shell。

    `deadline` 是本次 Hook 调用的共享内部截止；单次 Git 不超过 300ms，
    剩余预算更小时以剩余预算为准。
    """
    budget = max(timeout_ms, 1)
    left = remaining_ms(deadline)
    if left is not None:
        if left <= 0:
            return None
        budget = min(budget, left)
    try:
        binary = git_binary()
        completed = subprocess.run(
            [binary, "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "--no-pager"]
            + args,
            cwd=cwd,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=budget / 1000.0,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except (OSError, subprocess.SubprocessError, GitUnavailable):
        return None


def repo_root(cwd: str, deadline: Optional[float] = None) -> Optional[str]:
    if not cwd or not os.path.isdir(cwd):
        return None
    result = run_git(cwd, ["rev-parse", "--show-toplevel"], deadline=deadline)
    if result is None or result[0] != 0:
        return None
    path = result[1].decode("utf-8", "replace").strip()
    return os.path.realpath(path) if path else None


def repo_id(root: str) -> str:
    return hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()[:12]


def cheap_repo_root(cwd: str) -> Optional[str]:
    """不启动 Git 子进程的仓库根：从 cwd 逐级向上找 `.git` 目录或文件。

    用于把未激活短路定位到与完整判定相同的状态分区（状态根按仓库分目录）。
    不需要判断 `.git` 是否可解析：找不到时返回规范化 cwd，让调用方得到一个稳定
    的分区键；即使它与 `repo_root()` 的结论不同，结果也只是没命中状态、退回完整
    判定，不会拿别的分区当依据。
    """
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        return None
    start = os.path.realpath(normalize_path(os.path.abspath(os.path.expanduser(cwd))))
    current = start
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return os.path.realpath(current)
        parent = os.path.dirname(current)
        if not parent or parent == current:
            return start
        current = parent


def has_head(root: str, deadline: Optional[float] = None) -> bool:
    result = run_git(root, ["rev-parse", "--verify", "-q", "HEAD"], deadline=deadline)
    return result is not None and result[0] == 0


def canonical_rel(root: str, target: str, cwd: Optional[str] = None) -> Optional[str]:
    """把目标解析为仓库内规范相对路径；越界/绝对混杂返回 None。"""
    if not isinstance(target, str) or not target or "\x00" in target:
        return None
    base = cwd or root
    expanded = os.path.expanduser(target)
    if os.path.isabs(expanded):
        absolute = os.path.normpath(expanded)
    else:
        absolute = os.path.normpath(os.path.join(base, expanded))
    root_norm = os.path.normpath(os.path.realpath(root))
    # realpath 同时消解系统临时根别名与仓库路径上的软链；不存在的文件取
    # 已存在父目录的真实位置，仍保持“越界返回 None”的语义。
    absolute_norm = os.path.normpath(os.path.realpath(os.path.abspath(absolute)))
    try:
        inside = os.path.commonpath([root_norm, absolute_norm]) == root_norm
    except ValueError:
        inside = False
    if not inside:
        return None
    relative = os.path.relpath(absolute_norm, root_norm)
    if relative == ".." or relative.startswith("../") or os.path.isabs(relative):
        return None
    return relative.replace(os.sep, "/")


def status_map(root: str, deadline: Optional[float] = None) -> Optional[Dict[str, Dict[str, bool]]]:
    """porcelain v1 -z；返回 path -> {index, worktree, untracked}。"""
    result = run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], deadline=deadline)
    if result is None or result[0] != 0:
        return None
    entries: Dict[str, Dict[str, bool]] = {}
    payload = result[1].decode("utf-8", "surrogateescape")
    tokens = payload.split("\x00")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        code = token[:2]
        # `git status --porcelain=v1 -z` 的 rename/copy 记录：首路径是目的路径，
        # 紧随其后的 NUL 字段是来源路径。两者都要保留状态。
        destination = token[3:]
        source = None
        if code[0] in {"R", "C"} and index < len(tokens):
            source = tokens[index]
            index += 1
        entry = {
            "index": code[0] not in {" ", "?", "!"},
            "worktree": code[1] not in {" ", "?", "!"},
            "untracked": code == "??",
        }
        if source is not None:
            entry["renamed_from"] = source
        entries[destination] = entry
        if source is not None and source not in entries:
            # 来源路径已随 rename/copy 从索引移除；不能当作干净文件。
            entries[source] = {
                "index": True,
                "worktree": False,
                "untracked": False,
                "renamed_to": destination,
            }
    return entries


def is_ignored(root: str, relative: str, deadline: Optional[float] = None) -> bool:
    result = run_git(root, ["check-ignore", "-q", "--", relative], deadline=deadline)
    return result is not None and result[0] == 0


def classify(root: str, relative: str, deadline: Optional[float] = None) -> str:
    """首次写前分类；Git 不可用抛出 GitUnavailable，由调用方降级。"""
    absolute = os.path.join(root, relative)
    if not os.path.lexists(absolute):
        return "absent"
    if not has_head(root, deadline):
        return "unreconstructible"
    entries = status_map(root, deadline)
    if entries is None:
        raise GitUnavailable("git status failed")
    entry = entries.get(relative)
    if entry is None:
        if is_ignored(root, relative, deadline):
            return "unreconstructible"
        return "clean_tracked"
    if entry.get("untracked"):
        return "untracked"
    if entry.get("index") or entry.get("worktree"):
        return "dirty"
    return "clean_tracked"


def fingerprint(path: str, deadline: Optional[float] = None) -> str:
    """只读普通文件的指纹；非普通文件/超大/超时返回可识别的降级值，不阻塞。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat.S_ISREG(info.st_mode):
        return "nonregular"
    if info.st_size > FINGERPRINT_MAX_BYTES:
        return "toolarge"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    handle = None
    try:
        handle = os.fdopen(os.open(path, flags), "rb")
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            return "nonregular"
        digest = hashlib.sha256()
        read_bytes = 0
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            read_bytes += len(chunk)
            if read_bytes > FINGERPRINT_MAX_BYTES:
                return "toolarge"
            if remaining_ms(deadline) is not None and remaining_ms(deadline) <= 0:
                return "deadline"
            digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return "unreadable"
    finally:
        if handle is not None:
            handle.close()


def _owner_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _check_private_dir(path: str, uid: int) -> Optional[str]:
    try:
        info = os.lstat(path)
    except OSError:
        return "目录不存在"
    if os.path.islink(path):
        return "目录是符号链接"
    if not os.path.isdir(path):
        return "不是真实目录"
    if hasattr(info, "st_uid") and info.st_uid != uid:
        return "目录所有者不是当前用户"
    if hasattr(info, "st_mode") and (info.st_mode & 0o777) != 0o700:
        return "目录权限不是 0700"
    return None


def _fstat_dir_problem(fd: int, uid: int) -> Optional[str]:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        return "不是真实目录"
    if hasattr(info, "st_uid") and info.st_uid != uid:
        return "目录所有者不是当前用户"
    if (info.st_mode & 0o777) != 0o700:
        return "目录权限不是 0700"
    return None


def lexical_rel(root: str, target: str) -> Optional[str]:
    """词法仓库相对路径（不 realpath）；越界返回 None。"""
    root_norm = normalize_path(root)
    target_norm = normalize_path(target)
    try:
        inside = os.path.commonpath([root_norm, target_norm]) == root_norm
    except ValueError:
        inside = False
    if not inside:
        return None
    relative = os.path.relpath(target_norm, root_norm)
    if relative == ".." or relative.startswith("../") or os.path.isabs(relative):
        return None
    return relative.replace(os.sep, "/")


def symlink_component(root: str, absolute: str) -> Optional[str]:
    """返回仓库内目标路径上（含目标自身）的第一个软链组件；不跟随。

    业务文件或其仓库内父链是软链时不能 realpath 后当普通路径：调用方按
    设计拒绝可见且需要基线的写入，或明确降级告警。
    """
    root_norm = normalize_path(root)
    target = normalize_path(absolute)
    try:
        inside = os.path.commonpath([root_norm, target]) == root_norm
    except ValueError:
        inside = False
    if not inside:
        return None
    relative = os.path.relpath(target, root_norm)
    cursor = root_norm
    for part in relative.split(os.sep):
        if part in {"", "."}:
            continue
        cursor = os.path.join(cursor, part)
        try:
            info = os.lstat(cursor)
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return cursor
    return None


def open_secure_directory(path: str) -> Tuple[Optional[int], str]:
    """从已核实的文件系统根逐层 `dir_fd + O_NOFOLLOW` 打开目录。

    返回 (目录 fd, 失败原因)；失败时 fd 为 None。只有逐层打开成功才算
    “祖先链无符号链接”，不 realpath、不修复、不跟随。调用方负责关闭 fd。
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    normalized = normalize_path(path)
    parts = [part for part in normalized.split(os.sep) if part and part != "."]
    fd: Optional[int] = None
    reason = ""
    try:
        fd = os.open(os.sep, flags)
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        result = fd
        fd = None
        return result, ""
    except FileNotFoundError:
        reason = "目录不存在"
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            reason = "路径中有符号链接或非目录组件"
        else:
            reason = "目录不可读"
    finally:
        if fd is not None:
            os.close(fd)
    return None, reason


def secure_task_root(task_root: str) -> Tuple[Optional[str], str]:
    """逐层核验任务根祖先无软链后返回可作归属键的安全路径。

    返回 (安全路径, 失败原因)。安全路径是逐层 O_NOFOLLOW 打开成功的规范化
    词法路径：任何一层是符号链接都不返回路径，未消解词法串不能作为归属证明。
    """
    fd, reason = open_secure_directory(task_root)
    if fd is None:
        return None, reason
    try:
        problem = _fstat_dir_problem(fd, _owner_uid())
    finally:
        os.close(fd)
    if problem:
        return None, "任务根目录%s" % problem
    return normalize_path(task_root), ""


def verify_baseline(task_root: str, root: str, relative: str) -> Tuple[bool, str]:
    """只读核对任务根/baseline/父链/备份文件；不读内容、不修改权限。

    从系统根起逐层 `dir_fd + O_NOFOLLOW` 打开 task_root 祖先与 task_root 本身，
    随后逐层打开 baseline 父链：任一层是软链、权限/owner/硬链不合规都拒绝，
    绝不跟随链接读取或写入。备份最终文件用 nonblock 打开并 fstat 普通文件，
    命名管道等替代品不会阻塞核验。
    """
    del root  # 备份落点只取决于任务根的规范相对路径
    uid = _owner_uid()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    opened: List[int] = []
    try:
        base_fd, reason = open_secure_directory(task_root)
        if base_fd is None:
            return False, "任务根" + reason
        opened.append(base_fd)
        problem = _fstat_dir_problem(base_fd, uid)
        if problem:
            return False, "任务目录%s" % problem
        parts = ["baseline"] + [part for part in relative.split("/") if part]
        fd = base_fd
        for part in parts[:-1]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                return False, "缺少目录 %s" % part
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    return False, "%s 是符号链接或非目录" % part
                return False, "%s 不可读" % part
            opened.append(child)
            problem = _fstat_dir_problem(child, uid)
            if problem:
                return False, "%s %s" % (part, problem)
            fd = child
        try:
            file_fd = os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=fd,
            )
        except FileNotFoundError:
            return False, "缺少首次备份 baseline/%s" % relative
        except OSError as error:
            if error.errno == errno.ELOOP:
                return False, "备份是符号链接"
            return False, "备份不可读"
        opened.append(file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            return False, "备份不是普通文件"
        if hasattr(info, "st_uid") and info.st_uid != uid:
            return False, "备份所有者不是当前用户"
        if (info.st_mode & 0o777) != 0o600:
            return False, "备份权限不是 0600"
        if info.st_nlink != 1:
            return False, "备份存在硬链"
        return True, "已核对首次备份"
    finally:
        for fd in reversed(opened):
            os.close(fd)


def verify_private_root(task_root: str) -> Tuple[bool, str]:
    problem = _check_private_dir(task_root, _owner_uid())
    if problem:
        return False, problem
    return True, "私有任务根合规"


def _patch_begin_targets(text: str) -> Tuple[List[str], bool]:
    targets: List[str] = []
    complete = True
    for line in _diff_lines(text):
        if line.startswith("*** Add File: "):
            targets.append(line[len("*** Add File: ") :].strip())
        elif line.startswith("*** Update File: "):
            targets.append(line[len("*** Update File: ") :].strip())
        elif line.startswith("*** Delete File: "):
            targets.append(line[len("*** Delete File: ") :].strip())
        elif line.startswith("*** Move to: "):
            targets.append(line[len("*** Move to: ") :].strip())
        elif line.startswith("*** ") and line.strip() not in {"*** Begin Patch", "*** End Patch"}:
            complete = False
    return targets, complete


def _unified_header_path(line: str) -> Tuple[Optional[str], bool]:
    """取 `---`/`+++` 文件头里的路径；非文件头返回 (None, True)。

    返回 (路径, 是否可证明)。空头与 Git 的引号路径无法证明真实文件名，
    按不完整处理而不是制造可用伪路径。
    """
    for prefix in ("--- ", "+++ "):
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].split("\t")[0].strip()
        if not value or value.startswith('"'):
            return None, False
        return value, True
    return None, True


def _strip_diff_prefix(path: str) -> str:
    """按 `-p1` 语义只剥离一层 `a/` 或 `b/` 前缀。"""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


# `@@ -n,m +x,y @@`：只按计数消费 hunk 正文，正文里的 `--- …`/`+++ …` 是数据。
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _diff_lines(text: str) -> List[str]:
    """只按补丁自身的 LF 分行（行尾 `\r` 去掉），末行无 LF 时保留。

    `str.splitlines()` 会把 U+2028、垂直制表符、U+0085 等也当换行：hunk 正文
    里出现这些字符就会被当成“多出来的行”，把计数打断成“目标不完整”，进而
    让 dirty 目标的首次快照被跳过。补丁的边界只有 LF（可带 CRLF）。
    """
    lines: List[str] = []
    start = 0
    length = len(text)
    while start < length:
        index = text.find("\n", start)
        if index == -1:
            lines.append(text[start:])
            break
        line = text[start:index]
        if line.endswith("\r"):
            line = line[:-1]
        lines.append(line)
        start = index + 1
    return lines


def _unified_targets(text: str) -> Tuple[List[str], bool]:
    targets: List[str] = []
    seen: set = set()
    complete = True
    old_left = 0
    new_left = 0
    for line in _diff_lines(text):
        marker = line[:1]
        if old_left > 0 or new_left > 0:
            if marker == "\\":
                # `\ No newline at end of file` 不是正文行，不消耗计数。
                continue
            if marker in {"-", "+", " "} or line == "":
                # 空行是工具省略行尾空格的上下文行，按上下文消费。
                consumed = " " if line == "" else marker
                if consumed in {"-", " "}:
                    old_left -= 1
                if consumed in {"+", " "}:
                    new_left -= 1
                continue
            # 计数未耗尽却出现别的行：hunk 不可证明，按不完整披露。
            complete = False
            old_left = 0
            new_left = 0
        match = _HUNK_RE.match(line)
        if match:
            old_left = int(match.group(2)) if match.group(2) is not None else 1
            new_left = int(match.group(4)) if match.group(4) is not None else 1
            continue
        candidates: List[str] = []
        header, proven = _unified_header_path(line)
        if not proven:
            complete = False
        if header is not None and header != "/dev/null":
            # 添加/删除的另一端是 /dev/null：先排除再剥离前缀。
            candidates.append(_strip_diff_prefix(header))
        for marker in ("rename from ", "rename to "):
            if not line.startswith(marker):
                continue
            # rename 路径本来就是未加前缀的真实路径，不能再剥一层 a/b。
            value = line[len(marker) :].strip()
            if value:
                candidates.append(value)
            else:
                complete = False
        for path in candidates:
            if path not in seen:
                seen.add(path)
                targets.append(path)
        if line.startswith("Binary files ") and " differ" in line:
            complete = False
    if old_left > 0 or new_left > 0:
        # hunk 计数声称的行数没出现：补丁本身不完整，目标不可证明。
        complete = False
    return targets, complete


def patch_targets(text: str) -> Tuple[List[str], bool]:
    """提取 apply_patch / unified diff 的路径；解析不全标记 incomplete。"""
    if not isinstance(text, str):
        return [], False
    if "*** Begin Patch" in text:
        return _patch_begin_targets(text)
    if text.startswith("diff --git ") or "\n--- " in text or text.startswith("--- "):
        return _unified_targets(text)
    return [], False
