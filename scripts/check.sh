#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "=== 1. 已跟踪文件行尾空白检查（不依赖提交深度） ==="
# 直接扫描已跟踪文本文件：浅克隆（fetch-depth=1）或干净工作区都不会退化为空检查。
# 新增文件类型时同步补充扩展名，否则该类型不受门禁覆盖。
# *.patch 等格式文件含语义化行尾空格，不纳入扫描。
WS_PATHS=('*.md' '*.sh' '*.ps1' '*.yml' '*.yaml' '*.json' '*.py' '*.ts' '*.svg' '*.txt' '*.ini')
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
for SKILL_NAME in easythink easydev easypi; do
  SKILL_FILE="$SKILL_NAME/SKILL.md"
  if [ ! -f "$SKILL_FILE" ]; then
    echo "Error: $SKILL_FILE 不存在" >&2
    exit 1
  fi
  SKILL_SIZE=$(tr -d '\r' < "$SKILL_FILE" | wc -c | tr -d ' ')
  case "$SKILL_NAME" in
    easythink) MAX_SIZE=12288 ;;
    easydev) MAX_SIZE=5000 ;;
    easypi) MAX_SIZE=5000 ;;
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
    print("提示: 未安装 PyYAML，启用严格降级校验（拒绝内联 description 中的 ASCII \": \"、未知或非法顶层行）")
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
    allowed_keys = {"name", "description", "metadata"}
    for line in fm.splitlines():
        if not line or line[0].isspace():
            continue
        key = re.match(r"^([a-zA-Z0-9_-]+):", line)
        if key is None:
            print(f"Error: frontmatter 存在非法顶层行: {line!r}", file=sys.stderr)
            sys.exit(1)
        if key.group(1) not in allowed_keys:
            print(f"Error: frontmatter 含未知顶层字段: {key.group(1)!r}", file=sys.stderr)
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
        # 回归记录从 behavior.md 拆到 tests/runs/README.md，版本仍取最新一行的日期行。
        "tests/runs/README.md": ("easythink/tests/runs/README.md", r"\|\s*20[0-9]{2}-[0-9]{2}-[0-9]{2}\s*\|\s*([0-9]+\.[0-9]+\.[0-9]+)\s*\|"),
        "mindmap title": ("easythink/assets/easythink-mindmap.svg", r"<title[^>]*>EasyThink\s+([0-9]+\.[0-9]+\.[0-9]+)\s"),
        "mindmap eyebrow": ("easythink/assets/easythink-mindmap.svg", r">EASYTHINK\s+([0-9]+\.[0-9]+\.[0-9]+)\s")
    },
    "easydev": {
        "root README": ("README.md", r"\|\s*\[EasyDev\]\(easydev/README\.md\)\s*\|\s*([0-9]+\.[0-9]+\.[0-9]+)\s*\|"),
        "README.md": ("easydev/README.md", r"^当前版本：`([0-9]+\.[0-9]+\.[0-9]+)`"),
        "README.md mindmap alt": ("easydev/README.md", r"!\[EasyDev\s+([0-9]+\.[0-9]+\.[0-9]+)\s"),
        "SKILL.md": ("easydev/SKILL.md", None),
        "CHANGELOG.md": ("easydev/CHANGELOG.md", r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)\s*更新说明"),
        "mindmap title": ("easydev/assets/easydev-mindmap.svg", r"<title[^>]*>EasyDev\s+([0-9]+\.[0-9]+\.[0-9]+)\s"),
        "mindmap eyebrow": ("easydev/assets/easydev-mindmap.svg", r">EASYDEV\s+([0-9]+\.[0-9]+\.[0-9]+)\s"),
    },
    "easypi": {
        "root README": ("README.md", r"\|\s*\[EasyPi\]\(easypi/README\.md\)\s*\|\s*([0-9]+\.[0-9]+\.[0-9]+)\s*\|"),
        "README.md": ("easypi/README.md", r"^当前版本：`([0-9]+\.[0-9]+\.[0-9]+)`"),
        "SKILL.md": ("easypi/SKILL.md", None),
        "CHANGELOG.md": ("easypi/CHANGELOG.md", r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)\s*更新说明"),
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
    "easythink/references/safety.md",
    "easythink/references/examples.md",
    "easythink/tests/behavior.md"
] + sorted(glob.glob("easythink/tests/runs/**/*.md", recursive=True)) + sorted(glob.glob("agent-rules/**/*.md", recursive=True)) + sorted(glob.glob("easydev/**/*.md", recursive=True)) + sorted(glob.glob("easypi/**/*.md", recursive=True))

