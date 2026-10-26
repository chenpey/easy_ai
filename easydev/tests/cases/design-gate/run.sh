#!/usr/bin/env bash
# normal 转 full，以及独立设计环节的门控、d/3、r/5、施工闸与停止条件。
# 只验证规则文本里可判定的部分；模型是否遵守规则、真实宿主的分派与实例隔离
# 不在本载体范围内，只能靠 tests/behavior.md 的 B60/B61 行为回放。
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$CASE_DIR"
python3 verify.py
