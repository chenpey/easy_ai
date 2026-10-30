"""协议、激活与四宿主 envelope（A1）。"""

from __future__ import annotations

import json
import os
from unittest import mock

from support import HookCase, core_module


class ExplicitInvocationPatternTests:
    """显式调用边界按 ASCII 标识符判定：中文紧邻仍算调用，ASCII 连写不算。"""

    def test_chinese_adjacent_invocations(self) -> None:
        for prompt in (
            "请使用 easydev 进行审查",
            "帮我用$easydev",
            "开始使用easydev",
        ):
            assert core_module.explicit_invocation(prompt), prompt

    def test_ascii_identifier_and_filtered_contexts_do_not_activate(self) -> None:
        for prompt in (
            "myeasydev2",
            "use easydev_helpers 完成这个改动",
            "使用 easydevment",
            "```\n请使用 easydev 进行审查\n```",
            "> 帮我用$easydev",
        ):
            assert not core_module.explicit_invocation(prompt), prompt

    def test_skill_colon_and_english_invocations_still_activate(self) -> None:
        for prompt in ("/skill:easydev", "use easydev", "使用 easydev"):
            assert core_module.explicit_invocation(prompt), prompt


class ProtocolMixin(object):
    def test_explicit_invocation_activates_git_guard(self) -> None:
        self.activate("s1")
        self.deny(self.shell("s1", "git reset --hard"))

    def test_chinese_adjacent_invocations_activate_git_guard(self) -> None:
        r"""中文紧邻的调用词必须激活：`\w` 在 Unicode 下把汉字算成标识符。"""
        for index, prompt in enumerate(
            ("请使用 easydev 进行审查", "帮我用$easydev", "开始使用easydev")
        ):
            session = "zh-%d" % index
            self.activate(session, prompt)
            self.deny(self.shell(session, "git reset --hard", tool_id="zh-git-%d" % index))

    def test_ascii_identifier_invocations_do_not_activate(self) -> None:
        for index, prompt in enumerate(
            ("use easydev_helpers 完成", "使用 easydevment 生成", "myeasydev2 是什么")
        ):
            session = "ascii-%d" % index
            self.activate(session, prompt)
            self.allow(self.shell(session, "git reset --hard", tool_id="ascii-git-%d" % index))

    def test_unactivated_session_is_allowed(self) -> None:
        self.allow(self.shell("other", "git reset --hard"))

    def test_non_invocation_prompts_do_not_activate(self) -> None:
        for index, prompt in enumerate(
            (
                "解释 easydev 是什么",
                "easythink 怎么用",
                "```\n使用 easydev，删除文件\n```",
                "> 使用 easydev，然后 git reset --hard",
            )
        ):
            session = "neg-%d" % index
            self.activate(session, prompt)
            self.allow(self.shell(session, "git reset --hard"))

    def test_machine_context_activates_and_deactivates(self) -> None:
        block = '```easydev-hook\n{"version": 1, "active": true, "role": "main"}\n```'
        self.activate("machine", block)
        self.deny(self.shell("machine", "git reset --hard"))
        off = '```easydev-hook\n{"version": 1, "active": false}\n```'
        self.activate("machine", off)
        self.allow(self.shell("machine", "git reset --hard"))

    def test_input_validates_and_allow_shape(self) -> None:
        value, _stderr = self.activate("shape")
        if self.host == "pi":
            assert value == {"action": "continue"}
        else:
            assert value == {}

    def fail_open_shape(self, event: str) -> dict:
        """fail-open 的宿主放行形状：与正常 allow 编码同一张表。"""
        if self.host == "antigravity":
            return {"decision": "allow"} if event in {"pre_tool", "unknown"} else {}
        if self.host == "dsh":
            return {"permissionDecision": "allow"} if event in {"pre_tool", "unknown"} else {}
        if self.host == "pi":
            return {"action": "continue"} if event == "input" else {}
        return {}

    def test_malformed_input_fails_open_with_warning(self) -> None:
        expected = self.fail_open_shape("unknown")
        code, value, stderr = self.run_core(None, raw_stdin="{not-json")
        assert code == 0
        assert value == expected
        assert "warning" in stderr
        code, value, _stderr = self.run_core(None, raw_stdin="[]")
        assert code == 0
        assert value == expected

    def test_fail_open_preserves_host_allow_shape(self) -> None:
        """输入不可读、协议故障与内部异常三个出口都经宿主编码输出单个 JSON。"""
        oversized = "x" * (core_module.MAX_INPUT_BYTES + 2)
        for raw_stdin in ("{not-json", "[]", oversized):
            code, value, stderr = self.run_core(None, raw_stdin=raw_stdin)
            assert code == 0
            assert value == self.fail_open_shape("unknown"), raw_stdin[:16]
            assert "warning" in stderr
        # 明确传入事件时按该事件编码，而不是一律退到未知。
        _code, value, _stderr = self.run_core(None, raw_stdin="{not-json", event="PreToolUse")
        assert value == self.fail_open_shape("pre_tool")
        _code, value, _stderr = self.run_core(None, raw_stdin="{not-json", event="input")
        assert value == self.fail_open_shape("input")
        payload = self.input_payload("fault", "使用 easydev")
        if self.host == "antigravity":
            payload["status"] = []
        elif self.host in {"codex", "dsh"}:
            payload["stop_hook_active"] = "false"
        else:
            payload["child"] = "false"
        # 载荷自带的合法事件名优先于 --event：协议故障仍按 input 编码。
        _code, value, stderr = self.run_core(payload, event="PreToolUse")
        assert value == self.fail_open_shape("input")
        assert "协议" in stderr
        assert "Traceback" not in stderr

    def test_internal_error_preserves_normalized_event_allow_shape(self) -> None:
        """内部异常只在 Engine.handle 边界注入：已归一化的事件仍决定放行形状。"""
        self.activate("boom")
        cases = (
            (self.shell("boom", "git status", tool_id="boom-pre"), "pre_tool"),
            (self.settle_payload("boom"), "settle"),
        )
        for payload, normalized in cases:
            with mock.patch.object(
                core_module.Engine, "handle", side_effect=RuntimeError("boom")
            ):
                _code, value, stderr = self.run_core(payload)
            assert value == self.fail_open_shape(normalized), normalized
            assert "内部异常" in stderr
            assert "Traceback" not in stderr

    def test_unknown_event_and_missing_session_are_allowed(self) -> None:
        _code, value, _stderr = self.run_core(
            {"hook_event_name": "SomethingNew", "event": "SomethingNew"}
        )
        assert value == {}
        _code, value, _stderr = self.run_core(
            self.tool_payload("no-session", "bash", {"command": "git reset --hard"})
        )
        assert not self.is_denied(value)

    def test_unknown_host_fields_are_tolerated(self) -> None:
        payload = self.input_payload("extra", "使用 easydev")
        payload["future_field"] = {"nested": True}
        _code, value, _stderr = self.run_core(payload)
        assert not self.is_denied(value)

    def test_skill_read_success_activates(self) -> None:
        path = str(self.skill / "SKILL.md")
        if self.host == "antigravity":
            pre = self.tool_payload("skill", "read_file", {"Path": path}, tool_id="r1")
            post = self.tool_payload("skill", "read_file", {"Path": path}, tool_id="r1", event="post_tool", outcome="success")
        elif self.host in {"codex", "dsh"}:
            pre = self.tool_payload("skill", "read", {"path": path}, tool_id="r1")
            post = self.tool_payload("skill", "read", {"path": path}, tool_id="r1", event="post_tool", outcome="success")
        else:
            pre = self.tool_payload("skill", "read", {"path": path}, tool_id="r1")
            post = self.tool_payload("skill", "read", {"path": path}, tool_id="r1", event="post_tool", outcome="success")
        self.allow(pre)
        self.allow(post)
        self.deny(self.shell("skill", "git reset --hard"))

    def test_skill_read_binds_root_task_for_dispatch(self) -> None:
        path = str(self.skill / "SKILL.md")
        if self.host == "antigravity":
            post = self.tool_payload("skill", "read_file", {"Path": path}, event="post_tool", outcome="success")
        else:
            post = self.tool_payload("skill", "read", {"path": path}, event="post_tool", outcome="success")
        self.allow(post)
        # 绑定后 P2 的 models 预检与交接检查照常执行。
        self.allow(self.dispatch_payload("skill", self.reviewer_handoff()))

    def test_other_skill_file_read_does_not_activate(self) -> None:
        other = self.root / "other" / "SKILL.md"
        other.parent.mkdir(exist_ok=True)
        other.write_text("---\nname: easydev\n---\n", encoding="utf-8")
        if self.host == "antigravity":
            post = self.tool_payload("other", "read_file", {"Path": str(other)}, event="post_tool", outcome="success")
        else:
            post = self.tool_payload("other", "read", {"path": str(other)}, event="post_tool", outcome="success")
        self.allow(post)
        self.allow(self.shell("other", "git reset --hard"))

    def test_session_start_does_not_inject_or_create_task(self) -> None:
        payload = (
            {"hookEventName": "SessionStart", "conversationId": "ss"}
            if self.host == "antigravity"
            else {"hook_event_name": "SessionStart", "session_id": "ss"}
            if self.host in {"codex", "dsh"}
            else {"event": "session_start", "sessionId": "ss"}
        )
        _code, value, stderr = self.run_core(payload)
        assert value == {}
        assert stderr == ""
        tasks = self.state / "tasks"
        assert not (tasks.exists() and any(tasks.iterdir()))

    def test_unlinked_reviewer_handoff_is_role_only(self) -> None:
        prompt = self.reviewer_handoff()
        self.activate("review-only", prompt)
        self.deny(self.write_tool("review-only", "src/app.py"))
        # role_only 不做基线/P3，也不影响其他 actor。
        self.allow(self.write_tool("plain", "src/app.py"))

    def test_reading_reviewer_role_file_does_not_reclassify_main(self) -> None:
        self.activate("main")
        role_file = str(self.skill / "references" / "reviewer-role.md")
        if self.host == "antigravity":
            post = self.tool_payload("main", "read_file", {"Path": role_file}, event="post_tool", outcome="success")
        else:
            post = self.tool_payload("main", "read", {"path": role_file}, event="post_tool", outcome="success")
        self.allow(post)
        self.allow(self.write_tool("main", "src/app.py"))

    def test_probe_mode_allows_and_records_without_payload_text(self) -> None:
        self.activate("probe")
        _code, value, _stderr = self.run_core(
            self.shell("probe", "git reset --hard"), mode="probe"
        )
        # 放行必须按宿主协议编码：Antigravity/DSH 的 PreToolUse 放行是必填字段。
        if self.host == "antigravity":
            assert value == {"decision": "allow"}
        elif self.host == "dsh":
            assert value == {"permissionDecision": "allow"}
        else:
            assert value == {}
        probe_file = self.state / "probe" / "events.json"
        assert probe_file.exists()
        text = probe_file.read_text(encoding="utf-8")
        assert "PreToolUse" in text.replace("pre_tool", "PreToolUse")
        assert "reset --hard" not in text


