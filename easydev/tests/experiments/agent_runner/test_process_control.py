"""Behavior checks for bounded acceptance execution."""
import sys
import subprocess
import os
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenarios.common import run_python
from process_control import terminate_process, wait_for_process


class AcceptanceTimeoutTest:
    def test_timeout_with_inherited_output_handles_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            result = run_python(Path(directory),
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "print('started', flush=True); time.sleep(30)", timeout=0.3)
            assert result['timed_out']
            assert result['returncode'] != 0
            assert 'started' in result['stdout']
            assert (time.monotonic() - started) < 15

    @pytest.mark.skipif(os.name != "posix", reason="PID identity check is POSIX-specific")
    def test_stale_identity_does_not_signal_reused_pid(self):
        process = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                   start_new_session=True)
        class ReapedProcess:
            pid = process.pid
            def poll(self): return 0
            def wait(self, timeout=None): return 0
        try:
            _, tracked = wait_for_process(process, .05)
            assert process.pid in tracked
            stale = tuple(value - 100 for value in tracked[process.pid])
            terminate_process(ReapedProcess(), tracked={process.pid: stale}, grace_seconds=.1)
            assert process.poll() is None, "a different process owns the reused PID"
        finally:
            if process.poll() is None: process.kill()
            process.wait(timeout=3)
