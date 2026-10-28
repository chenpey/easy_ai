"""P2：模型配置、reviewer 交接与子代理嵌套。"""

from __future__ import annotations

import json
import os
import unittest

from support import HookCase


VALID = {
    "developer": {"model": "provider/dev", "reasoning_effort": "high"},
    "reviewer": {"model": "provider/rev", "reasoning_effort": "high"},
}


class DispatchMixin(object):
    def write_models(self, value) -> None:
        target = self.repo / ".easydev" / "models.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(
            value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
        )

    def test_valid_models_and_complete_handoff_are_allowed(self) -> None:
        self.write_models(VALID)
        self.activate()
        self.allow(self.dispatch_payload("s1", self.reviewer_handoff()))

    def test_invalid_selected_models_deny(self) -> None:
        self.activate()
        self.write_models("{not-json")
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({"developer": {"model": "provider/dev"}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({"developer": {"model": "nodash", "reasoning_effort": "high"}, **{"reviewer": VALID["reviewer"]}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({**VALID, "unknown": {}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({**VALID, "developer": {"model": "provider/dev", "reasoning_effort": "extreme"}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))

    def test_duplicate_keys_and_identical_upgrade_are_denied(self) -> None:
        self.activate()
        self.write_models('{"developer": {"model": "p/d", "reasoning_effort": "high"}, "developer": {"model": "p/d", "reasoning_effort": "high"}, "reviewer": {"model": "p/r", "reasoning_effort": "high"}}')
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))
        same = {"developer": VALID["developer"], "reviewer": VALID["reviewer"]}
        self.write_models({**VALID, "upgrades": [same]})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))

    def test_designer_is_optional_and_valid(self) -> None:
        self.activate()
        self.write_models({**VALID, "designer": {"model": "inherit", "reasoning_effort": "inherit"}})
        self.allow(self.dispatch_payload("s1", self.developer_handoff()))
        self.write_models({**VALID, "designer": {"model": "", "reasoning_effort": "high"}})
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))

    def test_unreadable_source_cannot_be_skipped(self) -> None:
        self.activate()
        user = self.home / ".config" / "easydev" / "models.json"
        user.write_text(json.dumps(VALID), encoding="utf-8")
        os.chmod(user, 0o000)
        self.addCleanup(os.chmod, user, 0o600)
        self.deny(
            self.dispatch_payload("s1", self.developer_handoff()),
            env={"HOME": str(self.home)},
        )

    def test_explicit_missing_models_file_denies(self) -> None:
        self.activate()
        self.write_context(models_file=str(self.root / "missing.json"))
        self.deny(self.dispatch_payload("s1", self.developer_handoff()))

    def test_role_overrides_partial_fields(self) -> None:
        self.write_models(VALID)
        self.activate()
        self.write_context(role_overrides={"reviewer": {"reasoning_effort": "max"}})
        self.allow(self.dispatch_payload("s1", self.reviewer_handoff()))
        self.write_context(role_overrides={"reviewer": {"model": "provider/other"}})
        self.allow(self.dispatch_payload("s1", self.reviewer_handoff()))
        self.write_context(role_overrides={"reviewer": {}})
        self.deny(self.dispatch_payload("s1", self.reviewer_handoff()))
        self.write_context(role_overrides={"reviewer": {"reasoning_effort": "extreme"}})
        self.deny(self.dispatch_payload("s1", self.reviewer_handoff()))
        self.write_context(role_overrides={"reviewer": {"unknown": "x"}})
        self.deny(self.dispatch_payload("s1", self.reviewer_handoff()))

    def test_invalid_base_is_not_rescued_by_valid_override(self) -> None:
        self.write_models({"developer": {"model": "p/d", "reasoning_effort": "high"}})
        self.activate()
        self.write_context(role_overrides={"reviewer": {"reasoning_effort": "max"}})
        self.deny(self.dispatch_payload("s1", self.reviewer_handoff()))

    def test_reviewer_handoff_requires_path_and_format(self) -> None:
        self.activate()
        self.write_models(VALID)
        no_path = "role: reviewer\n只读审查 diff。\n**[P级] 简短标题**\n"
        self.deny(self.dispatch_payload("s1", no_path))
        no_format = "role: reviewer\n只读审查 diff。\nreferences/reviewer-role.md\n"
        self.deny(self.dispatch_payload("s1", no_format))
        self.allow(self.dispatch_payload("s1", self.reviewer_handoff()))

    def test_developer_task_without_reviewer_template_is_allowed(self) -> None:
        self.activate()
        self.write_models(VALID)
        self.allow(self.dispatch_payload("s1", "实现这个函数并回报。", role="developer"))
        self.allow(self.dispatch_payload("s1", "不要读 references/reviewer-role.md，直接实现。", role="developer"))

    def test_inactive_dispatch_is_allowed(self) -> None:
        self.write_models("{not-json")
        self.allow(self.dispatch_payload("other", self.reviewer_handoff()))

    def test_high_priority_valid_config_ignores_broken_low_priority(self) -> None:
        self.activate()
        self.write_models(VALID)
        (self.home / ".config" / "easydev" / "models.json").write_text("{broken", encoding="utf-8")
        self.allow(self.dispatch_payload("s1", self.developer_handoff()))

    def test_status_and_wait_are_not_nested_dispatches(self) -> None:
        self.activate()
        self.write_models(VALID)
        if self.host == "pi":
            payload = self.tool_payload(
                "s1", "subagent", {"action": "wait", "id": "x"}, tool_id="wait"
            )
        else:
            payload = self.tool_payload("s1", "wait", {"id": "x"}, tool_id="wait")
        self.allow(payload)


