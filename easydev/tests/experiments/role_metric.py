#!/usr/bin/env python3
"""按角色统计一次会话读了哪些 references、花了多少。

数据源：
- pi（默认）：会话目录里的 `*.jsonl` 与 `subagent-artifacts/`；行为保持不变。
- Antigravity / Codex：显式 `--transcript`（可重复 `--child-transcript`），解析
  `PLANNER_RESPONSE.tool_calls[]` 或 rollout/exec 两种 Codex 格式；不扫描
  `~/.gemini`、`~/.codex` 下的其他会话。

只做统计，不判断审查质量；用于核对交付报告里的 `计量：模块=main:…；dev:…；reviewer:…`
与实际读取是否一致，以及某个子代理是否真的读了指定路径（例如修复点的直接调用方）。

边界：
- 只归因明确读工具与静态读命令；prompt、补丁、交接文本里的路径不算读取。
- 只有可关联成功结果才标 confirmed；否则是 requested/unknown，不能据此声称读成功。
- 子代理产物按 pi 的父会话目录归属；用子代理自己的 cwd 找不到 pi 会话文件。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

EASYDEV_ROOT = Path(__file__).resolve().parents[2]
if str(EASYDEV_ROOT) not in sys.path:
    sys.path.insert(0, str(EASYDEV_ROOT))

from hooks import transcripts  # noqa: E402

REFERENCE_RE = re.compile(r"references/([A-Za-z0-9_.-]+\.md)")
# 参数里出现路径键才算一次可见读取；payload 文本匹配比解析 JSON 宽容。
PATH_KEY_RE = re.compile(r'"(?:file_)?path"\s*:')
SESSION_ID_RE = re.compile(r"_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
# 子代理交接 prompt 里会复述 references 路径，那不是「主代理读了它」。
HANDOFF_TOOLS = {"subagent"}
SESSIONS_ROOT = Path.home() / ".pi" / "agent" / "sessions"


def session_dir_for(cwd: Path) -> Path:
    """pi 把工作目录映射为 --path-with-dashes-- 形式的目录名。"""
    name = "--" + str(cwd).strip("/").replace("/", "-") + "--"
    candidate = SESSIONS_ROOT / name
    if candidate.is_dir():
        return candidate
    raise SystemExit(f"找不到会话目录: {candidate}（用 --session-dir 指定）")


def session_id_of(path: Path) -> Optional[str]:
    match = SESSION_ID_RE.search(path.name)
    return match.group(1) if match else None


def resolve_main_session(session_dir: Path, selector: Optional[str]) -> Path:
    """按 uuid 或文件路径选定主会话；未指定时取最新。"""
    files = [Path(p) for p in glob.glob(str(session_dir / "*.jsonl"))]
    if not files:
        raise SystemExit(f"{session_dir} 下没有会话 jsonl")
    if selector is None:
        return max(files, key=lambda path: path.stat().st_mtime)
    candidate = Path(selector)
    if candidate.is_file():
        return candidate
    matches = [
        path for path in files if path.name == selector or session_id_of(path) == selector
    ]
    if not matches:
        raise SystemExit(f"{session_dir} 下没有会话 {selector}")
    return matches[0]


def tool_payloads(path: Path, child: bool) -> Iterable[Tuple[str, str]]:
    """产出 (工具名, 读取类工具参数文本)；跳过交接待办 prompt。"""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if child:
                if event.get("recordType") != "tool_start":
                    continue
                if event.get("toolName") in HANDOFF_TOOLS:
                    continue
                payload = event.get("argsPayload")
                if isinstance(payload, str) and payload:
                    yield str(event.get("toolName") or "unknown"), payload
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "toolCall":
                    continue
                if block.get("name") in HANDOFF_TOOLS:
                    continue
                arguments = block.get("arguments")
                if arguments is not None:
                    yield (
                        str(block.get("name") or "unknown"),
                        json.dumps(arguments, ensure_ascii=False),
                    )


def _forms(path: str, cwd: Path) -> set:
    """同一路径的绝对与相对写法：查询与参数两侧都要能对上。"""
    forms = {path, os.path.normpath(path)}
    absolute = Path(path).expanduser()
    if absolute.is_absolute():
        forms.add(os.path.relpath(str(absolute), str(cwd)))
    else:
        forms.add(os.path.normpath(str(cwd / absolute)))
    return forms


def summarize_records(
    records: Iterable[transcripts.ReadRecord],
    wanted: Iterable[str],
    cwd: Path,
) -> Dict[str, Any]:
    """按读取记录汇总；只统计读工具/静态读命令，未确认的读取单独标记。"""
    refs: set = set()
    hits: Dict[str, int] = {}
    tools: Counter = Counter()
    file_access: List[Dict[str, Any]] = []
    requested = 0
    confirmed = 0
    queries = {path: _forms(path, cwd) for path in wanted}
    for record in records:
        tools[str(record.get("tool") or "unknown")] += 1
        if record.get("confirmed"):
            confirmed += 1
        else:
            requested += 1
        refs.update(transcripts.reference_paths([record]))
        for path in record.get("paths") or []:
            text = str(path)
            entry = {"path": text, "confirmed": bool(record.get("confirmed"))}
            if entry not in file_access:
                file_access.append(entry)
            for query, forms in queries.items():
                if any(form in text for form in forms):
                    hits[query] = hits.get(query, 0) + 1
    summary: Dict[str, Any] = {
        "tool_calls": len(tools.values()) and sum(tools.values()) or 0,
        "references": sorted(refs),
        "tools": dict(tools.most_common()),
        "visible_reads": bool(file_access),
        "file_access": file_access,
        "module_access": [path for path in sorted(refs)],
    }
    if not file_access:
        summary["attribution_status"] = "unknown"
    elif requested:
        summary["attribution_status"] = "requested" if not confirmed else "partial"
    else:
        summary["attribution_status"] = "confirmed"
    if hits:
        summary["matched_paths"] = dict(sorted(hits.items()))
    return summary


def summarize(payloads: Iterable[Tuple[str, str]], wanted: Iterable[str], cwd: Path) -> Dict[str, Any]:
    refs: set = set()
    hits: Dict[str, int] = {}
    tools: Counter = Counter()
    calls = 0
    visible = False
    queries = {path: _forms(path, cwd) for path in wanted}
    for tool, payload in payloads:
        calls += 1
        tools[tool] += 1
        if PATH_KEY_RE.search(payload):
            visible = True
        refs.update(REFERENCE_RE.findall(payload))
        for path, forms in queries.items():
            if any(form in payload for form in forms):
                hits[path] = hits.get(path, 0) + 1
    summary: Dict[str, Any] = {
        "tool_calls": calls,
        "references": sorted(refs),
        "tools": dict(tools.most_common()),
        # 没有任何带路径键的工具参数：读取可能发生在事件层看不见的外部委派里，
        # 不能因为参数文本里出现 references/ 字样就算读过。
        "visible_reads": visible,
    }
    if hits:
        summary["matched_paths"] = dict(sorted(hits.items()))
    return summary


def child_entries(session_dir: Path, session_id: Optional[str]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for transcript in sorted(glob.glob(str(session_dir / "subagent-artifacts" / "*_transcript.jsonl"))):
        transcript_path = Path(transcript)
        meta_path = Path(transcript.replace("_transcript.jsonl", "_meta.json"))
        meta: Dict[str, Any] = {}
        if meta_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                loaded = {}
            if isinstance(loaded, dict):
                meta = loaded
        if session_id is not None and meta.get("parentSessionId") != session_id:
            # 同一目录下可能有多次会话的产物；只统计本次选中的主会话。
            continue
        usage = meta.get("usage") or {}
        entries.append(
            {
                "role": meta.get("agent") or "unknown",
                "source": str(transcript_path),
                "model": meta.get("model") or "unknown",
                "tokens": {
                    "input": usage.get("input"),
                    "output": usage.get("output"),
                    "cache_read": usage.get("cacheRead"),
                },
                "duration_ms": meta.get("durationMs"),
                "tool_count": meta.get("toolCount"),
                "child": True,
            }
        )
    return entries


def collect(
    session_dir: Path,
    wanted: Iterable[str],
    selector: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    main = resolve_main_session(session_dir, selector)
    session_id = session_id_of(main)
    rows = [
        {
            "role": "main",
            "source": str(main),
            "model": "见宿主",
            "tokens": {},
            "duration_ms": None,
            "tool_count": None,
            "child": False,
        }
    ]
    rows.extend(child_entries(session_dir, session_id))
    workdir = cwd or Path(os.getcwd())
    for row in rows:
        source = Path(row["source"])
        row.update(summarize(tool_payloads(source, child=row["child"]), wanted, workdir))
    return rows


def parse_role_map(raw: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """用户显式声明的 transcript → 角色/父映射；只接受数据，不接受指令。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SystemExit(f"--role-map 不是合法 JSON: {error}")
    if not isinstance(value, dict):
        raise SystemExit("--role-map 必须是对象")
    mapping: Dict[str, Dict[str, Any]] = {}
    for key, entry in value.items():
        if isinstance(entry, str):
            mapping[str(key)] = {"role": entry}
        elif isinstance(entry, dict):
            role = entry.get("role")
            parent = entry.get("parent")
            record: Dict[str, Any] = {}
            if isinstance(role, str) and role:
                record["role"] = role
            if isinstance(parent, str) and parent:
                record["parent"] = parent
            mapping[str(key)] = record
        else:
            raise SystemExit("--role-map 的每个值必须是角色字符串或对象")
    return mapping


