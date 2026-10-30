"""写序列记账：没有写目标不等于写目标不完整，非写工具不参与配对。

误报都来自把“没有写目标”当成“写目标不完整”：非写工具的成功 post 也去配对、
纯读 shell 建了空目标的 pending、同一文件两次写让目标集合看起来不一致。反向的
错误同样要钉住：合并不得丢掉已观察到的软链，执行型 stdin 与写 pre 之后的
读/失败 post 仍必须披露缺口。这里只钉可观察结果：stderr 文案、pending 内容、
写序列是否推进。
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any, Dict, List, Set
from unittest import mock

from support import HookCase, core_module
from test_heredoc_warnings import engine_warnings
from test_r5_fixes import task_states

from hooks import shell as shell_module


POST_INCOMPLETE = "post 写入目标不完整，写序列记覆盖缺口"
POST_UNPAIRED = "post 未找到与该 actor/epoch/tool ID 配对的 pre 记录，未建立成功豁免"
POST_FAILED = "post 缺少可信成功结果，未计入成功写入"


class WriteAccountingCase(HookCase):
    host = "pi"

    def task_state(self) -> Dict[str, Any]:
        states = task_states(self.state)
        self.assertEqual(len(states), 1, states)
        return states[0]

    def read_post(self, session: str, outcome: str, tool_id: str) -> str:
        payload = self.tool_payload(
            session,
            "read",
            {"path": "src/app.py"},
            tool_id=tool_id,
            event="post_tool",
            outcome=outcome,
        )
        _code, value, stderr = self.run_core(payload)
        self.assertFalse(self.is_denied(value), value)
        return stderr

    def write_code_twice(self, session: str, tool_id: str) -> List[str]:
        """同一条命令两次写同一文件：pre/post 各一次，返回两次的 stderr。"""
        command = "printf a > src/app.py; printf b >> src/app.py"
        _code, value, pre_stderr = self.run_core(self.shell(session, command, tool_id=tool_id))
        self.assertFalse(self.is_denied(value), value)
        (self.repo / "src" / "app.py").write_text("ab\n", encoding="utf-8")
        _code, value, post_stderr = self.run_core(
            self.shell(session, command, tool_id=tool_id, event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        return [pre_stderr, post_stderr]


class NonWriteToolTests(WriteAccountingCase):
    """非写工具的 post 既不建 pending，也不做配对。"""

    def test_read_tool_post_is_silent(self) -> None:
        self.activate("s1")
        self.assertEqual(self.read_post("s1", "success", "read-ok"), "")
        self.assertEqual(self.read_post("s1", "error", "read-fail"), "")
        task = self.task_state()
        self.assertEqual(task.get("pending_writes") or {}, {})
        self.assertEqual(int(task.get("write_seq") or 0), 0)


class ReadOnlyShellTests(WriteAccountingCase):
    """纯读 shell 没有写目标可记账：pre 不建 pending，post 不配对。"""

    def test_readonly_shell_pre_post_are_silent_and_not_pending(self) -> None:
        self.activate("s1")
        _code, value, pre_stderr = self.run_core(self.shell("s1", "git status", tool_id="status-1"))
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(pre_stderr, "")
        self.assertEqual(self.task_state().get("pending_writes") or {}, {})
        _code, value, post_stderr = self.run_core(
            self.shell("s1", "git status", tool_id="status-1", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(post_stderr, "")
        self.assertEqual(int(self.task_state().get("write_seq") or 0), 0)
        # 宿主只投递 post（没有 pre）时同样不配对、不告警。
        _code, value, post_only_stderr = self.run_core(
            self.shell("s1", "rg TODO src/", tool_id="status-2", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(post_only_stderr, "")
        self.assertEqual(int(self.task_state().get("write_seq") or 0), 0)


class DuplicateTargetTests(WriteAccountingCase):
    """同一文件多次写只算一个目标：配对一致，写序列只推进一次。"""

    def test_same_file_written_twice_advances_sequence_once(self) -> None:
        self.activate("s1")
        pre_stderr, post_stderr = self.write_code_twice("s1", "dup-1")
        self.assertEqual(pre_stderr, "")
        self.assertEqual(post_stderr, "")
        task = self.task_state()
        self.assertEqual(int(task.get("write_seq") or 0), 1)
        self.assertEqual(int(task.get("last_code_write_seq") or 0), 1)
        last_success = task.get("last_success") or {}
        self.assertEqual(
            sorted(key.rsplit("::", 1)[-1] for key in last_success), ["src/app.py"], last_success
        )
        self.assertEqual(task.get("pending_writes") or {}, {})


class GapDisclosureTests(WriteAccountingCase):
    """真正的目标集合缺口仍按原文披露：放行不等于静默。"""

    def test_genuine_gaps_still_disclosed(self) -> None:
        self.activate("s1")
        opaque = "cat > out.txt <<'EOF'\nhello\n"
        # (a) 宿主只投递 post：没有 pre 配对记录，仍是记账缺口。
        _code, value, stderr = self.run_core(
            self.shell("s1", opaque, tool_id="gap-post-only", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertIn(POST_UNPAIRED, engine_warnings(stderr))
        # (b) pre 目标完整、post 换成 opaque 参数：目标集合无法完整建立，不建立成功豁免。
        _code, value, pre_stderr = self.run_core(
            self.shell("s1", "printf x > src/app.py", tool_id="gap-swap")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(pre_stderr, "")
        _code, value, post_stderr = self.run_core(
            self.shell("s1", opaque, tool_id="gap-swap", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertIn(POST_INCOMPLETE, engine_warnings(post_stderr))
        task = self.task_state()
        self.assertEqual(int(task.get("write_seq") or 0), 0)
        self.assertEqual(task.get("pending_writes") or {}, {})


class RoleOnlyWriteSequenceTests(WriteAccountingCase):
    """无父关联的 reviewer 只有 role_only：不借用父任务的写序列，也不刷记账告警。"""

    def test_role_only_reviewer_readonly_shell_is_silent(self) -> None:
        self.activate("rev", self.reviewer_handoff())
        _code, value, pre_stderr = self.run_core(self.shell("rev", "git status", tool_id="rev-1"))
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(pre_stderr, "")
        _code, value, post_stderr = self.run_core(
            self.shell("rev", "git status", tool_id="rev-1", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(post_stderr, "")
        # reviewer 只读的写拒绝不变，且仍是一行 deny。
        self.deny(self.write_tool("rev", "src/app.py", tool_id="rev-write"))


class MergeWriteEntriesTests(unittest.TestCase):
    """同一目标的多次预取合并：软链不得被普通条目盖掉，指纹不一致要标不稳定。"""

    def entry(self, fingerprint: str = "clean", symlink: str = "") -> Dict[str, Any]:
        return {
            "relative": "src/app.py",
            "repo": "/repo",
            "origin_key": "repo::src/app.py",
            "symlink": symlink,
            "fingerprint": "symlink" if symlink else fingerprint,
            "origin": "",
        }

    def test_regular_then_symlink_keeps_symlink(self) -> None:
        entries, unstable = core_module._merge_write_entries(
            [self.entry("clean"), self.entry(symlink="src/app.py")]
        )
        self.assertEqual(len(entries), 1, entries)
        self.assertTrue(entries[0].get("symlink"), entries)
        self.assertFalse(unstable)

    def test_different_prefetch_fingerprints_are_unstable(self) -> None:
        entries, unstable = core_module._merge_write_entries(
            [self.entry("clean"), self.entry("dirty")]
        )
        self.assertEqual(len(entries), 1, entries)
        self.assertTrue(unstable)
        self.assertTrue(entries[0].get("unstable"), entries)

    def test_unsafe_fingerprint_wins_over_clean_first(self) -> None:
        """后一次预取发现目标不是普通文件/不可读时，不能被先到的干净条目盖掉。"""
        for unsafe in ("nonregular", "toolarge", "unreadable"):
            with self.subTest(fingerprint=unsafe):
                entries, unstable = core_module._merge_write_entries(
                    [self.entry("clean"), self.entry(unsafe)]
                )
                self.assertEqual(len(entries), 1, entries)
                self.assertEqual(entries[0]["fingerprint"], unsafe)
                self.assertTrue(unstable)
                self.assertTrue(entries[0].get("unstable"), entries)

    def test_deadline_prefetch_is_not_a_state_change(self) -> None:
        """共享预算耗尽的 `deadline` 是取证降级，不是观察到的状态变化。"""
        for first, second in (("aaa", "deadline"), ("deadline", "aaa")):
            with self.subTest(order=(first, second)):
                entries, unstable = core_module._merge_write_entries(
                    [self.entry(first), self.entry(second)]
                )
                self.assertEqual(len(entries), 1, entries)
                self.assertEqual(entries[0]["fingerprint"], "aaa")
                self.assertTrue(entries[0].get("deadline"), entries)
                self.assertFalse(unstable)

    def test_deadline_between_two_states_is_unstable(self) -> None:
        entries, unstable = core_module._merge_write_entries(
            [self.entry("aaa"), self.entry("deadline"), self.entry("bbb")]
        )
        self.assertEqual(len(entries), 1, entries)
        self.assertTrue(entries[0].get("deadline"), entries)
        self.assertTrue(unstable)
        self.assertTrue(entries[0].get("unstable"), entries)

    def test_same_fingerprint_twice_is_stable(self) -> None:
        entries, unstable = core_module._merge_write_entries(
            [self.entry("clean"), self.entry("clean")]
        )
        self.assertEqual(len(entries), 1, entries)
        self.assertFalse(unstable)


class SymlinkMergeTests(WriteAccountingCase):
    """同一命令两次写同一目标：已观察到的软链必须仍被拒绝。"""

    COMMAND = "printf a > src/app.py; printf b >> src/app.py"

    def link_app_to_guide(self) -> Path:
        guide = self.repo / "docs" / "guide.md"
        app = self.repo / "src" / "app.py"
        app.unlink()
        os.symlink(guide, str(app))
        return guide

    def assert_denied_and_guide_untouched(self, reason: str, guide: Path, before: str) -> None:
        self.assertIn("符号链接", reason)
        self.assertEqual(guide.read_text(encoding="utf-8"), before)
        self.assertEqual(int(self.task_state().get("write_seq") or 0), 0)

    def test_symlinked_duplicate_target_is_denied(self) -> None:
        self.activate("s1")
        guide = self.link_app_to_guide()
        before = guide.read_text(encoding="utf-8")
        reason = self.deny(self.shell("s1", self.COMMAND, tool_id="dup-link"))
        self.assert_denied_and_guide_untouched(reason, guide, before)

    def test_symlink_swapped_between_prefetches_is_denied(self) -> None:
        """确定性交错：首次预取完整分类之后、第二次软链核对之前换成软链。"""
        self.activate("s1")
        guide = self.repo / "docs" / "guide.md"
        before = guide.read_text(encoding="utf-8")
        app = self.repo / "src" / "app.py"
        real_classify = core_module.paths.classify
        calls: Dict[str, int] = {"classify": 0}

        def classify_then_swap(root: str, relative: str, deadline=None) -> str:
            calls["classify"] += 1
            origin = real_classify(root, relative, deadline=deadline)
            if calls["classify"] == 1:
                # 第一条目已取证完成（普通、干净、无需快照），此时才换成软链。
                app.unlink()
                os.symlink(guide, str(app))
            return origin

        with mock.patch.object(core_module.paths, "classify", classify_then_swap):
            reason = self.deny(self.shell("s1", self.COMMAND, tool_id="dup-swap"))
        self.assert_denied_and_guide_untouched(reason, guide, before)


class UnstablePrefetchTests(WriteAccountingCase):
    """同一次分析里预取指纹变了：pre 必须拒绝，不能拿旧成功指纹冒充满足。"""

    COMMAND = "printf a > src/app.py; printf b >> src/app.py"

    def test_prefetch_change_after_first_classify_is_denied(self) -> None:
        self.activate("s1")
        # 先建立一次真实写入：last_success = 指纹 A，write_seq = 1。
        self.write_path("s1", "base", "src/app.py", "value = 5\n")
        self.assertEqual(int(self.task_state().get("write_seq") or 0), 1)
        app = self.repo / "src" / "app.py"
        real_classify = core_module.paths.classify
        calls: Dict[str, int] = {"classify": 0}

        def classify_then_modify(root: str, relative: str, deadline=None) -> str:
            calls["classify"] += 1
            origin = real_classify(root, relative, deadline=deadline)
            if calls["classify"] == 1:
                # 第一条目已取证（指纹 A、成功指纹同样为 A），此时外部改成 B。
                app.write_text("value = 2\n", encoding="utf-8")
            return origin

        with mock.patch.object(core_module.paths, "classify", classify_then_modify):
            reason = self.deny(self.shell("s1", self.COMMAND, tool_id="dup-unstable"))
        self.assertIn("预取指纹", reason)
        self.assertIn("同一次分析期间", reason)
        # 命令没有执行：外部内容仍是 B，写序列与 pending 都不推进。
        self.assertEqual(app.read_text(encoding="utf-8"), "value = 2\n")
        task = self.task_state()
        self.assertEqual(int(task.get("write_seq") or 0), 1)
        self.assertEqual(task.get("pending_writes") or {}, {})


class PrefetchDeadlineTests(WriteAccountingCase):
    """指纹取证超时不是并发修改：放行并降级告警，不 pre 拒绝。"""

    COMMAND = "printf a > src/app.py; printf b >> src/app.py"

    def fingerprint_deadline_on(self, calls: Set[int], on_deadline: Any = None):
        """第 N 次指纹查询返回 `deadline`（可附带一次外部改写）。"""
        real = core_module.paths.fingerprint
        counter: Dict[str, int] = {"n": 0}

        def fingerprint(path: str, deadline: float = None) -> str:
            counter["n"] += 1
            if counter["n"] in calls:
                if on_deadline is not None:
                    on_deadline()
                return "deadline"
            return real(path, deadline=deadline)

        return mock.patch.object(core_module.paths, "fingerprint", fingerprint)

    def test_deadline_prefetch_allows_with_warning(self) -> None:
        self.activate("s1")
        self.write_path("s1", "base", "src/app.py", "value = 5\n")
        self.assertEqual(int(self.task_state().get("write_seq") or 0), 1)
        with self.fingerprint_deadline_on({2}):
            _code, value, pre_stderr = self.run_core(
                self.shell("s1", self.COMMAND, tool_id="dl-1")
            )
        self.assertFalse(self.is_denied(value), value)
        self.assertIn("基线指纹查询超时，基线检查降级", engine_warnings(pre_stderr))
        entries = list((self.task_state().get("pending_writes") or {}).values())
        self.assertEqual(len(entries), 1, entries)
        self.assertFalse(entries[0]["complete"])
        _code, value, post_stderr = self.run_core(
            self.shell("s1", self.COMMAND, tool_id="dl-1", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        # 超时仍算目标集不完整：成功 post 不建立成功豁免。
        self.assertIn(POST_INCOMPLETE, engine_warnings(post_stderr))
        task = self.task_state()
        self.assertEqual(int(task.get("write_seq") or 0), 1)
        self.assertEqual(task.get("pending_writes") or {}, {})

    def test_deadline_then_change_is_still_denied(self) -> None:
        self.activate("s1")
        self.write_path("s1", "base", "src/app.py", "value = 5\n")
        app = self.repo / "src" / "app.py"
        command = "printf a > src/app.py; printf b >> src/app.py; printf c >> src/app.py"
        with self.fingerprint_deadline_on(
            {2}, on_deadline=lambda: app.write_text("value = 2\n", encoding="utf-8")
        ):
            reason = self.deny(self.shell("s1", command, tool_id="dl-2"))
        self.assertIn("预取指纹", reason)
        self.assertEqual(app.read_text(encoding="utf-8"), "value = 2\n")
        task = self.task_state()
        self.assertEqual(int(task.get("write_seq") or 0), 1)
        self.assertEqual(task.get("pending_writes") or {}, {})


class WrappedStdinTests(WriteAccountingCase):
    """包装器/管道下的执行型 stdin 正文同样未被解析：不得当纯读。"""

    def assert_gap_pre_and_post(self, command: str, tool_id: str) -> None:
        _code, value, pre_stderr = self.run_core(self.shell("s1", command, tool_id=tool_id))
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(pre_stderr, "")
        entries = list((self.task_state().get("pending_writes") or {}).values())
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(entries[0]["targets"], [])
        self.assertFalse(entries[0]["complete"])
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        _code, value, post_stderr = self.run_core(
            self.shell("s1", command, tool_id=tool_id, event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertIn(POST_INCOMPLETE, engine_warnings(post_stderr))
        task = self.task_state()
        self.assertEqual(int(task.get("write_seq") or 0), 0)
        self.assertEqual(task.get("pending_writes") or {}, {})

    def test_shell_dash_c_consumer_keeps_gap(self) -> None:
        self.activate("s1")
        self.assert_gap_pre_and_post(
            "bash -c 'python3 -' <<'PY'\nimport writer\nPY", "wrap-c"
        )

    def test_pipeline_consumer_keeps_gap(self) -> None:
        self.activate("s1")
        for index, command in enumerate(
            (
                "cat <<'EOF' | python3 -\nprint(1)\nEOF",
                "cat <<'EOF' | bash -s\nprint(1)\nEOF",
            )
        ):
            with self.subTest(command=command):
                self.assert_gap_pre_and_post(command, "wrap-pipe-%d" % index)

    def test_shell_script_file_argument_keeps_gap(self) -> None:
        """壳从脚本文件读脚本时，heredoc 正文只是子进程 stdin：写目标仍不可枚举。"""
        self.activate("s1")
        for index, command in enumerate(
            (
                "bash consumer.sh <<'PY'\nimport writer\nPY",
                "env bash consumer.sh <<'PY'\nimport writer\nPY",
                "sudo bash consumer.sh <<'PY'\nimport writer\nPY",
                # 脚本名之后的 `-s` 是脚本的 $1，不是壳选项。
                "bash consumer.sh -s <<'PY'\nimport writer\nPY",
                # 单个 `-` 是选项终止：后面的 `consumer.sh` 才是脚本。
                "bash - consumer.sh <<'PY'\nimport writer\nPY",
                # `--rcfile` 的取值以 `-` 开头，不能被当选项扫描。
                "bash --rcfile -s consumer.sh <<'PY'\nimport writer\nPY",
            )
        ):
            with self.subTest(command=command.splitlines()[0]):
                self.assert_gap_pre_and_post(command, "script-arg-%d" % index)

    def test_stdin_unparsed_marks_only_unproven_bodies(self) -> None:
        cases = (
            ("sh <<'EOF'\necho hi\nEOF", False),
            ("bash <<'EOF'\necho hi\nEOF", False),
            ("sudo bash <<'EOF'\necho hi\nEOF", False),
            ("env bash <<'EOF'\necho hi\nEOF", False),
            ("bash -s <<'EOF'\necho hi\nEOF", False),
            ("bash -s consumer.sh <<'EOF'\necho hi\nEOF", False),
            ("bash -x <<'EOF'\necho hi\nEOF", False),
            ("bash -- <<'EOF'\necho hi\nEOF", False),
            ("bash -s -- consumer.sh <<'EOF'\necho hi\nEOF", False),
            ("bash -s -O extglob <<'EOF'\necho hi\nEOF", False),
            ("bash -O extglob <<'EOF'\necho hi\nEOF", False),
            ("bash -o pipefail <<'EOF'\necho hi\nEOF", False),
            ("bash - <<'EOF'\necho hi\nEOF", False),
            ("bash --noprofile <<'EOF'\necho hi\nEOF", False),
            ("bash -Os <<'EOF'\necho hi\nEOF", False),
            ("cat <<'EOF'\nhello\nEOF", False),
            ("bash consumer.sh <<'EOF'\necho hi\nEOF", True),
            ("env bash consumer.sh <<'EOF'\necho hi\nEOF", True),
            ("sudo bash consumer.sh <<'EOF'\necho hi\nEOF", True),
            # 脚本文件之后的 `-s` 是脚本参数；`--`/`-` 之后的 `-s` 是操作数。
            ("bash consumer.sh -s <<'EOF'\necho hi\nEOF", True),
            ("bash -- -s <<'EOF'\necho hi\nEOF", True),
            ("bash - consumer.sh <<'EOF'\necho hi\nEOF", True),
            ("bash --rcfile -s consumer.sh <<'EOF'\necho hi\nEOF", True),
            ("bash --rcfile=-s consumer.sh <<'EOF'\necho hi\nEOF", True),
            ("bash -O extglob consumer.sh <<'EOF'\necho hi\nEOF", True),
            ("bash -o pipefail consumer.sh <<'EOF'\necho hi\nEOF", True),
            ("python3 - <<'PY'\nprint(1)\nPY", True),
            ("bash -c 'python3 -' <<'PY'\nimport writer\nPY", True),
            ("cat <<'EOF' | python3 -\nprint(1)\nEOF", True),
            ("sh <<< 'echo hi'", True),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(shell_module.analyze(command).stdin_unparsed, expected)


class InterpreterStdinTests(WriteAccountingCase):
    """执行型 stdin 没有枚举出写目标：不得当纯读静默，也不得误伤已解析的壳正文。"""

    PY_STDIN = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        'Path("src/app.py").write_text("value = 2\\n")\n'
        "PY"
    )

    def pending_entries(self) -> List[Dict[str, Any]]:
        return list((self.task_state().get("pending_writes") or {}).values())

    def test_interpreter_stdin_write_keeps_gap(self) -> None:
        self.activate("s1")
        _code, value, pre_stderr = self.run_core(
            self.shell("s1", self.PY_STDIN, tool_id="py-stdin")
        )
        self.assertFalse(self.is_denied(value), value)
        # pre 不新增结构告警，但必须留下不完整的写意图。
        self.assertEqual(pre_stderr, "")
        entries = self.pending_entries()
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(entries[0]["targets"], [])
        self.assertFalse(entries[0]["complete"])
        # 正文真实改了代码文件，但目标集不可证明：成功 post 也不建立豁免。
        (self.repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
        _code, value, post_stderr = self.run_core(
            self.shell("s1", self.PY_STDIN, tool_id="py-stdin", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertIn(POST_INCOMPLETE, engine_warnings(post_stderr))
        task = self.task_state()
        self.assertEqual(int(task.get("write_seq") or 0), 0)
        self.assertEqual(task.get("pending_writes") or {}, {})

    def test_shell_heredoc_script_body_stays_silent(self) -> None:
        self.activate("s1")
        command = "sh <<'EOF'\necho hi\nEOF"
        _code, value, pre_stderr = self.run_core(self.shell("s1", command, tool_id="sh-body"))
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(pre_stderr, "")
        self.assertEqual(self.pending_entries(), [])
        _code, value, post_stderr = self.run_core(
            self.shell("s1", command, tool_id="sh-body", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(post_stderr, "")
        self.assertEqual(int(self.task_state().get("write_seq") or 0), 0)

    def test_herestring_stdin_keeps_gap(self) -> None:
        self.activate("s1")
        command = "sh <<< 'echo hi'"
        _code, value, pre_stderr = self.run_core(self.shell("s1", command, tool_id="hs"))
        self.assertFalse(self.is_denied(value), value)
        self.assertEqual(pre_stderr, "")
        entries = self.pending_entries()
        self.assertEqual(len(entries), 1, entries)
        self.assertFalse(entries[0]["complete"])
        _code, value, post_stderr = self.run_core(
            self.shell("s1", command, tool_id="hs", event="post_tool", outcome="success")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertIn(POST_INCOMPLETE, engine_warnings(post_stderr))
        self.assertEqual(int(self.task_state().get("write_seq") or 0), 0)


class CrossToolPostTests(WriteAccountingCase):
    """写 pre 之后换成读/失败的 post：必须消费 pending 并披露，不能静默。"""

    def test_write_pre_then_read_post_is_disclosed(self) -> None:
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w-read"))
        self.assertEqual(len(self.task_state().get("pending_writes") or {}), 1)
        post = self.tool_payload(
            "s1",
            "read",
            {"path": "src/app.py"},
            tool_id="w-read",
            event="post_tool",
            outcome="success",
        )
        _code, value, stderr = self.run_core(post)
        self.assertFalse(self.is_denied(value), value)
        self.assertIn(POST_INCOMPLETE, engine_warnings(stderr))
        task = self.task_state()
        self.assertEqual(task.get("pending_writes") or {}, {})
        self.assertEqual(int(task.get("write_seq") or 0), 0)

    def test_write_pre_then_failed_readonly_post_is_disclosed(self) -> None:
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w-fail"))
        _code, value, stderr = self.run_core(
            self.shell("s1", "git status", tool_id="w-fail", event="post_tool", outcome="error")
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertIn(POST_FAILED, engine_warnings(stderr))
        task = self.task_state()
        self.assertEqual(task.get("pending_writes") or {}, {})
        self.assertEqual(int(task.get("write_seq") or 0), 0)

    def test_write_pre_then_failed_write_post_is_disclosed(self) -> None:
        self.activate("s1")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w-fail-2"))
        _code, value, stderr = self.run_core(
            self.write_tool(
                "s1", "src/app.py", tool_id="w-fail-2", event="post_tool", outcome="error"
            )
        )
        self.assertFalse(self.is_denied(value), value)
        self.assertIn(POST_FAILED, engine_warnings(stderr))
        self.assertEqual(self.task_state().get("pending_writes") or {}, {})


class UnanalyzedPrefetchTests(HookCase):
    """没有 prepared 的异常入口必须按不完整记账，不能算成完整空集合。"""

    host = "pi"

    def test_missing_prepared_prefetch_is_incomplete(self) -> None:
        context = json.loads(self.context_path.read_text(encoding="utf-8"))
        engine = core_module.Engine(self.host, context, mode="enforce", env=self.environment())
        record = core_module.Record(self.host, {}, core_module.HOSTS[self.host])
        record.event = "pre_tool"
        record.session_id = "s1"
        record.cwd = str(self.repo)
        record.activation_scope = "task_active"
        record.tool = {
            "id": "t-1",
            "name": "bash",
            "kind": "shell",
            "args": {"command": "printf x > src/app.py"},
            "paths_complete": True,
        }
        task: Dict[str, Any] = {}
        engine._record_pre_write(record, task, None)
        entries = list((task.get("pending_writes") or {}).values())
        self.assertEqual(len(entries), 1, entries)
        self.assertFalse(entries[0]["complete"])
        self.assertEqual(entries[0]["targets"], [])


if __name__ == "__main__":
    unittest.main()
