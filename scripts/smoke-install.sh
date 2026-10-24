#!/usr/bin/env bash
# 安装脚本冒烟测试：在临时 HOME 中实际执行 README 里发布的安装命令，
# 覆盖全新安装、重装备份、克隆失败回滚、软链技能目录与两种专用目录同步。
# 用法：bash scripts/smoke-install.sh
#   SMOKE_TMP=<一次性目录>  指定测试用 HOME；脚本不会删除调用方指定的目录，需自行清理。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
README="$ROOT_DIR/easythinking/README.md"

if [ -n "${SMOKE_TMP:-}" ]; then
  TMP_HOME="$SMOKE_TMP"
  mkdir -p "$TMP_HOME"
  cleanup() { :; }   # 调用方指定的目录不擅自删除
else
  TMP_HOME="$(mktemp -d)"
  cleanup() { rm -rf "$TMP_HOME"; }
fi
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "  OK: $*"
}

# 从 README 提取 bash 代码块，并把 GitHub 克隆地址替换为本地快照地址，
# 保证测试的是文档里逐步发布的命令，而不是另一套重写的安装逻辑。
SNAPSHOT="$TMP_HOME/src"
mkdir -p "$SNAPSHOT"
tar -c --exclude=.git --exclude=.smoke-tmp --exclude=.mut -C "$ROOT_DIR" . | tar -x -C "$SNAPSHOT"
git -c user.name=smoke -c user.email=smoke@example.com -C "$SNAPSHOT" init --quiet
git -c user.name=smoke -c user.email=smoke@example.com -C "$SNAPSHOT" add -A
git -c user.name=smoke -c user.email=smoke@example.com -C "$SNAPSHOT" commit --quiet -m "smoke snapshot"
REPO_URL="file://$SNAPSHOT"
BROKEN_URL="file://$TMP_HOME/does-not-exist/easy_ai"

BLOCK_DIR="$TMP_HOME/blocks"
mkdir -p "$BLOCK_DIR"
python3 - "$README" "$BLOCK_DIR" "$REPO_URL" "$BROKEN_URL" <<'PY'
import re
import sys
from pathlib import Path

readme, outdir, repo_url, broken_url = sys.argv[1:5]
content = Path(readme).read_text(encoding="utf-8")
blocks = re.findall(r"^[ \t]*```bash\r?\n(.*?)\r?\n[ \t]*```", content, re.DOTALL | re.MULTILINE)
if len(blocks) != 3:
    sys.exit(f"README bash 代码块数量异常：{len(blocks)}，预期 3（通用目录 / Claude Code / Antigravity）")

github_url = "https://github.com/chenpey/easy_ai.git"
out = Path(outdir)
for i, block in enumerate(blocks, 1):
    text = block.replace(github_url, repo_url)
    if i == 1:
        (out / "block1_broken.sh").write_text(block.replace(github_url, broken_url), encoding="utf-8")
    (out / f"block{i}.sh").write_text(text, encoding="utf-8")
PY

# 测试用 mktemp 垫片：受限环境里 `mktemp -d` 会落到不可写的宿主临时目录，
# 这里把临时目录固定在测试 HOME 内；README 中的命令本身保持不变。
mkdir -p "$TMP_HOME/bin"
cat > "$TMP_HOME/bin/mktemp" <<'SHIM'
#!/usr/bin/env bash
root="${SMOKE_TMPDIR_ROOT:?SMOKE_TMPDIR_ROOT 未设置}"
mkdir -p "$root"
if [ "${1:-}" = "-d" ] && [ -z "${2:-}" ]; then
  exec /usr/bin/mktemp -d "$root/tmp.XXXXXXXX"
fi
exec /usr/bin/mktemp "$@"
SHIM
chmod 755 "$TMP_HOME/bin/mktemp"

