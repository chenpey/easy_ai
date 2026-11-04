"""Behavior checks for bounded acceptance execution."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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
