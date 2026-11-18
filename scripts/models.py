"""EasyDev 模型配置的唯一 schema/validator 与来源选择。

零第三方依赖：hooks 运行时、scripts/check.sh 与测试都 import 本文件，避免
再复制一份 roles/efforts 常量。错误只报告字段路径与规则，不回显配置值。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 档位对齐宿主 thinking 集合（off 是非推理模型的唯一档位）；跨宿主映射在分派前核对，
# 无宿主依据的 none/ultra 已移除，避免 schema 接受后无法映射。
EFFORTS = ("inherit", "off", "minimal", "low", "medium", "high", "xhigh", "max")
MAX_CONFIG_BYTES = 512 * 1024
REQUIRED_ROLES = ("developer", "reviewer")
OPTIONAL_ROLES = ("designer",)
TOP_LEVEL_KEYS = REQUIRED_ROLES + OPTIONAL_ROLES + ("windows",)
ROLE_KEYS = ("model", "reasoning_effort")
# 中国无夏令时，固定偏移即可，不引入第三方时区库。
BEIJING = timezone(timedelta(hours=8))
WINDOW_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])$")


class ConfigError(Exception):
    """确定违规：字段路径 + 规则说明，不含配置值。"""

    def __init__(self, path: str, message: str) -> None:
        super().__init__("%s: %s" % (path, message))
        self.path = path
        self.message = message


def _reject_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("$", "重复键 %r" % key)
        result[key] = value
    return result


def parse_json_text(text: str) -> Any:
    """解析 JSON；重复键按确定违规拒绝。"""
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicates)
    except json.JSONDecodeError as error:
        raise ConfigError("$", "JSON 无效（第 %d 行）" % error.lineno)


def _require_object(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(path, "必须是对象")
    return value


def _reject_unknown(value: Dict[str, Any], allowed: Iterable[str], path: str) -> None:
    extra = sorted(set(value) - set(allowed))
    if extra:
        raise ConfigError(path, "含未知键 %s" % extra)


def validate_model(value: Any, path: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(path, "model 必须是非空字符串")
    if value != value.strip():
        raise ConfigError(path, "model 不得含首尾空白")
    if value == "inherit":
        return
    provider, separator, name = value.partition("/")
    if not separator or not provider.strip() or not name.strip():
        raise ConfigError(path, "model 必须是完整 ID（供应商与模型名都非空）或 inherit")


def validate_effort(value: Any, path: str) -> None:
    if not isinstance(value, str) or value not in EFFORTS:
        raise ConfigError(path, "reasoning_effort 不在允许集合内")


def validate_role(value: Any, path: str) -> None:
    role = _require_object(value, path)
    _reject_unknown(role, ROLE_KEYS, path)
    for key in ROLE_KEYS:
        if key not in role:
            raise ConfigError(path + "." + key, "缺少必需字段")
    validate_model(role["model"], path + ".model")
    validate_effort(role["reasoning_effort"], path + ".reasoning_effort")


def validate_role_partial(value: Any, path: str) -> None:
    """对话 role_overrides：只校验实际提供的字段，至少一项。"""
    role = _require_object(value, path)
    _reject_unknown(role, ROLE_KEYS, path)
    if not role:
        raise ConfigError(path, "覆盖不能是空对象")
    if "model" in role:
        validate_model(role["model"], path + ".model")
    if "reasoning_effort" in role:
        validate_effort(role["reasoning_effort"], path + ".reasoning_effort")


def _window_minutes(key: str, path: str) -> Tuple[int, int]:
    """时段键转为当天分钟数；格式或起止相同时按确定违规拒绝。"""
    match = WINDOW_PATTERN.match(key)
    if match is None:
        raise ConfigError(path, "时段必须写成 HH:MM-HH:MM（24 小时制）")
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    if start == end:
        raise ConfigError(path, "起止时间不能相同")
    return start, end


def validate_windows(value: Any) -> None:
    """windows：键是北京时间时段（键顺序即匹配优先级），值是角色局部覆盖。"""
    windows = _require_object(value, "$.windows")
    if not windows:
        raise ConfigError("$.windows", "必须是至少一个时段的非空对象")
    for key, overrides in windows.items():
        path = "$.windows." + key
        _window_minutes(key, path)
        entry = _require_object(overrides, path)
        if not entry:
            raise ConfigError(path, "覆盖不能是空对象")
        _reject_unknown(entry, REQUIRED_ROLES + OPTIONAL_ROLES, path)
        for role_name, fields in entry.items():
            validate_role_partial(fields, path + "." + role_name)


def validate_config(value: Any) -> None:
    """严格校验一份完整配置。"""
    config = _require_object(value, "$")
    _reject_unknown(config, TOP_LEVEL_KEYS, "$")
    for role_name in REQUIRED_ROLES:
        if role_name not in config:
            raise ConfigError("$." + role_name, "缺少必需角色")
        validate_role(config[role_name], "$." + role_name)
    if "designer" in config:
        validate_role(config["designer"], "$.designer")
    if "windows" in config:
        validate_windows(config["windows"])


def validate_overrides(value: Any) -> Dict[str, Dict[str, Any]]:
    """显式给出的 role_overrides 必须是非空对象；空/null/false/数组都是确定违规。"""
    overrides = _require_object(value, "$.role_overrides")
    if not overrides:
        raise ConfigError("$.role_overrides", "覆盖不能是空对象；省略该字段才表示不覆盖")
    _reject_unknown(overrides, REQUIRED_ROLES + OPTIONAL_ROLES, "$.role_overrides")
    for role_name, role_value in overrides.items():
        validate_role_partial(role_value, "$.role_overrides." + role_name)
    return overrides  # type: ignore[return-value]


def merge_overrides(config: Dict[str, Any], overrides: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """把已校验的局部覆盖叠加到完整基础配置，再走同一 validator。"""
    merged = json.loads(json.dumps(config))
    for role_name, fields in overrides.items():
        if role_name not in merged:
            merged[role_name] = {"model": "inherit", "reasoning_effort": "inherit"}
        merged[role_name].update(fields)
    validate_config(merged)
    return merged


def builtin_config() -> Dict[str, Any]:
    role = {"model": "inherit", "reasoning_effort": "inherit"}
    return {"developer": dict(role), "reviewer": dict(role), "designer": dict(role)}


class Selection(object):
    def __init__(
        self,
        config: Optional[Dict[str, Any]],
        source: str,
        error: Optional[str] = None,
        window: Optional[str] = None,
    ) -> None:
        self.config = config
        self.source = source
        self.error = error
        self.window = window

    @property
    def ok(self) -> bool:
        return self.error is None


def user_config_path(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    environment = env if env is not None else os.environ
    appdata = environment.get("APPDATA")
    if os.name == "nt" and appdata:
        return os.path.join(appdata, "easydev", "models.json")
    # 注入的 env 优先：隔离环境没有 HOME 时不得静默换成宿主家目录。
    home = environment.get("HOME")
    if home:
        return os.path.join(home, ".config", "easydev", "models.json")
    if env is not None:
        return None
    return os.path.join(os.path.expanduser("~"), ".config", "easydev", "models.json")


def _read_config(path: str, source: str) -> Selection:
    path = os.path.expanduser(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
    if not nofollow and os.path.islink(path):
        # 平台无 O_NOFOLLOW（如 Windows）时显式拦软链，不靠打开的副作用。
        return Selection(None, source, "%s 不是普通文件；不能确认配置来源" % source)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return Selection(None, source, None)
    except OSError:
        return Selection(None, source, "%s 不可读；不能确认配置来源，按规则暂停分派" % source)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            # 命名管道/设备等替代品不当作配置文件，也不会阻塞读取。
            return Selection(None, source, "%s 不是普通文件；不能确认配置来源" % source)
        if info.st_size > MAX_CONFIG_BYTES:
            return Selection(None, source, "%s 超过大小上限；不能确认配置来源" % source)
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_CONFIG_BYTES + 1)
        # st_size 可能失真（特殊卷）或与并发追写赛跑，以实际读取长度定案。
        if len(payload) > MAX_CONFIG_BYTES:
            return Selection(None, source, "%s 超过大小上限；不能确认配置来源" % source)
    except OSError:
        return Selection(None, source, "%s 不可读；不能确认配置来源，按规则暂停分派" % source)
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return Selection(None, source, "%s 配置无效（不是 UTF-8）" % source)
    try:
        data = parse_json_text(text)
        validate_config(data)
    except ConfigError as error:
        return Selection(None, source, "%s 配置无效（%s）" % (source, error))
    return Selection(data, source)


def select(
    *,
    context: Optional[Dict[str, Any]] = None,
    project_root: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    now: Optional[datetime] = None,
    locked_window: Optional[str] = None,
) -> Selection:
    """按优先级选第一个存在的完整文件；文件之间不合并，但会按北京时间（或
    locked_window）应用所选文件内的 windows 覆盖后再应用对话级覆盖。"""
    context = context or {}
    if "models_file" in context:
        # 显式给出即强制校验：空/非字符串是确定违规，不能静默回退到低优先级来源。
        explicit = context.get("models_file")
        if not isinstance(explicit, str) or not explicit.strip():
            return Selection(None, "对话指定文件", "对话指定的配置文件无效；不能静默回退到低优先级来源")
        if not os.path.isabs(os.path.expanduser(explicit)) and isinstance(context.get("repo_root"), str):
            explicit = os.path.join(context["repo_root"], explicit)
        selection = _read_config(explicit, "对话指定文件")
        if selection.config is None and selection.error is None:
            return Selection(None, "对话指定文件", "对话指定配置文件不存在；不能静默回退到低优先级来源")
        return _apply_window(selection, context, now, locked_window)
    if project_root:
        project_path = os.path.join(project_root, ".easydev", "models.json")
        project = _read_config(project_path, "项目配置")
        if project.error:
            return project
        if project.config is not None:
            return _apply_window(project, context, now, locked_window)
    user_path = user_config_path(env)
    if user_path is not None:
        user = _read_config(user_path, "用户配置")
        if user.error:
            return user
        if user.config is not None:
            return _apply_window(user, context, now, locked_window)
    return _apply_window(Selection(builtin_config(), "内置默认"), context, now, locked_window)


def _beijing(now: Optional[datetime]) -> datetime:
    """naive 时间视为北京时间，aware 时间换算到北京时间。"""
    if now is None:
        return datetime.now(BEIJING)
    if now.tzinfo is None:
        return now.replace(tzinfo=BEIJING)
    return now.astimezone(BEIJING)


def _apply_window(
    selection: Selection,
    context: Dict[str, Any],
    now: Optional[datetime],
    locked_window: Optional[str],
) -> Selection:
    """应用选中文件的 windows 覆盖并移除该键；锁定窗口失效时报错，不静默回退。"""
    if not selection.ok:
        return selection
    config = selection.config or {}
    if "windows" in config:
        windows = config["windows"]
        window = None
        if locked_window is not None:
            if locked_window != "default":
                if locked_window not in windows:
                    return Selection(
                        None,
                        selection.source,
                        "锁定窗口 %r 不在 %s 的 windows 中；按规则暂停分派" % (locked_window, selection.source),
                    )
                window = locked_window
        else:
            moment = _beijing(now)
            minute_of_day = moment.hour * 60 + moment.minute
            for key in windows:
                start, end = _window_minutes(key, "$.windows." + key)
                if start < end:
                    hit = start <= minute_of_day < end
                else:
                    hit = minute_of_day >= start or minute_of_day < end
                if hit:
                    window = key
                    break
        if window is not None:
            for role_name, fields in windows[window].items():
                # 缺失的可选角色按 merge_overrides 的既有做法补 inherit 两字段。
                config.setdefault(role_name, {"model": "inherit", "reasoning_effort": "inherit"})
                config[role_name].update(fields)
                validate_role(config[role_name], "$." + role_name)
        del config["windows"]
        selection.window = window
    elif locked_window not in (None, "default"):
        return Selection(
            None,
            selection.source,
            "%s 没有 windows；锁定窗口 %r 无法应用，按规则暂停分派" % (selection.source, locked_window),
        )
    return _apply_overrides(selection, context)


def _apply_overrides(selection: Selection, context: Dict[str, Any]) -> Selection:
    if "role_overrides" not in context:
        return selection
    overrides = context.get("role_overrides")
    if not selection.ok:
        return selection
    try:
        validated = validate_overrides(overrides)
        selection.config = merge_overrides(selection.config or builtin_config(), validated)
    except ConfigError as error:
        return Selection(None, selection.source, "role_overrides 无效（%s）" % error)
    return selection


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="models.py", description="解析按北京时间生效的模型配置")
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve", help="输出 JSON：生效来源、时段与配置")
    resolve.add_argument("--locked", default=None, help="锁定时段键；default 锁定顶层默认配置")
    resolve.add_argument("--project-root", default=None, help="项目根目录（读取 .easydev/models.json）")
    resolve.add_argument("--now", default=None, help="ISO 8601 时间，用于确定性验证时间边界")
    args = parser.parse_args(argv)
    now = None
    error = None
    if args.now is not None:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError:
            error = "--now 不是合法 ISO 8601 时间；按规则暂停分派"
    selection = Selection(None, "命令行参数", error) if error else select(
        project_root=args.project_root, now=now, locked_window=args.locked
    )
    print(json.dumps({
        "ok": selection.ok,
        "source": selection.source,
        "window": selection.window,
        "config": selection.config,
        "error": selection.error,
    }, ensure_ascii=False, indent=2))
    return 0 if selection.ok else 1


if __name__ == "__main__":
    sys.exit(main())