def role_for(path: Path, mapping: Dict[str, Dict[str, Any]], default: str) -> Dict[str, Any]:
    for key in (path.name, path.stem, str(path)):
        if key in mapping:
            return mapping[key]
    return {"role": default}


def collect_transcripts(
    host: str,
    transcript: Path,
    children: Iterable[Path],
    wanted: Iterable[str],
    role_map: Dict[str, Dict[str, Any]],
    cwd: Optional[Path],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """主 transcript + 显式子 transcript 的读取归因。"""
    workdir = cwd or Path(os.getcwd())
    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []
    sources = [(transcript, False)] + [(Path(child), True) for child in children]
    for source, is_child in sources:
        if not source.is_file():
            raise SystemExit(f"transcript 不存在: {source}")
        meta = role_for(source, role_map, "unknown" if is_child else "main")
        records, record_warnings = transcripts.read_records(
            host,
            str(source),
            # 未显式给 --cwd 时尊重 transcript 自带元数据 cwd，不用进程目录冒充。
            cwd=str(cwd) if cwd is not None else None,
            actor=meta.get("role"),
        )
        warnings.extend(record_warnings)
        row: Dict[str, Any] = {
            "role": meta.get("role", "unknown"),
            "source": str(source),
            "model": "见宿主",
            "tokens": {},
            "duration_ms": None,
            "tool_count": None,
            "child": is_child,
        }
        row.update(summarize_records(records, wanted, workdir))
        if meta.get("parent"):
            row["parent"] = meta["parent"]
        rows.append(row)
    if not any(row.get("visible_reads") for row in rows):
        warnings.append("没有抽取到任何可归因的读取；空集合不等于没有读取")
    return rows, warnings


def format_table(rows: Iterable[Dict[str, Any]]) -> str:
    rows = list(rows)
    lines = [f"session   {Path(rows[0]['source']).name if rows else '-'}", "role      refs                                                          tokens              ms      paths"]
    for row in rows:
        tokens = row.get("tokens") or {}
        if tokens.get("input") is None:
            token_text = "-"
        else:
            token_text = f"{tokens['input']}in/{tokens['output']}out"
        refs = ",".join(row["references"]) or "-"
        mat = ",".join(f"{k}({v})" for k, v in (row.get("matched_paths") or {}).items()) or "-"
        lines.append(
            f"{row['role']:<9} {refs[:60]:<60} {token_text:<19} "
            f"{str(row.get('duration_ms') or '-'):<7} {mat}"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="按角色统计会话的 references 读取与成本")
    parser.add_argument("--host", choices=("pi", "antigravity", "codex"), default="pi")
    parser.add_argument("--session-dir", type=Path, default=None)
    parser.add_argument(
        "--session",
        default=None,
        help="pi 主会话 uuid 或 jsonl 路径；默认取最新",
    )
    parser.add_argument("--transcript", type=Path, default=None, help="非 pi 宿主的主 transcript")
    parser.add_argument(
        "--child-transcript",
        type=Path,
        action="append",
        default=[],
        help="非 pi 宿主的子 transcript（可重复）",
    )
    parser.add_argument("--role-map", default=None, help="transcript → 角色/父 的 JSON 映射")
    parser.add_argument("--cwd", type=Path, default=None, help="相对路径的解析基准")
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="关注的文件路径或片段（可重复），统计哪些角色在工具参数里提到它",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 数组")
    args = parser.parse_args(argv)

    use_host_parser = args.host != "pi" or args.transcript is not None or args.child_transcript
    if use_host_parser:
        if args.transcript is None:
            parser.error("--host 非 pi 时必须提供 --transcript")
        rows, warnings = collect_transcripts(
            args.host,
            args.transcript,
            args.child_transcript,
            args.path,
            parse_role_map(args.role_map),
            args.cwd,
        )
        for warning in warnings:
            print(f"提示: {warning}", file=sys.stderr)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print(format_table(rows))
        return 0

    if args.child_transcript:
        parser.error("--child-transcript 仅用于 --host 非 pi 或显式 --transcript")
    session_dir = args.session_dir or session_dir_for(Path(os.getcwd()))
    rows = collect(session_dir, args.path, selector=args.session, cwd=args.cwd)
    if rows and session_id_of(Path(rows[0]["source"])) is None:
        print("提示: 主会话文件名不含 uuid，无法按会话过滤子代理产物", file=sys.stderr)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(format_table(rows))
    opaque = [row for row in rows if row.get("tool_calls") and not row.get("visible_reads")]
    if opaque:
        seen: Counter = Counter()
        for row in opaque:
            seen.update(row.get("tools") or {})
        top = ", ".join(f"{name}×{count}" for name, count in seen.most_common(3))
        print(
            f"提示: {len(opaque)} 个角色运行的工具参数里没有路径（工具: {top}）；"
            "读取可能发生在事件层看不见的地方（如外部委派），--path 为空不代表没读",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
