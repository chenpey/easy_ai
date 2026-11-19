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
from process_control import terminate_process
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
