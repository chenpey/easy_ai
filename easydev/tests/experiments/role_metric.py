#!/usr/bin/env python3
"""按角色统计一次 pi 会话读了哪些 references、花了多少。

数据源是 pi 的会话目录：
- 主代理：<session>/<ts>_<id>.jsonl，工具调用是 message.content[].type == "toolCall"；
- 子代理：<session>/subagent-artifacts/<runId>_<agent>_<i>_meta.json（实际模型、usage、
  耗时、toolCount）与同名 _transcript.jsonl（recordType == "tool_start" 的 argsPayload）。

只做统计，不判断审查质量；用于核对交付报告里的 `计量：模块=main:…；dev:…；reviewer:…`
与实际读取是否一致，以及某个子代理是否真的读了指定路径（例如修复点的直接调用方）。

两个边界：
- 只能看见 pi 原生工具参数里的读取。经 `antigravity` 等外部委派工具执行的读取
  不在事件层，`--path` 为空不代表没读；此时会给一行提示。
- 子代理产物落在发起它的主会话目录下，不跟着子代理自己的 cwd。用子代理 cwd
  当 `--session-dir` 找不到会话文件。
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
    parser = argparse.ArgumentParser(description="按角色统计 pi 会话的 references 读取与成本")
    parser.add_argument("--session-dir", type=Path, default=None)
    parser.add_argument(
        "--session",
        default=None,
        help="主会话 uuid 或 jsonl 路径；默认取最新",
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="关注的文件路径或片段（可重复），统计哪些角色在工具参数里提到它",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 数组")
    args = parser.parse_args(argv)

    session_dir = args.session_dir or session_dir_for(Path(os.getcwd()))
    rows = collect(session_dir, args.path, selector=args.session)
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
