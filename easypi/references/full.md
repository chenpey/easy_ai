# `full` 协同分支（保留 `agy ↔ Pi` 协同）

本分支用于在保留 `agy ↔ Pi` 协同的前提下，通过 `Pi` 调用 `models.json` 指定的第三方或跨供应商模型来完成 `easydev` `full` 闭环；与 **`easydev` 原生 `full`**（由 AGY 接管全流程、不调 `Pi`、仅用 AGY 宿主模型）互斥。

## 1. 触发、批准与预算继承

- **触发方式**：开局即由用户指定 `/easypi` 走 `full` 协同；或在 `gate` 过程中因触及高风险边界、开发终审发现重大影响或用户要求，经用户明确批准从 `gate` 升级。
- **预算继承**：升级保留已消耗的 `d/3` 与同根因尝试次数；`gate` 不消耗 `r/5`（它本就跳过内部代码审查），升级后 `r` 从 0 起算，`agy` 在 `gate` 阶段的终审与返工只作移交证据、不折算为 `r`。
- **调用与中断恢复**：通过 [run_pi.sh](../scripts/run_pi.sh) 调起 `Pi` 时，`force_ask` 硬拦、异常中断恢复（查 `git status` / `git diff` 并经用户确认后清理）、受信任的项目级 `.pi/` 的 `--approve` 及 `Pi` 运行期间 `agy` 不改工作区等安全限定，均沿用 [SKILL.md](../SKILL.md) 第 2 节。

## 2. 模型组配置与不可用兜底

