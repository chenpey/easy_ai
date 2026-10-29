"""EasyDev 可选 Hook Plugin 的 CLI 核心。

入口：`python3 core.py --host <host> --context <install-context.json> [--mode probe|enforce]`
读 stdin 一个 UTF-8 JSON object，stdout 只写一个 JSON object。规则顺序：
解析 → 可信激活/角色 → Git → reviewer 写检查 → 基线 → 分派 → 状态事件 → 编码。

运行期故障开路（告警放行）：解析失败、状态不可用、Git/指纹查询失败、超时。
P0 回放守卫是独立策略，不复用本文件的 fail-open。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

if __package__ in (None, ""):
    # 独立 Plugin 目录：按同目录模块导入，不依赖仓库目录名或安装位置。
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import models  # type: ignore[no-redef]
    import paths  # type: ignore[no-redef]
    import shell  # type: ignore[no-redef]
    import state  # type: ignore[no-redef]
    import transcripts  # type: ignore[no-redef]
else:  # pragma: no cover - 包内导入
    from . import models, paths, shell, state, transcripts  # type: ignore

PROTOCOL = 1
MAX_INPUT_BYTES = 1024 * 1024
INTERNAL_BUDGET_MS = 1500
BUDGET_ENV = "EASYDEV_HOOK_BUDGET_MS"
MIN_BUDGET_MS = 20
ROLE_VALUES = {"main", "developer", "reviewer", "designer", "unknown"}
CHILD_SUCCESS_STATUSES = {"completed", "success", "succeeded", "done"}
CHILD_FAILURE_STATUSES = {
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
    "timeout",
    "timed_out",
    "aborted",
    "interrupted",
    "running",
    "started",
    "pending",
    "stopped_early",
}
CHILD_ENV = "PI_SUBAGENT_CHILD"
PARENT_ENV = "PI_SUBAGENT_PARENT_SESSION"
TASK_ROOT_ENV = "EASYDEV_TASK_ROOT"
REVIEWER_ROLE_SUFFIX = "references/reviewer-role.md"
REVIEWER_HEADER_FORMS = (
    "**[P级] 简短标题**",
    "**[P级别] 简短标题**",
    "**[P0/P1/P2/P3] 简短标题**",
)
REVIEWER_SEVERITIES = ("P0", "P1", "P2", "P3")
NEGATION_MARKERS = ("不要", "不需要", "别", "禁止", "不是", "never", "do not", "don't")
TEMPLATE_EXPLANATION_MARKERS = (
    "解释以下",
    "解释这个",
    "说明以下",
    "示例如下",
    "模板如下",
    "以下是交接模板",
    "explain the following",
    "template:",
)
READONLY_MARKERS = re.compile(
    r"(只读|不要修改|不修改|不改代码|不要改动|不要编辑|read[- ]only|do not modify|don't modify|no code changes)",
    re.IGNORECASE,
)
REVIEW_AUDIT_RE = re.compile(r"(review|审查|审计|audit)", re.IGNORECASE)
RUNTIME_RULE_NAMES = {"SKILL.md", "CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md"}
DOC_SUFFIXES = (".md", ".txt", ".rst", ".adoc")
REPORT_NAME_RE = re.compile(
    r"^(?:report|reports|review|findings|audit|summary|handoff|note|notes|progress|fix)[-_.]?",
    re.IGNORECASE,
)
BUILD_MANIFEST_NAMES = {
    "makefile",
    "gnu makefile",
    "dockerfile",
    "containerfile",
    "docker-compose.yml",
    "go.mod",
    "go.sum",
    "cargo.lock",
    "poetry.lock",
    "uv.lock",
    "gemfile",
    "gemfile.lock",
    "cmakelists.txt",
    "requirements.txt",
    "yarn.lock",
    "pnpm-lock.yaml",
    "package-lock.json",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    ".editorconfig",
}
RUNTIME_CONFIG_SUFFIXES = (
    ".conf",
    ".properties",
    ".service",
    ".cmake",
    ".gradle",
    ".lock",
)
CODE_SUFFIXES = (
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
    ".jsonc",
    ".toml",
    ".yml",
    ".yaml",
    ".ini",
    ".cfg",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".kt",
    ".swift",
    ".sql",
)
# 边界用 ASCII 标识符字符类：`\w` 在 Unicode 下把汉字算进去，中文紧邻的
# “请使用 easydev”“帮我用$easydev”会永不命中；`easydev_helpers` 仍不算调用。
EXPLICIT_INVOCATION_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:\$easydev|/skill:easydev|使用\s*easydev|use\s+easydev)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
FENCED_BLOCK_RE = re.compile(r"```([A-Za-z0-9_-]*)\n(.*?)```", re.DOTALL)
ROLE_LINE_RE = re.compile(
    r"^[ \t*-]*(?:角色|role)[ \t]*[:：][ \t]*(main|developer|reviewer|designer)\b",
    re.IGNORECASE | re.MULTILINE,
)
REVIEWER_DECLARATION_RE = re.compile(
    r"(?:独立\s*reviewer|reviewer\s*子代理|作为\s*reviewer|你是\s*(?:一名\s*)?reviewer|role[ \t]*[:：][ \t]*reviewer)",
    re.IGNORECASE,
)
CODE_REVIEW_RE = re.compile(r"(?:diff|代码审查|审查代码|代码范围|review\s+the\s+(?:code|diff))", re.IGNORECASE)
WRITE_TOOL_NAMES = shell.WRITE_TOOL_NAMES
DISPATCH_TOOL_NAMES = shell.DISPATCH_TOOL_NAMES
READ_TOOL_NAMES = shell.READ_TOOL_NAMES


class ProtocolFault(Exception):
    """envelope 字段类型不符：按协议故障告警放行，不输出 traceback。"""


# 已使用字段的精确类型：bool 不再被当作 int/str（`"false"` 不等于真）。
FIELD_TYPES: Dict[str, Dict[str, Tuple[type, ...]]] = {
    "antigravity": {
        "hookEventName": (str,),
        "eventName": (str,),
        "event": (str,),
        "type": (str,),
        "conversationId": (str,),
        "session_id": (str,),
        "sessionId": (str,),
        "agentId": (str,),
        "actorId": (str,),
        "parentConversationId": (str,),
        "parentId": (str,),
        "parent_id": (str,),
        "cwd": (str,),
        "workspacePath": (str,),
        "workspacePaths": (list,),
        "transcriptPath": (str,),
        "transcript_path": (str,),
        "stepIdx": (str, int),
        "step_idx": (str, int),
        "executionNum": (int,),
        "invocationNum": (int,),
        "initialNumSteps": (int,),
        "error": (str,),
        "terminationReason": (str,),
        "prompt": (str,),
        "userMessage": (str,),
        "message": (str,),
        "messageId": (str,),
        "inputId": (str,),
        "eventId": (str,),
        "isChild": (bool,),
        "status": (str,),
        "fullyIdle": (bool,),
        "toolCall": (dict,),
        "tool_call": (dict,),
        "toolName": (str,),
        "tool_name": (str,),
        "toolInput": (dict, str),
        "tool_input": (dict, str),
        "args": (dict, str),
        "toolCallId": (str,),
        "tool_use_id": (str,),
    },
    "codex": {
        "hook_event_name": (str,),
        "hookEventName": (str,),
        "event": (str,),
        "session_id": (str,),
        "sessionId": (str,),
        "agent_id": (str,),
        "agentId": (str,),
        "parent_session_id": (str,),
        "parentId": (str,),
        "cwd": (str,),
        "working_directory": (str,),
        "transcript_path": (str,),
        "transcriptPath": (str,),
        "turn_id": (str,),
        "turnId": (str,),
        "tool_name": (str,),
        "toolName": (str,),
        "tool_input": (dict, str),
        "toolInput": (dict, str),
        "tool_use_id": (str,),
        "toolUseId": (str,),
        "toolCallId": (str,),
        "prompt": (str,),
        "user_prompt": (str,),
        "message": (str,),
        "is_child": (bool,),
        "stop_hook_active": (bool,),
        "outcome": (str,),
        "status": (str,),
        "dispatch_id": (str,),
        "dispatchId": (str,),
        "agent_type": (str,),
        "agentType": (str,),
        "report": (str,),
        "last_assistant_message": (str,),
    },
    "pi": {
        "event": (str,),
        "hookEventName": (str,),
        "type": (str,),
        "sessionId": (str,),
        "session_id": (str,),
        "actorId": (str,),
        "actor_id": (str,),
        "parentId": (str,),
        "parentSessionId": (str,),
        "parent_id": (str,),
        "cwd": (str,),
        "transcriptPath": (str,),
        "transcript_path": (str,),
        "turnKey": (str,),
        "turnId": (str,),
        "toolName": (str,),
        "tool_name": (str,),
        "toolInput": (dict, str),
        "tool_input": (dict, str),
        "input": (dict,),
        "toolCallId": (str,),
        "tool_call_id": (str,),
        "prompt": (str,),
        "text": (str,),
        "inputId": (str,),
        "input_id": (str,),
        "child": (bool,),
        "stopHookActive": (bool,),
        "outcome": (str,),
        "status": (str,),
        "executionNum": (int,),
        "terminationReason": (str,),
        "dispatchId": (str,),
        "report": (str,),
    },
}
FIELD_TYPES["dsh"] = FIELD_TYPES["codex"]


def validate_envelope(host: str, raw: Dict[str, Any]) -> None:
    """对已使用字段做精确类型校验；None 视为缺省，其余不匹配即协议故障。"""
    spec = FIELD_TYPES.get(host, FIELD_TYPES["codex"])
    for name, expected in spec.items():
        if name not in raw:
            continue
        value = raw[name]
        if value is None:
            continue
        if isinstance(value, bool) and bool not in expected:
            raise ProtocolFault("字段 %s 类型不符" % name)
        if not isinstance(value, expected):
            raise ProtocolFault("字段 %s 类型不符" % name)


class Record(object):
    __slots__ = (
        "protocol",
        "host",
        "event",
        "session_id",
        "actor_id",
        "parent_id",
        "root_actor_id",
        "task_identity",
        "activation_scope",
        "turn_key",
        "cwd",
        "transcript_path",
        "tool",
        "prompt",
        "outcome",
        "capabilities",
        "raw",
        "input_id",
        "child",
        "stop_hook_active",
        "execution_num",
        "termination_reason",
    )

    def __init__(self, host: str, raw: Dict[str, Any], capabilities: Dict[str, Any]) -> None:
        self.protocol = PROTOCOL
        self.host = host
        self.raw = raw
        self.capabilities = capabilities
        self.event = "unknown"
        self.session_id = ""
        self.actor_id = ""
        self.parent_id = ""
        self.root_actor_id = ""
        self.task_identity = ""
        self.activation_scope = "inactive"
        self.turn_key = ""
        self.cwd = ""
        self.transcript_path = ""
        self.tool = {"id": "", "name": "", "kind": "other", "args": {}, "paths_complete": True}
        self.prompt = ""
        self.outcome = ""
        self.input_id = ""
        self.child = False
        self.stop_hook_active = False
        self.execution_num = None
        self.termination_reason = ""


HOSTS: Dict[str, Dict[str, Any]] = {
    "antigravity": {
        "transcript": True,
        "structured_non_shell_args": True,
        "stop_hook_active_reliable": False,
        "child_events": True,
        # 只能加载 Stop/无稳定子代理终态 selector：P3 完成事件不启用，不虚报。
        "completion_events": False,
    },
    "codex": {
        "transcript": True,
        "structured_non_shell_args": True,
        "stop_hook_active_reliable": True,
        "child_events": True,
        # 模板注册 SubagentStop，并有结构化 agent_id 与报告字段。
        "completion_events": True,
    },
    "dsh": {
        "transcript": False,
        "structured_non_shell_args": False,
        "stop_hook_active_reliable": False,
        "child_events": False,
        "completion_events": False,
    },
    "pi": {
        "transcript": True,
        "structured_non_shell_args": True,
        "stop_hook_active_reliable": True,
        "child_events": True,
        # pi 原生 tool_result 对 custom subagent 无统一 completed schema。
        "completion_events": False,
    },
}

EVENT_ALIASES = {
    "sessionstart": "session_start",
    "session_start": "session_start",
    "sessionstarted": "session_start",
    "preinvocation": "input",
    "userpromptsubmit": "input",
    "input": "input",
    "pretooluse": "pre_tool",
    "pre_tool": "pre_tool",
    "tool_call": "pre_tool",
    "posttooluse": "post_tool",
    "post_tool": "post_tool",
    "tool_result": "post_tool",
    "stop": "settle",
    "settle": "settle",
    "agent_before_settle": "settle",
    "subagentstart": "child_start",
    "child_start": "child_start",
    "subagentstop": "child_done",
    "child_done": "child_done",
    "postinvocation": "post_invocation",
}


def _event_name(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    key = value.strip().replace("-", "_").replace(" ", "").lower()
    return EVENT_ALIASES.get(key, "unknown")


def _first(mapping: Dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _int_or(value: Any, default: int) -> int:
    """整数值；其他类型（含 bool）回落 default（外部输入不当真）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _record_key_offset(record_key: str) -> int:
    """transcript 用户记录键（`<字节偏移>:<摘要>`）里的偏移；无法解析时为 -1。

    0 是合法偏移（记录在文件开头），所以“未知”用负数表示。
    """
    head = record_key.split(":", 1)[0] if record_key else ""
    return int(head) if re.match(r"^[0-9]+$", head) else -1


def _tool_from_input(name: Any, args: Any) -> Dict[str, Any]:
    tool_name = _string(name)
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            parsed = {"command": args}
        args = parsed
    if not isinstance(args, dict):
        args = {}
    lower = tool_name.lower()
    if lower in {"bash", "run_command", "exec_command", "powershell", "shell"}:
        kind = "shell"
    elif lower == "apply_patch":
        kind = "patch"
    elif lower in {item.lower() for item in WRITE_TOOL_NAMES}:
        kind = "write"
    elif lower in {item.lower() for item in READ_TOOL_NAMES}:
        kind = "read"
    elif lower in {item.lower() for item in DISPATCH_TOOL_NAMES}:
        kind = "dispatch"
    else:
        kind = "other"
    cwd = args.get("Cwd", args.get("cwd"))
    if not isinstance(cwd, str):
        cwd = args.get("WorkingDirectory") if isinstance(args.get("WorkingDirectory"), str) else ""
    return {
        "id": "",
        "name": tool_name,
        "kind": kind,
        "args": args,
        "cwd": cwd,
        "paths_complete": True,
    }


def _tool_cwd(record: "Record") -> str:
    """该次工具调用的可信 cwd：宿主工具参数优先于会话 cwd。"""
    value = record.tool.get("cwd") if isinstance(record.tool, dict) else ""
    if isinstance(value, str) and value:
        return value
    return record.cwd


def _workspace_paths(raw: Dict[str, Any]) -> List[str]:
    """Antigravity 公共字段 `workspacePaths`（数组）；单字符串写法保留兼容。"""
    values = raw.get("workspacePaths")
    if isinstance(values, list):
        return [item for item in values if isinstance(item, str) and item]
    return []


def _step_tool_id(turn_key: str) -> str:
    """无调用 ID 的 Antigravity 工具事件：用步骤序号合成稳定 ID。

    官方 `PreToolUse`/`PostToolUse` 的 `toolCall` 内没有 `id`，只有顶层整型
    `stepIdx`。同一步里出现多个工具调用时该 ID 会重名（配对按同 ID 覆盖），
    这一限制由 hooks/README.md 披露。
    """
    return "step:%s" % turn_key if turn_key else ""


