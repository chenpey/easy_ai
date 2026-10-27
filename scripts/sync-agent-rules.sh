#!/usr/bin/env bash
set -euo pipefail

# 把仓库内的规则文件显式发布到用户级运行目录，并提供只读核对模式。
# 职责边界：不安装技能、不创建或修复 harness 链接、不提交、不提权、不监听变化。
# 代理沙箱通常不能写 ~/.config；完整发布需用户在自己的终端执行 --apply。

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1---check}"

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [--check|--apply]" >&2
  exit 2
fi
case "$MODE" in
  --check|--apply) ;;
  *) echo "Usage: $0 [--check|--apply]" >&2; exit 2 ;;
esac

sources=(
  "$ROOT_DIR/AGENTS.md"
  "$ROOT_DIR/agent-rules/antigravity.md"
)
targets=(
  "$HOME/.config/agent-rules/AGENTS.md"
  "$HOME/.gemini/config/rules/antigravity.md"
)
entries=(
  "$HOME/.pi/agent/AGENTS.md"
  "$HOME/.codex/AGENTS.md"
  "$HOME/.gemini/config/AGENTS.md"
)

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

# 先核对现有拓扑；本脚本不负责重建入口。
for entry in "${entries[@]}"; do
  if [ ! -L "$entry" ] || [ "$(readlink "$entry")" != "${targets[0]}" ]; then
    echo "Unexpected rule entry: $entry" >&2
    exit 1
  fi
done

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

# 允许 AGY 专有规则当前仍是"指向仓库"的软链，迁移后应是真文件。
if [ -L "${targets[1]}" ] && [ "$(readlink "${targets[1]}")" != "${sources[1]}" ]; then
  echo "Unexpected AGY symlink target" >&2
  exit 1
fi

tmp=''
trap 'if [ -n "$tmp" ]; then rm -f -- "$tmp"; fi' EXIT

if [ "$MODE" = "--apply" ]; then
  for i in 0 1; do
    target_dir="$(dirname "${targets[$i]}")"
    mkdir -p "$target_dir"
    tmp="$(mktemp "$target_dir/.agent-rule.XXXXXX")"
    cp "${sources[$i]}" "$tmp"
    diff -q "${sources[$i]}" "$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "${targets[$i]}"
    tmp=''
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