class PiProtocolTests(ProtocolMixin, HookCase):
    host = "pi"

    def test_trusted_parent_handoff_activates_child(self) -> None:
        self.activate("parent")
        child_env = {
            "PI_SUBAGENT_CHILD": "1",
            "PI_SUBAGENT_PARENT_SESSION": "parent",
        }
        _code, value, stderr = self.run_core(
            {
                "event": "input",
                "sessionId": "child",
                "prompt": self.reviewer_handoff(),
                "cwd": str(self.repo),
                "child": True,
            },
            env=child_env,
        )
        assert not self.is_denied(value), stderr
        self.deny(
            self.tool_payload("child", "write", {"path": "src/app.py"}, tool_id="cw"),
            env=child_env,
        )
        self.deny(
            self.tool_payload(
                "child",
                "subagent",
                {"action": "create", "agent": "developer", "task": "x"},
                tool_id="nested",
            ),
            env=child_env,
        )

    def test_child_settle_does_not_remind(self) -> None:
        self.activate("parent")
        child_env = {"PI_SUBAGENT_CHILD": "1", "PI_SUBAGENT_PARENT_SESSION": "parent"}
        self.run_core(
            {
                "event": "input",
                "sessionId": "child",
                "prompt": self.reviewer_handoff(),
                "cwd": str(self.repo),
                "child": True,
            },
            env=child_env,
        )
        _code, value, _stderr = self.run_core(
            {"event": "settle", "sessionId": "child"}, env=child_env
        )
        assert not self.is_continued(value)


