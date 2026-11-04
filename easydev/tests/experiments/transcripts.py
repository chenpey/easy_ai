"""宿主 transcript 只读抽取：把事件流还原成受控的读取记录。

role_metric 与 Hook 共用本模块；输出是内部 records，不是新的公共成绩协议。
只归因明确读工具与静态读命令，不从 prompt、补丁、交接文本里猜路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

if __package__ in (None, ""):
    import shell  # type: ignore[no-redef]
else:  # pragma: no cover - 包内导入
    from . import shell  # type: ignore[no-redef]

READ_TOOL_FIELDS = {
    "read": ("path", "file_path", "filePath", "Path", "absolute_path"),
    "read_file": ("Path", "path", "file_path", "filePath", "absolute_path"),
    "view_file": ("Path", "path", "file_path", "filePath", "absolute_path", "AbsolutePath"),
    "view": ("path", "file_path", "Path"),
    "readfile": ("path", "file_path", "Path"),
    "read_text_file": ("path", "file_path", "filePath"),
    "grep": ("path", "glob", "cwd"),
    "find": ("path", "cwd"),
    "ls": ("path", "cwd"),
}
SEARCH_TOOLS = {"grep", "find", "ls"}
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MALFORMED_LIMIT = 1000
TAIL_BLOCK_BYTES = 64 * 1024
IDENTITY_MAX_BYTES = 256 * 1024


class UserTurn(object):
    """一次「最新原始用户记录」抽取结果；`complete` 表示结论是否可下。"""

    __slots__ = ("text", "key", "scanned", "complete")

    def __init__(
        self, text: Optional[str], key: str, scanned: int, complete: bool
    ) -> None:
        self.text = text
        self.key = key
        self.scanned = scanned
        self.complete = complete


class ReadRecord(dict):
    """一条读取归因：tool/paths/confirmed/actor/source。"""


def _iter_lines(
    path: str,
    deadline: Optional[float] = None,
) -> Iterable[Tuple[int, int, Optional[Dict[str, Any]]]]:
    """no-follow/nonblock 打开，fstat 普通文件后有界读取；FIFO/设备不阻塞。

    产出 (行起始字节偏移, 行号, 解析结果)：偏移与用户记录键（`user_text_key`）
    同一坐标系，调用方可用它判断一条记录是否晚于某个边界。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    chunks: List[bytes] = []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            total = 0
            while total <= MAX_TRANSCRIPT_BYTES:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                chunk = handle.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
    except OSError:
        return
    finally:
        if fd >= 0:
            os.close(fd)
    payload = b"".join(chunks)
    truncated = len(payload) > MAX_TRANSCRIPT_BYTES
    payload = payload[:MAX_TRANSCRIPT_BYTES]
    offset = 0
    for number, raw in enumerate(payload.split(b"\n"), 1):
        line_offset = offset
        offset += len(raw) + 1
        if not raw.strip():
            continue
        try:
            value = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            yield line_offset, number, None
            continue
        if isinstance(value, dict):
            yield line_offset, number, value
        else:
            yield line_offset, number, None
    if truncated:
        yield 0, 0, None


USER_TEXT_KEYS = ("prompt", "text", "content", "message")


def _user_text_from(entry: Dict[str, Any]) -> Optional[str]:
    """只取顶层 user 记录里的原始指令文本；tool_result / 被审文本不算证据。"""
    role = entry.get("role")
    if role is None:
        kind = entry.get("type")
        if kind in {"user", "user_message", "USER_INPUT", "user_input"}:
            role = "user"
        elif isinstance(entry.get("payload"), dict):
            payload = entry["payload"]
            if payload.get("role") == "user" and payload.get("type") in {"message", "user_message"}:
                entry = payload
                role = "user"
    if role != "user":
        return None
    if entry.get("tool_call_id") or entry.get("tool_result") or entry.get("is_error"):
        return None
    for key in USER_TEXT_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            parts = [
                item.get("text")
                for item in value
                if isinstance(item, dict)
                and item.get("type") in {None, "text", "input_text", "output_text"}
                and isinstance(item.get("text"), str)
            ]
            if parts:
                return "\n".join(parts)
    return None


def user_text_key(number: int, text: str) -> str:
    """稳定的用户记录键：行偏移 + 文本摘要，重复投递同一记录得到同一键。"""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return "%d:%s" % (number, digest)


def _tail_lines(data: bytes, base: int) -> Iterable[Tuple[int, bytes]]:
    """把尾部字节切成 (起始偏移, 行) 并由新到旧产出；跳过可能被截断的首行。"""
    lines = data.split(b"\n")
    offsets: List[int] = []
    position = base
    for chunk in lines:
        offsets.append(position)
        position += len(chunk) + 1
    for index in range(len(lines) - 1, -1, -1):
        if index == 0 and base > 0:
            # 文件未读完到头部时，data[0] 可能落在某行中间：不能当成完整记录。
            break
        yield offsets[index], lines[index]