file_slugs = {f: extract_slugs(f) for f in files}
link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

for f in files:
    with open(f, "r", encoding="utf-8") as fh:
        content = strip_fenced_code(fh.read())
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

for readme in ("README.md", "easythink/README.md", "easydev/README.md", "easypi/README.md"):
    with open(readme, "r", encoding="utf-8") as f:
        content = f.read()

    bash_blocks = re.findall(r"^[ \t]*```bash\r?\n(.*?)\r?\n[ \t]*```", content, re.DOTALL | re.MULTILINE)
    print(f"{readme}: 找到 {len(bash_blocks)} 个 bash 代码块，正在逐个运行 bash -n 语法解析...")
    if len(bash_blocks) == 0:
        continue

    for i, block in enumerate(bash_blocks, 1):
        proc = subprocess.run(["bash", "-n"], input=block, text=True, capture_output=True)
        if proc.returncode != 0:
            print(f"Error: {readme} 第 {i} 个 bash 代码块语法错误:\n{proc.stderr}", file=sys.stderr)
            sys.exit(1)

print("OK: 所有 README bash 代码块语法均合法")
'

echo "=== 6. 模型配置内联示例校验（共享 validator） ==="
# schema 与 scripts/models.py 共用同一 validator：这里只负责从 README 提取示例，
# 再断言示例同时演示跨供应商角色配置与 windows 时段切换；不复制字段规则。
python3 - <<'PY'
import json, re, sys

sys.path.insert(0, "scripts")
import models


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

# README 是唯一示例事实源：示例必须演示它自己声明的形状——跨供应商角色配置
# 与 windows 时段切换，且不得再出现已移除的 upgrades 键。
if "upgrades" in block:
    fail("内联示例不应再包含已移除的 upgrades 键")
developer_provider = provider_of(block["developer"]["model"])
reviewer_provider = provider_of(block["reviewer"]["model"])
if developer_provider is None or reviewer_provider is None:
    fail("内联示例的开发与审查角色应为具体供应商，不能用 inherit 演示跨供应商")
if developer_provider == reviewer_provider:
    fail("内联示例的开发与审查角色应分属不同供应商")
if not block.get("windows"):
    fail("内联示例应演示 windows 时段切换（键为北京时间时段 HH:MM-HH:MM）")
print("OK: easydev/README.md 内联示例符合共享模型 schema、跨供应商角色与 windows 时段示例")
PY

echo "=== 6b. Python 脚本与回放守卫静态检查 ==="
python3 - <<'PY'
import ast
import os
import subprocess
import sys
import warnings

errors = []

python_files = sorted(
    subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
)


def position(exc):
    """诊断里的行号：优先用异常自身的 lineno。"""
    lineno = getattr(exc, "lineno", None)
    return f" (line {lineno})" if lineno else ""


for path in python_files:
    source = open(path, encoding="utf-8").read()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            warnings.filterwarnings(
                "error", message=r"invalid escape sequence.*", category=DeprecationWarning
            )
            ast.parse(source, filename=path)
            compile(source, path, "exec", flags=0, dont_inherit=True)
    except (SyntaxWarning, DeprecationWarning) as exc:
        errors.append(f"{path} 编译期告警（如非法转义序列）{position(exc)}: {exc}")
    except SyntaxError as exc:
        errors.append(f"{path} 语法错误{position(exc)}: {exc}")

guard_source = open("easydev/tests/experiments/agent_runner/replay_read_guard.py", encoding="utf-8").read()
for needle in ("DEFAULT_MAX_ENTRIES = 20000", "DEFAULT_DEADLINE_MS = 2000", "permissionDecision"):
    if needle not in guard_source:
        errors.append(f"replay_read_guard.py 缺契约: {needle}")

if errors:
    for error in errors:
        print(f"Error: {error}", file=sys.stderr)
    sys.exit(1)
print("OK: 全部 Python 文件语法与编译期告警、回放守卫契约通过")
PY

