"""F08 软链/私有路径与 F25 内部预算、非普通文件、锁外查询。"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
import unittest

from support import HookCase

SHELL_GIT_WRAPPER = """#!/bin/sh
sleep %s
exec %s "$@"
"""


class SymlinkSafetyTests(HookCase):
    host = "pi"

    def test_state_ancestor_symlink_is_refused(self) -> None:
        real = self.root / "real-state"
        real.mkdir(mode=0o700)
        link = self.root / "linked-state"
        link.symlink_to(real)
        self.write_context(state_root=str(link / "state"))
        _code, value, stderr = self.run_core(
            self.input_payload("s1", "使用 easydev，完成这个改动")
        )
        self.assertFalse(self.is_denied(value), value)
        # 不穿过祖先软链创建/写入任何内容。
        self.assertEqual(sorted(path.name for path in real.iterdir()), [])
        self.assertIn("warning", stderr)

    def test_task_root_symlink_is_denied(self) -> None:
        task = self.make_task_root("task-real")
        self.save_baseline(task, "src/app.py")
        before = os.lstat(task / "baseline" / "src" / "app.py")
        self.touch_dirty()
        link = self.root / "task-link"
        link.symlink_to(task)
        self.write_context(task_root=str(link))
        self.activate()
        self.deny(self.write_tool("s1", "src/app.py"))
        after = os.lstat(task / "baseline" / "src" / "app.py")
        self.assertEqual(before.st_mode, after.st_mode)
        self.assertEqual(before.st_ino, after.st_ino)

    def test_baseline_parent_chain_symlink_is_denied(self) -> None:
        task = self.make_task_root("task-chain")
        outside = self.make_task_root("outside")
        (outside / "app.py").write_text("value = 1\n", encoding="utf-8")
        os.chmod(outside / "app.py", 0o600)
        (task / "baseline").mkdir(mode=0o700)
        os.chmod(task / "baseline", 0o700)
        (task / "baseline" / "src").symlink_to(outside)
        self.touch_dirty()
        self.write_context(task_root=str(task))
        self.activate()
        reason = self.deny(self.write_tool("s1", "src/app.py"))
        self.assertIn("baseline", reason)
        # 备份目录里的真实目标保持原状。
        self.assertEqual((outside / "app.py").read_text(encoding="utf-8"), "value = 1\n")

    def test_business_file_symlink_is_denied(self) -> None:
        outside = self.root / "outside-secret.py"
        outside.write_text("secret = 1\n", encoding="utf-8")
        os.chmod(outside, 0o600)
        link = self.repo / "src" / "linked.py"
        link.symlink_to(outside)
        self.git("add", "-A")
        self.git("commit", "-qm", "symlink")
        self.activate()
        reason = self.deny(self.write_tool("s1", "src/linked.py"))
        self.assertIn("符号链接", reason)
        self.assertEqual(outside.read_text(encoding="utf-8"), "secret = 1\n")

    def test_business_directory_symlink_is_denied(self) -> None:
        outside = self.root / "outside-dir"
        outside.mkdir(mode=0o700)
        (outside / "app.py").write_text("secret = 1\n", encoding="utf-8")
        link = self.repo / "src" / "linked-dir"
        link.symlink_to(outside)
        self.git("add", "-A")
        self.git("commit", "-qm", "dir symlink")
        self.activate()
        reason = self.deny(self.write_tool("s1", "src/linked-dir/app.py"))
        self.assertIn("符号链接", reason)
        self.assertEqual((outside / "app.py").read_text(encoding="utf-8"), "secret = 1\n")


class InternalBudgetTests(HookCase):
    host = "pi"

    def test_git_delay_is_cut_by_internal_budget(self) -> None:
        real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
        wrapper_dir = self.root / "slow-git"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "git"
        wrapper.write_text(SHELL_GIT_WRAPPER % ("0.25", real_git), encoding="utf-8")
        os.chmod(wrapper, 0o755)
        self.activate()
        env = {"PATH": str(wrapper_dir) + os.pathsep + os.environ.get("PATH", ""),
               "EASYDEV_HOOK_BUDGET_MS": "60"}
        started = time.monotonic()
        _code, value, stderr = self.run_core(self.write_tool("s1", "src/app.py"), env=env)
        elapsed = time.monotonic() - started
        self.assertFalse(self.is_denied(value), value)
        self.assertIn("warning", stderr)
        # 共享内部截止（60ms）切断这次 Git 查询：不会等满 0.25s 的脚本延迟，
        # 也不会退回到单次 300ms 的默认 Git 超时。
        self.assertLess(elapsed, 0.25)

    def test_fifo_target_is_denied_without_blocking(self) -> None:
        fifo = self.repo / "src" / "pipe.py"
        os.mkfifo(fifo)
        self.activate()
        started = time.monotonic()
        _code, value, _stderr = self.run_core(self.write_tool("s1", "src/pipe.py"))
        elapsed = time.monotonic() - started
        # 非普通文件没有可信首次内容：拒绝写入而不是静默降级，且不得阻塞。
        self.assertTrue(self.is_denied(value), value)
        self.assertIn("普通文件", self.reason(value))
        self.assertLess(elapsed, 5.0)

    def test_lock_busy_degrades_quickly(self) -> None:
        self.activate("P")
        lock_dir = self.state / "locks"
        held = []
        try:
            for path in sorted(lock_dir.glob("*.lock")):
                handle = open(path, "r+b")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(handle)
            self.assertTrue(held)
            started = time.monotonic()
            _code, value, stderr = self.run_core(self.settle_payload("P"))
            elapsed = time.monotonic() - started
        finally:
            for handle in held:
                handle.close()
        self.assertFalse(self.is_continued(value), value)
        self.assertIn("锁", stderr)
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
