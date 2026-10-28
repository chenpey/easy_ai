#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "=== 1. 已跟踪文件行尾空白检查（不依赖提交深度） ==="
# 直接扫描已跟踪文本文件：浅克隆（fetch-depth=1）或干净工作区都不会退化为空检查。
# 新增文件类型时同步补充扩展名，否则该类型不受门禁覆盖。
# *.patch 等格式文件含语义化行尾空格，不纳入扫描。
WS_PATHS=('*.md' '*.sh' '*.yml' '*.yaml' '*.json' '*.py' '*.ts' '*.svg' '*.txt')
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
# 体量上限包含 UTF-8 正文和 frontmatter，防止入口重新膨胀。
for SKILL_NAME in easythink easydev; do
  SKILL_FILE="$SKILL_NAME/SKILL.md"
  if [ ! -f "$SKILL_FILE" ]; then
    echo "Error: $SKILL_FILE 不存在" >&2
    exit 1
  fi
  SKILL_SIZE=$(tr -d '\r' < "$SKILL_FILE" | wc -c | tr -d ' ')
  case "$SKILL_NAME" in
    easythink) MAX_SIZE=12288 ;;
    easydev) MAX_SIZE=4096 ;;
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
        "CHANGELOG.md": ("easydev/CHANGELOG.md", r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)\s*更新说明"),
        "hooks/manifest.json": ("easydev/hooks/manifest.json", r"^\s*\"release\":\s*\"([0-9]+\.[0-9]+\.[0-9]+)\"")
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
    "AGENTS.md",
    "easythink/README.md",
    "easythink/SKILL.md",
    "easythink/CHANGELOG.md",
    "easythink/references/methods.md",
    "easythink/references/examples.md",
    "easythink/tests/behavior.md"
] + sorted(glob.glob("agent-rules/**/*.md", recursive=True)) + sorted(glob.glob("easydev/**/*.md", recursive=True))

file_slugs = {f: extract_slugs(f) for f in files}
link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

for f in files:
    with open(f, "r", encoding="utf-8") as fh:
        content = fh.read()
    base_dir = os.path.dirname(f)
    for text, url in link_pattern.findall(content):
        if url.startswith(("http://", "https://", "mailto:", "file://")):
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

for readme in ("README.md", "easythink/README.md", "easydev/README.md"):
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

echo "=== 6. 模型配置内联示例校验（共享 validator） ==="
# schema 与运行期共用 easydev/hooks/models.py：这里只负责从 README 提取示例，
# 再断言示例演示的跨供应商升级形状；不再复制一份字段规则。
python3 - <<'PY'
import json, re, sys

sys.path.insert(0, "easydev")
from hooks import models


def fail(message):
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def provider_of(model):
    """供应商前缀；inherit 没有供应商。"""
    return None if model == "inherit" else model.split("/", 1)[0]


with open("easydev/README.md", encoding="utf-8") as f:
    readme = f.read()
m = re.search(r"^```json\n(.*?)^```", readme, re.DOTALL | re.MULTILINE)
if not m:
    fail("easydev/README.md 未找到 json 代码块")
try:
    block = models.parse_json_text(m.group(1))
except models.ConfigError as exc:
    fail(f"easydev/README.md 内联示例不是合法 JSON: {exc}")
try:
    models.validate_config(block)
except models.ConfigError as exc:
    fail(f"easydev/README.md 内联示例不符合共享模型 schema: {exc}")

# README 是唯一示例事实源，所以示例要演示它自己声明的形状：
# 「开发与审查分属不同供应商，升级阶段换更强模型且两级都保持不同供应商」。
upgrades = block.get("upgrades", [])
if not upgrades:
    fail("内联示例的 upgrades 为空，无法演示升级阶段的模型更换")
developer_provider = provider_of(block["developer"]["model"])
reviewer_provider = provider_of(block["reviewer"]["model"])
if developer_provider is None or reviewer_provider is None:
    fail("内联示例的基础角色应为具体供应商，不能用 inherit 演示跨供应商")
if developer_provider == reviewer_provider:
    fail("内联示例的基础开发与审查角色应分属不同供应商")
first = upgrades[0]
first_developer = provider_of(first["developer"]["model"])
first_reviewer = provider_of(first["reviewer"]["model"])
if first_developer is None or first_reviewer is None:
    fail("内联示例 upgrades[1] 应为具体供应商，不能用 inherit 演示")