def normalize_antigravity(
    raw: Dict[str, Any],
    cli_event: str = "",
    warnings: Optional[List[str]] = None,
) -> Record:
    """Antigravity envelope 归一。

    真实宿主 stdin **不带事件名字段**，事件类型只能由注册时的 `--event` 传入；
    stdin 里若确实带了事件名，以 stdin 为准。
    """
    record = Record("antigravity", raw, HOSTS["antigravity"])
    record.event = _event_name(_first(raw, "hookEventName", "eventName", "event", "type"))
    if record.event == "unknown" and cli_event:
        record.event = _event_name(cli_event)
    record.session_id = _string(_first(raw, "conversationId", "session_id", "sessionId"))
    record.actor_id = _string(_first(raw, "agentId", "actorId", "conversationId"))
    record.parent_id = _string(_first(raw, "parentConversationId", "parentId", "parent_id"))
    record.cwd = _string(_first(raw, "cwd", "workspacePath"))
    if not record.cwd:
        candidates = _workspace_paths(raw)
        usable = [path for path in candidates if os.path.isdir(path)]
        chosen = usable[0] if usable else (candidates[0] if candidates else "")
        if len(candidates) > 1 and warnings is not None:
            # 多工作区无法从 envelope 判断哪个属于本次写入：按第一个可用路径判定并披露。
            warnings.append(
                "Antigravity 提供 %d 个 workspacePaths，按第一个可用路径判定工作区"
                % len(candidates)
            )
        record.cwd = chosen
    record.transcript_path = _string(_first(raw, "transcriptPath", "transcript_path"))
    step_idx = _first(raw, "stepIdx", "step_idx")
    # 官方 `stepIdx` 是整数：`_string()` 会把它丢成空串，回合键与工具 ID 都没了。
    record.turn_key = (
        str(step_idx)
        if isinstance(step_idx, int) and not isinstance(step_idx, bool)
        else _string(step_idx)
    )
    record.execution_num = raw.get("executionNum")
    record.termination_reason = _string(raw.get("terminationReason"))
    record.stop_hook_active = False
    record.prompt = _string(_first(raw, "prompt", "userMessage", "message"))
    record.input_id = _string(_first(raw, "messageId", "inputId", "eventId"))
    record.child = bool(raw.get("isChild")) or bool(record.parent_id)
    tool_call = raw.get("toolCall") or raw.get("tool_call")
    if isinstance(tool_call, dict):
        tool = _tool_from_input(tool_call.get("name"), tool_call.get("args"))
        tool["id"] = (
            _string(_first(tool_call, "id", "toolCallId", "tool_call_id"))
            or _string(_first(raw, "toolCallId", "tool_use_id"))
            or _step_tool_id(record.turn_key)
        )
        record.tool = tool
    else:
        record.tool = _tool_from_input(
            _first(raw, "toolName", "tool_name"), _first(raw, "toolInput", "tool_input", "args")
        )
        record.tool["id"] = _string(_first(raw, "toolCallId", "tool_use_id")) or _step_tool_id(
            record.turn_key
        )
    if raw.get("status") in {"success", "completed"}:
        record.outcome = "success"
    elif raw.get("status") in {"error", "failed"}:
        record.outcome = "error"
    elif record.event == "post_tool":
        # 官方 PostToolUse 没有 `status`：失败带非空 `error`，成功不带或为空串。
        error = raw.get("error")
        record.outcome = "error" if isinstance(error, str) and error.strip() else "success"
    return record


def normalize_codex(raw: Dict[str, Any], host: str = "codex") -> Record:
    """Codex 子 Hook 的 `session_id` 是父 session，`agent_id` 才是 child actor。

    因此 child 事件不需要宿主额外提供 `parent_session_id`：父 session 直接取
    `session_id`，child 身份取 `agent_id`。缺 `agent_id` 的事件按根 actor 处理。
    """
    record = Record(host, raw, HOSTS[host])
    record.event = _event_name(_first(raw, "hook_event_name", "hookEventName", "event"))
    record.session_id = _string(_first(raw, "session_id", "sessionId"))
    record.actor_id = _string(_first(raw, "agent_id", "agentId"))
    explicit_parent = _string(_first(raw, "parent_session_id", "parentId"))
    record.parent_id = explicit_parent or (
        record.session_id if record.actor_id and record.actor_id != record.session_id else ""
    )
    record.cwd = _string(_first(raw, "cwd", "working_directory"))
    record.transcript_path = _string(_first(raw, "transcript_path", "transcriptPath"))
    record.turn_key = _string(_first(raw, "turn_id", "turnId"))
    record.tool = _tool_from_input(
        _first(raw, "tool_name", "toolName"),
        _first(raw, "tool_input", "toolInput"),
    )
    record.tool["id"] = _string(_first(raw, "tool_use_id", "toolUseId", "toolCallId"))
    record.prompt = _string(_first(raw, "prompt", "user_prompt", "message"))
    record.input_id = _string(_first(raw, "turn_id", "inputId"))
    # 官方契约：出现 agent_id 即子 Hook（session_id 仍是父 session）。
    record.child = bool(record.parent_id)
    record.stop_hook_active = raw.get("stop_hook_active") is True
    record.outcome = _string(raw.get("outcome"))
    return record


def normalize_dsh(raw: Dict[str, Any]) -> Record:
    return normalize_codex(raw, "dsh")


def normalize_pi(raw: Dict[str, Any], env: Dict[str, str]) -> Record:
    record = Record("pi", raw, HOSTS["pi"])
    record.event = _event_name(_first(raw, "event", "hookEventName", "type"))
    record.session_id = _string(_first(raw, "sessionId", "session_id"))
    record.actor_id = _string(_first(raw, "actorId", "actor_id"))
    record.parent_id = _string(
        _first(raw, "parentId", "parentSessionId", "parent_id")
    ) or env.get(PARENT_ENV, "")
    record.cwd = _string(_first(raw, "cwd"))
    record.transcript_path = _string(_first(raw, "transcriptPath", "transcript_path"))
    record.turn_key = _string(_first(raw, "turnKey", "turnId"))
    record.tool = _tool_from_input(
        _first(raw, "toolName", "tool_name"),
        _first(raw, "toolInput", "tool_input", "input"),
    )
    record.tool["id"] = _string(_first(raw, "toolCallId", "tool_call_id"))
    record.prompt = _string(_first(raw, "prompt", "text"))
    record.input_id = _string(_first(raw, "inputId", "input_id"))
    record.child = bool(raw.get("child")) or env.get(CHILD_ENV) == "1"
    record.stop_hook_active = raw.get("stopHookActive") is True
    record.outcome = _string(raw.get("outcome"))
    record.execution_num = raw.get("executionNum")
    record.termination_reason = _string(raw.get("terminationReason"))
    return record


def normalize(
    host: str,
    raw: Dict[str, Any],
    env: Dict[str, str],
    cli_event: str = "",
    warnings: Optional[List[str]] = None,
) -> Record:
    if host == "antigravity":
        return normalize_antigravity(raw, cli_event=cli_event, warnings=warnings)
    if host == "dsh":
        return normalize_dsh(raw)
    if host == "pi":
        return normalize_pi(raw, env)
    return normalize_codex(raw, "codex" if host == "codex" else host)


def _pending_key(actor_id: str, epoch: int, tool_id: str) -> str:
    """写入配对键：actor + task epoch + 调用 ID（跨 actor 可能重名）。"""
    return "%s|%d|%s" % (actor_id, epoch, tool_id)


class Decision(object):
    def __init__(self, decision: str = "allow", rule: str = "", code: str = "", reason: str = "") -> None:
        self.decision = decision
        self.rule = rule
        self.code = code
        self.reason = reason

    def as_dict(self) -> Dict[str, str]:
        if self.decision == "allow":
            return {}
        return {"decision": self.decision, "rule": self.rule, "reason": self.reason}


def encode(host: str, event: str, decision: Decision) -> Dict[str, Any]:
    if decision.decision == "allow":
        if host == "pi" and event == "input":
            return {"action": "continue"}
        # 宿主对 PreToolUse 的放行是必填字段，不是空对象：Antigravity 要 decision，
        # DSH 桥接要 permissionDecision；Codex/pi 的放行保持空对象。
        if host == "antigravity" and event == "pre_tool":
            return {"decision": "allow"}
        if host == "dsh" and event == "pre_tool":
            return {"permissionDecision": "allow"}
        return {}
    reason = decision.reason
    if decision.decision == "deny":
        if host == "antigravity":
            return {"decision": "deny", "reason": reason}
        if host == "pi":
            return {"block": True, "reason": reason}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if decision.decision == "force_ask":
        if host == "antigravity":
            return {"decision": "force_ask", "reason": reason}
        if host == "pi":
            return {"block": True, "reason": reason}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if decision.decision == "continue":
        if host == "antigravity":
            return {"decision": "continue", "reason": reason}
        if host == "pi":
            return {
                "continue": True,
                "entries": [
                    {
                        "type": "custom_message",
                        "customType": "easydev-review-reminder",
                        "content": reason,
                        "display": True,
                    }
                ],
            }
        return {"decision": "block", "reason": reason}
    return {}


def _fail_open_event(
    host: str,
    raw: Optional[Dict[str, Any]],
    cli_event: str,
    record_event: str = "",
) -> str:
    """故障开路时的事件名：只用字段别名，不调用可能已经抛错的 `normalize`。

    优先采用已归一化记录的事件，否则按各宿主 normalizer 的同一字段顺序取名。
    AG/DSH 的 PreToolUse 放行字段是必填的：事件完全无法恢复时按 `pre_tool` 兜底，
    pi/Codex 保持 `unknown`（编码为 `{}`）。
    """
    if record_event and record_event != "unknown":
        return record_event
    names = {
        "antigravity": ("hookEventName", "eventName", "event", "type"),
        "pi": ("event", "hookEventName", "type"),
    }.get(host, ("hook_event_name", "hookEventName", "event"))
    event = _event_name(_first(raw, *names)) if isinstance(raw, dict) else "unknown"
    if event == "unknown" and cli_event:
        event = _event_name(cli_event)
    if event != "unknown":
        return event
    return "pre_tool" if host in {"antigravity", "dsh"} else "unknown"


def _strip_fences(text: str) -> str:
    return FENCED_BLOCK_RE.sub("\n", text)


def explicit_invocation(prompt: str) -> bool:
    if not prompt:
        return False
    quoted = []
    for line in prompt.splitlines():
        if line.lstrip().startswith(">"):
            continue
        quoted.append(line)
    body = _strip_fences("\n".join(quoted))
    return bool(EXPLICIT_INVOCATION_RE.search(body))


def machine_context(prompt: str) -> Optional[Dict[str, Any]]:
    if not prompt:
        return None
    for match in FENCED_BLOCK_RE.finditer(prompt):
        language = (match.group(1) or "").strip().lower()
        if language not in {"easydev-hook", "json: easydev-hook", "easydev-hook-json"}:
            continue
        try:
            value = json.loads(match.group(2))
        except json.JSONDecodeError:
            return None
        if isinstance(value, dict) and value.get("version") == 1:
            return value
    return None


def _evidence_lines(text: str) -> str:
    """丢弃引用行、取反与“解释模板”语境，只留真实初始声明。"""
    kept = []
    for line in _strip_fences(text or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):
            continue
        lowered = stripped.lower()
        if any(marker in stripped for marker in NEGATION_MARKERS):
            continue
        if any(marker in lowered for marker in TEMPLATE_EXPLANATION_MARKERS):
            # “发现模板如下：**[P级] 简短标题**”这类同行写法本身就是格式证据：
            # 只有该行不含任何冻结模板时才当纯解释丢掉，且只保留这一行。
            if not finding_format_evidence(stripped):
                continue
        kept.append(line)
    return "\n".join(kept)


def declared_role(prompt: str) -> str:
    match = ROLE_LINE_RE.search(_evidence_lines(prompt or ""))
    return match.group(1).lower() if match else ""


def role_path_evidence(text: str, role: str, role_path: str) -> bool:
    """按安装上下文核对角色路径；只含子串的另一安装树不算证据。"""
    wanted = "references/%s-role.md" % role
    normalized = (text or "").replace("\\", "/")
    target = os.path.realpath(role_path)
    for token in re.split(r"[\s`'\"()<>\[\]{},，。；、：:]+", normalized):
        if not token or not token.endswith(wanted):
            continue
        candidate = token if os.path.isabs(token) else os.path.join(os.path.dirname(os.path.dirname(role_path)), token.lstrip("./"))
        if os.path.realpath(candidate) == target:
            return True
    return False


def finding_format_evidence(text: str) -> bool:
    """只接受冻结形式：通用模板字面量或完整 P0–P3 模板。"""
    for form in REVIEWER_HEADER_FORMS:
        if form in text:
            return True
    return all(("**[%s]" % level) in text for level in REVIEWER_SEVERITIES)


def standard_handoff(prompt: str, skill_root: str) -> Dict[str, Any]:
    """标准角色交接：角色声明 + 精确角色路径 + 机器格式模板。"""
    text = _evidence_lines(prompt or "")
    role = declared_role(prompt or "")
    if not role:
        for candidate in ("reviewer", "developer", "designer"):
            if candidate == "reviewer" and REVIEWER_DECLARATION_RE.search(text):
                role = "reviewer"
                break
    if role not in {"reviewer", "developer", "designer"}:
        return {"complete": False, "role": "", "missing": ["role"]}
    role_path = os.path.join(os.path.abspath(skill_root), "references", "%s-role.md" % role)
    has_path = role_path_evidence(text, role, role_path)
    has_format = finding_format_evidence(text)
    missing = []
    if not has_path:
        missing.append("role_path")
    if role == "reviewer" and not has_format:
        missing.append("finding_format")
    return {
        "complete": not missing,
        "role": role,
        "missing": missing,
        "role_path": role_path,
        "code_review": bool(CODE_REVIEW_RE.search(text)),
    }


def dispatch_info(tool: Dict[str, Any]) -> Dict[str, Any]:
    name = (tool.get("name") or "").lower()
    if name not in {item.lower() for item in DISPATCH_TOOL_NAMES}:
        return {"is_dispatch": False}
    args = tool.get("args") or {}
    if not isinstance(args, dict):
        return {"is_dispatch": True, "structured": False, "operation": "unknown", "role": "", "prompt": ""}
    operation = _string(_first(args, "action", "operation", "mode", "command"))
    if operation.lower() in shell.STATUS_SUBCOMMANDS:
        return {"is_dispatch": False}
    if name == "subagent" and operation.lower() not in {"", "create", "dispatch", "run", "start", "batch", "chain"}:
        return {"is_dispatch": False}
    role = _string(_first(args, "agent_type", "agentType", "agent", "role", "subagent_type")).lower()
    prompt = _string(_first(args, "prompt", "task", "input", "message", "instructions"))
    items: List[Dict[str, str]] = []
    tasks = args.get("tasks")
    if isinstance(tasks, list):
        for item in tasks:
            if not isinstance(item, dict):
                items.append({"role": "", "prompt": ""})
                continue
            item_role = _string(_first(item, "agent_type", "agentType", "agent", "role")).lower()
            item_prompt = _string(_first(item, "prompt", "task", "input"))
            items.append({"role": item_role, "prompt": item_prompt})
            prompt = prompt or item_prompt
    return {
        "is_dispatch": True,
        "structured": True,
        "operation": operation,
        "role": role if role in ROLE_VALUES else "",
        "prompt": prompt,
        "items": items,
    }


