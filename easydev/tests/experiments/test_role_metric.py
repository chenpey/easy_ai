"""role_metric：按角色归因 references 读取与成本（无网络、无模型）。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
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


class RoleMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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

    def test_roles_are_separated_and_scoped_to_one_session(self) -> None:
        rows = role_metric.collect(self.session, [], selector=SESSION_A)
        roles = [row["role"] for row in rows]
        self.assertEqual(roles, ["main", "reviewer"])
        main, reviewer = rows
        self.assertEqual(main["references"], ["routing.md"])
        self.assertEqual(reviewer["references"], ["reviewer-role.md"])
        self.assertEqual(reviewer["model"], "provider/model-a:high")
        self.assertEqual(reviewer["tokens"]["input"], 100)

    def test_handoff_prompt_is_not_counted_as_main_reading(self) -> None:
        rows = role_metric.collect(self.session, [], selector=SESSION_A)
        # 交接待办 prompt 里提到 review.md，但不能算主代理读取。
        self.assertNotIn("review.md", rows[0]["references"])

    def test_other_session_artifacts_are_excluded(self) -> None:
        rows = role_metric.collect(self.session, [], selector=SESSION_A)
        models = [row["model"] for row in rows]
        self.assertNotIn("provider/model-b:low", models)
        self.assertEqual(len(rows), 2)

    def test_newest_session_is_selected_by_default(self) -> None:
        rows = role_metric.collect(self.session, [], cwd=Path("/repo"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["source"].endswith(f"{SESSION_B}.jsonl"))

    def test_path_filter_answers_who_read_the_caller(self) -> None:
        rows = role_metric.collect(
            self.session,
            ["src/cart.py", "references/routing.md"],
            selector=SESSION_A,
            cwd=Path("/repo"),
        )
        main, reviewer = rows
        self.assertEqual(reviewer.get("matched_paths"), {"src/cart.py": 1})
        self.assertEqual(main.get("matched_paths"), {"references/routing.md": 1})

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
        self.assertEqual(worker.get("matched_paths"), {"/repo/easydev/references/execution.md": 1})

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
        self.assertEqual(len(opaque), 1)
        self.assertEqual(opaque[0]["tools"], {"antigravity": 1})

        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            role_metric.main(["--session-dir", str(self.session), "--session", SESSION_A])
        self.assertIn("读取可能发生在事件层看不见的地方", err.getvalue())

    def test_no_hint_when_reads_are_visible(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            role_metric.main(["--session-dir", str(self.session), "--session", SESSION_A])
        self.assertEqual(err.getvalue(), "")

    def test_non_object_meta_does_not_crash(self) -> None:
        artifacts = self.session / "subagent-artifacts"
        (artifacts / "run-bad_reviewer_0_transcript.jsonl").write_text(
            json.dumps({"recordType": "tool_start", "toolName": "read", "argsPayload": "{}"}) + "\n",
            encoding="utf-8",
        )
        (artifacts / "run-bad_reviewer_0_meta.json").write_text('["not", "an", "object"]', encoding="utf-8")
        rows = role_metric.collect(self.session, [], selector=SESSION_A)
        self.assertTrue(any(row["role"] == "unknown" for row in rows) is False)

    def test_json_output_is_machine_readable(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = role_metric.main(
                ["--session-dir", str(self.session), "--session", SESSION_A, "--json"]
            )
        self.assertEqual(code, 0)
        parsed = json.loads(buffer.getvalue())
        self.assertEqual([row["role"] for row in parsed], ["main", "reviewer"])
        self.assertIn("tool_calls", parsed[0])


if __name__ == "__main__":
    unittest.main()
