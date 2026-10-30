"""安装生成、合并、卸载、分发包启动与 pi 适配器（A0 / F04 / F05 / F23）。"""

from __future__ import annotations

import pytest

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from support import HOOKS_ROOT, HookCase, clean_env, core_module

INSTALL = HOOKS_ROOT / "hooks" / "install.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pi_adapter_fixture.mjs"
ANTIGRAVITY_GROUP = "easydev-hooks"
HIDDEN_STDERR = "core_stderr：核心有诊断，细节已隐藏"

# pi 适配器固定类别的验收 catalog。每条消息都取自 hooks/core.py 的真实文案（静态字面量、
# 动态模板取当前生产者值域内的代表值、write_targets 的 gap、normalize 的多工作区、
# run 的输入/协议/内部异常直接输出、probe 直接输出），第二项是期望的固定类别，
# 顺序即 design §7 的输出顺序。期望值独立写死，不由被测正则生成。
CORE_DIAGNOSTIC_CATALOG: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 状态不可用
    ("私有状态不可用，已降级放行", ("状态不可用",)),
    ("状态读取失败，已降级放行", ("状态不可用",)),
    ("状态读取失败，保留已确定的拒绝", ("状态不可用",)),
    ("状态读取失败，SKILL 读取恢复降级", ("状态不可用",)),
    ("状态处理异常，已降级放行", ("状态不可用",)),
    ("状态处理异常，保留已确定的拒绝", ("状态不可用",)),
    ("状态写失败，已保留本次判定并降级", ("状态不可用",)),
    ("transcript 输入取证的状态不可用，激活判定降级", ("状态不可用",)),
    ("分派预取状态不可用，模型预检降级", ("状态不可用",)),
    ("缺少会话标识，状态判定降级", ("状态不可用",)),
    ("probe 记录不可写", ("状态不可用",)),
    # 锁忙
    ("状态锁不可用，已降级放行", ("锁忙",)),
    ("状态锁不可用，SKILL 读取恢复降级", ("锁忙",)),
    # Git 查询失败：只匹配两条固定文案，旧带路径的超时模板不再归类
    ("Git 状态查询失败，基线检查降级", ("Git 查询失败",)),
    ("基线指纹查询超时，基线检查降级", ("Git 查询失败",)),
    # 旧的带路径超时模板已不存在于 core，且不得再归入 Git 类别（只走隐藏兜底）
    ("基线指纹查询超时（/tmp/STATE-ROOT-SENTINEL/rel），基线检查降级", ()),
    # 命令结构未覆盖
    ("reviewer 命令结构未覆盖，已放行并披露", ("命令结构未覆盖",)),
    ("命令结构无法完整解析，基线检查未覆盖", ("命令结构未覆盖",)),
    ("命令结构无法完整解析", ("命令结构未覆盖",)),
    # 写入目标未覆盖
    ("缺少可定位的仓库根或 cwd，基线检查降级", ("写入目标未覆盖",)),
    ("写入目标不完整，基线检查未覆盖", ("写入目标未覆盖",)),
    ("写入目标位于仓库外，未纳入基线检查", ("写入目标未覆盖",)),
    ("命令写入目标不完整", ("写入目标未覆盖",)),
    ("命令内目录变化无法确定，基线检查未覆盖", ("写入目标未覆盖",)),
    ("补丁目标解析不完整", ("写入目标未覆盖",)),
    ("宿主未提供结构化非 shell 参数，无法核对写入目标", ("写入目标未覆盖",)),
    ("写工具缺少可识别路径字段", ("写入目标未覆盖",)),
    ("已有任务状态缺少任务根归属索引：无法识别历史任务根的旧归属，按降级处理", ("写入目标未覆盖",)),
    # 写序列记账缺口：以 post 开头的记账文案只归本类
    ("post 缺少可信成功结果，未计入成功写入", ("写序列记账缺口",)),
    ("缺少工具 ID，写事件无法幂等记账", ("写序列记账缺口",)),
    ("post 未找到与该 actor/epoch/tool ID 配对的 pre 记录，未建立成功豁免", ("写序列记账缺口",)),
    ("post 写入目标不完整，写序列记覆盖缺口", ("写序列记账缺口",)),
    ("post 目标含符号链接，未建立成功豁免", ("写序列记账缺口",)),
    ("post 目标指纹不可信，未建立成功豁免", ("写序列记账缺口",)),
    ("post 目标与 pre 记录的调用目标不一致（含仓库身份），未建立成功豁免", ("写序列记账缺口",)),
    ("post 目标缺少 pre 指纹，未建立成功豁免", ("写序列记账缺口",)),
    ("锁内复核发现任务绑定已变化，丢弃锁外预取并降级本次写入判定", ("写序列记账缺口",)),
    ("锁外预取结果与当前任务绑定不一致，已降级", ("写序列记账缺口",)),
    # transcript 证据降级：身份描述取当前生产者值域（会话/线程/actor）
    ("transcript 不是普通文件，激活证据抽取降级", ("transcript 证据降级",)),
    ("transcript 身份核验失败，激活判定降级", ("transcript 证据降级",)),
    ("transcript 原生会话身份与当前会话不一致，未采用该 transcript 证据", ("transcript 证据降级",)),
    ("transcript 原生线程身份与当前会话不一致，未采用该 transcript 证据", ("transcript 证据降级",)),
    ("transcript 原生 actor 身份与当前会话不一致，未采用该 transcript 证据", ("transcript 证据降级",)),
    ("transcript 缺原生会话身份，激活证据只按记录内容判定", ("transcript 证据降级",)),
    ("transcript 证据抽取失败，激活判定降级", ("transcript 证据降级",)),
    ("transcript 读取证据抽取失败，激活判定降级", ("transcript 证据降级",)),
    ("transcript 输入取证失败，激活判定降级", ("transcript 证据降级",)),
    ("无法从 transcript 取得最新用户记录，激活判定降级", ("transcript 证据降级",)),
    ("该宿主无 transcript 能力，激活证据只按原始输入判定", ("transcript 证据降级",)),
    # 分派/子代理降级：终态只取 CHILD_FAILURE_STATUSES 内的固定值
    ("子代理未匹配到有效分派记录（或旧 epoch 已退役），只保留局部角色约束", ("分派/子代理降级",)),
    ("子代理绑定与 root 当前 epoch 不匹配，已降级为局部只读约束", ("分派/子代理降级",)),
    ("子代理启动未能匹配可信分派记录，未建立跨任务绑定", ("分派/子代理降级",)),
    ("子代理完成缺少可关联分派，未计入审查完成", ("分派/子代理降级",)),
    # 终态文案的动态值域就是 core 的 CHILD_FAILURE_STATUSES：逐值一条，不只举 failed。
    *[
        ("子代理终态为 %s，未计入审查完成" % status, ("分派/子代理降级",))
        for status in sorted(core_module.CHILD_FAILURE_STATUSES)
    ],
    ("分派预取失败，模型预检降级", ("分派/子代理降级",)),
    ("分派预检的锁外取证与当前上下文不一致，模型预检降级", ("分派/子代理降级",)),
    ("同一分派 ID 的内容或角色已变化，该段观测失效且不重新取样", ("分派/子代理降级",)),
    ("该分派 ID 的观测已因内容变化失效，完成事件不计入", ("分派/子代理降级",)),
    ("宿主未提供 reviewer 交接正文，交接格式检查降级放行", ("分派/子代理降级",)),
    ("该宿主无法可靠归因审查完成事件，P3 完成不启用", ("分派/子代理降级",)),
    ("该宿主无法可靠归因审查完成事件，P3 不启用", ("分派/子代理降级",)),
    # 激活判定降级
    ("同一工具调用 ID 的成功读取内容已变化，该调用观测作废", ("激活判定降级",)),
    ("SKILL 读取缺少工具 ID，重复投递无法按调用 ID 去重", ("激活判定降级",)),
    ("缺少 root 绑定，SKILL 读取恢复降级", ("激活判定降级",)),
    ("同一 SKILL 读取调用已消费，重复投递不恢复激活", ("激活判定降级",)),
    ("SKILL 读取恢复的 root 绑定发生变化，丢弃本次恢复", ("激活判定降级",)),
    ("输入取证的签名在锁内复核时已变化，丢弃该取证并降级", ("激活判定降级",)),
    # 工具权限未覆盖
    ("未知自定义工具的写权限未推断，已放行并披露", ("工具权限未覆盖",)),
    # 输入或协议无效
    ("协议字段类型不符（workspacePaths）；已放行", ("输入或协议无效",)),
    ("输入超过上限；已放行", ("输入或协议无效",)),
    ("输入不是合法 JSON；已放行", ("输入或协议无效",)),
    ("输入不是 JSON 对象；已放行", ("输入或协议无效",)),
    ("未知事件，已放行", ("输入或协议无效",)),
    ("缺少命令文本", ("输入或协议无效",)),
    ("Antigravity 提供 2 个 workspacePaths，按第一个可用路径判定工作区", ("输入或协议无效",)),
    # 内部异常
    ("hook 内部异常；已放行", ("内部异常",)),
    # 多类同时命中：一条汇总内按 §7 顺序输出且不重复
    (
        "状态写失败，已保留本次判定并降级；写入目标位于仓库外，未纳入基线检查；"
        "post 未找到与该 actor/epoch/tool ID 配对的 pre 记录，未建立成功豁免",
        ("状态不可用", "写入目标未覆盖", "写序列记账缺口"),
    ),
    # 已知与未知混合：只输出已知类别；纯未知走隐藏兜底
    ("状态写失败，已保留本次判定并降级；UNCLASSIFIED-SENTINEL 未知诊断", ("状态不可用",)),
    ("UNCLASSIFIED-SENTINEL 不匹配任何固定类别", ()),
    # 无合法诊断边界、只在正文里出现类别关键词：不得归类，只走隐藏兜底。
    # 实现必须把每个类别族整体包成非捕获组，不能只给第一支加边界。
    ("UNCLASSIFIED-SENTINEL 状态写失败", ()),
    ("UNCLASSIFIED-SENTINEL 写入目标不完整，基线检查未覆盖", ()),
    ("UNCLASSIFIED-SENTINEL 内部异常", ()),
)

