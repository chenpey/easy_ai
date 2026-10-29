#!/usr/bin/env python3
"""回放专用越界读取守卫（Codex PreToolUse）。

只作用于 agent_runner 的回放工作区，独立于 EasyDev 激活：任何可观察读取目标
只要位于 canonical workspace 与被测 Skill 副本两个允许根之外，一律 deny。
这里不沿用运行期 Plugin 的 fail-open：缺 policy、格式错误、畸形输入、枚举
超限/异常都返回 deny 并记脱敏健康失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

GUARD_DIR = os.path.dirname(os.path.abspath(__file__))
SHELL_MODULE_NAME = "shell.py"


def _find_hooks_root(start: str) -> Optional[str]:
    """向上查找包含 `hooks/shell.py` 的源码树根；与安装目录名无关。"""
    current = start
    for _ in range(6):
        if os.path.isfile(os.path.join(current, "hooks", SHELL_MODULE_NAME)):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _load_shell() -> Tuple[Any, str]:
    """优先加载随守卫一起部署的副本；源码树内回退到 hooks 包。"""
    if os.path.isfile(os.path.join(GUARD_DIR, SHELL_MODULE_NAME)):
        if GUARD_DIR not in sys.path:
            sys.path.insert(0, GUARD_DIR)
        try:
            import shell as deployed  # type: ignore

            return deployed, ""
        except (Exception, SystemExit) as error:  # pragma: no cover - 部署件损坏
            return None, "部署件 %s 无法导入（%s）" % (SHELL_MODULE_NAME, error.__class__.__name__)
    root = _find_hooks_root(GUARD_DIR)
    if not root:
        return None, "未找到随守卫部署的 %s，也找不到源码树" % SHELL_MODULE_NAME
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from hooks import shell as packaged  # type: ignore

        return packaged, ""
    except (Exception, SystemExit) as error:
        return None, "hooks 包无法导入（%s）" % error.__class__.__name__


shell, IMPORT_ERROR = _load_shell()

MAX_INPUT_BYTES = 1024 * 1024
DEFAULT_MAX_ENTRIES = 20000
DEFAULT_DEADLINE_MS = 2000
HOOK_NAME = "replay-read-guard"
READ_PATH_FIELDS = ("path", "paths", "file_path", "filePath", "files", "Path", "absolute_path")
STRUCTURED_READ_TOOLS = {"read", "read_file", "view_file", "view", "read_text_file", "grep", "find", "ls"}
DIRECT_FOLLOW_FLAGS = {"rg --follow": "--follow", "find -L": "-L", "ls -L": "-L"}


class GuardFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _load_policy(path: str) -> Dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise GuardFailure("policy_missing", "读取守卫未找到 policy")
    if len(payload) > MAX_INPUT_BYTES:
        raise GuardFailure("policy_invalid", "policy 过大")
    try:
        policy = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GuardFailure("policy_invalid", "policy 不是合法 JSON")
    if not isinstance(policy, dict):
        raise GuardFailure("policy_invalid", "policy 不是对象")
    roots = policy.get("allowed_roots")
    if not isinstance(roots, list) or not roots:
        raise GuardFailure("policy_invalid", "policy 缺 allowed_roots")
    canonical: List[str] = []
    for root in roots:
        if not isinstance(root, str) or not os.path.isabs(root):
            raise GuardFailure("policy_invalid", "allowed_roots 必须是绝对路径")
        resolved = os.path.realpath(root)
        if not os.path.isdir(resolved):
            raise GuardFailure("policy_invalid", "allowed_roots 目录不存在")
        canonical.append(resolved)
    real_home = policy.get("real_home")
    if not isinstance(real_home, str) or not real_home:
        raise GuardFailure("policy_invalid", "policy 缺 real_home")
    return {
        "allowed_roots": canonical,
        "real_home": os.path.realpath(real_home),
        "health_path": policy.get("health_path"),
        "max_entries": policy.get("max_entries", DEFAULT_MAX_ENTRIES),
        "deadline_ms": policy.get("deadline_ms", DEFAULT_DEADLINE_MS),
    }


def read_health(path: str) -> List[str]:
    """读取健康记录码列表；文件缺失/不可读返回 `health_unreadable`。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        # 缺失与不可读都意味着没有可核对的健康证据。
        return ["health_unreadable"]
    codes: List[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            codes.append("health_corrupt")
            continue
        if not isinstance(value, dict):
            codes.append("health_corrupt")
            continue
        codes.append(str(value.get("code") or "health_corrupt"))
    return codes


def _health_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _truncate(path: str, size: int) -> None:
    try:
        with open(path, "r+b") as handle:
            handle.truncate(size)
    except OSError:
        pass


INVOCATION_SUFFIX = ".invocations"


def invocations_path_for(policy: Dict[str, Any]) -> str:
    explicit = policy.get("invocations_path")
    if isinstance(explicit, str) and explicit:
        return explicit
    health = policy.get("health_path")
    if isinstance(health, str) and health:
        return health + INVOCATION_SUFFIX
    return ""


def _record_invocation(policy: Dict[str, Any], code: str) -> None:
    """每次调用留一条最小记录，用于区分“健康”与“根本没触发 Hook”。"""
    path = invocations_path_for(policy)
    if not path:
        return
    entry = json.dumps({"code": code}, separators=(",", ":")) + "\n"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, entry.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def read_invocations(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def _record_health(policy: Dict[str, Any], code: str) -> None:
    path = policy.get("health_path")
    if not isinstance(path, str) or not path:
        return
    entry = json.dumps({"hook": HOOK_NAME, "code": code}, separators=(",", ":")) + "\n"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, entry.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _expand(path: str, real_home: str) -> str:
    expanded = path
    if expanded == "~" or expanded.startswith("~/"):
        expanded = real_home + expanded[1:]
    for token in ("${HOME}", "$HOME"):
        expanded = expanded.replace(token, real_home)
    return expanded


def _inside(path: str, roots: Iterable[str]) -> bool:
    candidate = os.path.normpath(path)
    for root in roots:
        try:
            if os.path.commonpath([candidate, os.path.normpath(root)]) == os.path.normpath(root):
                return True
        except ValueError:
            continue
    return False


UNRESOLVED_CHARS = ("$", "`", "*", "?", "[", "]", "{", "}")


def _resolve(target: str, cwd: Optional[str], real_home: str) -> str:
    """把显式读取目标解析成规范路径，并核对链接链的终止与存在性。

    `realpath` 会丢掉“入口是链接”这一事实：悬空链接解析后是一个不存在的路径，
    自环/双环会原样返回，两者都仍可能落在允许根内。因此解析后必须再核对目标
    存在且类型可读，无法证明归属的一律拒绝。
    """
    if not isinstance(target, str) or not target or "\x00" in target:
        raise GuardFailure("target_unresolved", "读取目标无法解析")
    expanded = _expand(target, real_home)
    if "~" in expanded:
        # `~user` 需要查询系统账户；不查账户也不回显目标，残留即无法证明归属。
        raise GuardFailure("target_unresolved", "读取目标含无法解析的用户主目录简写")
    if any(char in expanded for char in UNRESOLVED_CHARS):
        # 残留变量/通配符意味着目标无法证明，保守拒绝。
        raise GuardFailure("target_unresolved", "读取目标含未解析的变量或通配符")
    if not os.path.isabs(expanded):
        if not cwd:
            raise GuardFailure("target_unresolved", "缺少 cwd，无法定位相对读取目标")
        expanded = os.path.join(cwd, expanded)
    lexical = os.path.normpath(expanded)
    if os.path.lexists(lexical):
        resolved = os.path.realpath(lexical)
        try:
            info = os.stat(resolved)
        except OSError:
            # 悬空链接、自环/双环在 realpath 之后仍终止不了：归属不可证明。
            raise GuardFailure("symlink_unresolved", "符号链接悬空或循环，目标归属不可证明")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise GuardFailure("target_unresolved", "读取目标不是普通文件或目录")
        return resolved
    # 目标本身尚不存在（例如探测一个还没生成的文件）：位置仍可证明，但父链不能
    # 经悬空/循环链接消解。
    ancestor = os.path.dirname(lexical)
    while ancestor and ancestor != os.path.dirname(ancestor):
        if os.path.lexists(ancestor):
            break
        ancestor = os.path.dirname(ancestor)
    if ancestor:
        try:
            os.stat(os.path.realpath(ancestor))
        except OSError:
            raise GuardFailure("symlink_unresolved", "父链含悬空或循环链接，目标归属不可证明")
    return os.path.realpath(lexical)


def _check_path(path: str, policy: Dict[str, Any]) -> None:
    if not _inside(path, policy["allowed_roots"]):
        raise GuardFailure("outside_allowed", "读取目标位于回放允许根之外")


def _walk_without_follow(
    roots: List[str],
    policy: Dict[str, Any],
    started: float,
) -> None:
    """lstat/不跟链枚举；所有 symlink 的 canonical 目标必须在允许根内。"""
    max_entries = policy["max_entries"]
    deadline = started + policy["deadline_ms"] / 1000.0
    budget = {"entries": 0}
    visited: set = set()

    def visit(directory: str) -> None:
        real = os.path.realpath(directory)
        if real in visited:
            return
        visited.add(real)
        if time.monotonic() > deadline:
            raise GuardFailure("deadline_exceeded", "递归枚举超过内部截止")
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    budget["entries"] += 1
                    if budget["entries"] > max_entries:
                        raise GuardFailure("budget_exceeded", "递归枚举超过入口预算")
                    if time.monotonic() > deadline:
                        raise GuardFailure("deadline_exceeded", "递归枚举超过内部截止")
                    try:
                        is_link = entry.is_symlink()
                    except OSError:
                        raise GuardFailure("enumerate_error", "无法判定目录项类型")
                    path = entry.path
                    if is_link:
                        try:
                            target = os.path.realpath(path)
                        except OSError:
                            raise GuardFailure("enumerate_error", "无法解析符号链接目标")
                        if not _inside(target, policy["allowed_roots"]):
                            raise GuardFailure("outside_allowed", "目录内符号链接指向允许根之外")
                        # 悬空/自环/双环解析后不存在：无法证明目标归属，按拒绝处理，
                        # 不能因为 realpath 仍落在允许根内就放行。
                        try:
                            target_info = os.stat(target)
                        except OSError:
                            raise GuardFailure(
                                "symlink_unresolved", "符号链接目标不存在或无法解析（悬空/循环）"
                            )
                        if stat.S_ISDIR(target_info.st_mode):
                            visit(target)
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            visit(path)
                    except OSError:
                        raise GuardFailure("enumerate_error", "无法读取目录")
        except GuardFailure:
            raise
        except OSError:
            raise GuardFailure("enumerate_error", "目录枚举失败")

    for root in roots:
        if not os.path.lexists(root):
            continue
        if os.path.islink(root):
            raise GuardFailure("outside_allowed", "读取目标本身是符号链接，无法证明归属")
        if os.path.isdir(root):
            visit(root)


def _targets_from_tool(tool: str, tool_input: Any) -> Tuple[List[Tuple[str, Optional[str]]], bool]:
    """返回 (读取目标, 是否递归)；未知结构不是空目标。"""
    if not isinstance(tool_input, dict):
        raise GuardFailure("tool_input_missing", "工具参数不是对象，无法证明无越界读取")
    if tool in {"Bash", "bash", "exec_command"}:
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            raise GuardFailure("tool_input_missing", "缺少命令，无法判定读取目标")
        analysis = shell.analyze(command)
        if analysis.opaque or analysis.too_deep or analysis.read_unknown:
            raise GuardFailure("target_unresolved", "命令结构无法安全解析，读取目标不可证明")
        targets: List[Tuple[str, Optional[str]]] = []
        for read in analysis.reads:
            base = read.get("cwd")
            targets.append(
                (str(read.get("path")), base if isinstance(base, str) and base else None)
            )
        recursive = any(bool(read.get("recursive")) for read in analysis.reads)
        follow = analysis.recursive_follow
        if follow:
            raise GuardFailure("recursive_follow", "rg --follow / find -L / ls -L 不在本版放行范围")
        if not targets:
            return [], False
        return targets, recursive
    if tool in STRUCTURED_READ_TOOLS:
        plain: List[str] = []
        for field in READ_PATH_FIELDS:
            value = tool_input.get(field)
            if isinstance(value, str) and value:
                plain.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item:
                        plain.append(item)
        if tool == "ls" and any(
            isinstance(token, str) and token.startswith("-") and "L" in token
            for token in tool_input.get("flags", []) or []
        ):
            raise GuardFailure("recursive_follow", "ls -L 不在本版放行范围")
        if not plain:
            raise GuardFailure("target_unresolved", "结构化读取工具缺少路径字段")
        return [(item, None) for item in plain], tool in {"grep", "find", "ls"}
    return [], False


def check_request(request: Dict[str, Any], policy: Dict[str, Any]) -> None:
    tool = request.get("tool_name") or request.get("toolName")
    tool_input = request.get("tool_input", request.get("toolInput"))
    if not isinstance(tool, str) or not tool:
        raise GuardFailure("input_invalid", "缺少工具名")
    cwd = request.get("cwd") or request.get("working_directory")
    if cwd is not None and not isinstance(cwd, str):
        raise GuardFailure("input_invalid", "cwd 类型错误")
    targets, recursive = _targets_from_tool(tool, tool_input)
    if not targets:
        return
    started = time.monotonic()
    resolved: List[str] = []
    for target, base in targets:
        path = _resolve(target, base or cwd, policy["real_home"])
        _check_path(path, policy)
        resolved.append(path)
    if recursive:
        _walk_without_follow(resolved, policy, started)
    else:
        for path in resolved:
            if os.path.isdir(path):
                _walk_without_follow([path], policy, started)


def handle(request: Any, policy: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if shell is None:
        _record_health(policy, "import_failed")
        _record_invocation(policy, "import_failed")
        return _deny("%s: %s" % (HOOK_NAME, IMPORT_ERROR or "解析模块不可用")), "import_failed"
    try:
        if not isinstance(request, dict):
            raise GuardFailure("input_invalid", "stdin 不是 JSON 对象")
        event = request.get("hook_event_name") or request.get("hookEventName") or request.get("event")
        if event not in (None, "PreToolUse", "pre_tool_use", "preToolUse"):
            _record_invocation(policy, "skipped")
            return {}, None
        check_request(request, policy)
    except GuardFailure as failure:
        _record_health(policy, failure.code)
        _record_invocation(policy, failure.code)
        return _deny("%s: %s" % (HOOK_NAME, failure.message)), failure.code
    except Exception:  # 可捕获异常一律 deny，不泄漏细节
        _record_health(policy, "internal_error")
        _record_invocation(policy, "internal_error")
        return _deny("%s: 内部异常，按回放守卫策略拒绝" % HOOK_NAME), "internal_error"
    _record_invocation(policy, "ok")
    return {}, None


def read_stdin(stream: Any) -> Any:
    payload = stream.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise GuardFailure("input_invalid", "stdin 超过上限")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GuardFailure("input_invalid", "stdin 不是合法 JSON")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EasyDev replay read guard")
    parser.add_argument("--policy", required=True)
    args = parser.parse_args(argv)
    try:
        policy = _load_policy(args.policy)
    except GuardFailure as failure:
        print(json.dumps(_deny("%s: %s" % (HOOK_NAME, failure.message)), ensure_ascii=False))
        return 0
    try:
        request = read_stdin(sys.stdin.buffer)
    except GuardFailure as failure:
        _record_health(policy, failure.code)
        print(json.dumps(_deny("%s: %s" % (HOOK_NAME, failure.message)), ensure_ascii=False))
        return 0
    decision, _code = handle(request, policy)
    print(json.dumps(decision, ensure_ascii=False))
    return 0


def _create_empty_health(path: str) -> None:
    """受信安装阶段建空健康日志：不跟随、不截断。

    `O_EXCL` 让已存在的运行期日志在重新安装时原样保留：正常首次 probe 需要
    空文件的 before_codes 基线，但删日志的证据不能被安装“修复”。
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def install_replay_guard(
    run_dir: str,
    workspace: str,
    skill_root: str,
    real_home: str,
    python: Optional[str] = None,
) -> Dict[str, Any]:
    """在运行目录生成受信脚本副本、其依赖、policy 与工作区 `.codex/hooks.json`。

    守卫与它实际 import 的解析模块一起部署到 policy 目录，复制后不依赖源码树
    位置或仓库目录名。
    """
    policy_dir = os.path.join(run_dir, "policy")
    os.makedirs(policy_dir, mode=0o700, exist_ok=True)
    os.chmod(policy_dir, 0o700)
    script = os.path.join(policy_dir, "replay_read_guard.py")
    shutil.copyfile(os.path.abspath(__file__), script)
    os.chmod(script, 0o700)
    shell_source = getattr(shell, "__file__", None) if shell is not None else None
    if not shell_source or not os.path.isfile(shell_source):
        raise RuntimeError("守卫依赖不可用：找不到 shell 解析模块")
    deployed_shell = os.path.join(policy_dir, SHELL_MODULE_NAME)
    shutil.copyfile(shell_source, deployed_shell)
    os.chmod(deployed_shell, 0o600)
    health_path = os.path.join(policy_dir, "health.jsonl")
    _create_empty_health(health_path)
    policy = {
        "allowed_roots": [os.path.realpath(workspace), os.path.realpath(skill_root)],
        "real_home": os.path.realpath(real_home),
        "health_path": health_path,
        "invocations_path": health_path + INVOCATION_SUFFIX,
        "max_entries": DEFAULT_MAX_ENTRIES,
        "deadline_ms": DEFAULT_DEADLINE_MS,
    }
    policy_path = os.path.join(policy_dir, "policy.json")
    with open(policy_path, "w", encoding="utf-8") as handle:
        json.dump(policy, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    interpreter = os.path.realpath(python or sys.executable)
    command = "%s -I %s --policy %s" % (
        _quote(interpreter),
        _quote(script),
        _quote(policy_path),
    )
    hooks = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }
    codex_dir = os.path.join(workspace, ".codex")
    os.makedirs(codex_dir, exist_ok=True)
    hooks_path = os.path.join(codex_dir, "hooks.json")
    if os.path.lexists(hooks_path):
        raise RuntimeError("replay workspace already has .codex/hooks.json; refusing to overwrite")
    with open(hooks_path, "w", encoding="utf-8") as handle:
        json.dump(hooks, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    exclude = os.path.join(workspace, ".git", "info", "exclude")
    if os.path.isdir(os.path.dirname(exclude)):
        with open(exclude, "a", encoding="utf-8") as handle:
            handle.write(".codex/hooks.json\n")
    return {
        "policy_dir": policy_dir,
        "policy_path": policy_path,
        "script": script,
        "shell": deployed_shell,
        "hooks_path": hooks_path,
        "health_path": health_path,
        "invocations_path": health_path + INVOCATION_SUFFIX,
        "command": command,
        "files": [script, deployed_shell, policy_path, hooks_path],
    }


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def file_digest(path: str) -> str:
    """文件摘要；缺失/不可读时返回可比较的标记，不抛异常。"""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def guard_files_intact(files: Sequence[str], digests: Dict[str, str]) -> bool:
    return all(file_digest(path) == digests.get(path, "") for path in files)


def _run_command(
    command: str,
    stdin_text: str,
    *,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """执行生成的原命令，返回退出状态与单个 JSON 输出。"""
    try:
        completed = subprocess.run(
            command,
            shell=True,
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "exit": None,
            "json": None,
            "decision": "error",
            "stdout": "",
            "stderr": error.__class__.__name__,
        }
    parsed = None
    parse_error = ""
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        parse_error = "stdout 不是单个 JSON"
    decision = "allow"
    if isinstance(parsed, dict) and parsed:
        decision = str(
            parsed.get("hookSpecificOutput", {}).get("permissionDecision") or "unknown"
        )
    return {
        "exit": completed.returncode,
        "json": parsed,
        "decision": decision if parsed is not None else "malformed-output",
        "stdout": completed.stdout[:2000],
        "stderr": (completed.stderr or parse_error)[:2000],
    }


def _run_budget_case(command: str, policy_path: str, workspace: str) -> Dict[str, Any]:
    """临时收紧预算，验证预算/截止失败也被真实执行并拒绝。"""
    with open(policy_path, "r", encoding="utf-8") as handle:
        original = handle.read()
    try:
        policy = json.loads(original)
        policy["max_entries"] = 1
        policy["deadline_ms"] = 1
        with open(policy_path, "w", encoding="utf-8") as handle:
            json.dump(policy, handle)
        return _run_command(
            command,
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls -R ."},
                    "cwd": workspace,
                }
            ),
        )
    finally:
        with open(policy_path, "w", encoding="utf-8") as handle:
            handle.write(original)


def probe(policy_path: str, command: Optional[str] = None) -> Dict[str, Any]:
    """模型前探针：执行生成的原命令，覆盖允许/越界/畸形/预算四类。"""
    try:
        policy = _load_policy(policy_path)
    except GuardFailure as failure:
        return {"ok": False, "error": failure.code, "cases": {}}
    if not command:
        command = "%s -I %s --policy %s" % (
            _quote(os.path.realpath(sys.executable)),
            _quote(os.path.abspath(__file__)),
            _quote(policy_path),
        )
    workspace = policy["allowed_roots"][0]
    health_path = str(policy.get("health_path") or "")
    before_codes = read_health(health_path) if health_path else []
    if "health_unreadable" in before_codes:
        # 运行期删日志后探针不“修复”它，否则删除证据会被掩盖。
        return {"ok": False, "error": "health_unreadable", "cases": {}}
    before_size = _health_size(health_path)
    probe_home = os.path.join(os.path.dirname(policy_path), "probe-home")
    os.makedirs(probe_home, mode=0o700, exist_ok=True)
    outside = os.path.join(probe_home, "sentinel.txt")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("sentinel\n")
    cases: Dict[str, Any] = {}
    try:
        cases["allow"] = _run_command(
            command,
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls ."},
                    "cwd": workspace,
                }
            ),
        )
        cases["deny"] = _run_command(
            command,
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "cat " + _quote(outside)},
                    "cwd": workspace,
                }
            ),
        )
        cases["malformed"] = _run_command(command, "{not-json")
        cases["budget"] = _run_budget_case(command, policy_path, workspace)
    finally:
        # 探针期的预期拒绝记入健康文件供自检，随后恢复原长度，运行期健康从头开始。
        after = read_health(health_path) if health_path else []
        new_codes = after[len(before_codes) :]
        _truncate(health_path, before_size)
    expected_health = {"outside_allowed", "input_invalid"}
    budget_codes = {"budget_exceeded", "deadline_exceeded"}
    ok = (
        cases["allow"].get("decision") == "allow"
        and cases["deny"].get("decision") == "deny"
        and cases["malformed"].get("decision") == "deny"
        and cases["budget"].get("decision") == "deny"
        and all(cases[name].get("exit") == 0 for name in cases)
        and all(isinstance(cases[name].get("json"), dict) for name in cases)
        and expected_health.issubset(set(new_codes))
        and bool(budget_codes & set(new_codes))
    )
    return {
        "ok": ok,
        "command": command,
        "cases": cases,
        "health": new_codes,
    }


if __name__ == "__main__":
    sys.exit(main())
