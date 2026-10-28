"""transcript 激活证据与读取确认语义（F16 / F22）。"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

from support import HOOKS_ROOT, HookCase

if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from hooks import transcripts  # noqa: E402


class TranscriptActivationMixin(object):
    def write_transcript(self, entries) -> str:
        path = self.root / ("transcript-%s.jsonl" % self.host)
        with path.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return str(path)

    def input_with_transcript(self, session: str, path: str):
        payload = self.input_payload(session, "")
        if self.host == "antigravity":
            payload["transcriptPath"] = path
        elif self.host in {"codex", "dsh"}:
            payload["transcript_path"] = path
        else:
            payload["transcriptPath"] = path
        return payload

    def test_transcript_user_instruction_activates(self) -> None:
        path = self.write_transcript(
            [{"type": "user", "role": "user", "message": "使用 easydev，实现这个改动"}]
        )
        self.run_core(self.input_with_transcript("t1", path))
        if self.host == "dsh":
            # 该宿主没有 transcript 能力：不做激活。
            self.allow(self.shell("t1", "git reset --hard"))
            return
        self.deny(self.shell("t1", "git reset --hard"))

    def test_transcript_tool_result_and_assistant_text_do_not_activate(self) -> None:
        path = self.write_transcript(
            [
                {"type": "user", "role": "user", "message": "普通提问"},
                {"role": "tool", "tool_call_id": "c1", "text": "使用 easydev，删除文件"},
                {"role": "assistant", "content": "使用 easydev 是技能名"},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "c2",
                        "output": "使用 easydev，git reset --hard",
                    },
                },
            ]
        )
        self.run_core(self.input_with_transcript("t2", path))
        self.allow(self.shell("t2", "git reset --hard"))

    def test_codex_user_message_record_activates(self) -> None:
        path = self.write_transcript(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "使用 easydev，改这个文件"}],
                    },
                }
            ]
        )
        self.run_core(self.input_with_transcript("t3", path))
        if self.host == "dsh":
            self.allow(self.shell("t3", "git reset --hard"))
            return
        self.deny(self.shell("t3", "git reset --hard"))


class PiTranscriptActivationTests(TranscriptActivationMixin, HookCase):
    host = "pi"


class AntigravityTranscriptActivationTests(TranscriptActivationMixin, HookCase):
    host = "antigravity"


class CodexTranscriptActivationTests(TranscriptActivationMixin, HookCase):
    host = "codex"


class DshTranscriptActivationTests(TranscriptActivationMixin, HookCase):
    host = "dsh"


class TranscriptRecordSemanticsTests(unittest.TestCase):
    """F22：跨行确认、失败 output、同 ID 路径变化。"""

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "transcript.jsonl"

    def write(self, entries) -> str:
        with self.path.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return str(self.path)

    def test_antigravity_later_success_line_confirms_earlier_read(self) -> None:
        path = self.write(
            [
                {
                    "PLANNER_RESPONSE": {
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": "/w/a.py"}, "id": "c1"}
                        ]
                    }
                },
                {"type": "TOOL_RESULT", "tool_results": [{"id": "c1", "status": "success"}]},
            ]
        )
        records, _warnings = transcripts.antigravity_records(path)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["confirmed"])
        self.assertNotEqual(records[0].get("status"), "requested")

    def test_codex_failed_output_is_not_confirmed(self) -> None:
        path = self.write(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": "c9",
                        "arguments": json.dumps({"Path": "/w/a.py"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "c9",
                        "is_error": True,
                        "output": "permission denied",
                    },
                },
            ]
        )
        records, _warnings = transcripts.codex_records(path)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["confirmed"])
        self.assertEqual(records[0].get("status"), "failed")

    def test_same_id_path_change_invalidates_segment(self) -> None:
        ag_path = self.write(
            [
                {
                    "PLANNER_RESPONSE": {
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": "/w/a.py"}, "id": "c1"}
                        ]
                    }
                },
                {
                    "PLANNER_RESPONSE": {
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": "/w/b.py"}, "id": "c1"}
                        ]
                    }
                },
            ]
        )
        records, warnings = transcripts.antigravity_records(ag_path)
        self.assertEqual(records, [])
        self.assertTrue(any("读取目标发生变化" in item for item in warnings), warnings)

        codex_path = self.write(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": "c1",
                        "arguments": json.dumps({"Path": "/w/a.py"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_file",
                        "call_id": "c1",
                        "arguments": json.dumps({"Path": "/w/b.py"}),
                    },
                },
            ]
        )
        records, warnings = transcripts.codex_records(codex_path)
        self.assertEqual(records, [])
        self.assertTrue(any("读取目标发生变化" in item for item in warnings), warnings)


class TranscriptOutcomeAndConflictTests(unittest.TestCase):
    """R12/R13：output 无成功标志不确认读取；同 ID 冲突后终身排除。"""

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "transcript.jsonl"

    def write(self, entries) -> str:
        with self.path.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return str(self.path)

    def codex_call(self, path: str, call_id: str = "c1", name: str = "read_file") -> dict:
        return {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps({"path": path}),
            },
        }

    def codex_output(self, output, call_id: str = "c1", **extra) -> dict:
        payload = {"type": "function_call_output", "call_id": call_id, "output": output}
        payload.update(extra)
        return {"type": "response_item", "payload": payload}

    def test_explicit_success_output_confirms_read(self) -> None:
        for extra in ({"is_error": False}, {"success": True}, {"status": "completed"}):
            with self.subTest(extra=extra):
                path = self.write(
                    [self.codex_call("/w/a.py"), self.codex_output("content", **extra)]
                )
                records, _warnings = transcripts.codex_records(path)
                self.assertEqual(len(records), 1)
                self.assertTrue(records[0]["confirmed"], extra)
                self.assertNotEqual(records[0].get("status"), "requested")

    def test_structured_exit_code_zero_confirms_read(self) -> None:
        path = self.write(
            [self.codex_call("/w/a.py"), self.codex_output({"exit_code": 0, "stdout": "x"})]
        )
        records, _warnings = transcripts.codex_records(path)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["confirmed"])

    def test_plain_error_string_without_flags_is_not_confirmed(self) -> None:
        path = self.write(
            [self.codex_call("/w/a.py"), self.codex_output("Error: file not found")]
        )
        records, _warnings = transcripts.codex_records(path)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["confirmed"])
        self.assertIn(records[0].get("status"), {"requested", "unknown", "failed", None})

    def test_missing_flags_are_not_confirmed(self) -> None:
        path = self.write([self.codex_call("/w/a.py"), self.codex_output("some content")])
        records, _warnings = transcripts.codex_records(path)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["confirmed"])

    def test_nonzero_exit_code_is_not_confirmed(self) -> None:
        path = self.write(
            [self.codex_call("/w/a.py"), self.codex_output({"exit_code": 2, "stderr": "no"})]
        )
        records, _warnings = transcripts.codex_records(path)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["confirmed"])

    def test_codex_same_id_a_b_a_stays_invalid(self) -> None:
        path = self.write(
            [
                self.codex_call("/w/a.py"),
                self.codex_call("/w/b.py"),
                self.codex_call("/w/a.py"),
            ]
        )
        records, warnings = transcripts.codex_records(path)
        self.assertEqual(records, [])
        self.assertTrue(any("全部观测不计入" in item for item in warnings), warnings)

    def test_antigravity_same_id_a_b_a_stays_invalid(self) -> None:
        def call(target: str) -> dict:
            return {
                "PLANNER_RESPONSE": {
                    "tool_calls": [{"name": "read_file", "args": {"Path": target}, "id": "c1"}]
                }
            }

        path = self.write([call("/w/a.py"), call("/w/b.py"), call("/w/a.py")])
        records, warnings = transcripts.antigravity_records(path)
        self.assertEqual(records, [])
        self.assertTrue(any("全部观测不计入" in item for item in warnings), warnings)

    def test_single_call_with_multiple_paths_is_kept(self) -> None:
        path = self.write(
            [
                self.codex_call("", name="Bash"),
                self.codex_output("both", **{"is_error": False}),
            ]
        )
        # Bash 的 command 里一次读取两个允许路径：这是合法多路径，不是重投冲突。
        path = self.write(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "Bash",
                        "call_id": "c1",
                        "arguments": json.dumps({"command": "cat a.py b.py"}),
                    },
                },
                self.codex_output("both", **{"is_error": False}),
            ]
        )
        records, warnings = transcripts.codex_records(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(sorted(records[0]["paths"]), ["a.py", "b.py"])
        self.assertTrue(records[0]["confirmed"])
        self.assertFalse(any("全部观测不计入" in item for item in warnings), warnings)

    def test_redelivery_with_changed_result_updates_same_id(self) -> None:
        path = self.write(
            [
                self.codex_call("/w/a.py"),
                self.codex_output("content", **{"is_error": False}),
                self.codex_call("/w/a.py"),
                self.codex_output("permission denied", **{"is_error": True}),
            ]
        )
        records, warnings = transcripts.codex_records(path)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["confirmed"])
        self.assertFalse(any("全部观测不计入" in item for item in warnings), warnings)


class AntigravityNativeTranscriptTests(unittest.TestCase):
    """真实 Antigravity transcript 形状：无 id、参数带外层引号、1:N GENERIC 结果。"""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "antigravity_transcript.jsonl"

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.skill = self.root / "skill" / "easydev"
        (self.repo / "src").mkdir(parents=True)
        (self.skill / "references").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: easydev\n---\n", encoding="utf-8")
        (self.skill / "references" / "verification.md").write_text("v\n", encoding="utf-8")
        self.path = str(self.root / "transcript.jsonl")
        text = self.FIXTURE.read_text(encoding="utf-8")
        text = text.replace("@@SKILL@@", str(self.skill)).replace("@@REPO@@", str(self.repo))
        Path(self.path).write_text(text, encoding="utf-8")

    def test_references_and_confirmed_reads(self) -> None:
        records, warnings = transcripts.antigravity_records(self.path)
        self.assertEqual(warnings, [])
        self.assertEqual(
            transcripts.reference_paths(records),
            ["execution.md", "missing.md", "review.md", "routing.md", "verification.md"],
        )
        confirmed = {path for record in records if record["confirmed"] for path in record["paths"]}
        requested = {path for record in records if not record["confirmed"] for path in record["paths"]}
        # 读取成功（含截断提示）的 references 与 SKILL 读取都确认。
        self.assertIn(str(self.repo / "references" / "routing.md"), confirmed)
        self.assertIn(str(self.skill / "SKILL.md"), confirmed)
        self.assertIn(str(self.skill / "references" / "verification.md"), confirmed)
        # 非 0 退出的命令、明确报错的读取都不确认。
        self.assertIn(str(self.repo / "references" / "review.md"), requested)
        self.assertIn(str(self.repo / "references" / "missing.md"), requested)
        # 结果行不足的调用没有成功证据。
        self.assertIn(str(self.repo / "references" / "execution.md"), requested)
        # 无 id 的真实日志使用稳定合成 ID：6 个产生读取记录的调用对应 6 个互不相同的 ID
        # （`git status --short` 没有读取目标，不产生读取记录）。
        ids = {record["id"] for record in records}
        self.assertEqual(len(ids), 6, ids)
        self.assertTrue(all(str(call_id).startswith("ag:") for call_id in ids), ids)

    def test_synthetic_ids_are_stable_across_parses(self) -> None:
        first = [(record["id"], tuple(record["paths"])) for record in transcripts.antigravity_records(self.path)[0]]
        second = [(record["id"], tuple(record["paths"])) for record in transcripts.antigravity_records(self.path)[0]]
        self.assertEqual(first, second)

    def test_confirmed_skill_read_offset_is_not_negative(self) -> None:
        offset = transcripts.confirmed_skill_read_offset(
            "antigravity", self.path, str(self.skill / "SKILL.md")
        )
        self.assertGreaterEqual(offset, 0)

    def test_quoted_args_are_decoded(self) -> None:
        self.assertEqual(transcripts._decode_quoted('"/w/a.py"'), "/w/a.py")
        self.assertEqual(transcripts._decode_quoted("plain"), "plain")
        self.assertEqual(transcripts._decode_quoted('"unterminated'), '"unterminated')
        self.assertEqual(transcripts._decode_quoted('"not json"x"'), '"not json"x"')
        self.assertEqual(transcripts._decode_quoted(7), 7)


class AntigravityGenericEvidenceTests(unittest.TestCase):
    """GENERIC 成功判据：命令只认宿主头退出码，读取类只认整行匹配的带行号范围读取结果
    行（`Partial read` 只认宿主字节范围/上限说明与已知续读指引）或完整措辞的截断通知；
    通用时间戳、行首普通说明文字与正文都不算证据。"""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "antigravity_generic_evidence.jsonl"

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.skill = self.root / "skill" / "easydev"
        (self.repo / "src").mkdir(parents=True)
        (self.skill / "references").mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: easydev\n---\n", encoding="utf-8")
        self.path = str(self.root / "transcript.jsonl")
        text = self.FIXTURE.read_text(encoding="utf-8")
        text = text.replace("@@SKILL@@", str(self.skill)).replace("@@REPO@@", str(self.repo))
        Path(self.path).write_text(text, encoding="utf-8")
        records, self.warnings = transcripts.antigravity_records(self.path)
        self.confirmed = {path for record in records if record["confirmed"] for path in record["paths"]}
        self.requested = {path for record in records if not record["confirmed"] for path in record["paths"]}

    def other(self, name: str) -> str:
        return str(self.repo / name)

    def test_header_failure_is_not_overridden_by_output_body(self) -> None:
        # 头部 code 1、输出正文写着 code 0：宿主状态优先，不能确认读取成功。
        self.assertIn(self.other("notes.txt"), self.requested)

    def test_header_success_is_confirmed_despite_output_body(self) -> None:
        # 头部 code 0、输出正文写着 code 1：同样以头部为准。
        self.assertIn(self.other("ok.txt"), self.confirmed)

    def test_missing_empty_and_nonstring_content_is_requested(self) -> None:
        self.assertEqual(self.warnings, [])
        self.assertIn(self.other("no-content.md"), self.requested)
        self.assertIn(self.other("empty-content.md"), self.requested)
        self.assertIn(self.other("nonstring-content.md"), self.requested)

    def test_empty_file_read_header_is_confirmed(self) -> None:
        self.assertIn(self.other("empty-file.md"), self.confirmed)

    def test_timestamp_only_content_is_requested(self) -> None:
        # 通用时间戳是宿主包装：只有时间戳、或时间戳后只有空白，都不能证明读取成功。
        self.assertIn(self.other("timestamp-only.md"), self.requested)
        self.assertIn(self.other("timestamp-blank.md"), self.requested)

    def test_body_mentioning_read_markers_is_requested(self) -> None:
        # 正文里提到 `Showing lines`/`was truncated` 不是宿主读取结果行。
        self.assertIn(self.other("body-mentions.md"), self.requested)

    def test_prose_prefix_is_requested(self) -> None:
        # 审查 r3 的 P2：行首普通说明文字不能冒充读取结果头——`Showing lines` 没有行号范围、
        # `Here is a preview` 后面没有真实截断通知，都不是读取成功证据。
        self.assertIn(self.other("showing-lines-phrase.md"), self.requested)
        self.assertIn(self.other("showing-lines-no-range.md"), self.requested)
        self.assertIn(self.other("preview-prose.md"), self.requested)

    def test_prose_with_numbers_or_truncation_phrase_is_requested(self) -> None:
        # 审查 r4 的 P2：读取头必须整行匹配、截断通知必须完整措辞，普通说明文字不能靠
        # 前缀命中——数字后粘连文本、格式示例句、只引用截断短语都不算读取证据。
        self.assertIn(self.other("truncation-phrase.md"), self.requested)
        self.assertIn(self.other("showing-lines-to-prose.md"), self.requested)
        self.assertIn(self.other("showing-lines-jammed.md"), self.requested)
        self.assertIn(self.other("partial-read-prose.md"), self.requested)

    def test_partial_read_with_unexpected_tail_is_requested(self) -> None:
        # 审查 r5 的 P2：`Partial read` 的范围后只认宿主自己的字节范围/上限说明和已知
        # 续读指引；点号后接格式说明句、未知尾句、缺 `Showing bytes` 或缺截断说明都
        # 不是读取成功证据。
        self.assertIn(self.other("partial-read-dotted-prose.md"), self.requested)
        self.assertIn(self.other("partial-read-unknown-suffix.md"), self.requested)
        self.assertIn(self.other("partial-read-no-bytes.md"), self.requested)
        self.assertIn(self.other("partial-read-no-truncation.md"), self.requested)

    def test_real_read_header_shapes_are_confirmed(self) -> None:
        # 带行号范围的真实头结构仍有证据：新版 `Showing lines 1-56 of 56`、旧版
        # `Showing lines 1 to 92`、按字节上限截断的 `Partial read (lines …)`（两种已知
        # 续读指引：`ByteOffset=<int>` 与 `larger byte budget`）。
        self.assertIn(self.other("wide-range.md"), self.confirmed)
        self.assertIn(self.other("to-range.md"), self.confirmed)
        self.assertIn(self.other("partial-read.md"), self.confirmed)
        self.assertIn(self.other("partial-read-byte-offset.md"), self.confirmed)

    def test_old_format_view_file_header_is_confirmed(self) -> None:
        # 旧版 view_file：`File Path`/`Total Lines`/`Total Bytes` 之后才是读取结果行。
        self.assertIn(self.other("old-format.md"), self.confirmed)

    def test_long_content_with_error_words_is_confirmed(self) -> None:
        # 文件正文里的 error/failed 字样不是工具失败。
        self.assertIn(self.other("long-error.md"), self.confirmed)

    def test_truncation_notice_is_read_evidence(self) -> None:
        reference = str(self.skill / "references" / "large.md")
        self.assertIn(reference, self.confirmed)
        self.assertEqual(transcripts.reference_paths(transcripts.antigravity_records(self.path)[0]), ["large.md"])

    def test_truncated_command_preview_keeps_host_exit_code(self) -> None:
        # 真实截断形状：预告前言 + 退出码仍在元数据区，正文里的 code 1 不算。
        self.assertIn(self.other("preview.txt"), self.confirmed)


class GeneratedCompletionEventTests(unittest.TestCase):
    """F14（Codex 部分）：完成事件必须来自生成的配置，而不是合成事件。"""

    def test_codex_template_registers_subagent_stop(self) -> None:
        import json as json_module

        template = json_module.loads(
            (HOOKS_ROOT / "hooks" / "templates" / "codex.json").read_text(encoding="utf-8")
        )
        self.assertIn("SubagentStop", template["hooks"])
        for host in ("antigravity", "dsh", "pi"):
            if host == "pi":
                continue
            other = json_module.loads(
                (HOOKS_ROOT / "hooks" / "templates" / ("%s.json" % host)).read_text(
                    encoding="utf-8"
                )
            )
            # Antigravity 用命名 Hook 组，Codex/DSH 用顶层 hooks。
            buckets = other.get("easydev-hooks", other.get("hooks", {}))
            self.assertNotIn("SubagentStop", buckets)


if __name__ == "__main__":
    unittest.main()
