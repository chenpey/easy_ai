#!/usr/bin/env python3
"""Print the ``metadata.version`` of a ``SKILL.md`` frontmatter.

This is the only version extractor: ``scripts/check.sh`` compares the value it
prints with the other files that state a version, and ``scripts/smoke-install.sh``
compares the same value with the README. Both gates therefore read the version
from one parser instead of one regex each.

Duplicate keys are refused. With PyYAML the frontmatter is loaded through a
loader that rejects them; without PyYAML a strict line scan requires
``  version:`` to be a direct child of the top-level ``metadata:`` mapping and
ignores block-scalar content, so a description that mentions ``version:`` or a
``version`` key under another mapping is never read as ``metadata.version``.
Double-quoted key escapes are refused rather than interpreted by that subset.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
TOP_LEVEL_RE = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")
BLOCK_SCALAR_RE = re.compile(r"^[>|][+-]?[0-9]?$")


class FrontmatterError(ValueError):
    """The frontmatter cannot provide a single metadata.version."""


def split_child_line(line: str) -> tuple:
    """Split a two-space-indented mapping line into an unquoted key and value."""
    body = line[2:]
    if body[:1] in {"'", '"'}:
        quote = body[0]
        end = body.find(quote, 1)
        if end < 0:
            raise FrontmatterError(f"frontmatter 存在未闭合的引号键: {line!r}")
        key, rest = body[1:end], body[end + 1:]
        if quote == '"' and "\\" in key:
            raise FrontmatterError(
                f"frontmatter 不支持双引号键中的反斜杠转义: {line!r}"
            )
        if not rest.startswith(":"):
            raise FrontmatterError(f"frontmatter 存在无法解释的缩进行: {line!r}")
        return key, rest[1:]
    key, separator, rest = body.partition(":")
    if not separator or key != key.strip() or not key:
        raise FrontmatterError(f"frontmatter 存在无法解释的缩进行: {line!r}")
    return key, rest


def unquote_value(raw: str) -> str:
    """Strip exactly one balanced quote pair and refuse any other quote usage."""
    value = raw.strip()
    if not value:
        return value
    if value[0] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise FrontmatterError(
                "frontmatter 的 metadata.version 引号未正确闭合"
            )
        value = value[1:-1]
        if "'" in value or '"' in value:
            raise FrontmatterError(
                "frontmatter 的 metadata.version 值在引号内仍含引号"
            )
        return value
    if "'" in value or '"' in value:
        raise FrontmatterError("frontmatter 的 metadata.version 值引号不合法")
    return value


def split_frontmatter(content: str, path: str) -> str:
    match = FRONTMATTER_RE.match(content)
    if match is None:
        raise FrontmatterError(f"{path} 缺少有效的 YAML frontmatter")
    return match.group(1)


def load_with_yaml(frontmatter: str) -> dict:
    import yaml

    class StrictLoader(yaml.SafeLoader):
        """SafeLoader that refuses duplicate mapping keys."""

        def construct_mapping(self, node, deep: bool = False) -> dict:
            seen = set()
            for key_node, _value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise FrontmatterError(f"frontmatter 存在重复键: {key}")
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    try:
        data = yaml.load(frontmatter, Loader=StrictLoader)
    except FrontmatterError:
        raise
    except yaml.YAMLError as error:
        raise FrontmatterError(f"frontmatter 不是合法 YAML: {error}") from error
    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter 必须是 YAML 映射")
    return data


def version_without_yaml(frontmatter: str) -> str:
    """Read metadata.version with a strict line scan (no PyYAML installed)."""
    current_key = ""
    block_scalar = False
    top_keys = set()
    version = None
    for line in frontmatter.splitlines():
        if not line.strip():
            continue
        if block_scalar:
            # Block scalar content is literal text, never a key declaration.
            if line[:1].isspace():
                continue
            block_scalar = False
        match = TOP_LEVEL_RE.match(line)
        if match:
            key, rest = match.group(1), match.group(2).strip()
            if key in top_keys:
                raise FrontmatterError(f"frontmatter 存在重复键: {key}")
            top_keys.add(key)
            current_key = key
            block_scalar = bool(BLOCK_SCALAR_RE.match(rest))
            continue
        if not line[:1].isspace():
            raise FrontmatterError(f"frontmatter 存在非法顶层行: {line!r}")
        indent = len(line) - len(line.lstrip(" "))
        if indent != 2:
            # Deeper lines belong to a nested mapping; only a direct child of a
            # top-level key can declare metadata.version.
            continue
        body = line[2:]
        if not body.strip() or body.lstrip().startswith(("#", "-")):
            # Comments and sequence items do not declare mapping keys.
            continue
        key, raw_value = split_child_line(line)
        if key != "version":
            continue
        if current_key != "metadata":
            raise FrontmatterError(
                f"metadata.version 必须直接隶属 metadata 映射，"
                f"不能在 {current_key or '其他映射'} 下声明 version"
            )
        if version is not None:
            raise FrontmatterError("frontmatter 存在重复键: version")
        version = unquote_value(raw_value)
    if version is None:
        raise FrontmatterError("metadata.version 缺失或格式不为 x.y.z")
    return version


def read_version(path: Path) -> str:
    """Return the frontmatter metadata.version of ``path`` or raise."""
    content = path.read_text(encoding="utf-8")
    frontmatter = split_frontmatter(content, str(path))
    try:
        import yaml  # noqa: F401 - presence decides which branch parses
    except ImportError:
        version = version_without_yaml(frontmatter)
    else:
        metadata = load_with_yaml(frontmatter).get("metadata")
        if not isinstance(metadata, dict):
            raise FrontmatterError(f"{path} frontmatter 缺少 metadata 映射")
        raw = metadata.get("version")
        if raw is None:
            raise FrontmatterError("metadata.version 缺失或格式不为 x.y.z")
        version = str(raw).strip()
    if not VERSION_RE.fullmatch(version):
        raise FrontmatterError("metadata.version 缺失或格式不为 x.y.z")
    return version


def main(argv: list) -> int:
    if len(argv) != 1:
        print("用法: skill_version.py <SKILL.md>", file=sys.stderr)
        return 2
    try:
        version = read_version(Path(argv[0]))
    except (OSError, UnicodeError, FrontmatterError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
