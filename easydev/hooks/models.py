"""EasyDev 模型配置的唯一 schema/validator 与来源选择。

零第三方依赖：hooks 运行时、scripts/check.sh 与测试都 import 本文件，避免
再复制一份 roles/efforts 常量。错误只报告字段路径与规则，不回显配置值。
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any, Dict, Iterable, List, Optional, Tuple

EFFORTS = ("inherit", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MAX_CONFIG_BYTES = 512 * 1024
REQUIRED_ROLES = ("developer", "reviewer")
OPTIONAL_ROLES = ("designer",)
TOP_LEVEL_KEYS = REQUIRED_ROLES + OPTIONAL_ROLES + ("upgrades",)
ROLE_KEYS = ("model", "reasoning_effort")
MAX_UPGRADES = 2


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


def _role_tuple(role: Dict[str, Any]) -> Tuple[Any, Any]:
    return (role.get("model"), role.get("reasoning_effort"))


def validate_config(value: Any, *, partial_roles: bool = False) -> None:
    """严格校验一份完整配置；partial_roles 供 machine context 的合成校验使用。"""
    config = _require_object(value, "$")
    _reject_unknown(config, TOP_LEVEL_KEYS, "$")
    for role_name in REQUIRED_ROLES:
        if role_name not in config:
            raise ConfigError("$." + role_name, "缺少必需角色")
        validate_role(config[role_name], "$." + role_name)
    if "designer" in config:
        validate_role(config["designer"], "$.designer")
    upgrades = config.get("upgrades", [])
    if not isinstance(upgrades, list):
        raise ConfigError("$.upgrades", "必须是数组")
    if len(upgrades) > MAX_UPGRADES:
        raise ConfigError("$.upgrades", "最多两项")
    previous = {role: _role_tuple(config[role]) for role in REQUIRED_ROLES}
    for index, item in enumerate(upgrades):
        path = "$.upgrades[%d]" % index
        entry = _require_object(item, path)
        _reject_unknown(entry, REQUIRED_ROLES, path)
        for role_name in REQUIRED_ROLES:
            if role_name not in entry:
                raise ConfigError(path + "." + role_name, "缺少必需角色")
            validate_role(entry[role_name], path + "." + role_name)
        current = {role: _role_tuple(entry[role]) for role in REQUIRED_ROLES}
        if all(current[role] == previous[role] for role in REQUIRED_ROLES):
            raise ConfigError(path, "与上一阶段完全相同，至少一个角色必须变化")
        previous = current


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
    return {"developer": dict(role), "reviewer": dict(role), "designer": dict(role), "upgrades": []}


class Selection(object):
    def __init__(self, config: Optional[Dict[str, Any]], source: str, error: Optional[str] = None) -> None:
        self.config = config
        self.source = source
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None


def user_config_path(env: Optional[Dict[str, str]] = None) -> str:
    environment = env if env is not None else os.environ
    appdata = environment.get("APPDATA")
    if os.name == "nt" and appdata:
        return os.path.join(appdata, "easydev", "models.json")
    return os.path.join(os.path.expanduser("~"), ".config", "easydev", "models.json")


def _read_config(path: str, source: str) -> Selection:
    path = os.path.expanduser(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
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
) -> Selection:
    """按优先级选第一个存在的完整文件；文件之间不合并。"""
    context = context or {}
    explicit = context.get("models_file")
    if isinstance(explicit, str) and explicit:
        if not os.path.isabs(os.path.expanduser(explicit)) and isinstance(context.get("repo_root"), str):
            explicit = os.path.join(context["repo_root"], explicit)
        selection = _read_config(explicit, "对话指定文件")
        if selection.config is None and selection.error is None:
            return Selection(None, "对话指定文件", "对话指定配置文件不存在；不能静默回退到低优先级来源")
        result = _apply_overrides(selection, context)
        return result
    if project_root:
        project_path = os.path.join(project_root, ".easydev", "models.json")
        project = _read_config(project_path, "项目配置")
        if project.error:
            return project
        if project.config is not None:
            return _apply_overrides(project, context)
    user_path = user_config_path(env)
    user = _read_config(user_path, "用户配置")
    if user.error:
        return user
    if user.config is not None:
        return _apply_overrides(user, context)
    selection = Selection(builtin_config(), "内置默认")
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
