"""D：dirty/untracked 目标的首次内容由 Hook 自动快照到私有状态根。

快照是私有缓存，不是业务备份：不写业务文件、不进 task 锁、不覆盖首次内容。
显式 `task_root` 仍走手工 baseline 核对路径（兼容旧协议）。
"""

from __future__ import annotations

import pytest

import json
import os
import stat
from pathlib import Path

from support import CaseBase, HookCase
from test_r5_fixes import task_states


class BaselineAutoSnapshotMixin(object):
    def auto_context(self) -> None:
        """关闭手工任务根：走未显式配置时的自动快照路径。"""
        self.write_context(task_root="")

    def snapshots(self, relative: str) -> list:
        return sorted((self.state / "baselines").glob("*/*/" + relative))

    def activate_auto(self, session: str = "s1") -> None:
        self.auto_context()
        self.activate(session)

    def test_first_dirty_write_saves_original_content(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        self.activate_auto()
        value = self.allow(self.write_tool("s1", "src/app.py", tool_id="w1"))
        assert not self.is_denied(value), value
        saved = self.snapshots("src/app.py")
        assert len(saved) == 1, saved
        snapshot = saved[0]
        assert snapshot.read_text(encoding="utf-8") == "value = 2\n"
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
        assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
        # 私有状态根之外不产生业务文件改动。
        assert (self.repo / "src" / "app.py").read_text(encoding="utf-8") == "value = 2\n"

    def test_untracked_write_saves_first_content(self) -> None:
        (self.repo / "notes.txt").write_text("user notes\n", encoding="utf-8")
        self.activate_auto()
        self.allow(self.write_tool("s1", "notes.txt", tool_id="n1"))
        saved = self.snapshots("notes.txt")
        assert len(saved) == 1, saved
        assert saved[0].read_text(encoding="utf-8") == "user notes\n"

    def test_second_write_does_not_overwrite_first_snapshot(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        self.activate_auto()
        self.write_path("s1", "w1", content="value = 3\n")
        # 第二次写目标内容与上次成功指纹一致（未被外部修改），仍不得覆盖首次快照。
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w2"))
        saved = self.snapshots("src/app.py")
        assert len(saved) == 1, saved
        assert saved[0].read_text(encoding="utf-8") == "value = 2\n"

    def test_clean_and_absent_targets_do_not_create_snapshots(self) -> None:
        self.activate_auto()
        self.allow(self.write_tool("s1", "src/app.py", tool_id="c1"))
        self.allow(self.write_tool("s1", "src/new_mod.py", tool_id="a1"))
        baselines = self.state / "baselines"
        assert not (baselines.exists() and any(baselines.rglob("*.*")))

    def test_external_modification_after_success_is_still_denied(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        self.activate_auto()
        self.write_path("s1", "w1", content="value = 3\n")
        (self.repo / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="w2"))
        assert "外部修改" in reason

    def test_multi_file_patch_snapshots_every_target(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        (self.repo / "docs" / "guide.md").write_text("user guide\n", encoding="utf-8")
        self.activate_auto()
        patch = (
            "*** Begin Patch\n"
            "*** Update File: src/app.py\n@@\n-value = 2\n+value = 3\n"
            "*** Update File: docs/guide.md\n@@\n-user guide\n+guide\n"
            "*** End Patch\n"
        )
        payload = self.tool_payload("s1", "apply_patch", {"input": patch}, tool_id="p1")
        self.allow(payload)
        assert len(self.snapshots("src/app.py")) == 1
        assert len(self.snapshots("docs/guide.md")) == 1
        assert self.snapshots("src/app.py")[0].read_text(encoding="utf-8") == "value = 2\n"
        assert self.snapshots("docs/guide.md")[0].read_text(encoding="utf-8") == "user guide\n"

    def test_unified_patch_snapshots_dirty_real_path(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        self.activate_auto()
        patch = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-value = 2\n"
            "+value = 3\n"
        )
        payload = self.tool_payload("s1", "apply_patch", {"input": patch}, tool_id="u1")
        self.allow(payload)
        # 首次写保存真实路径的原字节快照，不产生 `a/src`/`b/src` 伪目标。
        saved = self.snapshots("src/app.py")
        assert len(saved) == 1, saved
        assert saved[0].read_text(encoding="utf-8") == "value = 2\n"
        assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600
        keys = self.registered_targets()
        assert [key for key in keys if key.endswith("::src/app.py")], keys
        assert not [key for key in keys if "::a/" in key or "::b/" in key], keys
        # 模拟补丁应用成功后再被外部修改：真实 origin 已绑定，仍拒绝。
        (self.repo / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
        self.allow(
            self.tool_payload(
                "s1", "apply_patch", {"input": patch}, tool_id="u1", event="post_tool", outcome="success"
            )
        )
        (self.repo / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="u2"))
        assert "外部修改" in reason

    def test_manual_task_root_still_requires_baseline(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        task_root = self.make_task_root("manual")
        self.write_context(task_root=str(task_root))
        self.activate()
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="m1"))
        assert "缺少合规首次基线" in reason
        # 手工根提供合规备份后放行，且不产生自动快照。
        self.save_baseline(task_root, "src/app.py", "value = 2\n")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="m2"))
        assert not (self.state / "baselines").exists()

    def test_substitution_redirect_target_is_incomplete_not_literal_path(self) -> None:
        self.activate_auto()
        _code, value, stderr = self.run_core(
            self.shell("s1", "printf x >$(printf out)", tool_id="dyn-target")
        )
        assert not self.is_denied(value), value
        assert "命令内目录变化无法确定" in stderr
        # 动态替换输出不当源码字面路径登记：没有 `$(` 或 `out` 名字的快照/目标。
        keys = self.registered_targets()
        assert not [key for key in keys if "$(" in key or key.endswith("::out")], keys
        baselines = self.state / "baselines"
        names = [path.name for path in baselines.rglob("*")] if baselines.exists() else []
        assert not [name for name in names if "$(" in name or name == "out"], names

    def test_until_condition_cd_never_registers_absent_root_target(self) -> None:
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "app.py").write_text("USER-SUB\n", encoding="utf-8")
        assert not (self.repo / "app.py").exists()
        self.activate_auto()
        _code, value, stderr = self.run_core(
            self.shell("s1", "until cd sub; do true; done; printf x > app.py", tool_id="until-cd")
        )
        assert not self.is_denied(value), value
        assert "命令内目录变化无法确定" in stderr
        # 不能把仓库根 app.py 登记成 absent origin/auto_baseline/成功目标/pending 目标。
        keys = self.registered_targets()
        assert not [key for key in keys if key.endswith("::app.py")], keys
        assert not self.snapshots("app.py")
        # 真实目标独立写死为 sub/app.py，业务字节不变。
        assert (sub / "app.py").read_text(encoding="utf-8") == "USER-SUB\n"

    def test_unknown_cwd_substitution_write_does_not_register_root_target(self) -> None:
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "app.py").write_text("USER-SUB\n", encoding="utf-8")
        self.activate_auto()
        command = "until cd sub; do true; done; printf '%s' \"$(printf x > app.py)\""
        _code, value, stderr = self.run_core(self.shell("s1", command, tool_id="until-sub"))
        assert not self.is_denied(value), value
        assert "命令内目录变化无法确定" in stderr
        keys = self.registered_targets()
        assert not [key for key in keys if key.endswith("::app.py")], keys
        assert not self.snapshots("app.py")
        assert not (self.repo / "app.py").exists()
        assert (sub / "app.py").read_text(encoding="utf-8") == "USER-SUB\n"

    def registered_targets(self) -> list:
        """任务状态里登记过的 origin/成功/自动快照/pending 目标键。"""
        keys = []
        for task in task_states(self.state):
            keys.extend((task.get("origins") or {}).keys())
            keys.extend((task.get("last_success") or {}).keys())
            keys.extend((task.get("auto_baseline") or {}).keys())
            for entry in (task.get("pending_writes") or {}).values():
                keys.extend(entry.get("targets") or [])
        return keys

    def test_oversized_target_is_denied(self) -> None:
        big = self.repo / "src" / "big.py"
        big.write_bytes(b"x" * (9 * 1024 * 1024))
        self.activate_auto()
        reason = self.deny(self.write_tool("s1", "src/big.py", tool_id="big1"))
        assert "上限" in reason
        assert not self.snapshots("src/big.py")

    def test_unreadable_target_is_denied(self) -> None:
        target = self.repo / "src" / "locked.py"
        target.write_text("secret\n", encoding="utf-8")
        os.chmod(target, 0o000)
        self.defer(os.chmod, target, 0o600)
        self.activate_auto()
        reason = self.deny(self.write_tool("s1", "src/locked.py", tool_id="lock1"))
        assert "普通文件" in reason
        assert not self.snapshots("src/locked.py")

    def test_fifo_target_is_denied(self) -> None:
        fifo = self.repo / "src" / "pipe.py"
        os.mkfifo(fifo)
        self.activate_auto()
        reason = self.deny(self.write_tool("s1", "src/pipe.py", tool_id="f1"))
        assert "普通文件" in reason

    def test_sort_output_snapshots_tracked_dirty_file(self) -> None:
        """`sort -o <已跟踪且 dirty 的文件>`：tracked-dirty 分类下也按原字节首写快照。"""
        target = self.repo / "src" / "app.py"
        assert self.git("status", "--porcelain", "--", "src/app.py") == ""
        target.write_text("value = 2\n", encoding="utf-8")
        # `HookCase.git()` 会 strip 输出：未暂存的修改显示为 `M <path>`。
        assert self.git("status", "--porcelain", "--", "src/app.py") == "M src/app.py"
        self.activate_auto()
        payload = self.shell("s1", "sort -o src/app.py src/app.py", tool_id="sort-tracked")
        self.allow(payload)
        saved = self.snapshots("src/app.py")
        assert len(saved) == 1, saved
        assert saved[0].read_bytes() == b"value = 2\n"
        # 业务文件本身没被 Hook 改动（快照只是私有缓存）。
        assert target.read_bytes() == b"value = 2\n"

    def test_unicode_separator_patch_still_snapshots_original_bytes(self) -> None:
        """hunk 正文含 U+2028 时目标仍可证明：首写必须快照原字节，不能按“不完整”跳过。"""
        original = "a\u2028b\nUSER\n"
        (self.repo / "src" / "app.py").write_text(original, encoding="utf-8")
        self.activate_auto()
        patch = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-a\u2028b\n"
            "+c\n"
        )
        payload = self.tool_payload("s1", "apply_patch", {"input": patch}, tool_id="uni-patch")
        self.allow(payload)
        saved = self.snapshots("src/app.py")
        assert len(saved) == 1, saved
        assert saved[0].read_bytes() == original.encode("utf-8")

    def test_compound_write_snapshots_real_target(self) -> None:
        """复合命令（`cd` + 条件）里的真实写目标仍按原字节快照。"""
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "sub-app")
        (sub / "app.py").write_text("USER SUB\n", encoding="utf-8")
        self.activate_auto()
        payload = self.shell("s1", "cd sub && printf x >> app.py", tool_id="compound-w")
        self.allow(payload)
        saved = self.snapshots("sub/app.py")
        assert len(saved) == 1, saved
        assert saved[0].read_text(encoding="utf-8") == "USER SUB\n"
        assert (sub / "app.py").read_text(encoding="utf-8") == "USER SUB\n"

    def test_sort_output_snapshots_existing_file(self) -> None:
        """`sort -o <dirty 文件>` 的写目标也按镜像快照规则保护。"""
        target = self.repo / "sorted.txt"
        target.write_text("USER SORTED\n", encoding="utf-8")
        self.activate_auto()
        payload = self.shell("s1", "sort -o sorted.txt src/app.py", tool_id="sort-w")
        self.allow(payload)
        saved = self.snapshots("sorted.txt")
        assert len(saved) == 1, saved
        assert saved[0].read_text(encoding="utf-8") == "USER SORTED\n"
        assert target.read_text(encoding="utf-8") == "USER SORTED\n"

    def test_inactive_session_does_not_write_snapshots(self) -> None:
        self.auto_context()
        self.touch_dirty("src/app.py", "value = 2\n")
        self.allow(self.write_tool("other", "src/app.py", tool_id="i1"))
        assert not (self.state / "baselines").exists()


