#!/usr/bin/env bash
# 安装契约冒烟测试：按 README 远程安装提示词规范校验运行时文件集落盘契约，
# 覆盖运行时文件集完整性、维护文件隔离边界、版本一致性、备份隔离与两技能共存。
# 用法：bash scripts/smoke-install.sh
#   SMOKE_TMP=<一次性目录>  指定测试用 HOME；脚本不会删除调用方指定的目录，需自行清理。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -n "${SMOKE_TMP:-}" ]; then
  TMP_HOME="$SMOKE_TMP"
  mkdir -p "$TMP_HOME"
  cleanup() { :; }   # 调用方指定的目录不擅自删除
else
  TMP_HOME="$(mktemp -d "${TMPDIR:-/tmp}/easy_ai_smoke.XXXXXX")"
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
  local leftovers=() list
  list="$(mktemp "${TMPDIR:-/tmp}/smoke-residue.XXXXXX")"
  # find 的退出状态不能在进程替换里取到；先落文件再判断，遍历失败不能当成“无残留”。
  if ! find "$TMP_HOME" -name '.stage.*' -o -name '.stage_*' >"$list" 2>/dev/null; then
    rm -f "$list"
    fail "暂存残留检查失败：无法完整遍历 $TMP_HOME"
  fi
  while IFS= read -r path; do
    leftovers+=("$path")
  done <"$list"
  rm -f "$list"
  [ "${#leftovers[@]}" -eq 0 ] || fail "存在暂存目录残留：${leftovers[*]}"
  pass "无暂存目录残留"
}

# 辅助函数：模拟 Agent 按 README 提示词落盘运行时文件集
install_runtime_fileset() {
  local skill_name="$1"
  local src_dir="$ROOT_DIR/$skill_name"
  local dest_dir="$TMP_HOME/.agents/skills/$skill_name"
  mkdir -p "$dest_dir"
  chmod 755 "$dest_dir"
  cp "$src_dir/SKILL.md" "$dest_dir/SKILL.md"
  cp -R "$src_dir/references" "$dest_dir/references"
  cp -R "$src_dir/agents" "$dest_dir/agents"
}

echo "=== 场景 1：easythink 运行时文件集安装契约（SKILL.md + references/ + agents/） ==="
THINK_SRC="$ROOT_DIR/easythink"
THINK_DEST="$TMP_HOME/.agents/skills/easythink"
install_runtime_fileset "easythink"

[ -f "$THINK_DEST/SKILL.md" ] || fail "easythink 未安装 SKILL.md"
[ -d "$THINK_DEST/references" ] || fail "easythink 未安装 references/"
[ -d "$THINK_DEST/agents" ] || fail "easythink 未安装 agents/"
[ ! -e "$THINK_DEST/tests" ] && [ ! -e "$THINK_DEST/CHANGELOG.md" ] && [ ! -e "$THINK_DEST/README.md" ] || fail "easythink 安装了运行时不需要的维护文件"

