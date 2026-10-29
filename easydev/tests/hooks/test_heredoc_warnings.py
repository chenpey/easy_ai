"""M2a：五类基线结构告警只在 pre 披露（design v4 §6 / §8.3）。

post 仍照常计算 prepared 用于指纹配对与写序列记账，只是不重复 pre 已披露的
结构缺口；这是按事件限定披露，不是跨事件“已警告”状态。危险 Git 字面在源码里
用字符串拼接构造，避免作者自己的命令行被现网 hook 拦下。
"""

from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from support import HookCase, core_module
from test_r5_fixes import task_states

from hooks import shell  # noqa: E402


DANGEROUS_GIT = "git " + "reset --hard"
BODY_SENTINEL = "HEREDOC-BODY-SENTINEL-9f2c"

STRUCTURE_UNPARSABLE = "命令结构无法完整解析，基线检查未覆盖"
NO_REPO = "缺少可定位的仓库根或 cwd，基线检查降级"
WRITE_TOOL_WITHOUT_PATH = "写工具缺少可识别路径字段"
CWD_UNKNOWN_GAP = "命令内目录变化无法确定，基线检查未覆盖"
TARGET_OUTSIDE = "写入目标位于仓库外，未纳入基线检查"
REVIEWER_UNKNOWN = "reviewer 命令结构未覆盖，已放行并披露"
POST_INCOMPLETE = "post 写入目标不完整，写序列记覆盖缺口"
POST_UNPAIRED = "post 未找到与该 actor/epoch/tool ID 配对的 pre 记录，未建立成功豁免"


def engine_warnings(stderr: str) -> List[str]:
    """core.run 只在 stderr 打一行去重后的前三条告警。"""
    for line in stderr.splitlines():
        if line.startswith("warning: "):
            return [item for item in line[len("warning: "):].split("；") if item]
    return []


