#!/usr/bin/env python3
"""Pre-execution guardrail shared across AI coding harnesses (Python 3.9+ compatible).

Enforces deterministic rules from AGENTS.md / antigravity.md:
- Non-interactive stdin closure (`</dev/null`) & blocking interactive command denial
- High-impact Git / destructive `rm -rf` confirmation (`force_ask`)
- Secret / credential scanning on writes, commands, and `git commit` staged diffs (`deny`)
- Hard timeout enforcement on network / probe commands (`deny`)
- Shell `sleep` prohibition on Antigravity (`deny`)
- Sensitive credential directory access confirmation (`force_ask`)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from typing import Iterable, List, Optional, Tuple

# Order matters: longer/specific tokens first.
QUOTED_STR_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"])*\"")

SECRET_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("Private Key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("GitHub Fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("Stripe Key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("API Secret Key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("Slack Token", re.compile(r"\bxox[bp]-[A-Za-z0-9-]{10,}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)

SECRET_PLACEHOLDERS = {
    "AKIAIOSFODNN7EXAMPLE",
}

INTERACTIVE_CMDS = {
    "less",
    "more",
    "vim",
    "vi",
    "nano",
    "emacs",
    "htop",
    "script",
    "telnet",
    "ftp",
}

NO_STDIN_CMDS = {
    "cd",
    "pushd",
    "popd",
    "export",
    "unset",
    "alias",
    "mkdir",
    "rmdir",
    "touch",
    "cp",
    "mv",
    "ln",
    "chmod",
    "chown",
    "ls",
    "pwd",
    "echo",
    "printf",
    "true",
    "false",
    "test",
    "[",
    "[[",
    "which",
    "basename",
    "dirname",
    "realpath",
    "date",
    "uname",
    "whoami",
    "id",
    "printenv",
    "sleep",
    "kill",
    "find",
    "stat",
    "du",
    "df",
    "ps",
    "lsof",
    "tree",
    "for",
    "while",
    "until",
    "if",
    "case",
    "do",
    "then",
    "else",
    "elif",
    "fi",
    "done",
    "esac",
}

# 这些命令的每个位置参数都是输入文件；带模式/表达式的 grep/sed/awk/jq/rg 不在此列。
FILE_OPERAND_CMDS = {
    "cat",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "cut",
    "diff",
    "comm",
    "paste",
    "nl",
    "od",
    "xxd",
    "base64",
    "shasum",
    "sha256sum",
    "md5sum",
    "iconv",
    "strings",
    "column",
    "fold",
    "file",
}

PREFIX_WRAPPERS = {"sudo", "env", "command", "exec", "nohup", "nice", "time", "xargs"}
XARGS_VALUE_FLAGS = ("-n", "-I", "-P", "-L", "-s", "-d", "-E", "-a")
# `-n` 是 sudo 的布尔开关（非交互），不在带值集合里；`nice -n` 才带值。
SUDO_VALUE_FLAGS = ("-u", "-g", "-h", "-p", "-r", "-t", "-C", "-D", "-R", "-T", "-U")
ENV_VALUE_FLAGS = ("-u", "-C", "-S", "--unset", "--chdir")
GIT_VALUE_OPTS = ("--git-dir", "--work-tree", "--namespace", "--super-prefix")
# `git commit` options whose value is not a pathspec; the value must not trigger the worktree scan.
COMMIT_VALUE_OPTS = (
    "-m",
    "-F",
    "-c",
    "-C",
    "-u",
    "--message",
    "--file",
    "--author",
    "--date",
    "--cleanup",
    "--pathspec-from-file",
)

SENSITIVE_DIR_RE = re.compile(
    r"(?:~|\$\{?HOME\}?|/Users/[^/\s\"']+|/home/[^/\s\"']+)/\.(?:ssh|aws|gnupg|netrc|docker|kube|config/gh)(?:/|\b|\s|$)"
    r"|(?:~|\$\{?HOME\}?|/Users/[^/\s\"']+|/home/[^/\s\"']+)/\.netrc\b"
)

# `rm -rf` 的可重建目标：临时目录与缓存/构建产物，删掉后可由脚本或包管理器重建。
RM_DISPOSABLE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".cache",
    ".next",
    "node_modules",
    "dist",
    "build",
    "htmlcov",
    "coverage",
}
RM_TEMP_ROOTS = ("/tmp/", "/var/tmp/", "/private/tmp/", "/private/var/tmp/")
RM_TEMP_NAME_RE = re.compile(r"^\.tmp(?:[-_.].*)?$|^tmp[-_].*$")
# `$VAR`、通配符与花括号展开的实际目标无法在 hook 侧静态确定，一律按高危处理。
RM_EXPANSION_RE = re.compile(r"[$`*?\[\]{}]")
# `git checkout` / `git restore` 的整仓级路径参数：丢弃范围是整棵树而非单个文件。
GIT_WHOLE_TREE_PATHS = {".", "./"}

# 允许紧贴命令词的无空格写法（`ls</dev/null`），但仍排除 `>&` / `&>` 这类输出重定向。
STDIN_REDIRECTION_RE = re.compile(r"0?<(?:&-|\s*[^\s<(&>])|<<<?")
SHORT_OPT_WITH_C_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")


def _heredoc_starts(line: str) -> List[Tuple[str, bool]]:
    """引号感知扫描一行的 heredoc 起点，返回 [(定界符, 定界符是否带引号)]。

    单/双引号内的 `<<` 与引号外的 `#` 注释不是 heredoc 起点，因此 `echo "<<EOF"`
    不会吞掉后续行；`<<<`（herestring）也不算。
    """
    found: List[Tuple[str, bool]] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            j = line.find("'", i + 1)
            i = n if j == -1 else j + 1
            continue
        if ch == '"':
            i += 1
            while i < n and line[i] != '"':
                i += 2 if line[i] == "\\" else 1
            i += 1
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace() or line[i - 1] in ";|&"):
            break
        if line.startswith("<<", i) and not line.startswith("<<<", i):
            j = i + 2
            if j < n and line[j] == "-":
                j += 1
            while j < n and line[j] in " \t":
                j += 1
            if j < n and line[j] in "'\"":
                quote = line[j]
                k = line.find(quote, j + 1)
                if k == -1:
                    i = j
                    continue
                found.append((line[j + 1 : k], True))
                i = k + 1
                continue
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", line[j:])
            if m:
                found.append((m.group(0), False))
                i = j + len(m.group(0))
                continue
        i += 1
    return found


def _strip_heredoc_bodies(cmd: str) -> Tuple[str, List[str]]:
    """移除 heredoc 正文，返回 (清理后的命令, 未加引号 heredoc 的正文)。

    正文本身不是命令，全部不留在返回的命令里；未加引号定界符的正文仍会做
    `$(...)` / 反引号替换，由调用方按隐藏命令评估，带引号定界符的正文直接丢弃。
    """
    lines = cmd.splitlines()
    if len(lines) <= 1:
        return cmd, []
    out: List[str] = []
    pending: List[Tuple[str, bool]] = []
    bodies: List[str] = []
    body: List[str] = []
    for line in lines:
        if pending:
            delimiter, quoted = pending[0]
            if line.strip() == delimiter:
                if not quoted:
                    bodies.append("\n".join(body))
                body = []
                pending.pop(0)
                continue
            body.append(line)
            continue
        out.append(line)
        pending.extend(_heredoc_starts(line))
    # 未写结束定界符时 bash 仍把余下各行当正文执行其中的替换，不能整段丢弃。
    if pending and not pending[0][1] and body:
        bodies.append("\n".join(body))
    return "\n".join(out), bodies


def _strip_comments(text: str) -> str:
    """只剔除引号外的 `#` 注释；引号内容原样保留（替换仍在其中）。"""
    result: List[str] = []
    i = 0
    n = len(text)
    in_sq = False
    in_dq = False
    while i < n:
        ch = text[i]
        if in_sq:
            result.append(ch)
            if ch == "'":
                in_sq = False
            i += 1
            continue
        if in_dq:
            result.append(ch)
            if ch == "\\" and i + 1 < n:
                result.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True
        elif ch == '"':
            in_dq = True
        elif ch == "#" and (i == 0 or text[i - 1].isspace() or text[i - 1] in ";|&"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _strip_comments_and_quotes(text: str) -> str:
    """Remove single/double quoted literals and shell `#` comments outside quotes."""
    result: List[str] = []
    i = 0
    n = len(text)
    in_sq = False
    in_dq = False
    while i < n:
        ch = text[i]
        if in_sq:
            if ch == "'":
                in_sq = False
                result.append(" ")
            i += 1
            continue
        if in_dq:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_dq = False
                result.append(" ")
            i += 1
            continue
        if ch == "'":
            in_sq = True
            i += 1
        elif ch == '"':
            in_dq = True
            i += 1
        elif ch == "#" and (i == 0 or text[i - 1].isspace() or text[i - 1] in ";|&"):
            while i < n and text[i] != "\n":
                i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def _strip_single_quotes(text: str) -> str:
    """Blank single-quoted spans so `$(...)` / backticks inside them are not treated as commands."""
    out: List[str] = []
    i = 0
    n = len(text)
    in_dq = False
    while i < n:
        ch = text[i]
        if in_dq:
            if ch == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            if ch == '"':
                in_dq = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dq = True
            out.append(ch)
            i += 1
            continue
        if ch != "'":
            out.append(ch)
            i += 1
            continue
        i += 1
        while i < n and text[i] != "'":
            i += 1
        i += 1
        out.append(" ")
    return "".join(out)


def _mask_quotes(text: str) -> str:
    """Replace quoted literals with `Q` so redirections inside them are not counted."""
    return QUOTED_STR_RE.sub("Q", text)


def _iter_substitutions(text: str) -> List[str]:
    """Yield inner scripts of `$(...)` (parenthesis-balanced) and backticks."""
    found: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("$(", i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            inner = text[i + 2 : j - 1] if depth == 0 else text[i + 2 : j]
            if inner.strip():
                found.append(inner)
            i = j
            continue
        if text[i] == "`":
            j = text.find("`", i + 1)
            if j == -1:
                break
            inner = text[i + 1 : j]
            if inner.strip():
                found.append(inner)
            i = j + 1
            continue
        i += 1
    return found


def _iter_process_substitutions(text: str) -> List[str]:
    """Yield inner commands of unquoted `<( ... )` / `>( ... )` process substitutions."""
    found: List[str] = []
    i = 0
    n = len(text)
    in_sq = False
    in_dq = False
    while i < n:
        ch = text[i]
        if in_sq:
            in_sq = ch != "'"
            i += 1
            continue
        if in_dq:
            if ch == "\\":
                i += 2
                continue
            in_dq = ch != '"'
            i += 1
            continue
        if ch == "'":
            in_sq = True
            i += 1
            continue
        if ch == '"':
            in_dq = True
            i += 1
            continue
        if ch in "<>" and text.startswith("(", i + 1):
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            inner = text[i + 2 : j - 1] if depth == 0 else text[i + 2 : j]
            if inner.strip():
                found.append(inner)
            i = j
            continue
        i += 1
    return found


def _has_path_arg(args: List[str], cwd: str) -> bool:
    """True when some non-flag argument names an existing path, i.e. input comes from a file."""
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if re.fullmatch(r"\d*[<>]+", arg):
            i += 1  # 重定向目标不是输入文件
            continue
        if re.match(r"^\d*[<>]", arg) or arg.startswith("-"):
            continue
        if os.path.exists(arg if os.path.isabs(arg) else os.path.join(cwd, arg)):
            return True
    return False


def _split_outside_quotes(text: str, seps: Tuple[str, ...]) -> List[str]:
    """Split `text` on separators in `seps` outside quotes, ignoring `2>&1` / `>&2` / `&>`."""
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(text)
    in_sq = False
    in_dq = False
    depth = 0
    ordered_seps = sorted(seps, key=len, reverse=True)

    while i < n:
        ch = text[i]
        if in_sq:
            buf.append(ch)
            if ch == "'":
                in_sq = False
            i += 1
            continue
        if in_dq:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dq = True
            buf.append(ch)
            i += 1
            continue

        # Do not treat `2>&1` / `&>` / `3<&0` as a statement separator.
        if text[i : i + 2] in ("<&", ">&", "&>"):
            buf.append(text[i : i + 2])
            i += 2
            continue

        if ch == "\\" and i + 1 < n:
            # 转义字符（`\(`、`\;`）是字面量，不参与括号计数也不当分隔符。
            buf.append(text[i : i + 2])
            i += 2
            continue

        if ch in "()":
            # 括号分组内部不按 seps 拆分，整组交给 _unwrap_group 递归处理。
            depth = depth + 1 if ch == "(" else max(depth - 1, 0)
            buf.append(ch)
            i += 1
            continue

        matched_sep = None
        for sep in (ordered_seps if depth == 0 else ()):
            if text.startswith(sep, i):
                # Avoid splitting `|` when it's `||` if `||` is not in seps.
                if sep == "|" and text.startswith("||", i):
                    continue
                matched_sep = sep
                break
        if matched_sep:
            parts.append("".join(buf))
            buf = []
            i += len(matched_sep)
            continue

        buf.append(ch)
        i += 1

    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _tokenize(segment: str) -> List[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _unwrap_group(text: str) -> str:
    """剥掉包住整条语句的 `( ... )` / `{ ... }` 外壳，保留其后原本的重定向等尾部。"""
    stripped = text.strip()
    opener = stripped[:1]
    if opener not in ("(", "{"):
        return text
    closer = ")" if opener == "(" else "}"
    # 等长挖空引号内容，使下标与原文对齐，避免括号里的引号干扰配对。
    masked = QUOTED_STR_RE.sub(lambda m: " " * len(m.group(0)), stripped)
    depth = 0
    for i, ch in enumerate(masked):
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                inner = stripped[1:i].strip().rstrip(";").strip()
                return f"{inner} {stripped[i + 1 :]}".strip()
    return text


def _unwrap_prefixes(tokens: List[str]) -> Tuple[str, List[str], bool]:
    """Strip env assignments and wrapper commands; return (base_cmd, args, has_timeout_wrapper)."""
    i = 0
    has_timeout = False
    while i < len(tokens):
        tok = tokens[i]
        # 子 shell `(cmd ...)`、花括号组 `{ cmd; }` 与取反 `! cmd` 的记号前缀不是命令名。
        stripped = tok.lstrip("({!}")
        if stripped != tok:
            if not stripped:
                i += 1
                continue
            tokens[i] = tok = stripped
        if "=" in tok and not tok.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            continue
        base = os.path.basename(tok)
        if base in ("timeout", "gtimeout"):
            has_timeout = True
            i += 1
            # Skip flags like `-k 1` or `-s TERM` and the duration arg.
            while i < len(tokens) and tokens[i].startswith("-"):
                if tokens[i] in ("-k", "-s", "--kill-after", "--signal") and i + 1 < len(tokens):
                    i += 2
                else:
                    i += 1
            if i < len(tokens):
                i += 1
            continue
        if base in PREFIX_WRAPPERS:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                value_flag = tokens[i].split("=", 1)[0]
                if base == "sudo" and tokens[i] in SUDO_VALUE_FLAGS and i + 1 < len(tokens):
                    i += 2
                elif base == "nice" and tokens[i] == "-n" and i + 1 < len(tokens):
                    i += 2
                elif base == "xargs" and tokens[i] in XARGS_VALUE_FLAGS and i + 1 < len(tokens):
                    i += 2
                elif base == "env" and value_flag in ENV_VALUE_FLAGS:
                    i += 1 if "=" in tokens[i] or i + 1 >= len(tokens) else 2
                else:
                    i += 1
            continue
        if base in ("do", "then", "else", "elif"):
            # 控制流关键字不是命令，真实命令在它后面。
            i += 1
            continue
        return base, tokens[i + 1 :], has_timeout
    return "", [], has_timeout


def scan_secrets(texts: Iterable[str], location: str) -> Optional[str]:
    """Scan texts for high-confidence secret patterns without echoing matched secret."""
    for text in texts:
        if not text:
            continue
        for label, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                if m.group(0) in SECRET_PLACEHOLDERS:
                    continue
                return f"检测到疑似敏感凭据（{label}）位于 {location}，已阻断以防泄露（§4）。"
    return None


def _scan_git_commit_diff(cwd: str, args: List[str]) -> Optional[str]:
    """Scan staged (and `-a` / path-specified) diffs before `git commit`."""
    target_cwd = cwd
    i = 0
    while i < len(args):
        if args[i] == "-C" and i + 1 < len(args):
            target_cwd = os.path.join(target_cwd, args[i + 1]) if not os.path.isabs(args[i + 1]) else args[i + 1]
            i += 2
        elif args[i] == "-c" and i + 1 < len(args):
            i += 2
        elif args[i] in GIT_VALUE_OPTS and i + 1 < len(args):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            break

    # `commit` subcommand args follow `commit`.
    commit_args = args[i + 1 :] if i < len(args) and args[i] == "commit" else []
    has_all = False
    has_path = False
    j = 0
    while j < len(commit_args):
        arg = commit_args[j]
        if arg in COMMIT_VALUE_OPTS:
            j += 2
            continue
        if re.fullmatch(r"\d*[<>]+", arg):
            j += 2
            continue
        if re.match(r"^\d*[<>]", arg):
            j += 1
            continue
        if arg.startswith("-") and not arg.startswith("--"):
            # 短选项簇按字符解析：带值选项的值可紧贴（`-mmsg`）或占用下一个参数（`-m msg`）。
            flags = arg[1:]
            value_at = next((k for k, c in enumerate(flags) if c in "mFcCu"), len(flags))
            if "a" in flags[:value_at]:
                has_all = True
            j += 2 if value_at == len(flags) - 1 else 1
            continue
        elif arg == "--all":
            has_all = True
        elif not arg.startswith("-"):
            has_path = True
        j += 1

    diff_cmds = [["git", "-C", target_cwd, "diff", "--cached"]]
    if has_all or has_path:
        diff_cmds.append(["git", "-C", target_cwd, "diff"])

    for dcmd in diff_cmds:
        proc = subprocess.run(dcmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4, check=False)
        if proc.returncode == 0 and proc.stdout:
            added = "\n".join(
                line[1:]
                for line in proc.stdout.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            hit = scan_secrets([added], "git 待提交变更")
            if hit:
                return hit
    return None


def _git_staged_only(sub_args: List[str]) -> bool:
    """`git restore --staged` 只动暂存区（可逆），未带 `--worktree` 时不碰工作区。"""
    staged = worktree = False
    for arg in sub_args:
        if arg == "--staged":
            staged = True
        elif arg == "--worktree":
            worktree = True
        elif arg.startswith("-") and not arg.startswith("--"):
            staged = staged or "S" in arg[1:]
            worktree = worktree or "W" in arg[1:]
    return staged and not worktree


def _git_wide_discard(sub: str, sub_args: List[str]) -> bool:
    """是否整仓级丢弃工作区修改：`-f` 或整仓路径；`restore --staged` 例外。"""
    if sub == "restore" and _git_staged_only(sub_args):
        return False
    force = any(a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a) for a in sub_args)
    return force or _git_whole_tree_path(sub_args)


def _git_whole_tree_path(sub_args: List[str]) -> bool:
    """`. `、`./`、`:/` 等整仓级路径参数（`--` 前后的位置参数都计入）。"""
    return any(
        arg in GIT_WHOLE_TREE_PATHS or arg.startswith(":/")
        for arg in sub_args
        if not arg.startswith("-")
    )


def _check_git_action(args: List[str], cwd: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (decision, reason) for git subcommands."""
    i = 0
    while i < len(args):
        if args[i] in ("-C", "-c") and i + 1 < len(args):
            i += 2
        elif args[i] in GIT_VALUE_OPTS and i + 1 < len(args):
            i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            break
    if i >= len(args):
        return None, None

    sub = args[i]
    sub_args = args[i + 1 :]

    if sub == "commit":
        secret_hit = _scan_git_commit_diff(cwd, args)
        if secret_hit:
            return "deny", secret_hit
        return "force_ask", "执行 `git commit` 须经用户明确要求或确认（§7）。"

    if sub == "push":
        return "force_ask", "执行 `git push` 须经用户明确要求或确认（§7）。"

    if sub == "reset" and "--hard" in sub_args:
        return "force_ask", "执行 `git reset --hard` 会丢弃未提交改动，请确认（§1）。"

    if sub == "clean":
        has_force = any(
            a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a)
            for a in sub_args
        )
        has_dry = any(
            a == "--dry-run" or (a.startswith("-") and not a.startswith("--") and "n" in a)
            for a in sub_args
        )
        if has_force and not has_dry:
            return "force_ask", "执行 `git clean -f` 会永久删除未跟踪文件，请确认（§1）。"

    # 单文件丢弃与切分支可逆，只有整仓级丢弃（`.`/`./`/`:/`/`-f`）才需确认。
    if sub in ("checkout", "restore") and _git_wide_discard(sub, sub_args):
        return "force_ask", f"执行 `git {sub}` 整仓丢弃工作区修改不可逆，请确认（§1）。"

    if sub == "stash" and any(a in ("drop", "clear") for a in sub_args):
        return "force_ask", "执行 `git stash drop/clear` 不可逆，请确认（§1）。"

    if sub == "branch" and "-D" in sub_args:
        return "force_ask", "执行 `git branch -D` 会强制删除分支，请确认（§1）。"

    return None, None


