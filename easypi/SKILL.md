---
name: easypi
description: >-
  当用户要求调用 easypi、或通过 Antigravity（agy）把开发任务分派给 Pi 走 easydev gate 模式完成设计审查与编码实现时使用。
  采用“agy 设计 → Pi 走 easydev gate 审设计并开发 → agy 开发终审”的闭环流程。
metadata:
  version: "0.3.1"
---

# easypi

本流程专用于 **Antigravity（agy）分派任务到 Pi 走 `easydev` `gate` 模式**：agy 负责设计与开发终审，Pi 负责设计审查（`d/3`）与唯一代码实现。`gate` 的两处例外（`d/3` 由后续实现者执行、终审由设计方执行）由 `easydev` 规则定义，报告中须披露。

触及 `easydev` 高风险边界（鉴权、支付、敏感数据、并发、破坏性操作、密码学、CI 与供应链、生产日志等）的任务不适用本流程：改按 `full` 保留内部独立审查，或经用户明确接受未覆盖风险后按 gate 执行并披露。

## 1. 准备与设计（agy）

1. 运行 `git status --short --untracked-files=all </dev/null` 记录基线，保护用户未提交的改动。
2. 明确目标、范围与验收标准；高代价歧义先向用户澄清。
3. **冻结设计**：写清目标文件、接口契约、数据流、失败路径与测试策略并与用户确认；按宿主规则（`agent-rules/antigravity.md`）落盘到会话 scratch（`<appDataDir>/brain/<conversation-id>/scratch/`，不写 `/tmp`）并冻结为 `v1`（修订升版），不写入产品仓库。该文件是设计唯一事实源，Pi 只核对首行版本并只读审查。
4. **写 prompt 文件**：按 [README 模板](README.md#prompt-模板) 写成同一 scratch 下的 `prompt.md`，只放设计路径、验收标准与约束；设计正文不内联到命令行（宿主钩子会把双引号内的反引号与 `$(...)` 当命令复核）。
5. 工作目录由调用方从外部带入，本流程不指定；生成唯一会话 ID（如 `easypi-YYYYMMDD-HHMMSS`），返工时在同一目录用同一 `--session-id` 续接。

## 2. 分派 Pi 走 `easydev gate`（agy -> pi）

在调用方工作目录下用绝对路径调用 [run_pi.sh](scripts/run_pi.sh)，第二参数是 prompt 文件（内部执行 `pi -p --session-id ... </dev/null`）：

```bash
bash "$HOME/.gemini/config/skills/easypi/scripts/run_pi.sh" "<session-id>" "<prompt-file>" --model "<provider/id>" </dev/null
```

- **设计分歧处理**：Pi 停在施工闸前返回分歧时，agy 结合用户意见修订设计（升版）并同步 `prompt.md` 与设计路径，保持同一工作目录与 `--session-id` 再次调用；修订重审计入 `d/3`，累计 3 轮用尽即停并报用户。
- **高风险与硬拦**：Pi 发现任务实际触及高风险边界时立即停写回报；`pi -p` 无 UI，`force_ask` 级命令（`rm -rf`、`git reset/restore/clean/checkout --`、`commit/push`）会被钩子直接阻断，需要此类操作时交 agy 经用户确认执行。
- **中断恢复**：超时或异常中断时，先查 `git status --short --untracked-files=all </dev/null` 与 `git diff`，再决定续跑或经用户确认后清理。按需追加 `--thinking <level>`；需加载受信任的项目级 `.pi/` 资源时追加 `--approve`；Pi 运行期间 agy 不修改工作区。

## 3. 开发终审（agy）

1. **核对改动**：先 `git status --short --untracked-files=all </dev/null`，逐个读取所有未跟踪新增文件全文，再查 `git diff`，确认无越界改动、未偏离过闸设计、未误删用户修改。
2. **核验证据**：确认 Pi 已加载 `easydev`（`gate`）、设计审查无分歧，核实实际验证命令与输出；关键检查缺失时由 agy 补跑。
3. **对照终审**：对照冻结设计审查正确性、边界处理、调用方与契约同步、测试真实性（无空断言或过度 mock）；报告注明审查实例与范围。

## 4. 返工或结束

- **有缺陷（至多返工 2 轮）**：同一工作目录与 `--session-id` 再次调用，附文件路径、行号与修改要求，修完后由 agy 复审；需改设计时先修订并重走 `gate` 设计审查。
- **达到上限**：累计 2 轮返工（或同根因 3 次修复）仍未通过时暂停，汇报根因、阻塞点、当前 `git status` 与已/未验证项。
- **通过**：汇总设计要点、Pi 设计审查结论、改动内容、验证结果与 agy 终审结论。