def _tail_latest_user(
    path: str, deadline: Optional[float] = None
) -> Tuple[Optional[Tuple[str, int]], int, bool]:
    """从文件尾部增量扫描最新一条 user 记录。

    返回 `((文本, 记录起始偏移), 已扫描行数, 结论是否完整)`：只在“已找到最新记录”
    或“确实读到文件头仍没有 user 记录”时给出完整结论；读满 2 MiB 或超时仍无法
    判定时返回 `complete=False`，调用方必须披露降级而不是沿用旧记录。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, 0, False
    try:
        try:
            info = os.fstat(fd)
        except OSError:
            return None, 0, False
        if not stat.S_ISREG(info.st_mode):
            return None, 0, False
        position = info.st_size
        data = b""
        base = position
        scanned = 0
        while True:
            for offset, raw in _tail_lines(data, base):
                if not raw.strip():
                    continue
                scanned += 1
                try:
                    value = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, dict):
                    continue
                text = _user_text_from(value)
                if text:
                    return (text, offset), scanned, True
            if base <= 0:
                # 已读到文件头：确实没有 user 记录。
                return None, scanned, True
            if position <= 0:
                return None, scanned, True
            if deadline is not None and time.monotonic() >= deadline:
                return None, scanned, False
            if len(data) >= MAX_TRANSCRIPT_BYTES:
                return None, scanned, False
            step = min(TAIL_BLOCK_BYTES, position, MAX_TRANSCRIPT_BYTES - len(data))
            if step <= 0:
                return None, scanned, False
            position -= step
            try:
                os.lseek(fd, position, os.SEEK_SET)
                block = os.read(fd, step)
            except OSError:
                return None, scanned, False
            if not block:
                return None, scanned, False
            data = block + data
            base = position
    finally:
        os.close(fd)


def latest_user_turn(path: str, deadline: Optional[float] = None) -> UserTurn:
    """有界取得**最新**一条原始用户记录（从尾部增量读取，不在 200 行处截断）。"""
    if not path:
        return UserTurn(None, "", 0, True)
    found, scanned, complete = _tail_latest_user(path, deadline=deadline)
    if found is None:
        return UserTurn(None, "", scanned, complete)
    text, offset = found
    return UserTurn(text, user_text_key(offset, text), scanned, True)


def latest_user_evidence(
    path: str,
    limit: int = 200,
    deadline: Optional[float] = None,
) -> Tuple[Optional[str], str, int]:
    """兼容封装：`limit` 已不再截断尾部扫描，只保留旧签名。"""
    del limit
    turn = latest_user_turn(path, deadline=deadline)
    return turn.text, turn.key, turn.scanned


def latest_user_text(
    path: str,
    limit: int = 200,
    deadline: Optional[float] = None,
) -> Tuple[Optional[str], int]:
    """兼容封装：只要最后一条原始用户指令与已扫描行数。"""
    text, _key, scanned = latest_user_evidence(path, limit=limit, deadline=deadline)
    return text, scanned


def _path_value(args: Any) -> List[str]:
    if not isinstance(args, dict):
        return []
    for key in ("Path", "path", "file_path", "filePath", "absolute_path", "AbsolutePath", "Absolute"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return [value]
    return []


def _absolutize(paths: List[str], cwd: Optional[str]) -> List[str]:
    result: List[str] = []
    for path in paths:
        expanded = os.path.expanduser(path)
        if os.path.isabs(expanded) or not cwd:
            result.append(os.path.normpath(expanded) if os.path.isabs(expanded) else expanded)
        else:
            result.append(os.path.normpath(os.path.join(cwd, expanded)))
    return result


def _read_record_for(
    tool: str,
    targets: Iterable[Tuple[str, Optional[str]]],
    *,
    confirmed: bool,
    actor: Optional[str],
    call_id: Optional[str],
    source: str,
) -> Optional[ReadRecord]:
    """按每个读取目标自己的有效 cwd 绝对化：一条命令内 `cd` 后读的是另一个文件。"""
    resolved: List[str] = []
    for path, base in targets:
        resolved.extend(_absolutize([path], base))
    if not resolved:
        return None
    return ReadRecord(
        tool=tool,
        paths=resolved,
        confirmed=confirmed,
        actor=actor,
        id=call_id,
        source=source,
    )


def _read_record(
    tool: str,
    paths: List[str],
    *,
    confirmed: bool,
    actor: Optional[str],
    call_id: Optional[str],
    cwd: Optional[str],
    source: str,
) -> Optional[ReadRecord]:
    if not paths:
        return None
    return _read_record_for(
        tool,
        [(path, cwd) for path in paths],
        confirmed=confirmed,
        actor=actor,
        call_id=call_id,
        source=source,
    )


def _decode_quoted(value: Any) -> Any:
    """Antigravity 把参数值再编码成 JSON 字符串：配对双引号时解码一次。

    真实 `transcript.jsonl` 里 `args` 的字符串值形如 `"\"/w/a.py\""`；不剥离外层
    引号会让路径带尾引号、`reference_paths` 锚点全部失配，命令也解析不出子命令。
    解码失败或结果不是字符串时保留原值，不猜测修复。
    """
    if not isinstance(value, str):
        return value
    if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
        return value
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value
    return decoded if isinstance(decoded, str) else value


def _antigravity_args(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: _decode_quoted(item) for key, item in value.items()}


def _antigravity_results(entry: Dict[str, Any]) -> Dict[str, bool]:
    results: Dict[str, bool] = {}
    for key in ("tool_results", "toolResults", "results"):
        values = entry.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            identifier = item.get("tool_call_id") or item.get("toolCallId") or item.get("id")
            if not isinstance(identifier, str):
                continue
            status = item.get("status")
            status_text = status.lower() if isinstance(status, str) else ""
            is_error = item.get("is_error", item.get("isError"))
            # 失败证据优先：status=success 不能反转显式 is_error/失败状态。
            if is_error is True or status_text in FAILURE_STATUS:
                success = False
            elif status_text in SUCCESS_STATUS or (status is None and is_error is False):
                success = True
            else:
                success = False
            results[identifier] = bool(success)
    return results


DONE_STATUS = "done"
COMMAND_TOOL_NAMES = {"run_command", "bash", "exec_command", "shell", "powershell"}
# run_command 的成功结论只能看真实退出码：外层 `status` 对失败命令同样是 DONE。
# 真实日志两种写法（`Command exited with code 0.` / `The command exited with code 0.`），
# 且只在宿主元数据头里认，输出正文里的同名字样不是宿主状态。
COMMAND_EXIT_LINE_RE = re.compile(r"^(?:the )?command exited with code (-?\d+)\.?$", re.IGNORECASE)
# 读取类工具的成功靠“没有明确失败标记”判断；标记只在结果头部找，避免文件正文
# 里的 error/failed 字样把成功读取误判为失败。
FAILURE_MARKER_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_./-])"
    r"(error|failed|failure|not found|no such file|permission denied|does not exist|cannot|unable)"
    r"(?![A-Za-z0-9_./-])",
    re.IGNORECASE,
)
# 正文分隔标记：宿主把命令输出放在这一行之后。
RESULT_BODY_MARKERS = {"command output:", "output:"}
# 读取成功的正面证据：结果头里的读取结果行与截断通知。两种都必须匹配宿主完整的头结构，
# 不只匹配前缀，否则行首普通说明文字（格式示例句、数字后粘连文本）也会被当成读取成功。
# 三种真实写法：`Showing lines 1-56 of 56`（read_file）、`Showing lines 1 to 92`（旧版
# view_file）、`Partial read (lines 119-158 of 158). …`（按字节上限截断的行范围读取）。
# `Partial read` 的范围之后只认宿主自己的字节范围与上限说明，再接已知的续读指引：
# 句点之后允许任意尾句时，普通格式说明文字（`… . This is an example …`）会被当成
# 读取成功证据。
READ_RESULT_LINE_RE = re.compile(
    r"^(?:"
    r"Showing lines \d+-\d+ of \d+"
    r"|Showing lines \d+ to \d+"
    r"|Partial read \(lines \d+-\d+ of \d+\)\. "
    r"Showing bytes \d+-\d+ of \d+ in this line range, "
    r"truncated at the \d+-byte limit\."
    r"(?: "
    r"To read the rest of this line range, call this tool again with the same "
    r"StartLine and EndLine and ByteOffset=\d+\."
    r"| To read the rest of this line range, read it with a larger byte budget\."
    r")?"
    r")$"
)
# 输出过长时宿主在结果头里给出的截断通知，完整措辞是 `The tool's output was truncated due to
# being too long.`（其后可接 ` You can find the full output …`）；只引用前缀短语的普通说明
# 文字不算通知。`Here is a preview` 只是该通知之后的预览前言，单独出现不证明这次读取产生
# 了（被截断的）输出。
READ_TRUNCATION_LINE_RE = re.compile(
    r"^The tool's output was truncated due to being too long\."
    r"(?: You can find the full output .*)?$"
)
READ_PREVIEW_PREFIX = "Here is a preview"
# 读取结果头里的宿主行：`Created At:`/`Completed At:` 是任何步骤都有的宿主包装，旧版
# view_file 在读取结果行之前还有三行元数据，规范读取结果还有一行说明加了行号。只用于
# 让证据扫描跨过它们；它们本身不是读取成功的证据。
READ_HEADER_PREFIXES = (
    "Created At:",
    "Completed At:",
    "File Path:",
    "Total Lines:",
    "Total Bytes:",
    "The following code has been modified",
)
GENERIC_HEAD_CHARS = 400


def _header_lines(text: str) -> List[str]:
    """宿主结果元数据头：跳过 `Created At`/`Completed At`，到正文分隔或首段结束。

    真实命令结果有两种写法：退出码紧跟固定头（`…\nCommand exited with code 0.\n\n
    Command output:`），或先空一行再写退出码（`…\n\nThe command exited with code
    0.\nOutput:`）；输出被截断时元数据头会多一段 `Here is a preview …` 前言，退出码
    仍在该段里。三种都只到 `Command output:`/`Output:` 之前的元数据区里认；正文
    里的退出码字样属于被读取的内容，不是宿主状态。
    """
    lines = text.splitlines()
    while lines and lines[0].startswith(("Created At:", "Completed At:")):
        lines.pop(0)
    for index, line in enumerate(lines):
        if line.strip().lower() in RESULT_BODY_MARKERS:
            return lines[:index]
    # 没有正文分隔标记：只取首个非空段，后面都是正文。
    header: List[str] = []
    seen_content = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if seen_content:
                break
            header.append(line)
            continue
        seen_content = True
        header.append(line)
    return header


def _header_text(text: str) -> str:
    """头部文本；没有分隔标记的长结果只取首个非空行（其余是正文）。"""
    header = _header_lines(text)
    joined = "\n".join(header)
    if len(joined) <= GENERIC_HEAD_CHARS:
        return joined
    for line in header:
        if line.strip():
            return line
    return ""


def _command_exit_code(text: str) -> Optional[int]:
    """只从宿主元数据头取退出码；头部缺失返回 None（不猜成功）。"""
    for line in _header_lines(text):
        match = COMMAND_EXIT_LINE_RE.match(line.strip())
        if match:
            return int(match.group(1))
    return None


def _read_has_evidence(text: str) -> bool:
    """读取类工具的成功证据：结果头里的读取结果行或截断通知，而不是“没有 error 字样”。

    宿主读取结果头形如 `Created At`/`Completed At` +（旧版 `File Path:`/`Total Lines:`/
    `Total Bytes:`）+ `Showing lines <int>-<int> of <int>`/`Showing lines <int> to <int>`/
    `Partial read (lines …). Showing bytes … truncated at the …-byte limit. …`，正文是加行号
    的原文（`N: …`），点号之后只认已知的续读指引；输出过长时换成完整措辞的
    `The tool's output was truncated due to being too long. …` 通知（其后可能有
    `Here is a preview …` 前言）。

    因此只扫描首个正文行之前的宿主行：命中整行匹配的读取结果行或完整截断通知即成功；
    只有通用时间戳、空行之后、首段正文恰好以 `Showing lines`/`The tool's output was
    truncated` 之类短语开头、或只在正文里提到这些字样时返回 False（调用方记 requested），
    不默认成功。已知宿主头只用于跨过，本身不是证据；`Here is a preview` 只在同一头部块
    有真实截断通知时才作为可跨过的前言，否则它就是首段正文；遇到第一个既不是已知头也
    不是有效证据的行（含空行）即停止。
    """
    result_seen = False
    truncation_seen = False
    preview_seen = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            break
        if READ_TRUNCATION_LINE_RE.match(stripped):
            truncation_seen = True
            continue
        if READ_RESULT_LINE_RE.match(stripped):
            result_seen = True
            continue
        if stripped.startswith(READ_HEADER_PREFIXES):
            continue
        if stripped.startswith(READ_PREVIEW_PREFIX):
            preview_seen = True
            continue
        # 第一个既不是已知宿主头、也不是有效读取证据的行：正文开始，其后的同名字样
        # 属于被读取的内容。
        break
    if truncation_seen:
        return True
    if preview_seen:
        # 没有同块的真实截断通知时，预览前言本身就是普通说明文字，其后内容不再可信。
        return False
    return result_seen


def _planner_of(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    wrapped = entry.get("PLANNER_RESPONSE")
    planner = wrapped if isinstance(wrapped, dict) else entry
    if not isinstance(planner, dict):
        return None
    if wrapped is None and planner.get("type") != "PLANNER_RESPONSE":
        return None
    return planner


def _generic_confirmed(name: str, entry: Dict[str, Any]) -> bool:
    """一条 GENERIC 结果是否等于该工具调用成功。

    GENERIC 的 `status` 只说明步骤跑完（失败命令也是 DONE）：run_command 必须有
    `Command exited with code 0.`；读取类工具要求 DONE、头部无失败标记，且结果头里有
    读取结果行或截断提示（通用时间戳不算）。
    无法判定时返回 False（调用方记 `requested`），不默认成功。
    """
    status = entry.get("status")
    if not isinstance(status, str) or status.strip().lower() != DONE_STATUS:
        return False
    content = entry.get("content")
    # 缺 content/空 content/非字符串 content 都没有成功证据，保留 requested。
    text = content if isinstance(content, str) else ""
    if not text.strip():
        return False
    if name in COMMAND_TOOL_NAMES:
        return _command_exit_code(text) == 0
    # 只看结果头：文件正文里的 error/failure 字样不是工具失败。短结果（只有消息
    # 本身、没有正文）整体看，避免把多行错误消息当成成功。
    head = _header_text(text)
    head_lines = head.splitlines()
    first = next((line for line in head_lines if line.strip()), "")
    if FAILURE_MARKER_RE.search(first):
        return False
    if len(head) <= GENERIC_HEAD_CHARS and FAILURE_MARKER_RE.search(head):
        return False
    return _read_has_evidence(text)


def _paths_set(records: Iterable[ReadRecord]) -> frozenset:
    """一次工具调用的完整目标集合（单个调用可合法读取多个路径）。"""
    return frozenset(path for record in records for path in record.get("paths") or [])


def antigravity_records(
    path: str,
    cwd: Optional[str] = None,
    actor: Optional[str] = None,
    deadline: Optional[float] = None,
) -> Tuple[List[ReadRecord], List[str]]:
    """解析 Antigravity 原生 transcript（`PLANNER_RESPONSE` + 后续 `GENERIC`）。

    真实日志没有 `tool_calls[].id`，工具结果也不在 planner 行里：一条
    `PLANNER_RESPONSE` 的 k 个 tool_calls 对应紧随其后的 k 条独立 `GENERIC` 行，
    按 `call_index` 一一对应。无真实 ID 时用稳定合成 ID（`ag:<planner偏移>:<call
    序号>`）参与同 ID 冲突/去重；显式 `tool_results`（旧格式/夹具）优先。
    """
    records: List[ReadRecord] = []
    warnings: List[str] = []
    malformed = 0
    explicit: Dict[str, bool] = {}
    entries: List[Tuple[int, Dict[str, Any]]] = []
    # 冲突后该 ID 在本轮抽取中终身排除（含其后重投记录），不允许复活。
    invalid_ids: set = set()
    observed_paths: Dict[str, frozenset] = {}
    for offset, _number, entry in _iter_lines(path, deadline=deadline):
        if entry is None:
            malformed += 1
            continue
        entries.append((offset, entry))
    # 先收集全部显式结果行（不限于 planner 行）：结果行要能确认先前已请求的读取。
    for _offset, entry in entries:
        wrapped = entry.get("PLANNER_RESPONSE")
        planner = wrapped if isinstance(wrapped, dict) else entry
        if isinstance(planner, dict):
            explicit.update(_antigravity_results(planner))
    # 位置映果：每条 PLANNER_RESPONSE 之后、下一条 PLANNER_RESPONSE 之前的
    # GENERIC 行按顺序对应该调用的 call_index（不跨过下一条 planner 行）。
    planner_rows = [index for index, (_offset, entry) in enumerate(entries) if _planner_of(entry) is not None]
    generic_rows: Dict[int, List[Dict[str, Any]]] = {}
    for position, index in enumerate(planner_rows):
        stop = planner_rows[position + 1] if position + 1 < len(planner_rows) else len(entries)
        generic_rows[index] = [
            entries[row][1]
            for row in range(index + 1, stop)
            if entries[row][1].get("type") == "GENERIC"
        ]
    for index, (offset, entry) in enumerate(entries):
        planner = _planner_of(entry)
        if planner is None:
            if entry.get("PLANNER_RESPONSE") is not None:
                malformed += 1
            continue
        if planner.get("cwd") and not cwd:
            cwd = str(planner["cwd"])
        tool_calls = planner.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        results = generic_rows.get(index) or []
        for call_index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                malformed += 1
                continue
            name = call.get("name")
            args = call.get("args")
            if not isinstance(name, str) or not isinstance(args, dict):
                malformed += 1
                continue
            args = _antigravity_args(args)
            real_id = call.get("id") or call.get("tool_call_id") or call.get("toolCallId")
            real_id = real_id if isinstance(real_id, str) and real_id else None
            call_id = real_id or "ag:%d:%d" % (offset, call_index)
            if real_id is not None and real_id in explicit:
                outcome = explicit[real_id] is True
            elif call_index < len(results):
                outcome = _generic_confirmed(name, results[call_index])
            else:
                outcome = False
            produced: List[ReadRecord] = []
            if name in READ_TOOL_FIELDS:
                record = _read_record(
                    name,
                    _path_value(args),
                    confirmed=outcome,
                    actor=actor,
                    call_id=call_id,
                    cwd=cwd,
                    source=os.path.basename(path),
                )
                if record is not None:
                    produced.append(record)
            elif name == "run_command":
                command = args.get("CommandLine") or args.get("command")
                run_cwd = args.get("Cwd") or args.get("cwd") or cwd
                if isinstance(command, str):
                    for read in shell.read_command_paths(command, cwd=run_cwd)[0]:
                        record = _read_record_for(
                            "run_command",
                            [
                                (
                                    str(read.get("path")),
                                    read.get("cwd") or run_cwd,
                                )
                            ],
                            confirmed=outcome,
                            actor=actor,
                            call_id=call_id,
                            source=os.path.basename(path),
                        )
                        if record is not None:
                            produced.append(record)
            if call_id:
                if call_id in invalid_ids:
                    continue
                call_paths = _paths_set(produced)
                if call_id in observed_paths:
                    if observed_paths[call_id] != call_paths:
                        invalid_ids.add(call_id)
                        records = [item for item in records if item.get("id") != call_id]
                        warnings.append(
                            "同一工具 ID 的读取目标发生变化，该 ID 的全部观测不计入"
                        )
                        continue
                else:
                    observed_paths[call_id] = call_paths
            for record in produced:
                if not record["confirmed"]:
                    record["status"] = "requested"
                record["offset"] = offset
                records.append(record)
    if malformed:
        warnings.append("跳过 %d 条无法解析的 Antigravity 记录" % malformed)
    return records, warnings


def _codex_arguments(raw: Any) -> Optional[Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed
    return None


FAILURE_STATUS = {"error", "failed", "failure", "cancelled", "canceled", "timeout", "timed_out", "aborted"}
SUCCESS_STATUS = {"success", "succeeded", "completed", "ok"}


def _status_of(value: Dict[str, Any]) -> str:
    status = value.get("status")
    return status.lower() if isinstance(status, str) else ""


def _codex_output_success(payload: Dict[str, Any]) -> bool:
    """function_call_output 只有在显式成功/exit_code 0/结构化成功标志时才确认。

    普通字符串错误、缺成功标志或无法判定的输出保持 requested/unknown，不能默认成功。
    """
    for source in (payload, payload.get("output")):
        if not isinstance(source, dict):
            continue
        for key in ("is_error", "isError"):
            if source.get(key) is True:
                return False
        if source.get("success") is False:
            return False
        if _status_of(source) in FAILURE_STATUS:
            return False
        exit_code = source.get("exit_code", source.get("exitCode"))
        if exit_code is not None and exit_code not in (0, "0"):
            return False
    for source in (payload, payload.get("output")):
        if not isinstance(source, dict):
            continue
        if source.get("is_error") is False or source.get("success") is True:
            return True
        if _status_of(source) in SUCCESS_STATUS:
            return True
        if source.get("exit_code", source.get("exitCode")) in (0, "0"):
            return True
    return False


def _codex_item_record(
    name: str,
    args: Any,
    *,
    confirmed: bool,
    call_id: Optional[str],
    cwd: Optional[str],
    source: str,
    actor: Optional[str] = None,
) -> Optional[ReadRecord]:
    parsed = _codex_arguments(args)
    if name in READ_TOOL_FIELDS:
        paths = _path_value(parsed)
        record = _read_record(name, paths, confirmed=confirmed, actor=actor, call_id=call_id, cwd=cwd, source=source)
        if record is not None and not confirmed:
            record["status"] = "requested"
        return record
    if name in {"Bash", "bash", "exec_command"} and isinstance(parsed, dict):
        command = parsed.get("command")
        if isinstance(command, str):
            reads, _opaque = shell.read_command_paths(command, cwd=cwd)
            # 每条读取用解析器给出的有效 cwd（`cd sub; cat a.py` 的目标是 sub/a.py）。
            targets = [
                (str(read.get("path")), read.get("cwd") or cwd) for read in reads
            ]
            record = _read_record_for(
                "exec_command",
                targets,
                confirmed=confirmed,
                actor=actor,
                call_id=call_id,
                source=source,
            )
            if record is not None and not confirmed:
                record["status"] = "requested"
            return record
    return None


def codex_records(
    path: str,
    cwd: Optional[str] = None,
    actor: Optional[str] = None,
    deadline: Optional[float] = None,
) -> Tuple[List[ReadRecord], List[str]]:
    """同时支持 rollout（response_item/function_call）与 exec --json（item.*）。"""
    records: List[ReadRecord] = []
    warnings: List[str] = []
    malformed = 0
    seen_completed: Dict[str, ReadRecord] = {}
    outcomes: Dict[str, bool] = {}
    for offset, _number, entry in _iter_lines(path, deadline=deadline):
        if entry is None:
            malformed += 1
            continue
        if entry.get("type") == "session_meta":
            payload = entry.get("payload")
            if isinstance(payload, dict):
                if not cwd and isinstance(payload.get("cwd"), str):
                    cwd = payload["cwd"]
                if not actor and isinstance(payload.get("agent_id"), str):
                    actor = payload["agent_id"]
            continue
        if entry.get("type") == "turn_context":
            payload = entry.get("payload")
            if isinstance(payload, dict) and not cwd and isinstance(payload.get("cwd"), str):
                cwd = payload["cwd"]
            continue
        if entry.get("type") == "response_item":
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                malformed += 1
                continue
            kind = payload.get("type")
            if kind == "function_call":
                name = payload.get("name")
                call_id = payload.get("call_id") or payload.get("id")
                identifier = call_id if isinstance(call_id, str) else None
                record = _codex_item_record(
                    str(name),
                    payload.get("arguments"),
                    confirmed=bool(identifier and outcomes.get(identifier) is True),
                    call_id=identifier,
                    cwd=cwd,
                    source=os.path.basename(path),
                    actor=actor,
                )
                if record is not None:
                    if not record["confirmed"]:
                        record["status"] = "requested"
                    record["offset"] = offset
                    records.append(record)
                    if identifier:
                        seen_completed[identifier] = record
                continue
            if kind == "function_call_output":
                call_id = payload.get("call_id") or payload.get("id")
                if isinstance(call_id, str):
                    ok = _codex_output_success(payload)
                    outcomes[call_id] = ok
                    for record in records:
                        if record.get("id") != call_id:
                            continue
                        record["confirmed"] = ok
                        if ok:
                            record.pop("status", None)
                        else:
                            record["status"] = "failed"
                continue
            continue
        if entry.get("type") in {"item.started", "item.completed", "item.updated"}:
            item = entry.get("item")
            if not isinstance(item, dict):
                malformed += 1
                continue
            completed = entry.get("type") == "item.completed"
            item_type = item.get("type")
            call_id = item.get("id") if isinstance(item.get("id"), str) else None
            if item_type == "command_execution":
                command = item.get("command")
                if not isinstance(command, str):
                    malformed += 1
                    continue
                # 事件完成与进程成功是两件事：有失败证据（非零退出码/is_error/
                # 失败状态）一律不确认；只有显式成功标志才确认。
                exit_code = item.get("exit_code", item.get("exitCode"))
                failed = (
                    item.get("is_error") is True
                    or _status_of(item) in FAILURE_STATUS
                    or (exit_code is not None and exit_code not in (0, "0"))
                )
                explicit_success = (
                    exit_code in (0, "0")
                    or item.get("is_error") is False
                    or _status_of(item) in SUCCESS_STATUS
                )
                success = completed and not failed and explicit_success
                reads, _opaque = shell.read_command_paths(command, cwd=cwd)
                record = _read_record_for(
                    "exec_command",
                    [(str(read.get("path")), read.get("cwd") or cwd) for read in reads],
                    confirmed=bool(success),
                    actor=actor,
                    call_id=call_id,
                    source=os.path.basename(path),
                )
                if record is not None:
                    if not record["confirmed"]:
                        record["status"] = "requested"
                    record["offset"] = offset
                    if call_id:
                        seen_completed[call_id] = record
                    records.append(record)
                continue
            if item_type in {"tool_call", "function_call"}:
                name = item.get("name") or item.get("tool_name")
                if not isinstance(name, str):
                    malformed += 1
                    continue
                record = _codex_item_record(
                    name,
                    item.get("arguments") or item.get("input"),
                    # `item.completed` 是宿主结构化成功事件；显式失败状态仍排除。
                    confirmed=completed
                    and item.get("is_error") is not True
                    and _status_of(item) not in FAILURE_STATUS,
                    call_id=call_id,
                    cwd=cwd,
                    source=os.path.basename(path),
                    actor=actor,
                )
                if record is not None:
                    if not record["confirmed"]:
                        record["status"] = "requested"
                    record["offset"] = offset
                    if call_id:
                        seen_completed[call_id] = record
                    records.append(record)
                continue
            continue
    # 同 id 只保留一次；路径集合变化时该 id 的观测终身作废（含其后重投）。
    ordered: List[ReadRecord] = []
    groups: Dict[str, List[ReadRecord]] = {}
    order: List[str] = []
    for record in records:
        identifier = record.get("id")
        if not identifier:
            ordered.append(record)
            continue
        if identifier not in groups:
            groups[identifier] = []
            order.append(identifier)
        groups[identifier].append(record)
    conflicting = set()
    for identifier in order:
        group = groups[identifier]
        if len({_paths_set([item]) for item in group}) > 1:
            conflicting.add(identifier)
            continue
        merged = group[0]
        for extra in group[1:]:
            if extra.get("confirmed") and not merged.get("confirmed"):
                merged.update(extra)
        ordered.append(merged)
    if conflicting:
        warnings.append("同一工具 ID 的读取目标发生变化，该 ID 的全部观测不计入")
    if malformed:
        warnings.append("跳过 %d 条无法解析的 Codex 记录" % malformed)
    return ordered, warnings


def read_records(
    host: str,
    path: str,
    *,
    cwd: Optional[str] = None,
    actor: Optional[str] = None,
    deadline: Optional[float] = None,
) -> Tuple[List[ReadRecord], List[str]]:
    if host == "antigravity":
        return antigravity_records(path, cwd=cwd, actor=actor, deadline=deadline)
    if host == "codex":
        return codex_records(path, cwd=cwd, actor=actor, deadline=deadline)
    return [], ["未知宿主 %s，未抽取读取记录" % host]


def transcript_identity(
    host: str, path: str, deadline: Optional[float] = None
) -> Dict[str, str]:
    """transcript 自带的有来源身份（session/actor/thread），只读文件头部。

    原生 `session_meta` 给出的是宿主自己的 session/thread 与 agent 身份；Hook
    必须用它核对“这条 transcript 是否就是当前会话”，不能把明确无关的会话当成
    当前会话。
    """
    identity = {"session": "", "actor": "", "thread": ""}
    if host != "codex" or not path:
        return identity
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return identity
    try:
        try:
            info = os.fstat(fd)
        except OSError:
            return identity
        if not stat.S_ISREG(info.st_mode):
            return identity
        try:
            payload = os.read(fd, IDENTITY_MAX_BYTES)
        except OSError:
            return identity
    finally:
        os.close(fd)
    for raw in payload.decode("utf-8", "replace").splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "session_meta":
            continue
        meta = entry.get("payload")
        if not isinstance(meta, dict):
            continue
        for key, field in (
            ("session", "id"),
            ("thread", "thread_id"),
            ("actor", "agent_id"),
        ):
            value = meta.get(field)
            if isinstance(value, str) and value and not identity[key]:
                identity[key] = value
        break
    return identity


def confirmed_skill_read_offset(
    host: str,
    path: str,
    skill_file: str,
    *,
    actor: Optional[str] = None,
    session: Optional[str] = None,
    deadline: Optional[float] = None,
) -> int:
    """最近一次确认成功的 SKILL 读取在 transcript 里的字节偏移；-1 表示没有。

    偏移与用户记录键同坐标系，调用方用它把“停用前的历史读取”和“停用后的新
    读取”分开：只有后者才是新的激活证据。0 是合法偏移（记录在文件开头）。
    """
    if not path or not skill_file:
        return -1
    identity = transcript_identity(host, path, deadline=deadline)
    # 明确不匹配的原生身份不能退回“无身份”激活。
    if session:
        for key in ("session", "thread"):
            value = identity.get(key) or ""
            if value and value != session:
                return -1
    expected_actor = identity.get("actor") or ""
    if actor and expected_actor and expected_actor != actor:
        return -1
    target = os.path.realpath(skill_file)
    try:
        # 不把调用方 actor 当参数传入：Codex 的 actor 由 transcript 自己的
        # session_meta 决定，否则无关 actor 会退化成“恒等于期望值”的自证。
        records, _warnings = read_records(host, path, deadline=deadline)
    except Exception:  # transcript 结构异常不影响其余判定
        return -1
    newest = -1
    for record in records:
        if not record.get("confirmed"):
            continue
        record_actor = record.get("actor")
        if actor and record_actor and record_actor != actor:
            # 只认同 actor 的读取；其他 actor/无关会话不能激活本 actor。
            continue
        for candidate in record.get("paths") or []:
            if os.path.realpath(str(candidate)) == target:
                newest = max(newest, int(record.get("offset") or 0))
                break
    return newest


def reference_paths(records: Iterable[ReadRecord]) -> List[str]:
    """references/*.md 的读取；只看读取记录，不看交接文本。"""
    pattern = re.compile(r"(?:^|/)references/([A-Za-z0-9_.-]+\.md)$")
    found = set()
    for record in records:
        for path in record.get("paths") or []:
            match = pattern.search(str(path).replace("\\", "/"))
            if match:
                found.add(match.group(1))
    return sorted(found)