echo "=== 7. 规则同步与辅助脚本语法检查 ==="
bash -n scripts/sync-agent-rules.sh
bash -n easypi/scripts/run_pi.sh
echo "OK: scripts/sync-agent-rules.sh 与 easypi/scripts/run_pi.sh 语法合法"

# 7b. run_pi.sh 的硬超时、正常退出清理与参数守卫：桩 pi 忽略 SIGTERM 并派生 sleep 子进程，
# 超时必须 SIGKILL 整组并以 124 退出；外部 SIGTERM 也必须整组清理并以 143 退出；
# 桩正常退出时其忽略 TERM 的后台子进程也必须由 wrapper 在退出路径清掉（不靠 wrapper 退出码兜底）；
# 响应 SIGTERM 的桩（plain）必须靠 SIGTERM 立即收口，不得因僵尸组长占组而空等满宽限期。
RUNPI_TMP="$(mktemp -d "${TMPDIR:-/tmp}/run-pi-check.XXXXXX")"
runpi_bg=""

# 等待 PID 文件里记录的进程全部消失，上限 1s（SIGKILL 投递有延迟，单次 kill -0 会抖）。
runpi_wait_gone() {
  for _ in {1..50}; do
    runpi_alive=0
    while read -r runpi_pid; do
      if [ -n "$runpi_pid" ] && kill -0 "$runpi_pid" 2>/dev/null; then
        runpi_alive=1
      fi
    done < "$1"
    [ "$runpi_alive" -eq 0 ] && return 0
    sleep 0.02
  done
  return 1
}

# 中断或早退时：先按 PID 文件清桩进程组（其子进程在独立会话里，wrapper 死后就没人管），
# 再停 wrapper，最后删临时目录。
runpi_cleanup() {
  trap - EXIT INT TERM
  local pid_file stub_pid recorded
  for pid_file in "$RUNPI_TMP/pid" "$RUNPI_TMP/sig-pid" "$RUNPI_TMP/exit-pid" "$RUNPI_TMP/pass-pid" \
    "$RUNPI_TMP/plain-pid"; do
    [ -f "$pid_file" ] || continue
    stub_pid="$(head -n 1 "$pid_file")"
    if [ -n "$stub_pid" ]; then
      kill -KILL -- "-$stub_pid" >/dev/null 2>&1 || true
    fi
    while read -r recorded; do
      if [ -n "$recorded" ]; then
        kill -KILL "$recorded" >/dev/null 2>&1 || true
      fi
    done < "$pid_file"
  done
  if [ -n "$runpi_bg" ]; then
    kill -TERM "$runpi_bg" >/dev/null 2>&1 || true
    wait "$runpi_bg" >/dev/null 2>&1 || true
  fi
  rm -rf "$RUNPI_TMP"
}
trap runpi_cleanup EXIT
trap 'runpi_cleanup; exit 130' INT
trap 'runpi_cleanup; exit 143' TERM
mkdir -p "$RUNPI_TMP/bin"
cat > "$RUNPI_TMP/bin/pi" <<'RUNPI_STUB'
#!/usr/bin/env bash
printf '%s\n' "$$" > "$STUB_PID_FILE"
# 记录 wrapper 实际透传的参数（一行一个），用于验证白名单参数不是被守卫吞掉。
if [ -n "${STUB_ARGV_FILE:-}" ]; then
  printf '%s\n' "$@" > "$STUB_ARGV_FILE"
fi
if [ "${STUB_MODE:-hang}" = plain ]; then
  # 响应 SIGTERM 的桩：不装 trap '' TERM，wrapper 的 SIGTERM 就能收口整组。
  exec sleep 30
fi
trap '' TERM
sleep 30 &
printf '%s\n' "$!" >> "$STUB_PID_FILE"
if [ "${STUB_MODE:-hang}" = exit ]; then
  exit 0
fi
wait
RUNPI_STUB
chmod +x "$RUNPI_TMP/bin/pi"
printf 'prompt\n' > "$RUNPI_TMP/prompt.txt"
runpi_ok=1
runpi_code=0
STUB_PID_FILE="$RUNPI_TMP/pid" PATH="$RUNPI_TMP/bin:$PATH" EASYPI_TIMEOUT=2 \
  bash easypi/scripts/run_pi.sh stub-session "$RUNPI_TMP/prompt.txt" \
  > "$RUNPI_TMP/out.log" 2>&1 </dev/null || runpi_code=$?
