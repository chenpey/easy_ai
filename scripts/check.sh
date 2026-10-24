#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "=== 1. Git diff formatting check (HEAD~1 + working tree) ==="
if git rev-parse --verify HEAD~1 >/dev/null 2>&1; then
  git diff --check HEAD~1
else
  git diff --check HEAD
fi
git diff --check
echo "OK: 无空白或冲突格式问题"

echo "=== 2. SKILL.md 体量与 Frontmatter 校验 ==="
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

python3 -c '
import re, sys

with open("easythinking/SKILL.md", "r", encoding="utf-8") as f:
    content = f.read()

m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
if not m:
    print("Error: SKILL.md 缺少有效的 YAML frontmatter", file=sys.stderr)
    sys.exit(1)

fm = m.group(1)

# 校验 name
m_name = re.search(r"^name:\s*([^\s]+)", fm, re.MULTILINE)
if not m_name or m_name.group(1) != "easythinking":
    print("Error: frontmatter 中的 name 必须为 easythinking", file=sys.stderr)
    sys.exit(1)

# 校验 description 存在且长度 <= 1024
m_desc = re.search(r"^description:\s*(.*?)(?=\n[a-zA-Z0-9_-]+:|\Z)", fm, re.DOTALL | re.MULTILINE)
if not m_desc:
    print("Error: frontmatter 缺少 description", file=sys.stderr)
    sys.exit(1)
desc = m_desc.group(1).strip()
desc_len = len(desc)
print(f"description 长度: {desc_len} 字符 (上限 1024)")
if desc_len == 0 or desc_len > 1024:
    print(f"Error: description 长度不合规 ({desc_len})", file=sys.stderr)
    sys.exit(1)

print("OK: SKILL.md frontmatter 校验通过")
'

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

echo "=== 4. Markdown 相对链接与 GitHub Slug 标题锚点精确检查 ==="
python3 -c '
import os, re, sys, unicodedata

def github_slug(heading_text):
    # 移除 HTML 标签及链接语法，仅保留纯文本
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading_text)
    text = re.sub(r"<[^>]+>", "", text).strip()
    text = text.lower()
    # 移除大多数标点符号，仅保留字符、数字、空白、短横线、下划线及中文字符
    text = re.sub(r"[^\w\s\u4e00-\u9fa5\-_]", "", text)
    # 空白转短横线
    slug = re.sub(r"\s+", "-", text)
    return slug

def extract_slugs(file_path):
    slug_counts = {}
    slugs = set()
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#"):
                m = re.match(r"^#{1,6}\s+(.+)$", line)
                if m:
                    raw_heading = m.group(1).strip()
                    # 去掉末尾可能存在的 #
                    raw_heading = re.sub(r"\s+#+$", "", raw_heading)
                    base_slug = github_slug(raw_heading)
                    if not base_slug:
                        continue
                    if base_slug in slug_counts:
                        slug_counts[base_slug] += 1
                        final_slug = f"{base_slug}-{slug_counts[base_slug]}"
                    else:
                        slug_counts[base_slug] = 0
                        final_slug = base_slug
                    slugs.add(final_slug)
    return slugs

files = [
    "README.md",
    "easythinking/README.md",
    "easythinking/SKILL.md",
    "easythinking/CHANGELOG.md",
    "easythinking/references/methods.md",
    "easythinking/references/examples.md",
    "easythinking/tests/behavior.md"
]

file_slugs = {f: extract_slugs(f) for f in files}
link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

for f in files:
    with open(f, "r", encoding="utf-8") as fh:
        content = fh.read()
    base_dir = os.path.dirname(f)
    for text, url in link_pattern.findall(content):
        if url.startswith(("http://", "https://", "mailto:")):
            continue
        if url.startswith("#"):
            anchor = url[1:]
            if anchor not in file_slugs[f]:
                print(f"Error: {f} 内部标题锚点不存在: #{anchor} (有效锚点数: {len(file_slugs[f])})", file=sys.stderr)
                sys.exit(1)
            continue
        file_part, _, anchor_part = url.partition("#")
        target_path = os.path.normpath(os.path.join(base_dir, file_part))
        if not os.path.exists(target_path):
            print(f"Error: {f} 链接目标不存在: {url} -> {target_path}", file=sys.stderr)
            sys.exit(1)
        if anchor_part:
            if target_path not in file_slugs:
                file_slugs[target_path] = extract_slugs(target_path)
            if anchor_part not in file_slugs[target_path]:
                print(f"Error: {f} 外部文件标题锚点不存在: {url} (目标文件: {target_path})", file=sys.stderr)
                sys.exit(1)

print("OK: 所有 Markdown 相对链接与 GitHub Slug 标题锚点校验通过")
'

echo "=== 5. README bash 代码块语法检查 (bash -n) ==="
python3 -c '
import re, subprocess, sys

with open("easythinking/README.md", "r", encoding="utf-8") as f:
    content = f.read()

bash_blocks = re.findall(r"^[ \t]*```bash\r?\n(.*?)\r?\n[ \t]*```", content, re.DOTALL | re.MULTILINE)
print(f"找到 {len(bash_blocks)} 个 bash 代码块，正在逐个运行 bash -n 语法解析...")
if len(bash_blocks) == 0:
    print("Error: 未在 README.md 中找到任何 bash 代码块", file=sys.stderr)
    sys.exit(1)

for i, block in enumerate(bash_blocks, 1):
    proc = subprocess.run(["bash", "-n"], input=block, text=True, capture_output=True)
    if proc.returncode != 0:
        print(f"Error: README.md 第 {i} 个 bash 代码块语法错误:\n{proc.stderr}", file=sys.stderr)
        sys.exit(1)

print("OK: 所有 README bash 代码块语法均合法")
'

echo "=== 全部自动化门禁检查通过 ==="
