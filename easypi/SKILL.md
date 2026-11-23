---
name: easypi
description: >-
  当用户要求调用 easypi、或通过 Antigravity（agy）分派 Pi 走 easydev gate 模式或 full 协同分支完成设计审查、编码与独立审查时使用。
metadata:
  version: "0.8.0"
---

# easypi

专用于 **Antigravity（agy）分派 Pi 走 `easydev` `gate`**：agy 设计与终审，Pi 审设计（`d/3`）并唯一实现。`gate` 两处例外（`d/3` 由实现者执行、终审由设计方执行）须在报告披露。

触及 `easydev` 高风险边界或重大外部契约/持久数据影响不适用 `gate`：交 agy 接管原生 `full`，或走第 5 节 `full` 分支。

## 1. 准备与设计（agy）

1. 运行 `git status --short --untracked-files=all </dev/null` 并记 `git rev-parse HEAD`；任务相关脏/未跟踪文件的首次原内容按[敏感快照](references/gate.md#baseline-敏感快照)存 `baseline/`。
2. 明确目标、范围与验收标准；高代价歧义先向用户澄清。
3. **冻结设计**：写清目标、契约、数据流、失败路径与测试策略并与用户确认；落盘到会话 scratch（`<appDataDir>/brain/<conversation-id>/scratch/`，禁 `/tmp`）冻结为 `v1`（修订升版），不入产品仓库；作唯一事实源，Pi 核首行版本只读审查。
4. **写 prompt 文件**：按 [gate 模板](references/gate.md#prompt-模板) 写同一 scratch 下 `prompt.md`，只放设计路径、冷启动摘要、验收与约束，不内联设计正文（防钩子复核反引号与 `$(...)`）。
5. 工作目录由调用方带入；生成唯一会话 ID（如 `easypi-YYYYMMDD-HHMMSS`），返工时在同一目录用同一 `--session-id` 续接。

## 2. 分派 Pi 走 `easydev gate`（agy -> pi）

在调用方工作目录下调 [run_pi.sh](scripts/run_pi.sh)，第二参数为 prompt 文件（`pi -p --session-id ... </dev/null`；`EASYPI_TIMEOUT` 默认 `0` 不硬杀，正数超时退出码 `124`；会话 ID 不得以 `-` 开头，额外参数限白名单）：

```bash
bash "$HOME/.gemini/config/skills/easypi/scripts/run_pi.sh" "<session-id>" "<prompt-file>" </dev/null
```

- **设计分歧处理**：Pi 停在施工闸前返回分歧时，agy 按用户意见升版设计并同步 `prompt.md`，同目录 `--session-id` 重调并显式传开工锁定的 `--model`/`--thinking`；修订重审计入 `d/3`，累计 3 轮用尽即停并报用户。
- **模型参数**：`gate`/`full` 派发新 `Pi` 按 `models.json`（含 `windows` 时段）解析 `developer`，未显式 `--model` 时注非 `inherit` 的 `--model`/`--thinking`；`Pi` 内部按 `easydev gate` 不按 `models.json` 委派 `developer`/`reviewer`，不猜模型。
- **高风险与硬拦**：触及高风险或重大外部契约/持久数据影响即停写回报；`pi -p` 无 UI，`force_ask` 级命令（非临时/缓存目标的 `rm -rf`、`git reset --hard`/`clean -f`/整仓 `restore`·`checkout`/`stash drop`·`clear`/`branch -D`/`commit`/`push`、敏感凭据目录等，见守卫）会被硬拦，需此操作交 agy 确认执行。
- **15分钟巡检与恢复**：以低 `WaitMsBeforeAsync` 异步派发拿到 `TaskId` 后立即设 900 秒 `schedule`，到点按[巡检规程](references/gate.md#15-分钟巡检与插话询问)查进展，停滞再插话（重调传锁定参数）；异常中断重调亦同，先查 `git status --short --untracked-files=all </dev/null` 与 `git diff HEAD`。按需加 `--approve`；Pi 运行期 agy 不改工作区。

## 3. 开发终审（agy）

1. **核对改动**：先 `git status --short --untracked-files=all </dev/null`，逐个读取所有未跟踪新增文件全文，再查 `git diff HEAD`（无提交用 `git diff --cached` 与 `git diff`），确认无越界改动、未偏离过闸设计、未误删用户修改。
2. **核验证据**：确认 Pi 已加载 `easydev`（`gate`）、设计审查无分歧，核实验证命令与输出；关键检查缺失时由 agy 补跑。
3. **对照终审**：对照冻结设计审查正确性、边界处理、调用方与契约同步、测试真实性（无空断言或过度 mock）；报告注明审查实例与范围。

## 4. 返工或结束

- **有缺陷（至多返工 2 轮）**：同一工作目录与 `--session-id` 再次调用（同传锁定 `--model`/`--thinking`），附文件路径、行号与修改要求，修完后由 agy 复审；改设计须先修订并重走 `gate` 设计审查。
- **达到上限**：返工达 2 轮或同根因 3 次修复任一上限即暂停，汇报根因、阻塞点、当前 `git status` 与已/未验证项。
- **通过**：汇总设计要点、Pi 设计审查结论、改动、验证结果、文档校准状态、`gate` 两处例外、遗留与未验证项及 agy 终审结论。

## 5. `full` 协同分支（保留 `agy ↔ Pi` 协同）

开局即指定或经用户批准从 `gate` 升级时，完整规程（模型组、三方隔离、发现核实与终审两阶段）及 `developer`/`reviewer` 模板按 [references/full.md](references/full.md) 执行。
