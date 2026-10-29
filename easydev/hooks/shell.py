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
    "sort",
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
# 已建模的读取/搜索/写入与常见简单命令：其余命令名仍按读取未知披露。
_KNOWN_COMMANDS = frozenset(
    READ_COMMANDS
    | SEARCH_COMMANDS
    | WRITE_COMMANDS
    | {
        "git",
        "echo",
        "printf",
        "true",
        "false",
        "pwd",
        "cd",
        "which",
        "test",
        "[",
        "sed",
        "perl",
        "python",
        "python3",
        "node",
        "curl",
        "wget",
    }
)

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
        # 已识别为复合结构但无法安全配对/归属：reviewer 侧的窄拒绝类别。
        self.compound_unresolved = False

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
        self.compound_unresolved = self.compound_unresolved or other.compound_unresolved

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
            "compound_unresolved": self.compound_unresolved,
        }


def normalize_limit(value: Optional[int]) -> int:
    return value if isinstance(value, int) and value > 0 else MAX_COMMAND_BYTES


class Token(object):
    """一个命令行 token；保留引号语义，避免把字面数据当成执行行为。

    `operator` 只在裸（未引用）位置出现，`dynamic` 表示内容可能发生替换；单引号
    与反斜杠转义的内容两者都为假。`raw_source` 是同一份词法结果上的原始片段
    （含引号与反斜杠），替换扫描按它判定真实求值，禁止用去引用后的 `text` 反推。
    `quoted` 是「裸写 vs 被引用/转义」的事实：`text` 已去引用，保留字与结构关键
    字只认裸写，所以它必须由词法阶段记录，不能用 `dynamic` 反推。手工构造的
    token 可以不提供来源（保持既有构造调用兼容），默认按裸写处理。
    """

    __slots__ = ("text", "operator", "dynamic", "raw_source", "quoted")

    def __init__(
        self,
        text: str,
        operator: bool = False,
        dynamic: bool = False,
        raw_source: Optional[str] = None,
        quoted: bool = False,
    ) -> None:
        self.text = text
        self.operator = operator
        self.dynamic = dynamic
        self.raw_source = raw_source
        self.quoted = quoted


def _is_fd_word(word: str) -> bool:
    """`>&word` 的 word 是否指向 fd（纯数字或 `-`）。

    引号不改变结论（`>&"1"` 仍展开成 1）；可能展开的 word 内容不是数字，按文件
    目标处理更安全。
    """
    return bool(_FD_WORD_RE.match(word))


_SPACE_CHARS = " \t\n"
_TOP_LEVEL_OPERATOR_CHARS = "|;&<>"


def _is_standalone_group_char(text: str, index: int) -> bool:
    """裸 `{`/`}` 只有独立成词才是分组符号：`foo{a,b}`、find 的 `{}` 仍是 word。"""
    following = text[index + 1] if index + 1 < len(text) else ""
    return not following or following in _SPACE_CHARS + "|;&<>()"


def _scan_structure(text: str, index: int = 0):
    """统一词法上下文：逐字符产出 `(位置, 种类, 嵌套层数, 框架类型)`。

    引号/转义/注释/命令替换嵌套只在这里定义一次：续行预处理、词切分与替换配对都
    消费同一份规则，避免各写一套“近似”状态机。每层保存 quote 与词首状态，双引号
    内进入 `$()`/反引号时压入新的命令上下文，闭合后恢复外层引号。种类：

    - `code`：当前层代码字符（嵌套层数 >0 表示位于命令替换内部）
    - `op`/`space`/`grammar`：顶层运算符字符/空白/裸 `( ) { }`
    - `quoted`/`literal`：双引号/单引号内的字面字符
    - `quote_open`/`quote_close`：引号定界符本身
    - `escape`：反斜杠转义对的首字符（被转义字符不再单独产出）
    - `continuation`：反斜杠 + 真实换行（单引号外可删除）
    - `comment`：注释字符（词首 `#` 起至物理行尾，反斜杠不续行）
    - `open`/`close`：进入/离开 `$(...)` 或反引号（框架类型标识是哪一个）
    """
    stack: List[List[object]] = []
    quote: Optional[str] = None
    word_start = True
    i = index
    length = len(text)
    while i < length:
        char = text[i]
        depth = len(stack)
        if quote == "'":
            if char == "'":
                quote = None
                yield (i, "quote_close", depth, None)
            else:
                yield (i, "literal", depth, None)
            i += 1
            continue
        if quote == '"':
            if char == "\\" and i + 1 < length:
                yield (i, "continuation" if text[i + 1] == "\n" else "escape", depth, None)
                i += 2
                continue
            if char == '"':
                quote = None
                yield (i, "quote_close", depth, None)
                i += 1
                continue
            if char == "$" and text.startswith("$(", i):
                stack.append(["$(", 1, quote])
                quote = None
                word_start = True
                yield (i, "open", depth, "$(")
                i += 2
                continue
            if char == "`":
                stack.append(["`", 0, quote])
                quote = None
                word_start = True
                yield (i, "open", depth, "`")
                i += 1
                continue
            yield (i, "quoted", depth, None)
            i += 1
            continue
        if char == "\\" and i + 1 < length:
            if text[i + 1] == "\n":
                # 续行是纯删除：它不产生词，也不改变词首状态。若在这里把
                # word_start 置假，`\`+换行 之后的物理行首 `#` 会被当成词内
                # 字符而不是注释，注释末尾的反斜杠又会被当成续行删掉，
                # 结果整行（含其后的真实命令）被吞进注释。
                yield (i, "continuation", depth, None)
            else:
                yield (i, "escape", depth, None)
                word_start = False
            i += 2
            continue
        if stack and stack[-1][0] == "`":
            # 反引号内容整体当一个子上下文：内层 `$(...)` 由递归分析重新发现。
            if char == "`":
                frame = stack.pop()
                quote = frame[2]  # type: ignore[assignment]
                word_start = False
                yield (i, "close", len(stack), "`")
            else:
                yield (i, "code", depth, None)
            i += 1
            continue
        if char in "'\"":
            quote = char
            word_start = False
            yield (i, "quote_open", depth, None)
            i += 1
            continue
        if char == "$" and text.startswith("$(", i):
            stack.append(["$(", 1, None])
            word_start = False
            yield (i, "open", depth, "$(")
            i += 2
            continue
        if char == "`":
            stack.append(["`", 0, None])
            word_start = False
            yield (i, "open", depth, "`")
            i += 1
            continue
        if stack:
            frame = stack[-1]
            if char == "#" and word_start:
                # 内层注释与顶层一样止于物理行尾：注释末尾的反斜杠不续行。
                while i < length and text[i] != "\n":
                    yield (i, "comment", depth, None)
                    i += 1
                word_start = True
                continue
            if char in "()":
                frame[1] = int(frame[1]) + (1 if char == "(" else -1)
                if char == ")" and int(frame[1]) == 0:
                    stack.pop()
                    quote = frame[2]  # type: ignore[assignment]
                    word_start = False
                    yield (i, "close", len(stack), "$(")
                else:
                    yield (i, "code", depth, None)
                    word_start = char == "("
                i += 1
                continue
            yield (i, "code", depth, None)
            word_start = char in _SPACE_CHARS + "|;&("
            i += 1
            continue
        # 顶层、未引用
        if char in _SPACE_CHARS:
            word_start = True
            yield (i, "space", 0, None)
            i += 1
            continue
        if char == "#" and word_start:
            while i < length and text[i] != "\n":
                yield (i, "comment", 0, None)
                i += 1
            continue
        if char in "()" or (
            word_start and char in "{}" and _is_standalone_group_char(text, i)
        ):
            word_start = True
            yield (i, "grammar", 0, None)
            i += 1
            continue
        yield (i, "op" if char in _TOP_LEVEL_OPERATOR_CHARS else "code", 0, None)
        word_start = char in _SPACE_CHARS + "|;&("
        i += 1


def _scan_word(text: str, index: int) -> Tuple[int, str, bool, bool]:
    """消费一个词：返回 (结束位置, 去引用文本, 是否可能展开, 引号是否闭合)。

    引号、转义与命令替换嵌套都交给 `_scan_structure`，因此 `>&1.txt`、`$(...)`
    内部空白与重定向 word 边界共用同一份词法规则。词内替换整体保留在 `text` 里，
    具体执行语义由 `raw_source` 上的替换扫描决定。
    """
    pieces: List[str] = []
    dynamic = False
    end = index
    opened = 0
    closed_count = 0
    for position, kind, depth, frame in _scan_structure(text, index):
        if kind in {"space", "op", "grammar"}:
            break
        if kind == "comment":
            # 注释只在词首（含命令替换内部）开始；它属于该替换的源码，不结束词。
            pieces.append(text[position])
            end = position + 1
            continue
        if kind == "escape":
            if position + 1 < len(text):
                pieces.append(text[position + 1])
                end = position + 2
            else:
                end = position
            continue
        if kind == "continuation":
            end = position + 2
            continue
        if kind == "quote_open":
            opened += 1
            if text[position] == '"':
                dynamic = True
            end = position + 1
            continue
        if kind == "quote_close":
            closed_count += 1
            end = position + 1
            continue
        if kind in {"quoted", "literal"}:
            pieces.append(text[position])
            end = position + 1
            continue
        if kind == "open":
            opened += 1
            dynamic = True
            pieces.append("$(" if frame == "$(" else "`")
            end = position + (2 if frame == "$(" else 1)
            continue
        if kind == "close":
            closed_count += 1
            pieces.append(")" if frame == "$(" else "`")
            end = position + 1
            continue
        pieces.append(text[position])
        if depth == 0:
            dynamic = True
        end = position + 1
    return end, "".join(pieces), dynamic, opened == closed_count


def _substitution_spans(text: str) -> Tuple[List[str], bool]:
    """单趟栈式配对：返回 (要递归分析的替换内容, 是否存在未闭合替换)。

    完整跨度优先：未闭合的节点只丢自己，其中已完整的内层仍然提升为结果；选中
    区间按源码顺序且互不重叠，扫描、选择与输出都是 O(n)。
    """
    spans: List[Dict[str, object]] = []
    stack: List[int] = []
    for position, kind, _depth, frame in _scan_structure(text):
        if kind == "open":
            parent = stack[-1] if stack else None
            spans.append(
                {
                    "start": position + (2 if frame == "$(" else 1),
                    "end": None,
                    "parent": parent,
                    "children": [],
                }
            )
            if parent is not None:
                children = spans[parent]["children"]
                children.append(len(spans) - 1)  # type: ignore[union-attr]
            stack.append(len(spans) - 1)
        elif kind == "close" and stack:
            spans[stack.pop()]["end"] = position
    unclosed = any(span["end"] is None for span in spans)
    selected: List[str] = []
    pending = [index for index, span in enumerate(spans) if span["parent"] is None]
    pending.reverse()
    while pending:
        span_index = pending.pop()
        span = spans[span_index]
        end = span["end"]
        if end is not None:
            selected.append(text[int(span["start"]) : int(end)])  # type: ignore[arg-type]
            continue
        children = list(span["children"])  # type: ignore[arg-type]
        children.reverse()
        pending.extend(children)
    return selected, unclosed


