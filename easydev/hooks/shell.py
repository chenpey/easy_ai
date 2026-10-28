"""有限 shell 解析：分段、包装解包、Git 与读写命令识别。

永不执行被检命令，也不求值变量。任何无法安全解析的结构标记为 opaque，由
调用方决定告警放行或拒绝；所有分支都检查，不因前一条可能失败而忽略后一条。
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Dict, Iterable, List, Optional, Tuple

MAX_COMMAND_BYTES = 256 * 1024
MAX_UNWRAP_DEPTH = 8

GIT_GLOBAL_EQUAL_OPTIONS = (
    "--git-dir=",
    "--work-tree=",
    "--namespace=",
    "--exec-path=",
    "--super-prefix=",
    "--config-env=",
)
GIT_GLOBAL_VALUE_OPTIONS = ("--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env")
GIT_GLOBAL_FLAGS = {
    "--no-pager",
    "--bare",
    "--literal-pathspecs",
    "--no-replace-objects",
    "--no-optional-locks",
    "--version",
    "--help",
    "-p",
    "--paginate",
    "--no-advice",
}
GIT_READ_SUBCOMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "ls-files",
    "rev-parse",
    "cat-file",
    "describe",
    "blame",
    "grep",
    "help",
    "version",
    "for-each-ref",
    "shortlog",
    "whatchanged",
    "show-ref",
    "symbolic-ref",
    "merge-base",
    "count-objects",
    "check-ignore",
    "ls-tree",
    "name-rev",
    "rev-list",
}
GIT_WRITE_SUBCOMMANDS = {
    "add",
    "rm",
    "mv",
    "commit",
    "reset",
    "restore",
    "checkout",
    "switch",
    "clean",
    "stash",
    "merge",
    "rebase",
    "cherry-pick",
    "revert",
    "update-index",
    "apply",
    "fetch",
    "pull",
    "push",
    "am",
    "archive",
    "gc",
    "prune",
    "repack",
    "filter-branch",
    "submodule",
    "worktree",
}
SHELL_WRAPPERS = {"bash", "sh", "zsh"}
WRITE_COMMANDS = {
    "touch",
    "mkdir",
    "rm",
    "rmdir",
    "mv",
    "cp",
    "install",
    "truncate",
    "ln",
    "chmod",
    "chown",
    "tee",
    "dd",
    "patch",
    "unzip",
    "tar",
    "rsync",
    "apply_patch",
}
READ_COMMANDS = {
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "readlink",
    "stat",
    "wc",
    "file",
    "md5sum",
    "sha256sum",
    "shasum",
    "xxd",
    "od",
    "strings",
}
SEARCH_COMMANDS = {"rg", "grep", "ag", "ack", "find", "ls", "tree"}
PACKAGE_WRITE_PATTERNS = (
    ({"npm", "pnpm", "yarn"}, {"install", "add", "remove", "uninstall", "update", "upgrade"}),
    ({"pip", "pip3"}, {"install", "uninstall"}),
    ({"cargo"}, {"add", "remove"}),
    ({"brew"}, {"install", "uninstall", "upgrade"}),
    ({"apt", "apt-get"}, {"install", "remove", "purge"}),
)
# 读工具别名：只统计明确读取，不从交接 prompt 或补丁正文里猜路径。
READ_TOOL_NAMES = {
    "read",
    "read_file",
    "view_file",
    "view",
    "readfile",
    "read_text_file",
}
WRITE_TOOL_NAMES = {
    "write",
    "write_file",
    "write_to_file",
    "edit",
    "replace_file_content",
    "str_replace_editor",
    "str_replace",
    "apply_patch",
    "multiedit",
    "create_file",
}
DISPATCH_TOOL_NAMES = {
    "subagent",
    "subagent_supervisor",
    "spawn_agent",
    "agent",
    "invoke_subagent",
}
STATUS_SUBCOMMANDS = {"status", "wait", "cancel", "list", "poll", "result"}

# 重定向按运算符分类：数字前缀只是被重定向的 fd，不是目标文件名。
_REDIRECT_OPERATOR_RE = re.compile(r"^[0-9]*(&>>|&>|>>|>\||<<<|<<|<>|>|<)$")
# `>&1`/`2>&1`/`<&3`/`>&-` 是 fd 复制，没有文件目标；`> 1`、`2> 1` 是普通文件。
_REDIRECT_DUP_RE = re.compile(r"^[0-9]*(?:>&|<&)(?:[0-9]+|-)$")
# `>&word` 只在 word 展开为纯数字或 `-` 时复制/关闭 fd；否则整段 word 是文件目标。
_FD_WORD_RE = re.compile(r"^(?:[0-9]+|-)$")
_READ_REDIRECTS = {"<", "<>"}
_WRITE_REDIRECTS = {">", ">>", "<>", ">|", "&>", "&>>"}


class Analysis(object):
    """一次命令分析的合并结果。"""

    def __init__(self) -> None:
        self.opaque = False
        self.too_deep = False
        self.git: List[Dict[str, object]] = []
        self.writes: List[Dict[str, object]] = []
        self.reads: List[Dict[str, object]] = []
        self.commands: List[List[str]] = []
        self.write_unknown = False
        self.read_unknown = False
        self.recursive_follow = False

    def merge(self, other: "Analysis") -> None:
        self.opaque = self.opaque or other.opaque
        self.too_deep = self.too_deep or other.too_deep
        self.git.extend(other.git)
        self.writes.extend(other.writes)
        self.reads.extend(other.reads)
        self.commands.extend(other.commands)
        self.write_unknown = self.write_unknown or other.write_unknown
        self.read_unknown = self.read_unknown or other.read_unknown
        self.recursive_follow = self.recursive_follow or other.recursive_follow

    def as_dict(self) -> Dict[str, object]:
        return {
            "opaque": self.opaque,
            "too_deep": self.too_deep,
            "git": self.git,
            "writes": self.writes,
            "reads": self.reads,
            "write_unknown": self.write_unknown,
            "read_unknown": self.read_unknown,
            "recursive_follow": self.recursive_follow,
        }


def normalize_limit(value: Optional[int]) -> int:
    return value if isinstance(value, int) and value > 0 else MAX_COMMAND_BYTES


class Token(object):
    """一个命令行 token；保留引号语义，避免把字面数据当成执行行为。

    `operator` 只在裸（未引用）位置出现，`dynamic` 表示内容可能发生替换；单引号
    与反斜杠转义的内容两者都为假。
    """

    __slots__ = ("text", "operator", "dynamic")

    def __init__(self, text: str, operator: bool = False, dynamic: bool = False) -> None:
        self.text = text
        self.operator = operator
        self.dynamic = dynamic


def _is_fd_word(word: str) -> bool:
    """`>&word` 的 word 是否指向 fd（纯数字或 `-`）。

    引号不改变结论（`>&"1"` 仍展开成 1）；可能展开的 word 内容不是数字，按文件
    目标处理更安全。
    """
    return bool(_FD_WORD_RE.match(word))


def _redirect_word(text: str, index: int) -> Tuple[str, bool, int, bool]:
    """解析 `>&` 之后的完整 word：引号、转义与词边界按 Bash 语义。

    返回 (未引用值, 是否可能展开, 结束位置, 引号是否闭合)。只消费数字前缀会把
    `>&1.txt` 拆成 `>&1` + `.txt`，把真实文件写入伪装成 fd 复制。
    """
    word: List[str] = []
    dynamic = False
    quote: Optional[str] = None
    i = index
    length = len(text)
    while i < length:
        char = text[i]
        if quote is not None:
            if char == "\\" and quote == '"' and i + 1 < length:
                i += 1
                word.append(text[i])
                dynamic = True
            elif char == quote:
                quote = None
            else:
                word.append(char)
                if quote == '"':
                    dynamic = True
            i += 1
            continue
        if char in "'\"":
            quote = char
            i += 1
            continue
        if char == "\\" and i + 1 < length:
            i += 1
            word.append(text[i])
            dynamic = True
            i += 1
            continue
        if char in " \t\n" or char in "|;&<>":
            break
        word.append(char)
        if char in "$`":
            dynamic = True
        i += 1
    return "".join(word), dynamic, i, quote is None


def _strip_line_continuations(text: str) -> str:
    """删除单引号外的 `\\`+真实换行（Bash 行续接），单引号内保持字面。

    必须在任何词法/运算符判定之前整体完成：`>` + 反斜杠 + 真实换行 + `>1.txt`
    在 Bash 里是 `>>1.txt`。若只在校验 word 时消除续接，运算符识别仍看原始相邻
    字符，会把第二个 `>` 当成文件名目标，真实文件写入绕过检查。双引号内的续接
    同样消失；其他反斜杠转义原样保留给词法阶段处理。

    引号状态必须与注释和词边界共用一份词法状态：`#` 只在词首（行首、空白或
    运算符之后）开始注释，注释里的引号没有语法作用。注释到**物理行尾**结束
    （`\\`+换行在注释里不是续接）：让注释跨行会把下一行的真实命令当注释文本
    静默放行，例如 `# note \\` + 换行 + `git reset --hard` 在 Bash 里确实执行。
    """
    stripped: List[str] = []
    quote: Optional[str] = None
    comment = False
    word_start = True
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if comment:
            # 注释内的字符（引号、反斜杠）都没有语法作用，只找物理行尾。
            if char == "\n":
                comment = False
                word_start = True
            stripped.append(char)
            i += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            stripped.append(char)
            i += 1
            continue
        if char == "\\" and i + 1 < length:
            if text[i + 1] == "\n":
                # 续接整体消失；词首与引号状态不变，下一字符仍按原语境解释。
                i += 2
                continue
            # 转义保留原样：是否取消特殊含义由引号语境下的词法阶段决定。
            stripped.append(char)
            stripped.append(text[i + 1])
            word_start = False
            i += 2
            continue
        if quote == '"':
            if char == '"':
                quote = None
            stripped.append(char)
            i += 1
            continue
        if char in "'\"":
            quote = char
            word_start = False
            stripped.append(char)
            i += 1
            continue
        if char == "#" and word_start:
            comment = True
            stripped.append(char)
            i += 1
            continue
        stripped.append(char)
        # 与 tokenize 的空白/运算符集合一致：只有这些字符之后再出现 `#` 才是词首。
        word_start = char in " \t\n|;&<>"
        i += 1
    return "".join(stripped)


def tokenize(text: str) -> Tuple[List[Token], bool]:
    """按引号/转义/注释状态切成 token；返回 (tokens, closed)。

    入口先消除单引号外的行续接，argv、重定向运算符与目标、cwd 因此共用同一
    份词法结果。
    """
    text = _strip_line_continuations(text)
    tokens: List[Token] = []
    buffer: List[str] = []
    started = False
    dynamic = False
    quote: Optional[str] = None
    i = 0
    length = len(text)
    closed = True

    def flush() -> None:
        nonlocal buffer, started, dynamic
        if started:
            tokens.append(Token("".join(buffer), False, dynamic))
        buffer = []
        started = False
        dynamic = False

    def emit_operator(value: str) -> None:
        flush()
        tokens.append(Token(value, True))

    while i < length:
        char = text[i]
        if quote is not None:
            if char == "\\" and quote == '"' and i + 1 < length:
                i += 1
                buffer.append(text[i])
            elif char == quote:
                quote = None
            else:
                buffer.append(char)
            i += 1
            continue
        if char in "'\"":
            quote = char
            started = True
            if char == '"':
                dynamic = True
            i += 1
            continue
        if char == "\\" and i + 1 < length:
            i += 1
            buffer.append(text[i])
            started = True
            i += 1
            continue
        if char in " \t":
            flush()
            i += 1
            continue
        if char == "#" and not started:
            while i < length and text[i] != "\n":
                i += 1
            continue
        if text.startswith("&&", i) or text.startswith("||", i):
            emit_operator(text[i : i + 2])
            i += 2
            continue
        if char in "|;&\n":
            emit_operator(char)
            i += 1
            continue
        if char in "<>":
            if started and "".join(buffer).isdigit():
                prefix = "".join(buffer)
                buffer = []
                started = False
                dynamic = False
            else:
                prefix = ""
                flush()
            op = prefix + char
            if i + 1 < length and text[i + 1] == char and char != "<":
                op += char
                i += 1
            if i + 1 < length and char == ">" and text[i + 1] == "|":
                op += "|"
                i += 1
            if i + 1 < length and char == "<" and text[i + 1] == ">":
                op += ">"
                i += 1
            if i + 1 < length and text[i + 1] == "&" and op.endswith(">"):
                # `&` 紧跟 `>` 才是 fd 复制：`>&1`/`2>&1`/`>&-`。`2> 1` 是写
                # 普通文件 `1`；`>&file` 等价于 `&>file`，仍是写重定向。word 按
                # 完整词边界判定，`>&1.txt` 的目标是文件 `1.txt`。
                file_op = "&>" + (">" if op.endswith(">>") else "")
                word, word_dynamic, end, word_closed = _redirect_word(text, i + 2)
                closed = closed and word_closed
                if not word:
                    # `>& 1` 与 `>&1` 同样是 fd 复制；`>& 1.txt` 的目标是下一个 token。
                    look = end
                    while look < length and text[look] in " \t":
                        look += 1
                    spaced, _spaced_dynamic, spaced_end, spaced_closed = _redirect_word(
                        text, look
                    )
                    if spaced and _is_fd_word(spaced):
                        closed = closed and spaced_closed
                        tokens.append(Token(op + "&" + spaced, True))
                        i = spaced_end
                        continue
                    tokens.append(Token(file_op, True))
                    i = end
                    continue
                if _is_fd_word(word):
                    tokens.append(Token(op + "&" + word, True))
                else:
                    tokens.append(Token(file_op, True))
                    tokens.append(Token(word, False, word_dynamic))
                i = end
                continue
            tokens.append(Token(op, True))
            i += 1
            continue
        buffer.append(char)
        started = True
        dynamic = True
        i += 1
    if quote is not None:
        closed = False
    flush()
    return tokens, closed


def _split_segments(tokens: Iterable[Token]) -> List[List[Token]]:
    segments: List[List[Token]] = []
    current: List[Token] = []
    for token in tokens:
        if token.operator and token.text in {"&&", "||", "|", ";", "&", "\n"}:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _looks_like_option(token: str) -> bool:
    return token.startswith("-") and token != "-"


def _redirect_base(text: str) -> Optional[str]:
    """把重定向运算符规范化成不带 fd 前缀的形式；未知运算符返回 None。"""
    match = _REDIRECT_OPERATOR_RE.match(text)
    return match.group(1) if match else None


def _strip_redirects(
    tokens: List[Token],
) -> Tuple[List[str], List[Token], List[Tuple[str, str]], bool]:
    argv: List[str] = []
    argv_tokens: List[Token] = []
    redirects: List[Tuple[str, str]] = []
    index = 0
    opaque = False
    while index < len(tokens):
        token = tokens[index]
        if token.operator and _REDIRECT_DUP_RE.match(token.text):
            # fd 复制没有文件目标：`2>&1` 不等同于写名为 `1` 的文件。
            index += 1
            continue
        base = _redirect_base(token.text) if token.operator else None
        if base is not None:
            if base == "<<":
                # heredoc 正文不在 argv 里，无法证明内容安全。
                opaque = True
                index += 2
                continue
            if index + 1 < len(tokens):
                redirects.append((base, tokens[index + 1].text))
                index += 2
                continue
            opaque = True
            index += 1
            continue
        if token.operator:
            # 未识别的裸操作符：不静默丢弃。
            opaque = True
            index += 1
            continue
        argv.append(token.text)
        argv_tokens.append(token)
        index += 1
    # 前置 KEY=VALUE 环境赋值不是命令。
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        argv = argv[1:]
        argv_tokens = argv_tokens[1:]
    return argv, argv_tokens, redirects, opaque


def _requote(token: Token) -> str:
    """把已切分的 token 还原为可重新解析的文本，同时保留引号语义。"""
    text = token.text
    if not text:
        return "''"
    if not re.search(r"[\s'\"$`\\|&;<>()*?\[\]{}~#!]", text):
        return text
    if token.dynamic:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`") + '"'
    return "'" + text.replace("'", "'\\''") + "'"


def _expand_substitutions(text: str) -> List[str]:
    """提取 `$(…)` 与反引号的字面内容，供递归分类；不执行求值。"""
    found: List[str] = []
    index = 0
    while index < len(text):
        if text.startswith("$(", index):
            depth = 1
            cursor = index + 2
            while cursor < len(text) and depth:
                if text[cursor] == "(":
                    depth += 1
                elif text[cursor] == ")":
                    depth -= 1
                cursor += 1
            if depth == 0:
                found.append(text[index + 2 : cursor - 1])
                index = cursor
                continue
        if text[index] == "`":
            end = text.find("`", index + 1)
            if end > index:
                found.append(text[index + 1 : end])
                index = end + 1
                continue
        index += 1
    return found


ENV_NO_VALUE_OPTIONS = {
    "-i",
    "--ignore-environment",
    "-0",
    "--null",
    "-v",
    "--debug",
}
ENV_VALUE_OPTIONS = {
    "-u",
    "--unset",
    "-C",
    "--chdir",
    "-S",
    "--split-string",
    "--block-signal",
    "--default-signal",
    "--ignore-signal",
}
SUDO_VALUE_OPTIONS = {"-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t", "-U", "-D"}


def _wrapper_inner(tokens: List[Token]) -> Tuple[Optional[str], bool, str]:
    """识别 shell/eval/env/sudo/command/exec 包装。

    返回 (内层命令文本或 None, 是否无法安全判定, 内层有效 cwd 或 "")。
    - shell `-c`/`eval` 的参数本身就是命令字符串，按原文重新解析；
    - env/sudo/command/exec 之后是 argv，逐项重新引用以保留参数边界；
    - `env -C/--chdir`、`env -C<dir>` 是内层命令的有效 cwd，必须保留，
      否则内层相对读/写会被错误地按外层 cwd 定位。
    无参数选项、带值选项和等号选项分开处理；未知选项不吞掉后面的实际命令，
    而是标记 opaque。
    """
    if not tokens:
        return None, False, ""
    argv = [token.text for token in tokens]
    name = os.path.basename(argv[0])
    rest = tokens[1:]
    opaque = False

    def as_argv(inner: List[Token]) -> str:
        return " ".join(_requote(token) for token in inner)

    def as_command(inner: List[Token]) -> str:
        return " ".join(token.text for token in inner)

    if name in SHELL_WRAPPERS:
        for index, text in enumerate(argv[1:]):
            if text in {"-c", "-lc", "-ic", "-cl", "-xc"} and index + 2 < len(argv):
                return as_command(rest[index + 1 :]), False, ""
        return None, False, ""
    if name == "env":
        index = 0
        inner_cwd = ""
        while index < len(rest):
            text = rest[index].text
            if text in ENV_NO_VALUE_OPTIONS:
                index += 1
                continue
            if text in ENV_VALUE_OPTIONS:
                if index + 1 >= len(rest):
                    return None, True, ""
                if text in {"-C", "--chdir"}:
                    inner_cwd = rest[index + 1].text
                index += 2
                continue
            if text.startswith("-C") and len(text) > 2:
                inner_cwd = text[2:]
                index += 1
                continue
            if "=" in text and text.startswith("--") and not text.startswith("--="):
                option = text.split("=", 1)[0]
                if option in ENV_VALUE_OPTIONS:
                    if option == "--chdir":
                        inner_cwd = text.split("=", 1)[1]
                    index += 1
                    continue
            if text.startswith("-") and text != "-":
                # 未知 env 选项：不吞掉后续命令，标记无法完整判定。
                opaque = True
                index += 1
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", text):
                index += 1
                continue
            break
        if index >= len(rest):
            return None, opaque, ""
        return as_argv(rest[index:]), opaque, inner_cwd
    if name == "sudo":
        index = 0
        while index < len(rest):
            text = rest[index].text
            if text == "--":
                index += 1
                break
            if text in SUDO_VALUE_OPTIONS:
                if index + 1 >= len(rest):
                    return None, True, ""
                index += 2
                continue
            if text.startswith("-") and text != "-":
                if "=" in text and text.split("=", 1)[0] in SUDO_VALUE_OPTIONS:
                    index += 1
                    continue
                index += 1
                continue
            break
        if index >= len(rest):
            return None, opaque, ""
        return as_argv(rest[index:]), opaque, ""
    if name in {"command", "exec"}:
        index = 0
        while index < len(rest) and _looks_like_option(rest[index].text):
            index += 1
        if index >= len(rest):
            return None, opaque, ""
        return as_argv(rest[index:]), opaque, ""
    if name == "eval":
        return as_command(rest), opaque, ""
    return None, opaque, ""


def _wrapper_effective_cwd(
    value: str, base: Optional[str], base_known: bool
) -> Tuple[Optional[str], bool]:
    """把包装器给出的（可能相对的）cwd 相对进入包装前的有效 cwd 解析。

    `env -C sub` 的相对值以进入 wrapper 时的目录为基准，而不是最终 cwd；多层
    wrapper 由同样的递归逐层累积。真实基准未知时返回 `(None, False)`，调用方按
    “目录不明”降级，绝不当成仓库根或工具 cwd。
    """
    if not value:
        return base, base_known
    if os.path.isabs(value):
        return os.path.normpath(value), True
    if not base:
        return None, False
    return os.path.normpath(os.path.join(base, value)), base_known


def _track_cd(args: List[str], current: Optional[str]) -> Optional[str]:
    """跟踪可静态确定的 `cd` 目标；无法确定返回 None。"""
    positional = [token for token in args if not _looks_like_option(token)]
    if len(positional) != 1:
        return None
    target = positional[0]
    if target in {"~", "-"} or re.search(r"[$`*?\[\]{}]", target):
        return None
    if os.path.isabs(target):
        return os.path.normpath(target)
    if not current:
        return None
    return os.path.normpath(os.path.join(current, target))


def _git_actions(argv: List[str]) -> List[Dict[str, object]]:
    if not argv or os.path.basename(argv[0]) != "git":
        return []
    index = 1
    globals_seen: List[str] = []
    while index < len(argv):
        token = argv[index]
        if token in {"-C", "-c"} and index + 1 < len(argv):
            globals_seen.extend(argv[index : index + 2])
            index += 2
            continue
        if token.startswith("-C") and token != "-C" and len(token) > 2:
            globals_seen.append(token)
            index += 1
            continue
        if token.startswith("-c") and token != "-c" and len(token) > 2:
            globals_seen.append(token)
            index += 1
            continue
        if any(token.startswith(prefix) for prefix in GIT_GLOBAL_EQUAL_OPTIONS):
            globals_seen.append(token)
            index += 1
            continue
        if token in GIT_GLOBAL_VALUE_OPTIONS and index + 1 < len(argv):
            globals_seen.extend(argv[index : index + 2])
            index += 2
            continue
        if token in GIT_GLOBAL_FLAGS:
            globals_seen.append(token)
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            globals_seen.append(token)
            index += 1
            continue
        if token.startswith("-") and token != "-":
            # 未知 global flag：若下一个 token 像子命令，不吞掉它。
            globals_seen.append(token)
            index += 1
            continue
        break
    if index >= len(argv):
        return []
    return [{"global": globals_seen, "sub": argv[index], "args": argv[index + 1 :]}]


def _option_present(args: List[str], names: Iterable[str]) -> bool:
    wanted = set(names)
    for token in args:
        if token in wanted:
            return True
        if token.startswith("--") and "=" in token and token.split("=", 1)[0] in wanted:
            return True
    return False


def _short_flag_present(args: List[str], letters: str) -> bool:
    for token in args:
        if token.startswith("-") and not token.startswith("--") and len(token) > 1:
            if any(letter in token[1:] for letter in letters):
                return True
    return False


def git_violation(action: Dict[str, object]) -> Optional[str]:
    """返回 P1 破坏性 Git 的规则说明；只读/查询返回 None。"""
    sub = str(action.get("sub") or "")
    args = [str(token) for token in action.get("args") or []]
    if sub == "reset":
        if _option_present(args, {"--hard"}) or any(
            token.startswith("--hard=") for token in args
        ):
            return "git reset --hard 会丢弃工作区改动"
        return None
    if sub == "checkout":
        if "--" in args or _short_flag_present(args, "f") or _option_present(args, {"--force"}):
            return "git checkout 的路径还原或强制切换会丢弃工作区改动"
        return None
    if sub == "restore":
        staged_only = False
        worktree = False
        for token in args:
            if token in {"--staged", "-S"}:
                staged_only = True
            elif token in {"--worktree", "-W"}:
                worktree = True
            elif token.startswith("-") and not token.startswith("--") and len(token) > 1:
                letters = token[1:]
                if "S" in letters:
                    staged_only = True
                if "W" in letters:
                    worktree = True
            elif token == "--source" or token.startswith("--source="):
                continue
        if worktree:
            return "git restore 会改写工作区文件"
        if not staged_only:
            return "git restore 缺 --staged 时默认改写工作区"
        return None
    if sub == "clean":
        if _option_present(args, {"--force"}) or _short_flag_present(args, "f"):
            if _short_flag_present(args, "n") or _option_present(args, {"--dry-run"}):
                return None
            return "git clean -f 会删除未跟踪文件"
        return None
    if sub == "stash":
        if not args:
            return "git stash 默认会保存并清理工作区改动"
        child = args[0]
        if child in {"list", "show", "help"} or child.startswith("--help"):
            return None
        return "git stash %s 会改动工作区或 stash 栈" % child
    if sub == "push":
        if _short_flag_present(args, "n") or _option_present(args, {"--dry-run"}):
            return None
        if _short_flag_present(args, "f") or _option_present(args, {"--force", "--force-with-lease"}):
            return "强制推送会覆盖远端历史"
        for token in args:
            if token.startswith("+") and ":" in token:
                return "强制 refspec 会覆盖远端历史"
        return None
    return None


CURL_BODY_OPTIONS = {
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "-F",
    "--form",
    "-T",
    "--upload-file",
}
_REF_LIST_FLAGS = {
    "-l",
    "--list",
    "-a",
    "--all",
    "-r",
    "--remotes",
    "-v",
    "-vv",
    "--verbose",
    "--show-current",
    "--contains",
    "--no-contains",
    "--merged",
    "--no-merged",
    "--points-at",
    "--sort",
    "--column",
    "--omit-empty",
}
_REF_WRITE_FLAGS = {
    "-d",
    "-D",
    "-m",
    "-M",
    "-c",
    "-C",
    "-f",
    "-u",
    "--delete",
    "--move",
    "--copy",
    "--force",
    "--set-upstream-to",
    "--unset-upstream",
    "--edit-description",
    "--create-reflog",
}


def _ref_command_writes(args: List[str]) -> bool:
    """`git branch`/`git tag`：只有创建/删除/移动/改上游才算写入。"""
    if any(token in _REF_WRITE_FLAGS for token in args):
        return True
    if any(token in _REF_LIST_FLAGS or token.startswith("--format") for token in args):
        return False
    return any(not _looks_like_option(token) for token in args)


def _operands(
    rest: List[str], short_value_letters: set, long_value_options: set
) -> Tuple[List[str], Dict[str, List[str]], bool]:
    """拆分带值选项与位置参数；返回 (位置参数, 选项值, 是否含无法判定的组合)。"""
    positional: List[str] = []
    values: Dict[str, List[str]] = {}
    ambiguous = False
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            positional.extend(rest[index + 1 :])
            break
        if token.startswith("--"):
            option, separator, value = token.partition("=")
            if separator:
                values.setdefault(option, []).append(value)
                index += 1
                continue
            if option in long_value_options:
                if index + 1 < len(rest):
                    values.setdefault(option, []).append(rest[index + 1])
                    index += 2
                    continue
                ambiguous = True
            index += 1
            continue
        if token.startswith("-") and token != "-":
            first = token[1:2]
            letters = token[1:]
            if first in short_value_letters:
                attached = token[2:]
                if attached:
                    values.setdefault("-" + first, []).append(attached)
                    index += 1
                elif index + 1 < len(rest):
                    values.setdefault("-" + first, []).append(rest[index + 1])
                    index += 2
                else:
                    ambiguous = True
                    index += 1
                continue
            if any(letter in short_value_letters for letter in letters):
                # `-rS` 之类无法确定哪个字母吃到了附值：不猜。
                ambiguous = True
            index += 1
            continue
        positional.append(token)
        index += 1
    return positional, values, ambiguous


def _stat_directory(path: str, cwd: Optional[str]) -> Optional[bool]:
    """静态判定目标是否为目录：真实结果、False（不存在/普通文件）或 None（无法定位）。"""
    if not isinstance(path, str) or not path:
        return None
    if os.path.isabs(path):
        absolute = path
    elif cwd:
        absolute = os.path.join(cwd, path)
    else:
        return None
    try:
        if not os.path.lexists(absolute):
            return False
        return os.path.isdir(absolute)
    except OSError:
        return None


def _has_long_or_short_flag(rest: List[str], short: str, long: str) -> bool:
    if long in rest:
        return True
    for token in rest:
        if token.startswith("-") and not token.startswith("--") and short in token[1:]:
            return True
    return False


def _copy_operands(
    name: str, rest: List[str]
) -> Tuple[List[str], Dict[str, List[str]], bool]:
    short_value = {"S", "t"}
    long_value = {"--suffix", "--target-directory"}
    if name == "install":
        short_value = {"S", "t", "m", "o", "g", "Z"}
        long_value = {
            "--suffix",
            "--target-directory",
            "--mode",
            "--owner",
            "--group",
            "--context",
        }
    return _operands(rest, short_value, long_value)


def _copy_layout(name: str, rest: List[str]) -> Dict[str, object]:
    """cp/mv/install 的共享 operand 布局：哪些是源、目的与目的语法。

    返回 `sources`/`destination`/`form`/`positional`：`-t/--target-directory` 下
    所有位置参数都是源，`-T/--no-target-directory` 下最后一个才是目的。写入目标
    与读取源都用这一份解析，消费方不得再假定“最后一个位置参数是目的”。
    """
    positional, values, ambiguous = _copy_operands(name, rest)
    layout: Dict[str, object] = {
        "sources": [],
        "destination": None,
        "form": "incomplete",
        "positional": positional,
    }
    if ambiguous:
        return layout
    if name == "install" and _has_long_or_short_flag(rest, "d", "--directory"):
        # `install -d`：位置参数都是被创建的目录，没有源。
        layout["form"] = "create-directory" if positional else "incomplete"
        return layout
    target_dirs = values.get("-t", []) + values.get("--target-directory", [])
    if target_dirs:
        if len(target_dirs) == 1 and positional:
            layout.update(
                {
                    "sources": list(positional),
                    "destination": target_dirs[0],
                    "form": "target-directory",
                }
            )
        return layout
    if len(positional) < 2:
        return layout
    layout.update(
        {
            "sources": positional[:-1],
            "destination": positional[-1],
            "form": (
                "no-target-directory"
                if "--no-target-directory" in rest
                or _has_long_or_short_flag(rest, "T", "--no-target-directory")
                else "plain"
            ),
        }
    )
    return layout


def _copy_like_targets(
    name: str, rest: List[str], cwd: Optional[str]
) -> List[Tuple[Optional[str], bool]]:
    """cp/mv/install 的实际写入目标（目录目的 + 源 basename、多源、-t/-T）。

    目录目的不是唯一的被改文件：真正被覆盖的是 `目的/源 basename`。目录源无法
    静态枚举时标作不完整，宁可不声称完整也不只核验目录本身。mv 的每种目的语法
    都会移走源，因此源路径在每个分支都附加为被改动目标。
    """
    layout = _copy_layout(name, rest)
    form = str(layout["form"])
    positional = [str(item) for item in layout["positional"]]  # type: ignore[union-attr]
    if form == "create-directory":
        return [(item, True) for item in positional]
    if form == "incomplete":
        return [(None, False)]
    sources = [str(item) for item in layout["sources"]]  # type: ignore[union-attr]
    destination = str(layout["destination"])
    targets: List[Tuple[Optional[str], bool]]
    if form == "target-directory":
        targets = _destination_targets(destination, sources, cwd)
    elif form == "no-target-directory":
        targets = [(destination, True)]
    else:
        is_directory = _stat_directory(destination, cwd)
        if is_directory is None:
            return [(None, False)]
        if is_directory:
            targets = _destination_targets(destination, sources, cwd)
        elif len(sources) > 1:
            # 多源但目的不是已有目录：真实语义只能靠命令报错，无法给出完整目标。
            return [(None, False)]
        else:
            targets = [(destination, True)]
    if name == "mv":
        # mv 同时删除源（内容被移走）：-t/-T/普通目的的源都必须作为被改动
        # 路径参与基线检查；目录源内部无法枚举，不标完整。
        for source in sources:
            targets.append((source, _stat_directory(source, cwd) is False))
    return targets


def _destination_targets(
    destination: str, sources: List[str], cwd: Optional[str]
) -> List[Tuple[Optional[str], bool]]:
    targets: List[Tuple[Optional[str], bool]] = []
    for source in sources:
        trimmed = source.rstrip(os.sep)
        base = os.path.basename(trimmed)
        if not base or base in {".", ".."}:
            return [(None, False)]
        target = os.path.join(destination, base)
        targets.append((target, _stat_directory(source, cwd) is False))
    return targets


def _write_targets_for_command(
    argv: List[str],
    redirects: List[Tuple[str, str]],
    analysis: Analysis,
    cwd: Optional[str] = None,
) -> None:
    for op, target in redirects:
        if op in _WRITE_REDIRECTS and target != "/dev/null":
            analysis.writes.append({"kind": "redirect", "target": target, "complete": True})
    if not argv:
        return
    name = os.path.basename(argv[0])
    rest = [token for token in argv[1:]]
    if name in {"cp", "mv", "install"}:
        for target, complete in _copy_like_targets(name, rest, cwd):
            analysis.writes.append({"kind": name, "target": target, "complete": complete})
        return
    if name == "tee":
        targets = [token for token in rest if not _looks_like_option(token)]
        if not targets:
            analysis.writes.append({"kind": "tee", "target": None, "complete": False})
        for target in targets:
            analysis.writes.append({"kind": "tee", "target": target, "complete": True})
        return
    if name == "dd":
        for token in rest:
            if token.startswith("of="):
                analysis.writes.append({"kind": "dd", "target": token[3:], "complete": True})
        return
    if name == "sed":
        in_place = _option_present(rest, {"--in-place"}) or any(
            token.startswith("--in-place=") for token in rest
        ) or any(token.startswith("-i") and token != "-i" for token in rest) or "-i" in rest
        if in_place:
            files = [token for token in rest if not _looks_like_option(token)]
            if len(files) > 1:
                for target in files[1:]:
                    analysis.writes.append({"kind": "sed", "target": target, "complete": True})
            else:
                analysis.writes.append({"kind": "sed", "target": None, "complete": False})
        return
    if name == "perl":
        if any(token.startswith("-i") for token in rest):
            files = [token for token in rest if not _looks_like_option(token)]
            if len(files) > 1:
                for target in files[1:]:
                    analysis.writes.append({"kind": "perl", "target": target, "complete": True})
            else:
                analysis.writes.append({"kind": "perl", "target": None, "complete": False})
        return
    if name in WRITE_COMMANDS:
        positional = [token for token in rest if not _looks_like_option(token)]
        if name in {"mkdir", "touch", "rm", "rmdir", "chmod", "chown", "truncate", "ln", "rsync"}:
            targets = positional[1:] if name in {"chmod", "chown", "rsync"} else positional
            if name == "chmod" and not targets:
                targets = []
            for target in targets:
                analysis.writes.append({"kind": name, "target": target, "complete": True})
            if not targets:
                analysis.writes.append({"kind": name, "target": None, "complete": False})
            return
        if name == "tar":
            if any(token in {"-x", "-xf", "--extract"} or token.startswith("-x") for token in rest):
                analysis.writes.append({"kind": "tar", "target": ".", "complete": True})
            return
        if name == "unzip":
            analysis.writes.append({"kind": "unzip", "target": ".", "complete": True})
            return
        analysis.writes.append({"kind": name, "target": None, "complete": False})
        return
    if name == "git":
        actions = _git_actions(argv)
        for action in actions:
            sub = str(action.get("sub"))
            args = [str(token) for token in action.get("args") or []]
            if sub in {"branch", "tag"}:
                if _ref_command_writes(args):
                    analysis.writes.append({"kind": "git-" + sub, "target": None, "complete": False})
                continue
            if sub in GIT_WRITE_SUBCOMMANDS:
                if sub == "stash":
                    if args and args[0] in {"list", "show", "help"}:
                        continue
                analysis.writes.append({"kind": "git-" + sub, "target": None, "complete": False})
                continue
            if sub == "config":
                if not any(
                    token.startswith("--get")
                    or token in {"--list", "--show-origin", "-l"}
                    for token in args
                ):
                    analysis.writes.append({"kind": "git-config", "target": None, "complete": False})
        return
    if name in {"python", "python3", "node"}:
        literal = " ".join(rest)
        if name.startswith("python"):
            if re.search(r"open\s*\([^)]*['\"][wax+]", literal) or re.search(
                r"\.(write_text|write_bytes|unlink|rename|replace|mkdir)\s*\(", literal
            ):
                analysis.writes.append({"kind": "interpreter", "target": None, "complete": False})
        else:
            if re.search(r"fs\.(writeFile|appendFile|unlink|rename|mkdir|rm)", literal):
                analysis.writes.append({"kind": "interpreter", "target": None, "complete": False})
        return
    for executables, subcommands in PACKAGE_WRITE_PATTERNS:
        if name in executables and rest and rest[0] in subcommands:
            analysis.writes.append({"kind": "package", "target": None, "complete": False})
            return
    if name in {"npm", "pnpm", "yarn", "bun"} and rest and rest[0] in {"run", "run-script"}:
        # `npm run lint -- --fix` 是明确的自动修复写入，不是普通 lint。
        if any(token in {"--fix", "--write"} for token in rest[1:]):
            analysis.writes.append({"kind": "package-fix", "target": None, "complete": False})
        return
    if name in {"curl", "wget"}:
        if name == "curl":
            method = None
            explicit_write = False
            sends_body = False
            index = 0
            while index < len(rest):
                token = rest[index]
                if token in {"-X", "--request"} and index + 1 < len(rest):
                    method = rest[index + 1].upper()
                    index += 2
                    continue
                if token.startswith("--request="):
                    method = token.split("=", 1)[1].upper()
                elif token in CURL_BODY_OPTIONS:
                    sends_body = True
                elif token.startswith("--data") or token.startswith("--upload-file=") or token.startswith("--form="):
                    sends_body = True
                elif token in {"-o", "--output", "-O", "--remote-name"}:
                    explicit_write = True
                    if token in {"-o", "--output"} and index + 1 < len(rest):
                        index += 2
                        continue
                elif token.startswith("--output=") or token.startswith("--output-document"):
                    explicit_write = True
                index += 1
            if method in {"POST", "PUT", "PATCH", "DELETE"} or sends_body or explicit_write:
                analysis.writes.append({"kind": "curl", "target": None, "complete": False})
        else:
            if any(
                token.startswith("-O")
                or token == "--output-document"
                or token.startswith("--output-document=")
                for token in rest
            ):
                analysis.writes.append({"kind": "wget", "target": None, "complete": False})
        return


_READ_VALUE_OPTIONS = {
    "-e",
    "-g",
    "-t",
    "-T",
    "-f",
    "-A",
    "-B",
    "-C",
    "-m",
    "-j",
    "-M",
    "--glob",
    "--iglob",
    "--type",
    "--type-not",
    "--max-count",
    "--max-depth",
    "--context",
    "--after-context",
    "--before-context",
    "--file",
    "--regexp",
    "--encoding",
}
_FIND_VALUE_OPTIONS = {
    "-name",
    "-iname",
    "-path",
    "-ipath",
    "-type",
    "-maxdepth",
    "-mindepth",
    "-newer",
    "-perm",
    "-size",
    "-user",
    "-group",
    "-mtime",
    "-atime",
    "-ctime",
    "-regex",
    "-regextype",
    "-printf",
    "-exec",
    "-execdir",
}
# 搜索类的短选项：带值字母吃后续参数（f 的参数是真实读取目标），其余为开关。
# 未知字母无法确定是否吃参数 → 读取目标不可证明，调用方保守处理。
SEARCH_VALUE_LETTERS = {
    "grep": set("efmdDABC"),
    "rg": set("efgtTABCmjMrdE"),
    "ag": set("fGgmABC"),
    "ack": set("fgGmABC"),
}
SEARCH_FLAG_LETTERS = {
    "grep": set("EFGPivwxyclLoqsbHhnrRaIUzZTX0123456789"),
    "rg": set("abchilLnoNpPqsSUuvVwxzFHIM0123456789"),
    "ag": set("acilnopQrsSuUvVwWzZ0123456789"),
    "ack": set("achilnorRsvwx0123456789"),
}


def _search_targets(name: str, rest: List[str], analysis: Analysis) -> None:
    follow = False
    if name == "rg":
        follow = "--follow" in rest or "-L" in rest
    elif name == "find":
        follow = any(token in {"-L", "-H"} and index == 0 for index, token in enumerate(rest))
        follow = follow or "-L" in rest[:2]
    elif name == "ls":
        follow = any(token.startswith("-") and "L" in token for token in rest)
    if follow:
        analysis.recursive_follow = True
    roots: List[str] = []
    pattern_files: List[str] = []
    has_explicit_pattern = False
    # rg 的无 pattern 模式（`--files`）：位置参数全是根，不能把第一个当 pattern。
    patternless = name == "rg" and any(token == "--files" for token in rest)
    index = 0
    positional: List[str] = []
    supports_pattern_file = name in {"rg", "grep", "ag", "ack"}
    while index < len(rest):
        token = rest[index]
        if name == "find":
            if token in _FIND_VALUE_OPTIONS:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            positional.append(token)
            index += 1
            continue
        if token in _READ_VALUE_OPTIONS:
            if token in {"-e", "--regexp"}:
                has_explicit_pattern = True
            if supports_pattern_file and token in {"-f", "--file"}:
                has_explicit_pattern = True
                if index + 1 < len(rest):
                    pattern_files.append(rest[index + 1])
                else:
                    analysis.read_unknown = True
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            option = token.split("=", 1)[0]
            if option == "--regexp":
                has_explicit_pattern = True
            if supports_pattern_file and option == "--file":
                value = token.split("=", 1)[1]
                if value:
                    pattern_files.append(value)
                else:
                    analysis.read_unknown = True
            index += 1
            continue
        if (
            token.startswith("-")
            and not token.startswith("--")
            and len(token) > 1
            and name in SEARCH_FLAG_LETTERS
        ):
            # 短选项集群：`-nf<file>`、`-rf<file>`、`-n`、`-rn` 都要拆开；
            # f/e 等带值字母的参数是真实读取目标。
            letters = token[1:]
            consume_next = False
            for position, letter in enumerate(letters):
                if letter in SEARCH_VALUE_LETTERS.get(name, set()):
                    attached = letters[position + 1 :]
                    value = attached
                    if not value:
                        if index + 1 < len(rest):
                            value = rest[index + 1]
                            consume_next = True
                        else:
                            analysis.read_unknown = True
                    if letter == "e":
                        has_explicit_pattern = True
                    elif letter == "f":
                        if not supports_pattern_file:
                            analysis.read_unknown = True
                        else:
                            has_explicit_pattern = True
                            if value:
                                pattern_files.append(value)
                    break
                if letter in SEARCH_FLAG_LETTERS.get(name, set()):
                    continue
                # 未知短选项组合：不猜哪个字母吃到了参数，读取目标不可证明。
                analysis.read_unknown = True
            index += 2 if consume_next else 1
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        positional.append(token)
        index += 1
    if name == "find":
        roots = positional or ["."]
    elif name in {"rg", "grep", "ag", "ack"}:
        if has_explicit_pattern or patternless:
            roots = positional
        else:
            roots = positional[1:]
        if not roots:
            roots = ["."]
    else:
        roots = positional or ["."]
    for root in roots:
        analysis.reads.append(
            {"kind": name, "path": root, "recursive": True, "follow": follow}
        )
    for pattern_file in pattern_files:
        # 读取型选项参数也是真实读取目标，必须纳入越界检查。
        analysis.reads.append(
            {"kind": name + "-file", "path": pattern_file, "recursive": False, "follow": False}
        )


def _collect_reads(argv: List[str], redirects: List[Tuple[str, str]], analysis: Analysis) -> None:
    for op, target in redirects:
        if op in _READ_REDIRECTS and target not in {"/dev/null"}:
            analysis.reads.append({"kind": "redirect", "path": target, "recursive": False, "follow": False})
    if not argv:
        return
    name = os.path.basename(argv[0])
    rest = argv[1:]
    if name in READ_COMMANDS:
        targets = [token for token in rest if not _looks_like_option(token)] or ["."]
        for target in targets:
            analysis.reads.append({"kind": name, "path": target, "recursive": name in {"cat", "less", "more"}, "follow": False})
        return
    if name in SEARCH_COMMANDS:
        _search_targets(name, rest, analysis)
        return
    if name in {"cp", "mv", "install"}:
        # 共用 operand 布局：`-t/--target-directory` 下所有位置参数都是源，
        # 不能再对 positional 做 `[:-1]`（单源会变成空读取集，多源会漏最后一项）。
        layout = _copy_layout(name, rest)
        if layout["form"] == "incomplete":
            analysis.read_unknown = True
            return
        for target in layout["sources"]:  # type: ignore[union-attr]
            analysis.reads.append(
                {"kind": name + "-source", "path": target, "recursive": False, "follow": False}
            )
        return
    if name in {"python", "python3"}:
        literal = " ".join(rest)
        for match in re.finditer(
            r"(?:open|read_text|read_bytes)\s*\(\s*['\"]([^'\"]+)['\"]", literal
        ):
            analysis.reads.append({"kind": "interpreter", "path": match.group(1), "recursive": False, "follow": False})
        # 明确 literal 接收者：`Path("x").read_text()` / `.read_bytes()` / `.open()`。
        for match in re.finditer(
            r"(?:Path|pathlib\.Path)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\.\s*"
            r"(?:read_text|read_bytes|open|readlink)\s*\(",
            literal,
        ):
            analysis.reads.append({"kind": "interpreter", "path": match.group(1), "recursive": False, "follow": False})
        return


def analyze(
    command: str,
    depth: int = 0,
    budget: Optional[int] = None,
    cwd: Optional[str] = None,
) -> Analysis:
    """分析一条命令；返回可观察的读/写/ Git 动作，opaque 表示无法完整判定。

    `cwd` 是该次工具调用的可信 cwd，用于解析命令内的相对 `cd`；不提供时
    相对 cd 视为不可确定（调用方披露缺口）。
    """
    analysis = Analysis()
    if not isinstance(command, str) or not command.strip():
        return analysis
    limit = normalize_limit(budget)
    if len(command) > limit or depth > MAX_UNWRAP_DEPTH:
        analysis.opaque = True
        analysis.too_deep = True
        return analysis
    tokens, closed = tokenize(command)
    if not closed:
        analysis.opaque = True
        return analysis
    return _analyze_tokens(tokens, analysis, depth, limit, cwd=cwd or None)


def _analyze_tokens(
    tokens: List[Token],
    analysis: Analysis,
    depth: int,
    limit: int,
    cwd: Optional[str] = None,
    cwd_known: bool = True,
) -> Analysis:
    # `cwd` 是进入本段命令时的有效目录（包装内层命令可能带 `env -C`）；
    # `cwd_known=False` 表示调用方无法静态确定该段目录。
    tracked_cwd: Optional[str] = cwd
    for segment in _split_segments(tokens):
        argv, argv_tokens, redirects, redirect_opaque = _strip_redirects(segment)
        analysis.opaque = analysis.opaque or redirect_opaque
        # 只有可能真正求值的 token 才做替换抽样；单引号内容是数据。
        dynamic_text = " ".join(token.text for token in argv_tokens if token.dynamic)
        if dynamic_text:
            for substitution in _expand_substitutions(dynamic_text):
                nested = analyze(substitution, depth + 1, limit)
                analysis.merge(nested)
        is_cd = bool(argv) and os.path.basename(argv[0]) == "cd"
        # 没有命令名的重定向（`> app.py`、`cd src; > app.py`）仍是真实读写
        # 副作用：不能因 argv 为空就跳过该段的 redirect 与有效 cwd。
        # `cd . > file` 的重定向由 shell 在执行 cd 之前打开，因此按进入本段
        # 时的有效 cwd 解析；cwd 在本段重定向处理完后再更新。
        before = len(analysis.reads)
        _collect_reads(argv, redirects, analysis)
        if tracked_cwd:
            for entry in analysis.reads[before:]:
                entry["cwd"] = tracked_cwd
        elif not cwd_known:
            analysis.read_unknown = True
        before_writes = len(analysis.writes)
        _write_targets_for_command(argv, redirects, analysis, tracked_cwd)
        if len(analysis.writes) > before_writes:
            if tracked_cwd:
                # 该段有效 cwd 决定相对写目标的真实位置：`cd sub; >> app.py`
                # 检查的是 sub/app.py，不是工具 cwd 下的同名文件。
                for entry in analysis.writes[before_writes:]:
                    entry["cwd"] = tracked_cwd
            elif not cwd_known:
                analysis.write_unknown = True
        if is_cd:
            tracked_cwd = _track_cd(argv[1:], tracked_cwd)
            if tracked_cwd is None:
                cwd_known = False
            continue
        if not argv:
            continue
        name = os.path.basename(argv[0])
        actions = _git_actions(argv)
        if actions:
            analysis.git.extend(actions)
        analysis.commands.append(argv)
        inner, wrapper_opaque, wrapper_cwd = _wrapper_inner(argv_tokens)
        if wrapper_opaque:
            analysis.opaque = True
        if inner is not None:
            inner_tokens, inner_closed = tokenize(inner)
            if not inner_closed or depth + 1 > MAX_UNWRAP_DEPTH:
                analysis.opaque = True
            else:
                inherited, inherited_known = _wrapper_effective_cwd(
                    wrapper_cwd, tracked_cwd, cwd_known
                )
                nested = _analyze_tokens(
                    inner_tokens,
                    Analysis(),
                    depth + 1,
                    limit,
                    cwd=inherited,
                    cwd_known=inherited_known,
                )
                if not inherited_known and not inherited:
                    if nested.reads:
                        nested.read_unknown = True
                    if nested.writes:
                        nested.write_unknown = True
                analysis.merge(nested)
        else:
            if name not in READ_COMMANDS | SEARCH_COMMANDS | WRITE_COMMANDS | {
                "git", "echo", "printf", "true", "false", "pwd", "cd", "which", "test",
                "[", "grep", "rg", "find", "ls", "cat", "sed", "perl", "python", "python3",
                "node", "curl", "wget", "tee", "dd", "patch", "unzip", "tar", "install",
            } and not any(name in executables for executables, _ in PACKAGE_WRITE_PATTERNS):
                analysis.read_unknown = True
    return analysis


def segment_text(tokens: List[str]) -> str:
    return " ".join(tokens)


def argv_of(command: str) -> List[List[str]]:
    return analyze(command).commands


def read_targets(analysis: Analysis) -> List[Dict[str, object]]:
    return analysis.reads


def write_candidates(analysis: Analysis) -> List[Dict[str, object]]:
    return analysis.writes


def is_read_only_command(command: str) -> Tuple[bool, bool]:
    """返回 (只读且可判定, 有未知/不透明部分)。"""
    analysis = analyze(command)
    if analysis.opaque or analysis.write_unknown:
        return False, True
    if any(not write.get("complete", False) for write in analysis.writes):
        return False, True
    return not analysis.writes, analysis.read_unknown


def reviewer_violation(command: str) -> Optional[Dict[str, object]]:
    """reviewer 只读：已知写场景拒绝；未知命令放行但标记未覆盖。"""
    analysis = analyze(command)
    for action in analysis.git:
        violation = git_violation(action)
        if violation:
            return {"kind": "git", "detail": violation, "unknown": False}
    writes = analysis.writes
    if writes:
        return {
            "kind": "write",
            "detail": "写操作会改动审查目标",
            "unknown": False,
            "targets": [write.get("target") for write in writes],
        }
    if analysis.opaque or analysis.write_unknown:
        return {"kind": "unknown", "detail": "命令结构无法完整判定", "unknown": True}
    return None


def parse_shell(command: str) -> Dict[str, object]:
    return analyze(command).as_dict()


def read_command_paths(
    command: str, cwd: Optional[str] = None
) -> Tuple[List[Dict[str, object]], bool]:
    """回放读取守卫/transcript 用：返回 (读取目标候选, 是否存在无法判定的隐式读取)。

    `cwd` 是该条记录的有效 cwd：相对 `cd` 只有在给出基准时才可静态确定。
    """
    analysis = analyze(command, cwd=cwd)
    return analysis.reads, analysis.opaque or analysis.read_unknown


def command_flags(command: str) -> Dict[str, bool]:
    analysis = analyze(command)
    return {"recursive_follow": analysis.recursive_follow}
