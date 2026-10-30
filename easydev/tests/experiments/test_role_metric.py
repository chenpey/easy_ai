"""role_metric：按角色归因 references 读取与成本（无网络、无模型）。"""

from __future__ import annotations

import pytest

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

EXPERIMENTS_ROOT = Path(__file__).resolve().parent
if str(EXPERIMENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS_ROOT))

import role_metric  # noqa: E402

SESSION_A = "0f0e0d0c-0b0a-4999-8888-777766665555"
SESSION_B = "11111111-2222-4333-9444-555555555555"


def write_main_session(session_dir: Path, session_id: str, calls) -> Path:
    """calls 是 [(tool_name, arguments), ...]。"""
    path = session_dir / f"2026-01-01T00-00-00-000Z_{session_id}.jsonl"
    lines = []
    for tool, payload in calls:
        lines.append(
            json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "name": tool, "arguments": payload}
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_child(
    session_dir: Path,
    run_id: str,
    agent: str,
    payloads,
    meta,
    *,
    parent: str,
    tool: str = "read",
) -> None:
    artifacts = session_dir / "subagent-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    transcript = artifacts / f"{run_id}_{agent}_0_transcript.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(
                {
                    "recordType": "tool_start",
                    "toolName": tool,
                    "argsPayload": json.dumps(payload, ensure_ascii=False),
                },
                ensure_ascii=False,
            )
            for payload in payloads
        )
        + "\n",
        encoding="utf-8",
    )
    (artifacts / f"{run_id}_{agent}_0_meta.json").write_text(
        json.dumps({**meta, "parentSessionId": parent}, ensure_ascii=False), encoding="utf-8"
    )


