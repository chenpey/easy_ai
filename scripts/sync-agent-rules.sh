#!/usr/bin/env bash
set -euo pipefail

# 把规则文件显式发布到用户级运行目录，并提供只读核对模式。
# 支持两种源模式：
# 1. 本地仓库：若本地存在 AGENTS.md 则直接使用本地文件（方便本地开发测试）。
# 2. 远端模式：若无本地仓库或管道执行，自动从 GitHub 远端拉取最新规则，无需 clone 代码。
# 职责边界：不安装技能、不提交、不提权、不监听变化。
# 代理沙箱通常不能写 ~/.config；完整发布需用户在自己的终端执行 --apply。

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
ROOT_DIR=""
if [ -n "$SCRIPT_SOURCE" ]; then
  ROOT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")/.." 2>/dev/null && pwd || true)"
fi
MODE="${1---check}"
RAW_BASE="${EASY_AI_RAW_BASE:-https://raw.githubusercontent.com/chenpey/easy_ai/main}"

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [--check|--apply]" >&2
  exit 2
fi
case "$MODE" in
  --check|--apply) ;;
  *) echo "Usage: $0 [--check|--apply]" >&2; exit 2 ;;
esac

targets=(
  "$HOME/.config/agent-rules/AGENTS.md"
  "$HOME/.gemini/config/rules/antigravity.md"
  "$HOME/.config/agent-hooks/check_command.py"
  "$HOME/.pi/agent/extensions/enforce-noninteractive.ts"
  "$HOME/.config/agent-hooks/models.py"
  "$HOME/.pi/agent/extensions/apply-model-windows.ts"
)
entries=(
  "$HOME/.pi/agent/AGENTS.md"
  "$HOME/.codex/AGENTS.md"
  "$HOME/.gemini/config/AGENTS.md"
)
rel_paths=(
  "AGENTS.md"
  "agent-rules/antigravity.md"
  "agent-hooks/check_command.py"
  "agent-hooks/pi/enforce-noninteractive.ts"
  "scripts/models.py"
  "agent-hooks/pi/apply-model-windows.ts"
)
HOOKS_REL_PATH="agent-hooks/antigravity/hooks.json"
HOOKS_TARGET="$HOME/.gemini/config/hooks.json"

apply_tmp=''
hooks_tmp=''
remote_tmp_dir=''
cleanup() {
  if [ -n "$apply_tmp" ] && [ -f "$apply_tmp" ]; then
    rm -f -- "$apply_tmp"
  fi
  if [ -n "$hooks_tmp" ] && [ -f "$hooks_tmp" ]; then
    rm -f -- "$hooks_tmp"
  fi
  if [ -n "$remote_tmp_dir" ] && [ -d "$remote_tmp_dir" ]; then
    rm -rf -- "$remote_tmp_dir"
  fi
}
trap cleanup EXIT

download_file() {
  local url="$1"
  local dest="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --max-time 30 "$url" -o "$dest"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$dest" -T 30 "$url"
  else
    echo "Error: 需要 curl 或 wget 从远端下载规则" >&2
    exit 1
  fi
}

use_local=true
if [ -z "$SCRIPT_SOURCE" ] || [ -z "$ROOT_DIR" ] || [ ! -f "$ROOT_DIR/$HOOKS_REL_PATH" ]; then
  use_local=false
else
  for rel in "${rel_paths[@]}"; do
    if [ ! -f "$ROOT_DIR/$rel" ]; then
      use_local=false
      break
    fi
  done
fi

sources=()
HOOKS_SOURCE=""
if [ "$use_local" = true ]; then
  for rel in "${rel_paths[@]}"; do
    sources+=("$ROOT_DIR/$rel")
  done
  HOOKS_SOURCE="$ROOT_DIR/$HOOKS_REL_PATH"
else
  remote_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/easy_ai_rules.XXXXXX")"
  for rel in "${rel_paths[@]}"; do
    src="$remote_tmp_dir/$rel"
    mkdir -p "$(dirname "$src")"
    download_file "$RAW_BASE/$rel" "$src"
    sources+=("$src")
  done
  HOOKS_SOURCE="$remote_tmp_dir/$HOOKS_REL_PATH"
  mkdir -p "$(dirname "$HOOKS_SOURCE")"
  download_file "$RAW_BASE/$HOOKS_REL_PATH" "$HOOKS_SOURCE"
fi

hash_sha256() {
  # Linux 常见 sha256sum（coreutils），macOS 常见 shasum；两者输出均为「hash  path」。
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$@"
  else
    echo "Error: 需要 sha256sum 或 shasum 之一来输出 SHA-256" >&2
    return 1
  fi
}

# 先完成全部只读校验再逐个原子替换目标文件（中途失败可重新执行 --apply 收敛，不做整批自动回滚）。
for i in "${!targets[@]}"; do
  test -f "${sources[$i]}"
  if [ -d "${targets[$i]}" ]; then
    echo "Target is a directory: ${targets[$i]}" >&2
    exit 1
  fi
done

# 运行时规则发布文件必须是非软链的真文件；index 1（AGY 专有规则）由下面的过渡分支单独处理。
for i in "${!targets[@]}"; do
  if [ "$i" -eq 1 ]; then
    continue
  fi
  if [ -L "${targets[$i]}" ]; then
    echo "Runtime rule target must be a regular file: ${targets[$i]}" >&2
    exit 1
  fi
