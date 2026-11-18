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
}

PREFIX_WRAPPERS = {"sudo", "env", "command", "exec", "nohup", "nice", "time"}

SENSITIVE_DIR_RE = re.compile(
    r"(?:~|\$\{?HOME\}?|/Users/[^/\s\"']+|/home/[^/\s\"']+)/\.(?:ssh|aws|gnupg|netrc|docker|kube|config/gh)(?:/|\b|\s|$)"
    r"|(?:~|\$\{?HOME\}?|/Users/[^/\s\"']+|/home/[^/\s\"']+)/\.netrc\b"
)

STDIN_REDIRECTION_RE = re.compile(r"(?:^|\s)0?<(?:&-|\s*[^\s<(&>])|<<<?")
HEREDOC_START_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


def _strip_heredoc_bodies(cmd: str) -> str:
    """Replace heredoc body lines with spaces so data lines aren't parsed as commands."""
    lines = cmd.splitlines()
    if len(lines) <= 1:
        return cmd
    out: List[str] = []
    end_marker: Optional[str] = None
    for line in lines:
        if end_marker is not None:
            if line.strip() == end_marker:
                end_marker = None
            continue
        out.append(line)
        m = HEREDOC_START_RE.search(line)
        if m:
            end_marker = m.group(1)
    return "\n".join(out)


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


def _split_outside_quotes(text: str, seps: Tuple[str, ...]) -> List[str]:
    """Split `text` on separators in `seps` outside quotes, ignoring `2>&1` / `>&2` / `&>`."""
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(text)
    in_sq = False
    in_dq = False
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

        # Do not treat `>&` / `2>&1` / `&>` as a statement separator.
        if text[i : i + 2] in (">&", "&>"):
            buf.append(text[i : i + 2])
            i += 2
            continue

        matched_sep = None
        for sep in ordered_seps:
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


def _unwrap_prefixes(tokens: List[str]) -> Tuple[str, List[str], bool]:
    """Strip env assignments and wrapper commands; return (base_cmd, args, has_timeout_wrapper)."""
    i = 0
    has_timeout = False
    while i < len(tokens):
        tok = tokens[i]
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
                if base in ("sudo", "nice") and tokens[i] in ("-u", "-g", "-n") and i + 1 < len(tokens):
                    i += 2
                else:
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
        elif args[i].startswith("-"):
            i += 1
        else:
            break

    # `commit` subcommand args follow `commit`.
    commit_args = args[i + 1 :] if i < len(args) and args[i] == "commit" else []
    diff_cmds = [["git", "-C", target_cwd, "diff", "--cached"]]
    if any(a in ("-a", "--all") or (not a.startswith("-") and a) for a in commit_args):
        diff_cmds.append(["git", "-C", target_cwd, "diff"])

    for dcmd in diff_cmds:
        proc = subprocess.run(dcmd, capture_output=True, text=True, timeout=4, check=False)
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


def _check_git_action(args: List[str], cwd: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (decision, reason) for git subcommands."""
    i = 0
    while i < len(args):
        if args[i] in ("-C", "-c") and i + 1 < len(args):
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

    if sub == "checkout" and "--" in sub_args:
        return "force_ask", "执行 `git checkout -- ...` 会覆盖工作区修改，请确认（§1）。"

    if sub == "restore":
        return "force_ask", "执行 `git restore` 会丢弃工作区/暂存区修改，请确认（§1）。"

    if sub == "stash" and any(a in ("drop", "clear") for a in sub_args):
        return "force_ask", "执行 `git stash drop/clear` 不可逆，请确认（§1）。"

    if sub == "branch" and "-D" in sub_args:
        return "force_ask", "执行 `git branch -D` 会强制删除分支，请确认（§1）。"

    return None, None


def _check_rm_force(args: List[str]) -> Optional[str]:
    has_r = any(a == "--recursive" or (a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a)) for a in args)
    has_f = any(a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a) for a in args)
    if has_r and has_f:
        return "执行递归强制删除 `rm -rf` 属于不可逆高危操作，请确认（§1）。"
    return None


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


def evaluate_command(cmd: str, cwd: str, host: str) -> Tuple[Optional[str], Optional[str]]:
    """Evaluate shell command; returns (decision, reason) where decision is deny | force_ask | None."""
    stripped = cmd.strip()
    if not stripped:
        return None, None

    secret_hit = scan_secrets([stripped], "命令行参数")
    if secret_hit:
        return "deny", secret_hit

    body_stripped = _strip_heredoc_bodies(stripped)
    statements = _split_outside_quotes(body_stripped, ("&&", "||", ";", "\n"))

    best_decision: Optional[str] = None
    best_reason: Optional[str] = None

    def record(dec: Optional[str], reason: Optional[str]) -> None:
        nonlocal best_decision, best_reason
        if dec == "deny":
            best_decision, best_reason = "deny", reason
        elif dec == "force_ask" and best_decision is None:
            best_decision, best_reason = "force_ask", reason

    for stmt in statements:
        stmt_unquoted = _strip_comments_and_quotes(stmt).strip()
        if not stmt_unquoted:
            continue
        pipe_segments = _split_outside_quotes(stmt, ("|",))
        stmt_has_stdin = bool(STDIN_REDIRECTION_RE.search(stmt))

        for seg_idx, seg in enumerate(pipe_segments):
            tokens = _tokenize(seg)
            base, args, has_timeout = _unwrap_prefixes(tokens)
            if not base:
                continue

            # Recursively check `sh -c` / `bash -c` / `zsh -c` inner script.
            if base in ("sh", "bash", "zsh") and "-c" in args:
                c_idx = args.index("-c")
                if c_idx + 1 < len(args):
                    sub_dec, sub_reason = evaluate_command(args[c_idx + 1], cwd=cwd, host=host)
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

            # Stdin check applies only to the head of each pipeline.
            if seg_idx == 0 and not stmt_has_stdin and base not in NO_STDIN_CMDS:
                return (
                    "deny",
                    "无前置数据输入的命令必须显式封闭 stdin（例如在命令末尾加 `</dev/null`），"
                    "严禁悬空 stdin 等待输入。请重写命令后重试。",
                )

            # Sensitive credential directory check (allow `ssh -i` / `scp -i` / `ssh-add`).
            if SENSITIVE_DIR_RE.search(seg) and not (base in ("ssh", "scp", "ssh-add", "ssh-keygen") and ("-i" in args or base != "ssh")):
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
    for key in ("Content", "Replacement", "CodeContent", "content", "newText"):
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

    if tool_name in ("write_file", "edit_file", "write_to_file", "replace_file_content", "write", "edit"):
        path, texts = _extract_write_texts(payload)
        hit = scan_secrets(texts, f"写入内容（{os.path.basename(path)}）")
        if hit:
            return "deny", hit
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