class AntigravityProtocolTests(ProtocolMixin, HookCase):
    host = "antigravity"

    def test_cli_event_names_the_slot_when_stdin_has_none(self) -> None:
        """真实 Antigravity stdin 不带事件名：只能靠注册时的 `--event` 识别。"""
        self.activate("cli-event")
        payload = {
            "conversationId": "cli-event",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "git reset --hard"}},
            "workspacePaths": [str(self.repo)],
        }
        _code, value, _stderr = self.run_core(payload, event="PreToolUse")
        assert self.is_denied(value), value

    def test_cli_event_allow_shape(self) -> None:
        payload = {
            "conversationId": "cli-allow",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
            "workspacePaths": [str(self.repo)],
        }
        _code, value, _stderr = self.run_core(payload, event="PreToolUse")
        assert value == {"decision": "allow"}

    def test_stdin_event_wins_over_cli_event(self) -> None:
        self.activate("both-event")
        payload = {
            "hookEventName": "PreToolUse",
            "conversationId": "both-event",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "git reset --hard"}},
        }
        _code, value, _stderr = self.run_core(payload, event="Stop")
        assert self.is_denied(value), value

    def test_workspace_paths_array_is_used_and_multiple_warns(self) -> None:
        other = self.root / "other-ws"
        other.mkdir()
        payload = {
            "conversationId": "ws",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
            "workspacePaths": [str(self.repo), str(other)],
        }
        _code, value, stderr = self.run_core(payload, event="PreToolUse")
        assert value == {"decision": "allow"}
        assert "workspacePaths" in stderr

    def test_workspace_path_scalar_still_supported(self) -> None:
        self.activate("scalar-ws")
        payload = {
            "conversationId": "scalar-ws",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "git reset --hard"}},
            "workspacePath": str(self.repo),
        }
        _code, value, _stderr = self.run_core(payload, event="PreToolUse")
        assert self.is_denied(value), value