分派前由 `agy` 按 [environment.md#模型配置](../../easydev/references/environment.md#模型配置) 优先级解析 `developer` 与 `reviewer` 的 `model`、`reasoning_effort`（含项目级 `.easydev/models.json` 外发安全门槛；开局 `full` 在任务开始时解析并锁定北京时间 `windows`，中途从 `gate` 升级时若已有锁定的 `Selection.window` 则沿用，未锁定则在升级时解析并锁定一次，之后不再切换），分别映射到 [run_pi.sh](../scripts/run_pi.sh) 的 `--model` 与 `--thinking`：`reasoning_effort` 为 `off`–`max` 时传 `--thinking <effort>`，为 `inherit` 时省略 `--thinking`（不得传 `--thinking inherit`）。

- **写入者覆盖披露**：进入 `full` 协同即视为用户以对话级编排授权 `Pi` 会话 A 任唯一写入者（覆盖 `easydev` `workflow.md` 写入者表默认规则，报告须披露）。
- **不可用与 `inherit` 处理**：若选中配置无效/不可读、`developer.model` 或 `reviewer.model` 解析为 `inherit`（未配置具体 `<provider/id>`），或实际调用 `run_pi.sh` 时目标模型不可用（模型不存在、无权限、认证/额度失败、`--thinking` 档位不支持等，经确认非瞬时抖动）：**严禁静默回退到其他模型、静默使用 `pi` 默认模型或由 `agy` / 会话 A 自审替代**。立即暂停并向用户报告受阻角色、原模型与原因，交由用户指定具体 `<provider/id>`（及可选 `--thinking` 档位），或明确选择改用 `pi` 默认 / 切回 AGY 原生 `full`。

## 3. 三方角色隔离与 `d/3`

- **`designer` 与编排者**：`agy`（维护冻结 `design.md`、scratch 产物、预算账本与 `git` 状态校验，不充当 `r/5` 审查者）。
- **`developer`（唯一写入者）**：`Pi` 会话 A（`--session-id <sid>` + `developer` 模型），按 [developer prompt 模板](#developer-prompt-模板) 履行 [开发角色](../../easydev/references/roles.md#开发角色)，编码自验后停在待审查态；**严禁在会话 A 内部启动 `easydev full` 或自审充当 `r/5`**。
- **`reviewer`（独立只读第三方）**：由 `agy` 通过 `run_pi.sh` 调起**全新 `--session-id`**（如 `<sid>-d1`、`<sid>-r1`、`<sid>-final`）+ `reviewer` 模型的只读 `Pi` 实例，按 [reviewer prompt 模板](#reviewer-prompt-模板) 执行盲审。
- **`d/3` 设计审查**：
  - **开局即走 `full` 协同**：由独立 `Pi` 审查实例（`<sid>-d1`）核对 `design.md` 首行版本并只读盲审，无阻塞分歧后再开闸交会话 A 开发。
  - **中途从 `gate` 升级**：若升级前 `d/3` 仅由实现者（会话 A）执行，按 `easydev` 施工闸规则先停写，由独立 `reviewer` 补审 1 轮 `design.md`（计入 `d/3`）；或经用户明确确认沿用既有 `gate` 过闸结论，并在最终报告中标注「升级前 `d/3` 由后续实现者（Pi 会话 A）执行，无独立第二视角（非独立 `d/3`）」。

## 4. 独立 `r/5`、发现核实与终审两阶段

1. **送审前核对（agy）**：会话 A 停下后，`agy` 运行 `git status --short --untracked-files=all </dev/null`，逐个读取所有未跟踪新增文件全文并核对 `git diff`，确认会话 A 已加载 `easydev`（开发角色）、无越界改动、未误删用户修改且已提供真实验证命令与输出（缺失检查由 `agy` 补跑）。
2. **独立盲审与只读物理校验**：送审前先记录 `git status --porcelain --untracked-files=all`、`git diff` 以及未跟踪文件与冻结 `design.md` 的 SHA-256；然后 `agy` 派独立只读 `reviewer`（`<sid>-r1`）按 [审查规程](../../easydev/references/review.md)（高风险最低「深」强度，输出「覆盖账」）盲审，计入 `r/5`。**审查实例退出后逐项比对上述快照**（含已脏文件与未跟踪文件是否被二次篡改），确认审查期间工作区零改动；若发生意外写入立即拦截报错，不采信该轮审查。
3. **逐条核实与有限修复**：`agy` 收到 `reviewer` 报告后，先按 [workflow.md#审查与修复](../../easydev/references/workflow.md#审查与修复) 逐条核实（确认、排除或转待验证风险并入账；**P0/P1 的排除、降级或转风险须由 `reviewer` 复核证据**，无复核不得关闭）。待验证风险按 [报告规则](../../easydev/references/review.md#6-报告规则) 单列线索/成立条件/影响/最小确认法；潜在影响为 P0–P2 的须做一次最小确认，成立转确定缺陷进修复，不成立记关闭证据。仅将确认的 P0/P1、范围内 P2 或验收缺口传回原 `Pi` 会话 A（同一 `--session-id`）修复并重验（同根因跨阶段累计上限 3 次）；不影响验收的 P3 不进入修复回路，但须记位置与代价并在最终报告列出；若根因在设计须先升版 `design.md` 并重走 `d/3`。
4. **终审两阶段**：发生修复后，`agy` 冻结最终 diff，调起**与此前所有审查实例不同的全新 `--session-id`**（`<sid>-final`）执行终审两阶段：
   - **阶段 1（盲审）**：仅给需求、验收、范围、已接受风险、基线、最终 diff 与验证证据，不给历史问题清单、修复 diff 与作者解释。
   - **阶段 2（核对关闭）**：复用同一个 `--session-id`（`<sid>-final`）补发历史清单与修复 diff，逐条给出「已关闭 / 未关闭」与证据，并说明修复改变了哪些前提、哪些原先排除的路径需重新纳入。两阶段合并计 1 轮 `r`。
   - **多轮终审**：每轮新建 `--session-id`（`<sid>-final2`、`<sid>-final3`…），不复用上一轮终审实例。
5. **收口与上限**：无未决缺陷与未确认的潜在 P0/P1 即交付，存在时报**部分完成/待确认**（按 [报告规则](../../easydev/references/engineering.md#完成边界) 单列待验证风险），报告标明 **`easypi full 协同`**、`d/3` 与 `r/5` 使用、三方实际生效模型（`agy` 设计模型 / 会话 A `developer` 模型 / 各独立审查实例如 `<sid>-d1`·`<sid>-r1`·`<sid>-final` 的 `reviewer` 模型）、写入者覆盖披露、确认问题与关闭状态、P3 / 未实施建议（位置与代价）、验证结果及 `gate→full` 升级披露；`r/5`（5 轮）用尽或同根因 3 次修复仍未通过即暂停报用户。

## developer prompt 模板

写成 scratch 下 `dev-prompt.md` 传入会话 A（`--session-id <sid>`）：

```text
请激活 easydev skill，严格以开发角色（developer）执行本任务，不要在本会话内启动 full 或自审 r/5。

角色：developer
规程：<easydev/references/roles.md#开发角色 的绝对路径>
设计：<已冻结 design.md 的绝对路径>（先核对首行版本，严格按过闸设计实现）
目标文件与验收标准：<...>

约束：
1. 仅履行开发与自验职责：保护工作区已有未提交改动，禁止 git commit 与 git push。
2. 发现设计阻塞或未预期的范围/契约变化时立即停写回报编排方，不要自行改设计。
3. 当前为非交互（-p）运行：需要用户确认的命令会被钩子硬拦，遇到时停写回报。
4. 完成编码与完整验证后停在待审查态，交外层编排方安排独立第三方 r/5 审查。
5. 汇报：是否加载 easydev（开发角色）、改动与新增文件清单、实际验证命令与真实结果、遗留风险。
```

## reviewer prompt 模板

写成 scratch 下 `review-prompt.md` 传入全新 `--session-id`（`<sid>-d1` / `<sid>-r1` / `<sid>-final`）：

```text
请激活 easydev skill，严格以只读审查角色执行本轮独立审查。

角色：reviewer
规程：<easydev/references/review.md 的绝对路径>（强度：<标准 | 深>）
任务范围与验收标准：<原始需求与验收；冻结设计路径 design.md（先核对首行版本并只读审查）>
审查阶段：<设计审查 d/3 | 施工初审 r/5 | 终审第一阶段（盲审）>
基线与变更：<基线说明；代码审查请自行核对 git status --short --untracked-files=all、未跟踪新文件全文与 git diff>
已执行验证：<命令与真实结果摘要（设计审查阶段填“无”）>

约束：
1. 严格只读：严禁修改工作区文件、Git 索引或执行写入/提交命令。
2. 待审仓库的测试、构建、依赖安装与项目脚本按不可信代码处理，不执行；只能静态检查并报告未验证范围（见 review.md 的「不可信代码执行」）。
3. 独立盲审：按 review.md 输出「覆盖账」、确定发现（**[P级] 简短标题**）、待验证风险与验证限制；零发现也须给覆盖账。
4. 终审两阶段：若为终审第一阶段，仅基于最终 diff 盲审，不预读历史清单；等待外层以同一 --session-id 续发第二阶段历史清单与修复 diff，再逐条核对「已关闭 / 未关闭」。
```