THINK_SRC_REFS=$(ls "$THINK_SRC/references"/*.md | wc -l | tr -d ' ')
THINK_DEST_REFS=$(ls "$THINK_DEST/references"/*.md | wc -l | tr -d ' ')
[ "$THINK_SRC_REFS" = "$THINK_DEST_REFS" ] || fail "easythink references 数量不一致：$THINK_DEST_REFS != $THINK_SRC_REFS"

grep -q '^name: easythink$' "$THINK_DEST/SKILL.md" || fail "easythink SKILL.md 的 frontmatter name 不是 easythink"
THINK_VERSION="$(python3 "$ROOT_DIR/scripts/skill_version.py" "$THINK_DEST/SKILL.md")" || fail "无法从 easythink SKILL.md 提取 metadata.version"
grep -q "版本：\`$THINK_VERSION\`" "$THINK_SRC/README.md" || fail "easythink SKILL.md 版本 $THINK_VERSION 与 README 不一致"

assert_perm_755 "$THINK_DEST"
assert_no_stage_residue
pass "easythink 全新安装契约与隔离边界通过"

echo "=== 场景 2：更新时备份位于 skills 目录之外 ==="
mkdir -p "$TMP_HOME/.agents/backups"
BAK_DEST="$TMP_HOME/.agents/backups/easythink.$(date +%s)"
cp -R "$THINK_DEST" "$BAK_DEST"
case "$BAK_DEST" in
  "$TMP_HOME/.agents/skills/"*) fail "备份位于 skills 目录内：$BAK_DEST" ;;
esac
[ -f "$BAK_DEST/SKILL.md" ] || fail "备份不完整"
pass "备份位于 skills 目录之外：${BAK_DEST#"$TMP_HOME"/}"

echo "=== 场景 3：专用目录软链接映射与有效性 ==="
for parent in "$TMP_HOME/.claude/skills" "$TMP_HOME/.gemini/config/skills" "$TMP_HOME/.gemini/antigravity-cli/skills"; do
  mkdir -p "$parent"
  dest="$parent/easythink"
  ln -s "$THINK_DEST" "$dest"
  [ -L "$dest" ] || fail "软链接创建失败: $dest"
  [ -f "$dest/SKILL.md" ] || fail "软链接目标无效: $dest/SKILL.md"
  pass "宿主专用目录软链接有效: ${dest#"$TMP_HOME"/}"
done

echo "=== 场景 4：easydev 运行时文件集安装契约 ==="
DEV_SRC="$ROOT_DIR/easydev"
DEV_DEST="$TMP_HOME/.agents/skills/easydev"
install_runtime_fileset "easydev"

[ -f "$DEV_DEST/SKILL.md" ] || fail "easydev 未安装 SKILL.md"
[ -d "$DEV_DEST/references" ] || fail "easydev 未安装 references/"
[ -d "$DEV_DEST/agents" ] || fail "easydev 未安装 agents/"
[ ! -e "$DEV_DEST/tests" ] && [ ! -e "$DEV_DEST/CHANGELOG.md" ] && [ ! -e "$DEV_DEST/README.md" ] || fail "easydev 安装了运行时不需要的文件"

DEV_SRC_REFS=$(ls "$DEV_SRC/references"/*.md | wc -l | tr -d ' ')
DEV_DEST_REFS=$(ls "$DEV_DEST/references"/*.md | wc -l | tr -d ' ')
[ "$DEV_SRC_REFS" = "$DEV_DEST_REFS" ] || fail "easydev references 数量不一致：$DEV_DEST_REFS != $DEV_SRC_REFS"

grep -q '^name: easydev$' "$DEV_DEST/SKILL.md" || fail "easydev SKILL.md 的 frontmatter name 不是 easydev"
DEV_VERSION="$(python3 "$ROOT_DIR/scripts/skill_version.py" "$DEV_DEST/SKILL.md")" || fail "无法从 easydev SKILL.md 提取 metadata.version"
grep -q "当前版本：\`$DEV_VERSION\`" "$DEV_SRC/README.md" || fail "easydev SKILL.md 版本 $DEV_VERSION 与 README 不一致"

assert_perm_755 "$DEV_DEST"
pass "easydev 运行时文件集完整"

echo "=== 场景 5：两技能在 skills 根目录下共存且无多余残留 ==="
ENTRIES=$(ls -A "$TMP_HOME/.agents/skills" | sort | tr '\n' ' ')
[ "$ENTRIES" = "easydev easythink " ] || fail "skills 根目录内容异常：$ENTRIES"
assert_no_stage_residue
pass "skills 根目录两技能共存且无多余残留"

echo "=== 场景 6：README 远程安装提示词完整性检查 ==="
for skill in easythink easydev; do
  readme="$ROOT_DIR/$skill/README.md"
  grep -q "请安装或更新 Easy" "$readme" || fail "$readme 缺少远程安装提示词"
  grep -q "https://github.com/chenpey/easy_ai/tree/main/$skill" "$readme" || fail "$readme 缺少远程仓库 URL"
  grep -q "~/.agents/skills/$skill" "$readme" || fail "$readme 缺少标准安装路径"
  pass "$skill README 远程安装提示词契约完整"
done

echo "=== 安装契约冒烟测试全部通过 ==="
