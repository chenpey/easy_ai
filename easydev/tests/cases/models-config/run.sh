#!/usr/bin/env bash
# 模型配置用例：scripts/models.py 是 check.sh 与 environment.md 共用的唯一 schema/
# validator 与来源选择实现。正例必须通过；重复键、非法 JSON、未知键、缺角色、
# 非法 model/effort、upgrades 超限或相邻同值、无效文件静默回退、空覆盖都必须被拒绝。
# 来源优先级固定为「对话指定文件 > 项目 > 用户 > 内置默认」，且文件之间不合并。
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
# 显式模板：macOS 裸 `mktemp -d` 不使用 TMPDIR，受限沙箱里会被拒绝。
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/models-config.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/home/.config/easydev"

# HOME 指向临时目录，用户配置路径由此隔离；用例不会读到真实 ~/.config/easydev。
HOME="$TMP_DIR/home" MODELS="$ROOT_DIR/scripts/models.py" WORK="$TMP_DIR" python3 - <<'PY'
import importlib.util
import json
import os
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("models", os.environ["MODELS"])
models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(models)

WORK = Path(os.environ["WORK"])
HOME = Path(os.environ["HOME"])
USER_CONFIG = HOME / ".config" / "easydev" / "models.json"
PROJECT = WORK / "project"
PROJECT_CONFIG = PROJECT / ".easydev" / "models.json"
PROJECT.mkdir()

BASE = {
    "developer": {"model": "openai/gpt-6", "reasoning_effort": "high"},
    "reviewer": {"model": "anthropic/claude-5", "reasoning_effort": "medium"},
}
# 用户配置刻意带一个项目配置没有的可选角色：若实现做了跨文件合并（补齐缺失键或
# 低优先级覆盖），designer 会出现在结果里，下面的「文件不合并」断言才抓得住。
OTHER = {
    "developer": {"model": "google/gemini-4", "reasoning_effort": "low"},
    "reviewer": {"model": "xai/grok-5", "reasoning_effort": "high"},
    "designer": {"model": "xai/grok-5", "reasoning_effort": "high"},
}

failures = []


def check(label, fn):
    try:
        fn()
    except AssertionError as error:
        failures.append(label)
        print("FAIL: %s: %s" % (label, error))
    else:
        print("  OK: %s" % label)


def with_role(role, **fields):
    config = json.loads(json.dumps(BASE))
    config[role].update(fields)
    return config


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def accept_config(payload):
    models.validate_config(payload)


def reject_config(payload, needle):
    try:
        models.validate_config(payload)
    except models.ConfigError as error:
        assert needle in str(error), "错误信息缺「%s」：%s" % (needle, error)
        return
    raise AssertionError("应被拒绝却通过")


def reject_text(text, needle):
    try:
        models.parse_json_text(text)
    except models.ConfigError as error:
        assert needle in str(error), "错误信息缺「%s」：%s" % (needle, error)
        return
    raise AssertionError("应被拒绝却通过")


def reject_overrides(value, needle):
    try:
        models.validate_overrides(value)
    except models.ConfigError as error:
        assert needle in str(error), "错误信息缺「%s」：%s" % (needle, error)
        return
    raise AssertionError("应被拒绝却通过")


def expect_selection(expected_source, expected_config, **kwargs):
    selection = models.select(**kwargs)
    assert selection.ok, "应成功，实际失败：%s" % selection.error
    assert selection.source == expected_source, "来源为 %s" % selection.source
    assert selection.config == expected_config, "选中内容不符：%s" % selection.config


def expect_error(needle, **kwargs):
    selection = models.select(**kwargs)
    assert not selection.ok, "应暂停并报错，实际成功（来源 %s）" % selection.source
    assert selection.config is None, "失败时不得带回配置"
    assert needle in (selection.error or ""), "错误信息缺「%s」：%s" % (needle, selection.error)