class PiDispatchTests(DispatchMixin, HookCase):
    host = "pi"

    def test_child_cannot_dispatch_nested(self) -> None:
        self.activate("parent")
        self.write_models(VALID)
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
        reason = self.deny(
            self.dispatch_payload("child", self.reviewer_handoff(), tool_id="d2"),
            env=child_env,
        )
        self.assertIn("子代理", reason)

    def test_unactivated_child_dispatch_is_allowed(self) -> None:
        # 冻结契约：未激活一律放行；只带 PI_SUBAGENT_CHILD 的普通子任务不属于
        # 本 Plugin 的分派语义，不能因为它派生子代理就被拦截。
        env = {"PI_SUBAGENT_CHILD": "1", "PI_SUBAGENT_PARENT_SESSION": "missing"}
        self.allow(
            self.dispatch_payload("child", self.developer_handoff(), tool_id="nc"),
            env=env,
        )
        self.allow(
            self.dispatch_payload("child", self.reviewer_handoff(), tool_id="nc2"),
            env=env,
        )

    def test_batch_item_with_bad_handoff_denies_whole_call(self) -> None:
        self.activate()
        self.write_models(VALID)
        payload = self.tool_payload(
            "s1",
            "subagent",
            {
                "action": "create",
                "tasks": [
                    {"agent": "developer", "task": "实现"},
                    {"agent": "reviewer", "task": "只读审查，没有格式"},
                ],
            },
            tool_id="batch",
        )
        self.deny(payload)


class AntigravityDispatchTests(DispatchMixin, HookCase):
    host = "antigravity"


class CodexDispatchTests(DispatchMixin, HookCase):
    host = "codex"


class DshDispatchTests(DispatchMixin, HookCase):
    host = "dsh"

    def test_lost_reviewer_prompt_warns_and_allows(self) -> None:
        self.activate()
        self.write_models(VALID)
        payload = self.tool_payload(
            "s1", "subagent", {"agent_type": "reviewer", "command": ""}, tool_id="lost"
        )
        _code, value, stderr = self.run_core(payload)
        self.assertFalse(self.is_denied(value))
        self.assertIn("warning", stderr)


if __name__ == "__main__":
    unittest.main()