class PiBaselineAutoTests(BaselineAutoSnapshotMixin, HookCase):
    host = "pi"


class AntigravityBaselineAutoTests(BaselineAutoSnapshotMixin, HookCase):
    host = "antigravity"


class CodexBaselineAutoTests(BaselineAutoSnapshotMixin, HookCase):
    host = "codex"


class DshBaselineAutoTests(BaselineAutoSnapshotMixin, HookCase):
    host = "dsh"


class SnapshotStateTests(CaseBase):
    """快照写盘本身：不覆盖、拒绝软链/非普通文件、目录 0700。"""

    def setup_method(self) -> None:
        import sys
        import tempfile

        from support import HOOKS_ROOT

        if str(HOOKS_ROOT) not in sys.path:
            sys.path.insert(0, str(HOOKS_ROOT))
        from hooks import state

        self.state = state
        self.tmp = tempfile.TemporaryDirectory()
        self.defer(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "state"
        self.base.mkdir(mode=0o700)
        os.chmod(self.base, 0o700)
        self.source = self.root / "source.py"
        self.source.write_text("first\n", encoding="utf-8")

    def save(self, relative: str = "src/app.py", source: Path | None = None, limit: int = 1024):
        directory = self.base / "baselines" / "task" / "repo"
        return self.state.save_snapshot(
            str(directory), relative, str(source or self.source), limit
        )

    def test_saves_first_content_with_private_modes(self) -> None:
        ok, reason = self.save()
        assert ok, reason
        target = self.base / "baselines" / "task" / "repo" / "src" / "app.py"
        assert target.read_text(encoding="utf-8") == "first\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700

    def test_existing_snapshot_is_not_overwritten(self) -> None:
        assert self.save()[0]
        self.source.write_text("second\n", encoding="utf-8")
        ok, reason = self.save()
        assert ok, reason
        target = self.base / "baselines" / "task" / "repo" / "src" / "app.py"
        assert target.read_text(encoding="utf-8") == "first\n"

    def test_symlink_snapshot_is_rejected(self) -> None:
        target = self.base / "baselines" / "task" / "repo" / "src" / "app.py"
        target.parent.mkdir(parents=True)
        os.chmod(target.parent, 0o700)
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("x\n", encoding="utf-8")
        target.symlink_to(elsewhere)
        ok, reason = self.save()
        assert not ok
        assert "符号链接" in reason
        assert elsewhere.read_text(encoding="utf-8") == "x\n"

    def test_fifo_source_is_rejected(self) -> None:
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        ok, reason = self.save(source=fifo)
        assert not ok
        assert "普通文件" in reason

    def test_oversized_source_is_rejected(self) -> None:
        ok, reason = self.save(limit=4)
        assert not ok
        assert "上限" in reason

    def open_fd_count(self) -> int:
        """当前进程打开的文件描述符数量；不支持的平台跳过。"""
        for path in ("/dev/fd", "/proc/self/fd"):
            if os.path.isdir(path):
                return len(os.listdir(path))
        pytest.skip("该系统不支持列举打开的文件描述符")

    def test_rejection_paths_do_not_leak_source_descriptors(self) -> None:
        big = self.root / "big.bin"
        big.write_bytes(b"x" * 4096)
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        # 先各跑一次：避开首次调用的惰性导入/目录创建噪声。
        assert not self.save(relative="big.bin", source=big, limit=1024)[0]
        assert not self.save(relative="pipe", source=fifo)[0]
        before = self.open_fd_count()
        for _index in range(20):
            ok, reason = self.save(relative="big.bin", source=big, limit=1024)
            assert not ok, reason
            assert "上限" in reason
            ok, reason = self.save(relative="pipe", source=fifo)
            assert not ok, reason
            assert "普通文件" in reason
        # 拒绝分支必须关闭源文件 FD：连续 40 次拒绝前后打开数量不变。
        assert self.open_fd_count() == before