# 按调用顺序逐条输出 catalog 文案的替身核心：每次调用一条 stderr 汇总。
# 文案以 ASCII 转义的 JSON 引入：本机 Python 3.9 的 tokenizer 在含非 ASCII 的长行
# （超过 1024 字节块边界）上会报“Non-UTF-8 code”，这种写法对长短都稳定。
CATALOG_STUB = '''import json
import os
import sys

MESSAGES = json.loads(__MESSAGES__)
CURSOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog-cursor")
try:
    index = int(open(CURSOR).read())
except (OSError, ValueError):
    index = 0
open(CURSOR, "w").write(str(index + 1))
sys.stderr.write("warning: " + MESSAGES[index] + "\\n")
print("{}")
'''


def catalog_summary(labels: tuple) -> str:
    """catalog 期望的汇总文案：无类别时是隐藏兜底，否则是固定类别列表。"""
    return HIDDEN_STDERR if not labels else "core 降级：" + "；".join(labels)


class InstallTests(HookCase):
    host = "codex"

    def generate(self, host: str = "codex", output: Path | None = None, mode: str = "probe", root: Path | None = None) -> dict:
        output = output or (self.root / "generated")
        completed = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "generate",
                "--host",
                host,
                "--plugin-root",
                str(root or HOOKS_ROOT / "hooks"),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                mode,
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def test_generate_writes_only_output_dir(self) -> None:
        before = sorted(path.name for path in self.root.iterdir())
        result = self.generate("codex", mode="enforce")
        output = Path(result["output"])
        assert output.is_dir()
        context = json.loads((output / "install-context.json").read_text(encoding="utf-8"))
        assert context["host"] == "codex"
        assert context["mode"] == "enforce"
        release = json.loads(
            (HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8")
        )["release"]
        assert context["version"] == release
        assert context["skill_root"] == str(self.skill)
        assert Path(context["plugin_root"]) == (HOOKS_ROOT / "hooks")
        manifest = json.loads((output / "install-manifest.json").read_text(encoding="utf-8"))
        assert not manifest["install_skill"]
        assert not manifest["install_tests"]
        files = " ".join(manifest["files"])
        assert "tests/" not in files
        assert "SKILL.md" not in files
        assert "references/" not in files
        assert "hooks/core.py".replace("hooks/", "") in manifest["files"]
        # 清单条目带完整标识/摘要，apply/uninstall 不再靠路径子串。
        assert manifest["entries"]
        for item in manifest["entries"]:
            assert item["identity"]
            assert item["digest"]
            assert "--context" in item["command"]
        # 只在生成目录内产生文件，不改宿主配置。
        for name in ("codex-hooks.json", "install-context.json", "install-manifest.json"):
            assert (output / name).exists(), name

    def test_command_is_absolute_and_safely_quoted(self) -> None:
        output = self.root / "out dir'with$dollar`tick"
        result = self.generate("codex", output=output)
        command = result["command"]
        assert os.path.isabs(command.split(" ")[0])
        assert str(output / "install-context.json").replace("'", "'\"'\"'") in command
        config = json.loads((output / "codex-hooks.json").read_text(encoding="utf-8"))
        hook_command = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert hook_command == command

    def test_all_hosts_generate_declared_events(self) -> None:
        for host in ("antigravity", "codex", "dsh"):
            result = self.generate(host)
            config = json.loads(Path(result["config"]).read_text(encoding="utf-8"))
            # Antigravity 用命名 Hook 组，Codex/DSH 用顶层 hooks。
            group = ANTIGRAVITY_GROUP if host == "antigravity" else "hooks"
            assert group in config
            buckets = config[group]
            for event in ("SessionStart", "PreToolUse", "PostToolUse", "Stop"):
                assert event in buckets, host
            text = Path(result["config"]).read_text(encoding="utf-8")
            assert "additionalContext" not in text
            assert "overwrite" not in text
        plugin = self.copied_plugin("plugin-pi")
        pi = self.generate("pi", root=plugin)
        assert pi["config"] is None
        manifest = json.loads(Path(pi["manifest"]).read_text(encoding="utf-8"))
        assert manifest["extension_entry"].endswith("index.ts")
        # pi 的 context 必须落在扩展实际读取的位置，并记录绝对解释器。
        plugin_context = Path(pi["plugin_context"])
        assert plugin_context == (plugin / "install-context.json")
        value = json.loads(plugin_context.read_text(encoding="utf-8"))
        assert value["python"] == os.path.realpath(sys.executable)

    def test_apply_is_idempotent_preserves_others_and_uninstall(self) -> None:
        result = self.generate("codex")
        source = result["config"]
        manifest = result["manifest"]
        target = self.root / "hooks.json"
        other = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": ".*", "hooks": [{"type": "command", "command": "other-hook"}]}
                ]
            },
            "custom": {"keep": True},
        }
        target.write_text(json.dumps(other), encoding="utf-8")
        first = self.run_install(["apply", "--target", str(target), "--source", source])
        assert first["changed"]
        merged = json.loads(target.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for entry in merged["hooks"]["PreToolUse"]
            for hook in entry.get("hooks", [])
        ]
        assert "other-hook" in commands
        assert any("core.py" in command for command in commands)
        assert merged["custom"] == {"keep": True}
        backup = Path(first["backup"])
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == json.dumps(other)
        second = self.run_install(["apply", "--target", str(target), "--source", source])
        assert not second["changed"]

        removed = self.run_install(["uninstall", "--target", str(target), "--manifest", manifest])
        assert removed["changed"]
        after = json.loads(target.read_text(encoding="utf-8"))
        assert after["hooks"]["PreToolUse"] == other["hooks"]["PreToolUse"]
        assert after["custom"] == {"keep": True}
        again = self.run_install(["uninstall", "--target", str(target), "--manifest", manifest])
        assert not again["changed"]

    def test_apply_conflict_and_unsafe_target(self) -> None:
        result = self.generate("codex")
        target = self.root / "hooks.json"
        conflict = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": str(HOOKS_ROOT / "hooks" / "core.py") + " --old",
                            }
                        ],
                    }
                ]
            }
        }
        target.write_text(json.dumps(conflict), encoding="utf-8")
        failed = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "apply",
                "--target",
                str(target),
                "--source",
                result["config"],
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
        assert failed.returncode != 0
        assert "同标识" in failed.stderr

        link = self.root / "link.json"
        link.symlink_to(target)
        failed = subprocess.run(
            [sys.executable, str(INSTALL), "apply", "--target", str(link), "--source", result["config"]],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
        assert failed.returncode != 0

    # ---- 工具 ------------------------------------------------------
    def copied_plugin(self, name: str = "plugin-rand0m") -> Path:
        """按 manifest.distribution.files 构建独立 Plugin 目录（随机名）。"""
        manifest = json.loads((HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8"))
        plugin = self.root / name
        for relative in manifest["distribution"]["files"]:
            source = HOOKS_ROOT / "hooks" / relative
            destination = plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return plugin

    def run_install(self, args: list) -> dict:
        completed = subprocess.run(
            [sys.executable, str(INSTALL)] + args,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def install_failure(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(INSTALL)] + args,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=30,
        )

    def commands_for(self, target: Path, event: str) -> list:
        config = json.loads(target.read_text(encoding="utf-8"))
        group = config.get(ANTIGRAVITY_GROUP) if isinstance(config.get(ANTIGRAVITY_GROUP), dict) else config.get("hooks", {})
        bucket = group.get(event) or []
        return [
            hook["command"] for entry in bucket for hook in (entry.get("hooks") or [entry])
            if isinstance(hook, dict) and isinstance(hook.get("command"), str)
        ]


# `generate --host pi` 会把 `install-context.json` 写回 `--plugin-root`（含源码目录），
# `.DS_Store` 是 macOS 目录残留：两者都不是分发源，也不能因此让清单比较变空转。
GENERATED_ARTIFACTS = frozenset({"install-context.json", ".DS_Store"})


def distribution_source_files(source_root: Path, excluded_top: set) -> list:
    """源分发目录里的正式文件集（相对路径，已排序）。"""
    actual = []
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root).as_posix()
        if relative.split("/")[0] in excluded_top:
            continue
        if "__pycache__" in relative or relative.endswith(".pyc"):
            continue
        if path.name in GENERATED_ARTIFACTS:
            continue
        actual.append(relative)
    return sorted(actual)


class DistributionManifestTests:
    """P3-4：源分发目录的正式文件集必须与 manifest.files 完全一致，防止清单再漏文件。"""

    def test_manifest_lists_exactly_the_distributed_files(self) -> None:
        manifest = json.loads((HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8"))
        files = manifest["distribution"]["files"]
        # 防真空转：清单必须仍是那个完整分发集，不能靠“两边都排除成空”通过。
        for required in (
            "README.md",
            "manifest.json",
            "__init__.py",
            "core.py",
            "index.ts",
            "pi/index.ts",
        ):
            assert required in files
        actual = distribution_source_files(
            HOOKS_ROOT / "hooks", set(manifest.get("excludes") or [])
        )
        assert actual == sorted(files)

    def test_generated_and_system_files_are_not_distribution_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "core.py").write_text("", encoding="utf-8")
            (root / "pi").mkdir()
            (root / "pi" / "index.ts").write_text("", encoding="utf-8")
            (root / "install-context.json").write_text("{}", encoding="utf-8")
            (root / ".DS_Store").write_bytes(b"")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "core.cpython-39.pyc").write_bytes(b"")
            (root / "tests").mkdir()
            (root / "tests" / "test_x.py").write_text("", encoding="utf-8")
            # 排除只针对生成物/系统文件/非分发目录，正式文件仍全部保留。
            assert distribution_source_files(root, {"tests"}) == ["core.py", "pi/index.ts"]


class DistributionLaunchTests(HookCase):
    """F04：只用 manifest 分发集构建随机命名 Plugin，从仓库外 cwd 启动。"""

    host = "pi"

    def test_distributed_plugin_runs_core_and_installer_outside_repo(self) -> None:
        manifest = json.loads((HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8"))
        plugin = self.root / "plugin-x9f2"
        for relative in manifest["distribution"]["files"]:
            destination = plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOOKS_ROOT / "hooks" / relative, destination)
        assert (plugin / "manifest.json").is_file()
        assert not (plugin / "hooks").exists()

        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        payload = {
            "event": "input",
            "sessionId": "s1",
            "prompt": "使用 easydev，完成这个改动",
            "cwd": str(self.repo),
        }
        completed = subprocess.run(
            [sys.executable, str(plugin / "core.py"), "--host", "pi", "--context", str(self.context_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(elsewhere),
            env=clean_env(),
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == {"action": "continue"}
        assert "No module named 'hooks'" not in completed.stderr

        output = self.root / "generated"
        generated = subprocess.run(
            [
                sys.executable,
                str(plugin / "install.py"),
                "generate",
                "--host",
                "codex",
                "--plugin-root",
                str(plugin),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                "enforce",
            ],
            capture_output=True,
            text=True,
            cwd=str(elsewhere),
            env=clean_env(),
            timeout=60,
        )
        assert generated.returncode == 0, generated.stderr
        assert (output / "codex-hooks.json").is_file()
        installed = json.loads((output / "install-manifest.json").read_text(encoding="utf-8"))
        assert "core.py" in " ".join(installed["files"])
        assert str(installed["plugin_root"]).startswith(str(plugin))


class PiExtensionTests(HookCase):
    """F05：按文档复制 Plugin 树 + generate 后，pi 适配器调真实 core 并阻断。"""

    host = "pi"

    def build(self, name: str = "easydev-hooks") -> Path:
        manifest = json.loads((HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8"))
        plugin = self.root / name
        for relative in manifest["distribution"]["files"]:
            destination = plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOOKS_ROOT / "hooks" / relative, destination)
        output = self.root / "generated"
        completed = subprocess.run(
            [
                sys.executable,
                str(plugin / "install.py"),
                "generate",
                "--host",
                "pi",
                "--plugin-root",
                str(plugin),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                "enforce",
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        context_path = plugin / "install-context.json"
        source = plugin / "pi" / "index.ts"
        assert context_path.is_file()
        assert "shell" not in source.read_text(encoding="utf-8")
        return plugin

    def test_extension_entry_calls_real_core_and_blocks(self) -> None:
        node = shutil.which("node")
        if not node:
            pytest.skip("node 不可用，无法运行 pi 适配夹具")
        plugin = self.build()
        context_path = plugin / "install-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        # 安装上下文本就可显式选择同等私有状态根；夹具用它避免落到共享临时根。
        context.update({"state_root": str(self.state), "repo_root": str(self.repo)})
        context_path.write_text(json.dumps(context), encoding="utf-8")

        spec = {
            "entry": str(plugin / "index.ts"),
            "plugin": str(plugin),
            "repo": str(self.repo),
            "session": "pi-session",
            "activation": "使用 easydev 执行只读 review，不要修改代码",
            "calls": [
                {
                    "toolName": "write",
                    "toolCallId": "w1",
                    "input": {"path": str(self.repo / "src" / "app.py"), "content": "x"},
                },
                {"toolName": "bash", "toolCallId": "t1", "input": {"command": "git reset --hard"}},
                {"toolName": "bash", "toolCallId": "t2", "input": {"command": "git status --short"}},
            ],
        }
        completed = subprocess.run(
            [node, str(FIXTURE), json.dumps(spec)],
            capture_output=True,
            text=True,
            cwd=str(self.root),
            # 清空 PATH：适配器若回落 PATH 里的 python3 就起不来核心，从而暴露问题。
            env=clean_env({"PATH": "/nonexistent"}),
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        value = json.loads(completed.stdout)
        assert "tool_call" in value["handlers"]
        write_result, reset_result, status_result = value["results"]
        # 只读 review：写工具被真实核心拒绝。
        assert write_result["value"]["block"] is True
        # 破坏性 Git：真实核心拒绝。
        assert reset_result["value"]["block"] is True
        # 只读命令放行。
        assert status_result["value"] is None
        # 核心确实运行过：私有状态已由本次调用写出。
        assert any((self.state / "actors").glob("*.json"))
        # 判定不变，告警通道也不影响返回值：每次工具调用至多一条汇总，且不含细节。
        # 本用例刻意清空 PATH，Git 不可用：三次 pre 都在取目标前因仓库定位降级告警，
        # 归入“写入目标未覆盖”，而不是命令结构缺口或目标的 Git 状态查询。
        lines = self.warning_lines(completed.stderr)
        assert len(lines) == 3, completed.stderr
        for line in lines:
            assert line == "[easydev-hooks] warning: core 降级：写入目标未覆盖"
        self.assert_no_detail_leak(completed.stderr, "git reset --hard", "git status")

    # ---- P2-10：降级告警通道 --------------------------------------------
    def warning_lines(self, stderr: str) -> list:
        return [
            line for line in stderr.splitlines() if line.startswith("[easydev-hooks] warning")
        ]

    def run_fixture(
        self,
        plugin: Path,
        calls: list,
        transcript: str = "",
        context_overrides: dict | None = None,
    ) -> subprocess.CompletedProcess:
        node = shutil.which("node")
        if not node:
            pytest.skip("node 不可用，无法运行 pi 适配夹具")
        context_path = plugin / "install-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context.update({"state_root": str(self.state), "repo_root": str(self.repo)})
        context.update(context_overrides or {})
        context_path.write_text(json.dumps(context), encoding="utf-8")
        spec = {
            "entry": str(plugin / "index.ts"),
            "plugin": str(plugin),
            "repo": str(self.repo),
            "session": "pi-session",
            "activation": "使用 easydev 执行只读 review，不要修改代码",
            "transcript": transcript,
            "calls": calls,
        }
        return subprocess.run(
            [node, str(FIXTURE), json.dumps(spec)],
            capture_output=True,
            text=True,
            cwd=str(self.root),
            # 清空 PATH：适配器若回落 PATH 里的 python3 就起不来核心，从而暴露问题。
            env=clean_env({"PATH": "/nonexistent"}),
            timeout=120,
        )

    def assert_no_detail_leak(self, stderr: str, *sentinels: str) -> None:
        """告警只许出现类别与固定文案：路径、解释器、payload、stack 都不许进 stderr。"""
        for sentinel in sentinels:
            assert sentinel not in stderr
        assert str(self.root) not in stderr
        assert "core.py" not in stderr
        assert "Traceback" not in stderr

    def test_spawn_failure_warns_once_per_call_and_still_allows(self) -> None:
        plugin = self.build()
        sentinel = self.root / "PY-SENTINEL"
        completed = self.run_fixture(
            plugin,
            [{"toolName": "bash", "toolCallId": "t1", "input": {"command": "git reset --hard"}}],
            context_overrides={"python": str(sentinel / "python3")},
        )
        assert completed.returncode == 0, completed.stderr
        value = json.loads(completed.stdout)
        # 无法启动核心：仍然 fail-open 放行（高危 Git 也不阻断），不冒充“已核对”。
        assert value["results"][0]["value"] is None
        lines = self.warning_lines(completed.stderr)
        # input 与 tool_call 各一次核心调用，每次一条汇总。
        assert len(lines) == 2, completed.stderr
        for line in lines:
            assert line.count("[easydev-hooks] warning") == 1, line
            assert "core 启动失败，已放行" in line
        self.assert_no_detail_leak(completed.stderr, "PY-SENTINEL")

    def test_transcript_diagnostics_name_only_the_category(self) -> None:
        plugin = self.build()
        # transcript 指向目录：核心给出“不是普通文件”的降级诊断，归属 transcript 类别。
        transcript = self.root / "TRANSCRIPT-SENTINEL"
        transcript.mkdir()
        completed = self.run_fixture(plugin, [], transcript=str(transcript))
        assert completed.returncode == 0, completed.stderr
        lines = self.warning_lines(completed.stderr)
        assert len(lines) == 1, completed.stderr
        # 固定类别名本身含 transcript 字样，但核心原句与路径不得回显。
        assert lines[0] == "[easydev-hooks] warning: core 降级：transcript 证据降级"
        assert "不是普通文件" not in completed.stderr
        self.assert_no_detail_leak(completed.stderr, "TRANSCRIPT-SENTINEL")

    def test_unknown_core_diagnostics_stay_hidden(self) -> None:
        plugin = self.build_with_core(
            "import sys\n"
            "sys.stderr.write('warning: UNCLASSIFIED-SENTINEL 未知降级文本\\n')\n"
            "print('{}')\n"
        )
        completed = self.run_fixture(
            plugin, [{"toolName": "bash", "toolCallId": "t1", "input": {"command": "ls"}}]
        )
        assert completed.returncode == 0, completed.stderr
        # 完全不匹配的新文案：保留“有诊断”兜底，不回显任何原文。
        assert self.warning_lines(completed.stderr) == (["[easydev-hooks] warning: " + HIDDEN_STDERR] * 2), completed.stderr
        self.assert_no_detail_leak(completed.stderr, "UNCLASSIFIED-SENTINEL", "未知降级文本")

    def test_forged_separators_only_leak_fixed_labels(self) -> None:
        """替身伪造终态值与分隔符：只证明文本协议理论上可以多命中类别。

        真实 core 的终态取自 CHILD_FAILURE_STATUSES、身份描述只取“会话/线程/actor”，
        都不能注入全角分号；这条用例不得被当作真实 core 可达诊断，真实可达性另由
        `CoreDiagnosticProducerTests` 验收。
        """
        forged = "warning: 子代理终态为 failed；状态写失败，已保留本次判定并降级 FORGED-STATUS-SENTINEL\n"
        plugin = self.build_with_core(
            "import sys\nsys.stderr.write(%r)\nprint('{}')\n" % forged
        )
        completed = self.run_fixture(
            plugin, [{"toolName": "bash", "toolCallId": "t1", "input": {"command": "ls"}}]
        )
        assert completed.returncode == 0, completed.stderr
        # 伪造的分隔符让两个类别各命中一次：汇总仍只有固定 label，不回显原句。
        # input 与 tool_call 各一次核心调用，每次一条汇总。
        assert self.warning_lines(completed.stderr) == (["[easydev-hooks] warning: core 降级：状态不可用；分派/子代理降级"] * 2), completed.stderr
        self.assert_no_detail_leak(completed.stderr, "FORGED-STATUS-SENTINEL")

    def build_with_core(self, source: str) -> Path:
        """用替身核心驱动适配器故障分支：真实 spawn/pipe/close 语义不变。"""
        plugin = self.build()
        (plugin / "core.py").write_text(source, encoding="utf-8")
        return plugin

    def test_nonzero_exit_does_not_trust_stdout(self) -> None:
        plugin = self.build_with_core(
            "import json, sys\n"
            "print(json.dumps({'block': True, 'reason': 'STUB-DENY'}))\n"
            "sys.stdout.flush()\n"
            "sys.exit(1)\n"
        )
        completed = self.run_fixture(
            plugin, [{"toolName": "bash", "toolCallId": "t1", "input": {"command": "ls"}}]
        )
        assert completed.returncode == 0, completed.stderr
        value = json.loads(completed.stdout)
        # 非 0 退出的 stdout 不可信：不返回 block，实际放行。
        assert value["results"][0]["value"] is None
        lines = self.warning_lines(completed.stderr)
        assert len(lines) == 2, completed.stderr  # input + tool_call
        for line in lines:
            assert "core 非 0 退出（1），已放行" in line
        assert "STUB-DENY" not in completed.stderr

    def test_empty_or_non_object_output_is_disclosed(self) -> None:
        cases = (
            ("import sys\nsys.exit(0)\n", "core 无输出，已放行"),
            ("print('null')\n", "core 输出不是 JSON 对象，已放行"),
            ("print('[]')\n", "core 输出不是 JSON 对象，已放行"),
            ("print('0')\n", "core 输出不是 JSON 对象，已放行"),
            ("print('not-json')\n", "core 输出无效 JSON，已放行"),
        )
        for source, expected in cases:
            plugin = self.build()
            (plugin / "core.py").write_text(source, encoding="utf-8")
            completed = self.run_fixture(
                plugin, [{"toolName": "bash", "toolCallId": "t1", "input": {"command": "ls"}}]
            )
            assert completed.returncode == 0, completed.stderr
            value = json.loads(completed.stdout)
            assert value["results"][0]["value"] is None, source
            assert (self.warning_lines(completed.stderr).count(expected) if False else len(
                [line for line in self.warning_lines(completed.stderr) if expected in line]
            )) == 2, (source, completed.stderr)

    def test_valid_empty_object_stays_silent(self) -> None:
        plugin = self.build_with_core("print('{}')\n")
        completed = self.run_fixture(
            plugin, [{"toolName": "bash", "toolCallId": "t1", "input": {"command": "ls"}}]
        )
        assert completed.returncode == 0, completed.stderr
        value = json.loads(completed.stdout)
        assert value["results"][0]["value"] is None
        assert self.warning_lines(completed.stderr) == [], completed.stderr

    def test_whitelisted_core_degradation_names_only_the_category(self) -> None:
        plugin = self.build()
        blocked_root = self.root / "STATE-ROOT-SENTINEL"
        blocked_root.write_text("x\n", encoding="utf-8")
        completed = self.run_fixture(
            plugin, [], context_overrides={"state_root": str(blocked_root)}
        )
        assert completed.returncode == 0, completed.stderr
        lines = self.warning_lines(completed.stderr)
        assert len(lines) == 1, completed.stderr
        # 只给完整固定汇总：类别名，不含核心原句与状态根路径。
        assert lines[0] == "[easydev-hooks] warning: core 降级：状态不可用"
        assert "私有状态不可用" not in completed.stderr
        self.assert_no_detail_leak(completed.stderr, "STATE-ROOT-SENTINEL")

    def test_lock_diagnostics_never_double_as_state_unavailable(self) -> None:
        plugin = self.build_with_core(
            "import sys\n"
            "sys.stderr.write('warning: 状态锁不可用，已降级放行\\n')\n"
            "print('{}')\n"
        )
        completed = self.run_fixture(
            plugin, [{"toolName": "bash", "toolCallId": "t1", "input": {"command": "ls"}}]
        )
        assert completed.returncode == 0, completed.stderr
        assert self.warning_lines(completed.stderr) == (["[easydev-hooks] warning: core 降级：锁忙"] * 2), completed.stderr
        # 锁故障只归“锁忙”：不得同时出现“状态不可用”。
        assert "状态不可用" not in completed.stderr

    def test_core_diagnostic_catalog_maps_fixed_categories(self) -> None:
        plugin = self.build()
        messages = [message for message, _ in CORE_DIAGNOSTIC_CATALOG]
        (plugin / "core.py").write_text(
            CATALOG_STUB.replace("__MESSAGES__", repr(json.dumps(messages))), encoding="utf-8"
        )
        # 夹具的 input 调用先消耗第 0 条文案，tool_call 数比 catalog 少 1，
        # 因此总调用数正好等于文案条数，第 i 行对应第 i 条。
        calls = [
            {"toolName": "bash", "toolCallId": "c%d" % index, "input": {"command": "ls"}}
            for index in range(len(messages) - 1)
        ]
        completed = self.run_fixture(plugin, calls)
        assert completed.returncode == 0, completed.stderr
        lines = self.warning_lines(completed.stderr)
        assert len(lines) == len(messages), completed.stderr
        for index, (message, labels) in enumerate(CORE_DIAGNOSTIC_CATALOG):
            assert lines[index] == ("[easydev-hooks] warning: " + catalog_summary(labels))
        self.assert_no_detail_leak(completed.stderr, "UNCLASSIFIED-SENTINEL", "STATE-ROOT-SENTINEL")


class CoreDiagnosticProducerTests(HookCase):
    """真实 core 的生产者值域：与替身能伪造的文本协议限制分开验收。

    design §7/§8.4：终态只取 CHILD_FAILURE_STATUSES、身份描述只取“会话/线程/actor”，
    这两个动态字段都不能注入全角分号或 `warning:`，因此真实可达诊断不会被伪造原文。
    """

    host = "codex"

    def test_child_failure_statuses_are_the_frozen_domain(self) -> None:
        assert core_module.CHILD_FAILURE_STATUSES == {
            "failed",
            "failure",
            "error",
            "cancelled",
            "canceled",
            "timeout",
            "timed_out",
            "aborted",
            "interrupted",
            "running",
            "started",
            "pending",
            "stopped_early",
        }

    def record_child_done(self, session: str, status: str):
        dispatch = "d-" + session
        self.activate(session)
        self.allow(self.dispatch_payload(session, self.reviewer_handoff(), tool_id=dispatch))
        self.allow(
            self.child_start_payload("agent-" + session, session=session, dispatch_id=dispatch)
        )
        return self.run_core(
            self.child_done_payload(
                session,
                dispatch_id=dispatch,
                status=status,
                report="**[P1] 证据**\n",
                agent="agent-" + session,
            )
        )

    @pytest.mark.parametrize("status", sorted(core_module.CHILD_FAILURE_STATUSES))
    def test_every_child_failure_status_produces_its_own_terminal_warning(self, status) -> None:
        _code, value, stderr = self.record_child_done("p-" + status, status)
        assert not self.is_denied(value), value
        assert ("子代理终态为 %s，未计入审查完成" % status) in stderr

    def test_non_set_status_never_produces_the_terminal_warning(self) -> None:
        # 含全角分号的非集合 status：真实 core 不产生该条终态告警，也不会把
        # `；状态写失败` 送进诊断通道。
        _code, value, stderr = self.record_child_done(
            "p-forged", "failed；状态写失败，已保留本次判定并降级"
        )
        assert not self.is_denied(value), value
        assert "子代理终态为" not in stderr
        assert "状态写失败" not in stderr

    def test_transcript_identity_reasons_are_bounded(self) -> None:
        engine = core_module.Engine(
            "codex",
            json.loads(self.context_path.read_text(encoding="utf-8")),
            mode="enforce",
            env=self.environment(),
        )
        record = core_module.Record("codex", {}, core_module.HOSTS["codex"])
        record.session_id = "s1"
        record.actor_id = ""
        path = self.root / "transcript.jsonl"
        for payload, expected in (
            ({"id": "other-session"}, ("会话", True)),
            ({"thread_id": "other-thread"}, ("线程", True)),
            ({"agent_id": "other-actor"}, ("actor", True)),
            ({}, ("", False)),
        ):
            path.write_text(
                json.dumps({"type": "session_meta", "payload": payload}) + "\n",
                encoding="utf-8",
            )
            assert engine._transcript_identity_problem(record, str(path)) == expected


class AntigravityNamedGroupTests(HookCase):
    """B：Antigravity 命名 Hook 组、逐事件 `--event`、apply/uninstall 只动本组。"""

    host = "antigravity"

    def setup_method(self) -> None:
        super().setup_method()
        self.target = self.root / "hooks.json"
        self.other = {
            "easydev-hooks": {
                "PreToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "other-hook"}]}
                ]
            },
            "user-hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "stop-hook"}]}]
            },
            "custom": {"keep": True},
        }
        self.target.write_text(json.dumps(self.other), encoding="utf-8")

    def generate(self, mode: str = "probe") -> dict:
        output = self.root / "generated"
        completed = subprocess.run(
            [
                sys.executable,
                str(INSTALL),
                "generate",
                "--host",
                "antigravity",
                "--plugin-root",
                str(HOOKS_ROOT / "hooks"),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                mode,
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def run_install(self, args: list) -> dict:
        completed = subprocess.run(
            [sys.executable, str(INSTALL)] + args,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def group(self) -> dict:
        return json.loads(self.target.read_text(encoding="utf-8"))[ANTIGRAVITY_GROUP]

    def test_generated_config_uses_named_group_and_per_event_command(self) -> None:
        result = self.generate("enforce")
        config = json.loads(Path(result["config"]).read_text(encoding="utf-8"))
        assert list(config) == [ANTIGRAVITY_GROUP]
        buckets = config[ANTIGRAVITY_GROUP]
        assert sorted(buckets) == ["PostToolUse", "PreInvocation", "PreToolUse", "SessionStart", "Stop"]
        for event, entries in buckets.items():
            # 官方两种结构：工具事件 Grouped（matcher + hooks[]），其余 Flat。
            if event in ("PreToolUse", "PostToolUse"):
                assert isinstance(entries[0].get("hooks"), list), event
                assert entries[0]["matcher"] == "*", event
                hook = entries[0]["hooks"][0]
            else:
                assert "hooks" not in entries[0], event
                hook = entries[0]
            assert hook["type"] == "command", event
            assert ("--event %s" % event) in hook["command"], event
            assert "--mode enforce" in hook["command"]
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        assert {item["group"] for item in manifest["entries"]} == {ANTIGRAVITY_GROUP}

    def test_apply_and_uninstall_touch_only_own_named_group(self) -> None:
        result = self.generate()
        applied = self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        assert applied["changed"]
        value = json.loads(self.target.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for entry in value[ANTIGRAVITY_GROUP]["PreToolUse"]
            for hook in entry.get("hooks", [])
        ]
        assert "other-hook" in commands
        easydev = [command for command in commands if "core.py" in command]
        assert len(easydev) == 1
        assert "--event PreToolUse" in easydev[0]
        assert value["user-hooks"] == self.other["user-hooks"]
        assert value["custom"] == {"keep": True}
        assert "hooks" not in value

        again = self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        assert not again["changed"]

        removed = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        assert removed["changed"]
        after = json.loads(self.target.read_text(encoding="utf-8"))
        # 用户自己的 easydev-hooks 条目保留（本组不清空），其他命名组原样。
        assert after[ANTIGRAVITY_GROUP] == self.other[ANTIGRAVITY_GROUP]
        assert after["user-hooks"] == self.other["user-hooks"]
        assert after["custom"] == {"keep": True}

    def test_flat_non_tool_events_apply_and_uninstall(self) -> None:
        result = self.generate("enforce")
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        group = self.group()
        for event in ("SessionStart", "PreInvocation", "Stop"):
            easydev = [entry for entry in group[event] if "core.py" in entry.get("command", "")]
            assert len(easydev) == 1, event
            assert "hooks" not in easydev[0], event
            assert ("--event %s" % event) in easydev[0]["command"], event
        self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        assert self.group() == self.other[ANTIGRAVITY_GROUP]

    def test_user_modified_flat_entry_is_preserved(self) -> None:
        result = self.generate()
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        value = json.loads(self.target.read_text(encoding="utf-8"))
        value[ANTIGRAVITY_GROUP]["Stop"][0]["command"] += " --user-flag"
        self.target.write_text(json.dumps(value), encoding="utf-8")
        self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        after = json.loads(self.target.read_text(encoding="utf-8"))
        stop = after[ANTIGRAVITY_GROUP]["Stop"]
        assert len(stop) == 1, stop
        assert stop[0]["command"].endswith("--user-flag"), stop

    def test_empty_own_group_is_removed_on_uninstall(self) -> None:
        target = self.root / "clean-hooks.json"
        target.write_text(json.dumps({"user-hooks": self.other["user-hooks"]}), encoding="utf-8")
        result = self.generate()
        self.run_install(["apply", "--target", str(target), "--source", result["config"]])
        value = json.loads(target.read_text(encoding="utf-8"))
        assert ANTIGRAVITY_GROUP in value
        self.run_install(["uninstall", "--target", str(target), "--manifest", result["manifest"]])
        after = json.loads(target.read_text(encoding="utf-8"))
        assert ANTIGRAVITY_GROUP not in after
        assert after["user-hooks"] == self.other["user-hooks"]

    def test_probe_to_enforce_replaces_in_named_group(self) -> None:
        probe = self.generate("probe")
        self.run_install(["apply", "--target", str(self.target), "--source", probe["config"]])
        enforce = self.generate("enforce")
        self.run_install(["apply", "--target", str(self.target), "--source", enforce["config"]])
        commands = [
            hook["command"]
            for entry in self.group()["PreToolUse"]
            for hook in entry.get("hooks", [])
            if "core.py" in hook.get("command", "")
        ]
        assert len(commands) == 1
        assert "--mode enforce" in commands[0]


class ApplyManifestTests(HookCase):
    """F23：安装清单驱动的 apply/uninstall。"""

    host = "codex"

    def setup_method(self) -> None:
        super().setup_method()
        self.target = self.root / "hooks.json"
        self.other = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": ".*", "hooks": [{"type": "command", "command": "other-hook"}]}
                ]
            },
            "custom": {"keep": True},
        }
        self.target.write_text(json.dumps(self.other), encoding="utf-8")

    def generate(self, plugin_root: Path, mode: str = "probe") -> dict:
        output = self.root / "generated"
        completed = subprocess.run(
            [
                sys.executable,
                str(plugin_root / "install.py"),
                "generate",
                "--host",
                "codex",
                "--plugin-root",
                str(plugin_root),
                "--skill-root",
                str(self.skill),
                "--output",
                str(output),
                "--mode",
                mode,
            ],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def copied_plugin(self, name: str = "plugin-rand0m") -> Path:
        manifest = json.loads((HOOKS_ROOT / "hooks" / "manifest.json").read_text(encoding="utf-8"))
        plugin = self.root / name
        for relative in manifest["distribution"]["files"]:
            destination = plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOOKS_ROOT / "hooks" / relative, destination)
        return plugin

    def run_install(self, args: list) -> dict:
        completed = subprocess.run(
            [sys.executable, str(INSTALL)] + args,
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def commands(self, event: str = "PreToolUse") -> list:
        value = json.loads(self.target.read_text(encoding="utf-8"))
        bucket = value.get("hooks", {}).get(event) or []
        return [
            hook["command"]
            for entry in bucket
            for hook in (entry.get("hooks") or [entry])
            if isinstance(hook, dict) and isinstance(hook.get("command"), str)
        ]

    def test_default_usage_needs_no_plugin_root(self) -> None:
        plugin = self.copied_plugin()
        result = self.generate(plugin)
        applied = self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        assert applied["changed"]
        commands = self.commands()
        assert "other-hook" in commands
        easydev = [command for command in commands if "core.py" in command]
        assert len(easydev) == 1
        assert "--mode probe" in easydev[0]
        assert json.loads(self.target.read_text(encoding="utf-8"))["custom"] == {"keep": True}

    def test_probe_to_enforce_replaces_instead_of_appending(self) -> None:
        plugin = self.copied_plugin()
        probe = self.generate(plugin, mode="probe")
        self.run_install(["apply", "--target", str(self.target), "--source", probe["config"]])
        enforce = self.generate(plugin, mode="enforce")
        self.run_install(["apply", "--target", str(self.target), "--source", enforce["config"]])
        commands = self.commands()
        assert len([command for command in commands if "core.py" in command]) == 1
        assert any("--mode enforce" in command for command in commands)
        assert not any("--mode probe" in command for command in commands)
        assert "other-hook" in commands

    def test_user_modified_entry_is_preserved_and_reported(self) -> None:
        plugin = self.copied_plugin()
        result = self.generate(plugin)
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        value = json.loads(self.target.read_text(encoding="utf-8"))
        hooks = value["hooks"]["PreToolUse"]
        target_entry = [entry for entry in hooks if "core.py" in json.dumps(entry)][0]
        target_entry["hooks"][0]["command"] = target_entry["hooks"][0]["command"] + " --user-edit"
        self.target.write_text(json.dumps(value), encoding="utf-8")
        modified = json.dumps(target_entry, sort_keys=True)

        failed = subprocess.run(
            [sys.executable, str(INSTALL), "apply", "--target", str(self.target), "--source", result["config"]],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert failed.returncode != 0
        assert "同标识" in failed.stderr
        assert modified in json.dumps(json.loads(self.target.read_text(encoding="utf-8")), sort_keys=True)

        removed = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        assert removed["changed"]
        # 用户修改过的条目必须保留，其余仍与清单一致的条目被移除。
        after = json.dumps(json.loads(self.target.read_text(encoding="utf-8")), sort_keys=True)
        assert modified in after
        assert len([command for command in self.commands() if "core.py" in command]) == 1
        again = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        assert not again["changed"]

    def test_plugin_root_with_special_characters(self) -> None:
        plugin = self.copied_plugin("plug in'with$dollar`tick")
        result = self.generate(plugin)
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        commands = self.commands()
        assert len([command for command in commands if "core.py" in command]) == 1
        removed = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        assert removed["changed"]
        assert self.commands() == ["other-hook"]

    def test_repeated_uninstall_is_idempotent(self) -> None:
        plugin = self.copied_plugin()
        result = self.generate(plugin)
        self.run_install(["apply", "--target", str(self.target), "--source", result["config"]])
        first = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        assert first["changed"]
        second = self.run_install(["uninstall", "--target", str(self.target), "--manifest", result["manifest"]])
        assert not second["changed"]
        value = json.loads(self.target.read_text(encoding="utf-8"))
        assert value["hooks"]["PreToolUse"] == self.other["hooks"]["PreToolUse"]
        assert value["custom"] == {"keep": True}


class GenerateLayoutAndPruneTests(HookCase):
    """1.4.3：默认 enforce、`--output-root` 稳定布局、旧版本生成目录清理。"""

    host = "codex"

    def _install(self, *args: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(INSTALL), *args],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def _generate(
        self,
        output: Path | None = None,
        output_root: Path | None = None,
        host: str = "codex",
        mode: str | None = None,
    ) -> dict:
        args = [
            "generate",
            "--host",
            host,
            "--plugin-root",
            str(HOOKS_ROOT / "hooks"),
            "--skill-root",
            str(self.skill),
        ]
        if output is not None:
            args += ["--output", str(output)]
        else:
            args += ["--output-root", str(output_root)]
        if mode is not None:
            args += ["--mode", mode]
        return self._install(*args)

    def _old_release(self, config_root: Path, version: str, host: str = "codex") -> Path:
        """模拟上一代 release 留下的生成目录：目录名与清单 release 一致。"""
        target = config_root / version / host
        self._generate(output=target, host=host, mode="enforce")
        manifest_path = target / "install-manifest.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["release"] = version
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        context_path = target / "install-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["version"] = version
        context_path.write_text(json.dumps(context), encoding="utf-8")
        return config_root / version

    def test_generate_defaults_to_enforce_and_uses_stable_layout(self) -> None:
        config_root = self.root / "config"
        result = self._generate(output_root=config_root)
        assert result["mode"] == "enforce"
        assert Path(result["output"]) == (config_root / "codex")
        assert result["stale"] == []
        context = json.loads((config_root / "codex" / "install-context.json").read_text(encoding="utf-8"))
        assert context["mode"] == "enforce"
        # 再次生成同一稳定目录：原地覆盖，不产生版本号目录。
        self._generate(output_root=config_root, mode="probe")
        assert sorted(item.name for item in config_root.iterdir()) == ["codex"]
        context = json.loads((config_root / "codex" / "install-context.json").read_text(encoding="utf-8"))
        assert context["mode"] == "probe"

    def test_generate_requires_exactly_one_output_target(self) -> None:
        base = [
            sys.executable,
            str(INSTALL),
            "generate",
            "--host",
            "codex",
            "--plugin-root",
            str(HOOKS_ROOT / "hooks"),
            "--skill-root",
            str(self.skill),
        ]
        both = subprocess.run(
            base + ["--output", str(self.root / "a"), "--output-root", str(self.root / "b")],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert both.returncode == 2
        neither = subprocess.run(base, capture_output=True, text=True, env=clean_env(), timeout=60)
        assert neither.returncode == 2
        assert not (self.root / "a").exists()

    def test_generate_reports_stale_release_dirs(self) -> None:
        config_root = self.root / "config"
        old = self._old_release(config_root, "0.9.0")
        result = self._generate(output_root=config_root)
        assert result["stale"] == [str(old)]

    def test_prune_dry_run_then_apply_keeps_current_and_keep(self) -> None:
        config_root = self.root / "config"
        old = self._old_release(config_root, "0.9.0")
        kept = self._old_release(config_root, "0.9.1")
        self._generate(output_root=config_root)

        dry = self._install("prune", "--root", str(config_root))
        assert dry["dry_run"]
        assert dry["removed"] == [os.path.realpath(old), os.path.realpath(kept)]
        assert (old.is_dir() and kept.is_dir())

        applied = self._install("prune", "--root", str(config_root), "--keep", "0.9.1", "--apply")
        assert not applied["dry_run"]
        assert applied["removed"] == [os.path.realpath(old)]
        assert not old.exists()
        assert kept.is_dir()
        assert "note" in applied
        # 当前 release 与稳定布局的宿主目录都不能被删。
        assert (config_root / "codex").is_dir()
        assert [item["reason"] for item in applied["skipped"]] == ["当前或显式保留的版本"]

    def test_prune_skips_user_content_symlinks_and_foreign_manifests(self) -> None:
        config_root = self.root / "config"
        notes = self._old_release(config_root, "0.8.0")
        (notes / "codex" / "NOTES.md").write_text("mine\n", encoding="utf-8")
        foreign = config_root / "0.8.1" / "codex"
        foreign.mkdir(parents=True)
        (foreign / "install-manifest.json").write_text(
            json.dumps({"name": "other-hooks", "release": "0.8.1"}), encoding="utf-8"
        )
        unexpected = config_root / "0.8.2"
        (unexpected / "notes").mkdir(parents=True)
        (unexpected / "notes" / "a.txt").write_text("x\n", encoding="utf-8")
        real = config_root / "real"
        (real / "codex").mkdir(parents=True)
        (real / "codex" / "install-manifest.json").write_text(
            json.dumps({"name": "easydev-hooks", "release": "0.8.3"}), encoding="utf-8"
        )
        (config_root / "0.8.3").symlink_to(real)

        result = self._install("prune", "--root", str(config_root), "--apply")
        assert result["removed"] == []
        reasons = " ".join(item["reason"] for item in result["skipped"])
        assert "未知文件" in reasons
        assert "不是本插件生成" in reasons
        assert "未知子目录" in reasons
        assert "软链" in reasons
        for path in (notes, config_root / "0.8.1", unexpected, config_root / "0.8.3"):
            assert os.path.lexists(path), path

    def test_prune_rejects_symlinked_root(self) -> None:
        real_root = self.root / "real-config"
        real_root.mkdir()
        link = self.root / "link-config"
        link.symlink_to(real_root)
        completed = subprocess.run(
            [sys.executable, str(INSTALL), "prune", "--root", str(link)],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert completed.returncode != 0
        assert "非软链" in completed.stderr

    def test_upgrade_never_overwrites_user_modified_entry(self) -> None:
        config_root = self.root / "config"
        target = self.root / "hooks.json"
        target.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        first = self._generate(output_root=config_root)
        self._install("apply", "--target", str(target), "--source", first["config"])
        value = json.loads(target.read_text(encoding="utf-8"))
        entry = [item for item in value["hooks"]["PreToolUse"] if "core.py" in json.dumps(item)][0]
        entry["hooks"][0]["command"] += " --user-edit"
        target.write_text(json.dumps(value), encoding="utf-8")
        edited = json.dumps(entry, sort_keys=True)

        # 升级 = 同一稳定目录重新生成 + 再次 apply。
        upgraded = self._generate(output_root=config_root, mode="enforce")
        failed = subprocess.run(
            [sys.executable, str(INSTALL), "apply", "--target", str(target), "--source", upgraded["config"]],
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=60,
        )
        assert failed.returncode != 0
        assert "同标识" in failed.stderr
        after = json.dumps(json.loads(target.read_text(encoding="utf-8")), sort_keys=True)
        assert edited in after