def _has_evaluation(raw: str) -> bool:
    """该原始片段是否含真实命令替换（未闭合也算，不能用去引用文本反推）。"""
    contents, unclosed = _substitution_spans(raw)
    return bool(contents) or unclosed


def _strip_line_continuations(text: str) -> str:
    """删除单引号外的 `\\`+真实换行（Bash 行续接），其余字节原样保留。

    必须在任何词法/运算符判定之前整体完成：`>` + 反斜杠 + 真实换行 + `>1.txt`
    在 Bash 里是 `>>1.txt`。若只在校验 word 时消除续接，运算符识别仍看原始相邻
    字符，会把第二个 `>` 当成文件名目标，真实文件写入绕过检查。双引号内的续接
    同样消失；其他反斜杠转义原样保留给词法阶段处理。

    引号/注释/嵌套命令替换共用 `_scan_structure` 的同一份词法状态：`#` 只在词首
    开始注释且止于**物理行尾**（注释里的反斜杠不续行），内层单引号与内层注释按
    各自语境判定，因此 `$(printf '%s' 'literal\\` + 换行 + `x')` 的字节不被删除。
    """
    pieces: List[str] = []
    cursor = 0
    for position, kind, _depth, _frame in _scan_structure(text):
        if kind != "continuation":
            continue
        pieces.append(text[cursor:position])
        cursor = position + 2
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)


def tokenize(text: str) -> Tuple[List[Token], bool]:
    """按引号/转义/注释/替换嵌套切成 token；返回 (tokens, closed)。

    入口先消除单引号外的行续接，argv、重定向运算符与目标、cwd 因此共用同一
    份词法结果。词内 `$(...)`/反引号整体保留在一个 token 里（内部空白与 `;` 不再
    切词），裸 `( ) { }` 是语法 token，而 `foo{a,b}`、find 的 `{}` 仍是 word。
    """
    text = _strip_line_continuations(text)
    tokens: List[Token] = []
    previous_end = -1
    closed = True

    def emit(token: Token, end: int) -> None:
        nonlocal previous_end
        tokens.append(token)
        previous_end = end

    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char in " \t":
            i += 1
            continue
        if char == "#":
            while i < length and text[i] != "\n":
                i += 1
            continue
        if text.startswith("&&", i) or text.startswith("||", i) or text.startswith(";;", i):
            emit(Token(text[i : i + 2], True, False, text[i : i + 2]), i + 2)
            i += 2
            continue
        if char in "|;&\n":
            emit(Token(char, True, False, char), i + 1)
            i += 1
            continue
        if char in "<>":
            prefix = ""
            if (
                tokens
                and previous_end == i
                and not tokens[-1].operator
                and re.match(r"^[0-9]+$", tokens[-1].text)
            ):
                # 数字前缀只是被重定向的 fd；`> 1`、`2> 1` 仍是普通文件。
                prefix = tokens.pop().text
            op = prefix + char
            i += 1
            if char == "<":
                if i < length and text[i] == "<":
                    if i + 1 < length and text[i + 1] == "<":
                        op += "<<"
                        i += 2
                    else:
                        op += "<"
                        i += 1
                elif i < length and text[i] == ">":
                    op += ">"
                    i += 1
            else:
                if i < length and text[i] == ">":
                    op += ">"
                    i += 1
                elif i < length and text[i] == "|":
                    op += "|"
                    i += 1
            if i < length and text[i] == "&" and op.endswith((">", "<")):
                # `&` 紧跟重定向才是 fd 复制/关闭或 `>&word`：`>&1`/`2>&1`/`>&-` 是
                # fd 操作，`>&1.txt`/`>&$(...)` 的整段 word 是文件目标。
                file_op = "<" if op.endswith("<") else "&>" + (">" if op.endswith(">>") else "")
                word_start = i + 1
                end, word, word_dynamic, word_closed = _scan_word(text, word_start)
                closed = closed and word_closed
                if not word:
                    # `>& 1` 与 `>&1` 同样是 fd 复制；`>& 1.txt` 的目标是下一个 token。
                    look = end
                    while look < length and text[look] in " \t":
                        look += 1
                    spaced_end, spaced, _spaced_dynamic, spaced_closed = _scan_word(text, look)
                    if (
                        spaced
                        and _is_fd_word(spaced)
                        and not _has_evaluation(text[look:spaced_end])
                    ):
                        closed = closed and spaced_closed
                        emit(Token(op + "&" + spaced, True, False, op + "&" + spaced), spaced_end)
                        i = spaced_end
                        continue
                    emit(Token(file_op, True, False, file_op), end)
                    i = end
                    continue
                raw_word = text[word_start:end]
                if _is_fd_word(word) and not _has_evaluation(raw_word):
                    emit(Token(op + "&" + word, True, False, op + "&" + word), end)
                else:
                    emit(Token(file_op, True, False, file_op), word_start)
                    emit(
                        Token(word, False, word_dynamic, raw_word, raw_word != word),
                        end,
                    )
                i = end
                continue
            emit(Token(op, True, False, op), i)
            continue
        if char in "()" or (char in "{}" and _is_standalone_group_char(text, i)):
            emit(Token(char, True, False, char), i + 1)
            i += 1
            continue
        end, word, dynamic, word_closed = _scan_word(text, i)
        closed = closed and word_closed
        if end <= i:
            end = i + 1
            word = text[i]
            dynamic = True
        raw_word = text[i:end]
        emit(Token(word, False, dynamic, raw_word, raw_word != word), end)
        i = end
    return tokens, closed


_SEPARATOR_OPERATORS = frozenset({";", "&", "\n"})
# 管道绑定比 `&&`/`||` 更紧：`cd x && a | b` 是 `cd x && (a | b)`。
_ANDOR_OPERATORS = frozenset({"&&", "||"})
_PIPE_OPERATORS = frozenset({"|"})
_SIMPLE_STOP_OPERATORS = frozenset(
    {";", "&", "\n", "&&", "||", "|", "(", ")", "{", "}", ";;"}
)
# 无归属时不得当普通 argv 静默放行的保留字。
_UNOWNED_KEYWORDS = frozenset(
    {"then", "elif", "else", "fi", "do", "done", "esac", "in"}
)


def _word_at(tokens: List[Token], index: int, value: str) -> bool:
    """裸写的保留字：`'fi'`/`\\fi` 是普通词，不是结构关键字或结束符。"""
    if index >= len(tokens):
        return False
    token = tokens[index]
    return not token.operator and not token.quoted and token.text == value


def _operator_at(tokens: List[Token], index: int, values: Iterable[str]) -> bool:
    return index < len(tokens) and tokens[index].operator and tokens[index].text in values


def _consume_redirects(tokens: List[Token], index: int) -> Tuple[List[Token], int]:
    """收集分组后的重定向 token（`( list ) > out` 的目标在组入口 cwd 打开）。"""
    collected: List[Token] = []
    while index < len(tokens):
        token = tokens[index]
        if not token.operator or token.text in _SIMPLE_STOP_OPERATORS:
            break
        collected.append(token)
        index += 1
        if _REDIRECT_DUP_RE.match(token.text):
            # fd 复制/关闭自带目标，不消费下一个 token。
            continue
        if index < len(tokens) and not tokens[index].operator:
            collected.append(tokens[index])
            index += 1
    return collected, index


def _parse_sequence(
    tokens: List[Token], index: int, terminators: Iterable[str], depth: int
) -> Tuple[Optional[Dict[str, object]], int, bool]:
    """解析一层命令列表：返回 (节点或 None, 结束位置, 是否成功)。"""
    if depth > MAX_UNWRAP_DEPTH:
        return None, index, False
    parts: List[Dict[str, object]] = []
    while True:
        # 空段/行首与连续的列表分隔符不构成命令（`# 注释` 行尾换行、`a;;` 之类）。
        while _operator_at(tokens, index, _SEPARATOR_OPERATORS):
            index += 1
        node, index, ok = _parse_andor(tokens, index, terminators, depth)
        if not ok:
            return ({"kind": "sequence", "parts": parts} if parts else None), index, False
        if node is None:
            break
        if _operator_at(tokens, index, {"&"}):
            # `&` 让前面的列表在子 shell 里异步执行：副作用仍真实发生，但
            # 其中的 `cd` 不外泄到父 shell。
            node = {"kind": "async", "body": node}
        parts.append(node)
    if not parts:
        return None, index, True
    return {"kind": "sequence", "parts": parts}, index, True


def _parse_pipeline(
    tokens: List[Token], index: int, terminators: Iterable[str], depth: int
) -> Tuple[Optional[Dict[str, object]], int, bool]:
    """解析一条管道（`cmd | cmd`）：管道比 `&&`/`||` 绑得更紧。"""
    node, index, ok = _parse_command(tokens, index, terminators, depth)
    if not ok or node is None:
        return None, index, ok
    while _operator_at(tokens, index, _PIPE_OPERATORS):
        index += 1
        # 与 `&&`/`||` 一致：管道符后的换行是续接。
        while _operator_at(tokens, index, {"\n"}):
            index += 1
        right, index, ok = _parse_command(tokens, index, terminators, depth)
        if not ok or right is None:
            return None, index, False
        node = {"kind": "chain", "connector": "|", "left": node, "right": right}
    return node, index, True


def _parse_andor(
    tokens: List[Token], index: int, terminators: Iterable[str], depth: int
) -> Tuple[Optional[Dict[str, object]], int, bool]:
    node, index, ok = _parse_pipeline(tokens, index, terminators, depth)
    if not ok or node is None:
        return None, index, ok
    while _operator_at(tokens, index, _ANDOR_OPERATORS):
        connector = tokens[index].text
        index += 1
        # bash 在 `&&`/`||`/`|` 后继续等待右侧命令：这里的换行是续接而不是空
        # 命令。当成空段会让整条命令变成无法归属的复合结构，危险 Git 也从
        # `analysis.git` 里消失（只能靠 opaque 兜底），因此按续接跳过。
        while _operator_at(tokens, index, {"\n"}):
            index += 1
        right, index, ok = _parse_pipeline(tokens, index, terminators, depth)
        if not ok or right is None:
            return None, index, False
        node = {"kind": "chain", "connector": connector, "left": node, "right": right}
    return node, index, True