class HeredocWarningCase(HookCase):
    host = "pi"

    # ---- 进程内 Engine/Record 工具（宿主 envelope 不含 Decision.code） ----
    def engine(self) -> "core_module.Engine":
        context = json.loads(self.context_path.read_text(encoding="utf-8"))
        return core_module.Engine(self.host, context, mode="enforce", env=self.environment())

    def record(
        self,
        event: str = "pre_tool",
        *,
        session: str = "s1",
        command: str = "",
        cwd: str = "",
        tool: Optional[Dict[str, Any]] = None,
    ) -> "core_module.Record":
        record = core_module.Record(self.host, {}, core_module.HOSTS[self.host])
        record.event = event
        record.session_id = session
        record.cwd = cwd or str(self.repo)
        record.activation_scope = "task_active"
        record.tool = tool or {
            "id": "t-1",
            "name": "bash",
            "kind": "shell",
            "args": {"command": command},
            "paths_complete": True,
        }
        return record

    def prepare(
        self, event: str, *, command: str = "", cwd: str = "", tool: Optional[Dict[str, Any]] = None
    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """只跑 `_prepare_write`：需要直接看 prepared 与完整 warnings 列表时使用。"""
        engine = self.engine()
        prepared = engine._prepare_write(
            self.record(event, command=command, cwd=cwd, tool=tool),
            classify=event == "pre_tool",
        )
        return prepared, engine.warnings

    def decide(self, payload: Dict[str, Any]) -> "core_module.Decision":
        engine = self.engine()
        return engine.handle(core_module.normalize(self.host, payload, self.environment()))

    # ---- 断言工具 --------------------------------------------------
    def snapshots(self, relative: str) -> List[Path]:
        root = self.state / "baselines"
        return sorted(root.rglob(relative)) if root.is_dir() else []

    def pending_entries(self) -> List[Dict[str, Any]]:
        entries = []
        for task in task_states(self.state):
            entries.extend((task.get("pending_writes") or {}).values())
        return entries


class InterpreterHeredocTests(HeredocWarningCase):
    """解释器 stdin 正文是数据：不产生 shell 假执行证据，也不产生结构告警。"""

    def test_python_stdin_heredoc_is_data_without_warning(self) -> None:
        self.activate("s1")
        command = "python3 - <<'PY'\n%s\nprint(%r)\nPY" % (DANGEROUS_GIT, BODY_SENTINEL)
        analysis = shell.analyze(command)
        self.assertEqual(analysis.git, [])
        self.assertTrue(analysis.stdin_program)
        self.assertFalse(analysis.opaque)
        _code, value, stderr = self.run_core(self.shell("s1", command, tool_id="py-data"))
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(stderr, "")
        self.assertNotIn(BODY_SENTINEL, stderr)
        entries = self.pending_entries()
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(entries[0]["targets"], [])


class CatRedirectHeredocTests(HeredocWarningCase):
    """引用正文是数据，外层重定向仍是完整写目标并进入既有首次快照流程。"""

    def test_cat_redirect_heredoc_keeps_complete_target_and_snapshot(self) -> None:
        (self.repo / "out.txt").write_text("user content\n", encoding="utf-8")
        self.activate("s1")
        command = "cat > out.txt <<'EOF'\nhello\n%s\n%s\nEOF" % (DANGEROUS_GIT, BODY_SENTINEL)
        analysis = shell.analyze(command)
        self.assertEqual(
            analysis.writes, [{"kind": "redirect", "target": "out.txt", "complete": True}]
        )
        self.assertEqual(analysis.git, [])
        _code, value, stderr = self.run_core(self.shell("s1", command, tool_id="cat-write"))
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(stderr, "")
        entries = self.pending_entries()
        self.assertEqual(len(entries), 1, entries)
        self.assertTrue(entries[0]["complete"], entries[0])
        self.assertEqual(len(entries[0]["targets"]), 1, entries[0])
        self.assertTrue(entries[0]["targets"][0].endswith("::out.txt"), entries[0])
        saved = self.snapshots("out.txt")
        self.assertEqual(len(saved), 1, saved)
        self.assertEqual(saved[0].read_text(encoding="utf-8"), "user content\n")
        self.assertEqual(stat.S_IMODE(saved[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(saved[0].parent.stat().st_mode), 0o700)
        self.assertNotIn(BODY_SENTINEL, saved[0].read_text(encoding="utf-8"))


class ShellHeredocTests(HeredocWarningCase):
    """壳消费者的正文是程序：既有危险 Git 拒绝必须保留。"""

    def test_shell_heredoc_body_keeps_dangerous_git_deny(self) -> None:
        self.activate("s1")
        command = "sh <<'EOF'\n%s\nEOF" % DANGEROUS_GIT
        analysis = shell.analyze(command)
        self.assertTrue([action for action in analysis.git if action["sub"] == "reset"])
        reason = self.deny(self.shell("s1", command, tool_id="sh-body"))
        self.assertIn("危险 Git", reason)


class StructureWarningScopeTests(HeredocWarningCase):
    """五类基线结构原句：pre 披露、post 不重复，原判定与返回不变。"""

    def test_unclosed_heredoc_structure_warning_is_pre_only(self) -> None:
        self.activate("s1")
        # 未闭合正文：heredoc 边界无法证明，pre 按原条件披露结构缺口。
        command = "cat > out.txt <<'EOF'\nhello\n"
        self.assertTrue(shell.analyze(command).opaque)
        pre = self.shell("s1", command, tool_id="open-heredoc")
        _code, value, pre_stderr = self.run_core(pre)
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(engine_warnings(pre_stderr).count(STRUCTURE_UNPARSABLE), 1)
        post = self.shell(
            "s1", command, tool_id="open-heredoc", event="post_tool", outcome="success"
        )
        _code, value, post_stderr = self.run_core(post)
        self.assertFalse(self.is_denied(value), value)
        warnings = engine_warnings(post_stderr)
        self.assertNotIn(STRUCTURE_UNPARSABLE, warnings)
        # post 自己的记账缺口保留，不因结构原句 pre-only 而消失。
        self.assertIn(POST_INCOMPLETE, warnings)

    def test_write_tool_without_path_is_still_disclosed_on_pre(self) -> None:
        tool = {
            "id": "t-no-path",
            "name": "write",
            "kind": "write",
            "args": {},
            "paths_complete": True,
        }
        prepared, warnings = self.prepare("pre_tool", tool=tool)
        self.assertIsNone(prepared)
        self.assertEqual(warnings, [WRITE_TOOL_WITHOUT_PATH])
        prepared, warnings = self.prepare("post_tool", tool=tool)
        self.assertIsNone(prepared)
        self.assertEqual(warnings, [])

    def test_baseline_structure_warnings_are_pre_only(self) -> None:
        cases = (
            ("opaque", dict(command="cat > out.txt <<'EOF'\nhello\n"), STRUCTURE_UNPARSABLE),
            ("no-repo", dict(command="rg TODO src/", cwd=str(self.root)), NO_REPO),
            (
                "cwd-unknown",
                dict(command="until cd sub; do true; done; printf x > app.py"),
                CWD_UNKNOWN_GAP,
            ),
            (
                "outside",
                dict(command="printf x > %s" % (self.root / "outside" / "a.txt")),
                TARGET_OUTSIDE,
            ),
        )
        (self.root / "outside").mkdir()
        for name, kwargs, message in cases:
            with self.subTest(case=name):
                prepared, warnings = self.prepare("pre_tool", **kwargs)
                self.assertIsNone(prepared)
                self.assertEqual(warnings, [message])
                prepared, warnings = self.prepare("post_tool", **kwargs)
                self.assertIsNone(prepared)
                self.assertEqual(warnings, [])

    def test_multiple_outside_targets_warn_once_per_pre_call(self) -> None:
        (self.root / "outside").mkdir()
        command = "printf x > %s; printf y > %s" % (
            self.root / "outside" / "a.txt",
            self.root / "outside" / "b.txt",
        )
        prepared, warnings = self.prepare("pre_tool", command=command)
        self.assertIsNone(prepared)
        self.assertEqual(warnings.count(TARGET_OUTSIDE), 1, warnings)

class ReviewerStructureWarningTests(HeredocWarningCase):
    """reviewer 的明确写仍是 reviewer_command_write；未知结构告警仅 pre。"""

    def test_reviewer_heredoc_write_is_command_write_deny(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        command = "sh <<'EOF'\ntouch protected\nEOF"
        analysis = shell.analyze(command)
        self.assertTrue(
            [write for write in analysis.writes if write.get("target") == "protected"], analysis.writes
        )
        payload = self.shell("rev", command, tool_id="rev-sh")
        decision = self.decide(payload)
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.code, "reviewer_command_write")
        self.assertIn("明确写副作用", decision.reason)

    def test_reviewer_structured_write_stays_reviewer_write(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        decision = self.decide(self.write_tool("rev", "src/app.py", tool_id="rev-write"))
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.code, "reviewer_write")

    def test_reviewer_unknown_structure_warning_is_pre_only(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        # 未闭合壳 heredoc：正文边界无法证明，未知结构的写风险仍在，按原条件告警放行。
        command = "sh <<'EOF'\nhello\n"
        self.assertTrue(shell.analyze(command).opaque)
        pre = self.shell("rev", command, tool_id="rev-unknown")
        _code, value, pre_stderr = self.run_core(pre)
        self.assertFalse(self.is_denied(value), value)
        # 预锁与锁内各追加一次，run 仍按原文只输出一条。
        self.assertEqual(engine_warnings(pre_stderr).count(REVIEWER_UNKNOWN), 1)
        post = self.shell(
            "rev", command, tool_id="rev-unknown", event="post_tool", outcome="success"
        )
        _code, value, post_stderr = self.run_core(post)
        self.assertFalse(self.is_denied(value), value)
        self.assertNotIn(REVIEWER_UNKNOWN, engine_warnings(post_stderr))


class PreOnlyInformationLossTests(HeredocWarningCase):
    """B3–B5：pre-only 是事件作用域，不是“已经披露过”；三种信息损失逐格断言。

    五类基线结构原句只按事件披露，既不验证 pre 是否运行，也不比较 pre/post 参数；
    因此下列三种情形下具体结构原因都不会被补报，post 自身的记账缺口也不能恢复它。
    """

    def test_missing_pre_suppresses_structure_reason_on_post(self) -> None:
        """B3：不跑 pre，直接提交 opaque 且目标不完整的 post。"""
        self.activate("s1")
        command = "cat > out.txt <<'EOF'\nhello\n"
        self.assertTrue(shell.analyze(command).opaque)
        _code, value, stderr = self.run_core(
            self.shell("s1", command, tool_id="b3-post", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        warnings = engine_warnings(stderr)
        for message in (STRUCTURE_UNPARSABLE, "写入目标不完整，基线检查未覆盖", POST_INCOMPLETE):
            self.assertNotIn(message, warnings)
        # post 自己的记账缺口按原条件保留，但不得据此声称结构原因已披露或可恢复。
        self.assertIn(POST_UNPAIRED, warnings)

    def test_post_only_host_suppresses_structure_reason_every_call(self) -> None:
        """B4：宿主全程只投递 post；每次都不输出结构原因，也没有“已披露”状态。"""
        self.activate("s1")
        for index in range(2):
            command = "cat > out%s.txt <<'EOF'\nhello\n" % index
            with self.subTest(call=index):
                _code, value, stderr = self.run_core(
                    self.shell(
                        "s1",
                        command,
                        tool_id="b4-%d" % index,
                        event="post_tool",
                        outcome="success",
                    )
                )
                self.assertFalse(self.is_denied(value), value)
                warnings = engine_warnings(stderr)
                self.assertNotIn(STRUCTURE_UNPARSABLE, warnings)
                self.assertIn(POST_UNPAIRED, warnings)

    def test_changed_post_parameters_keep_post_only_accounting(self) -> None:
        """B5：同工具 ID 的 pre 可解析，post 换成有结构缺口的参数。"""
        self.activate("s1")
        _code, value, stderr = self.run_core(
            self.shell("s1", "printf x > src/app.py", tool_id="b5")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(engine_warnings(stderr), [])
        self.assertEqual(len(self.pending_entries()), 1)
        post = "cat > out.txt <<'EOF'\nhello\n"
        _code, value, stderr = self.run_core(
            self.shell("s1", post, tool_id="b5", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        warnings = engine_warnings(stderr)
        for message in (STRUCTURE_UNPARSABLE, "写入目标不完整，基线检查未覆盖"):
            self.assertNotIn(message, warnings)
        # post 自身目标/记账缺口按原条件保留，不建立成功豁免。
        self.assertIn(POST_INCOMPLETE, warnings)
        self.assertEqual(self.pending_entries(), [])


class FaultInjectionTests(HeredocWarningCase):
    """状态/Git 故障的既有告警不被结构告警作用域过滤。"""

    def test_unavailable_state_warning_is_kept(self) -> None:
        self.activate("s1")
        files = sorted((self.state / "actors").glob("*.json"))
        self.assertTrue(files)
        for path in files:
            path.write_bytes(b"{not-json")
            os.chmod(path, 0o600)
        _code, value, stderr = self.run_core(self.shell("s1", "rg TODO src/", tool_id="corrupt"))
        self.assertFalse(self.is_denied(value), value)
        self.assertIn("私有状态不可用", stderr)

    def test_fingerprint_timeout_text_drops_repo_relative_path(self) -> None:
        engine = self.engine()
        record = self.record(
            "pre_tool",
            tool={
                "id": "t-timeout",
                "name": "write",
                "kind": "write",
                "args": {"path": "src/app.py"},
                "paths_complete": True,
            },
        )
        prepared = {
            "repo": str(self.repo),
            "entries": [
                {
                    "relative": "secret/dir/app.py",
                    "repo": str(self.repo),
                    "origin_key": "repo::secret/dir/app.py",
                    "fingerprint": "deadline",
                }
            ],
        }
        decision = engine._check_baseline(record, {}, {}, prepared)
        self.assertIsNone(decision)
        self.assertEqual(engine.warnings, ["基线指纹查询超时，基线检查降级"])
        self.assertNotIn("secret/dir/app.py", "；".join(engine.warnings))


if __name__ == "__main__":
    unittest.main()