def _rm_target_is_disposable(target: str) -> bool:
    """临时目录与缓存/构建产物可重建，删除它们不算不可逆高危。"""
    if not target or target.startswith("~") or RM_EXPANSION_RE.search(target):
        return False
    path = os.path.normpath(target).replace(os.sep, "/")
    if path.startswith("/"):
        # 规范化后仍须落在系统临时目录的子路径上，`/tmp` 本身不算。
        return any(path.startswith(root) for root in RM_TEMP_ROOTS)
    parts = path.split("/")
    if ".." in parts:
        # `../build` 之类会越出当前目录，删的是别的目录，不能当可重建缓存。
        return False
    return (
        RM_TEMP_NAME_RE.match(parts[0]) is not None
        or parts[0] in RM_DISPOSABLE_DIRS
        or parts[-1] in RM_DISPOSABLE_DIRS
    )


def _check_rm_force(args: List[str]) -> Optional[str]:
    has_r = any(a == "--recursive" or (a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a)) for a in args)
    has_f = any(a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a) for a in args)
    if not (has_r and has_f):
        return None

    targets: List[str] = []
    skip_next = False
    after_double_dash = False
    for arg in args:
        if skip_next:
            skip_next = False
        elif re.fullmatch(r"\d*[<>]+", arg):
            skip_next = True  # 重定向目标不是删除目标
        elif re.match(r"^\d*[<>]", arg):
            continue
        elif arg == "--":
            after_double_dash = True  # 此后连 `-x` 也是删除目标
        elif arg.startswith("-") and not after_double_dash:
            continue
        else:
            targets.append(arg)

    # 全部目标都是可重建的临时/缓存路径才放行，防止 `rm -rf .tmp-a /etc` 夹带。
    if targets and all(_rm_target_is_disposable(t) for t in targets):
        return None
    return "执行递归强制删除 `rm -rf` 属于不可逆高危操作，请确认（§1）。"