def _parse_command(
    tokens: List[Token], index: int, terminators: Iterable[str], depth: int
) -> Tuple[Optional[Dict[str, object]], int, bool]:
    negated = False
    while index < len(tokens) and not tokens[index].operator and tokens[index].text == "!":
        negated = not negated
        index += 1
    if index >= len(tokens):
        return None, index, not negated
    token = tokens[index]
    node: Dict[str, object]
    if token.quoted:
        # 被引用/转义的词是普通命令词：既不是结束符，也不是 if/for/case/函数等关键字。
        node, index = _collect_simple(tokens, index)
        if node is None:
            return None, index, False
    elif token.operator:
        if token.text in terminators:
            return None, index, True
        if token.text in {"(", "{"}:
            closer = ")" if token.text == "(" else "}"
            body, index, ok = _parse_sequence(
                tokens, index + 1, set(terminators) | {closer}, depth + 1
            )
            if not ok or not _operator_at(tokens, index, {closer}):
                return None, index, False
            redirects, index = _consume_redirects(tokens, index + 1)
            node = {
                "kind": "subshell" if closer == ")" else "brace",
                "body": body,
                "redirects": redirects,
            }
        elif token.text in _SIMPLE_STOP_OPERATORS:
            return None, index, False
        else:
            # 不是分隔符/分组的运算符开头（如 `> app.py`、`2> app.py`）仍是简单段。
            node, index = _collect_simple(tokens, index)
            if node is None:
                return None, index, False
    elif token.text in terminators:
        return None, index, True
    elif token.text in _UNOWNED_KEYWORDS or token.text == "function":
        # 无法归属的关键字不能当成普通 argv 静默放行（函数定义不在有限语法内）。
        return None, index, False
    elif token.text == "if":
        node, index, ok = _parse_if(tokens, index, terminators, depth)
        if not ok:
            return None, index, False
    elif token.text in {"while", "until"}:
        node, index, ok = _parse_loop(tokens, index, terminators, depth)
        if not ok:
            return None, index, False
    elif token.text == "for":
        node, index, ok = _parse_for(tokens, index, terminators, depth)
        if not ok:
            return None, index, False
    elif token.text == "case":
        node, index, ok = _parse_case(tokens, index, terminators, depth)
        if not ok:
            return None, index, False
    else:
        node, index = _collect_simple(tokens, index)
        if node is None:
            return None, index, False
    if negated:
        node = {"kind": "negated", "body": node}
    return node, index, True


def _collect_simple(
    tokens: List[Token], index: int
) -> Tuple[Optional[Dict[str, object]], int]:
    simple: List[Token] = []
    while index < len(tokens):
        candidate = tokens[index]
        if candidate.operator and candidate.text in _SIMPLE_STOP_OPERATORS:
            if candidate.text in {"{", "}"}:
                # 花括号只在**命令位置**才是分组：已经进入 argv 时它是普通词
                # （`printf '%s' {`、`echo }` 都合法）。
                simple.append(Token(candidate.text, False, False, candidate.raw_source))
                index += 1
                continue
            break
        simple.append(candidate)
        index += 1
    if not simple:
        return None, index
    return {"kind": "simple", "tokens": simple}, index


def _parse_if(
    tokens: List[Token], index: int, terminators: Iterable[str], depth: int
) -> Tuple[Optional[Dict[str, object]], int, bool]:
    clauses: List[Tuple[Optional[Dict[str, object]], Optional[Dict[str, object]]]] = []
    index += 1
    body_terms = set(terminators) | {"elif", "else", "fi"}
    while True:
        cond, index, ok = _parse_sequence(tokens, index, set(terminators) | {"then"}, depth + 1)
        if not ok or not _word_at(tokens, index, "then"):
            return None, index, False
        body, index, ok = _parse_sequence(tokens, index + 1, body_terms, depth + 1)
        if not ok:
            return None, index, False
        clauses.append((cond, body))
        if not _word_at(tokens, index, "elif"):
            break
        index += 1
    else_body: Optional[Dict[str, object]] = None
    if _word_at(tokens, index, "else"):
        else_body, index, ok = _parse_sequence(
            tokens, index + 1, set(terminators) | {"fi"}, depth + 1
        )
        if not ok:
            return None, index, False
    if not _word_at(tokens, index, "fi"):
        return None, index, False
    return {"kind": "if", "clauses": clauses, "else_body": else_body}, index + 1, True


def _parse_loop(
    tokens: List[Token], index: int, terminators: Iterable[str], depth: int
) -> Tuple[Optional[Dict[str, object]], int, bool]:
    keyword = tokens[index].text
    cond, index, ok = _parse_sequence(tokens, index + 1, set(terminators) | {"do"}, depth + 1)
    if not ok or not _word_at(tokens, index, "do"):
        return None, index, False
    body, index, ok = _parse_sequence(tokens, index + 1, set(terminators) | {"done"}, depth + 1)
    if not ok or not _word_at(tokens, index, "done"):
        return None, index, False
    return {"kind": "loop", "keyword": keyword, "cond": cond, "body": body}, index + 1, True


def _parse_for(
    tokens: List[Token], index: int, terminators: Iterable[str], depth: int
) -> Tuple[Optional[Dict[str, object]], int, bool]:
    index += 1
    if index >= len(tokens) or tokens[index].operator:
        return None, index, False
    name = tokens[index].text
    index += 1
    words: List[Token] = []
    if _word_at(tokens, index, "in"):
        index += 1
        while index < len(tokens) and not _operator_at(tokens, index, _SEPARATOR_OPERATORS):
            if tokens[index].operator:
                return None, index, False
            words.append(tokens[index])
            index += 1
        if _operator_at(tokens, index, _SEPARATOR_OPERATORS):
            index += 1
    elif _operator_at(tokens, index, _SEPARATOR_OPERATORS):
        index += 1
    if not _word_at(tokens, index, "do"):
        return None, index, False
    body, index, ok = _parse_sequence(tokens, index + 1, set(terminators) | {"done"}, depth + 1)
    if not ok or not _word_at(tokens, index, "done"):
        return None, index, False
    return {"kind": "for", "name": name, "words": words, "body": body}, index + 1, True


def _parse_case(
    tokens: List[Token], index: int, terminators: Iterable[str], depth: int
) -> Tuple[Optional[Dict[str, object]], int, bool]:
    index += 1
    selector: List[Token] = []
    while index < len(tokens) and not _word_at(tokens, index, "in"):
        if tokens[index].operator:
            return None, index, False
        selector.append(tokens[index])
        index += 1
    if not _word_at(tokens, index, "in"):
        return None, index, False
    index += 1
    items: List[Tuple[List[Token], Optional[Dict[str, object]]]] = []
    while True:
        if _word_at(tokens, index, "esac"):
            index += 1
            break
        if index >= len(tokens):
            return None, index, False
        if _operator_at(tokens, index, {"("}):
            index += 1
        patterns: List[Token] = []
        closed_pattern = False
        while index < len(tokens):
            token = tokens[index]
            if token.operator and token.text == ")":
                index += 1
                closed_pattern = True
                break
            if token.operator and token.text != "|":
                return None, index, False
            patterns.append(token)
            index += 1
        if not closed_pattern:
            return None, index, False
        body, index, ok = _parse_sequence(
            tokens, index, set(terminators) | {";;", "esac"}, depth + 1
        )
        if not ok:
            return None, index, False
        items.append((patterns, body))
        if _operator_at(tokens, index, {";;"}):
            index += 1
            continue
        if index >= len(tokens) or _word_at(tokens, index, "esac"):
            continue
        return None, index, False
    return {"kind": "case", "selector": selector, "items": items}, index, True


def _looks_like_option(token: str) -> bool:
    return token.startswith("-") and token != "-"


def _redirect_base(text: str) -> Optional[str]:
    """把重定向运算符规范化成不带 fd 前缀的形式；未知运算符返回 None。"""
    match = _REDIRECT_OPERATOR_RE.match(text)
    return match.group(1) if match else None


def _strip_redirects(
    tokens: List[Token],
    word_effects: Optional[Dict[int, Dict[str, bool]]] = None,
    analysis: Optional[Analysis] = None,
) -> Tuple[List[str], List[Token], List[Tuple[str, str]], bool]:
    """剥离重定向与前置赋值；返回 (argv, argv_tokens, redirects, opaque)。

    `word_effects` 是按 token 位置索引的求值信息，用于把动态重定向目标分流成
    “未知副作用”而不是源码字面路径；它不能改变 argv 或静态重定向的意义。
    """
    effects = word_effects or {}
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
                effect = effects.get(index + 1)
                if (
                    base != "<<<"
                    and effect
                    and (effect.get("dynamic") or effect.get("unclosed"))
                ):
                    # 含真实命令替换的文件目标不是源码名路径：按方向记未知副作用。
                    if analysis is not None:
                        if base in _WRITE_REDIRECTS:
                            analysis.writes.append(
                                {"kind": "redirect", "target": None, "complete": False}
                            )
                            analysis.write_unknown = True
                        if base in _READ_REDIRECTS:
                            analysis.read_unknown = True
                    index += 2
                    continue
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


def _wrapper_inner(tokens: List[Token]) -> Tuple[Optional[str], bool, str, str]:
    """识别 shell/eval/env/sudo/command/exec 包装。

    返回 (内层命令文本或 None, 是否无法安全判定, 内层有效 cwd 或 "", 阶段)。
    - shell `-c`/`eval` 的参数本身就是命令字符串，按原文重新解析（阶段 `script`）；
    - env/sudo/command/exec 之后是 argv，逐项重新引用以保留参数边界（阶段
      `argv`）：同一批外层 word 的执行证据已由外层扫描取得，递归时不得重复合并；
    - `env -C/--chdir`、`env -C<dir>` 是内层命令的有效 cwd，必须保留，
      否则内层相对读/写会被错误地按外层 cwd 定位。
    无参数选项、带值选项和等号选项分开处理；未知选项不吞掉后面的实际命令，
    而是标记 opaque。
    """
    if not tokens:
        return None, False, "", ""
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
                return as_command(rest[index + 1 :]), False, "", "script"
        return None, False, "", ""
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
                    return None, True, "", ""
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
            return None, opaque, "", ""
        return as_argv(rest[index:]), opaque, inner_cwd, "argv"
    if name == "sudo":
        index = 0
        while index < len(rest):
            text = rest[index].text
            if text == "--":
                index += 1
                break
            if text in SUDO_VALUE_OPTIONS:
                if index + 1 >= len(rest):
                    return None, True, "", ""
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
            return None, opaque, "", ""
        return as_argv(rest[index:]), opaque, "", "argv"
    if name in {"command", "exec"}:
        index = 0
        while index < len(rest) and _looks_like_option(rest[index].text):
            index += 1
        if index >= len(rest):
            return None, opaque, "", ""
        return as_argv(rest[index:]), opaque, "", "argv"
    if name == "eval":
        return as_command(rest), opaque, "", "script"
    return None, opaque, "", ""


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
    if value.startswith("~"):
        # 宿主 HOME 与 `~user` 的 passwd 解析都无法在本进程证明，不猜成相对目录。
        return None, False
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
    if target.startswith("~") or target == "-" or re.search(r"[$`*?\[\]{}]", target):
        # 去引用后仍以 `~` 开头即不可静态定位：不展开 HOME/passwd，也不拼相对目录。
        return None
    if os.path.isabs(target):
        return os.path.normpath(target)
    if not current:
        return None
    return os.path.normpath(os.path.join(current, target))


