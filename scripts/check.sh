#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "=== 1. 已跟踪文件行尾空白检查（不依赖提交深度） ==="
# 直接扫描已跟踪文本文件：浅克隆（fetch-depth=1）或干净工作区都不会退化为空检查。
# 新增文件类型时同步补充扩展名，否则该类型不受门禁覆盖。
# *.patch 等格式文件含语义化行尾空格，不纳入扫描。
WS_PATHS=('*.md' '*.sh' '*.yml' '*.yaml' '*.json' '*.py' '*.svg' '*.txt')
ws_status=0
ws_report="$(git grep -nI -E $'[ \t]+$' -- "${WS_PATHS[@]}")" || ws_status=$?
if [ "$ws_status" -gt 1 ]; then
  echo "Error: git grep 执行失败 (exit $ws_status)" >&2
  exit 1
fi
if [ -n "$ws_report" ]; then
  printf '%s\n' "$ws_report" >&2
  echo "Error: 上述行存在行尾空白" >&2
  exit 1
fi
git diff --check
echo "OK: 已跟踪文件与工作区均无行尾空白或冲突标记"

echo "=== 2. SKILL.md 体量与 Frontmatter 校验 ==="
# 体量上限按技能分别声明；easydev 暂无体量政策，只校验 frontmatter。
for SKILL_NAME in easythink easydev; do
  SKILL_FILE="$SKILL_NAME/SKILL.md"
  if [ ! -f "$SKILL_FILE" ]; then
    echo "Error: $SKILL_FILE 不存在" >&2
    exit 1
  fi
  SKILL_SIZE=$(wc -c < "$SKILL_FILE" | tr -d ' ')
  case "$SKILL_NAME" in
    easythink) MAX_SIZE=12288 ;;
    *) MAX_SIZE=0 ;;
  esac
  if [ "$MAX_SIZE" -gt 0 ]; then
    echo "$SKILL_FILE 大小: $SKILL_SIZE 字节 (上限: $MAX_SIZE 字节)"
    if [ "$SKILL_SIZE" -gt "$MAX_SIZE" ]; then
      echo "Error: $SKILL_FILE 超过体量上限 ($SKILL_SIZE > $MAX_SIZE)" >&2
      exit 1
    fi
  else
    echo "$SKILL_FILE 大小: $SKILL_SIZE 字节 (未设上限)"
  fi

  python3 - "$SKILL_NAME" <<'PY'
import re, sys

name = sys.argv[1]
path = f"{name}/SKILL.md"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
if not m:
    print(f"Error: {path} 缺少有效的 YAML frontmatter", file=sys.stderr)
    sys.exit(1)

fm = m.group(1)

try:
    import yaml
except ImportError:
    yaml = None

