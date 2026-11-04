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
)
entries=(
  "$HOME/.pi/agent/AGENTS.md"
  "$HOME/.codex/AGENTS.md"
  "$HOME/.gemini/config/AGENTS.md"
)
rel_paths=(
  "AGENTS.md"
  "agent-rules/antigravity.md"
)

apply_tmp=''
remote_tmp_dir=''
cleanup() {
  if [ -n "$apply_tmp" ] && [ -f "$apply_tmp" ]; then
    rm -f -- "$apply_tmp"
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
    curl -fsSL "$url" -o "$dest"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$dest" "$url"
  else
    echo "Error: 需要 curl 或 wget 从远端下载规则" >&2
    exit 1
  fi
}

use_local=true
if [ -z "$SCRIPT_SOURCE" ] || [ -z "$ROOT_DIR" ] || [ ! -f "$ROOT_DIR/${rel_paths[0]}" ] || [ ! -f "$ROOT_DIR/${rel_paths[1]}" ]; then
  use_local=false
fi

sources=()
if [ "$use_local" = true ]; then
  sources=(
    "$ROOT_DIR/${rel_paths[0]}"
    "$ROOT_DIR/${rel_paths[1]}"
  )
else
  remote_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/easy_ai_rules.XXXXXX")"
  sources=(
    "$remote_tmp_dir/AGENTS.md"
    "$remote_tmp_dir/antigravity.md"
  )
  for i in 0 1; do
    url="$RAW_BASE/${rel_paths[$i]}"
    download_file "$url" "${sources[$i]}"
  done
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

# 拓扑与软链入口处理
if [ "$MODE" = "--apply" ]; then
  for entry in "${entries[@]}"; do
    entry_dir="$(dirname "$entry")"
    mkdir -p "$entry_dir"
    if [ -L "$entry" ]; then
      if [ "$(readlink "$entry")" != "${targets[0]}" ]; then
        echo "已有非预期软链入口: $entry -> $(readlink "$entry")" >&2
        exit 1
      fi
    elif [ -e "$entry" ]; then
      echo "已有非预期入口（非符号链接），请先核对并备份: $entry" >&2
      exit 1
    else
      ln -s "${targets[0]}" "$entry"
    fi
  done
else
  for entry in "${entries[@]}"; do
    if [ ! -L "$entry" ] || [ "$(readlink "$entry")" != "${targets[0]}" ]; then
      echo "Unexpected rule entry: $entry" >&2
      exit 1
    fi
  done
fi

for i in 0 1; do
  test -f "${sources[$i]}"
  if [ -d "${targets[$i]}" ]; then
    echo "Target is a directory: ${targets[$i]}" >&2
    exit 1
  fi
done

# 通用规则发布文件必须是非软链的真文件。
if [ -L "${targets[0]}" ]; then
  echo "Shared rule target must be a regular file" >&2
  exit 1
fi

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
  for i in 0 1; do
    target_dir="$(dirname "${targets[$i]}")"
    mkdir -p "$target_dir"
    apply_tmp="$(mktemp "$target_dir/.agent-rule.XXXXXX")"
    cp "${sources[$i]}" "$apply_tmp"
    diff -q "${sources[$i]}" "$apply_tmp"
    chmod 644 "$apply_tmp"
    mv -f "$apply_tmp" "${targets[$i]}"
    apply_tmp=''
  done
fi

for i in 0 1; do
  if [ -L "${targets[$i]}" ]; then
    echo "Runtime rule must be a regular file: ${targets[$i]}" >&2
    exit 1
  fi
  diff -q "${sources[$i]}" "${targets[$i]}"
done

hash_sha256 "${sources[@]}" "${targets[@]}"
echo "Rule files match; harness loading has not been verified."