if first_developer == first_reviewer:
    fail("内联示例 upgrades[1] 的开发与审查应分属不同供应商")
if first_developer == developer_provider and first_reviewer == reviewer_provider:
    fail("内联示例 upgrades[1] 未更换任何一个角色的供应商，无法演示跨供应商升级")

print("OK: easydev/README.md 内联示例符合共享模型 schema 与跨供应商升级示例")
PY

echo "=== 6b. Plugin 脚本、模板与分发契约静态检查 ==="
# 只锁可机器判断的事实：语法、JSON 可解析、分发文件清单、pi 适配器契约与
# 专用回放守卫的关键常量。字符串门禁不等于类型检查，也不证明宿主已加载。
python3 - <<'PY'
import ast
import glob
import json
import os
import sys

errors = []

python_files = sorted(glob.glob("easydev/hooks/**/*.py", recursive=True))
python_files.append("easydev/tests/experiments/agent_runner/replay_read_guard.py")
for path in python_files:
    try:
        ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except SyntaxError as exc:
        errors.append(f"{path} 语法错误: {exc}")

for path in sorted(glob.glob("easydev/hooks/templates/*.json")) + ["easydev/hooks/manifest.json"]:
    try:
        with open(path, encoding="utf-8") as handle:
            json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{path} 不是合法 JSON: {exc}")

try:
    with open("easydev/hooks/manifest.json", encoding="utf-8") as handle:
        manifest = json.load(handle)
    for key in ("name", "release", "protocol", "distribution"):
        if key not in manifest:
            errors.append(f"manifest.json 缺 {key}")
    files = manifest.get("distribution", {}).get("files", [])
    if not isinstance(files, list) or not files:
        errors.append("manifest.json distribution.files 为空")
    forbidden = [item for item in files if item.startswith("tests") or item in {"SKILL.md"} or item.startswith("references")]
    if forbidden:
        errors.append(f"Plugin 分发不得包含 Skill/tests: {forbidden}")
    for required in ("core.py", "install.py", "pi/index.ts", "templates/codex.json"):
        if required not in files:
            errors.append(f"manifest.json 分发清单缺 {required}")
except (OSError, json.JSONDecodeError) as exc:
    errors.append(f"manifest.json 不可读: {exc}")

required_events = ("SessionStart", "PreToolUse", "PostToolUse", "Stop")
# Antigravity 官方两种事件数组结构：工具事件 Grouped（matcher + hooks[]），
# 其余事件 Flat（直接是 handler 对象）；把非工具事件包一层就读不到 command。
ANTIGRAVITY_TOOL_EVENTS = ("PreToolUse", "PostToolUse")


def hook_buckets(path, template):
    """Codex/DSH 用顶层 hooks；Antigravity 用唯一命名组 easydev-hooks。"""
    hooks = template.get("hooks")
    if isinstance(hooks, dict):
        return hooks
    named = template.get("easydev-hooks")
    if isinstance(named, dict):
        others = [key for key in template if key != "easydev-hooks"]
        if others:
            errors.append(f"{path} 命名 Hook 组之外还有顶层键: {others}")
        return named
    errors.append(f"{path} 缺 hooks 映射或 easydev-hooks 命名组")
    return None


for path in sorted(glob.glob("easydev/hooks/templates/*.json")):
    try:
        with open(path, encoding="utf-8") as handle:
            template = json.load(handle)
    except (OSError, json.JSONDecodeError):
        continue
    hooks = hook_buckets(path, template)
    if hooks is None:
        continue
    events = list(required_events)
    if path.endswith("antigravity.json"):
        # 激活事件（PreInvocation）是 EasyDev 能否生效的前提，必须注册且为 Flat。
        events.append("PreInvocation")
    for event in events:
        if event not in hooks:
            errors.append(f"{path} 缺事件 {event}")
            continue
        flat = path.endswith("antigravity.json") and event not in ANTIGRAVITY_TOOL_EVENTS
        for entry in hooks[event]:
            if not isinstance(entry, dict):
                errors.append(f"{path} 的 {event} 槽位条目必须是对象")
                continue
            if flat:
                if isinstance(entry.get("hooks"), list):
                    errors.append(
                        f"{path} 的 {event} 是非工具事件，必须是 Flat handler 对象，不能再包 hooks 数组"
                    )
                elif entry.get("type") != "command" or not isinstance(entry.get("command"), str):
                    errors.append(f"{path} 的 {event} 槽位必须是 {type: command, command: …} handler")
            elif not isinstance(entry.get("hooks"), list):
                errors.append(f"{path} 的 {event} 槽位条目必须是含 hooks 数组的匹配器对象")
            elif "matcher" not in entry and path.endswith("antigravity.json"):
                errors.append(f"{path} 的 {event} 槽位必须带 matcher")
    text = json.dumps(template)
    for token in ("additionalContext", "overwrite", "systemPrompt", "instructionsText"):
        if token in text:
            errors.append(f"{path} 含注入/覆写字段 {token}")

