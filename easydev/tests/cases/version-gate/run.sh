#!/usr/bin/env bash
# 版本提取门禁用例：scripts/skill_version.py 是 check.sh 与 smoke-install.sh
# 共用的唯一提取实现。合法 frontmatter 通过；重复 version 键、错位到其他映射的
# version、只在 description 块标量里出现的 version:、带引号或双引号转义后的重复
# version 键、值内嵌套引号与未闭合引号都必须被拒绝，不得当成 metadata.version。
# 两个分支都要覆盖：有 PyYAML 时用拒绝重复键的 loader，无 PyYAML 时用严格行匹配。
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
EXTRACTOR="$ROOT_DIR/scripts/skill_version.py"
# 显式模板：macOS 裸 `mktemp -d` 不使用 TMPDIR，受限沙箱里会被拒绝。
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/version-gate.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

FIXTURES="$TMP_DIR/fixtures"
mkdir -p "$FIXTURES" "$TMP_DIR/no-yaml"
# 用 PYTHONPATH 上的同名模块让 `import yaml` 抛 ImportError，从而固定走降级分支。
printf 'raise ImportError("PyYAML disabled by the version gate fixture")\n' \
  > "$TMP_DIR/no-yaml/yaml.py"

cat > "$FIXTURES/valid.md" <<'YAML'
---
name: easydev
description: >-
  统一处理编码需求、功能实现与代码审查。
metadata:
  version: "0.14.5"
---
YAML

cat > "$FIXTURES/duplicate-version.md" <<'YAML'
---
name: easydev
description: >-
  统一处理编码需求、功能实现与代码审查。
metadata:
  version: "0.14.5"
  version: "0.14.4"
---
YAML

cat > "$FIXTURES/misplaced-version.md" <<'YAML'
---
name: easydev
description: >-
  统一处理编码需求、功能实现与代码审查。
metadata:
  category: skill
other:
  version: "9.9.9"
---
YAML

cat > "$FIXTURES/description-version.md" <<'YAML'
---
name: easydev
description: |-
  metadata 写法示例：
  version: 9.9.9
metadata:
  version: "0.14.5"
---
YAML

cat > "$FIXTURES/description-only-version.md" <<'YAML'
---
name: easydev
description: |-
  metadata 写法示例：
  version: 9.9.9
metadata:
  category: skill
---
YAML

cat > "$FIXTURES/quoted-duplicate-version.md" <<'YAML'
---
name: easydev
description: >-
  统一处理编码需求、功能实现与代码审查。
metadata:
  "version": "0.14.5"
  version: "0.14.4"
---
YAML

cat > "$FIXTURES/escaped-duplicate-version.md" <<'YAML'
---
name: easydev
description: >-
  统一处理编码需求、功能实现与代码审查。
metadata:
  version: "0.14.5"
  "vers\u0069on": "0.14.4"
---
YAML

cat > "$FIXTURES/nested-quotes.md" <<'YAML'
---
name: easydev
description: >-
  统一处理编码需求、功能实现与代码审查。
metadata:
  version: "'0.14.5'"
---
YAML

cat > "$FIXTURES/unclosed-quote.md" <<'YAML'
---
name: easydev
description: >-
  统一处理编码需求、功能实现与代码审查。
metadata:
  version: "0.14.5
---
YAML

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "  OK: $*"
}

extract() {
  local mode="$1" fixture="$2"
  if [ "$mode" = "no-yaml" ]; then
    PYTHONPATH="$TMP_DIR/no-yaml" python3 "$EXTRACTOR" "$FIXTURES/$fixture"
  else
    python3 "$EXTRACTOR" "$FIXTURES/$fixture"
  fi
}

expect_version() {
  local mode="$1" fixture="$2" expected="$3" output
  output="$(extract "$mode" "$fixture")" \
    || fail "[$mode] $fixture 应通过却被拒绝"
  [ "$output" = "$expected" ] \
    || fail "[$mode] $fixture 提取版本为 $output，预期 $expected"
  pass "[$mode] $fixture -> $expected"
}

expect_rejected() {
  local mode="$1" fixture="$2" message="$3" status=0
  extract "$mode" "$fixture" > "$TMP_DIR/stdout" 2> "$TMP_DIR/stderr" || status=$?
  [ "$status" -ne 0 ] || fail "[$mode] $fixture 未被拒绝（输出 $(cat "$TMP_DIR/stdout")）"
  grep -qE "$message" "$TMP_DIR/stderr" \
    || fail "[$mode] $fixture 的错误信息缺少「$message」: $(cat "$TMP_DIR/stderr")"
  pass "[$mode] $fixture 被拒绝"
}

run_branch() {
  local mode="$1"
  echo "=== 版本提取：$mode 分支 ==="
  expect_version "$mode" valid.md 0.14.5
  expect_version "$mode" description-version.md 0.14.5
  expect_rejected "$mode" duplicate-version.md "重复键"
  expect_rejected "$mode" misplaced-version.md "metadata"
  expect_rejected "$mode" description-only-version.md "metadata.version"
  expect_rejected "$mode" quoted-duplicate-version.md "重复键"
  expect_rejected "$mode" escaped-duplicate-version.md "转义|重复键"
  expect_rejected "$mode" nested-quotes.md "引号|不是合法 YAML|metadata.version"
  expect_rejected "$mode" unclosed-quote.md "引号|不是合法 YAML|metadata.version"
}

run_branch no-yaml

if python3 -c 'import yaml' >/dev/null 2>&1; then
  run_branch yaml
else
  echo "=== 版本提取：PyYAML 分支 ==="
  echo "  提示: 未安装 PyYAML，跳过 PyYAML 分支（CI 会安装并执行）"
fi

echo "=== 版本提取门禁用例全部通过 ==="
