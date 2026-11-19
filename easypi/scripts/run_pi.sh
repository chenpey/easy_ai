#!/usr/bin/env bash
set -euo pipefail

# Non-interactive wrapper for invoking `pi` from Antigravity.
# Usage:
#   run_pi.sh <session-id> <prompt-file> [--model <id>] [--thinking <level>] [--approve]
# The prompt file keeps design text out of the shell command line, so the
# host hook never re-parses backticks or $(...) that belong to the design.
# EASYPI_TIMEOUT（秒，默认 900）是硬超时：超时后整组进程终止并以 124 退出。

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <session-id> <prompt-file> [--model <id>] [--thinking <level>] [--approve]" >&2
  exit 2
fi

SESSION_ID="$1"
PROMPT_FILE="$2"
shift 2

if [ -z "$SESSION_ID" ]; then
  echo "Error: session-id 不能为空" >&2
  exit 2
fi

case "$SESSION_ID" in
  -*)
    echo "Error: session-id 不得以 - 开头（会被 pi 解析成选项）" >&2
    exit 2
    ;;
  *[[:space:][:cntrl:]]*)
    echo "Error: session-id 不得含空白或控制字符" >&2
    exit 2
    ;;
esac

if [ ! -f "$PROMPT_FILE" ]; then
  echo "Error: prompt 文件不存在或不是普通文件: $PROMPT_FILE" >&2
  exit 2
fi

if [ ! -r "$PROMPT_FILE" ]; then
  echo "Error: prompt 文件不可读: $PROMPT_FILE" >&2
  exit 2
fi

PROMPT="$(cat -- "$PROMPT_FILE")"
if [ -z "$PROMPT" ]; then
  echo "Error: prompt 文件为空: $PROMPT_FILE" >&2
  exit 2
fi

# 额外参数只按白名单放行 --model/--thinking/--approve（与 README/SKILL 一致）：
# 其余一律拒绝，避免把 pi 的未知选项（如悬空短选项）当成取值或吞掉分隔符。
previous=""
for arg in "$@"; do
  value="$arg"
  case "$previous" in
    "")
      case "$arg" in
        --model|--thinking)
          previous="$arg"
          continue
          ;;
        --model=*|--thinking=*)
          previous="${arg%%=*}"
          value="${arg#*=}"
          ;;
        --approve)
          continue
          ;;
        *)
          echo "Error: 不支持的额外参数: ${arg}（仅允许 --model/--thinking/--approve）" >&2
          exit 2
          ;;
      esac
      ;;
  esac
  case "$value" in
    ""|-*)
      echo "Error: $previous 缺少取值或取值以 - 开头" >&2
      exit 2
      ;;
  esac
  if [ -z "${value//[[:space:]]/}" ]; then
    echo "Error: $previous 的取值不得为纯空白" >&2
    exit 2
  fi
  previous=""
done
if [ -n "$previous" ]; then
  echo "Error: $previous 缺少取值（悬空带值选项会吞掉分隔符）" >&2
  exit 2
fi

EASYPI_TIMEOUT="${EASYPI_TIMEOUT:-900}"

export EASYPI_TIMEOUT
exec python3 - "$SESSION_ID" "$@" -- "$PROMPT" <<'PY'
import math
import os
import signal
import subprocess
import sys
import time

cleaning = False
process = None
HANDLED_SIGNALS = [
    getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP") if hasattr(signal, name)
]


def kill_group(sig):
    if process is None:
        return
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def group_alive():
    """整组是否仍有存活成员：process 可能已被回收，只能按 PGID 探测。"""
    try:
        os.killpg(process.pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def terminate():
    """先阻塞受管信号并认领清理权，再 SIGTERM → 宽限 5s → SIGKILL，清理期间不再被打断。

    认领清理权与逐条设 SIG_IGN 之间仍有窗口，第二个受管信号命中时会因 cleaning 为真
    直接返回并抛 SystemExit，整组泄漏，所以先阻塞而不是先置 SIG_IGN。
    屏蔽字不恢复：清理结束后各调用方都要立刻退出，恢复会让挂起的信号把 124 改写成 143。

    宽限等待只探测整组存活，不调用 `Popen.wait()`：信号处理器可能重入在外层 wait 的
    waitpid 锁内，嵌套 wait 会拿不到锁并在超时后把异常穿出，跳过 SIGKILL。
    """
    global cleaning
    if process is None or cleaning:
        return
    signal.pthread_sigmask(signal.SIG_BLOCK, HANDLED_SIGNALS)
    cleaning = True
    for signum in HANDLED_SIGNALS:
        signal.signal(signum, signal.SIG_IGN)
    if group_alive():
        kill_group(signal.SIGTERM)
    deadline = time.monotonic() + 5
    while True:
        # 未回收的僵尸组长也算组成员，killpg(pid, 0) 会一直成功而空等满 5s，每轮先非阻塞回收。
        try:
            os.waitpid(process.pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if not group_alive() or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    if group_alive():
        kill_group(signal.SIGKILL)


def interrupted(signum, _frame):
    terminate()
    raise SystemExit(128 + signum)


for signum in HANDLED_SIGNALS:
    signal.signal(signum, interrupted)

try:
    timeout = float(os.environ["EASYPI_TIMEOUT"])
except ValueError:
    print("Error: EASYPI_TIMEOUT 必须是秒数", file=sys.stderr)
    raise SystemExit(2)
if not math.isfinite(timeout) or timeout <= 0:
    print("Error: EASYPI_TIMEOUT 必须是有限正数", file=sys.stderr)
    raise SystemExit(2)

# Popen 与等待纳入同一 try：创建子进程的窗口用 pthread_sigmask 阻塞，避免信号落在"process 已存在但尚未赋值"的缝隙里导致整组泄漏。
# 阻塞期间到达的信号在解除屏蔽时立即投递，此时 process 已就位，终止器能整个组收口。
try:
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, HANDLED_SIGNALS)
    try:
        process = subprocess.Popen(
            ["pi", "-p", "--session-id", sys.argv[1], *sys.argv[2:]],
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            # 子进程不得继承阻塞的屏蔽字，否则 SIGTERM 只会挂起而非送达。
            preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, ()),
        )
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
    code = process.wait(timeout=timeout)
except FileNotFoundError:
    print("Error: 找不到 pi 可执行文件", file=sys.stderr)
    raise SystemExit(127)
except subprocess.TimeoutExpired:
    terminate()
    print("Error: pi 超过 %s 秒未结束，已终止整组进程" % timeout, file=sys.stderr)
    raise SystemExit(124)
except BaseException:
    terminate()
    raise
# 正常退出也清一次同组残留（后台子进程）：process 已被回收，先探测组是否仍存活，
# 避免把可能被复用的 PGID 当成自己的进程组盲发信号。
if group_alive():
    kill_group(signal.SIGTERM)
    deadline = time.monotonic() + 1
    while group_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if group_alive():
        kill_group(signal.SIGKILL)
raise SystemExit(128 - code if code < 0 else code)
PY