pi_entry = "easydev/hooks/index.ts"
pi_adapter = "easydev/hooks/pi/index.ts"
for path in (pi_entry, pi_adapter):
    if not os.path.isfile(path):
        errors.append(f"缺少 pi 适配文件 {path}")
if os.path.isfile(pi_entry):
    entry_text = open(pi_entry, encoding="utf-8").read()
    if 'export { default } from "./pi/index.ts"' not in entry_text:
        errors.append("hooks/index.ts 必须只做 ./pi/index.ts 的薄入口")
if os.path.isfile(pi_adapter):
    adapter = open(pi_adapter, encoding="utf-8").read()
    for needle in ('pi.on("input"', 'pi.on("tool_call"', 'pi.on("tool_result"', 'pi.on("agent_before_settle"'):
        if needle not in adapter:
            errors.append(f"pi 适配器缺 {needle}")
    for needle in ("EXEC_TIMEOUT_MS = 2000", "MAX_BUFFER = 256 * 1024", "block: true", "catch"):
        if needle not in adapter:
            errors.append(f"pi 适配器缺契约: {needle}")
    if "exec(" in adapter:
        errors.append("pi 适配器不得使用 shell exec")

core_source = open("easydev/hooks/core.py", encoding="utf-8").read()
for name in ("normalize_antigravity", "normalize_codex", "normalize_dsh", "normalize_pi"):
    if f"def {name}" not in core_source:
        errors.append(f"core.py 缺宿主归一化 {name}")
if '"force_ask"' not in core_source and "'force_ask'" not in core_source:
    errors.append("core.py 缺 force_ask 适配语义")
if 'add_argument("--event"' not in core_source:
    errors.append("core.py 缺 --event（Antigravity stdin 不带事件名）")

install_source = open("easydev/hooks/install.py", encoding="utf-8").read()
for needle in ('subparsers.add_parser("generate")', 'subparsers.add_parser("apply")', 'subparsers.add_parser("uninstall")', '"install_tests": False', '"install_skill": False', '--event %s', 'HOOK_EVENT_NAMES'):
    if needle not in install_source:
        errors.append(f"install.py 缺契约: {needle}")
if "SKILL.md" in install_source or "tests/" in install_source:
    errors.append("install.py 不得安装 Skill 或 tests")

guard_source = open("easydev/tests/experiments/agent_runner/replay_read_guard.py", encoding="utf-8").read()
for needle in ("DEFAULT_MAX_ENTRIES = 20000", "DEFAULT_DEADLINE_MS = 2000", "permissionDecision"):
    if needle not in guard_source:
        errors.append(f"replay_read_guard.py 缺契约: {needle}")

if errors:
    for error in errors:
        print(f"Error: {error}", file=sys.stderr)
    sys.exit(1)
print("OK: Plugin 脚本语法、模板/manifest JSON、分发清单与适配契约通过")
PY

echo "=== 7. 规则同步脚本语法检查 ==="
bash -n scripts/sync-agent-rules.sh
echo "OK: scripts/sync-agent-rules.sh 语法合法"
if command -v pwsh >/dev/null 2>&1; then
  pwsh -NoProfile -Command '
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile("scripts/sync-agent-rules.ps1", [ref]$null, [ref]$errors)
    if ($errors) { $errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }; exit 1 }
  '
  echo "OK: scripts/sync-agent-rules.ps1 语法合法"
else
  echo "SKIP: 未安装 pwsh，跳过 sync-agent-rules.ps1 语法检查（CI 的 Windows 作业覆盖）"
fi