def _check_network_timeout(cmd: str, args: List[str], has_timeout_wrapper: bool) -> Optional[str]:
    if has_timeout_wrapper:
        return None
    if cmd == "curl":
        if any(a == "-m" or a.startswith("-m") and a[2:].isdigit() or a == "--max-time" or a.startswith("--max-time=") for a in args):
            return None
        return "网络命令 `curl` 必须显式设置全流程硬超时（如 `-m 10` / `--max-time 10` 或外层 `timeout`/`gtimeout`）（§2）。"
    if cmd == "wget":
        if any(a in ("-T", "--timeout") or a.startswith("--timeout=") for a in args):
            return None
        return "网络命令 `wget` 必须显式设置硬超时（如 `-T 10` 或外层 `timeout`/`gtimeout`）（§2）。"
    if cmd in ("ssh", "scp"):
        return f"远程命令 `{cmd}` 必须在外层加硬超时（Linux 用 `timeout`，macOS 用 `gtimeout`）（§2）。"
    if cmd == "nc":
        if any(a in ("-w", "-G") for a in args):
            return None
        return "网络探测 `nc` 必须设置超时（`-w <秒>` 或外层 `timeout`/`gtimeout`）（§2）。"
    if cmd == "ping":
        if any(a == "-c" or a.startswith("-c") and a[2:].isdigit() for a in args):
            return None
        return "`ping` 必须指定 `-c <次数>` 或外层 `timeout`/`gtimeout`，否则会无限阻塞（§2）。"
    return None


