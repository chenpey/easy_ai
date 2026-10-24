#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "=== 1. Git diff formatting check ==="
git diff --check
echo "OK: 无空白或冲突格式问题"

echo "=== 2. SKILL.md 体量检查 (上限 12 KB / 12,288 字节) ==="
SKILL_FILE="easythinking/SKILL.md"
if [ ! -f "$SKILL_FILE" ]; then
  echo "Error: $SKILL_FILE 不存在" >&2
  exit 1
fi
SKILL_SIZE=$(wc -c < "$SKILL_FILE" | tr -d ' ')
MAX_SIZE=12288
echo "当前大小: $SKILL_SIZE 字节 (上限: $MAX_SIZE 字节)"
if [ "$SKILL_SIZE" -gt "$MAX_SIZE" ]; then
  echo "Error: $SKILL_FILE 超过体量上限 ($SKILL_SIZE > $MAX_SIZE)" >&2
  exit 1
fi
echo "OK: 体量符合上限要求"

echo "=== 3. 版本号跨文件一致性检查 ==="
python3 -c '
import re, sys

files = {
    "root README": "README.md",
    "easythinking README": "easythinking/README.md",
    "SKILL.md": "easythinking/SKILL.md",
    "CHANGELOG.md": "easythinking/CHANGELOG.md",
    "tests/behavior.md": "easythinking/tests/behavior.md"
}

version_patterns = {
    "root README": r"\|\s*\[EasyThinking\]\(easythinking/README\.md\)\s*\|\s*([0-9]+\.[0-9]+\.[0-9]+)\s*\|",
    "easythinking README": r"^版本：`([0-9]+\.[0-9]+\.[0-9]+)`",
    "SKILL.md": r"version:\s*\"?([0-9]+\.[0-9]+\.[0-9]+)\"?",
    "CHANGELOG.md": r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)\s*更新说明",
    "tests/behavior.md": r"\|\s*20[0-9]{2}-[0-9]{2}-[0-9]{2}\s*\|\s*([0-9]+\.[0-9]+\.[0-9]+)\s*\|"
}

versions = {}
for name, path in files.items():
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    pattern = version_patterns[name]
    m = re.search(pattern, content, re.MULTILINE)
    if not m:
        print(f"Error: 无法在 {path} ({name}) 中提取版本号", file=sys.stderr)
        sys.exit(1)
    versions[name] = m.group(1)

print(f"提取版本号: {versions}")
unique_versions = set(versions.values())
if len(unique_versions) != 1:
    print(f"Error: 版本号不一致: {versions}", file=sys.stderr)
    sys.exit(1)

print(f"OK: 所有文件版本号一致 -> {unique_versions.pop()}")
'

echo "=== 4. Markdown 相对链接与锚点完整性检查 ==="
python3 -c '
import os, re, sys

files = [
    "README.md",
    "easythinking/README.md",
    "easythinking/SKILL.md",
    "easythinking/CHANGELOG.md",
    "easythinking/references/methods.md",
    "easythinking/references/examples.md",
    "easythinking/tests/behavior.md"
]

link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

for f in files:
    if not os.path.exists(f):
        print(f"Error: 缺失文件 {f}", file=sys.stderr)
        sys.exit(1)
    with open(f, "r", encoding="utf-8") as fh:
        content = fh.read()
    base_dir = os.path.dirname(f)
    for text, url in link_pattern.findall(content):
        if url.startswith(("http://", "https://", "mailto:")):
            continue
        if url.startswith("#"):
            anchor = url[1:]
            # 简单在同文件检查锚点
            if anchor not in content:
                print(f"Error: {f} 内部锚点失效: {url}", file=sys.stderr)
                sys.exit(1)
            continue
        file_part, _, anchor_part = url.partition("#")
        target_path = os.path.normpath(os.path.join(base_dir, file_part))
        if not os.path.exists(target_path):
            print(f"Error: {f} 链接目标不存在: {url} -> {target_path}", file=sys.stderr)
            sys.exit(1)
        if anchor_part:
            with open(target_path, "r", encoding="utf-8") as tf:
                target_content = tf.read()
            if anchor_part not in target_content:
                print(f"Error: {f} 外部锚点失效: {url} (#{anchor_part} not found in {target_path})", file=sys.stderr)
                sys.exit(1)

print("OK: 所有 Markdown 相对链接与锚点均有效")
'

echo "=== 全部自动化检查通过 ==="