if yaml is not None:
    try:
        data = yaml.safe_load(fm)
    except Exception as exc:  # yaml.YAMLError 或标量类型错误
        print(f"Error: {path} frontmatter 不是合法 YAML: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"Error: {path} frontmatter 必须是 YAML 映射", file=sys.stderr)
        sys.exit(1)
    unknown = set(data) - {"name", "description", "metadata"}
    if unknown:
        print(f"Error: {path} frontmatter 含未知顶层字段: {sorted(unknown)}", file=sys.stderr)
        sys.exit(1)
    if data.get("name") != name:
        print(f"Error: {path} frontmatter 中的 name 必须为 {name}", file=sys.stderr)
        sys.exit(1)
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        print(f"Error: {path} frontmatter 缺少非空 description", file=sys.stderr)
        sys.exit(1)
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        print(f"Error: {path} frontmatter 缺少 metadata 映射", file=sys.stderr)
        sys.exit(1)
    print(f"description 长度: {len(desc)} 字符 (上限 1024) [PyYAML 已解析]")
    if len(desc) > 1024:
        print(f"Error: {path} description 长度不合规 ({len(desc)} > 1024)", file=sys.stderr)
        sys.exit(1)
else:
    print("提示: 未安装 PyYAML，启用严格降级校验（拒绝内联 description 中的 ASCII \": \" 与非法顶层行）")
    m_name = re.search(r"^name:[ \t]*(\S+)[ \t]*$", fm, re.MULTILINE)
    if not m_name or m_name.group(1) != name:
        print(f"Error: {path} frontmatter 中的 name 必须为 {name}", file=sys.stderr)
        sys.exit(1)
    m_desc = re.search(r"^description:[ \t]*(.*?)(?=\n[a-zA-Z0-9_-]+:|\Z)", fm, re.DOTALL | re.MULTILINE)
    if not m_desc:
        print("Error: frontmatter 缺少 description", file=sys.stderr)
        sys.exit(1)
    body = m_desc.group(1)
    # 块标量（> 或 |）内的 ": " 是字面文本，不会破坏 YAML；只有内联标量才有该风险。
    if not body.lstrip().startswith((">", "|")) and re.search(r":\s", body):
        print(f"Error: {path} 的 description 是内联标量且含 ASCII \": \"，会被 YAML 当作键值分隔而解析失败", file=sys.stderr)
        sys.exit(1)
    for line in fm.splitlines():
        if line and not line[0].isspace() and not re.match(r"^[a-zA-Z0-9_-]+:", line):
            print(f"Error: frontmatter 存在非法顶层行: {line!r}", file=sys.stderr)
            sys.exit(1)
    if not re.search(r"^metadata:[ \t]*$", fm, re.MULTILINE):
        print("Error: frontmatter 缺少 metadata 映射", file=sys.stderr)
        sys.exit(1)
    lines = body.strip().splitlines()
    if lines and lines[0].lstrip().startswith((">", "|")):
        lines = lines[1:]
    desc = " ".join(part.strip() for part in lines).strip()
    print(f"description 长度: {len(desc)} 字符 (上限 1024) [降级校验]")
    if not desc:
        print(f"Error: {path} frontmatter 缺少非空 description", file=sys.stderr)
        sys.exit(1)
    if len(desc) > 1024:
        print(f"Error: {path} description 长度不合规 ({len(desc)} > 1024)", file=sys.stderr)
        sys.exit(1)

print(f"OK: {path} frontmatter 校验通过")
PY
  # 版本只由 scripts/skill_version.py 提取：与第 3 节比较的是同一个解析结果，
  # 重复 version 键与错位的 version 在提取处就被拒绝。
  SKILL_VERSION="$(python3 scripts/skill_version.py "$SKILL_FILE")"
  echo "OK: $SKILL_FILE metadata.version = $SKILL_VERSION (scripts/skill_version.py)"
done

echo "=== 3. 版本号跨文件一致性检查 ==="
python3 -c '
import glob, re, subprocess, sys

# 版本一致性按子项目分组校验：项目内部必须一致，跨项目不要求相同版本。
# SKILL.md 的版本用 pattern=None 标记，由 scripts/skill_version.py 统一提取；
# 不再用行首正则从全文猜第一个 version。
groups = {
    "easythink": {
        "root README": ("README.md", r"\|\s*\[EasyThink\]\(easythink/README\.md\)\s*\|\s*([0-9]+\.[0-9]+\.[0-9]+)\s*\|"),
        "easythink README": ("easythink/README.md", r"^版本：`([0-9]+\.[0-9]+\.[0-9]+)`"),
        "SKILL.md": ("easythink/SKILL.md", None),
        "CHANGELOG.md": ("easythink/CHANGELOG.md", r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)\s*更新说明"),
        "tests/behavior.md": ("easythink/tests/behavior.md", r"\|\s*20[0-9]{2}-[0-9]{2}-[0-9]{2}\s*\|\s*([0-9]+\.[0-9]+\.[0-9]+)\s*\|")
    },
    "easydev": {
        "root README": ("README.md", r"\|\s*\[EasyDev\]\(easydev/README\.md\)\s*\|\s*([0-9]+\.[0-9]+\.[0-9]+)\s*\|"),
        "README.md": ("easydev/README.md", r"^当前版本：`([0-9]+\.[0-9]+\.[0-9]+)`"),
        "SKILL.md": ("easydev/SKILL.md", None),
        "CHANGELOG.md": ("easydev/CHANGELOG.md", r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)\s*更新说明")
    }
}

# 覆盖断言：仓库里每个含 SKILL.md 的技能目录都必须登记版本来源，避免新增技能静默漏检。
skill_dirs = {p.split("/")[0] for p in glob.glob("*/SKILL.md")}
unregistered = sorted(skill_dirs - set(groups))
if unregistered:
    print(f"Error: 技能目录未登记版本来源: {unregistered}", file=sys.stderr)
    sys.exit(1)

errors = []
for project, spec in groups.items():
    versions = {}
    for name, (path, pattern) in spec.items():
        if pattern is None:
            proc = subprocess.run(
                [sys.executable, "scripts/skill_version.py", path],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                errors.append(
                    f"无法在 {path} ({name}) 中提取版本号: {proc.stderr.strip()}"
                )
                continue
            versions[name] = proc.stdout.strip()
            continue
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(pattern, content, re.MULTILINE)
        if not m:
            errors.append(f"无法在 {path} ({name}) 中提取版本号")
            continue
        versions[name] = m.group(1)
    print(f"提取版本号 [{project}]: {versions}")
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        errors.append(f"{project} 版本号不一致: {versions}")
        continue
    print(f"OK: {project} 版本号一致 -> {unique_versions.pop()}")

# 一次列全所有项目的版本问题，避免逐个修复后才发现下一个。
if errors:
    for err in errors:
        print(f"Error: {err}", file=sys.stderr)
    sys.exit(1)
'

echo "=== 4. Markdown 相对链接与 GitHub Slug 标题锚点精确检查 ==="
python3 -c '
import glob, os, re, sys

FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")

def strip_fenced_code(content):
    """移除围栏代码块（``` / ~~~），代码块内的 # 行不是标题。"""
    out = []
    fence = None
    for line in content.splitlines():
        m = FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
                out.append("")
                continue
            out.append(line)
            continue
        # 已在围栏内：仅在出现同类且不短于开启处的围栏时闭合
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
            fence = None
        out.append("")
    return "\n".join(out)

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
        content = strip_fenced_code(f.read())
    for line in content.splitlines():
        # 缩进 4 个空格以上属于缩进代码块，其中的 # 不是标题
        if len(line) - len(line.lstrip()) >= 4:
            continue
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
    "easythink/README.md",
    "easythink/SKILL.md",
    "easythink/CHANGELOG.md",
    "easythink/references/methods.md",
    "easythink/references/examples.md",
    "easythink/tests/behavior.md"
] + sorted(glob.glob("easydev/**/*.md", recursive=True))

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

for readme in ("easythink/README.md", "easydev/README.md"):
    with open(readme, "r", encoding="utf-8") as f:
        content = f.read()

    bash_blocks = re.findall(r"^[ \t]*```bash\r?\n(.*?)\r?\n[ \t]*```", content, re.DOTALL | re.MULTILINE)
    print(f"{readme}: 找到 {len(bash_blocks)} 个 bash 代码块，正在逐个运行 bash -n 语法解析...")
    if len(bash_blocks) == 0:
        print(f"Error: 未在 {readme} 中找到任何 bash 代码块", file=sys.stderr)
        sys.exit(1)

    for i, block in enumerate(bash_blocks, 1):
        proc = subprocess.run(["bash", "-n"], input=block, text=True, capture_output=True)
        if proc.returncode != 0:
            print(f"Error: {readme} 第 {i} 个 bash 代码块语法错误:\n{proc.stderr}", file=sys.stderr)
            sys.exit(1)

print("OK: 所有 README bash 代码块语法均合法")
'

echo "=== 6. 模型配置示例校验 ==="
python3 -c '
import glob, json, re, sys

files = sorted(glob.glob("easydev/examples/*.json"))
if not files:
    print("Error: easydev/examples 下没有 JSON 示例", file=sys.stderr)
    sys.exit(1)

for path in files:
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
    except json.JSONDecodeError as exc:
        print(f"Error: {path} 不是合法 JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: {path} 解析通过")

example = "easydev/examples/models.cross-provider.json"
if example in files:
    with open("easydev/README.md", encoding="utf-8") as f:
        readme = f.read()
    m = re.search(r"^```json\n(.*?)^```", readme, re.DOTALL | re.MULTILINE)
    if not m:
        print("Error: easydev/README.md 未找到 json 代码块", file=sys.stderr)
        sys.exit(1)
    try:
        block = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        print(f"Error: easydev/README.md 内联示例不是合法 JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    with open(example, encoding="utf-8") as f:
        reference = json.load(f)
    if block != reference:
        print(f"Error: easydev/README.md 内联示例与 {example} 不一致", file=sys.stderr)
        sys.exit(1)
    print(f"OK: easydev/README.md 内联示例与 {example} 一致")
'

echo "=== 全部自动化门禁检查通过 ==="