echo "=== 8. 宿主规则文件 frontmatter 校验 ==="
python3 - <<'PY'
import glob, sys

ALLOWED = ["always_on", "model_decision", "glob", "manual"]
paths = sorted(glob.glob("agent-rules/*.md"))
if not paths:
    print("Error: agent-rules/ 下没有规则文件", file=sys.stderr)
    sys.exit(1)

def frontmatter(text, path):
    if not text.startswith("---\n"):
        print(f"Error: {path} 缺少 YAML frontmatter", file=sys.stderr)
        sys.exit(1)
    end = text.find("\n---\n", 3)
    if end < 0:
        print(f"Error: {path} frontmatter 未闭合", file=sys.stderr)
        sys.exit(1)
    return text[4:end]

try:
    import yaml
except ImportError:
    yaml = None

# 降级模式只校验分隔符与 trigger，不得在汇总里宣称 frontmatter 整体合法。
degraded = yaml is None

for path in paths:
    text = open(path, encoding="utf-8").read()
    block = frontmatter(text, path)
    if yaml is None:
        # 降级模式：仅接受 `trigger: <值>`（值可整体加引号），不支持行尾注释等复杂写法。
        trigger = None
        for line in block.splitlines():
            if line.startswith("trigger:"):
                raw = line.split(":", 1)[1].strip()
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                    raw = raw[1:-1]
                elif any(ch in raw for ch in "\"'#"):
                    print(f"Error: {path} 降级模式不支持该 trigger 写法: {line!r}", file=sys.stderr)
                    sys.exit(1)
                trigger = raw
        if trigger not in ALLOWED:
            print(f"Error: {path} trigger 非法或缺失: {trigger!r}", file=sys.stderr)
            sys.exit(1)
        print(f"OK(降级，未安装 PyYAML): {path} trigger={trigger}")
    else:
        try:
            data = yaml.safe_load(block)
        except Exception as exc:
            print(f"Error: {path} frontmatter 不是合法 YAML: {exc}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, dict):
            print(f"Error: {path} frontmatter 不是映射", file=sys.stderr)
            sys.exit(1)
        trigger = data.get("trigger")
        if trigger not in ALLOWED:
            print(f"Error: {path} trigger 非法或缺失: {trigger!r}", file=sys.stderr)
            sys.exit(1)
if degraded:
    print(f"OK(降级)：{len(paths)} 个宿主规则文件的 frontmatter 边界与 trigger 合法（未安装 PyYAML，未验证完整 YAML）")
else:
    print(f"OK: {len(paths)} 个宿主规则文件 frontmatter 合法")
PY

echo "=== 9. 边界与预算一致性检查 ==="
# 两条门禁：① 全局规则与技能内的修复次数上限必须同值（各处手写，防漂移）；
# ② 可分发的 easydev 不得引用“全局规则”，CHANGELOG 的历史记录除外。
python3 - <<'PY'
import re, subprocess, sys

errors = []
CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def extract(pattern, text, label):
    m = re.search(pattern, text)
    if not m:
        errors.append(f"无法从 {label} 提取修复次数上限（措辞变了就同步更新本门禁）")
        return None
    raw = m.group(1)
    return int(raw) if raw.isdigit() else CN.get(raw)


limits = {
    "AGENTS.md": extract(
        r"同一问题累计修复\s*(\d+)\s*次仍未解决", read("AGENTS.md"), "AGENTS.md"
    ),
    "easydev/references/routing.md": extract(
        r"最多\s*([一二三四五六七八九十\d]+)\s*次有新证据的候选修复尝试",
        read("easydev/references/routing.md"),
        "easydev/references/routing.md「尝试预算」",
    ),
    "easydev/SKILL.md": extract(
        r"([一二三四五六七八九十\d]+)\s*次仍失败即停",
        read("easydev/SKILL.md"),
        "easydev/SKILL.md「quick 执行核心」",
    ),
}
missing = sorted(name for name, value in limits.items() if value is None)
if missing:
    errors.append(f"修复次数上限无法提取：{missing}")
elif len(set(limits.values())) > 1:
    errors.append(f"修复次数上限不一致：{limits}")
else:
    print(f"OK: 修复次数上限一致 -> {next(iter(limits.values()))}（{len(limits)} 处）")

# review.md 把 AGENTS.md 当作“审查数据”举例属正常用法，本门禁只拦“全局规则”这类跨层引用。
proc = subprocess.run(
    ["git", "grep", "-nI", "全局规则", "--", "easydev", ":!easydev/CHANGELOG.md"],
    capture_output=True,
    text=True,
)
if proc.returncode not in (0, 1):
    errors.append(f"git grep 执行失败 (exit {proc.returncode}): {proc.stderr.strip()}")
elif proc.returncode == 0:
    errors.append("easydev 不得引用“全局规则”：\n" + proc.stdout.rstrip())
else:
    print("OK: easydev 未引用“全局规则”（CHANGELOG 历史记录除外）")

# Hook 契约：只锁可机器判断的事实，不用关键词模拟安全行为。
# ① 模板不得含 SessionStart 注入/覆写字段，也不得替模型定档；
# ② 可选 Plugin 不进入 Skill 安装集（manifest 与安装器都不含 SKILL/tests）；
# ③ 核心不得出现自动档位选择开关。
import glob as _glob
import json as _json

template_text = ""
for _path in _glob.glob("easydev/hooks/templates/*.json"):
    template_text += open(_path, encoding="utf-8").read()
for _token in ("additionalContext", "overwrite", "systemPrompt", "instructionsText"):
    if _token in template_text:
        errors.append(f"Hook 模板出现 SessionStart 注入/覆写字段：{_token}")
_manifest = _json.load(open("easydev/hooks/manifest.json", encoding="utf-8"))
_manifest_files = _manifest.get("distribution", {}).get("files", [])
if any(item.startswith(("tests", "references")) or item == "SKILL.md" for item in _manifest_files):
    errors.append("Plugin 分发清单不得包含 Skill 或 tests")
_install_source = open("easydev/hooks/install.py", encoding="utf-8").read()
if "SKILL.md" in _install_source or "tests/" in _install_source:
    errors.append("安装器不得安装 Skill 或 tests")
_core_source = open("easydev/hooks/core.py", encoding="utf-8").read()
for _token in ("auto_select", "auto_tier", "set_path"):
    if _token in _core_source:
        errors.append(f"Hook 核心出现自动定档开关：{_token}")

if errors:
    for err in errors:
        print(f"Error: {err}", file=sys.stderr)
    sys.exit(1)
PY

echo "=== 10. 规则文档总量棘轮（只减不增） ==="
# 规则正文做减法：入口与 references 合计不得越过基线，新增规则必须同时删掉等量篇幅。
# 范围只含 SKILL.md 与 references/*.md（运行时实际下发的规则正文）；维护材料与 README 不计。
# 基线取 1.4.0 实测总量（1.3.2 之后随 models.md 措辞与精简同步下调），之后只允许在删除正文后向下重设。
# 改 MAX_TOTAL 必须同步 CHANGELOG 对应版本的数值；扩容需在 CHANGELOG 说明理由。
MAX_TOTAL=131715
TOTAL=$(cat easydev/SKILL.md easydev/references/*.md | tr -d '\r' | wc -c | tr -d ' ')
echo "规则文档总字节: $TOTAL (棘轮上限: $MAX_TOTAL)"
if [ "$TOTAL" -gt "$MAX_TOTAL" ]; then
  echo "Error: 规则文档总量超过棘轮 ($TOTAL > $MAX_TOTAL)；先删减同等篇幅再新增，或按 CHANGELOG 说明理由上调上限。" >&2
  exit 1
fi
# 基线双写：CHANGELOG 必须写明同一个上限（含「合计不得超过 N 字节」措辞），防止两处静默漂移。
if ! grep -q "合计不得超过 $MAX_TOTAL 字节" easydev/CHANGELOG.md; then
  echo "Error: MAX_TOTAL=$MAX_TOTAL 未出现在 CHANGELOG；基线双写须同步" >&2
  exit 1
fi
if [ "$TOTAL" -lt "$MAX_TOTAL" ]; then
  echo "提示: 总量低于棘轮 $((MAX_TOTAL - TOTAL)) 字节，可把 MAX_TOTAL 向下重设为 ${TOTAL}（并同步 CHANGELOG）"
fi
echo "OK: 规则文档总量未超棘轮 ($TOTAL ≤ $MAX_TOTAL)"

echo "=== 全部自动化门禁检查通过 ==="
