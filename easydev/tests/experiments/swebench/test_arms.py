"""档位对照臂：prompt 钉住路径、技能安装、默认臂不变与未知臂拒绝。"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

SWEBENCH_ROOT = Path(__file__).resolve().parent
if str(SWEBENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(SWEBENCH_ROOT))

import run as swebench_run  # noqa: E402


class ArmPromptTests(unittest.TestCase):
    def test_default_arms_stay_auto_only(self) -> None:
        self.assertEqual(swebench_run.DEFAULT_ARMS, ["baseline", "checklist", "easydev"])

    def test_path_arms_are_advertised(self) -> None:
        for arm in ("easydev-quick", "easydev-normal", "easydev-full"):
            self.assertIn(arm, swebench_run.AVAILABLE_ARMS)
        self.assertEqual(
            sorted(swebench_run.EASYDEV_PATH_ARMS),
            ["easydev-full", "easydev-normal", "easydev-quick"],
        )

    def test_auto_arm_prompt_is_unchanged(self) -> None:
        prompt = swebench_run.build_prompt("easydev", "TASK")
        self.assertTrue(prompt.startswith("使用 easydev auto 完成下面的仓库任务。"))
        self.assertIn("不要修改或创建测试来放宽验收", prompt)
        self.assertNotIn("不要改用 auto 重新分流", prompt)
        self.assertTrue(prompt.endswith("Task:\nTASK"))

    def test_each_path_arm_pins_its_own_path(self) -> None:
        prompts = set()
        for arm, path in swebench_run.EASYDEV_PATH_ARMS.items():
            prompt = swebench_run.build_prompt(arm, "TASK")
            self.assertIn(f"使用 easydev {path} 完成下面的仓库任务。", prompt)
            self.assertIn("不要改用 auto 重新分流", prompt)
            self.assertIn("不要修改或创建测试来放宽验收", prompt)
            self.assertNotIn("使用 easydev auto", prompt)
            self.assertTrue(prompt.endswith("Task:\nTASK"))
            prompts.add(prompt)
        # 三个臂必须互不相同，否则对照没有意义
        self.assertEqual(len(prompts), 3)

    def test_only_easydev_arms_install_the_skill(self) -> None:
        for arm in ("easydev", *swebench_run.EASYDEV_PATH_ARMS):
            self.assertTrue(swebench_run.is_easydev_arm(arm))
        for arm in ("baseline", "checklist", "easydev-"):
            self.assertFalse(swebench_run.is_easydev_arm(arm))

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
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("unknown arm", combined.lower())
        self.assertIn("easydev-normal", combined)


if __name__ == "__main__":
    unittest.main()