def evaluate_command(cmd: str, cwd: str, host: str, check_stdin: bool = True) -> Tuple[Optional[str], Optional[str]]:
    """Evaluate shell command; returns (decision, reason) where decision is deny | force_ask | None.

    `check_stdin=False` is used for commands whose stdin is the shell's own (command substitution),
    where the outer `</dev/null` does not apply; only deny / force_ask rules are enforced there.
    """
    stripped = cmd.strip()
    if not stripped:
        return None, None

    # 子 shell / 花括号组先剥外壳：否则 `(git push)` 的子命令会被解析成 `push)`。
    group_inner = _unwrap_group(stripped)
    if group_inner != stripped:
        return evaluate_command(group_inner, cwd=cwd, host=host, check_stdin=check_stdin)

    secret_hit = scan_secrets([stripped], "命令行参数")
    if secret_hit:
        return "deny", secret_hit

    body_stripped, unquoted_heredoc_bodies = _strip_heredoc_bodies(stripped)
    statements = _split_outside_quotes(body_stripped, ("&&", "||", "&", ";", "\n"))

    best_decision: Optional[str] = None
    best_reason: Optional[str] = None

    def record(dec: Optional[str], reason: Optional[str]) -> None:
        nonlocal best_decision, best_reason
        if dec == "deny":
            best_decision, best_reason = "deny", reason
        elif dec == "force_ask" and best_decision is None:
            best_decision, best_reason = "force_ask", reason

    # 未加引号定界符的 heredoc 正文仍是数据，只有其中的替换会被执行。
    for heredoc_body in unquoted_heredoc_bodies:
        for inner in _iter_substitutions(heredoc_body):
            sub_dec, sub_reason = evaluate_command(inner, cwd=cwd, host=host, check_stdin=False)
            record(sub_dec, sub_reason)
            if best_decision == "deny":
                return best_decision, best_reason

    for stmt in statements:
        # 链条后段的组（`cd x && (git push)`）先剥外壳再递归，组内多语句与多层括号一并覆盖。
        unwrapped = _unwrap_group(stmt)
        if unwrapped != stmt:
            sub_dec, sub_reason = evaluate_command(unwrapped, cwd=cwd, host=host, check_stdin=check_stdin)
            record(sub_dec, sub_reason)
            if best_decision == "deny":
                return best_decision, best_reason
            continue
        stmt_unquoted = _strip_comments_and_quotes(stmt).strip()
        if not stmt_unquoted:
            continue

        # Hidden commands: `$(...)`、反引号与进程替换先于外层命令执行。
        probe = _strip_single_quotes(_strip_comments(stmt))
        for inner in _iter_substitutions(probe) + _iter_process_substitutions(probe):
            sub_dec, sub_reason = evaluate_command(inner, cwd=cwd, host=host, check_stdin=False)
            record(sub_dec, sub_reason)
            if best_decision == "deny":
                return best_decision, best_reason

        pipe_segments = _split_outside_quotes(stmt, ("|",))

        for seg_idx, seg in enumerate(pipe_segments):
            tokens = _tokenize(seg)
            base, args, has_timeout = _unwrap_prefixes(tokens)
            if not base:
                continue
            seg_has_stdin = bool(STDIN_REDIRECTION_RE.search(_mask_quotes(seg)))

            # Recursively check `sh -c` / `bash -lc` / `zsh -ec` inner script.
            if base in ("sh", "bash", "zsh"):
                c_idx = next(
                    (k for k, a in enumerate(args) if SHORT_OPT_WITH_C_RE.fullmatch(a)),
                    None,
                )
                if c_idx is not None and c_idx + 1 < len(args):
                    script = args[c_idx + 1]
                    if script == "--" and c_idx + 2 < len(args):
                        script = args[c_idx + 2]
                    sub_dec, sub_reason = evaluate_command(
                        script,
                        cwd=cwd,
                        host=host,
                        check_stdin=check_stdin and not seg_has_stdin and seg_idx == 0,
                    )
                    record(sub_dec, sub_reason)
                    if best_decision == "deny":
                        return best_decision, best_reason

            if base in INTERACTIVE_CMDS:
                return "deny", f"禁止执行可能阻塞的交互式命令 `{base}`。请改用非交互替代命令。"

            if base == "top" and not any(a == "-l" or a.startswith("-l") or a == "-b" or a.startswith("-b") for a in args):
                return "deny", "禁止无批处理参数执行交互式 `top`（macOS 请用 `top -l 1`，Linux 请用 `top -b -n 1`）。"

            if base == "sleep" and host == "antigravity":
                return "deny", "禁止使用 shell `sleep` 作为定时器或轮询，请改用 `schedule` 或响应式等待。"

            timeout_err = _check_network_timeout(base, args, has_timeout)
            if timeout_err:
                return "deny", timeout_err

            # Stdin check applies only to the head of each pipeline, using that segment's own redirects.
            if check_stdin and seg_idx == 0 and base not in NO_STDIN_CMDS:
                if not seg_has_stdin and not (
                    base in FILE_OPERAND_CMDS and _has_path_arg(args, cwd)
                ):
                    return (
                        "deny",
                        "无前置数据输入的命令必须显式封闭 stdin（例如在命令末尾加 `</dev/null`），"
                        "严禁悬空 stdin 等待输入。请重写命令后重试。",
                    )

            # Sensitive credential directory check (allow `ssh -i` / `scp -i` / `ssh-add` / `ssh-keygen`).
            key_command = (base in ("ssh", "scp") and "-i" in args) or base in ("ssh-add", "ssh-keygen")
            if SENSITIVE_DIR_RE.search(seg) and not key_command:
                record("force_ask", "命令涉及敏感凭据目录（如 `.ssh`/`.aws`/`.gnupg`），须由用户明确授权（§3）。")

            if base == "git":
                g_dec, g_reason = _check_git_action(args, cwd)
                record(g_dec, g_reason)
                if best_decision == "deny":
                    return best_decision, best_reason

            if base == "rm":
                rm_reason = _check_rm_force(args)
                if rm_reason:
                    record("force_ask", rm_reason)

            # Track `cd <dir>` within the same statement chain for later `git commit`.
            if base == "cd" and args and not args[0].startswith("-"):
                cwd = os.path.join(cwd, args[0]) if not os.path.isabs(args[0]) else args[0]

    return best_decision, best_reason