if [ "$runpi_code" -ne 124 ]; then
  echo "Error: run_pi.sh 未按硬超时以 124 退出（exit=${runpi_code}）" >&2
  runpi_ok=0
fi
if [ ! -f "$RUNPI_TMP/pid" ] || [ "$(wc -l < "$RUNPI_TMP/pid")" -lt 2 ]; then
  echo "Error: 桩 pi 未记录 pi 与子进程 PID，无法证明整组清理" >&2
  runpi_ok=0
elif ! runpi_wait_gone "$RUNPI_TMP/pid"; then
  echo "Error: run_pi.sh 超时后仍有进程存活（1s 内未消失）" >&2
  runpi_ok=0
fi
# 响应 SIGTERM 的桩：SIGTERM 后必须立刻收口，不得因未回收的僵尸组长把整组探测成存活而空等 5s 宽限期。
# 旧实现此处耗满 5s 多（SECONDS 整数秒，新实现约 1s，取 3s 为界仍可判别）。
runpi_plain_code=0
runpi_plain_start=$SECONDS
STUB_MODE=plain STUB_PID_FILE="$RUNPI_TMP/plain-pid" PATH="$RUNPI_TMP/bin:$PATH" EASYPI_TIMEOUT=1 \
  bash easypi/scripts/run_pi.sh stub-session "$RUNPI_TMP/prompt.txt" \
  > "$RUNPI_TMP/plain.log" 2>&1 </dev/null || runpi_plain_code=$?
runpi_plain_elapsed=$((SECONDS - runpi_plain_start))
if [ "$runpi_plain_code" -ne 124 ]; then
  echo "Error: run_pi.sh 响应 SIGTERM 的桩未按硬超时以 124 退出（exit=${runpi_plain_code}）" >&2
  runpi_ok=0
fi
if [ "$runpi_plain_elapsed" -ge 3 ]; then
  echo "Error: run_pi.sh 响应 SIGTERM 的桩超时收口过慢（${runpi_plain_elapsed}s ≥ 3s，宽限期被空等）" >&2
  runpi_ok=0
fi
if [ ! -f "$RUNPI_TMP/plain-pid" ] || ! runpi_wait_gone "$RUNPI_TMP/plain-pid"; then
  echo "Error: run_pi.sh 响应 SIGTERM 的桩超时后仍有进程存活" >&2
  runpi_ok=0
fi
# 外部中断：SIGTERM 到 wrapper 也必须整组清理（桩忽略 TERM，靠 SIGKILL 收口），退出码 128+15=143。
STUB_PID_FILE="$RUNPI_TMP/sig-pid" PATH="$RUNPI_TMP/bin:$PATH" EASYPI_TIMEOUT=60 \
  bash easypi/scripts/run_pi.sh stub-session "$RUNPI_TMP/prompt.txt" \
  > "$RUNPI_TMP/sig.log" 2>&1 </dev/null &
runpi_bg=$!
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  [ -f "$RUNPI_TMP/sig-pid" ] && [ "$(wc -l < "$RUNPI_TMP/sig-pid")" -ge 2 ] && break
  sleep 0.2
done
if [ ! -f "$RUNPI_TMP/sig-pid" ] || [ "$(wc -l < "$RUNPI_TMP/sig-pid")" -lt 2 ]; then
  echo "Error: 外部 SIGTERM 用例未就绪（桩未记录两个 PID）" >&2
  runpi_ok=0
fi
kill -TERM "$runpi_bg" 2>/dev/null || true
runpi_sig_rc=0
wait "$runpi_bg" || runpi_sig_rc=$?
runpi_bg=""
if [ "$runpi_sig_rc" -ne 143 ]; then
  echo "Error: run_pi.sh 外部 SIGTERM 未以 143 退出（exit=${runpi_sig_rc}）" >&2
  runpi_ok=0
fi
if [ -f "$RUNPI_TMP/sig-pid" ] && ! runpi_wait_gone "$RUNPI_TMP/sig-pid"; then
  echo "Error: 外部 SIGTERM 后仍有 pi 进程存活（1s 内未消失）" >&2
  runpi_ok=0