class AntigravityOfficialShapeTests(HookCase):
    """官方 PreToolUse/PostToolUse 载荷：只有 `toolCall` 与整型 `stepIdx`。

    官方文档的 stdin 不带 `toolCall.id`、不带 `status`，失败时顶层带 `error`；事件
    名也不在 stdin 里，只能由 `--event` 指定。写配对、成功指纹与 `SKILL.md` 读取
    激活都靠这三个字段归一后才能工作。
    """

    host = "antigravity"

    def call(self, session: str, name: str, args: dict, step: int, **extra) -> dict:
        payload = {
            "conversationId": session,
            "toolCall": {"name": name, "args": args},
            "stepIdx": step,
            "workspacePaths": [str(self.repo)],
        }
        payload.update(extra)
        return payload

    def post(self, session: str, name: str, args: dict, step: int, error: str = "") -> dict:
        payload = self.call(session, name, args, step)
        if error:
            payload["error"] = error
        return payload

    def task_state(self) -> dict:
        files = sorted((self.state / "tasks").glob("*.json"))
        assert len(files) == 1, files
        return json.loads(files[0].read_text(encoding="utf-8"))

    def test_official_post_tool_use_via_real_cli(self) -> None:
        """真实 CLI（子进程）也要认得只有 `toolCall` + `stepIdx` 的官方载荷。"""
        path = str(self.skill / "SKILL.md")
        _code, _value, _stderr = self.run_core_cli(
            self.post("cli-skill", "read_file", {"Path": path}, 3), event="PostToolUse"
        )
        _code, value, _stderr = self.run_core_cli(
            self.call("cli-skill", "run_command", {"CommandLine": "git reset --hard"}, 4),
            event="PreToolUse",
        )
        assert self.is_denied(value), value

    def test_skill_read_post_activates_without_status(self) -> None:
        path = str(self.skill / "SKILL.md")
        self.allow(self.post("skill", "read_file", {"Path": path}, 1), event="PostToolUse")
        self.deny(
            self.call("skill", "run_command", {"CommandLine": "git reset --hard"}, 2),
            event="PreToolUse",
        )

    def test_failed_skill_read_post_does_not_activate(self) -> None:
        path = str(self.skill / "SKILL.md")
        self.allow(
            self.post("skill-fail", "read_file", {"Path": path}, 1, error="permission denied"),
            event="PostToolUse",
        )
        self.allow(
            self.call("skill-fail", "run_command", {"CommandLine": "git reset --hard"}, 2),
            event="PreToolUse",
        )

    def test_dirty_first_write_snapshots_and_post_records_success(self) -> None:
        self.write_context(task_root="")
        self.activate("dev")
        self.touch_dirty("src/app.py", "value = 2\n")
        target = str(self.repo / "src" / "app.py")
        self.allow(
            self.call("dev", "write_to_file", {"Path": target, "Content": "value = 3\n"}, 5),
            event="PreToolUse",
        )
        saved = sorted((self.state / "baselines").glob("*/*/src/app.py"))
        assert len(saved) == 1, saved
        assert saved[0].read_text(encoding="utf-8") == "value = 2\n"
        # 无调用 ID：配对键用整型 `stepIdx` 合成的 `step:5`。
        pending = self.task_state().get("pending_writes") or {}
        assert any(key.endswith("|step:5") for key in pending), pending
        (self.repo / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
        _code, _value, stderr = self.run_core(
            self.post("dev", "write_to_file", {"Path": target}, 5), event="PostToolUse"
        )
        assert stderr == ""
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 1
        assert task.get("last_success")
        # 第二次写同一文件：命中上次成功指纹，不再要求新基线也不覆盖首次快照。
        self.allow(
            self.call("dev", "write_to_file", {"Path": target, "Content": "value = 4\n"}, 6),
            event="PreToolUse",
        )
        assert len(sorted((self.state / "baselines").glob("*/*/src/app.py"))) == 1

    def test_failed_post_does_not_establish_success(self) -> None:
        self.write_context(task_root="")
        self.activate("dev2")
        self.touch_dirty("src/app.py", "value = 2\n")
        target = str(self.repo / "src" / "app.py")
        self.allow(
            self.call("dev2", "write_to_file", {"Path": target, "Content": "value = 3\n"}, 7),
            event="PreToolUse",
        )
        (self.repo / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
        _code, _value, stderr = self.run_core(
            self.post("dev2", "write_to_file", {"Path": target}, 7, error="exit status 1"),
            event="PostToolUse",
        )
        assert "缺少可信成功结果" in stderr
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 0
        assert not task.get("last_success")

    def test_post_with_another_step_does_not_pair(self) -> None:
        self.write_context(task_root="")
        self.activate("dev3")
        self.touch_dirty("src/app.py", "value = 2\n")
        target = str(self.repo / "src" / "app.py")
        self.allow(
            self.call("dev3", "write_to_file", {"Path": target, "Content": "value = 3\n"}, 8),
            event="PreToolUse",
        )
        (self.repo / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
        # 同一步的多工具调用共用一个 `step:<n>` 身份，配对按 ID 覆盖：换步号就配不上。
        _code, _value, stderr = self.run_core(
            self.post("dev3", "write_to_file", {"Path": target}, 9), event="PostToolUse"
        )
        assert "未找到与该 actor/epoch/tool ID 配对" in stderr
        assert int(self.task_state().get("write_seq") or 0) == 0

    def test_stop_discloses_p3_gap_instead_of_reminding(self) -> None:
        """Antigravity 仍是 completion_events=false：只验证写序列与缺口披露。"""
        self.write_context(task_root="")
        self.activate("dev4")
        self.allow(
            self.call(
                "dev4",
                "invoke_subagent",
                {"agent_type": "reviewer", "prompt": self.reviewer_handoff()},
                1,
            ),
            event="PreToolUse",
        )
        self.touch_dirty("src/app.py", "value = 2\n")
        target = str(self.repo / "src" / "app.py")
        self.allow(
            self.call("dev4", "write_to_file", {"Path": target, "Content": "value = 3\n"}, 2),
            event="PreToolUse",
        )
        (self.repo / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
        self.allow(
            self.post("dev4", "write_to_file", {"Path": target}, 2), event="PostToolUse"
        )
        task = self.task_state()
        assert int(task.get("write_seq") or 0) == 1
        assert task.get("reviews")
        _code, value, stderr = self.run_core(self.settle_payload("dev4"), event="Stop")
        assert not self.is_continued(value), value
        assert "P3 不启用" in stderr


class CodexProtocolTests(ProtocolMixin, HookCase):
    host = "codex"


class DshProtocolTests(ProtocolMixin, HookCase):
    host = "dsh"

    def test_lost_structured_args_warn_and_allow(self) -> None:
        self.activate("dsh")
        payload = self.tool_payload("dsh", "write", {"command": ""}, tool_id="empty")
        _code, value, stderr = self.run_core(payload)
        assert not self.is_denied(value)
        assert "warning" in stderr