def _git_actions(
    argv: List[str], cwd: Optional[str] = None, cwd_known: bool = True
) -> List[Dict[str, object]]:
    """提取 Git 子命令与有效 cwd。

    连续 `-C dir` 按顺序叠加，相对值基于上一个**具体已知** cwd；绝对 `-C` 可
    恢复已知性。无法证明的目录不借用进程/工具 cwd，而是 `cwd=None` 且
    `cwd_known=False`。
    """
    if not argv or os.path.basename(argv[0]) != "git":
        return []
    index = 1
    globals_seen: List[str] = []
    tracked_cwd = cwd if cwd_known else None
    tracked_known = cwd_known
    while index < len(argv):
        token = argv[index]
        if token in {"-C", "-c"} and index + 1 < len(argv):
            globals_seen.extend(argv[index : index + 2])
            if token == "-C":
                if _has_evaluation(argv[index + 1]):
                    tracked_cwd, tracked_known = None, False
                else:
                    tracked_cwd, tracked_known = _wrapper_effective_cwd(
                        argv[index + 1], tracked_cwd, tracked_known
                    )
            index += 2
            continue
        if token.startswith("-C") and token != "-C" and len(token) > 2:
            globals_seen.append(token)
            if _has_evaluation(token[2:]):
                tracked_cwd, tracked_known = None, False
            else:
                tracked_cwd, tracked_known = _wrapper_effective_cwd(
                    token[2:], tracked_cwd, tracked_known
                )
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
    return [
        {
            "global": globals_seen,
            "sub": argv[index],
            "args": argv[index + 1 :],
            "cwd": tracked_cwd if tracked_known else None,
            "cwd_known": tracked_known,
        }
    ]


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


# `git checkout` 的已知无值开关：跳过即可，不影响位置参数归属。
_CHECKOUT_FLAG_OPTIONS = {
    "-q",
    "--quiet",
    "--progress",
    "--no-progress",
    "-m",
    "--merge",
    "--detach",
    "--guess",
    "--no-guess",
    "-l",
    "--ignore-other-worktrees",
    "--ours",
    "--theirs",
    "-p",
    "--patch",
    "--overlay",
    "--no-overlay",
    "-2",
    "--2way",
    "-t",
    "--track",
    "--no-track",
    "--recurse-submodules",
    "--no-recurse-submodules",
}
# 已知带值选项：值是分支名或冲突风格，不是路径 operand。
_CHECKOUT_VALUE_OPTIONS = {"-b", "-B", "--branch", "--orphan", "--conflict"}
# 短选项集群里带值的字母（`-qb name`、`-bname`）。
_CHECKOUT_VALUE_LETTERS = frozenset("bB")


def _checkout_path_operands(
    args: List[str], cwd: Optional[str], cwd_known: bool
) -> Optional[str]:
    """返回 checkout 位置参数中实际存在的路径（命中即会覆盖工作区文件）。

    只按选项布局取位置参数：`-b/-B/--orphan/--branch`、`--conflict` 的值是分支名/
    冲突风格，永远不作路径；未知选项布局不猜（不把“不确定”当成路径）。`.`/`..`
    直接是路径还原；绝对路径独立可判定，其余相对目标只在有效 cwd 具体已知时才做
    `lexists` 检查——缺少可信 cwd 时不用进程 cwd 猜。
    """
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            # 显式路径分隔符由 git_violation 先行拒绝，这里不重复判定。
            return None
        if token.startswith("--"):
            option, separator, _value = token.partition("=")
            if option in _CHECKOUT_VALUE_OPTIONS:
                index += 1 if separator else 2
                continue
            if option in _CHECKOUT_FLAG_OPTIONS:
                index += 1
                continue
            return None
        if token.startswith("-") and token != "-" and len(token) > 1:
            letters = token[1:]
            consume_next = False
            known = True
            for position, letter in enumerate(letters):
                if letter in _CHECKOUT_VALUE_LETTERS:
                    # 附着值时它自己就是分支名，否则吃掉下一个 token。
                    consume_next = position == len(letters) - 1
                    break
                if ("-" + letter) not in _CHECKOUT_FLAG_OPTIONS:
                    known = False
                    break
            if not known:
                return None
            index += 2 if consume_next else 1
            continue
        if token in {".", ".."}:
            return token
        if os.path.isabs(token):
            if os.path.lexists(token):
                return token
        elif cwd_known and cwd and os.path.lexists(os.path.join(cwd, token)):
            return token
        index += 1
    return None


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
        operand = _checkout_path_operands(
            args,
            action.get("cwd") if isinstance(action.get("cwd"), str) else None,
            bool(action.get("cwd_known", True)),
        )
        if operand:
            return "git checkout %s 会还原工作区路径并丢弃改动" % operand
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
            if token.startswith("+") and len(token) > 1:
                # `+<refspec>` 一律是强制推送目标，不要求带 `:`（`+main` 同样强制）。
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


# rsync 短选项按真实语义分两组：无值字母逐字母列出（白名单唯一来源），带值字母
# 单独一组，两者不相交。白名单之外的字母（如 `Q`/`j`/`M`）没有已核实的语义，
# 一律按“布局不可证明”处理，不猜它是否吃值。
_RSYNC_NO_VALUE_LETTERS = frozenset(
    {
        "0", "4", "6", "8",
        "a", "b", "c", "d",
        "g", "h", "i", "k",
        "l", "m", "n", "o",
        "p", "q", "r", "t",
        "u", "v", "x", "y", "z",
        "C", "D", "E", "F",
        "H", "I", "L", "O",
        "P", "R", "S", "V", "W",
    }
)
_RSYNC_VALUE_LETTERS = frozenset("efBT")

_RSYNC_SHORT_VALUE_OPTIONS = {
    "e": "--rsh",
    "f": "--filter",
    "B": "--block-size",
    "T": "--temp-dir",
}
# 只有影响读写判定的字母映射到长选项名；其余无值字母只记 `-<字母>` 开关标记，
# 不假造未核实的长选项名。
_RSYNC_SHORT_FLAG_OPTIONS = {
    "a": "--archive",
    "b": "--backup",
    "r": "--recursive",
    "t": "--times",
    "H": "--hard-links",
    "R": "--relative",
    "L": "--copy-links",
    "k": "--copy-dirlinks",
    "F": "--filter-files",
}

_RSYNC_VALUE_OPTIONS = frozenset(
    {
        "--exclude",
        "--exclude-from",
        "--include",
        "--include-from",
        "--filter",
        "--files-from",
        "--rsh",
        "--rsync-path",
        "--temp-dir",
        "--partial-dir",
        "--backup-dir",
        "--suffix",
        "--compare-dest",
        "--copy-dest",
        "--link-dest",
        "--chmod",
        "--timeout",
        "--contimeout",
        "--block-size",
        "--bwlimit",
        "--log-file",
        "--log-format",
        "--log-file-format",
        "--password-file",
        "--port",
        "--out-format",
        "--usermap",
        "--groupmap",
        "--chown",
        "--max-size",
        "--min-size",
        "--max-delete",
        "--sockopts",
        "--address",
        "--protocol",
        "--iconv",
        "--compress-level",
        "--skip-compress",
        "--remote-option",
        "--modify-window",
        "--checksum-seed",
        "--stop-after",
        "--stop-at",
        "--checksum-choice",
        "--compress-choice",
        "--max-alloc",
        "--read-batch",
        "--write-batch",
        "--only-write-batch",
    }
)
_RSYNC_FLAG_OPTIONS = frozenset(
    {
        "--archive",
        "--recursive",
        "--checksum",
        "--times",
        "--perms",
        "--owner",
        "--group",
        "--links",
        "--hard-links",
        "--copy-links",
        "--copy-dirlinks",
        "--keep-dirlinks",
        "--relative",
        "--no-relative",
        "--no-implied-dirs",
        "--verbose",
        "--quiet",
        "--human-readable",
        "--progress",
        "--stats",
        "--itemize-changes",
        "--dry-run",
        "--list-only",
        "--help",
        "--version",
        "--compress",
        "--backup",
        "--update",
        "--ignore-existing",
        "--existing",
        "--ignore-non-existing",
        "--size-only",
        "--ignore-times",
        "--whole-file",
        "--sparse",
        "--inplace",
        "--append",
        "--append-verify",
        "--partial",
        "--delay-updates",
        "--delete",
        "--del",
        "--delete-before",
        "--delete-after",
        "--delete-during",
        "--delete-delay",
        "--delete-excluded",
        "--delete-missing-args",
        "--force",
        "--ignore-errors",
        "--ignore-missing-args",
        "--prune-empty-dirs",
        "--remove-source-files",
        "--remove-sent-files",
        "--numeric-ids",
        "--dirs",
        "--no-dirs",
        "--devices",
        "--specials",
        "--executability",
        "--acls",
        "--xattrs",
        "--super",
        "--fake-super",
        "--safe-links",
        "--copy-unsafe-links",
        "--blocking-io",
        "--no-blocking-io",
        "--8-bit-output",
        "--from0",
        "--no-motd",
        "--protect-args",
        "--secluded-args",
    }
)
_RSYNC_AUX_READ_FILES = frozenset(
    {"--exclude-from", "--include-from", "--files-from", "--password-file", "--read-batch"}
)
_RSYNC_AUX_READ_DIRS = frozenset({"--compare-dest", "--copy-dest", "--link-dest"})
_RSYNC_AUX_WRITE_DIRS = frozenset({"--backup-dir", "--temp-dir", "--partial-dir"})
_RSYNC_REMOVE_SOURCE_FLAGS = frozenset({"--remove-source-files", "--remove-sent-files"})
_RSYNC_FOLLOW_LINK_FLAGS = frozenset({"--copy-links", "--copy-dirlinks"})
_RSYNC_BATCH_WRITE_OPTIONS = frozenset({"--write-batch", "--only-write-batch"})
_RSYNC_REMOTE_SIDE_OPTIONS = frozenset({"--rsync-path", "--remote-option"})
# rsync 只在冒号出现在第一个 `/` 之前时把词当远程：`./a:b` 与 `/x/a:b` 是本地路径。
_RSYNC_REMOTE_WORD_RE = re.compile(r"^(?:rsync://|[^/]*:)")
_RSYNC_GLOB_CHARS = "*?[{"


def _rsync_remote_word(word: Optional[str]) -> bool:
    return bool(word) and bool(_RSYNC_REMOTE_WORD_RE.match(str(word)))