fi
# 正常退出：桩记录 PID 后立即 exit 0，其忽略 TERM 的后台子进程必须被 wrapper 清掉，退出码保持 0。
runpi_exit_code=0
STUB_MODE=exit STUB_PID_FILE="$RUNPI_TMP/exit-pid" PATH="$RUNPI_TMP/bin:$PATH" EASYPI_TIMEOUT=60 \
  bash easypi/scripts/run_pi.sh stub-session "$RUNPI_TMP/prompt.txt" \
  > "$RUNPI_TMP/exit.log" 2>&1 </dev/null || runpi_exit_code=$?
if [ "$runpi_exit_code" -ne 0 ]; then
  echo "Error: run_pi.sh 正常退出路径未保持 pi 的退出码 0（exit=${runpi_exit_code}）" >&2
  runpi_ok=0
fi
if [ ! -f "$RUNPI_TMP/exit-pid" ] || [ "$(wc -l < "$RUNPI_TMP/exit-pid")" -lt 2 ]; then
  echo "Error: 正常退出用例未就绪（桩未记录两个 PID）" >&2
  runpi_ok=0
elif ! runpi_wait_gone "$RUNPI_TMP/exit-pid"; then
  echo "Error: run_pi.sh 正常退出后仍遗留忽略 TERM 的后台子进程" >&2
  runpi_ok=0
fi
# 允许清单内的参数必须真实按序透传给 pi（只验退出码发现不了取值或选项被守卫吞掉）。
runpi_pass_code=0
STUB_MODE=exit STUB_PID_FILE="$RUNPI_TMP/pass-pid" STUB_ARGV_FILE="$RUNPI_TMP/pass-argv" \
  PATH="$RUNPI_TMP/bin:$PATH" EASYPI_TIMEOUT=60 \
  bash easypi/scripts/run_pi.sh stub-session "$RUNPI_TMP/prompt.txt" \
  --model provider/id --thinking high --approve \
  > "$RUNPI_TMP/pass.log" 2>&1 </dev/null || runpi_pass_code=$?
if [ "$runpi_pass_code" -ne 0 ]; then
  echo "Error: run_pi.sh 误拒允许清单内的参数（exit=${runpi_pass_code}）" >&2
  runpi_ok=0
fi
runpi_pass_argv=""
if [ -f "$RUNPI_TMP/pass-argv" ]; then
  runpi_pass_argv="$(tr '\n' ' ' < "$RUNPI_TMP/pass-argv")"
fi
case " $runpi_pass_argv " in
  *" --model provider/id --thinking high --approve "*) ;;
  *)
    echo "Error: 允许清单参数未按序透传给 pi: ${runpi_pass_argv:-（桩未记录参数）}" >&2
    runpi_ok=0
    ;;
esac
for bad_args in \
  "-bad|$RUNPI_TMP/prompt.txt" \
  "stub-session|$RUNPI_TMP/prompt.txt|--session-id|other" \
  "stub-session|$RUNPI_TMP/prompt.txt|--model" \
  "stub-session|$RUNPI_TMP/prompt.txt|--model=" \
  "stub-session|$RUNPI_TMP/prompt.txt|--thinking=" \
  "stub-session|$RUNPI_TMP/prompt.txt|--model|--approve" \
  "stub-session|$RUNPI_TMP/prompt.txt|--thinking" \
  "stub-session|$RUNPI_TMP/prompt.txt|--model=--approve" \
  "stub-session|$RUNPI_TMP/prompt.txt|--thinking=-x" \
  "stub-session|$RUNPI_TMP/prompt.txt|-m" \
  "stub session|$RUNPI_TMP/prompt.txt"; do
  IFS='|' read -r -a runpi_args <<< "$bad_args"
  runpi_args_rc=0
  PATH="$RUNPI_TMP/bin:$PATH" EASYPI_TIMEOUT=5 \
    bash easypi/scripts/run_pi.sh "${runpi_args[@]}" >/dev/null 2>&1 </dev/null || runpi_args_rc=$?
  if [ "$runpi_args_rc" -ne 2 ]; then
    echo "Error: run_pi.sh 参数守卫未拒绝: ${runpi_args[*]}（exit=${runpi_args_rc}）" >&2
    runpi_ok=0
  fi
