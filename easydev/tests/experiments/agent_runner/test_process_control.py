"""Behavior checks for bounded acceptance execution."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest

import process_control
from process_control import terminate_process, wait_for_process
from scenarios.common import run_python


class AcceptanceTimeoutTest:
    def test_timeout_with_inherited_output_handles_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            result = run_python(
                Path(directory),
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "print('started', flush=True); time.sleep(30)",
                timeout=0.3,
            )
            assert result["timed_out"]
            assert result["returncode"] != 0
            assert "started" in result["stdout"]
            assert (time.monotonic() - started) < 15


def test_terminate_process_leaves_reused_process_group_alone() -> None:
    """leader 已退出且无存活已跟踪后代时，同 PGID 的组可能已被复用，不得收编或发信号。"""
    leader = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        preexec_fn=os.setpgrp,
    )
    bystander = None
    try:
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            preexec_fn=lambda: os.setpgid(0, leader.pid),
        )
        leader.kill()
        leader.wait()
        assert os.getpgid(bystander.pid) == leader.pid

        terminate_process(leader, grace_seconds=0.5)

        assert bystander.poll() is None
    finally:
        for process in (bystander, leader):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()


def test_wait_for_process_keeps_tracked_when_interrupted(monkeypatch) -> None:
    """等待期间被中断时，已观测到的后代必须留在调用方的 tracked 里供清理。"""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        preexec_fn=os.setpgrp,
    )
    tracked = {}

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(process_control.time, "sleep", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            wait_for_process(process, 5, tracked=tracked)
        assert process.pid in tracked
    finally:
        monkeypatch.undo()
        terminate_process(process, tracked=tracked, grace_seconds=1.0)


def test_wait_for_process_records_descendants_in_caller_dict() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "time.sleep(30)",
        ],
        preexec_fn=os.setpgrp,
    )
    tracked = {}
    try:
        returncode, returned = wait_for_process(process, 0.8, tracked=tracked)
        assert returncode is None
        assert returned is tracked
        assert any(pid != process.pid for pid in tracked)
    finally:
        terminate_process(process, tracked=tracked, grace_seconds=1.0)


def test_terminate_process_reports_unverifiable_cleanup(monkeypatch) -> None:
    """快照持续失败时不得假报清理成功，也不向历史 PID 盲发信号。"""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        preexec_fn=os.setpgrp,
    )

    def boom():
        raise RuntimeError("snapshot down")

    monkeypatch.setattr(process_control, "_process_snapshot", boom)
    try:
        with pytest.raises(RuntimeError, match="snapshot unavailable"):
            process_control.terminate_process(process, grace_seconds=0.2)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def sigterm_ignoring_child(directory: Path) -> tuple[subprocess.Popen, dict]:
    """启动忽略 SIGTERM、独立进程组的进程，等它装好处理器后用真实快照读出 identity。"""
    ready = directory / "ready"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)",
        ],
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    identity = process_control._process_snapshot()[process.pid][1]
    return process, {process.pid: identity}


def test_terminate_process_sigkill_skips_tracked_when_snapshot_fails(monkeypatch) -> None:
    """SIGKILL 阶段快照持续失败时，不得用 SIGTERM 阶段的旧快照向已跟踪 PID 发信号。"""
    with tempfile.TemporaryDirectory() as directory:
        leader = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            preexec_fn=os.setpgrp,
        )
        leader.kill()
        leader.wait()
        bystander, tracked = sigterm_ignoring_child(Path(directory))
        real_snapshot = process_control._process_snapshot
        calls = []

        def flaky_snapshot():
            calls.append(None)
            if len(calls) == 1:
                return real_snapshot()
            raise RuntimeError("snapshot down")

        monkeypatch.setattr(process_control, "_process_snapshot", flaky_snapshot)
        try:
            with pytest.raises(RuntimeError, match="snapshot unavailable"):
                terminate_process(leader, grace_seconds=0, tracked=tracked)
            time.sleep(0.2)
            assert bystander.poll() is None
        finally:
            if bystander.poll() is None:
                bystander.kill()
                bystander.wait()


def test_terminate_process_sigkill_skips_tracked_on_transient_snapshot_failure(
    monkeypatch,
) -> None:
    """SIGKILL 阶段单次快照失败也不得回退到旧快照；恢复后的快照仍按实情报告存活。"""
    with tempfile.TemporaryDirectory() as directory:
        leader = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            preexec_fn=os.setpgrp,
        )
        leader.kill()
        leader.wait()
        bystander, tracked = sigterm_ignoring_child(Path(directory))
        real_snapshot = process_control._process_snapshot
        calls = []

        def flaky_snapshot():
            calls.append(None)
            if len(calls) == 2:
                raise RuntimeError("snapshot down")
            return real_snapshot()

        monkeypatch.setattr(process_control, "_process_snapshot", flaky_snapshot)
        try:
            with pytest.raises(RuntimeError, match="survived SIGKILL"):
                terminate_process(leader, grace_seconds=0, tracked=tracked)
            assert bystander.poll() is None
        finally:
            if bystander.poll() is None:
                bystander.kill()
                bystander.wait()


def test_terminate_process_reports_signal_failures(monkeypatch) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        preexec_fn=os.setpgrp,
    )
    monkeypatch.setattr(
        process_control,
        "_signal_known",
        lambda *_args, **_kwargs: ["cannot signal process 999"],
    )
    try:
        with pytest.raises(RuntimeError, match="signal errors"):
            process_control.terminate_process(process, grace_seconds=0.2)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