print("=== schema/validator ===")
check("内置默认配置合法", lambda: accept_config(models.builtin_config()))
check("合法基础配置与两项 upgrades", lambda: accept_config({
    **BASE,
    "upgrades": [
        {"developer": {"model": "openai/gpt-6", "reasoning_effort": "max"},
         "reviewer": {"model": "anthropic/claude-5", "reasoning_effort": "medium"}},
        {"developer": {"model": "google/gemini-4", "reasoning_effort": "max"},
         "reviewer": {"model": "xai/grok-5", "reasoning_effort": "high"}},
    ],
}))
check("重复键被拒绝", lambda: reject_text('{"developer": {}, "developer": {}}', "重复键"))
check("非法 JSON 被拒绝", lambda: reject_text('{"developer": }', "JSON 无效"))
check("未知顶层键被拒绝", lambda: reject_config({**BASE, "extra": 1}, "未知键"))
check("缺必需角色被拒绝", lambda: reject_config({"developer": BASE["developer"]}, "缺少必需角色"))
check("model 缺供应商被拒绝", lambda: reject_config(with_role("developer", model="gpt-6"), "完整 ID"))
check("model 含首尾空白被拒绝",
      lambda: reject_config(with_role("developer", model=" openai/gpt-6 "), "首尾空白"))
check("reasoning_effort 越界被拒绝",
      lambda: reject_config(with_role("reviewer", reasoning_effort="turbo"), "不在允许集合内"))
check("非推理模型的 off 档合法",
      lambda: accept_config(with_role("developer", reasoning_effort="off")))
check("无宿主依据的 ultra 档被拒绝",
      lambda: reject_config(with_role("reviewer", reasoning_effort="ultra"), "不在允许集合内"))
check("designer 缺字段被拒绝",
      lambda: reject_config({**BASE, "designer": {"model": "inherit"}}, "缺少必需字段"))
check("upgrades 超两项被拒绝", lambda: reject_config({
    **BASE,
    "upgrades": [
        {"developer": {"model": "a/b", "reasoning_effort": "low"}, "reviewer": BASE["reviewer"]},
        {"developer": {"model": "a/c", "reasoning_effort": "low"}, "reviewer": BASE["reviewer"]},
        {"developer": {"model": "a/d", "reasoning_effort": "low"}, "reviewer": BASE["reviewer"]},
    ],
}, "最多两项"))
check("upgrades 相邻级完全相同被拒绝",
      lambda: reject_config({**BASE, "upgrades": [BASE]}, "至少一个角色必须变化"))
check("upgrades 缺 reviewer 被拒绝", lambda: reject_config({
    **BASE,
    "upgrades": [{"developer": {"model": "a/b", "reasoning_effort": "low"}}],
}, "缺少必需角色"))
check("upgrades 不得含 designer", lambda: reject_config({
    **BASE,
    "upgrades": [{"developer": {"model": "a/b", "reasoning_effort": "low"},
                  "reviewer": BASE["reviewer"],
                  "designer": {"model": "inherit", "reasoning_effort": "inherit"}}],
}, "未知键"))
check("role_overrides 空对象被拒绝", lambda: reject_overrides({}, "覆盖不能是空对象"))
check("role_overrides 非对象被拒绝", lambda: reject_overrides(None, "必须是对象"))

print("=== 来源选择 ===")
write(PROJECT_CONFIG, BASE)
write(USER_CONFIG, OTHER)
check("项目配置优先于用户配置，且两个文件不合并",
      lambda: expect_selection("项目配置", BASE, project_root=str(PROJECT)))
PROJECT_CONFIG.unlink()
check("无项目配置时取用户配置",
      lambda: expect_selection("用户配置", OTHER, project_root=str(PROJECT)))
USER_CONFIG.unlink()
check("两级都没有时用内置默认",
      lambda: expect_selection("内置默认", models.builtin_config(), project_root=str(PROJECT)))

invalid_project = WORK / "invalid-project"
(invalid_project / ".easydev").mkdir(parents=True)
(invalid_project / ".easydev" / "models.json").write_text('{"developer": }', encoding="utf-8")
write(USER_CONFIG, OTHER)
check("项目配置无效时暂停分派而不是回退用户配置",
      lambda: expect_error("项目配置", project_root=str(invalid_project)))
USER_CONFIG.unlink()

check("对话指定文件不存在时不回退",
      lambda: expect_error("不存在", context={"models_file": str(WORK / "missing.json")}))
# 恢复一份内容不同的项目配置：否则这条只验证了相对路径解析，没验证与项目配置的优先级。
write(PROJECT_CONFIG, BASE)
write(WORK / "explicit.json", OTHER)
check("对话指定相对路径按 repo_root 解析并优先于项目配置",
      lambda: expect_selection("对话指定文件", OTHER,
                               context={"models_file": "explicit.json", "repo_root": str(WORK)},
                               project_root=str(PROJECT)))