done
# 纯空白取值：单列在此，避免 IFS 切分把空白字段吃掉。
for blank_args in "--model" "--thinking"; do
  runpi_blank_rc=0
  PATH="$RUNPI_TMP/bin:$PATH" EASYPI_TIMEOUT=5 \
    bash easypi/scripts/run_pi.sh stub-session "$RUNPI_TMP/prompt.txt" "$blank_args" "   " \
    >/dev/null 2>&1 </dev/null || runpi_blank_rc=$?
  if [ "$runpi_blank_rc" -ne 2 ]; then
    echo "Error: run_pi.sh 未拒绝纯空白取值: $blank_args \"   \"（exit=${runpi_blank_rc}）" >&2
    runpi_ok=0
  fi
  runpi_blank_rc=0
  PATH="$RUNPI_TMP/bin:$PATH" EASYPI_TIMEOUT=5 \
    bash easypi/scripts/run_pi.sh stub-session "$RUNPI_TMP/prompt.txt" "$blank_args=   " \
    >/dev/null 2>&1 </dev/null || runpi_blank_rc=$?
  if [ "$runpi_blank_rc" -ne 2 ]; then
    echo "Error: run_pi.sh 未拒绝纯空白取值: $blank_args=   （exit=${runpi_blank_rc}）" >&2
    runpi_ok=0
  fi
done
runpi_cleanup
if [ "$runpi_ok" -ne 1 ]; then
  exit 1