run_block() {
  local block="$1"
  PATH="$TMP_HOME/bin:$PATH" HOME="$TMP_HOME" SMOKE_TMPDIR_ROOT="$TMP_HOME/tmpdir" bash "$BLOCK_DIR/$block"
}

perm_of() {
  python3 - "$1" <<'PY'
import os
import stat
import sys
print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])
PY
}

assert_perm_755() {
  local target="$1"
  [ -e "$target" ] || fail "$target 不存在"
  [ "$(perm_of "$target")" = "755" ] || fail "$target 权限为 $(perm_of "$target")，预期 755"
  pass "$target 权限 755"
}

assert_no_stage_residue() {
  local leftovers=()
  while IFS= read -r path; do
    leftovers+=("$path")
  done < <(find "$TMP_HOME" -name '.stage.*' -o -name '.stage_*')
  [ "${#leftovers[@]}" -eq 0 ] || fail "存在暂存目录残留：${leftovers[*]}"
  pass "无暂存目录残留"
}

assert_skills_root_clean() {
  local entries
  entries="$(ls -A "$TMP_HOME/.agents/skills")"
  [ "$entries" = "easythinking" ] || fail "skills 根目录内容异常：$entries"
  pass "skills 根目录只有 easythinking（easydev 场景尚未执行）"
}

assert_backup_outside_skills() {
  local backups=("$TMP_HOME/.agents/backups/"*)
  [ -e "${backups[0]}" ] || fail "未在 ~/.agents/backups 下找到备份"
  local backup="${backups[0]}"
  case "$backup" in
    "$TMP_HOME/.agents/skills/"*) fail "备份位于 skills 目录内：$backup" ;;
  esac
  pass "备份位于 skills 目录之外：${backup#"$TMP_HOME"/}"
}

file_digest() {
  python3 - "$1" <<'PY'
import hashlib
import sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PY
}

echo "=== 场景 1：全新安装（通用目录） ==="
run_block block1.sh >/dev/null
DEST="$TMP_HOME/.agents/skills/easythinking"
[ -f "$DEST/SKILL.md" ] || fail "未安装 SKILL.md"
[ -d "$DEST/references" ] || fail "未安装 references/"
[ -d "$DEST/agents" ] || fail "未安装 agents/"
[ ! -e "$DEST/tests" ] && [ ! -e "$DEST/CHANGELOG.md" ] || fail "安装了运行时不需要的文件"
assert_perm_755 "$DEST"
assert_no_stage_residue
assert_skills_root_clean
pass "全新安装内容完整"

echo "=== 场景 2：重装（备份旧版，旧版内容保留在备份中） ==="
echo "local-marker" >> "$DEST/SKILL.md"
run_block block1.sh >/dev/null
assert_backup_outside_skills
BACKUP=("$TMP_HOME/.agents/backups/"*)
BACKUP="${BACKUP[0]}"
grep -q "local-marker" "$BACKUP/SKILL.md" || fail "备份未保留旧版内容"
pass "备份保留了旧版内容"
assert_perm_755 "$DEST"
assert_no_stage_residue
assert_skills_root_clean

echo "=== 场景 3：克隆失败（非零退出且保留已装版本） ==="
BEFORE="$(file_digest "$DEST/SKILL.md")"
if run_block block1_broken.sh >/dev/null 2>&1; then
  fail "克隆失败时安装脚本仍然返回 0"
fi
pass "克隆失败返回非零退出码"
[ "$(file_digest "$DEST/SKILL.md")" = "$BEFORE" ] || fail "克隆失败后已装版本被改动"
pass "已装版本保持不变"
assert_no_stage_residue
assert_skills_root_clean

echo "=== 场景 4：Claude Code 专用目录同步 ==="
run_block block2.sh >/dev/null
CLAUDE_DEST="$TMP_HOME/.claude/skills/easythinking"
[ -f "$CLAUDE_DEST/SKILL.md" ] || fail "Claude Code 目录未同步"
assert_perm_755 "$CLAUDE_DEST"
assert_no_stage_residue
pass "Claude Code 目录同步通过"