def _extract_write_texts(payload: dict) -> Tuple[str, List[str]]:
    """Extract (target_path, texts) for file write/edit tools across Antigravity & Pi."""
    args = payload.get("toolCall", {}).get("args")
    if not isinstance(args, dict):
        args = payload.get("input") if isinstance(payload.get("input"), dict) else {}

    path = str(args.get("Path") or args.get("TargetFile") or args.get("path") or "目标文件")
    texts: List[str] = []
    for key in ("Content", "ReplacementContent", "Replacement", "CodeContent", "content", "newText"):
        val = args.get(key)
        if isinstance(val, str):
            texts.append(val)
    for list_key in ("ReplacementChunks", "edits"):
        chunks = args.get(list_key)
        if isinstance(chunks, list):
            for item in chunks:
                if isinstance(item, dict):
                    for sub_key in ("ReplacementContent", "newText"):
                        if isinstance(item.get(sub_key), str):
                            texts.append(item[sub_key])
    return path, texts


def _extract_command(payload: dict) -> str:
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        args = tool_call.get("args")
        if isinstance(args, dict) and isinstance(args.get("CommandLine"), str):
            return args["CommandLine"]
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        return tool_input["command"]
    inp = payload.get("input")
    if isinstance(inp, dict) and isinstance(inp.get("command"), str):
        return inp["command"]
    if isinstance(payload.get("command"), str):
        return payload["command"]
    return ""