fi
echo "OK: run_pi.sh 硬超时 124、响应 SIGTERM 的桩 3s 内 124、外部 SIGTERM 143、正常退出清子进程、参数守卫均返回 2（允许清单内参数按序透传）"
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
# ② 全局规则 AGENTS.md §2/§5/§6 与 easydev（engineering.md / SKILL.md）必须引用并同步。
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
    "easydev/references/workflow.md": extract(
        r"最多\s*([一二三四五六七八九十\d]+)\s*次有新证据的候选修复尝试",
        read("easydev/references/workflow.md"),
        "easydev/references/workflow.md「尝试预算」",
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

# 全局规则与 easydev 同步检查：同仓分发自用，easydev 必须显式声明与全局规则 AGENTS.md §5/§6 同步，且关键规则一致。
agents_text = read("AGENTS.md")
eng_text = read("easydev/references/engineering.md")
skill_text = read("easydev/SKILL.md")

if "与全局规则 `AGENTS.md` §5 保持同步" not in eng_text:
    errors.append("easydev/references/engineering.md 缺少对全局规则 AGENTS.md §5 的同步引用声明")
if "与全局规则 `AGENTS.md` §6 保持同步" not in eng_text:
    errors.append("easydev/references/engineering.md 缺少对全局规则 AGENTS.md §6 的同步引用声明")

sync_pairs = [
    ("默认写直线式代码", agents_text, eng_text, "AGENTS.md §5 <-> engineering.md"),
    ("仅当同一逻辑有 2 处以上真实复用时才提取", agents_text, eng_text, "AGENTS.md §5 <-> engineering.md"),
    ("不加未被要求的类、接口、配置项、参数、wrapper", agents_text, eng_text, "AGENTS.md §5 <-> engineering.md"),
    ("不写备份、回退、降级、兼容旧版本逻辑", agents_text, eng_text, "AGENTS.md §5 <-> engineering.md"),
    ("不对不可能发生的情况做防御性处理", agents_text, eng_text, "AGENTS.md §5 <-> engineering.md"),
    ("只写用户视角的端到端测试", agents_text, eng_text, "AGENTS.md §6 <-> engineering.md"),
    ("不写单元测试，不用 mock 隔离内部实现", agents_text, eng_text, "AGENTS.md §6 <-> engineering.md"),
    ("只覆盖核心路径和关键失败场景", agents_text, eng_text, "AGENTS.md §6 <-> engineering.md"),
    ("新增或修改测试前", agents_text, eng_text, "AGENTS.md §6 <-> engineering.md"),
    ("不新写 `unittest.TestCase`", agents_text, eng_text, "AGENTS.md §6 <-> engineering.md"),
    ("文档随改动校准", agents_text, eng_text, "AGENTS.md §5 <-> engineering.md"),
    ("纯文档与注释变更免跑代码检查", agents_text, eng_text, "AGENTS.md §1 <-> engineering.md"),
    ("不为确认基线预跑既有测试", agents_text, eng_text, "AGENTS.md §1 <-> engineering.md"),
    ("同一工作区上同套或范围重叠的测试/构建", agents_text, eng_text, "AGENTS.md §1 <-> engineering.md"),
    ("在跑或已通过未失效", agents_text, eng_text, "AGENTS.md §1 <-> engineering.md"),
    ("隔离工作树分片与性能对比多次采样不限", agents_text, eng_text, "AGENTS.md §1 <-> engineering.md"),
    ("关闭无关 FD", agents_text, eng_text, "AGENTS.md §2 <-> engineering.md"),
    ("</dev/null", agents_text, eng_text, "AGENTS.md §2 <-> engineering.md"),
    ("硬超时", agents_text, eng_text, "AGENTS.md §2 <-> engineering.md"),
]
for phrase, src, dst, label in sync_pairs:
    if phrase not in src or phrase not in dst:
        errors.append(f"全局规则与 easydev 同步短语缺失 [{label}]: {phrase!r}")

# 文档校准门禁：§5 口径由上面的同步短语覆盖，§1 的任务范围与 §8 的报告行只在 AGENTS.md，
# 逐一按原文短语核对，避免只留在 easydev 一侧或被人悄悄删掉。
doc_calibration_phrases = [
    "受本次改动影响的既有文档校准属任务范围",
    "文档校准状态（同步了哪些说明，或内部改动无需校准）",
]
missing_doc_calibration = [p for p in doc_calibration_phrases if p not in agents_text]
if missing_doc_calibration:
    errors.append(f"AGENTS.md 缺少文档校准收尾条款: {missing_doc_calibration}")

if "端到端测试" not in skill_text or "不写单测" not in skill_text:
    errors.append("easydev/SKILL.md quick 核心未同步端到端/不写单测约束")
for needle in (
    "不含测试结果",
    "改前不跑既有测试/构建摸底",
    "新写复现/红测与无 CI/旧记录的[纯重构](references/engineering.md#测试硬约束与选择)单次对照除外",
    "同一工作区上同套或范围重叠的测试/构建在跑或已通过未失效时不重跑（隔离工作树分片与性能对比多次采样不限）",
):
    if needle not in skill_text:
        errors.append(f"easydev/SKILL.md 缺少禁预跑/防重跑关键短语: {needle!r}")
if "纯重构无 CI/旧记录时单次定向对照除外" not in agents_text:
    errors.append("AGENTS.md §1 缺少纯重构单次定向对照例外")
for needle in ("新写复现/红测与无 CI/旧记录的纯重构单次对照除外", "或无上述时在任务基线（提交及已保护的相关未提交改动）"):
    if needle not in eng_text:
        errors.append(f"easydev/references/engineering.md 缺少关键短语: {needle!r}")
roles_text = read("easydev/references/roles.md")
if "改前不跑既有测试摸底；新写复现/红测与无 CI/旧记录的纯重构单次对照除外" not in roles_text:
    errors.append("easydev/references/roles.md 缺少改前不跑既有测试摸底及条件例外约束")

if not errors:
    print(f"OK: 全局规则 AGENTS.md §2/§5/§6 与 easydev 引用及 {len(sync_pairs)} 项核心口径同步")

if errors:
    for err in errors:
        print(f"Error: {err}", file=sys.stderr)
    sys.exit(1)
PY

echo "=== 10. 规则文档总量棘轮（默认只减不增，扩容须记入 CHANGELOG） ==="
# 规则正文做减法：入口与 references 合计不得越过基线，新增规则必须同时删掉等量篇幅。
# 范围只含 SKILL.md 与 references/*.md（运行时实际下发的规则正文）；维护材料与 README 不计。
# 改 MAX_TOTAL 必须同步 CHANGELOG 对应版本的数值；扩容需在 CHANGELOG 说明理由。
MAX_TOTAL=75839
TOTAL=$(cat easydev/SKILL.md easydev/references/*.md | tr -d '\r' | wc -c | tr -d ' ')
echo "规则文档总字节: $TOTAL (棘轮上限: $MAX_TOTAL)"
if [ "$TOTAL" -gt "$MAX_TOTAL" ]; then
  echo "Error: 规则文档总量超过棘轮 ($TOTAL > $MAX_TOTAL)；先删减同等篇幅再新增，或按 CHANGELOG 说明理由上调上限。" >&2
  exit 1
fi
# 基线双写只接受最新发布小节；其版本须与 SKILL.md 一致。
CURRENT_VERSION="$(python3 scripts/skill_version.py easydev/SKILL.md)"
if ! awk -v version="$CURRENT_VERSION" -v limit="$MAX_TOTAL" '
BEGIN {
  prefix = "## " version " 更新说明（"
  phrase = "合计不得超过 " limit " 字节"
}
{
  if (index($0, "\r")) { unsupported = 1; next }
  line = $0
  marker = substr(line, 1, 1)
  run_length = 0
  if (marker == "`" || marker == "~") {
    while (substr(line, run_length + 1, 1) == marker) run_length++
  }
  if (fence_char != "") {
    if (marker == fence_char && run_length >= fence_length &&
        substr(line, run_length + 1) ~ /^[[:space:]]*$/) {
      fence_char = ""
      fence_length = 0
    }
    next
  }
  if (line ~ /^[[:space:]]/) {
    candidate = line
    for (indent = 0; indent < 3 && substr(candidate, 1, 1) == " "; indent++) {
      candidate = substr(candidate, 2)
    }
    if (indent > 0 && candidate !~ /^[[:space:]]/ &&
        (candidate ~ /^```/ || candidate ~ /^~~~/ ||
         (match(candidate, /^#+/) && RLENGTH <= 6 &&
          (length(candidate) == RLENGTH || substr(candidate, RLENGTH + 1, 1) ~ /^[ \t]/)) ||
         candidate ~ /^=+[ \t]*$/ || candidate ~ /^-+[ \t]*$/ ||
         substr(candidate, 1, 1) == "<")) unsupported = 1
    next
  }
  if (line ~ /^=+[ \t]*$/ || line ~ /^-+[ \t]*$/) { unsupported = 1; next }
  if (substr(line, 1, 1) == "<") { unsupported = 1; next }
  if (run_length >= 3 && index(substr(line, run_length + 1), marker) == 0) {
    fence_char = marker
    fence_length = run_length
    next
  }
  if (line ~ /^##([ \t]|$)/) {
    if (seen++) { active = 0; next }
    active = (index(line, prefix) == 1)
    valid = active
  }
  if (active && index(line, phrase)) matched = 1
}
END { exit !(valid && matched && fence_char == "" && !unsupported) }
' easydev/CHANGELOG.md; then
  echo "Error: MAX_TOTAL=$MAX_TOTAL 在 CHANGELOG 当前版本 $CURRENT_VERSION 小节校验失败（额度不一致或格式不支持）；请使用列 0 的 ## 发布标题与额度声明，基线双写须同步" >&2
  exit 1
fi
# 脑图脚注是同一事实的第二写点（历史上漏同步过），按 MAX_TOTAL 机械核对。
if ! grep -q "规则总账 ≤ ${MAX_TOTAL} B" easydev/assets/easydev-mindmap.svg; then
  echo "Error: easydev/assets/easydev-mindmap.svg 的规则总账数值与 MAX_TOTAL=${MAX_TOTAL} 不一致" >&2
  exit 1
fi
echo "OK: 脑图规则总账数值与 MAX_TOTAL 一致"
if [ "$TOTAL" -lt "$MAX_TOTAL" ]; then
  echo "提示: 总量低于棘轮 $((MAX_TOTAL - TOTAL)) 字节，可把 MAX_TOTAL 向下重设为 ${TOTAL}（并同步 CHANGELOG）"
fi
echo "OK: 规则文档总量未超棘轮 ($TOTAL ≤ $MAX_TOTAL)"

echo "=== 11. EasyThink 行为规约一致性 ==="
# 只校验可机械判定的部分：编号连续、覆盖矩阵对齐、全量范围一致、标签有定义、
# README 判据与 SKILL 同源、回归记录版本与 SKILL 版本一致。
python3 scripts/check_think_rubric.py

echo "=== 12. Agent Hooks 校验与 CLI 测试 ==="
python3 -c 'import json; json.load(open("agent-hooks/antigravity/hooks.json", encoding="utf-8"))'
PYTEST_BIN="python3"
if [ -x ".venv/bin/python" ]; then
  PYTEST_BIN=".venv/bin/python"
fi
"$PYTEST_BIN" -m pytest agent-hooks/test_check_command.py </dev/null

echo "=== 全部自动化门禁检查通过 ==="