done

# 允许 AGY 专有规则当前仍是"指向仓库"的软链（仅本地仓库模式过渡），迁移后应是真文件。
if [ -L "${targets[1]}" ]; then
  if [ "$use_local" = true ] && [ "$(readlink "${targets[1]}")" = "${sources[1]}" ]; then
    :
  else
    echo "Unexpected AGY symlink target" >&2
    exit 1
  fi
fi

if [ "$MODE" = "--apply" ]; then
  # 先把合并结果写进临时文件并校验 hooks.json，成功后才动规则文件与入口软链。
  hooks_dir="$(dirname "$HOOKS_TARGET")"
  mkdir -p "$hooks_dir"
  hooks_tmp="$(mktemp "$hooks_dir/.agent-hooks.XXXXXX")"
  python3 - "$HOOKS_SOURCE" "$HOOKS_TARGET" "$hooks_tmp" <<'PY'
import json, os, sys

src_path, dst_path, tmp_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src_path, "r", encoding="utf-8") as f:
    src_data = json.load(f)

dst_data = {}
if os.path.lexists(dst_path):
    if os.path.islink(dst_path) or not os.path.isfile(dst_path):
        print(f"Hooks target must be a regular file: {dst_path}", file=sys.stderr)
        sys.exit(1)
    with open(dst_path, "r", encoding="utf-8") as f:
        try:
            dst_data = json.load(f)
        except json.JSONDecodeError as error:
            print(f"Hooks target is not valid JSON: {dst_path}: {error}", file=sys.stderr)
            sys.exit(1)
    if not isinstance(dst_data, dict):
        print(f"Hooks target must be a JSON object: {dst_path}", file=sys.stderr)
        sys.exit(1)

for key, val in src_data.items():
    dst_data[key] = val

with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(dst_data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
  chmod 644 "$hooks_tmp"

  # 入口预检只读：任何冲突都在创建软链之前退出，不留悬空链接。
  for entry in "${entries[@]}"; do
    if [ -L "$entry" ]; then
      if [ "$(readlink "$entry")" != "${targets[0]}" ]; then
        echo "已有非预期软链入口: $entry -> $(readlink "$entry")" >&2
        exit 1
      fi
    elif [ -e "$entry" ]; then
      echo "已有非预期入口（非符号链接），请先核对并备份: $entry" >&2
      exit 1
    fi
  done

  for entry in "${entries[@]}"; do
    mkdir -p "$(dirname "$entry")"
    [ -L "$entry" ] || ln -s "${targets[0]}" "$entry"
  done

  for i in "${!targets[@]}"; do
    target_dir="$(dirname "${targets[$i]}")"
    mkdir -p "$target_dir"
    apply_tmp="$(mktemp "$target_dir/.agent-rule.XXXXXX")"
    cp "${sources[$i]}" "$apply_tmp"
    diff -q "${sources[$i]}" "$apply_tmp"
    if [[ "${targets[$i]}" == *.py ]]; then
      chmod 755 "$apply_tmp"
    else
      chmod 644 "$apply_tmp"
    fi
    mv -f "$apply_tmp" "${targets[$i]}"
    apply_tmp=''
  done

  # hooks.json 的临时文件在所有规则文件就位后才替换，失败时保留旧配置。
  mv -f "$hooks_tmp" "$HOOKS_TARGET"
  hooks_tmp=''
else
  for entry in "${entries[@]}"; do
    if [ ! -L "$entry" ] || [ "$(readlink "$entry")" != "${targets[0]}" ]; then
      echo "Unexpected rule entry: $entry" >&2
      exit 1
    fi
  done
fi

for i in "${!targets[@]}"; do
  if [ -L "${targets[$i]}" ]; then
    echo "Runtime rule must be a regular file: ${targets[$i]}" >&2
    exit 1
  fi
  if [ ! -f "${targets[$i]}" ]; then
    echo "Missing or non-regular target file: ${targets[$i]}" >&2
    exit 1
  fi
  # diff 的缺文件退出码是 2，与本脚本的用法错误码相同；统一归一化为 1。
  diff -q "${sources[$i]}" "${targets[$i]}" || exit 1
done

if [ -L "$HOOKS_TARGET" ] || [ ! -f "$HOOKS_TARGET" ]; then
  echo "Missing or non-regular hooks target: $HOOKS_TARGET" >&2
  exit 1
fi
python3 - "$HOOKS_SOURCE" "$HOOKS_TARGET" <<'PY'
import json, sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    src_data = json.load(f)
with open(sys.argv[2], "r", encoding="utf-8") as f:
    dst_data = json.load(f)
if not isinstance(dst_data, dict):
    print(f"Hooks target must be a JSON object: {sys.argv[2]}", file=sys.stderr)
    sys.exit(1)
for key, val in src_data.items():
    if dst_data.get(key) != val:
        print(f"Hooks key mismatch ({key}) in {sys.argv[2]}", file=sys.stderr)
        sys.exit(1)
PY

hash_sha256 "${sources[@]}" "$HOOKS_SOURCE" "${targets[@]}" "$HOOKS_TARGET"
echo "Rule files match; harness loading has not been verified."