class RoleMetricTests:
    def setup_method(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Path(self.tmp.name)
        write_main_session(
            self.session,
            SESSION_A,
            [
                ("read", {"path": "/repo/easydev/SKILL.md"}),
                ("read", {"path": "/repo/easydev/references/routing.md"}),
                ("subagent", {"task": "读 easydev/references/review.md 并审查"}),
            ],
        )
        write_child(
            self.session,
            "run-current",
            "reviewer",
            [
                {"path": "/repo/easydev/references/reviewer-role.md"},
                {"path": "/repo/src/cart.py"},
            ],
            {
                "agent": "reviewer",
                "model": "provider/model-a:high",
                "usage": {"input": 100, "output": 20, "cacheRead": 5},
                "durationMs": 1234,
                "toolCount": 2,
            },
            parent=SESSION_A,
        )
        # 同目录下另一个会话的产物：不得混进本次统计。
        write_main_session(
            self.session, SESSION_B, [("read", {"path": "/repo/easydev/references/planning.md"})]
        )
        write_child(
            self.session,
            "run-other",
            "reviewer",
            [{"path": "/repo/easydev/references/review.md"}],
            {
                "agent": "reviewer",
                "model": "provider/model-b:low",
                "usage": {"input": 7, "output": 1},
            },
            parent=SESSION_B,
        )

    def teardown_method(self) -> None:
        self.tmp.cleanup()

    def test_roles_are_separated_and_scoped_to_one_session(self) -> None:
        rows = role_metric.collect(self.session, [], selector=SESSION_A)
        roles = [row["role"] for row in rows]
        assert roles == ["main", "reviewer"]
        main, reviewer = rows
        assert main["references"] == ["routing.md"]
        assert reviewer["references"] == ["reviewer-role.md"]
        assert reviewer["model"] == "provider/model-a:high"
        assert reviewer["tokens"]["input"] == 100

    def test_handoff_prompt_is_not_counted_as_main_reading(self) -> None:
        rows = role_metric.collect(self.session, [], selector=SESSION_A)
        # 交接待办 prompt 里提到 review.md，但不能算主代理读取。
        assert "review.md" not in rows[0]["references"]

    def test_other_session_artifacts_are_excluded(self) -> None:
        rows = role_metric.collect(self.session, [], selector=SESSION_A)
        models = [row["model"] for row in rows]
        assert "provider/model-b:low" not in models
        assert len(rows) == 2

    def test_newest_session_is_selected_by_default(self) -> None:
        rows = role_metric.collect(self.session, [], cwd=Path("/repo"))
        assert len(rows) == 2
        assert rows[0]["source"].endswith(f"{SESSION_B}.jsonl")

    def test_path_filter_answers_who_read_the_caller(self) -> None:
        rows = role_metric.collect(
            self.session,
            ["src/cart.py", "references/routing.md"],
            selector=SESSION_A,
            cwd=Path("/repo"),
        )
        main, reviewer = rows
        assert reviewer.get("matched_paths") == {"src/cart.py": 1}
        assert main.get("matched_paths") == {"references/routing.md": 1}

    def test_absolute_query_matches_relative_payload(self) -> None:
        write_child(
            self.session,
            "run-rel",
            "worker",
            [{"path": "easydev/references/execution.md"}],
            {"agent": "worker", "model": "provider/model-c:low", "usage": {"input": 3, "output": 1}},
            parent=SESSION_A,
        )
        rows = role_metric.collect(
            self.session,
            [str(Path("/repo/easydev/references/execution.md"))],
            selector=SESSION_A,
            cwd=Path("/repo"),
        )
        worker = [row for row in rows if row["role"] == "worker"][0]
        assert worker.get("matched_paths") == {"/repo/easydev/references/execution.md": 1}

    def test_no_visible_read_hint_for_external_delegation(self) -> None:
        # 外部委派工具的参数里没有路径键：读取不在事件层，只能提示，不能当作没读。
        write_child(
            self.session,
            "run-opaque",
            "reviewer",
            [{"tool": "antigravity", "input": "审查当前改动"}],
            {"agent": "reviewer", "model": "provider/model-d:high", "usage": {"input": 9, "output": 2}},
            parent=SESSION_A,
            tool="antigravity",
        )
        collected = role_metric.collect(self.session, [], selector=SESSION_A)
        opaque = [row for row in collected if not row["visible_reads"]]
        assert len(opaque) == 1
        assert opaque[0]["tools"] == {"antigravity": 1}

        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            role_metric.main(["--session-dir", str(self.session), "--session", SESSION_A])
        assert "读取可能发生在事件层看不见的地方" in err.getvalue()

    def test_no_hint_when_reads_are_visible(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            role_metric.main(["--session-dir", str(self.session), "--session", SESSION_A])
        assert err.getvalue() == ""

    def test_non_object_meta_does_not_crash(self) -> None:
        artifacts = self.session / "subagent-artifacts"
        (artifacts / "run-bad_reviewer_0_transcript.jsonl").write_text(
            json.dumps({"recordType": "tool_start", "toolName": "read", "argsPayload": "{}"}) + "\n",
            encoding="utf-8",
        )
        (artifacts / "run-bad_reviewer_0_meta.json").write_text('["not", "an", "object"]', encoding="utf-8")
        rows = role_metric.collect(self.session, [], selector=SESSION_A)
        assert (any(row["role"] == "unknown" for row in rows) is False)

    def test_json_output_is_machine_readable(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = role_metric.main(
                ["--session-dir", str(self.session), "--session", SESSION_A, "--json"]
            )
        assert code == 0
        parsed = json.loads(buffer.getvalue())
        assert [row["role"] for row in parsed] == ["main", "reviewer"]
        assert "tool_calls" in parsed[0]

    def test_antigravity_transcript_attributes_reads_only(self) -> None:
        transcript = Path(self.tmp.name) / "antigravity.jsonl"
        events = [
            {
                "PLANNER_RESPONSE": {
                    "type": "PLANNER_RESPONSE",
                    "cwd": "/repo",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "read_file",
                            "args": {"Path": "/repo/easydev/references/routing.md"},
                        },
                        {
                            "id": "call-2",
                            "name": "view_file",
                            "args": {"Path": "easydev/references/review.md"},
                        },
                        {
                            "id": "call-3",
                            "name": "run_command",
                            "args": {"CommandLine": "cat /repo/easydev/references/execution.md", "Cwd": "/repo"},
                        },
                        {
                            "id": "call-4",
                            "name": "invoke_subagent",
                            "args": {"Prompt": "读 easydev/references/verification.md 并审查"},
                        },
                        {
                            "id": "call-5",
                            "name": "run_command",
                            "args": {"CommandLine": "echo 'references/debugging.md'", "Cwd": "/repo"},
                        },
                    ],
                    "tool_results": [
                        {"tool_call_id": "call-1", "status": "success"},
                        {"tool_call_id": "call-2", "status": "success"},
                    ],
                }
            },
            {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_file", "args": {"Path": "/repo/a.md"}}]},
            {"type": "NOT_A_PLANNER_RESPONSE"},
        ]
        transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        rows, warnings = role_metric.collect_transcripts(
            "antigravity", transcript, [], [], {}, Path("/repo")
        )
        assert warnings == []
        main = rows[0]
        assert main["references"] == ["execution.md", "review.md", "routing.md"]
        assert "verification.md" not in main["references"]
        assert "debugging.md" not in main["references"]
        assert main["attribution_status"] == "partial"
        confirmed = {entry["path"] for entry in main["file_access"] if entry["confirmed"]}
        assert "/repo/easydev/references/routing.md" in confirmed
        assert "/repo/easydev/references/execution.md" not in confirmed

    def test_antigravity_requested_without_results_is_not_confirmed(self) -> None:
        transcript = Path(self.tmp.name) / "antigravity-pending.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": "/repo/easydev/references/routing.md"}}
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        rows, _ = role_metric.collect_transcripts("antigravity", transcript, [], [], {}, Path("/repo"))
        assert rows[0]["attribution_status"] == "requested"
        assert rows[0]["references"] == ["routing.md"]

    def test_codex_rollout_dedupes_completed_and_ignores_patch_paths(self) -> None:
        transcript = Path(self.tmp.name) / "rollout.jsonl"
        events = [
            {"type": "session_meta", "payload": {"id": "s-1", "cwd": "/repo"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "/repo/easydev/SKILL.md"}),
                    "call_id": "call-a",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-a",
                    "output": "ok",
                    "is_error": False,
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "Bash",
                    "arguments": json.dumps({"command": "cat easydev/references/routing.md"}),
                    "call_id": "call-b",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_plan",
                    "arguments": json.dumps({"plan": "读 easydev/references/review.md"}),
                    "call_id": "call-c",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": "*** Begin Patch\n*** Update File: easydev/references/execution.md\n*** End Patch",
                    "call_id": "call-d",
                },
            },
            "not-an-object",
        ]
        transcript.write_text("\n".join(json.dumps(e) if not isinstance(e, str) else e for e in events) + "\n", encoding="utf-8")
        rows, warnings = role_metric.collect_transcripts("codex", transcript, [], [], {}, Path("/repo"))
        main = rows[0]
        assert main["references"] == ["routing.md"]
        assert "review.md" not in main["references"]
        assert "execution.md" not in main["references"]
        assert main["tools"].get("read_file") == 1
        assert main["attribution_status"] == "partial"
        assert any("跳过" in warning for warning in warnings)

    def test_codex_exec_dedupes_same_item_id(self) -> None:
        transcript = Path(self.tmp.name) / "exec.jsonl"
        events = [
            {
                "type": "item.started",
                "item": {
                    "id": "exec-1",
                    "type": "command_execution",
                    "command": "grep -n TODO /repo/easydev/references/code-quality.md",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "exec-1",
                    "type": "command_execution",
                    "command": "grep -n TODO /repo/easydev/references/code-quality.md",
                    "exit_code": 0,
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "exec-2",
                    "type": "command_execution",
                    "command": "cat /repo/easydev/references/review.md",
                    "exit_code": 1,
                },
            },
        ]
        transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        rows, _ = role_metric.collect_transcripts("codex", transcript, [], [], {}, Path("/repo"))
        main = rows[0]
        assert main["references"] == ["code-quality.md", "review.md"]
        assert main["tools"].get("exec_command") == 2
        statuses = {(entry["path"], entry["confirmed"]) for entry in main["file_access"]}
        assert ("/repo/easydev/references/code-quality.md", True) in statuses
        assert ("/repo/easydev/references/review.md", False) in statuses

    def test_child_transcript_and_role_map(self) -> None:
        parent = Path(self.tmp.name) / "parent.jsonl"
        child = Path(self.tmp.name) / "child.jsonl"
        parent.write_text(
            json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [{"name": "read_file", "args": {"Path": "/repo/easydev/SKILL.md"}}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        child.write_text(
            json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": "/repo/easydev/references/reviewer-role.md"}}
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        rows, _ = role_metric.collect_transcripts(
            "antigravity",
            parent,
            [child],
            [],
            {child.name: {"role": "reviewer", "parent": parent.name}},
            Path("/repo"),
        )
        assert [row["role"] for row in rows] == ["main", "reviewer"]
        assert rows[1]["child"]
        assert rows[1]["parent"] == parent.name
        assert rows[1]["references"] == ["reviewer-role.md"]

    def test_host_cli_requires_explicit_transcript(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit):
                role_metric.main(["--host", "codex"])

    def test_cli_role_map_labels_child_transcript(self) -> None:
        parent = Path(self.tmp.name) / "parent-map.jsonl"
        child = Path(self.tmp.name) / "child-map.jsonl"
        line = json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "read_file", "args": {"Path": "/repo/easydev/references/reviewer-role.md"}}],
            }
        )
        parent.write_text(line + "\n", encoding="utf-8")
        child.write_text(line + "\n", encoding="utf-8")
        role_map = json.dumps({child.name: {"role": "reviewer", "parent": parent.name}})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = role_metric.main(
                [
                    "--host",
                    "antigravity",
                    "--transcript",
                    str(parent),
                    "--child-transcript",
                    str(child),
                    "--role-map",
                    role_map,
                    "--json",
                ]
            )
        assert code == 0
        rows = json.loads(buffer.getvalue())
        assert [row["role"] for row in rows] == ["main", "reviewer"]

    def test_codex_cli_outputs_json_rows(self) -> None:
        transcript = Path(self.tmp.name) / "cli.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read",
                        "arguments": json.dumps({"path": "/repo/easydev/references/planning.md"}),
                        "call_id": "c1",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = role_metric.main(["--host", "codex", "--transcript", str(transcript), "--json"])
        assert code == 0
        parsed = json.loads(buffer.getvalue())
        assert parsed[0]["references"] == ["planning.md"]
        assert parsed[0]["attribution_status"] == "requested"
