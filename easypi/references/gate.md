# `gate` prompt 模板与敏感快照

`gate` 由 `agy` 冻结 `design.md`，派 `Pi` 审设计（`d/3`）并编码自验，最后交 `agy` 开发终审。

## 使用方式

模板写入同一 scratch 下 `prompt.md` 作 [run_pi.sh](../scripts/run_pi.sh) 第二参数；只含设计路径、冷启动摘要（`≤1KB`，仅 `HEAD`、`git status --short`、受影响目录与验证入口，不含凭证与正文）、验收与约束，不内联设计正文。

## prompt 模板

```text
请激活 easydev skill，严格按 gate 模式执行本任务。

设计：<冻结 design.md 绝对路径>（先核首行版本，只读审查）
冷启动摘要：<HEAD SHA；git status --short；受影响目录与验证入口（≤1KB，不含凭证与正文，不替代自行核对）>
目标文件与验收标准：<目标路径（已知切入点附符号或 #L起-止）与可观察验收>

约束：
1. 先按 gate 审设计（d/3）：有 P0/P1、范围内 P2、验收缺口或分歧时严禁写代码，停闸输出分歧待修订。
2. 无分歧后按设计编码自验；保护已有未提交改动，禁止 git commit/push/add。
3. 跳过内部 r/5 代码审查，停待终审态交外层 Antigravity 终审。
4. 发现触及 easydev 高风险边界或重大外部契约/持久数据影响立即停写回报，不自行改走 full 或跳过 r/5。
5. 非交互（-p）运行：需确认命令会被钩子硬拦，遇到即停写回报。
6. 汇报：是否加载 easydev（gate）、设计审查结论、改动与新增清单、验证命令与真实结果、遗留风险。
```

设计审查须保留 `**[建议] 简短标题**` 通道（无失败路径不给 P 级，外层逐条记「接受 / 拒绝」）。

## baseline 敏感快照

`baseline/` 只存首次原内容；含凭证或个人数据的文件**严禁写入 scratch 或 artifact 目录**，须放私有临时目录（目录 `0700`、文件 `0600`）并在账本与报告记录路径；无法确认保护条件暂停写入。详见 [基线与敏感快照](../../easydev/references/environment.md#基线与敏感快照) `#L39-L46`。

## 15 分钟巡检与插话询问

1. 以低 `WaitMsBeforeAsync`（如 `500`）异步派发 `run_pi.sh` 拿到 `TaskId` 后，立即用 `schedule` 设 `DurationSeconds=900` 定时器（`TimerCondition` 填任务 `TaskId` 或 `'any'`）。
2. 到点仍在运行时：
   - 用 `manage_task`（`status`）查近期输出，并查 `git status --short --untracked-files=all </dev/null` 与 `git diff --stat`。若日志或工作区持续推进，**不中断任务**，直接重设 900 秒定时器继续等待。
   - **停滞/不明时插话**：先 `manage_task`（`kill`）暂停 `pi -p`（走 `SIGTERM` 清理整组），再用 `pgrep -f "[p]i -p --session-id <sid>" </dev/null`（方括号防自匹配父 shell）确认输出为空；若有残留则 `kill -TERM` 后复查确认其已退出（防同会话双写），然后同 `--session-id` 调 `run_pi.sh` 询问（「已运行 15 分钟，请简报进度与是否卡住，勿改代码，直接回答。」）。
   - **去留**：正常推进 → 同 `--session-id` 异步发「继续执行原任务」并重设 900 秒定时器；卡死/阻塞 → 终止并汇报现场；无法判断 → 暂停交用户裁决（附回复、日志与 `git status`）。
