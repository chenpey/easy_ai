#!/usr/bin/env bash
# B65 的可执行断面：正文 20 条上限、附录完整保留与分片覆盖披露。
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$CASE_DIR"
python3 verify.py
