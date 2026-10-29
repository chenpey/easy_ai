"""F08 软链/私有路径与 F25 内部预算、非普通文件、锁外查询。"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import HOOKS_ROOT, HookCase

if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from hooks import models, state  # noqa: E402

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


class ModelsSelectionTests(unittest.TestCase):
    """显式 `models_file` 无效时不能静默回退到低优先级来源。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        (self.project / ".easydev").mkdir(parents=True)
        self.home = self.root / "home"
        (self.home / ".config" / "easydev").mkdir(parents=True)
        self.env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config")}
        # `user_config_path` 按 `~/.config` 定位：隔离进程内的 HOME，不读维护者配置。
        patched = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)
        # 低优先级来源都放合法哨兵配置：一旦回退，source 与 model 会直接暴露。
        self.write_config(self.project / ".easydev" / "models.json", "project")
        self.write_config(self.home / ".config" / "easydev" / "models.json", "user")

    def write_config(self, path: Path, tag: str) -> None:
        config = {
            "developer": {"model": "provider/%s-dev" % tag, "reasoning_effort": "high"},
            "reviewer": {"model": "provider/%s-rev" % tag, "reasoning_effort": "high"},
        }
        path.write_text(json.dumps(config), encoding="utf-8")

    def select(self, context: dict):
        return models.select(context=context, project_root=str(self.project), env=self.env)

    def test_explicit_presence_prevents_fallback(self) -> None:
        for value in ("", "   ", None, False, 0, [], ["x"], {"path": "x"}):
            with self.subTest(value=value):
                selection = self.select({"models_file": value})
                self.assertFalse(selection.ok, selection.error)
                self.assertIsNone(selection.config)
                self.assertEqual(selection.source, "对话指定文件")
                self.assertIn("对话指定", selection.error)

    def test_omitted_key_keeps_source_order(self) -> None:
        project = self.select({"repo_root": str(self.project)})
        self.assertTrue(project.ok, project.error)
        self.assertEqual(project.source, "项目配置")
        self.assertEqual(project.config["developer"]["model"], "provider/project-dev")
        (self.project / ".easydev" / "models.json").unlink()
        user = self.select({})
        self.assertTrue(user.ok, user.error)
        self.assertEqual(user.source, "用户配置")
        self.assertEqual(user.config["developer"]["model"], "provider/user-dev")
        (self.home / ".config" / "easydev" / "models.json").unlink()
        builtin = self.select({})
        self.assertTrue(builtin.ok, builtin.error)
        self.assertEqual(builtin.source, "内置默认")

    def test_explicit_file_wins_over_lower_sources(self) -> None:
        explicit = self.root / "explicit.json"
        self.write_config(explicit, "explicit")
        selection = self.select({"models_file": str(explicit)})
        self.assertTrue(selection.ok, selection.error)
        self.assertEqual(selection.source, "对话指定文件")
        self.assertEqual(selection.config["developer"]["model"], "provider/explicit-dev")

    def test_explicit_missing_file_does_not_fall_back(self) -> None:
        selection = self.select({"models_file": str(self.root / "missing.json")})
        self.assertFalse(selection.ok, selection.error)
        self.assertIsNone(selection.config)


def _open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


