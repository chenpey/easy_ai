"""档位对照臂：prompt 钉住路径、技能安装、默认臂不变与未知臂拒绝。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SWEBENCH_ROOT = Path(__file__).resolve().parent
if str(SWEBENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(SWEBENCH_ROOT))

import run as swebench_run  # noqa: E402


class ArmPromptTests:
    def test_default_arms_stay_auto_only(self) -> None:
        assert swebench_run.DEFAULT_ARMS == ["baseline", "checklist", "easydev"]

    def test_path_arms_are_advertised(self) -> None:
        for arm in ("easydev-quick", "easydev-normal", "easydev-full"):
            assert arm in swebench_run.AVAILABLE_ARMS
        assert sorted(swebench_run.EASYDEV_PATH_ARMS) == ["easydev-full", "easydev-normal", "easydev-quick"]

    def test_auto_arm_prompt_is_unchanged(self) -> None:
        prompt = swebench_run.build_prompt("easydev", "TASK")
        assert prompt.startswith("使用 easydev auto 完成下面的仓库任务。")
        assert "不要修改或创建测试来放宽验收" in prompt
        assert "不要改用 auto 重新分流" not in prompt
        assert prompt.endswith("Task:\nTASK")

    def test_each_path_arm_pins_its_own_path(self) -> None:
        prompts = set()
        for arm, path in swebench_run.EASYDEV_PATH_ARMS.items():
            prompt = swebench_run.build_prompt(arm, "TASK")
            assert f"使用 easydev {path} 完成下面的仓库任务。" in prompt
            assert "不要改用 auto 重新分流" in prompt
            assert "不要修改或创建测试来放宽验收" in prompt
            assert "使用 easydev auto" not in prompt
            assert prompt.endswith("Task:\nTASK")
            prompts.add(prompt)
        # 三个臂必须互不相同，否则对照没有意义
        assert len(prompts) == 3

    def test_only_easydev_arms_install_the_skill(self) -> None:
        for arm in ("easydev", *swebench_run.EASYDEV_PATH_ARMS):
            assert swebench_run.is_easydev_arm(arm)
        for arm in ("baseline", "checklist", "easydev-"):
            assert not swebench_run.is_easydev_arm(arm)

    def test_unknown_arm_rejected_before_reading_the_experiment(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SWEBENCH_ROOT / "run.py"),
                "run",
                "--experiment",
                "/tmp/easydev-no-such-experiment",
                "--arms",
                "easydev-fast",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        combined = proc.stdout + proc.stderr
        assert "unknown arm" in combined.lower()
        assert "easydev-normal" in combined