echo "=== 场景 5：Antigravity 两个用户级目录同步 ==="
run_block block3.sh >/dev/null
for parent in "$TMP_HOME/.gemini/config/skills" "$TMP_HOME/.gemini/antigravity-cli/skills"; do
  [ -f "$parent/easythinking/SKILL.md" ] || fail "Antigravity 目录未同步：$parent"
  assert_perm_755 "$parent/easythinking"
done
assert_no_stage_residue
pass "Antigravity 目录同步通过"

echo "=== 场景 6：skills 目录为软链接时可安装并替换为实体目录 ==="
mv "$DEST" "$TMP_HOME/.agents/skills-src-easythinking"
ln -s "$TMP_HOME/.agents/skills-src-easythinking" "$DEST"
run_block block1.sh >/dev/null
[ ! -L "$DEST" ] || fail "软链接未被实体安装替换"
[ -f "$DEST/SKILL.md" ] || fail "替换软链接后缺少 SKILL.md"
assert_perm_755 "$DEST"
assert_no_stage_residue
assert_skills_root_clean
pass "软链接安装场景通过"

echo "=== 场景 7：easydev 运行时文件集（README 提示词：SKILL.md + references/ + agents/） ==="
# easydev 的安装说明是给 Agent 的提示词，没有可直接执行的 bash 块；
# 这里按其声明的文件集落盘，校验相对位置与"不装维护文件"的边界。
DEV_SRC="$ROOT_DIR/easydev"
DEV_DEST="$TMP_HOME/.agents/skills/easydev"
mkdir -p "$DEV_DEST"
cp "$DEV_SRC/SKILL.md" "$DEV_DEST/SKILL.md"
cp -R "$DEV_SRC/references" "$DEV_DEST/references"
cp -R "$DEV_SRC/agents" "$DEV_DEST/agents"
[ -f "$DEV_DEST/SKILL.md" ] || fail "easydev 未安装 SKILL.md"
[ -d "$DEV_DEST/references" ] || fail "easydev 未安装 references/"
[ -d "$DEV_DEST/agents" ] || fail "easydev 未安装 agents/"
[ ! -e "$DEV_DEST/tests" ] && [ ! -e "$DEV_DEST/CHANGELOG.md" ] && [ ! -e "$DEV_DEST/README.md" ] || fail "easydev 安装了运行时不需要的文件"
DEV_SRC_REFS=$(ls "$DEV_SRC/references"/*.md | wc -l | tr -d ' ')
DEV_DEST_REFS=$(ls "$DEV_DEST/references"/*.md | wc -l | tr -d ' ')
[ "$DEV_SRC_REFS" = "$DEV_DEST_REFS" ] || fail "easydev references 数量不一致：$DEV_DEST_REFS != $DEV_SRC_REFS"
grep -q '^name: easydev$' "$DEV_DEST/SKILL.md" || fail "easydev SKILL.md 的 frontmatter name 不是 easydev"
DEV_VERSION=$(grep -m1 '^  version:' "$DEV_DEST/SKILL.md" | sed 's/.*"\(.*\)".*/\1/')
grep -q "当前版本：\`$DEV_VERSION\`" "$DEV_SRC/README.md" || fail "easydev SKILL.md 版本 $DEV_VERSION 与 README 不一致"
assert_perm_755 "$DEV_DEST"
DEV_ENTRIES=$(ls -A "$TMP_HOME/.agents/skills" | sort | tr '\n' ' ')
[ "$DEV_ENTRIES" = "easydev easythinking " ] || fail "skills 根目录内容异常：$DEV_ENTRIES"
assert_no_stage_residue
pass "easydev 文件集完整，skills 根目录同时容纳两个技能"

echo "=== 安装脚本冒烟测试全部通过（PowerShell 版未覆盖） ==="
