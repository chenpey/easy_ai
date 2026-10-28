"""D：dirty/untracked 目标的首次内容由 Hook 自动快照到私有状态根。

快照是私有缓存，不是业务备份：不写业务文件、不进 task 锁、不覆盖首次内容。
显式 `task_root` 仍走手工 baseline 核对路径（兼容旧协议）。
"""

from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path

from support import HookCase


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
        self.assertFalse(self.is_denied(value), value)
        saved = self.snapshots("src/app.py")
        self.assertEqual(len(saved), 1, saved)
        snapshot = saved[0]
        self.assertEqual(snapshot.read_text(encoding="utf-8"), "value = 2\n")
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snapshot.parent.stat().st_mode), 0o700)
        # 私有状态根之外不产生业务文件改动。
        self.assertEqual((self.repo / "src" / "app.py").read_text(encoding="utf-8"), "value = 2\n")

    def test_untracked_write_saves_first_content(self) -> None:
        (self.repo / "notes.txt").write_text("user notes\n", encoding="utf-8")
        self.activate_auto()
        self.allow(self.write_tool("s1", "notes.txt", tool_id="n1"))
        saved = self.snapshots("notes.txt")
        self.assertEqual(len(saved), 1, saved)
        self.assertEqual(saved[0].read_text(encoding="utf-8"), "user notes\n")

    def test_second_write_does_not_overwrite_first_snapshot(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        self.activate_auto()
        self.write_path("s1", "w1", content="value = 3\n")
        # 第二次写目标内容与上次成功指纹一致（未被外部修改），仍不得覆盖首次快照。
        self.allow(self.write_tool("s1", "src/app.py", tool_id="w2"))
        saved = self.snapshots("src/app.py")
        self.assertEqual(len(saved), 1, saved)
        self.assertEqual(saved[0].read_text(encoding="utf-8"), "value = 2\n")

    def test_clean_and_absent_targets_do_not_create_snapshots(self) -> None:
        self.activate_auto()
        self.allow(self.write_tool("s1", "src/app.py", tool_id="c1"))
        self.allow(self.write_tool("s1", "src/new_mod.py", tool_id="a1"))
        baselines = self.state / "baselines"
        self.assertFalse(baselines.exists() and any(baselines.rglob("*.*")))

    def test_external_modification_after_success_is_still_denied(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        self.activate_auto()
        self.write_path("s1", "w1", content="value = 3\n")
        (self.repo / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="w2"))
        self.assertIn("外部修改", reason)

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
        self.assertEqual(len(self.snapshots("src/app.py")), 1)
        self.assertEqual(len(self.snapshots("docs/guide.md")), 1)
        self.assertEqual(self.snapshots("src/app.py")[0].read_text(encoding="utf-8"), "value = 2\n")
        self.assertEqual(
            self.snapshots("docs/guide.md")[0].read_text(encoding="utf-8"), "user guide\n"
        )

    def test_manual_task_root_still_requires_baseline(self) -> None:
        self.touch_dirty("src/app.py", "value = 2\n")
        task_root = self.make_task_root("manual")
        self.write_context(task_root=str(task_root))
        self.activate()
        reason = self.deny(self.write_tool("s1", "src/app.py", tool_id="m1"))
        self.assertIn("缺少合规首次基线", reason)
        # 手工根提供合规备份后放行，且不产生自动快照。
        self.save_baseline(task_root, "src/app.py", "value = 2\n")
        self.allow(self.write_tool("s1", "src/app.py", tool_id="m2"))
        self.assertFalse((self.state / "baselines").exists())

    def test_oversized_target_is_denied(self) -> None:
        big = self.repo / "src" / "big.py"
        big.write_bytes(b"x" * (9 * 1024 * 1024))
        self.activate_auto()
        reason = self.deny(self.write_tool("s1", "src/big.py", tool_id="big1"))
        self.assertIn("上限", reason)
        self.assertFalse(self.snapshots("src/big.py"))

    def test_unreadable_target_is_denied(self) -> None:
        target = self.repo / "src" / "locked.py"
        target.write_text("secret\n", encoding="utf-8")
        os.chmod(target, 0o000)
        self.addCleanup(os.chmod, target, 0o600)
        self.activate_auto()
        reason = self.deny(self.write_tool("s1", "src/locked.py", tool_id="lock1"))
        self.assertIn("普通文件", reason)
        self.assertFalse(self.snapshots("src/locked.py"))

    def test_fifo_target_is_denied(self) -> None:
        fifo = self.repo / "src" / "pipe.py"
        os.mkfifo(fifo)
        self.activate_auto()
        reason = self.deny(self.write_tool("s1", "src/pipe.py", tool_id="f1"))
        self.assertIn("普通文件", reason)

    def test_inactive_session_does_not_write_snapshots(self) -> None:
        self.auto_context()
        self.touch_dirty("src/app.py", "value = 2\n")
        self.allow(self.write_tool("other", "src/app.py", tool_id="i1"))
        self.assertFalse((self.state / "baselines").exists())


class PiBaselineAutoTests(BaselineAutoSnapshotMixin, HookCase):
    host = "pi"


class AntigravityBaselineAutoTests(BaselineAutoSnapshotMixin, HookCase):
    host = "antigravity"


class CodexBaselineAutoTests(BaselineAutoSnapshotMixin, HookCase):
    host = "codex"


class DshBaselineAutoTests(BaselineAutoSnapshotMixin, HookCase):
    host = "dsh"


class SnapshotStateTests(unittest.TestCase):
    """快照写盘本身：不覆盖、拒绝软链/非普通文件、目录 0700。"""

    def setUp(self) -> None:
        import sys
        import tempfile

        from support import HOOKS_ROOT

        if str(HOOKS_ROOT) not in sys.path:
            sys.path.insert(0, str(HOOKS_ROOT))
        from hooks import state

        self.state = state
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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
        self.assertTrue(ok, reason)
        target = self.base / "baselines" / "task" / "repo" / "src" / "app.py"
        self.assertEqual(target.read_text(encoding="utf-8"), "first\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_existing_snapshot_is_not_overwritten(self) -> None:
        self.assertTrue(self.save()[0])
        self.source.write_text("second\n", encoding="utf-8")
        ok, reason = self.save()
        self.assertTrue(ok, reason)
        target = self.base / "baselines" / "task" / "repo" / "src" / "app.py"
        self.assertEqual(target.read_text(encoding="utf-8"), "first\n")

    def test_symlink_snapshot_is_rejected(self) -> None:
        target = self.base / "baselines" / "task" / "repo" / "src" / "app.py"
        target.parent.mkdir(parents=True)
        os.chmod(target.parent, 0o700)
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("x\n", encoding="utf-8")
        target.symlink_to(elsewhere)
        ok, reason = self.save()
        self.assertFalse(ok)
        self.assertIn("符号链接", reason)
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), "x\n")

    def test_fifo_source_is_rejected(self) -> None:
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        ok, reason = self.save(source=fifo)
        self.assertFalse(ok)
        self.assertIn("普通文件", reason)

    def test_oversized_source_is_rejected(self) -> None:
        ok, reason = self.save(limit=4)
        self.assertFalse(ok)
        self.assertIn("上限", reason)

    def open_fd_count(self) -> int:
        """当前进程打开的文件描述符数量；不支持的平台跳过。"""
        for path in ("/dev/fd", "/proc/self/fd"):
            if os.path.isdir(path):
                return len(os.listdir(path))
        self.skipTest("该系统不支持列举打开的文件描述符")

    def test_rejection_paths_do_not_leak_source_descriptors(self) -> None:
        big = self.root / "big.bin"
        big.write_bytes(b"x" * 4096)
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        # 先各跑一次：避开首次调用的惰性导入/目录创建噪声。
        self.assertFalse(self.save(relative="big.bin", source=big, limit=1024)[0])
        self.assertFalse(self.save(relative="pipe", source=fifo)[0])
        before = self.open_fd_count()
        for _index in range(20):
            ok, reason = self.save(relative="big.bin", source=big, limit=1024)
            self.assertFalse(ok, reason)
            self.assertIn("上限", reason)
            ok, reason = self.save(relative="pipe", source=fifo)
            self.assertFalse(ok, reason)
            self.assertIn("普通文件", reason)
        # 拒绝分支必须关闭源文件 FD：连续 40 次拒绝前后打开数量不变。
        self.assertEqual(self.open_fd_count(), before)


if __name__ == "__main__":
    unittest.main()
