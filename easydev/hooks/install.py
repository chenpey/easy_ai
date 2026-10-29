#!/usr/bin/env python3
"""EasyDev Hook 安装/卸载生成器（标准库）。

默认只把生成物写到 `--output`（用户显式给出的目录）；修改宿主配置必须显式
调用 `apply`。pi 额外在 Plugin 根写 `install-context.json`，因为扩展默认从
`<plugin-root>/install-context.json` 读取核心与解释器路径。不安装 Skill、
不联网、不改 models。

用法：
  install.py generate --host <antigravity|codex|dsh|pi> --plugin-root <abs>
                      --skill-root <abs> (--output <abs> | --output-root <abs>)
                      [--mode probe|enforce]      # 默认 enforce
  install.py apply --target <abs-config> --source <abs-generated-config>
                   [--manifest <abs-install-manifest>]
  install.py uninstall --target <abs-config> [--manifest <abs-install-manifest>
                        | --source <abs-generated-config>]
  install.py prune --root <abs-config-root> [--keep <release> ...] [--apply]

升级建议用 `--output-root <plugin-root>/config` 的稳定布局：生成物固定落在
`<root>/<host>/`，同一路径下重新生成只覆盖本插件自己的文件，升级不再堆积
`<root>/<X.Y.Z>/` 版本号目录；确实需要保留旧版本时用 `prune --keep`。`apply`
从不覆盖用户改过的条目：同标识但内容不同直接非零退出并保留原文件。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    # 独立 Plugin 目录：按同目录模块导入，不依赖仓库目录名或安装位置。
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import paths  # type: ignore[no-redef]
else:  # pragma: no cover - 包内导入
    from . import paths  # type: ignore

PLUGIN_ROOT_DEFAULT = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(PLUGIN_ROOT_DEFAULT, "manifest.json")
TEMPLATE_DIR = os.path.join(PLUGIN_ROOT_DEFAULT, "templates")
COMMAND_PLACEHOLDER = "__EASYDEV_COMMAND__"
INSTALL_MANIFEST_NAME = "install-manifest.json"
CONTEXT_NAME = "install-context.json"
MAX_PREVIOUS_ENTRIES = 80
# 模板里已注册的宿主动作名；用于识别命名 Hook 组，不把其他配置对象当成 Hook。
HOOK_EVENT_NAMES = {
    "SessionStart",
    "PreInvocation",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostInvocation",
    "SubagentStart",
    "SubagentStop",
    "Stop",
}
# 与 generate --host 的 choices 保持一致：prune 只把已知宿主子目录当作本插件生成物。
HOST_DIRS = {"antigravity", "codex", "dsh", "pi"}
RELEASE_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def load_manifest() -> Dict[str, Any]:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SystemExit("manifest.json 不是对象")
    return value


def resolve_path(value: str, base: Optional[str] = None) -> str:
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base or os.getcwd(), expanded)
    return os.path.abspath(expanded)


def core_command(python: str, core_path: str, context_path: str, mode: str) -> str:
    """逐项 POSIX 安全引用；不依赖启动时 PATH 猜解释器。"""
    return " ".join(
        [
            shlex.quote(os.path.realpath(python)),
            shlex.quote(core_path),
            "--host",
            shlex.quote("__HOST__"),
            "--context",
            shlex.quote(context_path),
            "--mode",
            shlex.quote(mode),
        ]
    )


def render_template(host: str, command: str) -> Dict[str, Any]:
    path = os.path.join(TEMPLATE_DIR, "%s.json" % host)
    with open(path, "r", encoding="utf-8") as handle:
        template = json.load(handle)
    if not isinstance(template, dict):
        raise SystemExit("%s 模板不是对象" % host)
    replaced = _replace_placeholders(template, command, host)
    if replaced == template:
        raise SystemExit("%s 模板缺少命令占位符" % host)
    return replaced


def _replace_placeholders(value: Any, command: str, host: str, event: str = "") -> Any:
    """按事件槽位替换命令占位符。

    `event` 是当前遍历到的宿主动作名（如 `PreToolUse`）：Antigravity 的 stdin 不带
    事件名，命令必须显式带 `--event`，否则核心只能看到 `unknown` 而全部放行。
    """
    if isinstance(value, dict):
        return {
            key: _replace_placeholders(
                item,
                command,
                host,
                key if isinstance(item, list) and key in HOOK_EVENT_NAMES else event,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_placeholders(item, command, host, event) for item in value]
    if value == COMMAND_PLACEHOLDER:
        if host == "antigravity" and event:
            return "%s --event %s" % (command, shlex.quote(event))
        return command
    return value


def distribution_files(plugin_root: str) -> List[str]:
    manifest = load_manifest()
    files = manifest.get("distribution", {}).get("files", [])
    result: List[str] = []
    for relative in files:
        path = os.path.join(plugin_root, relative)
        if os.path.isfile(path):
            result.append(relative)
    return result


def write_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(path),
            prefix="." + os.path.basename(path) + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _entry_command(entry: Any) -> str:
    """返回条目里第一条 command（Antigravity 在顶层，Codex/DSH 在 hooks[]）。"""
    if isinstance(entry, dict):
        if isinstance(entry.get("command"), str):
            return entry["command"]
        for item in entry.values():
            found = _entry_command(item)
            if found:
                return found
    elif isinstance(entry, list):
        for item in entry:
            found = _entry_command(item)
            if found:
                return found
    return ""


def _command_core(command: str) -> str:
    """本 Plugin 条目的身份：命令行里 core.py 的规范化绝对路径。

    用 shlex 解引用后比较，因此含空格/单引号/美元符的 Plugin 根也能识别；
    配置里的 `--context` 差异由条目摘要区分（probe/enforce 换代）。
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ""
    for token in tokens:
        if os.path.basename(token) == "core.py":
            return os.path.realpath(token)
    return ""