def evaluate_payload(payload: dict, host: str) -> Tuple[Optional[str], Optional[str]]:
    tool_name = str(
        (payload.get("toolCall") or {}).get("name")
        or payload.get("toolName")
        or ""
    ).lower()
    cwd = str(payload.get("cwd") or os.getcwd())

    if tool_name in (
        "write_file",
        "edit_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "write",
        "edit",
    ):
        path, texts = _extract_write_texts(payload)
        hit = scan_secrets(texts, f"写入内容（{os.path.basename(path)}）")
        if hit:
            return "deny", hit
        if SENSITIVE_DIR_RE.search(path):
            return "force_ask", "写入涉及敏感凭据目录（如 `.ssh`/`.aws`/`.gnupg`），须由用户明确授权（§3）。"
        return None, None

    cmd = _extract_command(payload)
    return evaluate_command(cmd, cwd=cwd, host=host)


def _emit(mode: str, decision: Optional[str], reason: Optional[str]) -> int:
    if mode == "antigravity":
        if decision in ("deny", "force_ask"):
            print(json.dumps({"decision": decision, "reason": reason or ""}, ensure_ascii=False))
        return 0

    if decision in ("deny", "force_ask"):
        print(reason or "命令被 Hook 策略拦截。", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Shared non-interactive & safety command check")
    parser.add_argument("--mode", choices=("antigravity", "exit2"), default="antigravity")
    parser.add_argument("--host", choices=("antigravity", "pi", "generic"), default="antigravity")
    args = parser.parse_args()

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return _emit(args.mode, "deny", "Hook 输入必须是 JSON 对象（fail-closed）。")
        decision, reason = evaluate_payload(payload, host=args.host)
        return _emit(args.mode, decision, reason)
    except Exception as exc:
        return _emit(args.mode, "deny", f"Hook 执行异常（fail-closed）：{exc}")


if __name__ == "__main__":
    raise SystemExit(main())