def write_targets_from_tool(
    host: str, tool: Dict[str, Any], cap: Dict[str, Any], cwd_hint: str = ""
) -> Tuple[List[Dict[str, str]], bool, str]:
    """返回 (目标列表, 路径是否完整, 覆盖缺口说明)。

    每项是 `{"path": 目标, "cwd": 该命令段有效 cwd 或 ""}`：shell 内的 `cd`
    与 `env -C` 会让相对目标落在与工具 cwd 不同的目录，必须逐项携带。
    """
    args = tool.get("args") or {}
    name = (tool.get("name") or "").lower()
    if tool.get("kind") == "patch" or name == "apply_patch":
        text = _string(_first(args, "command", "patch", "input"))
        paths_found, complete = paths.patch_targets(text)
        return (
            [{"path": item, "cwd": ""} for item in paths_found],
            complete,
            "" if complete else "补丁目标解析不完整",
        )
    if tool.get("kind") == "write":
        targets: List[Dict[str, str]] = []
        for key in ("path", "file_path", "filePath", "Path", "TargetFile", "target_file", "AbsolutePath"):
            value = args.get(key)
            if isinstance(value, str) and value:
                targets.append({"path": value, "cwd": ""})
        for key in ("paths", "files", "targets"):
            values = args.get(key)
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, str) and item:
                        targets.append({"path": item, "cwd": ""})
                    elif isinstance(item, dict):
                        for nested in ("path", "file_path", "Path"):
                            if isinstance(item.get(nested), str):
                                targets.append({"path": item[nested], "cwd": ""})
        if not targets:
            if cap.get("structured_non_shell_args") is False:
                return [], False, "宿主未提供结构化非 shell 参数，无法核对写入目标"
            return [], False, "写工具缺少可识别路径字段"
        return targets, True, ""
    if tool.get("kind") == "shell":
        command = _string(_first(args, "command", "CommandLine", "cmd"))
        if not command:
            return [], False, "缺少命令文本"
        analysis = shell.analyze(
            command, cwd=tool.get("cwd") or _string(args.get("cwd")) or cwd_hint
        )
        if analysis.opaque or analysis.too_deep:
            return [], False, "命令结构无法完整解析"
        targets = []
        complete = True
        for write in analysis.writes:
            target = write.get("target")
            if isinstance(target, str) and target:
                cwd = write.get("cwd")
                targets.append({"path": target, "cwd": cwd if isinstance(cwd, str) else ""})
            if not write.get("complete", False):
                complete = False
        if analysis.write_unknown:
            # 包装内 cd/相对 cd 无法静态确定：写入目标位置不可证明，披露缺口。
            return targets, False, "命令内目录变化无法确定，基线检查未覆盖"
        return targets, complete, "" if complete else "命令写入目标不完整"
    return [], True, ""


ROOT_MARKERS = {"root", "main"}