def _entry_digest(event: str, entry: Any) -> str:
    payload = json.dumps(
        {"event": event, "entry": entry}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _entry_records(config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """安装清单条目：group、event、身份、摘要与完整条目内容。"""
    records: List[Dict[str, Any]] = []
    for group, hooks in _hook_groups(config):
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                command = _entry_command(entry)
                records.append(
                    {
                        "group": group,
                        "event": event,
                        "identity": _command_core(command),
                        "digest": _entry_digest(event, entry),
                        "command": command,
                        "entry": entry,
                    }
                )
    return records


def _event_buckets(value: Any) -> Dict[str, Any]:
    """只把“事件名 -> 条目数组”映射当成 Hook 组，避开无关配置对象。"""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(item, list) and key in HOOK_EVENT_NAMES}


def _hook_groups(config: Optional[Dict[str, Any]]) -> List[Tuple[Optional[str], Dict[str, Any]]]:
    """宿主配置里的所有 Hook 组：顶层 `hooks` 与命名组（如 `easydev-hooks`）。

    Codex/DSH 用顶层 `{"hooks": {...}}`；Antigravity 用命名 Hook 组
    `{"easydev-hooks": {"PreToolUse": [...]}}`。只操作本 Plugin 自己的组，
    merge/uninstall 保留其他命名组。
    """
    groups: List[Tuple[Optional[str], Dict[str, Any]]] = []
    if not isinstance(config, dict):
        return groups
    top = config.get("hooks")
    if isinstance(top, dict):
        groups.append((None, top))
    for key, value in config.items():
        if key == "hooks" or not isinstance(value, dict):
            continue
        if not value:
            continue
        buckets = _event_buckets(value)
        if buckets and len(buckets) == len(value):
            groups.append((key, value))
    return groups


def _previous_entries(output: str, current: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一输出目录里上一次生成的条目摘要：用于识别 probe→enforce 换代。"""
    path = os.path.join(output, INSTALL_MANIFEST_NAME)
    previous: List[Dict[str, Any]] = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                old = json.load(handle)
        except (OSError, json.JSONDecodeError):
            old = None
        if isinstance(old, dict):
            for key in ("entries", "previous_entries"):
                for item in old.get(key) or []:
                    if isinstance(item, dict) and isinstance(item.get("digest"), str):
                        previous.append(
                            {
                                "event": item.get("event"),
                                "identity": item.get("identity"),
                                "digest": item["digest"],
                            }
                        )
    current_digests = {item["digest"] for item in current}
    seen = set(current_digests)
    result: List[Dict[str, Any]] = []
    for item in previous:
        if item["digest"] in seen:
            continue
        seen.add(item["digest"])
        result.append(item)
    return result[:MAX_PREVIOUS_ENTRIES]


def generate(args: argparse.Namespace) -> Dict[str, Any]:
    plugin_root = resolve_path(args.plugin_root)
    skill_root = resolve_path(args.skill_root)
    output_root = resolve_path(args.output_root) if args.output_root else None
    # 稳定布局：`--output-root <root>` 固定写 `<root>/<host>`，升级不再产生版本号目录。
    output = os.path.join(output_root, args.host) if output_root else resolve_path(args.output)
    if not os.path.isfile(os.path.join(plugin_root, "core.py")):
        raise SystemExit("plugin-root 缺少 core.py")
    if not os.path.isdir(os.path.join(skill_root, "references")):
        raise SystemExit("skill-root 缺少 references/")
    manifest = load_manifest()
    release = manifest.get("release")
    if not isinstance(release, str):
        raise SystemExit("manifest.json 缺少 release")
    command_template = core_command(
        sys.executable,
        os.path.join(plugin_root, "core.py"),
        os.path.join(output, CONTEXT_NAME),
        args.mode,
    )
    command = command_template.replace("__HOST__", args.host)
    os.makedirs(output, mode=0o700, exist_ok=True)
    os.chmod(output, 0o700)
    context = {
        "protocol": manifest.get("protocol", 1),
        "version": release,
        "host": args.host,
        "mode": args.mode,
        "plugin_root": plugin_root,
        "skill_root": skill_root,
        "python": os.path.realpath(sys.executable),
        "doc_paths": [],
    }
    context_path = os.path.join(output, CONTEXT_NAME)
    write_json(context_path, context)
    plugin_context = None
    if args.host == "pi":
        # pi 扩展默认读 `<plugin-root>/install-context.json`；生成器把同一份
        # context 放到扩展实际读取的位置，避免“生成成功但扩展静默放行”。
        plugin_context = os.path.join(plugin_root, CONTEXT_NAME)
        write_json(plugin_context, context)
    config_path = None
    config = None
    if args.host != "pi":
        config = render_template(args.host, command)
        config_path = os.path.join(output, "%s-hooks.json" % args.host)
        write_json(config_path, config)
    entries = _entry_records(config)
    install_manifest = {
        "name": manifest.get("name"),
        "release": release,
        "protocol": manifest.get("protocol", 1),
        "host": args.host,
        "mode": args.mode,
        "plugin_root": plugin_root,
        "skill_root": skill_root,
        "output": output,
        "config": config_path,
        "context": context_path,
        "plugin_context": plugin_context,
        "command": command if args.host != "pi" else None,
        "extension_entry": os.path.join(plugin_root, "index.ts") if args.host == "pi" else None,
        "entries": entries,
        "previous_entries": _previous_entries(output, entries),
        "files": distribution_files(plugin_root),
        "install_skill": False,
        "install_tests": False,
    }
    manifest_path = os.path.join(output, INSTALL_MANIFEST_NAME)
    write_json(manifest_path, install_manifest)
    # 只在“生成目录的上一级”是配置根时才有意义：稳定布局下会看到旧的版本号目录。
    layout_root = output_root or os.path.dirname(output)
    stale = _stale_release_dirs(layout_root, release, manifest.get("name"))
    if stale:
        print(
            "提示: 发现本插件的旧版本生成目录 %s；确认不再需要后运行 "
            "`python3 %s prune --root %s --apply` 清理（默认 dry-run）。"
            % (
                "、".join(stale),
                os.path.join(PLUGIN_ROOT_DEFAULT, "install.py"),
                layout_root,
            ),
            file=sys.stderr,
        )
    return {
        "host": args.host,
        "mode": args.mode,
        "output": output,
        "output_root": output_root,
        "stale": stale,
        "config": config_path,
        "context": context_path,
        "plugin_context": plugin_context,
        "manifest": manifest_path,
        "command": command if args.host != "pi" else None,
    }


def _release_dir_reason(path: str, child: str, plugin_name: Any) -> str:
    """版本号目录是否完全由本插件生成；返回空串表示可安全清理。

    只接受 `<root>/<X.Y.Z>/<host>/{install-manifest.json, …}` 这一种形状：
    任何软链、未知文件、缺失或对不上的清单都跳过。宁可留下也不误删用户内容。
    """
    if not RELEASE_PATTERN.match(child):
        return "目录名不是发布版本号"
    if os.path.islink(path):
        return "是软链，未跟随"
    if not os.path.isdir(path):
        return "不是目录"
    hosts = sorted(os.listdir(path))
    if not hosts:
        return "空目录"
    unknown = [host for host in hosts if host not in HOST_DIRS]
    if unknown:
        return "含未知子目录: %s" % unknown
    for host in hosts:
        host_dir = os.path.join(path, host)
        if os.path.islink(host_dir) or not os.path.isdir(host_dir):
            return "%s 不是真实目录" % host
        expected = {INSTALL_MANIFEST_NAME, CONTEXT_NAME, "%s-hooks.json" % host}
        for item in sorted(os.listdir(host_dir)):
            if item not in expected:
                return "%s 含未知文件: %s" % (host, item)
        manifest_path = os.path.join(host_dir, INSTALL_MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            return "%s 缺 %s" % (host, INSTALL_MANIFEST_NAME)
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return "%s 清单不可读" % host
        if not isinstance(value, dict) or value.get("name") != plugin_name:
            return "%s 清单不是本插件生成" % host
        if value.get("release") != child:
            return "%s 清单版本与目录名不一致" % host
        for base, dirs, files in os.walk(host_dir):
            for item in list(dirs) + list(files):
                if os.path.islink(os.path.join(base, item)):
                    return "含软链，未处理"
    return ""


def _stale_release_dirs(root: str, release: Any, plugin_name: Any) -> List[str]:
    """`root` 下由本插件生成、但版本不是当前 release 的版本号目录。"""
    stale: List[str] = []
    try:
        children = sorted(os.listdir(root))
    except OSError:
        return stale
    for child in children:
        if child == release:
            continue
        path = os.path.join(root, child)
        if not RELEASE_PATTERN.match(child):
            continue
        if _release_dir_reason(path, child, plugin_name) == "":
            stale.append(path)
    return stale


def prune(args: argparse.Namespace) -> Dict[str, Any]:
    """清理旧版本生成目录；默认 dry-run，只删除“完全由本插件生成”的版本号目录。"""
    root = resolve_path(args.root)
    manifest = load_manifest()
    plugin_name = manifest.get("name")
    release = manifest.get("release")
    if not isinstance(plugin_name, str) or not isinstance(release, str):
        raise SystemExit("manifest.json 缺 name/release")
    if os.path.islink(root) or not os.path.isdir(root):
        raise SystemExit("prune root 必须是已存在的真实目录（非软链）：%s" % root)
    # 祖先链上的软链（如 macOS 的 `/tmp` → `/private/tmp`）按真实目录处理；
    # 待删目录自身与内部条目仍逐项拒绝软链。
    root = os.path.realpath(root)
    keep = set(args.keep or [])
    keep.add(release)
    removed: List[str] = []
    skipped: List[Dict[str, str]] = []
    for child in sorted(os.listdir(root)):
        path = os.path.join(root, child)
        if child in keep:
            if RELEASE_PATTERN.match(child):
                skipped.append({"path": path, "reason": "当前或显式保留的版本"})
            continue
        reason = _release_dir_reason(path, child, plugin_name)
        if reason:
            if RELEASE_PATTERN.match(child):
                skipped.append({"path": path, "reason": reason})
            continue
        if not args.apply:
            removed.append(path)
            continue
        # 删除前复核一次，避免检查与删除之间被替换。
        reason = _release_dir_reason(path, child, plugin_name)
        if reason:
            skipped.append({"path": path, "reason": reason})
            continue
        shutil.rmtree(path)
        removed.append(path)
    result: Dict[str, Any] = {
        "root": root,
        "release": release,
        "dry_run": not args.apply,
        "removed": removed,
        "skipped": skipped,
    }
    if removed and args.apply:
        result["note"] = (
            "已删除旧版本生成目录；若该版本仍 apply 在某个宿主配置里，它已无法再按旧清单卸载，"
            "请用当前版本重新 generate/apply 覆盖。"
        )
    return result


def _iter_entries(value: Any) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("command"), str):
            entries.append(value)
        for item in value.values():
            entries.extend(_iter_entries(item))
    elif isinstance(value, list):
        for item in value:
            entries.extend(_iter_entries(item))
    return entries


MAX_TARGET_BYTES = 8 * 1024 * 1024


def _secure_target(path: str) -> Tuple[int, str, bytes, Dict[str, Any]]:
    """在任何备份/写入之前核验目标与父链，返回 (父目录 fd, 文件名, 原字节, JSON)。

    逐层 `dir_fd + O_NOFOLLOW` 打开目标父链、`O_NOFOLLOW` 打开目标本身：目标或
    任一祖先目录是软链、目标不是普通文件或存在硬链时直接拒绝，且不留下备份或
    临时文件。调用方负责关闭返回的 fd。
    """
    directory, name = os.path.split(path)
    if not name or name in {".", ".."}:
        raise SystemExit("目标配置路径缺少文件名")
    dir_fd, reason = paths.open_secure_directory(directory or os.curdir)
    if dir_fd is None:
        raise SystemExit("目标父目录无法逐层核验（%s）：%s" % (reason, directory))
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            file_fd = os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            raise SystemExit("目标配置必须是已存在的普通文件（非软链）")
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise SystemExit("目标配置必须是已存在的普通文件（非软链）")
            raise SystemExit("目标配置不可读")
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise SystemExit("目标配置必须是已存在的普通文件（非软链）")
            if info.st_nlink != 1:
                raise SystemExit("目标配置存在硬链，拒绝在无法确认唯一性的路径上写入")
            if info.st_size > MAX_TARGET_BYTES:
                raise SystemExit("目标配置过大")
            try:
                original = os.read(file_fd, MAX_TARGET_BYTES)
            except OSError:
                raise SystemExit("目标配置不可读")
        finally:
            os.close(file_fd)
    except BaseException:
        os.close(dir_fd)
        raise
    try:
        value = json.loads(original.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        os.close(dir_fd)
        raise SystemExit("目标配置不是合法 JSON")
    if not isinstance(value, dict):
        os.close(dir_fd)
        raise SystemExit("目标配置不是 JSON 对象")
    return dir_fd, name, original, value


def _write_all(fd: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])


def _backup(dir_fd: int, name: str, original: bytes) -> str:
    """在已核验的目录内按原字节保存备份（0600、非软链、不跟随）。"""
    backup = "%s.easydev-backup-%d" % (name, os.getpid())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(backup, flags, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, original)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(backup, dir_fd=dir_fd)
        raise
    os.close(fd)
    return backup


def _atomic_write(dir_fd: int, name: str, value: Dict[str, Any]) -> None:
    """在已核验的目录内原子替换目标：同目录临时文件 + rename（dir_fd）。"""
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = ".%s.%d.tmp" % (name, os.getpid())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600, dir_fd=dir_fd)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def load_install_manifest(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise SystemExit("缺少安装清单：%s（先用 generate 生成）" % path)
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SystemExit("安装清单不是对象")
    return value


def manifest_digests(manifest: Dict[str, Any]) -> Dict[str, Any]:
    current = {}
    known = {}
    for item in manifest.get("entries") or []:
        if isinstance(item, dict) and isinstance(item.get("digest"), str):
            current[item["digest"]] = item
            known[item["digest"]] = item
    for item in manifest.get("previous_entries") or []:
        if isinstance(item, dict) and isinstance(item.get("digest"), str):
            known.setdefault(item["digest"], item)
    identities = {item.get("identity") for item in current.values() if item.get("identity")}
    return {"current": current, "known": known, "identities": identities}


def _manifest_path_for(args: argparse.Namespace) -> str:
    if getattr(args, "manifest", None):
        return resolve_path(args.manifest)
    if getattr(args, "source", None):
        return os.path.join(os.path.dirname(resolve_path(args.source)), INSTALL_MANIFEST_NAME)
    raise SystemExit("需要 --manifest <install-manifest.json> 或 --source <生成的配置>")


def apply_config(args: argparse.Namespace) -> Dict[str, Any]:
    target_path = resolve_path(args.target)
    manifest = load_install_manifest(_manifest_path_for(args))
    digests = manifest_digests(manifest)
    if not digests["current"]:
        raise SystemExit("安装清单没有可应用的条目")
    # 按 (Hook 组, 事件) 分组：命名组的条目只能进同一个命名组，不能掉进顶层 hooks。
    addition: Dict[Tuple[Optional[str], str], List[Any]] = {}
    for digest, item in digests["current"].items():
        addition.setdefault((item.get("group"), str(item.get("event"))), []).append(item.get("entry"))
    # 目标与父链在任何备份/写入之前完成验证（不跟随软链），拒绝时不留备份。
    dir_fd, name, original, target = _secure_target(target_path)
    try:
        conflicts: List[str] = []
        for group, hooks in _hook_groups(target):
            for event, bucket in hooks.items():
                if not isinstance(bucket, list):
                    raise SystemExit("目标事件 %s 不是数组" % event)
                for entry in bucket:
                    digest = _entry_digest(event, entry)
                    if digest in digests["known"]:
                        continue
                    identity = _command_core(_entry_command(entry))
                    if identity and identity in digests["identities"]:
                        conflicts.append("%s（%s）" % (event, identity))
        if conflicts:
            raise SystemExit(
                "目标已存在同标识但内容不同的 EasyDev 条目：%s；请人工核对（保留用户修改）或先卸载"
                % "、".join(conflicts)
            )
        changed = False
        for (group, event), entries in addition.items():
            key = "hooks" if group is None else group
            target_hooks = target.setdefault(key, {})
            if not isinstance(target_hooks, dict):
                raise SystemExit("目标 %s 不是对象" % key)
            bucket = target_hooks.setdefault(event, [])
            if not isinstance(bucket, list):
                raise SystemExit("目标事件 %s 不是数组" % event)
            kept: List[Any] = []
            for entry in bucket:
                digest = _entry_digest(event, entry)
                if digest in digests["current"]:
                    kept.append(entry)
                elif digest in digests["known"]:
                    # 上一代生成物（如 probe→enforce）：替换为新条目，不追加两套。
                    changed = True
                else:
                    kept.append(entry)
            bucket[:] = kept
            for entry in entries:
                digest = _entry_digest(event, entry)
                if any(_entry_digest(event, existing) == digest for existing in bucket):
                    continue
                bucket.append(entry)
                changed = True
        if not changed:
            return {"changed": False, "target": target_path, "backup": None}
        backup = os.path.join(os.path.dirname(target_path), _backup(dir_fd, name, original))
        _atomic_write(dir_fd, name, target)
        return {"changed": True, "target": target_path, "backup": backup}
    finally:
        os.close(dir_fd)


def uninstall(args: argparse.Namespace) -> Dict[str, Any]:
    target_path = resolve_path(args.target)
    manifest = load_install_manifest(_manifest_path_for(args))
    digests = manifest_digests(manifest)
    dir_fd, name, original, target = _secure_target(target_path)
    try:
        removed = 0
        for group, hooks in _hook_groups(target):
            for event in list(hooks):
                bucket = hooks[event]
                if not isinstance(bucket, list):
                    continue
                kept: List[Any] = []
                for entry in bucket:
                    if _entry_digest(event, entry) in digests["known"]:
                        removed += 1
                    else:
                        # 用户修改过的条目或其他 Hook 保持原样。
                        kept.append(entry)
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
            if group is not None and not hooks:
                # 命名组已空：整组移除，恢复到安装前的配置形状。
                del target[group]
        if not removed:
            return {"changed": False, "target": target_path, "removed": 0}
        backup = os.path.join(os.path.dirname(target_path), _backup(dir_fd, name, original))
        _atomic_write(dir_fd, name, target)
        return {"changed": True, "target": target_path, "removed": removed, "backup": backup}
    finally:
        os.close(dir_fd)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EasyDev hooks generator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--host", choices=("antigravity", "codex", "dsh", "pi"), required=True)
    generate_parser.add_argument("--plugin-root", required=True)
    generate_parser.add_argument("--skill-root", required=True)
    output_target = generate_parser.add_mutually_exclusive_group(required=True)
    output_target.add_argument("--output", default=None)
    output_target.add_argument("--output-root", default=None)
    generate_parser.add_argument("--mode", choices=("probe", "enforce"), default="enforce")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--target", required=True)
    apply_parser.add_argument("--source", default=None)
    apply_parser.add_argument("--manifest", default=None)
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--target", required=True)
    uninstall_parser.add_argument("--manifest", default=None)
    uninstall_parser.add_argument("--source", default=None)
    prune_parser = subparsers.add_parser("prune")
    prune_parser.add_argument("--root", required=True)
    prune_parser.add_argument("--keep", action="append", default=None)
    prune_parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "generate":
        if getattr(args, "plugin_root", None):
            args.plugin_root = resolve_path(args.plugin_root)
        result = generate(args)
    elif args.command == "apply":
        result = apply_config(args)
    elif args.command == "prune":
        result = prune(args)
    else:
        result = uninstall(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