PROJECT_CONFIG.unlink()

overridden = json.loads(json.dumps(models.builtin_config()))
overridden["reviewer"]["model"] = "openai/gpt-6"
check("role_overrides 只覆盖给定字段",
      lambda: expect_selection("内置默认", overridden,
                               context={"role_overrides": {"reviewer": {"model": "openai/gpt-6"}}},
                               project_root=str(PROJECT)))
check("role_overrides 为空对象时暂停分派",
      lambda: expect_error("空对象", context={"role_overrides": {}}, project_root=str(PROJECT)))

write(WORK / "real-user.json", OTHER)
USER_CONFIG.symlink_to(WORK / "real-user.json")
check("用户配置是软链时拒绝当作配置文件",
      lambda: expect_error("不能确认配置来源"))
USER_CONFIG.unlink()

print("=== 读取契约边界（审计确认缺陷的回归） ===")
ALT_HOME = WORK / "alt-home"
write(ALT_HOME / ".config" / "easydev" / "models.json", OTHER)
if os.name != "nt":
    check("注入 env 的 HOME 决定用户配置来源，不读宿主家目录",
          lambda: expect_selection("用户配置", OTHER, project_root=str(PROJECT),
                                   env={"HOME": str(ALT_HOME)}))
    # 宿主家目录里放一份配置：隔离 env 缺 HOME 时必须走内置默认，不能回退去读它。
    write(USER_CONFIG, OTHER)
    check("注入不含 HOME 的隔离 env 时回退内置默认，不读宿主用户配置",
          lambda: expect_selection("内置默认", models.builtin_config(),
                                   project_root=str(PROJECT), env={}))
    USER_CONFIG.unlink()

USER_CONFIG.symlink_to(WORK / "real-user.json")
saved_nofollow = getattr(models.os, "O_NOFOLLOW", 0)
if saved_nofollow:
    models.os.O_NOFOLLOW = 0
    try:
        check("宿主无 O_NOFOLLOW 时仍拒绝软链配置",
              lambda: expect_error("不是普通文件"))
    finally:
        models.os.O_NOFOLLOW = saved_nofollow
USER_CONFIG.unlink()

LARGE = WORK / "large.json"
LARGE.write_text(json.dumps(BASE) + " " * models.MAX_CONFIG_BYTES, encoding="utf-8")
real_fstat = models.os.fstat


def lying_fstat(fd):
    """模拟 st_size 失真的卷：大小不可信，只有实际读取长度能定案。"""
    info = real_fstat(fd)
    return type("LyingInfo", (), {"st_mode": info.st_mode, "st_size": 0})()


models.os.fstat = lying_fstat
try:
    check("st_size 失真时按实际读取长度拒绝超限配置",
          lambda: expect_error("超过大小上限", context={"models_file": str(LARGE)}))
finally:
    models.os.fstat = real_fstat

# 上限边界：恰好 512 KiB 可读，多一个字节即拒绝（契约是「不得超过」）。
BASE_TEXT = json.dumps(BASE)
PAD = models.MAX_CONFIG_BYTES - len(BASE_TEXT)
EXACT = WORK / "exact-max.json"
EXACT.write_text(BASE_TEXT + " " * PAD, encoding="utf-8")
check("恰好 512 KiB 的配置可读取",
      lambda: expect_selection("对话指定文件", BASE, context={"models_file": str(EXACT)}))
OVER = WORK / "over-max.json"
OVER.write_text(BASE_TEXT + " " * (PAD + 1), encoding="utf-8")
check("超过 512 KiB 一个字节即拒绝",
      lambda: expect_error("超过大小上限", context={"models_file": str(OVER)}))

# 项目配置是软链：拒绝且不静默回退到内容不同的用户配置。
# 用户配置写成合法真文件：若项目软链被当成「不存在」，选择会成功落到用户配置，这条就不成立。
write(USER_CONFIG, OTHER)
PROJECT_CONFIG.symlink_to(WORK / "real-user.json")
check("项目配置是软链时拒绝且不回退用户配置",
      lambda: expect_error("不能确认配置来源", project_root=str(PROJECT)))
PROJECT_CONFIG.unlink()
USER_CONFIG.unlink()

if failures:
    print("FAIL: %d 项未通过：%s" % (len(failures), failures), file=sys.stderr)
    sys.exit(1)
PY

echo "=== 模型配置用例全部通过 ==="