def _rsync_word_unresolved(token: Optional[Token]) -> bool:
    r"""该词是否因替换/通配/tilde 展开而无法静态确定路径。

    `Token.dynamic` 不够用：普通双引号也会标 dynamic，而单引号 `'$DEST'`、转义
    `\$DEST` 都是静态。这里按同一份词法结果判定真实求值：命令替换、未转义 `$`、
    未引用通配与词首 `~` 才算不可解析。
    """
    if token is None:
        return False
    raw = token.raw_source if token.raw_source is not None else token.text
    for position, kind, depth, _frame in _scan_structure(raw):
        if kind == "open":
            return True
        if depth != 0 or kind not in {"code", "quoted"}:
            continue
        char = raw[position]
        if char == "$":
            return True
        if kind == "code" and char in _RSYNC_GLOB_CHARS:
            return True
        if kind == "code" and char == "~" and position == 0:
            return True
    return False


def _rsync_layout(
    rest: List[str], tokens: Optional[List[Token]] = None
) -> Dict[str, object]:
    """rsync 的参数布局：选项值、源/目的、特殊形式与未知标志。

    rsync 不是 cp：`-t` 是 `--times`（无值）、`-T` 吃临时目录，也没有
    `--target-directory`。白名单之外的选项可能带值，所以整段布局按“不可证明”
    处理，不把后续 token 猜成源或目的。`tokens` 是与 `rest` 对齐的词法元信息
    （生产调用必须传），为 None 表示静态参数列表。

    返回 `sources`/`destination`/`values`/`flags`/`incomplete`/`read_unknown`/
    `write_unknown`，并用内部字段 `mode` 区分 `unknown`/`read-batch`/
    `files-from`/`missing-operands`/`normal`。`values` 里的 None 表示该选项值不
    可静态定位。
    """
    words = tokens if tokens is not None and len(tokens) == len(rest) else None
    values: Dict[str, List[Optional[str]]] = {}
    flags: set = set()
    positional: List[int] = []
    unknown_option = False
    incomplete = False
    read_unknown = False
    write_unknown = False

    def static_at(index: int) -> bool:
        token = words[index] if words is not None and 0 <= index < len(words) else None
        return not _rsync_word_unresolved(token)

    def take_value(option: str, index: int, attached: Optional[str]) -> int:
        """消费一个带值选项，返回值消费后的下一个索引。"""
        nonlocal incomplete, read_unknown
        if attached is not None:
            value: Optional[str] = attached if static_at(index) else None
            next_index = index + 1
        elif index + 1 < len(rest):
            # 已知带值选项无条件消费下一个词，即使它以 `-` 起头。
            value = rest[index + 1] if static_at(index + 1) else None
            next_index = index + 2
        else:
            # 缺值：布局不完整，不把其他 token 退回源。
            incomplete = True
            return index + 1
        if not value:
            incomplete = True
        values.setdefault(option, []).append(value)
        if option == "--filter":
            # `--filter` 的 merge 规则可能间接读取文件：不把整段规则字符串当路径。
            read_unknown = True
        return next_index

    index = 0
    while index < len(rest):
        word = rest[index]
        if word == "--":
            positional.extend(range(index + 1, len(rest)))
            break
        if word.startswith("--"):
            option, separator, value = word.partition("=")
            if option in _RSYNC_VALUE_OPTIONS:
                if option in _RSYNC_BATCH_WRITE_OPTIONS:
                    # 批次写的主目的与 FILE/FILE.sh 都无法静态枚举：识别名称时即置，
                    # 缺值/空值/动态值也不会漏成可证明的写。
                    incomplete = True
                    write_unknown = True
                index = take_value(option, index, value if separator else None)
                continue
            if option in _RSYNC_FLAG_OPTIONS and not separator:
                flags.add(option)
                index += 1
                continue
            # 未知长选项、伪造的 `--flag=value` 与 `--target-directory` 都不可猜。
            unknown_option = True
            index += 1
            continue
        if word.startswith("-") and word != "-":
            letters = word[1:]
            cluster_done = False
            for offset, letter in enumerate(letters):
                if letter in _RSYNC_VALUE_LETTERS:
                    attached = letters[offset + 1 :]
                    index = take_value(
                        _RSYNC_SHORT_VALUE_OPTIONS[letter], index, attached or None
                    )
                    cluster_done = True
                    break
                if letter in _RSYNC_NO_VALUE_LETTERS:
                    flags.add(_RSYNC_SHORT_FLAG_OPTIONS.get(letter, "-" + letter))
                    continue
                unknown_option = True
                break
            if cluster_done:
                continue
            index += 1
            continue
        positional.append(index)
        index += 1

    if any(not static_at(item) for item in positional):
        incomplete = True
    if unknown_option:
        # 表 1：布局不可证明，先于任何源/目的推断。
        mode = "unknown"
    elif values.get("--read-batch"):
        mode = "read-batch"
    elif values.get("--files-from"):
        mode = "files-from"
    elif len(positional) < 2:
        mode = "missing-operands"
    else:
        mode = "normal"

    sources: List[str] = []
    destination: Optional[str] = None
    if mode in {"read-batch", "files-from", "normal"} and positional:
        last = positional[-1]
        destination = rest[last] if static_at(last) else None
    if mode in {"files-from", "normal"}:
        sources = [rest[item] for item in positional[:-1] if static_at(item)]
    if mode in {"read-batch", "files-from"}:
        # files-from/read-batch 的文件集无法静态枚举，目的只记不完整项。
        read_unknown = True
        write_unknown = True
        incomplete = True
    if mode == "missing-operands":
        # 缺源/缺目的：记未知写，不能退成只读。
        write_unknown = True
        incomplete = True
    if mode == "unknown":
        # 未知选项后的 token 既不是可证明的源，也不是可证明的目的。
        read_unknown = True
        write_unknown = True
        incomplete = True
        sources = []
    if _RSYNC_REMOTE_SIDE_OPTIONS & set(values):
        # 远端命令或额外远端选项会改变实际副作用，无法证明。
        read_unknown = True
        write_unknown = True
        incomplete = True
    if _RSYNC_SHORT_FLAG_OPTIONS["F"] in flags:
        read_unknown = True
    if any(_rsync_remote_word(source) for source in sources):
        # 远程源不是本地路径：不交给本地 stat，也不当作读取。
        sources = [source for source in sources if not _rsync_remote_word(source)]
        read_unknown = True
        incomplete = True
    if _rsync_remote_word(destination):
        destination = None
        write_unknown = True
        incomplete = True
    return {
        "sources": sources,
        "destination": destination,
        "values": values,
        "flags": flags,
        "incomplete": incomplete,
        "read_unknown": read_unknown,
        "write_unknown": write_unknown,
        "mode": mode,
    }


def _rsync_main_targets(
    layout: Dict[str, object], cwd: Optional[str]
) -> List[Tuple[Optional[str], bool]]:
    """决策表 1–7 行：主传输的写入目标与完整性。"""
    mode = str(layout["mode"])
    destination = layout["destination"]
    flags = layout["flags"]  # type: ignore[assignment]
    values = layout["values"]  # type: ignore[assignment]
    if mode in {"unknown", "missing-operands"} or not isinstance(destination, str):
        return [(None, False)]
    if mode in {"read-batch", "files-from"}:
        return [(destination, False)]
    if flags & {"--relative", "--hard-links", "--keep-dirlinks"} or values.get("--link-dest"):
        # 相对路径、硬链接与链接目的目录都不展开 basename，也不声称链接身份可快照。
        return [(destination, False)]
    if "--backup" in flags or values.get("--suffix"):
        # 备份文件布局不枚举：目的本身不是唯一的被改文件。
        return [(destination, False)]
    if layout["incomplete"]:
        return [(destination, False)]
    sources: List[str] = list(layout["sources"])  # type: ignore[arg-type]
    if len(sources) != 1:
        # 多源或源形态不可枚举：只记目的目录本身且不完整。
        return [(destination, False)]
    source = sources[0]
    if _stat_directory(source, cwd) is not False:
        # 目录源与无法定位的源都不能冒充单文件完整落点。
        return [(destination, False)]
    is_directory = _stat_directory(destination, cwd)
    if is_directory is None:
        return [(destination, False)]
    if is_directory:
        # 唯一可展开情形：单普通文件源 + 已存在目录目的。
        return _destination_targets(destination, sources, cwd)
    if destination.endswith("/"):
        # 目录意图但没有可枚举的文件落点，不伪造 basename。
        return [(destination, False)]
    return [(destination, True)]


def _rsync_targets(
    layout: Dict[str, object], cwd: Optional[str]
) -> Tuple[List[Tuple[Optional[str], bool]], bool]:
    """按决策表把 rsync 布局折算成写目标；返回 (目标列表, 是否写入未知)。"""
    targets = _rsync_main_targets(layout, cwd)
    write_unknown = bool(layout["write_unknown"])
    flags = layout["flags"]  # type: ignore[assignment]
    values = layout["values"]  # type: ignore[assignment]
    if layout["mode"] != "read-batch" and flags & _RSYNC_REMOVE_SOURCE_FLAGS:
        # 删除源是明确写副作用；被 read-batch 忽略的显式源不算已知删除源。
        for source in layout["sources"]:  # type: ignore[union-attr]
            targets.append((str(source), _stat_directory(str(source), cwd) is False))
    for value in values.get("--log-file", []):
        targets.append((value, True) if value else (None, False))
    for option in _RSYNC_BATCH_WRITE_OPTIONS:
        for value in values.get(option, []):
            # 批处理文件会写 FILE 与 FILE.sh，内容不可枚举。
            if value:
                targets.append((value, False))
                targets.append((value + ".sh", False))
            else:
                targets.append((None, False))
            write_unknown = True
    for option in sorted(_RSYNC_AUX_WRITE_DIRS):
        for value in values.get(option, []):
            # 空值（`--temp-dir=`）已记布局不完整，不追加空路径候选。
            if value != "":
                targets.append((value, False))
            write_unknown = True
    return targets, write_unknown


def _rsync_reads(layout: Dict[str, object]) -> Tuple[List[Dict[str, object]], bool]:
    """布局的读取侧：实际源、辅助读取文件/目录与无法枚举的读取。"""
    entries: List[Dict[str, object]] = []
    read_unknown = bool(layout["read_unknown"])
    flags = layout["flags"]  # type: ignore[assignment]
    values = layout["values"]  # type: ignore[assignment]
    follow = bool(flags & _RSYNC_FOLLOW_LINK_FLAGS)
    if layout["mode"] != "read-batch":
        # read-batch 的显式源被 rsync 忽略：既不是本地读取，也不是删除源。
        for source in layout["sources"]:  # type: ignore[union-attr]
            entries.append(
                {"kind": "rsync-source", "path": str(source), "recursive": True, "follow": follow}
            )
    for option in sorted(_RSYNC_AUX_READ_FILES):
        for value in values.get(option, []):
            if not value or value == "-":
                # `--files-from=-` 读 stdin，不能记名为 `-` 的文件。
                read_unknown = True
                continue
            entries.append(
                {"kind": "rsync-file", "path": value, "recursive": False, "follow": False}
            )
    for option in sorted(_RSYNC_AUX_READ_DIRS):
        for value in values.get(option, []):
            if not value:
                read_unknown = True
                continue
            entries.append(
                {"kind": "rsync-reference", "path": value, "recursive": True, "follow": False}
            )
    return entries, read_unknown