class LockRollbackTests(unittest.TestCase):
    """P3-7：中途取锁失败后回滚已获锁，同一进程内可立即重获。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "state"
        self.directory.mkdir(mode=0o700)
        os.chmod(self.directory, 0o700)

    def hold_lock(self, name: str):
        handle = open(self.directory / (name + ".lock"), "w+b")
        self.addCleanup(handle.close)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle

    def record_flock(self, events, interrupt_on_exclusive=None, interrupt=None):
        """记录 flock (op, fd) 调用序列；可选在第 N 次 LOCK_EX 上抛指定异常。"""
        real_flock = fcntl.flock
        exclusive = []

        def recorder(fd, operation):
            events.append((operation, fd))
            if not operation & fcntl.LOCK_UN:
                exclusive.append(fd)
                if interrupt_on_exclusive is not None and len(exclusive) == interrupt_on_exclusive:
                    raise interrupt
            return real_flock(fd, operation)

        return recorder

    def test_failed_open_of_second_lock_releases_the_first(self) -> None:
        """第二把锁 **os.open** 失败（软链 + O_NOFOLLOW → ELOOP）同样回滚第一把，
        且第一把的 fd 真的已关闭（EBADF），不只是从列表里移除。"""
        (self.directory / "b.lock").symlink_to(self.directory / "missing-target")
        locks = state.Locks(str(self.directory))
        events = []
        with mock.patch("fcntl.flock", side_effect=self.record_flock(events)):
            with self.assertRaises(state.StateUnavailable) as caught:
                locks.acquire(["a", "b"])
        self.assertIn("cannot open lock", str(caught.exception))
        acquired = [fd for operation, fd in events if not operation & fcntl.LOCK_UN]
        self.assertEqual(len(acquired), 1, events)
        self.assertEqual(locks._fds, [])
        self.assertFalse(_open(acquired[0]), "第一把锁的 fd 仍打开")
        with state.Locks(str(self.directory)) as retried:
            retried.acquire(["a"])
            self.assertEqual(len(retried._fds), 1)

    def test_base_exception_identity_survives_and_fds_close(self) -> None:
        """中途 `KeyboardInterrupt`：抛出的是同一个异常对象，已获 fd 全部关闭。"""
        interrupt = KeyboardInterrupt("ctrl-c")
        locks = state.Locks(str(self.directory))
        events = []
        recorder = self.record_flock(events, interrupt_on_exclusive=2, interrupt=interrupt)
        with mock.patch("fcntl.flock", side_effect=recorder):
            with self.assertRaises(KeyboardInterrupt) as caught:
                locks.acquire(["a", "b"])
        self.assertIs(caught.exception, interrupt)
        self.assertEqual(locks._fds, [])
        exclusive = [fd for operation, fd in events if not operation & fcntl.LOCK_UN]
        unlocked = [fd for operation, fd in events if operation & fcntl.LOCK_UN]
        self.assertEqual(len(exclusive), 2, events)
        # 已获 fd 全部关闭，且清理是**逆序**释放（后取先放）。
        for fd in exclusive:
            self.assertFalse(_open(fd), "fd %s 仍打开" % fd)
        self.assertEqual(unlocked, list(reversed(exclusive)), events)
        with state.Locks(str(self.directory)) as retried:
            retried.acquire(["a", "b"])
            self.assertEqual(len(retried._fds), 2)

    def test_cleanup_failure_still_releases_remaining_fds(self) -> None:
        """一次 `close` 失败不得中断其余释放：其余 fd 真的关闭且锁可立即重获。"""
        locks = state.Locks(str(self.directory))
        locks.acquire(["a", "b", "c"])
        fds = list(locks._fds)
        self.assertEqual(len(fds), 3)
        real_close = os.close

        def flaky_close(fd):
            if fd == fds[1]:
                raise OSError(5, "close failed")
            return real_close(fd)

        self.addCleanup(lambda: os.close(fds[1]) if _open(fds[1]) else None)
        with mock.patch("hooks.state.os.close", side_effect=flaky_close):
            error = locks._release()
        self.assertIsInstance(error, OSError)
        self.assertEqual(locks._fds, [])
        for fd in (fds[0], fds[2]):
            with self.assertRaises(OSError):
                os.fstat(fd)
        with state.Locks(str(self.directory)) as retried:
            retried.acquire(["a", "b", "c"])
            self.assertEqual(len(retried._fds), 3)

    def test_failed_second_lock_releases_the_first(self) -> None:
        self.hold_lock("b")
        locks = state.Locks(str(self.directory))
        started = time.monotonic()
        with self.assertRaises(state.StateUnavailable) as caught:
            locks.acquire(["a", "b"])
        self.assertIn("busy", str(caught.exception))
        self.assertLess(time.monotonic() - started, 2.0)
        # 已获 fd 不得移交下一次 acquire，也不允许继续占用第一把锁。
        self.assertEqual(locks._fds, [])
        with state.Locks(str(self.directory)) as retried:
            retried.acquire(["a"])
            self.assertEqual(len(retried._fds), 1)
        # 正常退出后同样可以再次获取。
        with state.Locks(str(self.directory)) as again:
            again.acquire(["a"])
            self.assertEqual(len(again._fds), 1)


if __name__ == "__main__":
    unittest.main()
