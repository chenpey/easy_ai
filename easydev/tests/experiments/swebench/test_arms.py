"""档位对照臂：prompt 钉住路径、技能安装、默认臂不变与未知臂拒绝。"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

SWEBENCH_ROOT = Path(__file__).resolve().parent
RUN = SWEBENCH_ROOT / "run.py"
STUBS = SWEBENCH_ROOT.parent / "stubs"
PYTEST_STUB = STUBS / "pytest_stub.py"

TASK = {
    "id": "demo__task-1",
    "f2p": "tests/test_f2p.py::test_f2p",
    "p2p": "tests/test_p2p.py::test_p2p",
}


class ArmPromptTests:
    def setup_method(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="easydev-swebench-arms-"))

    def teardown_method(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def make_experiment(self, name: str) -> SimpleNamespace:
        base = self.root / name
        experiment = base / "experiment"
        (experiment / "templates").mkdir(parents=True)
        (experiment / "venvs" / TASK["id"] / "bin").mkdir(parents=True)
        (experiment / "task-baselines").mkdir(parents=True)
        with tarfile.open(experiment / "templates" / f"{TASK['id']}.tar", "w") as archive:
            payload = b"VALUE = 1\n"
            info = tarfile.TarInfo("src/mod.py")
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
        (experiment / "venvs" / TASK["id"] / "bin" / "python").symlink_to(PYTEST_STUB)
        patch = (
            "diff --git a/tests/test_f2p.py b/tests/test_f2p.py\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "--- /dev/null\n"
            "+++ b/tests/test_f2p.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def test_f2p():\n"
            "+    assert True\n"
            "diff --git a/tests/test_p2p.py b/tests/test_p2p.py\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
            "--- /dev/null\n"
            "+++ b/tests/test_p2p.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def test_p2p():\n"
            "+    assert True\n"
        )
        (experiment / "dataset.json").write_text(
            json.dumps(
                [
                    {
                        "instance_id": TASK["id"],
                        "repo": "demo/repo",
                        "FAIL_TO_PASS": json.dumps([TASK["f2p"]]),
                        "PASS_TO_PASS": json.dumps([TASK["p2p"]]),
                        "patch": "",
                        "test_patch": patch,
                        "problem_statement": "TASK",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (experiment / "task-baselines" / f"{TASK['id']}.json").write_text(
            json.dumps(
                {
                    "eligible_p2p": [TASK["p2p"]],
                    "excluded_p2p": [],
                    "statuses": {TASK["p2p"]: "PASSED"},
                    "pytest_returncode": 0,
                }
            ),
            encoding="utf-8",
        )
        home = base / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "auth.json").write_text("{}\n", encoding="utf-8")
        pytest_plan = base / "pytest-plan.json"
        pytest_plan.write_text(
            json.dumps({"statuses": {TASK["f2p"]: "PASSED", TASK["p2p"]: "PASSED"}}),
            encoding="utf-8",
        )
        skill = base / "skill"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text("fixture skill\n", encoding="utf-8")
        launcher = base / "codex-recorder"
        launcher.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "workdir = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])\n"
            "last = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "prompt = sys.argv[-1]\n"
            "(workdir / 'src' / 'mod.py').write_text('VALUE = 2\\n', encoding='utf-8')\n"
            "last.write_text(prompt, encoding='utf-8')\n"
            "print(json.dumps({'type': 'item.completed', 'item': {'id': 'm1', 'type': 'agent_message', 'text': 'done'}}), flush=True)\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        env = {
            **os.environ,
            "HOME": str(home),
            "EASYDEV_PYTEST_PLAN": str(pytest_plan),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        return SimpleNamespace(experiment=experiment, launcher=launcher, skill=skill, env=env)

    def run_cli(self, fixture: SimpleNamespace, *arms: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            "-B",
            str(RUN),
            "run",
            "--experiment",
            str(fixture.experiment),
            "--tasks",
            TASK["id"],
            "--repeats",
            "1",
            "--concurrency",
            "1",
            "--codex-bin",
            str(fixture.launcher),
            "--skill-root",
            str(fixture.skill),
        ]
        if arms:
            cmd.extend(["--arms", *arms])
        return subprocess.run(cmd, env=fixture.env, capture_output=True, text=True, timeout=60)

    def test_default_arms_stay_auto_only(self) -> None:
        fixture = self.make_experiment("default-arms")
        proc = self.run_cli(fixture)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        task_runs = fixture.experiment / "runs" / TASK["id"]
        assert sorted(p.name for p in task_runs.iterdir() if p.is_dir()) == [
            "baseline",
            "checklist",
            "easydev",
        ]

    def test_path_arms_and_auto_arm_prompts_and_skill_install(self) -> None:
        fixture = self.make_experiment("all-arms")
        all_arms = (
            "baseline",
            "checklist",
            "easydev",
            "easydev-quick",
            "easydev-normal",
            "easydev-full",
        )
        proc = self.run_cli(fixture, *all_arms)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        task_runs = fixture.experiment / "runs" / TASK["id"]

        auto_prompt = (task_runs / "easydev" / "r1" / "last-message.txt").read_text(encoding="utf-8")
        assert auto_prompt.startswith("使用 easydev auto 完成下面的仓库任务。")
        assert "不要修改或创建测试来放宽验收" in auto_prompt
        assert "不要改用 auto 重新分流" not in auto_prompt
        assert auto_prompt.endswith("Task:\nTASK")

        prompts = set()
        for path in ("quick", "normal", "full"):
            arm = f"easydev-{path}"
            prompt = (task_runs / arm / "r1" / "last-message.txt").read_text(encoding="utf-8")
            assert f"使用 easydev {path} 完成下面的仓库任务。" in prompt
            assert "不要改用 auto 重新分流" in prompt
            assert "不要修改或创建测试来放宽验收" in prompt
            assert "使用 easydev auto" not in prompt
            assert prompt.endswith("Task:\nTASK")
            prompts.add(prompt)
        assert len(prompts) == 3

        for arm in ("easydev", "easydev-quick", "easydev-normal", "easydev-full"):
            assert (task_runs / arm / "r1" / "repo" / ".agents" / "skills" / "easydev" / "SKILL.md").is_file()
        for arm in ("baseline", "checklist"):
            assert not (task_runs / arm / "r1" / "repo" / ".agents" / "skills" / "easydev" / "SKILL.md").exists()

    def test_unknown_arm_rejected_before_reading_the_experiment(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(RUN),
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