def _argv_rest_tokens(tokens: Optional[List[Token]]) -> Optional[List[Token]]:
    """argv 词法元信息去掉命令名后的部分；无元信息时返回 None。"""
    return tokens[1:] if tokens else None


def _write_targets_for_command(
    argv: List[str],
    redirects: List[Tuple[str, str]],
    analysis: Analysis,
    cwd: Optional[str] = None,
    cwd_known: bool = True,
    argv_tokens: Optional[List[Token]] = None,
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
    if name == "rsync":
        layout = _rsync_layout(rest, _argv_rest_tokens(argv_tokens))
        targets, write_unknown = _rsync_targets(layout, cwd)
        # 目标是否完整都至少有一个写条目：reviewer 不能因未知布局退成告警放行。
        for target, complete in targets:
            analysis.writes.append({"kind": "rsync", "target": target, "complete": complete})
        analysis.write_unknown = analysis.write_unknown or write_unknown
        return
    if name == "find":
        layout = _find_layout(rest)
        # 删除/执行动作的具体目标不可枚举：只登记“有写副作用且不完整”。
        for _action in range(int(layout["delete"]) + int(layout["exec"])):
            analysis.writes.append({"kind": "find", "target": None, "complete": False})
            analysis.write_unknown = True
        return
    if name == "sort":
        layout = _sort_layout(rest)
        output = layout["output"]
        if isinstance(output, str) and output:
            analysis.writes.append({"kind": "sort", "target": output, "complete": True})
        if layout["output_incomplete"]:
            analysis.writes.append({"kind": "sort", "target": None, "complete": False})
            analysis.write_unknown = True
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
        if name in {"mkdir", "touch", "rm", "rmdir", "chmod", "chown", "truncate", "ln"}:
            targets = positional[1:] if name in {"chmod", "chown"} else positional
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
        actions = _git_actions(argv, cwd=cwd, cwd_known=cwd_known)
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
}
# `find` 的表达式动作：删除与执行。`-exec/-execdir/-ok` 的参数到裸 `;` 或 `+` 才结束。
_FIND_EXEC_ACTIONS = {"-exec", "-execdir", "-ok"}
_FIND_DELETE_ACTIONS = {"-delete"}
# `sort` 的显式输出选项：值是写目标，不是输入文件。
_SORT_OUTPUT_OPTION = "--output"
# `sort` 的已知带值长选项：key/分隔符/缓冲/临时目录等值不是输入文件。
_SORT_VALUE_OPTIONS = {
    "--key",
    "--field-separator",
    "--buffer-size",
    "--temporary-directory",
    "--batch-size",
    "--parallel",
    "--compress-program",
    "--random-source",
    "--files0-from",
}
# 值本身就是被读取的辅助文件；`files0-from` 的间接文件名永远无法证明。
_SORT_AUX_READ_OPTIONS = {"--random-source", "--files0-from"}
_SORT_VALUE_LETTERS = frozenset("ktSTo")
_SORT_FLAG_LETTERS = frozenset("bdfghiMmnRrVcCsuzyq")
# `sort` 的已知无值长选项。
_SORT_FLAG_OPTIONS = {
    "--check",
    "--debug",
    "--dictionary-order",
    "--general-numeric-sort",
    "--help",
    "--human-numeric-sort",
    "--ignore-case",
    "--ignore-leading-blanks",
    "--ignore-nonprinting",
    "--merge",
    "--month-sort",
    "--numeric-sort",
    "--random-sort",
    "--reverse",
    "--stable",
    "--unique",
    "--version",
    "--version-sort",
    "--zero-terminated",
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


def _find_layout(rest: List[str]) -> Dict[str, object]:
    """按 `find [起点…] [表达式…]` 布局分离搜索根与动作。

    起点只到第一个表达式 token（`-` 开头）为止，缺省是 `.`；带值谓词先消费自己的
    值，所以 `find . -name -delete` 里的 `-delete` 是被匹配的模式而不是动作。
    `-exec/-execdir/-ok` 的参数到裸 `;` 或 `+` 才结束，绝不当作搜索根。读端与写端
    共用这份布局，不各自重解一遍。
    """
    roots: List[str] = []
    exec_actions = 0
    delete_actions = 0
    index = 0
    expression_seen = False
    while index < len(rest):
        token = rest[index]
        if not expression_seen and not token.startswith("-"):
            roots.append(token)
            index += 1
            continue
        expression_seen = True
        if token in _FIND_VALUE_OPTIONS:
            # 带值谓词吃掉自己的值；值缺在末尾时直接结束扫描。
            index += 2
            continue
        if token in _FIND_DELETE_ACTIONS:
            delete_actions += 1
            index += 1
            continue
        if token in _FIND_EXEC_ACTIONS:
            exec_actions += 1
            index += 1
            while index < len(rest) and rest[index] not in {";", "+"}:
                index += 1
            # 终止符缺失时这些参数同样不是搜索根，写目标继续保持不完整。
            index += 1
            continue
        index += 1
    return {
        "roots": roots or ["."],
        "exec": exec_actions,
        "delete": delete_actions,
    }


def _sort_layout(rest: List[str]) -> Dict[str, object]:
    """按 `sort [选项] [文件…]` 布局分离输入、显式输出与辅助读取。

    `-o/--output` 的值是写目标，不是输入；缺值或空值记“输出不完整”而不是只读。
    key/分隔符/缓冲/临时目录等已知带值选项的值不是文件；未知选项布局不猜后续
    operand，按读取未知上报。无文件或 `-` 表示 stdin，不凭空读当前目录。
    """
    inputs: List[str] = []
    output = ""
    output_incomplete = False
    auxiliary: List[str] = []
    read_unknown = False
    operands_unknown = False
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            if not operands_unknown:
                for item in rest[index + 1 :]:
                    if item != "-":
                        inputs.append(item)
            break
        if token.startswith("--"):
            option, separator, attached = token.partition("=")
            takes_value = option == _SORT_OUTPUT_OPTION or option in _SORT_VALUE_OPTIONS
            if takes_value:
                if separator:
                    value = attached
                    index += 1
                elif index + 1 < len(rest):
                    value = rest[index + 1]
                    index += 2
                else:
                    value = ""
                    index += 1
                if option == _SORT_OUTPUT_OPTION:
                    if value:
                        output = value
                    else:
                        output_incomplete = True
                    continue
                if option in _SORT_AUX_READ_OPTIONS:
                    if value:
                        auxiliary.append(value)
                    # `--files0-from` 列出的文件名无法证明，辅助读取始终按未知上报。
                    if option == "--files0-from" or not value:
                        read_unknown = True
                    continue
                if not value:
                    read_unknown = True
                continue
            if option in _SORT_FLAG_OPTIONS:
                index += 1
                continue
            # 未知长选项：带等号时它自己吞掉值，否则无法确定是否吃下一个 operand。
            if not separator:
                read_unknown = True
                operands_unknown = True
            index += 1
            continue
        if token.startswith("-") and token != "-" and len(token) > 1:
            letters = token[1:]
            consume_next = False
            for position, letter in enumerate(letters):
                if letter in _SORT_VALUE_LETTERS:
                    attached_value = letters[position + 1 :]
                    if letter == "o":
                        if attached_value:
                            output = attached_value
                        elif index + 1 < len(rest):
                            output = rest[index + 1]
                            consume_next = True
                        else:
                            output_incomplete = True
                    elif not attached_value:
                        if index + 1 < len(rest):
                            # `sort -t -o in`：`-o` 是 `-t` 的值，不是输出开关。
                            consume_next = True
                        else:
                            read_unknown = True
                    break
                if letter not in _SORT_FLAG_LETTERS:
                    # 未知短选项字母：不猜哪个字母吃到了参数。
                    read_unknown = True
                    operands_unknown = True
                    break
            index += 2 if consume_next else 1
            continue
        if token == "-":
            index += 1
            continue
        if not operands_unknown:
            inputs.append(token)
        index += 1
    return {
        "inputs": inputs,
        "output": output,
        "output_incomplete": output_incomplete,
        "auxiliary": auxiliary,
        "read_unknown": read_unknown,
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
    if name == "find":
        layout = _find_layout(rest)
        if layout["exec"]:
            # 不解释被执行程序就无法证明读取边界（回放守卫在此 fail-closed）。
            analysis.read_unknown = True
        for root in layout["roots"]:  # type: ignore[union-attr]
            analysis.reads.append(
                {"kind": "find", "path": root, "recursive": True, "follow": follow}
            )
        return
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
    if name in {"rg", "grep", "ag", "ack"}:
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


def _collect_reads(
    argv: List[str],
    redirects: List[Tuple[str, str]],
    analysis: Analysis,
    argv_tokens: Optional[List[Token]] = None,
) -> None:
    for op, target in redirects:
        if op in _READ_REDIRECTS and target not in {"/dev/null"}:
            analysis.reads.append({"kind": "redirect", "path": target, "recursive": False, "follow": False})
    if not argv:
        return
    name = os.path.basename(argv[0])
    rest = argv[1:]
    if name == "sort":
        # 文件 operand 才是读取目标；`-o/--output` 的值只进写集合。
        layout = _sort_layout(rest)
        for target in layout["inputs"]:  # type: ignore[union-attr]
            analysis.reads.append(
                {"kind": "sort", "path": target, "recursive": False, "follow": False}
            )
        for target in layout["auxiliary"]:  # type: ignore[union-attr]
            analysis.reads.append(
                {"kind": "sort-aux", "path": target, "recursive": False, "follow": False}
            )
        if layout["read_unknown"]:
            analysis.read_unknown = True
        return
    if name in READ_COMMANDS:
        targets = [token for token in rest if not _looks_like_option(token)] or ["."]
        for target in targets:
            analysis.reads.append({"kind": name, "path": target, "recursive": name in {"cat", "less", "more"}, "follow": False})
        return
    if name in SEARCH_COMMANDS:
        _search_targets(name, rest, analysis)
        return
    if name == "rsync":
        layout = _rsync_layout(rest, _argv_rest_tokens(argv_tokens))
        entries, read_unknown = _rsync_reads(layout)
        analysis.reads.extend(entries)
        analysis.read_unknown = analysis.read_unknown or read_unknown
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
    cwd_known: bool = True,
) -> Analysis:
    """分析一条命令；返回可观察的读/写/ Git 动作，opaque 表示无法完整判定。

    `cwd` 是该次工具调用的可信 cwd，用于解析命令内的相对 `cd`；不提供时
    相对 cd 视为不可确定（调用方披露缺口）。`cwd_known=False` 表示命令内已
    发生无法证明的目录变化：此时丢弃残留 cwd 字符串，相对读写不再借工具/请求
    cwd 定位。禁止用 `bool(cwd)` 代替该参数。
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
    try:
        return _analyze_tokens(
            tokens, analysis, depth, limit, cwd=cwd if cwd_known else None, cwd_known=cwd_known
        )
    except RecursionError:
        # 极深结构不该把异常抛到调用方（那会被编码成 fail-open 放行）：保留
        # 已取得的读/写/Git 证据，并显式标记“结构无法完整判定”。
        analysis.opaque = True
        analysis.compound_unresolved = True
        return analysis


def _scan_evaluated_words(
    tokens: List[Token],
    analysis: Analysis,
    depth: int,
    limit: int,
    cwd: Optional[str],
    cwd_known: bool,
    owns_evaluation: bool = True,
) -> Dict[int, Dict[str, bool]]:
    """在过滤重定向/前置赋值之前扫描整段所有实际求值的 word。

    输入是未过滤的 token 列表（argv 不是完整输入），逐个按 `raw_source` 提取真实
    替换并递归分析；返回按 token 位置索引的求值元信息，供动态重定向目标分流。
    `owns_evaluation=False` 用于 argv 包装的递归解包：这些 word 的执行证据已由外层
    扫描取得，这里只重新计算元信息，不重复合并副作用。
    """
    effects: Dict[int, Dict[str, bool]] = {}
    skip_delimiter = False
    for index, token in enumerate(tokens):
        if token.operator:
            if _redirect_base(token.text) == "<<":
                # here-doc 的 delimiter 只做引用去除，不是求值 word。
                skip_delimiter = True
            continue
        if skip_delimiter:
            skip_delimiter = False
            continue
        raw = token.raw_source if token.raw_source is not None else token.text
        if not raw:
            continue
        contents, unclosed = _substitution_spans(raw)
        if contents or unclosed:
            effects[index] = {"dynamic": True, "unclosed": unclosed}
        if not owns_evaluation:
            continue
        for substring in contents:
            # 替换是独立子上下文：进入目录/已知性精确传递，子 cd 不污染父命令。
            analysis.merge(analyze(substring, depth + 1, limit, cwd, cwd_known))
    return effects


def _apply_cwd_rules(
    entries: List[Dict[str, object]],
    start: int,
    analysis: Analysis,
    cwd: Optional[str],
    cwd_known: bool,
    read: bool,
) -> None:
    """逐条目应用 cwd：已知时标注位置，未知且目标相对时才置未知标志。"""
    for entry in entries[start:]:
        path = entry.get("path") if read else entry.get("target")
        if cwd:
            entry["cwd"] = cwd
            continue
        if not cwd_known and isinstance(path, str) and path and not os.path.isabs(path):
            if read:
                analysis.read_unknown = True
            else:
                analysis.write_unknown = True
                entry["complete"] = False


def _analyze_simple_segment(
    tokens: List[Token],
    analysis: Analysis,
    depth: int,
    limit: int,
    cwd: Optional[str],
    cwd_known: bool,
    owns_evaluation: bool = True,
) -> Tuple[Tuple[Optional[str], bool], ...]:
    """分析一段简单命令；返回 (成功出口, 失败出口, 合并出口, 是否可能改 cwd)。

    顺序固定：先扫描（过滤前）全部实际求值的 word，再剥离重定向/前置赋值并做
    既有 argv 与读写分类；已识别的执行证据不能因 argv 为空或包装剥离而丢失。
    """
    entry: Tuple[Optional[str], bool] = (cwd, cwd_known)
    word_effects = _scan_evaluated_words(
        tokens, analysis, depth, limit, cwd, cwd_known, owns_evaluation
    )
    argv, argv_tokens, redirects, redirect_opaque = _strip_redirects(
        tokens, word_effects, analysis
    )
    analysis.opaque = analysis.opaque or redirect_opaque
    # 没有命令名的重定向（`> app.py`、`cd src; > app.py`）仍是真实读写副作用；
    # `cd . > file` 的重定向在 cd 之前打开，因此按进入本段的 cwd 解析。
    before = len(analysis.reads)
    _collect_reads(argv, redirects, analysis, argv_tokens)
    _apply_cwd_rules(analysis.reads, before, analysis, cwd, cwd_known, True)
    before_writes = len(analysis.writes)
    _write_targets_for_command(argv, redirects, analysis, cwd, cwd_known, argv_tokens)
    _apply_cwd_rules(analysis.writes, before_writes, analysis, cwd, cwd_known, False)
    if not argv:
        return entry, entry, entry, False
    if os.path.basename(argv[0]) == "cd":
        target = _track_cd(argv[1:], cwd if cwd_known else None)
        if target is None:
            return (None, False), entry, (None, False), True
        return (target, True), entry, (target, True), True
    analysis.commands.append(argv)
    analysis.git.extend(_git_actions(argv, cwd=cwd, cwd_known=cwd_known))
    inner, wrapper_opaque, wrapper_cwd, wrapper_stage = _wrapper_inner(argv_tokens)
    if wrapper_opaque:
        analysis.opaque = True
    if inner is not None:
        inner_tokens, inner_closed = tokenize(inner)
        if not inner_closed or depth + 1 > MAX_UNWRAP_DEPTH:
            analysis.opaque = True
        else:
            inherited, inherited_known = _wrapper_effective_cwd(wrapper_cwd, cwd, cwd_known)
            # argv 解包不重复合并同一批外层 word；shell -c/eval 是新的求值阶段。
            nested = _analyze_tokens(
                inner_tokens,
                Analysis(),
                depth + 1,
                limit,
                inherited,
                inherited_known,
                wrapper_stage == "script",
            )
            analysis.merge(nested)
    elif os.path.basename(argv[0]) not in _KNOWN_COMMANDS and not any(
        os.path.basename(argv[0]) in executables for executables, _ in PACKAGE_WRITE_PATTERNS
    ):
        analysis.read_unknown = True
    return entry, entry, entry, False


def _merge_cwd(
    first: Tuple[Optional[str], bool], second: Tuple[Optional[str], bool]
) -> Tuple[Optional[str], bool]:
    """两条出口合流：只有完全相同的已知位置才能保留，否则 `(None, False)`。"""
    return first if first == second else (None, False)


def _merge_states(
    states: List[Tuple[Optional[str], bool]],
) -> Tuple[Optional[str], bool]:
    merged = states[0]
    for state in states[1:]:
        merged = _merge_cwd(merged, state)
    return merged


def _segment_has_cd(tokens: List[Token]) -> bool:
    """简单段的命令名是否为 `cd`（重定向目标与前置赋值不是命令名）。"""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.operator:
            index += 2 if _redirect_base(token.text) else 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token.text):
            index += 1
            continue
        return os.path.basename(token.text) == "cd"
    return False


def _node_has_cd(node: Dict[str, object]) -> bool:
    """结构上是否可能在**当前 shell** 里执行 `cd`（subshell/管道/替换不算）。"""
    kind = node["kind"]
    if kind == "simple":
        return _segment_has_cd(node["tokens"])  # type: ignore[arg-type]
    if kind == "negated":
        return _node_has_cd(node["body"])  # type: ignore[arg-type]
    if kind in {"subshell", "async"}:
        # 子 shell 与 `&` 异步列表里的 cd 不影响当前 shell。
        return False
    if kind == "brace":
        body = node.get("body")
        return body is not None and _node_has_cd(body)  # type: ignore[arg-type]
    if kind == "chain":
        if node["connector"] == "|":
            return False
        # 左深链按迭代展开，避免长 `&&` 链在结构判定阶段也递归爆栈。
        current: object = node
        while isinstance(current, dict) and current.get("kind") == "chain":
            if current["connector"] == "|":
                return False
            if _node_has_cd(current["right"]):  # type: ignore[arg-type]
                return True
            current = current.get("left")
        return _node_has_cd(current)  # type: ignore[arg-type]
    if kind == "sequence":
        return any(_node_has_cd(part) for part in node["parts"])  # type: ignore[union-attr]
    if kind == "if":
        for cond, body in node["clauses"]:  # type: ignore[union-attr]
            if (cond is not None and _node_has_cd(cond)) or (
                body is not None and _node_has_cd(body)
            ):
                return True
        else_body = node.get("else_body")
        return else_body is not None and _node_has_cd(else_body)  # type: ignore[arg-type]
    if kind == "loop":
        cond = node.get("cond")
        body = node.get("body")
        return (cond is not None and _node_has_cd(cond)) or (  # type: ignore[arg-type]
            body is not None and _node_has_cd(body)  # type: ignore[arg-type]
        )
    if kind == "for":
        body = node.get("body")
        return body is not None and _node_has_cd(body)  # type: ignore[arg-type]
    if kind == "case":
        for _patterns, body in node["items"]:  # type: ignore[union-attr]
            if body is not None and _node_has_cd(body):
                return True
        return False
    return True


def _analyze_node(
    node: Optional[Dict[str, object]],
    analysis: Analysis,
    depth: int,
    limit: int,
    cwd: Optional[str],
    cwd_known: bool,
    owns_evaluation: bool = True,
) -> Tuple[Tuple[Optional[str], bool], ...]:
    """分析一个结构节点；返回 (成功出口, 失败出口, 合并出口, 是否可能改 cwd)。

    `&&` 右侧从左侧成功边进入、`||` 右侧从失败边进入；顺序列表用合并出口。
    subshell/管道的 cd 不外泄，brace/条件/循环按设计保守合流。
    `owns_evaluation=False` 只用于 argv 包装解包：外层已取过这些 word 的执行证据。
    """
    entry: Tuple[Optional[str], bool] = (cwd, cwd_known)
    if node is None:
        return entry, entry, entry, False
    kind = node["kind"]
    if kind == "simple":
        return _analyze_simple_segment(
            node["tokens"],  # type: ignore[arg-type]
            analysis,
            depth,
            limit,
            cwd,
            cwd_known,
            owns_evaluation,
        )
    if kind == "negated":
        result = _analyze_node(
            node["body"], analysis, depth, limit, cwd, cwd_known, owns_evaluation  # type: ignore[arg-type]
        )
        # `!` 只取反退出状态：成功/失败出口互换，目录副作用摘要照旧。
        return result[1], result[0], result[2], result[3]
    if kind == "async":
        body = node.get("body")
        if body is None:
            return entry, entry, entry, False
        # `&` 在子 shell 里执行：副作用保留，最终 cwd 不回传给父 shell。
        nested = Analysis()
        _analyze_node(
            body,  # type: ignore[arg-type]
            nested,
            depth,
            limit,
            cwd,
            cwd_known,
            owns_evaluation,
        )
        analysis.merge(nested)
        return entry, entry, entry, False
    if kind in {"subshell", "brace"}:
        redirects = node.get("redirects") or []
        if redirects:
            # 组后重定向按组入口 cwd 先记录，然后才分析组内 cd。
            _analyze_simple_segment(
                redirects,  # type: ignore[arg-type]
                analysis,
                depth,
                limit,
                cwd,
                cwd_known,
                owns_evaluation,
            )
        body = node.get("body")
        if body is None:
            return entry, entry, entry, False
        if kind == "subshell":
            nested = Analysis()
            _analyze_node(
                body,  # type: ignore[arg-type]
                nested,
                depth,
                limit,
                cwd,
                cwd_known,
                owns_evaluation,
            )
            analysis.merge(nested)
            return entry, entry, entry, False
        return _analyze_node(
            body, analysis, depth, limit, cwd, cwd_known, owns_evaluation  # type: ignore[arg-type]
        )
    if kind == "chain":
        if node.get("connector") == "|":
            return _analyze_pipeline_node(
                node, analysis, depth, limit, cwd, cwd_known, owns_evaluation
            )
        return _analyze_chain(node, analysis, depth, limit, cwd, cwd_known, owns_evaluation)
    if kind == "sequence":
        state = entry
        success: Tuple[Optional[str], bool] = entry
        failure: Tuple[Optional[str], bool] = entry
        may_change = False
        for part in node["parts"]:  # type: ignore[union-attr]
            result = _analyze_node(
                part, analysis, depth, limit, state[0], state[1], owns_evaluation
            )
            may_change = may_change or bool(result[3])
            state = result[2]
            success, failure = result[0], result[1]
        may_change = may_change or state != entry
        return success, failure, state, may_change
    if kind == "if":
        exits: List[Tuple[Optional[str], bool]] = []
        state = entry
        may_change = False
        for cond, body in node["clauses"]:  # type: ignore[union-attr]
            cond_result = _analyze_node(
                cond, analysis, depth, limit, state[0], state[1], owns_evaluation
            )
            may_change = may_change or bool(cond_result[3])
            # 不执行/常量折叠条件：then 从条件之后的状态进入，可能改 cwd 即未知。
            enter = _merge_cwd(cond_result[0], cond_result[1])
            body_result = _analyze_node(
                body, analysis, depth, limit, enter[0], enter[1], owns_evaluation
            )
            may_change = may_change or bool(body_result[3])
            exits.append(body_result[2])
            state = cond_result[1]
        else_body = node.get("else_body")
        if else_body is None:
            exits.append(state)
        else:
            else_result = _analyze_node(
                else_body,  # type: ignore[arg-type]
                analysis,
                depth,
                limit,
                state[0],
                state[1],
                owns_evaluation,
            )
            may_change = may_change or bool(else_result[3])
            exits.append(else_result[2])
        merged = _merge_states(exits)
        return merged, merged, merged, may_change or merged != entry
    if kind == "loop":
        # 条件与体任一可能改变当前 shell 的 cwd：循环内可重复进入的位置与循环后
        # 都按未知目录分析（零次执行体也必须计入已执行的条件）。
        may_change = _node_has_cd(node)  # type: ignore[arg-type]
        enter = (None, False) if may_change else entry
        _analyze_node(
            node.get("cond"),  # type: ignore[arg-type]
            analysis,
            depth,
            limit,
            enter[0],
            enter[1],
            owns_evaluation,
        )
        _analyze_node(
            node.get("body"),  # type: ignore[arg-type]
            analysis,
            depth,
            limit,
            enter[0],
            enter[1],
            owns_evaluation,
        )
        return enter, enter, enter, may_change
    if kind == "for":
        words = node.get("words") or []
        _scan_evaluated_words(
            words,  # type: ignore[arg-type]
            analysis,
            depth,
            limit,
            cwd,
            cwd_known,
            owns_evaluation,
        )
        may_change = _node_has_cd(node["body"]) if node.get("body") else False  # type: ignore[arg-type]
        enter = (None, False) if may_change else entry
        _analyze_node(
            node.get("body"),  # type: ignore[arg-type]
            analysis,
            depth,
            limit,
            enter[0],
            enter[1],
            owns_evaluation,
        )
        return enter, enter, enter, may_change
    if kind == "case":
        _scan_evaluated_words(
            node["selector"],  # type: ignore[arg-type]
            analysis,
            depth,
            limit,
            cwd,
            cwd_known,
            owns_evaluation,
        )
        exits = [entry]
        may_change = False
        for patterns, body in node["items"]:  # type: ignore[union-attr]
            _scan_evaluated_words(
                patterns, analysis, depth, limit, cwd, cwd_known, owns_evaluation
            )
            if body is None:
                continue
            result = _analyze_node(
                body, analysis, depth, limit, cwd, cwd_known, owns_evaluation
            )
            exits.append(result[2])
            may_change = may_change or bool(result[3])
        merged = _merge_states(exits)
        return merged, merged, merged, may_change or merged != entry
    # 未识别的节点类型：不静默忽略结构。
    analysis.compound_unresolved = True
    analysis.opaque = True
    return entry, entry, entry, False


def _chain_spine(
    node: Dict[str, object], connectors: Iterable[str]
) -> Tuple[List[Dict[str, object]], object]:
    """把同优先级连接符的左深链摊平成 (链节点列表, 最左操作数)。

    `a && b && c` 是 `((a && b) && c)`：递归遍历在长链上会 RecursionError，
    而异常会被上层编码成 fail-open 放行；这里改用显式迭代。`&&`/`||` 同级，
    `|` 是另一级，所以传入的是本层允许的连接符集合。
    """
    allowed = frozenset(connectors)
    spine: List[Dict[str, object]] = []
    current = node
    while True:
        spine.append(current)
        left = current.get("left")
        if (
            isinstance(left, dict)
            and left.get("kind") == "chain"
            and left.get("connector") in allowed
        ):
            current = left
            continue
        return spine, left


def _analyze_chain(
    node: Dict[str, object],
    analysis: Analysis,
    depth: int,
    limit: int,
    cwd: Optional[str],
    cwd_known: bool,
    owns_evaluation: bool = True,
) -> Tuple[Tuple[Optional[str], bool], ...]:
    entry: Tuple[Optional[str], bool] = (cwd, cwd_known)
    spine, leftmost = _chain_spine(node, _ANDOR_OPERATORS)
    result = _analyze_node(
        leftmost, analysis, depth, limit, cwd, cwd_known, owns_evaluation  # type: ignore[arg-type]
    )
    # 从左到右依次应用每一级 `&&`/`||` 的状态传播规则。
    for chain in reversed(spine):
        connector = chain["connector"]
        enter = result[0] if connector == "&&" else result[1]
        right = _analyze_node(
            chain["right"],  # type: ignore[arg-type]
            analysis,
            depth,
            limit,
            enter[0],
            enter[1],
            owns_evaluation,
        )
        may_change = bool(result[3]) or bool(right[3])
        if connector == "&&":
            success = right[0]
            failure = _merge_cwd(result[1], right[1])
        else:
            success = _merge_cwd(result[0], right[0])
            failure = right[1]
        result = (success, failure, _merge_cwd(success, failure), may_change)
    return result


def _analyze_pipeline_node(
    node: Dict[str, object],
    analysis: Analysis,
    depth: int,
    limit: int,
    cwd: Optional[str],
    cwd_known: bool,
    owns_evaluation: bool = True,
) -> Tuple[Tuple[Optional[str], bool], ...]:
    """管道元素各自在子 shell 中运行：cd 不外泄，副作用仍逐个分析。"""
    entry: Tuple[Optional[str], bool] = (cwd, cwd_known)
    spine, leftmost = _chain_spine(node, _PIPE_OPERATORS)
    elements: List[object] = [leftmost]
    # spine 是「外层→内层」（右操作数依次是 c、b），必须反向才能还原源码顺序 a,b,c。
    for chain in reversed(spine):
        elements.append(chain["right"])
    may_change = False
    for element in elements:
        result = _analyze_node(
            element, analysis, depth, limit, cwd, cwd_known, owns_evaluation  # type: ignore[arg-type]
        )
        may_change = may_change or bool(result[3])
    return entry, entry, entry, may_change


def _analyze_tokens(
    tokens: List[Token],
    analysis: Analysis,
    depth: int,
    limit: int,
    cwd: Optional[str] = None,
    cwd_known: bool = True,
    owns_evaluation: bool = True,
) -> Analysis:
    # `cwd` 是进入本段命令时的有效目录（包装内层命令可能带 `env -C`）；
    # `cwd_known=False` 表示调用方无法静态确定该段目录。
    if not cwd_known:
        cwd = None
    node, index, ok = _parse_sequence(tokens, 0, (), 0)
    if not ok or index < len(tokens):
        # 已识别为复合结构却无法安全配对/归属：置窄标志，保留已成功解析的段。
        analysis.compound_unresolved = True
        analysis.opaque = True
        _analyze_node(node, analysis, depth, limit, cwd, cwd_known, owns_evaluation)
        return analysis
    _analyze_node(node, analysis, depth, limit, cwd, cwd_known, owns_evaluation)
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


def reviewer_violation(
    command: str, cwd: Optional[str] = None, cwd_known: bool = True
) -> Optional[Dict[str, object]]:
    """reviewer 只读：先判明确危险 Git/写入，再区分窄的“复合结构无法核对”。

    `compound_unresolved` 是独立类别（`unknown=False`）：已识别为复合结构但无法
    安全配对/归属时拒绝，并提示改用简单命令重跑。其余未知结构仍 `unknown=True`
    告警放行，不统一收紧。
    """
    analysis = analyze(command, cwd=cwd, cwd_known=cwd_known)
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
    if analysis.compound_unresolved:
        return {
            "kind": "compound_unresolved",
            "detail": "复合结构无法核对，请改用简单命令重跑",
            "unknown": False,
        }
    if analysis.opaque or analysis.write_unknown:
        return {"kind": "unknown", "detail": "命令结构无法完整判定", "unknown": True}
    return None


def parse_shell(command: str) -> Dict[str, object]:
    return analyze(command).as_dict()


def read_command_paths(
    command: str, cwd: Optional[str] = None, cwd_known: bool = True
) -> Tuple[List[Dict[str, object]], bool]:
    """回放读取守卫/transcript 用：返回 (读取目标候选, 是否存在无法判定的隐式读取)。

    `cwd` 是该条记录的有效 cwd：相对 `cd` 只有在给出基准时才可静态确定。
    `cwd_known=False` 时丢弃 cwd，命令内得出的相对读取按未知上报，不回落请求 cwd。
    """
    analysis = analyze(command, cwd=cwd, cwd_known=cwd_known)
    return analysis.reads, analysis.opaque or analysis.read_unknown


def command_flags(command: str) -> Dict[str, bool]:
    analysis = analyze(command)
    return {"recursive_follow": analysis.recursive_follow}