class Engine(object):
    def __init__(
        self,
        host: str,
        context: Dict[str, Any],
        *,
        mode: str = "enforce",
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.host = host
        self.context = context
        self.mode = mode
        self.env = env if env is not None else os.environ
        self.capabilities = HOSTS.get(host, HOSTS["codex"])
        self.warnings: List[str] = []
        self.started = time.monotonic()
        # 一次调用只有一个共享内部截止：Git/指纹/transcript/遍历都从该预算
        # 里扣，离线夹具可用 EASYDEV_HOOK_BUDGET_MS 缩小以确定触发边界。
        self.deadline = self.started + self._budget_seconds()
        self.state_root = state.state_root_for(
            context.get("repo_root") if isinstance(context.get("repo_root"), str) else None,
            context.get("state_root") if isinstance(context.get("state_root"), str) else None,
        )
        self.skill_root = _string(context.get("skill_root")) or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        self.context_task_root = _string(context.get("task_root"))
        self.doc_paths = set(_string_list(context.get("doc_paths")))

    def _budget_seconds(self) -> float:
        raw = self.env.get(BUDGET_ENV, "")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return INTERNAL_BUDGET_MS / 1000.0
        if value <= 0:
            return INTERNAL_BUDGET_MS / 1000.0
        clipped = max(MIN_BUDGET_MS, min(value, INTERNAL_BUDGET_MS))
        return clipped / 1000.0

    # ---- 状态 -------------------------------------------------------
    def _actor_key(self, record: Record) -> str:
        return state.identity_key("actor", self.host, record.session_id, self.actor_key_part(record))

    def actor_key_part(self, record: Record) -> str:
        return record.actor_id or record.session_id or "unknown"

    def _root_key(self, root_session: str, root_actor: str) -> str:
        # 完整 session 身份（host + root session + root actor）才唯一：仅用
        # root_actor_id 会让同 actor ID 的不同 session 共用一个 epoch。
        return state.identity_key("root", self.host, root_session, root_actor)

    def _task_key(self, root_actor: str, identity: str) -> str:
        return state.identity_key("task", self.host, root_actor, identity)

    def _dir(self, kind: str) -> str:
        return os.path.join(self.state_root, kind)

    def _load(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        value = state.read_state(self._dir(kind), key + ".json")
        if (
            kind == "roots"
            and isinstance(value, dict)
            and value
            and "baseline_roots_seen" not in value
        ):
            # 读到已有 root 却缺归属索引：旧版本写入或结构丢失。首次运行的
            # None/{} 与显式空索引都健康，不能把它们也报成降级。
            self.warnings.append("已有任务状态缺少任务根归属索引：无法识别历史任务根的旧归属，按降级处理")
        return value

    def _save(self, kind: str, key: str, value: Dict[str, Any]) -> None:
        state.write_state(self._dir(kind), key + ".json", value)

    def _locks(self, actor_key: str, root_key: Optional[str], task_key: Optional[str]) -> state.Locks:
        locks = state.Locks(self._dir("locks"))
        names = ["actor-" + actor_key]
        if root_key:
            names.append("root-" + root_key)
        if task_key:
            names.append("task-" + task_key)
        locks.acquire(names)
        return locks

    # ---- 未激活短路 -------------------------------------------------
    def _cheap_state_root(self, record: Record) -> Optional[str]:
        """未激活短路用的状态分区：与完整判定同一仓库根，但不启动 Git 子进程。

        权威 actor 状态按仓库分目录；短路只能读本次事件所属的那个分区。显式
        `state_root` 时与完整判定同根；否则用 tool cwd（含 record.cwd 回退）的廉价
        仓库根。没有可信 cwd 时无法定位分区，返回 None 走完整判定。
        """
        override = _string(self.context.get("state_root"))
        if override:
            return state.state_root_for(None, override)
        cwd = _tool_cwd(record)
        if not cwd:
            return None
        return state.state_root_for(paths.cheap_repo_root(cwd), None)

    def _inactive_short_circuit(self, record: Record) -> Optional[Decision]:
        """未激活会话的 O(1) 放行：不调 Git、不预取写入、不建锁、不写状态。

        只读该分区里的 actor 状态（一个 JSON）。没有状态（首次事件）返回 None 走完整
        判定，由 `_persist` 写下“已判定为未激活”的锚点；这样“从未激活”的会话只在
        第一次调用付一次完整代价，之后都是常数开销。同一会话在另一仓库的 inactive
        写的是那个仓库的分区，不会掩盖本仓库的激活状态。
        """
        if record.event in {"unknown", "session_start", "input", "child_start"}:
            return None
        if self.mode == "probe":
            # probe 要观察真实事件与字段，不能短路掉自己的记录。
            return None
        if record.event not in {"pre_tool", "post_tool", "settle", "child_done"}:
            return None
        if not record.session_id:
            return None
        state_root = self._cheap_state_root(record)
        if not state_root:
            return None
        try:
            actor = state.read_state(
                os.path.join(state_root, "actors"), self._actor_key(record) + ".json"
            )
        except state.StateUnavailable:
            # 状态不可读时不短路：交回完整判定，按既有 fail-open 口径告警放行，
            # 而不是把另一个分区或旧缓存当成“已验证未激活”。
            return None
        if not isinstance(actor, dict):
            return None
        if _string(actor.get("activation_scope")) != "inactive":
            return None
        if record.event == "post_tool" and self._direct_skill_read(record):
            # 读取精确 SKILL.md 的 post 是激活证据，必须走完整流程。
            return None
        return Decision()

    # ---- 激活与身份 ------------------------------------------------
    def _decide_identity(self, actor: Dict[str, Any], record: Record) -> None:
        root_actor = actor.get("root_actor_id") or record.root_actor_id
        if not root_actor:
            if record.child and record.parent_id:
                root_actor = record.parent_id
            else:
                root_actor = self.actor_key_part(record)
        record.root_actor_id = str(root_actor)
        record.task_identity = str(actor.get("task_identity") or record.task_identity or "")
        record.activation_scope = str(actor.get("activation_scope") or "inactive")

    @staticmethod
    def _skill_read_after_deactivation(actor: Dict[str, Any], skill_read_offset: int) -> bool:
        """停用前的历史 SKILL 读取不是新激活证据。

        没有停用边界时保持原有语义（`skill_read` 即可激活）；有边界时必须能证明
        这次成功读取发生在边界之后：边界或读取偏移未知一律不恢复。
        """
        marker = actor.get("deactivated")
        if not isinstance(marker, dict):
            return True
        boundary = _int_or(marker.get("offset"), -1)
        return boundary >= 0 and skill_read_offset > boundary

    def _direct_skill_read(self, record: Record) -> bool:
        """本事件是否是对精确安装路径 `SKILL.md` 的一次成功读取。

        失败/只有 requested 的读取，以及其他位置的同名文件都不是激活证据。
        """
        if record.event != "post_tool":
            return False
        tool = record.tool
        if (tool.get("name") or "").lower() not in READ_TOOL_NAMES:
            return False
        if record.outcome not in {"success", "completed"}:
            return False
        skill_file = os.path.realpath(os.path.join(self.skill_root, "SKILL.md"))
        for key in ("path", "file_path", "Path", "filePath"):
            value = (tool.get("args") or {}).get(key)
            if isinstance(value, str) and os.path.realpath(
                os.path.join(tool.get("cwd") or record.cwd or "", value)
            ) == skill_file:
                return True
        return False

    def _skill_read_digest(self, record: Record) -> str:
        """成功读取事件的稳定内容摘要：同一 (actor, tool ID) 的参数变化要能被发现。

        只摘要调用本身（工具名、参数、事件、结果），不摘要宿主/会话回声的字段：
        同一事件在不同阶段被解析时 session cwd 可能尚未回填，把它算进摘要会把
        重投误判成新调用。
        """
        tool = record.tool or {}
        payload = {
            "name": _string(tool.get("name")).lower(),
            "args": tool.get("args") or {},
            "event": record.event,
            "outcome": record.outcome,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def _skill_read_consumed(self, actor: Dict[str, Any], record: Record) -> bool:
        """该 actor 是否已消费过这个工具调用 ID 的成功读取。

        幂等键是 `(actor, tool ID)`：只要该 ID 在这个 actor 上出现过，就不再是新的
        激活证据。同 ID 内容摘要变化意味着同一调用的观测不可信（使该段观测无效，
        不是新读取），因此不能据此恢复。判据是跨 epoch 的 `consumed_read_ids`，
        所以近期摘要索引淡出后结论不变。
        """
        tool_id = _string((record.tool or {}).get("id"))
        if not tool_id:
            return False
        seen = actor.get("consumed_read_ids")
        known = isinstance(seen, dict) and tool_id in seen
        consumed = actor.get("consumed_reads")
        entry = consumed.get(tool_id) if isinstance(consumed, dict) else None
        if not isinstance(entry, dict):
            entry = None
        if entry is not None and entry.get("digest") != self._skill_read_digest(record):
            self.warnings.append("同一工具调用 ID 的成功读取内容已变化，该调用观测作废")
        return known or entry is not None

    def _record_skill_read(self, actor: Dict[str, Any], record: Record) -> None:
        """记录本 actor 已消费的成功读取调用；重复记录幂等。

        `consumed_read_ids` 是“停用前见过的调用 ID 不可再激活”的持久证据：只存调用
        ID 与首次出现的 epoch，不含参数或内容；**不做容量淘汰**，因为抢救性地遗忘
        一个旧 ID 就等于允许它在停用后重新激活。`consumed_reads` 只是近期摘要索引
        （用于识别同 ID 内容变化），可以裁剪。
        """
        tool_id = _string((record.tool or {}).get("id"))
        if not tool_id:
            # 没有调用 ID 无法按 (actor, tool ID) 幂等：只能提示无法去重。
            self.warnings.append("SKILL 读取缺少工具 ID，重复投递无法按调用 ID 去重")
            return
        seen = actor.get("consumed_read_ids")
        if not isinstance(seen, dict):
            seen = {}
            actor["consumed_read_ids"] = seen
        if tool_id not in seen:
            seen[tool_id] = int(actor.get("epoch") or 0)
        digest = self._skill_read_digest(record)
        consumed = actor.get("consumed_reads")
        if not isinstance(consumed, dict):
            consumed = {}
            actor["consumed_reads"] = consumed
        entry = consumed.get(tool_id)
        if isinstance(entry, dict):
            if entry.get("digest") != digest:
                # 同 ID 内容变化：保留首次摘要并标记该调用观测失效，不覆盖、不新增。
                entry["invalid"] = True
                self.warnings.append("同一工具调用 ID 的成功读取内容已变化，该调用观测作废")
            return
        seq = int(actor.get("consumed_read_seq") or 0) + 1
        actor["consumed_read_seq"] = seq
        consumed[tool_id] = {
            "digest": digest,
            "epoch": int(actor.get("epoch") or 0),
            "seq": seq,
        }
        if len(consumed) > 100:
            oldest = sorted(consumed, key=lambda key: int(consumed[key].get("seq") or 0))
            for key in oldest[: len(oldest) - 100]:
                consumed.pop(key, None)

    def _activate_input(
        self,
        actor: Dict[str, Any],
        root: Dict[str, Any],
        task: Dict[str, Any],
        record: Record,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> None:
        """按当前用户回合的可信证据激活。

        `evidence` 是该 input 锁外归一的 transcript 取证（最新原始 user 文本、
        记录键与历史 SKILL 读取）；锁内不再读/解析 transcript 文件。
        """
        prompt = record.prompt
        user_text = prompt
        transcript_key = ""
        skill_read_offset = -1
        data = evidence or {}
        record_key = _string(data.get("record_key"))
        # 已激活会话里，同一 transcript 记录是重投而不是新回合：不重激活、不开新
        # epoch、不重开提醒预算（executionNum 不作为回合依据）。未激活时必须继续
        # 往下走 `skill_read_offset`：同一回合里先读 SKILL.md 再来的调用仍要能激活。
        already_active = actor.get("activation_scope") in {"task_active", "role_only"}
        if not prompt:
            transcript_key = record_key
            user_text = _string(data.get("text"))
            skill_read_offset = _int_or(data.get("skill_read_offset"), -1)
            if already_active and transcript_key and transcript_key == _string(
                actor.get("last_transcript_input")
            ):
                return
            if transcript_key:
                actor["last_transcript_input"] = transcript_key
        elif record_key and _string(data.get("text")) == prompt:
            # 自带 prompt 的宿主可能重复投递同一回合：transcript 已记录同一条
            # 原始 user 文本时用记录键去重，避免无 input ID 的宿主重复开 epoch。
            transcript_key = record_key
            if already_active and transcript_key == _string(actor.get("last_transcript_input")):
                return
            actor["last_transcript_input"] = transcript_key
        # 显式调用、机器上下文、角色与去重都从同一份已验证的最新原始 user 文本解析：
        # transcript 来源与直接 prompt 来源不再走两套语义。
        machine = machine_context(user_text) if user_text else None
        explicit = explicit_invocation(user_text)
        handoff = (
            standard_handoff(user_text, self.skill_root)
            if user_text
            else {"complete": False, "role": ""}
        )
        child = record.child or self.env.get(CHILD_ENV) == "1"
        parent_session = record.parent_id or self.env.get(PARENT_ENV, "")
        role = ""
        # 本回合是否有明确的激活证据（显式调用 / 机器上下文 / 标准角色交接）。
        current_turn = False
        if machine is not None:
            if machine.get("active") is False:
                actor.update({"activation_scope": "inactive", "role": "unknown", "role_source": "machine"})
                # 显式停用是不可被历史证据撤销的边界：记下停用回合在 transcript 里的
                # 字节偏移，停用前的 SKILL 读取（偏移更小）不得再把会话重新激活。
                actor["deactivated"] = {
                    "offset": _record_key_offset(transcript_key),
                    "record_key": transcript_key,
                    "epoch": int(root.get("epoch") or 0),
                }
                # 真实 user 输入的 active=false 使当前 root epoch 的后续 child 事件退役。
                if not child:
                    root["active"] = False
                return
            if machine.get("active") is True:
                explicit = True
                current_turn = True
                role = _string(machine.get("role"))
                if _string(machine.get("task_root")):
                    self.context_task_root = _string(machine.get("task_root"))
                for key in ("models_file", "role_overrides", "code_review", "doc_paths"):
                    if key in machine:
                        self.context[key] = machine[key]
                if isinstance(machine.get("doc_paths"), list):
                    self.doc_paths = set(_string_list(machine["doc_paths"]))
        if explicit:
            current_turn = True
        if handoff.get("complete"):
            current_turn = True
            role = role or handoff["role"]
        child = child or bool(machine and machine.get("child"))
        if not current_turn:
            if actor.get("activation_scope") in {"task_active", "role_only"}:
                # 当前回合没有明确的重新调用或机器上下文：历史 SKILL 读取只能
                # 保持激活，不能把空 prompt 重算成 main/可写。普通跟进只推进
                # 提醒 generation，保留当前任务的角色与只读约束。
                if not (record.child or actor.get("child")):
                    self._bump_generation(task, record, marker=transcript_key)
                return
            if skill_read_offset < 0:
                return
            if not self._skill_read_after_deactivation(actor, skill_read_offset):
                # 显式停用后，停用前的历史读取不是新激活证据：保持 inactive。
                return
            # 首次仅由历史 SKILL 读取激活：没有当前用户文本，继续走下面的激活建立。
        if child and not explicit and not (machine and machine.get("active") is True):
            # 只用可信分派证据（child ID 或唯一初始任务摘要）绑定，并固定分派 epoch；
            # 父“当前任务”不是关联证据：A 分派后切到 B，旧 child 不得归入 B。
            match = self._match_dispatch(
                parent_session,
                record.tool.get("id") or "",
                user_text,
                bound=self._child_binding_snapshot(actor),
                tombstone=self._child_tombstone(actor),
            )
            if match is not None:
                self._apply_child_binding(actor, record, match)
                if handoff.get("role") and not match.get("role"):
                    actor["role"] = handoff["role"]
                return
            self.warnings.append("子代理未匹配到有效分派记录（或旧 epoch 已退役），只保留局部角色约束")
            actor.update(
                {
                    "activation_scope": "role_only",
                    "role": role or handoff.get("role") or "unknown",
                    "role_source": "unlinked_child_handoff",
                    "parent_id": parent_session,
                    "child": True,
                }
            )
            return
        if handoff.get("complete") and handoff.get("role") in {"reviewer", "developer", "designer"} and not explicit:
            # 完整标准角色交接但无法关联父任务：只授予本 actor 的局部角色约束。
            actor.update(
                {
                    "activation_scope": "role_only",
                    "role": handoff["role"],
                    "role_source": "unlinked_standard_handoff",
                    "child": child,
                }
            )
            return
        if child:
            actor.update(
                {
                    "activation_scope": "role_only",
                    "role": role or handoff.get("role") or "unknown",
                    "role_source": "child_initial_task",
                    "child": True,
                }
            )
            return
        # 根用户输入：可信激活。
        self._activate_root_task(
            actor,
            root,
            task,
            record,
            role=role or declared_role(user_text) or "main",
            role_source="explicit" if explicit else "machine",
            readonly=bool(
                user_text and REVIEW_AUDIT_RE.search(user_text) and READONLY_MARKERS.search(user_text)
            ),
            machine=machine,
            transcript_key=transcript_key,
            explicit=explicit,
        )

    def _activate_root_task(
        self,
        actor: Dict[str, Any],
        root: Dict[str, Any],
        task: Dict[str, Any],
        record: Record,
        role: str,
        role_source: str,
        readonly: bool,
        machine: Optional[Dict[str, Any]],
        transcript_key: str,
        explicit: bool,
    ) -> None:
        """落地一次可信的根任务激活：同时更新 root 与 actor，必要时开新 epoch。

        input/transcript 恢复与停用后直接成功读取 SKILL 的恢复共用这一段语义：新
        epoch 必伴随 `_new_task` 清空本任务缓存（origins/last_success/reviews/
        checkpoint），`baseline_roots_seen` 跨 epoch 保留在 root 上。
        """
        previous_root = actor.get("root_actor_id")
        root_actor = self.actor_key_part(record)
        actor.update(
            {
                "activation_scope": "task_active",
                "role": role,
                "role_source": role_source,
                "root_actor_id": root_actor,
                "root_session": record.session_id,
                "child": False,
                "readonly": readonly,
                "cwd": record.cwd or _string(self.context.get("repo_root")),
                "user_generation": int(root.get("generation") or 0) + 1,
            }
        )
        new_epoch = False
        task_root = self.context_task_root or _string(machine.get("task_root") if machine else "")
        if not root:
            new_epoch = True
        elif previous_root and previous_root != root_actor:
            new_epoch = True
        elif task_root and task_root != _string(root.get("task_root")):
            new_epoch = True
        elif isinstance(actor.get("deactivated"), dict):
            # 从显式停用恢复：即使同一 input 重投也开新 epoch，旧 child 与旧分派因
            # epoch 不匹配而退役，不得归并到恢复后的任务。
            new_epoch = True
        elif explicit and root.get("epoch") and (
            not record.input_id or record.input_id != root.get("last_input_id")
        ):
            # 真实用户重新明确调用 EasyDev：开始新 epoch；重复投递同一 input ID 不重置。
            new_epoch = True
        if new_epoch:
            root["epoch"] = int(root.get("epoch") or 0) + 1
            # 新任务不继承旧任务的对话覆盖；新 epoch 重新生效，旧 epoch 的
            # 分派/子绑定因 epoch 不匹配而自然退役。
            root["machine"] = {}
        root["active"] = True
        root["root_actor_id"] = root_actor
        root["root_session"] = record.session_id
        root["task_root"] = task_root
        if machine is not None:
            stored: Dict[str, Any] = {}
            if _string(machine.get("task_root")):
                stored["task_root"] = self.context_task_root
            for key in ("models_file", "role_overrides", "code_review", "doc_paths"):
                if key in machine:
                    stored[key] = machine[key]
            root["machine"] = stored
        root.setdefault("baseline_roots_seen", {})
        root["last_input_id"] = record.input_id
        if transcript_key:
            actor["last_transcript_input"] = transcript_key
        root["generation"] = int(actor.get("user_generation") or 0)
        record.root_actor_id = root_actor
        identity = self._task_identity(
            root_actor, str(record.session_id), int(root["epoch"]), task_root, record
        )
        record.task_identity = identity
        actor["task_identity"] = identity
        actor["epoch"] = root["epoch"]
        # 恢复后的新激活已经落地：停用边界不再拦后续证据。
        actor.pop("deactivated", None)
        if new_epoch or not task.get("task_identity"):
            self._new_task(task, identity, root_actor, int(root["epoch"]))
            task["user_generation"] = int(root["generation"])
        else:
            self._bump_generation(task, record)

    def _restore_from_skill_read(
        self, record: Record, actor_key: str, snapshot: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """停用后直接成功读取 SKILL.md：按可信激活语义恢复（新 epoch/新任务）。

        与 input/transcript 恢复共用 `_activate_root_task`：同时更新 root 与 actor、
        创建新 epoch/task 并清空本任务缓存，`baseline_roots_seen` 跨 epoch 保留。
        该段锁只持 actor 与 root，不持 task 锁；锁内复核仍为停用状态后才落地。
        返回恢复后的 actor（供本次事件继续使用）；不满足恢复条件时返回 None。
        """
        if record.child or snapshot.get("child"):
            return None
        if snapshot.get("activation_scope") == "task_active":
            return None
        root_key, _task_key = self._keys_for(record, snapshot)
        if not root_key:
            self.warnings.append("缺少 root 绑定，SKILL 读取恢复降级")
            return None
        try:
            locks = self._locks(actor_key, root_key, None)
        except state.StateUnavailable:
            self.warnings.append("状态锁不可用，SKILL 读取恢复降级")
            return None
        with locks:
            try:
                actor = self._load("actors", actor_key) or {}
                root = self._load("roots", root_key) or {}
            except state.StateUnavailable:
                self.warnings.append("状态读取失败，SKILL 读取恢复降级")
                return None
            if actor.get("child") or actor.get("activation_scope") == "task_active":
                return actor
            if self._skill_read_consumed(actor, record):
                # 停用前已消费的同一成功读取（同 actor、同 tool ID、同参数、同结果）
                # 重投不是新激活证据：保持停用边界，不开新 epoch、不清任务缓存。
                self.warnings.append("同一 SKILL 读取调用已消费，重复投递不恢复激活")
                return None
            role = _string(actor.get("role"))
            task: Dict[str, Any] = {}
            self._activate_root_task(
                actor,
                root,
                task,
                record,
                # 直接读取没有当前用户文本：不重算角色，但也不把已知角色洗成 main。
                role=role if role in ROLE_VALUES else "main",
                role_source="skill_read",
                readonly=bool(actor.get("readonly")),
                machine=None,
                transcript_key="",
                explicit=False,
            )
            restored_root_key, restored_task_key = self._keys_for(record, actor)
            if restored_root_key != root_key or not restored_task_key:
                self.warnings.append("SKILL 读取恢复的 root 绑定发生变化，丢弃本次恢复")
                return None
            # 消费证据与恢复同锁同次落盘：并发重投不能各自开一个新 epoch。
            self._record_skill_read(actor, record)
            self._persist(record, actor_key, actor, root_key, root, restored_task_key, task)
            return actor

    def _transcript_file_usable(self, path: str) -> bool:
        """transcript 必须是可读普通文件：FIFO/设备不阻塞、也不冒充证据。"""
        try:
            info = os.lstat(path)
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode):
            self.warnings.append("transcript 不是普通文件，激活证据抽取降级")
            return False
        return True

    def _transcript_disclosure(self, record: Record) -> bool:
        """宿主无 transcript 能力时披露降级；返回能力是否可用。"""
        if self.capabilities.get("transcript"):
            return True
        if record.transcript_path:
            self.warnings.append("该宿主无 transcript 能力，激活证据只按原始输入判定")
        return False

    def _transcript_identity_problem(self, record: Record, path: str) -> Tuple[str, bool]:
        """返回 (原生身份明确不匹配的原因, 是否拿到原生身份)。

        `session_meta` 的 session/thread/actor 是“这条 transcript 属于谁”的唯一有
        来源证据：明确不匹配时两条激活路径（用户指令与 SKILL 读取）都不得退回
        “缺身份”降级。
        """
        identity = transcripts.transcript_identity(self.host, path, deadline=self.deadline)
        session = _string(identity.get("session"))
        thread = _string(identity.get("thread"))
        actor = _string(identity.get("actor"))
        expected_actor = record.actor_id or record.session_id
        if session and session != record.session_id:
            return "会话", True
        if thread and thread != record.session_id:
            return "线程", True
        if actor and expected_actor and actor != expected_actor:
            return "actor", True
        return "", bool(session or thread or actor)

    def _input_evidence(
        self,
        record: Record,
        root_key: Optional[str],
        task_key: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """锁外为一次 input 归一 transcript 取证（结果不可变）。

        一次输入只在这里读取/解析 transcript：最新原始 user 文本、记录键（去重）
        与历史 SKILL 读取。锁内只按签名复核记录键/actor/root/task/epoch（T11）。
        自带 prompt 的事件只需要记录键去重（宿主没有 input ID 时同一回合不重复
        推进 generation / 不开新 epoch），不再把历史读取当激活证据。
        """
        if not self._transcript_disclosure(record):
            return None
        path = record.transcript_path
        if not path or not self._transcript_file_usable(path):
            return None
        try:
            problem, known = self._transcript_identity_problem(record, path)
        except Exception:
            self.warnings.append("transcript 身份核验失败，激活判定降级")
            return None
        if problem:
            self.warnings.append(
                "transcript 原生%s身份与当前会话不一致，未采用该 transcript 证据" % problem
            )
            return None
        if not known:
            self.warnings.append("transcript 缺原生会话身份，激活证据只按记录内容判定")
        text = ""
        key = ""
        try:
            turn = transcripts.latest_user_turn(path, deadline=self.deadline)
        except Exception:  # transcript 结构异常不影响其余判定
            self.warnings.append("transcript 证据抽取失败，激活判定降级")
            turn = None
        if turn is not None:
            if not turn.complete:
                # 连最新一条 user 记录都取不到时不能把旧记录当成当前回合。
                self.warnings.append("无法从 transcript 取得最新用户记录，激活判定降级")
            text = turn.text or ""
            key = turn.key or ""
        skill_read_offset = -1
        try:
            if not record.prompt:
                skill_read_offset = transcripts.confirmed_skill_read_offset(
                    self.host,
                    path,
                    os.path.join(self.skill_root, "SKILL.md"),
                    actor=record.actor_id or record.session_id,
                    session=record.session_id,
                    deadline=self.deadline,
                )
        except Exception:
            self.warnings.append("transcript 读取证据抽取失败，激活判定降级")
            skill_read_offset = -1
        if not text and not key and skill_read_offset < 0:
            return None
        epoch = 0
        if root_key:
            try:
                epoch = int((self._load("roots", root_key) or {}).get("epoch") or 0)
            except state.StateUnavailable:
                epoch = 0
        return {
            "record_key": key,
            "text": text,
            "skill_read_offset": skill_read_offset,
            "signature": {
                "session": record.session_id,
                "actor": record.actor_id,
                "root_key": root_key or "",
                "task_key": task_key or "",
                "epoch": epoch,
            },
        }

    def _recheck_input_evidence(
        self,
        evidence: Optional[Dict[str, Any]],
        record: Record,
        actor: Dict[str, Any],
        root: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """锁内只复核取证签名，不重读 transcript；签名变化则丢弃并披露。"""
        if not evidence:
            return None
        root_key, task_key = self._keys_for(record, actor)
        current = {
            "session": record.session_id,
            "actor": record.actor_id,
            "root_key": root_key or "",
            "task_key": task_key or "",
            "epoch": int(root.get("epoch") or 0),
        }
        if evidence.get("signature") != current:
            self.warnings.append("输入取证的签名在锁内复核时已变化，丢弃该取证并降级")
            return None
        return evidence

    def _task_identity(
        self,
        root_actor: str,
        root_session: str,
        epoch: int,
        task_root: str,
        record: Record,
    ) -> str:
        task_id = _string(record.raw.get("taskId") or record.raw.get("task_id"))
        anchor = task_root or task_id or "unbound"
        # root session 进入任务身份：同 actor ID 的不同 session 不得共用任务状态。
        return state.identity_key("task", root_actor, root_session, str(epoch), anchor)

    def _new_task(self, task: Dict[str, Any], identity: str, root_actor: str, epoch: int) -> None:
        task.clear()
        task.update(
            {
                "task_identity": identity,
                "task_epoch": epoch,
                "root_actor_id": root_actor,
                "write_seq": 0,
                "last_code_write_seq": 0,
                "reviews": {},
                "has_completed_code_review": False,
                "review_checkpoint": 0,
                "reminded": False,
                "user_generation": 0,
                "pending": {},
                "origins": {},
                "last_success": {},
                "seen_tools": {},
                "seen_inputs": [],
            }
        )

    def _ensure_binding(
        self,
        actor: Dict[str, Any],
        root: Dict[str, Any],
        task: Dict[str, Any],
        record: Record,
    ) -> None:
        """SKILL 读取等无 prompt 的激活证据也要有 root/task 绑定。"""
        if actor.get("activation_scope") != "task_active" or actor.get("task_identity"):
            return
        root_actor = str(actor.get("root_actor_id") or self.actor_key_part(record))
        actor["root_actor_id"] = root_actor
        actor.setdefault("root_session", record.session_id)
        root.setdefault("root_actor_id", root_actor)
        root.setdefault("root_session", record.session_id)
        root.setdefault("epoch", 1)
        root.setdefault("baseline_roots_seen", {})
        root.setdefault("generation", int(actor.get("user_generation") or 0))
        identity = self._task_identity(
            root_actor, str(actor.get("root_session") or record.session_id),
            int(root.get("epoch") or 1), self.context_task_root, record,
        )
        actor["task_identity"] = identity
        actor["epoch"] = int(root.get("epoch") or 1)
        if not task.get("task_identity"):
            self._new_task(task, identity, root_actor, int(root.get("epoch") or 1))

    def _bump_generation(
        self, task: Dict[str, Any], record: Record, marker: str = ""
    ) -> None:
        """每个真实用户回合只推进一次 generation：宿主 input ID 或 user 记录键都算。"""
        if not task:
            return
        seen = task.setdefault("seen_inputs", [])
        markers = [item for item in (marker, record.input_id or "") if item]
        if any(item in seen for item in markers):
            return
        for item in markers:
            seen.append(item)
        if markers:
            del seen[:-100]
        task["user_generation"] = int(task.get("user_generation") or 0) + 1
        task["reminded"] = False

    # ---- 任务根 ----------------------------------------------------
    def _restore_machine_context(self, root: Dict[str, Any]) -> None:
        """按 epoch 恢复上一次 input 里已校验的对话上下文（独立 Hook 进程）。"""
        machine = root.get("machine")
        if not isinstance(machine, dict):
            return
        task_root = _string(machine.get("task_root"))
        if task_root and not self.context_task_root:
            self.context_task_root = task_root
        for key in ("models_file", "role_overrides", "code_review", "doc_paths"):
            if key not in machine:
                continue
            current = self.context.get(key)
            if current in (None, "", [], {}):
                self.context[key] = machine[key]
        if isinstance(machine.get("doc_paths"), list) and not self.doc_paths:
            self.doc_paths = set(_string_list(machine["doc_paths"]))

    def _select_task_root(self, record: Record, root: Dict[str, Any], task: Dict[str, Any]) -> str:
        # artifact 目录只是候选；未经显式绑定不作为基线根，避免把公开产物
        # 目录误当私有敏感快照位置。
        candidates = [
            self.context_task_root,
            self.env.get(TASK_ROOT_ENV, ""),
            _string(task.get("task_root")),
            _string((root.get("machine") or {}).get("task_root")),
        ]
        for candidate in candidates:
            if candidate:
                # 只做词法规范化与系统临时根别名规范化；不 realpath，
                # 否则 task_root 自身的软链事实会被抹去。
                return state.normalize_path(candidate)
        return ""

    def _repo_root(self, record: Record) -> Optional[str]:
        cwd = _tool_cwd(record) or _string(self.context.get("repo_root"))
        if not cwd:
            return None
        return paths.repo_root(cwd, deadline=self.deadline)

    # ---- 规则 ------------------------------------------------------
    def _check_git(self, record: Record, analysis: shell.Analysis) -> Optional[Decision]:
        for action in analysis.git:
            violation = shell.git_violation(action)
            if violation:
                return Decision(
                    "deny",
                    "git",
                    "git_destructive",
                    "拒绝危险 Git 操作：%s。保留用户未提交改动；如确需执行，请用户自行操作或先备份。" % violation,
                )
        return None

    def _check_reviewer(self, record: Record, actor: Dict[str, Any], analysis: Optional[shell.Analysis]) -> Optional[Decision]:
        role = actor.get("role")
        readonly = bool(actor.get("readonly"))
        if role != "reviewer" and not readonly:
            return None
        if record.tool.get("kind") == "write" or record.tool.get("kind") == "patch":
            return Decision(
                "deny",
                "readonly",
                "reviewer_write",
                "reviewer 只读：写文件/应用补丁被拒绝；请把修改交回主代理或开发者。",
            )
        name = (record.tool.get("name") or "").lower()
        if name in {"send_message", "publish", "deploy", "release"}:
            return Decision(
                "deny",
                "readonly",
                "reviewer_external_write",
                "reviewer 只读：对外发送、发布或部署类工具被拒绝。",
            )
        if record.tool.get("kind") == "other" and record.tool.get("name"):
            self.warnings.append("未知自定义工具的写权限未推断，已放行并披露")
        if record.tool.get("kind") == "shell" and analysis is not None:
            # 相对路径的 Git 路径判断需要该次工具调用的可信 cwd，不能回落到进程 cwd。
            violation = shell.reviewer_violation(
                _string(_first(record.tool.get("args") or {}, "command", "CommandLine", "cmd")),
                cwd=_tool_cwd(record) or None,
            )
            if violation and not violation.get("unknown"):
                if violation.get("kind") == "compound_unresolved":
                    # 这一类别没有可指认的写目标，给出的是“怎么改才能过”的可操作理由。
                    reason = (
                        "reviewer 只读：复合结构无法核对，被拒绝；请把命令拆成简单命令重跑，"
                        "只读检查、测试与构建仍可执行。"
                    )
                else:
                    reason = (
                        "reviewer 只读：命令含明确写副作用（%s），被拒绝；"
                        "只读检查、测试与构建仍可执行。" % violation.get("detail")
                    )
                return Decision("deny", "readonly", "reviewer_command_write", reason)
            if violation and violation.get("unknown"):
                self.warnings.append("reviewer 命令结构未覆盖，已放行并披露")
        return None

    def _warn_structure_once(self, record: Record, message: str) -> None:
        """基线结构告警只在 pre 披露；同一 pre 调用内同一原句只留一条。

        post 仍照常计算 prepared 用于指纹配对，只是不重复 pre 已披露的结构缺口；
        这是按事件限定披露，不是跨事件“已警告”状态。
        """
        if record.event != "pre_tool":
            return
        if message in self.warnings:
            return
        self.warnings.append(message)

    def _prepare_write(
        self,
        record: Record,
        *,
        classify: bool,
        root_key: Optional[str] = None,
        task_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """在 task 锁外完成 Git 查询/指纹/符号链检查（共享 deadline）。

        锁内只做短状态读改写；提交前用 task 键与 epoch 复核，不一致就降级。需要
        内容基线的目标在锁外在这里自动保存首次快照（大文件 I/O 不进共享 task 锁）。
        """
        tool = record.tool
        if tool.get("kind") not in {"write", "patch", "shell"}:
            return None
        if tool.get("kind") == "shell":
            command = _string(_first(tool.get("args") or {}, "command", "CommandLine", "cmd"))
            analysis = shell.analyze(command, cwd=_tool_cwd(record) or None)
            if analysis.opaque or analysis.too_deep:
                self._warn_structure_once(record, "命令结构无法完整解析，基线检查未覆盖")
                return None
        repo = self._repo_root(record)
        if not repo:
            self._warn_structure_once(record, "缺少可定位的仓库根或 cwd，基线检查降级")
            return None
        targets, complete, gap = write_targets_from_tool(
            self.host, tool, self.capabilities, _tool_cwd(record)
        )
        if not complete:
            self._warn_structure_once(record, gap or "写入目标不完整，基线检查未覆盖")
            return None
        tool_cwd = _tool_cwd(record)
        entries: List[Dict[str, Any]] = []
        for write_target in targets:
            target = write_target["path"]
            # 该段命令的有效 cwd 优先于工具 cwd（`cd sub; >> app.py`）。
            base = write_target.get("cwd") or tool_cwd or repo
            absolute = target if os.path.isabs(target) else os.path.join(base, target)
            absolute = os.path.normpath(absolute)
            # 先核对已知调用仓库内的词法父链：业务软链可能指向另一个 Git 仓库，
            # 若先做 repo 发现，target_repo 会变成外部仓库，词法目标就成了
            # “仓库外”而被静默跳过，永远到不了软链拒绝。
            lexical_in_repo = paths.lexical_rel(repo, absolute)
            link = (
                paths.symlink_component(repo, absolute)
                if lexical_in_repo is not None
                else None
            )
            if link:
                entries.append(
                    {
                        "relative": lexical_in_repo,
                        "repo": repo,
                        "origin_key": "%s::%s" % (paths.repo_id(repo), lexical_in_repo),
                        "symlink": link,
                        "fingerprint": "symlink",
                        "origin": "",
                    }
                )
                continue
            target_repo = paths.repo_root(os.path.dirname(absolute), deadline=self.deadline) or repo
            lexical = paths.lexical_rel(target_repo, absolute)
            if lexical is None:
                self._warn_structure_once(record, "写入目标位于仓库外，未纳入基线检查")
                continue
            # 解析后的目标仓库内父链同样不允许软链消解。
            link = paths.symlink_component(target_repo, absolute)
            relative = paths.canonical_rel(target_repo, absolute) or lexical
            fingerprint = (
                "symlink"
                if link
                else paths.fingerprint(os.path.join(target_repo, relative), deadline=self.deadline)
            )
            entry = {
                "relative": relative,
                "repo": target_repo,
                "origin_key": "%s::%s" % (paths.repo_id(target_repo), relative),
                "symlink": link,
                "fingerprint": fingerprint,
                "origin": "",
            }
            if classify and not link and fingerprint not in paths.FINGERPRINT_PROBLEMS:
                try:
                    entry["origin"] = paths.classify(target_repo, relative, deadline=self.deadline)
                except paths.GitUnavailable:
                    self.warnings.append("Git 状态查询失败，基线检查降级")
                    return None
            entries.append(entry)
        if not entries:
            return None
        if classify and record.activation_scope == "task_active" and not self._manual_task_root_for(root_key, task_key):
            for entry in entries:
                if entry.get("snapshot") is not None or entry.get("symlink"):
                    continue
                if entry.get("origin") not in {"dirty", "untracked", "unreconstructible"}:
                    continue
                entry["snapshot"] = self._save_snapshot(entry, task_key)
        return {"repo": repo, "entries": entries, "task_key": None}

    def _manual_task_root_for(self, root_key: Optional[str], task_key: Optional[str]) -> bool:
        """是否已有显式手工任务根：本次上下文/环境，或已存进状态的任务/机器上下文。

        有手工根时不写自动快照：锁内会走旧的只读核对路径，快照不会被使用，
        没必要多写一份私有数据。状态不可用时也不预取快照（交给锁内降级）。
        """
        if self.context_task_root or self.env.get(TASK_ROOT_ENV, ""):
            return True
        if not (root_key or task_key):
            return False
        try:
            task = self._load("tasks", task_key) or {} if task_key else {}
            root = self._load("roots", root_key) or {} if root_key else {}
        except state.StateUnavailable:
            return True
        return bool(
            _string(task.get("task_root"))
            or _string((root.get("machine") or {}).get("task_root"))
        )

    def _save_snapshot(self, entry: Dict[str, Any], task_key: Optional[str]) -> Dict[str, Any]:
        """锁外自动保存首次快照：只写私有状态根，不写业务文件。"""
        if not task_key:
            return {"ok": False, "reason": "缺少任务标识，无法定位私有快照目录"}
        fingerprint = entry.get("fingerprint")
        if fingerprint == "nonregular":
            return {"ok": False, "reason": "源文件不是普通文件"}
        if fingerprint == "toolarge":
            return {"ok": False, "reason": "源文件超过 %d 字节上限" % paths.FINGERPRINT_MAX_BYTES}
        if fingerprint in {"unreadable", "absent", "symlink"}:
            return {"ok": False, "reason": "源文件状态不可核对（%s）" % fingerprint}
        directory = os.path.join(
            self.state_root, "baselines", task_key, paths.repo_id(entry["repo"])
        )
        ok, reason = state.save_snapshot(
            directory,
            entry["relative"],
            os.path.join(entry["repo"], entry["relative"]),
            paths.FINGERPRINT_MAX_BYTES,
        )
        return {"ok": ok, "reason": reason}

    def _check_baseline(
        self,
        record: Record,
        root: Dict[str, Any],
        task: Dict[str, Any],
        prepared: Optional[Dict[str, Any]],
    ) -> Optional[Decision]:
        if record.tool.get("kind") not in {"write", "patch", "shell"}:
            return None
        if not prepared:
            return None
        task_root = self._select_task_root(record, root, task)
        for item in prepared.get("entries") or []:
            relative = item["relative"]
            target_repo = item["repo"]
            if item.get("symlink"):
                return Decision(
                    "deny",
                    "baseline",
                    "symlink_target",
                    "写入目标或其仓库内父链是符号链接（%s）：不跟随链接冒充仓库内文件；"
                    "请明确真实目标与范围（确需容受保护文件时显式给出其仓库内真实路径）。" % relative,
                )
            if item.get("fingerprint") in {"nonregular", "toolarge", "unreadable"}:
                # 不可读的普通文件无法自动快照，也无法证明可重建：直接拒绝而不是静默降级。
                fingerprint = item.get("fingerprint")
                detail = {
                    "nonregular": "不是普通文件（目录/FIFO/设备）",
                    "toolarge": "超过 %d 字节上限" % paths.FINGERPRINT_MAX_BYTES,
                    "unreadable": "当前用户不可读或存在硬链",
                }[fingerprint]
                return Decision(
                    "deny",
                    "baseline",
                    "baseline_source_unsafe",
                    "写入目标%s（%s）：请先确认它是可读的普通文件且未超过上限，"
                    "或显式提供 EASYDEV_TASK_ROOT 与 baseline/%s 手工保存原内容。"
                    % (detail, relative, relative),
                )
            if item.get("fingerprint") == "deadline":
                # 超时文案不带仓库相对路径：路径不进 stderr/诊断输出。
                self.warnings.append("基线指纹查询超时，基线检查降级")
                continue
            origins = task.setdefault("origins", {})
            origin_key = item["origin_key"]
            entry = origins.get(origin_key)
            if entry is None:
                entry = {
                    "origin": item.get("origin") or "unreconstructible",
                    "fingerprint": item["fingerprint"],
                    "repo": target_repo,
                    "task_epoch": int(task.get("task_epoch") or 0),
                }
                origins[origin_key] = entry
            origin = entry.get("origin")
            last_success = (task.get("last_success") or {}).get(origin_key)
            current = item["fingerprint"]
            if last_success is not None:
                if current == last_success:
                    continue
                return Decision(
                    "deny",
                    "baseline",
                    "external_modification",
                    "该文件在上一次成功写入后又被外部修改（%s）：请先确认并协调并发变更，"
                    "再按 baseline-safety 重新保存首次原内容。" % relative,
                )
            # 没有可信成功指纹时，“曾经允许过”不是 dirty 豁免：pre 或当前指纹变了
            # 就重新分类，避免取消/失败/缺 post 后的外部改动被旧 clean 放行。
            if entry.get("fingerprint") != current or origin not in {
                "clean_tracked",
                "dirty",
                "untracked",
                "unreconstructible",
                "absent",
            }:
                origin = item.get("origin") or "unreconstructible"
                entry.update(
                    {
                        "origin": origin,
                        "fingerprint": current,
                        "repo": target_repo,
                        "task_epoch": int(task.get("task_epoch") or 0),
                    }
                )
            needs_baseline = origin in {"dirty", "untracked", "unreconstructible"}
            if not needs_baseline:
                continue
            if not task_root:
                # 未显式给任务根：锁外已自动快照首次内容（私有缓存，不写业务文件）。
                snapshot = item.get("snapshot")
                if isinstance(snapshot, dict) and snapshot.get("ok"):
                    task.setdefault("auto_baseline", {})[relative] = {
                        "repo": paths.repo_id(target_repo),
                        "fingerprint": item["fingerprint"],
                        "task_epoch": int(task.get("task_epoch") or 0),
                    }
                    continue
                reason = (snapshot or {}).get("reason") or "未在锁外取得快照"
                return Decision(
                    "deny",
                    "baseline",
                    "baseline_snapshot_failed",
                    "无法自动保存首次快照（%s：%s）：请确认目标是普通文件、未超过上限且当前用户可读；"
                    "或显式提供 EASYDEV_TASK_ROOT 并在 baseline/%s 手工保存原内容。"
                    % (relative, reason, relative),
                )
            owners = task.setdefault("baseline_owners", {})
            owner = owners.get(relative)
            if owner and owner.get("repo") != paths.repo_id(target_repo):
                return Decision(
                    "deny",
                    "baseline",
                    "baseline_shared_path",
                    "同一任务根的 baseline/%s 已被另一个仓库占用：两处原内容不能共用一份快照，"
                    "请为当前仓库显式提供独立任务目录。" % relative,
                )
            # 逐层核验 task_root 祖先链（dir_fd + O_NOFOLLOW）：只有真实路径才能
            # 作为归属键；未消解词法串或祖先软链别名不能冒充新任务根。
            safe_root, root_problem = paths.secure_task_root(task_root)
            if safe_root is None:
                return Decision(
                    "deny",
                    "baseline",
                    "task_root_unsafe",
                    "任务目录无法逐层核验（%s）：祖先或自身是符号链接/非目录/权限不符；"
                    "请提供真实私有任务目录并保存 baseline/%s。" % (root_problem, relative),
                )
            seen = root.setdefault("baseline_roots_seen", {})
            registered = seen.get(safe_root)
            current_epoch = int(task.get("task_epoch") or 0)
            if registered and int(registered.get("epoch", -1)) != current_epoch and needs_baseline:
                return Decision(
                    "deny",
                    "baseline",
                    "reused_task_root",
                    "该任务根属于旧任务（需要内容基线），不能复用旧备份；请显式提供当前任务的新私有目录并保存 baseline/%s。"
                    % relative,
                )
            ok, reason = paths.verify_baseline(task_root, target_repo, relative)
            if not ok:
                return Decision(
                    "deny",
                    "baseline",
                    "missing_baseline",
                    "缺少合规首次基线（%s）：先按 baseline-safety 在任务目录保存 baseline/%s（0600、非软链、无硬链）。"
                    % (reason, relative),
                )
            if not registered:
                seen[safe_root] = {
                    "epoch": current_epoch,
                    "task_identity": task.get("task_identity"),
                }
            owners[relative] = {"repo": paths.repo_id(target_repo)}
            task["task_root"] = task_root
        return None

    def _check_dispatch(
        self,
        record: Record,
        actor: Dict[str, Any],
        root: Dict[str, Any],
        task: Dict[str, Any],
        info: Dict[str, Any],
        prefetch: Optional[Dict[str, Any]] = None,
    ) -> Optional[Decision]:
        if actor.get("child") or record.child:
            return Decision(
                "deny",
                "nested",
                "nested_dispatch",
                "子代理不能再派生新的分派；查询状态、等待结果和回报父代理不受限制。",
            )
        # 锁内只按锁外已取证的签名复核，绝不启动 Git 或全文读取配置。
        signature = (prefetch or {}).get("signature") or {}
        _prefetch_root, prefetch_task = _string(signature.get("root_key")), _string(
            signature.get("task_key")
        )
        _current_root, current_task = self._keys_for(record, actor)
        selection = (prefetch or {}).get("selection")
        if (
            selection is None
            or _string(signature.get("tool_id")) != (record.tool.get("id") or "")
            or prefetch_task != _string(current_task)
            or not prefetch_task
        ):
            self.warnings.append("分派预检的锁外取证与当前上下文不一致，模型预检降级")
            return None
        if not selection.ok:
            return Decision("deny", "models", "models_invalid", "模型配置预检失败：%s" % selection.error)
        items = info.get("items") or [{"role": info.get("role") or "", "prompt": info.get("prompt") or ""}]
        batch = len(items) > 1
        base_id = record.tool.get("id") or ""
        dispatches = root.setdefault("dispatches", {})
        epoch = int(task.get("task_epoch") or 0)
        for index, item in enumerate(items):
            role = item.get("role") or ""
            prompt = item.get("prompt") or ""
            if not role:
                handoff = standard_handoff(prompt, self.skill_root) if prompt else {"role": ""}
                role = handoff.get("role") or ""
            fallback = state.identity_key("dispatch", record.session_id, str(index), prompt[:120])
            # batch 每项需要独立身份，否则后一项会覆盖前一项的取样记录。
            dispatch_id = "%s#%d" % (base_id or fallback, index) if batch else (base_id or fallback)
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16] if prompt else ""
            # 待关联分派：只存摘要与 epoch，不存 prompt 原文；所有角色都登记，
            # 这样 child_start / 子初始 input 才有可信匹配依据。
            previous = dispatches.get(dispatch_id) or {}
            previous_digest = _string(previous.get("digest"))
            previous_role = _string(previous.get("role"))
            dispatches[dispatch_id] = {
                "epoch": epoch,
                "task_identity": task.get("task_identity"),
                "session": record.session_id,
                "digest": digest,
                "role": role or "unknown",
                "code_review": False,
                "child_id": _string(previous.get("child_id")),
            }
            if len(dispatches) > 50:
                for key in list(dispatches)[: len(dispatches) - 50]:
                    dispatches.pop(key, None)
            if previous.get("invalid") or (
                previous_digest
                and (previous_digest != digest or previous_role != (role or "unknown"))
            ):
                # 同 ID 改了交接内容或角色（含 a→b→a）：该 ID 的观测整段失效并保持
                # 失效——不重新取样 checkpoint，旧 child 完成也不再能归因，旧 child
                # 身份不转给新请求；合法的新分派必须使用新 ID。
                self.warnings.append("同一分派 ID 的内容或角色已变化，该段观测失效且不重新取样")
                reviews = task.setdefault("reviews", {})
                existing = reviews.get(dispatch_id)
                if existing is not None:
                    reviews[dispatch_id] = {
                        "start_write_seq": int(existing.get("start_write_seq") or 0),
                        "task_epoch": int(task.get("task_epoch") or 0),
                        "code_review": False,
                        "status": "invalid",
                        "digest": digest,
                    }
                task.setdefault("pending", {}).pop(dispatch_id, None)
                dispatches[dispatch_id]["invalid"] = True
                dispatches[dispatch_id]["digest"] = ""
                dispatches[dispatch_id]["child_id"] = ""
                continue
            if role != "reviewer":
                continue
            if not prompt:
                self.warnings.append("宿主未提供 reviewer 交接正文，交接格式检查降级放行")
                continue
            handoff = standard_handoff(prompt, self.skill_root)
            if not handoff.get("complete"):
                missing = handoff.get("missing") or []
                labels = {
                    "role": "独立 reviewer 角色声明",
                    "role_path": "references/reviewer-role.md 路径",
                    "finding_format": "**[P级] 简短标题** 机器格式",
                }
                described = "、".join(labels.get(entry, entry) for entry in missing)
                dispatches.pop(dispatch_id, None)
                return Decision(
                    "deny",
                    "handoff",
                    "reviewer_handoff_incomplete",
                    "reviewer 交接缺少：%s。请补齐后再分派；本 Hook 不改写交接正文。" % described,
                )
            code_review = bool(self.context.get("code_review")) or bool(handoff.get("code_review"))
            dispatches[dispatch_id]["code_review"] = code_review
            reviews = task.setdefault("reviews", {})
            existing = reviews.get(dispatch_id)
            pending = task.setdefault("pending", {})
            if existing is not None and existing.get("digest") == digest:
                # 同一分派重投：幂等，不重新取样 checkpoint。
                pending.setdefault(
                    dispatch_id,
                    {
                        "role": "reviewer",
                        "epoch": int(task.get("task_epoch") or 0),
                        "code_review": bool(existing.get("code_review")),
                        "digest": digest,
                    },
                )
                continue
            reviews[dispatch_id] = {
                "start_write_seq": int(task.get("write_seq") or 0),
                "task_epoch": int(task.get("task_epoch") or 0),
                "code_review": code_review,
                "status": "started",
                "digest": digest,
            }
            pending[dispatch_id] = {
                "role": "reviewer",
                "epoch": int(task.get("task_epoch") or 0),
                "code_review": code_review,
                "digest": digest,
            }
        return None

    def _prelock_deny(
        self, record: Record, snapshot: Dict[str, Any]
    ) -> Optional[Decision]:
        """取锁前可确定的纯局部拒绝：危险 Git 与 reviewer 只读约束。

        这两类判定不依赖 task 状态提交，却必须在锁忙/状态不可写时保留；
        状态故障只能降级依赖状态的规则（基线、写序列、P3）。
        """
        if record.event != "pre_tool":
            return None
        if self.mode == "probe":
            # probe 只记录观察：任何提前返回都不能变成真正的拦截。
            return None
        scope = snapshot.get("activation_scope")
        if scope not in {"task_active", "role_only"}:
            return None
        analysis = None
        if record.tool.get("kind") == "shell":
            command = _string(_first(record.tool.get("args") or {}, "command", "CommandLine", "cmd"))
            analysis = shell.analyze(command, cwd=_tool_cwd(record) or None)
        if scope == "task_active" and analysis is not None:
            decision = self._check_git(record, analysis)
            if decision:
                return decision
        return self._check_reviewer(record, snapshot, analysis)

    def _dispatch_prefetch(
        self, record: Record, snapshot: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """锁外完成分派前的 Git 仓库探测与 models 全文读取。"""
        if record.event != "pre_tool":
            return None
        if snapshot.get("activation_scope") != "task_active" or snapshot.get("child"):
            return None
        if not dispatch_info(record.tool).get("is_dispatch"):
            return None
        # models_file/task_root 等对话覆盖要按 root 已存上下文恢复后再读取，
        # 否则锁外预取会用上一次进程的默认来源。
        root_key, task_key = self._keys_for(record, snapshot)
        try:
            prelocked_root = self._load("roots", root_key) or {} if root_key else {}
        except state.StateUnavailable:
            return None
        self._restore_machine_context(prelocked_root)
        project_root = self._repo_root(record) or _string(self.context.get("repo_root"))
        return {
            "project_root": project_root,
            "selection": models.select(
                context=self.context, project_root=project_root, env=self.env
            ),
            # 锁内只用这份签名复核；签名不一致就丢弃预取并降级。
            "signature": {
                "root_key": root_key or "",
                "task_key": task_key or "",
                "session": record.session_id,
                "actor": record.actor_id,
                "tool_id": record.tool.get("id") or "",
            },
        }

    # ---- 事件 ------------------------------------------------------
    def handle(self, record: Record) -> Decision:
        short_circuit = self._inactive_short_circuit(record)
        if short_circuit is not None:
            return short_circuit
        if record.event == "unknown":
            self.warnings.append("未知事件，已放行")
            return Decision()
        if record.event == "session_start":
            return Decision()
        if not self.context.get("state_root"):
            # 未显式选择私有根时按当前仓库隔离；缺 cwd 仍可按会话降级使用。
            self.state_root = state.state_root_for(self._repo_root(record), None)
        if not record.session_id:
            self.warnings.append("缺少会话标识，状态判定降级")
            return Decision()
        actor_key = self._actor_key(record)
        try:
            snapshot = self._load("actors", actor_key) or {}
        except state.StateUnavailable:
            self.warnings.append("私有状态不可用，已降级放行")
            return Decision()
        self._decide_identity(snapshot, record)
        if snapshot.get("activation_scope") == "task_active" and not snapshot.get("task_identity"):
            self._ensure_binding(snapshot, {}, {}, record)
        if self._direct_skill_read(record):
            # 停用后直接成功读取 SKILL.md 是可信激活证据：它必须走与 input 恢复相同的
            # 新 epoch/新任务语义，不能只把 actor 的 activation_scope 改回去。
            restored = self._restore_from_skill_read(record, actor_key, snapshot)
            if restored is not None:
                snapshot = restored
                self._decide_identity(snapshot, record)
        if not record.cwd and snapshot.get("cwd"):
            record.cwd = str(snapshot["cwd"])
        # 无锁只读快照只用于定位候选 root/task，提交前会在锁内复核。
        root_key, task_key = self._keys_for(record, snapshot)
        # 只带 transcript 的 input：取证（最新原始 user、记录键、历史 SKILL 读取）
        # 一律在锁外做完并归一成不可变结果，锁内只复核签名（T11）。
        input_evidence: Optional[Dict[str, Any]] = None
        if record.event == "input":
            try:
                input_evidence = self._input_evidence(record, root_key, task_key)
            except state.StateUnavailable:
                input_evidence = None
                self.warnings.append("transcript 输入取证的状态不可用，激活判定降级")
            except Exception:
                input_evidence = None
                self.warnings.append("transcript 输入取证失败，激活判定降级")
        # 已取证的纯局部拒绝（危险 Git、reviewer 只读）在取锁前先算一次：
        # 锁失败或状态故障时不能把它们降级成 allow。
        try:
            prelock_deny = self._prelock_deny(record, snapshot)
        except state.StateUnavailable:
            prelock_deny = None
        except Exception:
            prelock_deny = None
        # 分派前的 Git 仓库探测与 models 全文读取也在锁外完成，锁内只复核签名。
        try:
            dispatch_prefetch = self._dispatch_prefetch(record, snapshot)
        except state.StateUnavailable:
            dispatch_prefetch = None
            self.warnings.append("分派预取状态不可用，模型预检降级")
        except Exception:
            dispatch_prefetch = None
            self.warnings.append("分派预取失败，模型预检降级")
        try:
            return self._handle_state(
                record,
                snapshot,
                actor_key,
                root_key,
                task_key,
                prelock_deny,
                dispatch_prefetch,
                input_evidence,
            )
        except state.StateUnavailable:
            # 状态解码/结构故障只降级依赖状态的规则：已确定的拒绝必须保留。
            if prelock_deny is not None:
                self.warnings.append("状态读取失败，保留已确定的拒绝")
                return prelock_deny
            self.warnings.append("状态读取失败，已降级放行")
            return Decision()
        except Exception:
            if prelock_deny is not None:
                self.warnings.append("状态处理异常，保留已确定的拒绝")
                return prelock_deny
            self.warnings.append("状态处理异常，已降级放行")
            return Decision()

    def _handle_state(
        self,
        record: Record,
        snapshot: Dict[str, Any],
        actor_key: str,
        root_key: Optional[str],
        task_key: Optional[str],
        prelock_deny: Optional[Decision],
        dispatch_prefetch: Optional[Dict[str, Any]],
        input_evidence: Optional[Dict[str, Any]] = None,
    ) -> Decision:
        if record.event == "input":
            # 预演一次激活，确定最终 root/task 键；预演不写盘也不变异真实 record
            # （否则预演出来的 task_identity 会让锁内签名复核误判）。
            probe_actor = dict(snapshot)
            probe_record = copy.copy(record)
            probe_root = self._load("roots", root_key) or {} if root_key else {}
            probe_task = self._load("tasks", task_key) or {} if task_key else {}
            self._activate_input(probe_actor, probe_root, probe_task, probe_record, input_evidence)
            root_key, task_key = self._keys_for(probe_record, probe_actor)
            prepared = None
        elif record.event == "child_start":
            # child_start 必须落实分派关联，而不是空操作；预演只用于定位锁。
            probe_actor = dict(snapshot)
            probe_record = copy.copy(record)
            probe_root = self._load("roots", root_key) or {} if root_key else {}
            probe_task = self._load("tasks", task_key) or {} if task_key else {}
            self._record_child_start(probe_actor, probe_root, probe_record)
            root_key, task_key = self._keys_for(probe_record, probe_actor)
            prepared = None
        elif record.event in {"pre_tool", "post_tool"}:
            # Git 查询/指纹/符号链检查在 task 锁外完成；锁内只做短状态读改写，
            # 提交前用 task_key 与 epoch 复核，不一致就降级。只激活的任务才写
            # 预取：未激活（inactive）与只带角色约束（role_only）都不需要写序列。
            if snapshot.get("activation_scope") == "task_active":
                prepared = self._prepare_write(
                    record,
                    classify=record.event == "pre_tool",
                    root_key=root_key,
                    task_key=task_key,
                )
                if prepared is not None:
                    prepared["task_key"] = task_key
            else:
                prepared = None
        else:
            prepared = None
        try:
            locks = self._locks(actor_key, root_key, task_key)
        except state.StateUnavailable:
            if prelock_deny is not None:
                return prelock_deny
            self.warnings.append("状态锁不可用，已降级放行")
            return Decision()
        with locks:
            try:
                actor = self._load("actors", actor_key) or {}
                self._decide_identity(actor, record)
                loaded_identity = _string(actor.get("task_identity"))
                if not record.cwd and actor.get("cwd"):
                    record.cwd = str(actor["cwd"])
                root = self._load("roots", root_key) or {} if root_key else {}
                task = self._load("tasks", task_key) or {} if task_key else {}
                self._ensure_binding(actor, root, task, record)
                if record.event != "input":
                    self._restore_machine_context(root)
                if (
                    self._direct_skill_read(record)
                    and not record.child
                    and not actor.get("child")
                ):
                    # 本次未恢复（已在 task_active）也要留下消费证据：停用后重投
                    # 同一成功读取不得把会话重新激活。
                    self._record_skill_read(actor, record)
            except state.StateUnavailable:
                if prelock_deny is not None:
                    return prelock_deny
                self.warnings.append("状态读取失败，已降级放行")
                return Decision()
            if record.event in {"pre_tool", "post_tool"}:
                # 锁外预取时的绑定可能已被另一个进程改掉：重新计算完整
                # root/task 身份，变化则丢弃预取，不向旧 task 提交任何记录。
                recheck_root, recheck_task = self._keys_for(record, actor)
                if loaded_identity and (
                    recheck_root != root_key or recheck_task != task_key
                ):
                    self.warnings.append(
                        "锁内复核发现任务绑定已变化，丢弃锁外预取并降级本次写入判定"
                    )
                    decision = prelock_deny or Decision()
                    self._persist(record, actor_key, actor, None, {}, None, {})
                    return decision
            if record.event == "input":
                # 锁内只用锁外归一的取证复核签名，不重新读取/解析 transcript。
                input_evidence = self._recheck_input_evidence(
                    input_evidence, record, actor, root
                )
                self._activate_input(actor, root, task, record, input_evidence)
                root_key, task_key = self._keys_for(record, actor)
                if root_key and not root:
                    root = self._load("roots", root_key) or {}
                if task_key and not task:
                    task = self._load("tasks", task_key) or {}
            elif record.event == "child_start":
                self._record_child_start(actor, root, record)
                root_key, task_key = self._keys_for(record, actor)
                if root_key and not root:
                    root = self._load("roots", root_key) or {}
                if task_key and not task:
                    task = self._load("tasks", task_key) or {}
            elif prepared is not None and prepared.get("task_key") != task_key:
                self.warnings.append("锁外预取结果与当前任务绑定不一致，已降级")
                prepared = None
            stale_child = bool(actor.get("child")) and not self._child_binding_valid(
                actor, root, task
            )
            if stale_child:
                self._retire_child(actor, record)
            if self.mode == "probe":
                self._write_probe(record)
                decision = Decision()
            else:
                decision = self._run_rules(
                    record, actor, root, task, prepared, stale_child, dispatch_prefetch
                )
            if record.session_id and actor.get("activation_scope") not in {
                "task_active",
                "role_only",
            }:
                # 没有任何激活状态的会话也要在本分区留下“已判定为未激活”的锚点：
                # 下一次事件的入口短路直接读它，不必再走一次完整判定。只存 scope，
                # 不含 prompt/命令/路径；一旦真的激活，它会被同一份 actor 状态覆盖。
                actor["activation_scope"] = "inactive"
            if stale_child:
                # 不给已退役的旧 epoch 写父任务状态，只保留本 actor 的降级绑定。
                self._persist(record, actor_key, actor, None, {}, None, {})
            else:
                self._persist(record, actor_key, actor, root_key, root, task_key, task)
        return decision

    def _keys_for(self, record: Record, actor: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        root_actor = str(actor.get("root_actor_id") or record.root_actor_id or "")
        root_session = str(actor.get("root_session") or record.session_id or "")
        identity = str(actor.get("task_identity") or record.task_identity or "")
        root_key = self._root_key(root_session, root_actor) if root_actor else None
        task_key = self._task_key(root_actor, identity) if root_actor and identity else None
        return root_key, task_key

    # ---- 父子绑定（可信分派证据 + 固定 epoch）--------------------------
    def _match_dispatch(
        self,
        parent_session: str,
        dispatch_id: str,
        prompt: str,
        unique_fallback: bool = False,
        bound: Optional[Dict[str, Any]] = None,
        tombstone: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """用可信 child ID 或唯一初始任务摘要匹配父待分派记录，并固定 epoch。

        `bound` 是该 actor 已有的固定绑定（dispatch_id/epoch/task_identity）。
        已有绑定优先返回：重投 start 事件不得凭“当前唯一待关联”把旧 child 改绑
        到新 epoch；绑定所属 epoch 已退役时返回 None（调用方降级 role_only）。

        只读快照：真正的绑定在子自己的锁内提交；这里的失败一律降级为不绑定。
        """
        if not parent_session:
            return None
        parent_key = state.identity_key("actor", self.host, parent_session, parent_session)
        try:
            parent = self._load("actors", parent_key) or {}
            root_actor = str(parent.get("root_actor_id") or "")
            if parent.get("activation_scope") != "task_active" or not root_actor:
                return None
            root_session = str(parent.get("root_session") or parent_session)
            root = self._load("roots", self._root_key(root_session, root_actor)) or {}
        except state.StateUnavailable:
            return None
        if root.get("active") is False:
            return None
        epoch = int(root.get("epoch") or 0)
        dispatches = root.get("dispatches") or {}
        if bound and _string(bound.get("dispatch_id")) and _string(bound.get("task_identity")):
            if int(bound.get("epoch") or 0) != epoch:
                return None
            existing = dispatches.get(_string(bound.get("dispatch_id"))) or {}
            if int(existing.get("epoch") or epoch) != epoch:
                return None
            return {
                "dispatch_id": _string(bound.get("dispatch_id")),
                "root_actor_id": root_actor,
                "root_session": root_session,
                "task_identity": _string(bound.get("task_identity")),
                "epoch": epoch,
                "role": _string(existing.get("role")) or _string(bound.get("role")) or "unknown",
                "code_review": bool(existing.get("code_review"))
                or bool(bound.get("code_review")),
            }
        match: Optional[Tuple[str, Dict[str, Any]]] = None
        speculate = tombstone is None
        if not speculate:
            # 已退役的旧 child 身份不能被重新解释成当前 epoch 的新分派：只有
            # “原分派 ID + 原 epoch + 原 task_identity”三项同时显式匹配才继续，
            # 且下面仍要 epoch 复核。墓碑核对的是不可变的原归属，所以同一
            # dispatch ID 在新 epoch 被重新分派也不会归并旧 child。
            if not (
                dispatch_id
                and dispatch_id == _string(tombstone.get("dispatch_id"))
                and int(tombstone.get("epoch") or 0) == epoch
                and _string(tombstone.get("task_identity"))
            ):
                return None
        if dispatch_id and dispatch_id in dispatches:
            entry = dispatches[dispatch_id]
            if not (isinstance(entry, dict) and entry.get("invalid")):
                match = (dispatch_id, entry)
        elif prompt and speculate:
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
            candidates = [
                (key, value)
                for key, value in dispatches.items()
                if isinstance(value, dict)
                and not value.get("invalid")
                and int(value.get("epoch") or 0) == epoch
                and value.get("digest") == digest
            ]
            if len(candidates) == 1:
                match = candidates[0]
        elif unique_fallback and speculate:
            candidates = [
                (key, value)
                for key, value in dispatches.items()
                if isinstance(value, dict)
                and not value.get("invalid")
                and int(value.get("epoch") or 0) == epoch
                and not value.get("child_id")
            ]
            if len(candidates) == 1:
                match = candidates[0]
        if match is None:
            return None
        matched_id, entry = match
        if not speculate and _string(entry.get("task_identity")) != _string(
            tombstone.get("task_identity")
        ):
            # 同 ID 在新 epoch 重新分派会带新的 task_identity：墓碑的原任务身份
            # 与它不一致时，旧 child 不能被归并到新任务。
            return None
        if int(entry.get("epoch") or 0) != epoch or not entry.get("task_identity"):
            # 旧 epoch 的分派已退役：不做跨任务绑定。
            return None
        return {
            "dispatch_id": matched_id,
            "root_actor_id": root_actor,
            "root_session": root_session,
            "task_identity": entry.get("task_identity"),
            "epoch": epoch,
            "role": _string(entry.get("role")) or "unknown",
            "code_review": bool(entry.get("code_review")),
        }

    def _child_binding_snapshot(self, actor: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """actor 已有的固定 child 绑定（仅当有分派 ID 与 task 身份时）。"""
        if not _string(actor.get("dispatch_id")) or not _string(actor.get("task_identity")):
            return None
        return {
            "dispatch_id": _string(actor.get("dispatch_id")),
            "task_identity": _string(actor.get("task_identity")),
            "epoch": int(actor.get("epoch") or 0),
            "role": _string(actor.get("role")),
        }

    def _apply_child_binding(self, actor: Dict[str, Any], record: Record, match: Dict[str, Any]) -> None:
        actor.update(
            {
                "activation_scope": "task_active",
                "role": match["role"],
                "role_source": "trusted_parent_dispatch",
                "root_actor_id": match["root_actor_id"],
                "root_session": match["root_session"],
                "task_identity": match["task_identity"],
                "epoch": match["epoch"],
                "parent_id": record.parent_id or match["root_session"],
                "child": True,
                "dispatch_id": match["dispatch_id"],
            }
        )
        record.root_actor_id = match["root_actor_id"]
        record.task_identity = match["task_identity"]
        record.activation_scope = "task_active"

    def _child_binding_valid(
        self,
        actor: Dict[str, Any],
        root: Dict[str, Any],
        task: Dict[str, Any],
    ) -> bool:
        """子 input/写/完成前复核 root 当前绑定与 epoch。"""
        if not actor.get("child"):
            return True
        if root.get("active") is False:
            return False
        if int(actor.get("epoch") or 0) != int(root.get("epoch") or 0):
            return False
        if root.get("root_actor_id") and str(actor.get("root_actor_id")) != str(root.get("root_actor_id")):
            return False
        if task and task.get("task_identity") and task.get("task_identity") != actor.get("task_identity"):
            return False
        return True

    def _retire_child(self, actor: Dict[str, Any], record: Record) -> None:
        """旧/不匹配绑定降级为局部 role_only：保留角色约束，不写父任务状态。

        退役必须留下不可重新解释的 child→原分派/epoch 墓碑：task_identity 会被删
        以停止写父任务，若只靠它判断“从未绑定”，第二次重投就会被 unique fallback
        重新绑定到新任务。
        """
        self.warnings.append("子代理绑定与 root 当前 epoch 不匹配，已降级为局部只读约束")
        actor["activation_scope"] = "role_only"
        actor["role"] = actor.get("role") or "unknown"
        actor["role_source"] = "retired_parent_epoch"
        actor["child"] = True
        existing = actor.get("retired_binding")
        if not isinstance(existing, dict) or not (
            _string(existing.get("dispatch_id")) and _string(existing.get("task_identity"))
        ):
            # 首次墓碑不被后续退役覆盖：原分派/epoch/task_identity 是唯一能证明
            # “这个 actor 曾属于哪里”的不变证据，反复退役会把它抹成空串。
            actor["retired_binding"] = {
                "dispatch_id": _string(actor.get("dispatch_id")),
                "task_identity": _string(actor.get("task_identity")),
                "epoch": int(actor.get("epoch") or 0),
                "role": actor.get("role") or "unknown",
            }
        actor.pop("task_identity", None)
        record.task_identity = ""
        record.activation_scope = "role_only"

    def _child_tombstone(self, actor: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """已退役绑定（不可再被重新解释成当前 epoch 的分派）。"""
        value = actor.get("retired_binding")
        if not isinstance(value, dict):
            return None
        if not _string(value.get("dispatch_id")) and not _string(value.get("task_identity")):
            return None
        return value

    def _record_child_start(
        self,
        actor: Dict[str, Any],
        root: Dict[str, Any],
        record: Record,
    ) -> None:
        """SubagentStart 等 child_start 事件：落实分派关联，而不是空操作。"""
        dispatch_id = _string(
            _first(record.raw, "dispatch_id", "dispatchId", "toolCallId", "tool_use_id")
        )
        agent_id = _string(_first(record.raw, "agent_id", "agentId", "actorId")) or record.actor_id
        prompt = record.prompt
        # 显式分派 ID 优先；否则只接受“当前 epoch 里唯一待关联分派”，不猜最近一次。
        # 已有固定绑定的 actor 先复核该绑定：跨 epoch 不得改用新分派。
        bound = self._child_binding_snapshot(actor)
        match = self._match_dispatch(
            record.parent_id,
            dispatch_id,
            prompt,
            unique_fallback=True,
            bound=bound,
            tombstone=self._child_tombstone(actor),
        )
        if match is None:
            self.warnings.append("子代理启动未能匹配可信分派记录，未建立跨任务绑定")
            if actor.get("activation_scope") not in {"task_active", "role_only"}:
                actor.setdefault("child", True)
            return
        self._apply_child_binding(actor, record, match)
        dispatches = root.get("dispatches") or {}
        entry = dispatches.get(match["dispatch_id"])
        if isinstance(entry, dict) and agent_id:
            entry["child_id"] = agent_id

    def _run_rules(
        self,
        record: Record,
        actor: Dict[str, Any],
        root: Dict[str, Any],
        task: Dict[str, Any],
        prepared: Optional[Dict[str, Any]] = None,
        stale_child: bool = False,
        dispatch_prefetch: Optional[Dict[str, Any]] = None,
    ) -> Decision:
        scope_active = (
            actor.get("activation_scope") in {"task_active", "role_only"}
            or record.activation_scope in {"task_active", "role_only"}
        )
        if not scope_active:
            # 未激活的会话（含只带宿主 child 环境变量的普通子任务）不归本 Plugin
            # 管：所有事件都直接放行，不进写序列、不刷 post 告警。唯一例外是可信
            # child 成功读取精确安装路径的 `SKILL.md`：那是本 actor 的局部激活证据，
            # 必须在入口处处理，否则 child 始终未激活、禁嵌套被静默跳过。该激活
            # 不建父任务绑定、不开新 epoch，且与根路径共享读取消费去重（同一 actor
            # 的同一工具调用 ID 只算一次证据），所以显式停用后重投旧读取不恢复。
            if self._activate_child_local_scope(record, actor):
                record.activation_scope = "task_active"
            return Decision()
        if record.event == "pre_tool" and scope_active and (record.child or actor.get("child")):
            info = dispatch_info(record.tool)
            if info.get("is_dispatch"):
                return Decision(
                    "deny",
                    "nested",
                    "nested_dispatch",
                    "子代理不能再派生新的分派；查询状态、等待结果和回报父代理不受限制。",
                )
        if record.event == "input":
            return Decision()
        analysis = None
        if record.tool.get("kind") == "shell":
            command = _string(_first(record.tool.get("args") or {}, "command", "CommandLine", "cmd"))
            analysis = shell.analyze(command, cwd=_tool_cwd(record) or None)
        if record.event == "pre_tool":
            if record.activation_scope == "task_active" and analysis is not None:
                decision = self._check_git(record, analysis)
                if decision:
                    return decision
            decision = self._check_reviewer(record, actor, analysis)
            if decision:
                return decision
            if stale_child:
                # 旧 epoch 的子只能保留局部 role_only：不写父任务写序列/基线/审查关系。
                return Decision()
            info = dispatch_info(record.tool)
            if record.activation_scope == "task_active":
                decision = self._check_baseline(record, root, task, prepared)
                if decision:
                    return decision
                if info.get("is_dispatch"):
                    decision = self._check_dispatch(
                        record, actor, root, task, info, dispatch_prefetch
                    )
                    if decision:
                        return decision
            self._record_pre_write(record, task, prepared)
            return Decision()
        if record.event == "post_tool":
            if not stale_child:
                self._record_post(record, actor, task, prepared)
            return Decision()
        if record.event == "child_start":
            return Decision()
        if record.event == "child_done":
            if not stale_child:
                self._record_child_done(record, root, task, actor)
            return Decision()
        if record.event == "settle":
            return self._check_settle(record, actor, task)
        return Decision()

    def _record_pre_write(
        self,
        record: Record,
        task: Dict[str, Any],
        prepared: Optional[Dict[str, Any]],
    ) -> None:
        """保存与本次 pre 配对的写入意图与写入前指纹（锁外预取）。"""
        tool = record.tool
        if tool.get("kind") not in {"write", "patch", "shell"}:
            return
        tool_id = tool.get("id") or ""
        if not tool_id:
            return
        entries = (prepared or {}).get("entries") or []
        # 目标键含完整仓库身份 + 规范相对路径：跨仓库同相对路径不能互相冒充。
        fingerprints: Dict[str, str] = {}
        for item in entries:
            if item.get("symlink"):
                continue
            fingerprints[item["origin_key"]] = item["fingerprint"]
        pending = task.setdefault("pending_writes", {})
        actor_id = record.actor_id or record.session_id
        epoch = int(task.get("task_epoch") or 0)
        pending[_pending_key(actor_id, epoch, tool_id)] = {
            "actor": actor_id,
            "epoch": epoch,
            "tool_id": tool_id,
            "targets": sorted(fingerprints),
            "fingerprints": fingerprints,
            "complete": bool(entries) and len(fingerprints) == len(entries),
        }
        if len(pending) > 200:
            for key in list(pending)[:100]:
                pending.pop(key, None)

    def _activate_child_local_scope(self, record: Record, actor: Dict[str, Any]) -> bool:
        """子 actor 成功读取精确 `SKILL.md`：只做本 actor 的局部激活。

        子任务的这一条读取证据不需要标准角色交接或父分派绑定；但也不能因此建立
        父任务绑定/写序列或开新 epoch。返回本次是否落地了激活。

        与根路径共享“同 actor、同工具调用 ID 只能作为一次激活证据”：首次接受即
        记下消费证据，因此显式停用后重投旧成功读取不会撤销边界；已激活时只补记
        消费证据，不重复激活、不开新 epoch。
        """
        if not (record.child or actor.get("child")):
            return False
        if not self._direct_skill_read(record):
            return False
        if self._skill_read_consumed(actor, record):
            return False
        self._record_skill_read(actor, record)
        if actor.get("activation_scope") == "task_active":
            return False
        actor["activation_scope"] = "task_active"
        actor["role"] = actor.get("role") or "main"
        actor["role_source"] = "skill_read"
        actor.setdefault("root_actor_id", self.actor_key_part(record))
        return True

    def _record_post(
        self,
        record: Record,
        actor: Dict[str, Any],
        task: Dict[str, Any],
        prepared: Optional[Dict[str, Any]],
    ) -> None:
        tool = record.tool
        name = (tool.get("name") or "").lower()
        success = record.outcome in {"success", "completed"}
        if success and name in READ_TOOL_NAMES and (record.child or actor.get("child")):
            # 子 actor 的读取证据只做本 actor 的局部激活，父任务绑定由分派关联补齐。
            # 根 actor 的直接读取恢复走 `_restore_from_skill_read`（新 epoch），此处
            # 不得只改 activation_scope，否则 root 与 epoch/缓存会停在旧任务。
            self._activate_child_local_scope(record, actor)
        if not success:
            # 结果未知或失败不能建立成功豁免（含可观测但未解析的结果）。
            self.warnings.append("post 缺少可信成功结果，未计入成功写入")
            return
        tool_id = tool.get("id") or ""
        if not tool_id:
            self.warnings.append("缺少工具 ID，写事件无法幂等记账")
            return
        pending = task.setdefault("pending_writes", {})
        actor_id = record.actor_id or record.session_id
        epoch = int(task.get("task_epoch") or 0)
        entry = pending.pop(_pending_key(actor_id, epoch, tool_id), None)
        if entry is None:
            # 只弹出/校验自己的 (actor, epoch, tool ID)，绝不删除其他 actor 的 pending。
            self.warnings.append(
                "post 未找到与该 actor/epoch/tool ID 配对的 pre 记录，未建立成功豁免"
            )
            return
        entries = (prepared or {}).get("entries") or []
        if not entries or not entry.get("complete"):
            self.warnings.append("post 写入目标不完整，写序列记覆盖缺口")
            return
        if any(item.get("symlink") for item in entries):
            self.warnings.append("post 目标含符号链接，未建立成功豁免")
            return
        # 完整目标集合按仓库身份 + 规范相对路径比较：不能只看相对路径。
        targets = sorted(item["origin_key"] for item in entries)
        if targets != list(entry.get("targets") or []):
            self.warnings.append(
                "post 目标与 pre 记录的调用目标不一致（含仓库身份），未建立成功豁免"
            )
            return
        before = entry.get("fingerprints") or {}
        if any(key not in before for key in targets):
            self.warnings.append("post 目标缺少 pre 指纹，未建立成功豁免")
            return
        last_success = task.setdefault("last_success", {})
        changed = False
        for item in entries:
            # 只有相对 pre 指纹真实变化的写入才推进写序列。
            if before.get(item["origin_key"]) != item["fingerprint"] and self._is_code_write(
                item["relative"], task, item["repo"]
            ):
                changed = True
        # 全部目标核对通过后才统一更新成功指纹：部分通过不能建立豁免。
        for item in entries:
            last_success[item["origin_key"]] = item["fingerprint"]
        if changed:
            task["write_seq"] = int(task.get("write_seq") or 0) + 1
            task["last_code_write_seq"] = task["write_seq"]

    def _is_code_write(self, relative: str, task: Dict[str, Any], repo: str) -> bool:
        """先识别运行规则/配置/测试，再应用精确文档/报告路径排除。"""
        normalized = relative.replace(os.sep, "/")
        base = normalized.rsplit("/", 1)[-1]
        lowered = base.lower()
        if base in RUNTIME_RULE_NAMES:
            return True
        path = "/" + normalized
        if "/tests/" in path or "/test/" in path:
            return True
        if lowered.startswith("test_") or lowered.endswith(
            ("_test.py", "_tests.py", ".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js")
        ):
            return True
        if lowered.endswith(CODE_SUFFIXES):
            return True
        if lowered.endswith(".md") and normalized.split("/", 1)[0] in {
            "references",
            "agents",
            "commands",
        }:
            return True
        # 构建/依赖清单与运行配置：即使位于报告目录或被误标文档也优先计入。
        if lowered in BUILD_MANIFEST_NAMES or lowered.startswith((".env", "requirements")):
            return True
        # 明确运行配置：即使被误列进 doc_paths 也按代码/配置写入计入。
        if lowered.endswith(RUNTIME_CONFIG_SUFFIXES):
            return True
        if normalized in self.doc_paths:
            return False
        # 目录前缀不能证明任意文件是任务报告：只排除已绑定 task_root 内
        # 明确的任务报告/文档（reports/、baseline/ 或 report/review 命名），
        # 且上面已优先判定的运行规则/清单/测试/代码不会走到这里。
        task_root = _string(task.get("task_root")) or self.context_task_root
        if task_root and lowered.endswith(DOC_SUFFIXES):
            absolute = os.path.realpath(os.path.join(repo, normalized))
            root_real = os.path.realpath(task_root)
            if absolute.startswith(root_real + os.sep):
                inside_root = os.path.relpath(absolute, root_real).replace(os.sep, "/")
                if inside_root.startswith(("baseline/", "reports/")):
                    return False
                if REPORT_NAME_RE.match(lowered):
                    return False
        return True

    def _record_child_done(
        self,
        record: Record,
        root: Dict[str, Any],
        task: Dict[str, Any],
        actor: Dict[str, Any],
    ) -> None:
        if not self.capabilities.get("completion_events"):
            # 无可信完成来源的宿主明确禁用 P3 完成事件，不把注入事件当支持。
            self.warnings.append("该宿主无法可靠归因审查完成事件，P3 完成不启用")
            return
        dispatch_id = _string(_first(record.raw, "dispatch_id", "dispatchId"))
        if not dispatch_id:
            # 绑定记录里的分派 ID 来自可信 child ID / 唯一摘要匹配，优先于推断。
            dispatch_id = _string(actor.get("dispatch_id"))
        agent_id = _string(_first(record.raw, "agent_id", "agentId", "actorId")) or record.actor_id
        if not dispatch_id and agent_id:
            dispatches = root.get("dispatches") or {}
            matches = [
                key
                for key, value in dispatches.items()
                if isinstance(value, dict) and value.get("child_id") == agent_id
            ]
            if len(matches) == 1:
                dispatch_id = matches[0]
        if not dispatch_id:
            self.warnings.append("子代理完成缺少可关联分派，未计入审查完成")
            return
        dispatches = root.get("dispatches") or {}
        bound = dispatches.get(dispatch_id) or {}
        if int(bound.get("epoch") or 0) != int(root.get("epoch") or 0):
            return
        status = (
            _string(_first(record.raw, "status", "outcome")) or record.outcome
        ).lower()
        report = _string(
            _first(record.raw, "report", "last_assistant_message", "final_message", "message")
        )
        reviews = task.get("reviews") or {}
        entry = reviews.get(dispatch_id)
        if not entry:
            return
        if entry.get("task_epoch") != task.get("task_epoch"):
            return
        if entry.get("status") == "invalid":
            self.warnings.append("该分派 ID 的观测已因内容变化失效，完成事件不计入")
            return
        if status in CHILD_FAILURE_STATUSES:
            # 显式失败/取消/超时/仍在运行优先：非空报告不能把终态反转成 completed。
            self.warnings.append("子代理终态为 %s，未计入审查完成" % status)
            return
        # 只认结构化成功终态；状态缺失时以宿主正常终止事件 + 非空结构化报告为准，
        # 不解析报告正文或最终散文。
        terminal = status in CHILD_SUCCESS_STATUSES or (not status and record.event == "child_done")
        if not terminal or not report:
            return
        if entry.get("status") == "completed":
            return
        entry["status"] = "completed"
        if entry.get("code_review"):
            task["has_completed_code_review"] = True
            task["review_checkpoint"] = max(
                int(task.get("review_checkpoint") or 0),
                int(entry.get("start_write_seq") or 0),
            )
        pending = task.get("pending") or {}
        pending.pop(dispatch_id, None)

    def _check_settle(self, record: Record, actor: Dict[str, Any], task: Dict[str, Any]) -> Decision:
        if record.outcome and record.outcome not in {"completed", "success"}:
            return Decision()
        if self.host == "antigravity":
            if record.termination_reason and record.termination_reason not in {"NO_TOOL_CALL", "model_stop"}:
                return Decision()
            if record.raw.get("fullyIdle") is False:
                return Decision()
        if record.stop_hook_active and self.capabilities.get("stop_hook_active_reliable"):
            return Decision()
        if actor.get("child"):
            return Decision()
        if actor.get("role") not in {"main", "unknown", ""}:
            return Decision()
        if not task.get("has_completed_code_review"):
            if not self.capabilities.get("completion_events") and (task.get("reviews") or {}):
                # 可加载 Stop 但无法可靠归因审查完成：明确披露能力缺口，不续跑。
                self.warnings.append("该宿主无法可靠归因审查完成事件，P3 不启用")
            return Decision()
        if int(task.get("last_code_write_seq") or 0) <= int(task.get("review_checkpoint") or 0):
            return Decision()
        if task.get("reminded"):
            return Decision()
        task["reminded"] = True
        return Decision(
            "continue",
            "settle",
            "review_after_write",
            "审查取样后又观察到代码写入，请定向复审或说明受阻状态。",
        )

    # ---- 探针与持久化 ----------------------------------------------
    def _write_probe(self, record: Record) -> None:
        payload = {
            "protocol": PROTOCOL,
            "host": self.host,
            "event": record.event,
            "tool": record.tool.get("name") or "",
            "fields": sorted((record.tool.get("args") or {}).keys())
            if isinstance(record.tool.get("args"), dict)
            else [],
            "capabilities_missing": [name for name, value in self.capabilities.items() if value is False],
            "decision": "allow",
        }
        try:
            directory = os.path.join(self.state_root, "probe")
            state.write_state(directory, "events.json", {"schema_version": state.SCHEMA_VERSION, "events": payload})
        except state.StateUnavailable:
            print("warning: probe 记录不可写", file=sys.stderr)

    def _persist(
        self,
        record: Record,
        actor_key: str,
        actor: Dict[str, Any],
        root_key: Optional[str],
        root: Dict[str, Any],
        task_key: Optional[str],
        task: Dict[str, Any],
    ) -> None:
        try:
            if actor:
                self._save("actors", actor_key, actor)
            if root_key and root:
                self._save("roots", root_key, root)
            if task_key and task:
                self._save("tasks", task_key, task)
        except state.StateUnavailable:
            self.warnings.append("状态写失败，已保留本次判定并降级")


def load_context(path: Optional[str], host: str) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_INPUT_BYTES + 1)
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return value


def read_request(stream: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    payload = stream.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        return None, "输入超过上限"
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "输入不是合法 JSON"
    if not isinstance(value, dict):
        return None, "输入不是 JSON 对象"
    return value, None


def run(argv: Optional[List[str]] = None, stream: Any = None, env: Optional[Dict[str, str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EasyDev optional hook core")
    parser.add_argument("--host", choices=tuple(HOSTS), required=True)
    parser.add_argument("--context", default=None)
    parser.add_argument("--mode", choices=("probe", "enforce"), default=None)
    parser.add_argument("--state-root", default=None)
    # Antigravity 的 stdin 不带事件名，事件类型由注册时的槽位显式传入。
    parser.add_argument("--event", default=None)
    args = parser.parse_args(argv)
    environment = env if env is not None else os.environ
    raw, error = read_request(stream if stream is not None else sys.stdin.buffer)
    if error:
        print("warning: %s；已放行" % error, file=sys.stderr)
        event = _fail_open_event(args.host, None, _string(args.event))
        print(json.dumps(encode(args.host, event, Decision()), ensure_ascii=False))
        return 0
    context = load_context(args.context, args.host)
    if args.state_root:
        context["state_root"] = args.state_root
    mode = args.mode or _string(context.get("mode")) or "enforce"
    record = None
    engine = None
    normalize_warnings: List[str] = []
    try:
        validate_envelope(args.host, raw)
        record = normalize(
            args.host,
            raw,
            environment,
            cli_event=_string(args.event),
            warnings=normalize_warnings,
        )
        engine = Engine(args.host, context, mode=mode, env=environment)
        engine.warnings.extend(normalize_warnings)
        decision = engine.handle(record)
    except ProtocolFault as fault:
        print("warning: 协议字段类型不符（%s）；已放行" % fault, file=sys.stderr)
        event = _fail_open_event(args.host, raw, _string(args.event))
        print(json.dumps(encode(args.host, event, Decision()), ensure_ascii=False))
        return 0
    except Exception:  # 运行期内部异常：告警放行
        print("warning: hook 内部异常；已放行", file=sys.stderr)
        normalized = record.event if record is not None else ""
        event = _fail_open_event(args.host, raw, _string(args.event), normalized)
        print(json.dumps(encode(args.host, event, Decision()), ensure_ascii=False))
        return 0
    if engine.warnings:
        seen = []
        for warning in engine.warnings:
            if warning not in seen:
                seen.append(warning)
        print("warning: " + "；".join(seen[:3]), file=sys.stderr)
    print(json.dumps(encode(args.host, record.event, decision), ensure_ascii=False))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
